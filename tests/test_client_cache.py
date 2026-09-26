"""The cached client must be closed on invalidation, and keyed on everything the SDK reads.

`TypeSafeClient` owns an `httpx2.Client` with a connection pool. The old
`_reset_client_cache` just dropped the reference, so the suite leaked a pool per
test and the server leaked one per `JEV_MCP_MOCK` toggle. The cache key also
omitted `TYPESAFE_BASE_URL` and `TYPESAFE_DEFAULT_MODEL`, both of which the SDK
reads from the environment — so a mid-process change left a live client aimed
at the old host.
"""

import threading

import pytest

import jev_engine
from config import _reset_config_cache
from jev_engine import _reset_client_cache, get_client


class _SpyClient:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.close_calls = 0
        self.raise_on_close = False

    def close(self):
        self.close_calls += 1
        if self.raise_on_close:
            raise RuntimeError("close failed")

    def system_one(self, state, questions, model=None, **kwargs):
        return None


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setenv("JEV_MCP_MOCK", "0")
    monkeypatch.setenv("TYPESAFE_API_KEY", "key-client-cache-0123456789")
    monkeypatch.setenv("JEV_MCP_TIMEOUT_MS", "30000")
    _reset_config_cache()
    _reset_client_cache()
    created = []

    def _factory(*args, **kwargs):
        client = _SpyClient(*args, **kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(jev_engine, "TypeSafeClient", _factory)
    return created


def test_repeated_cycles_close_every_client(live):
    for _ in range(200):
        get_client()
        _reset_client_cache()
    assert len(live) == 200
    assert sum(c.close_calls for c in live) == 200


def test_close_is_never_called_on_the_live_client(live):
    client = get_client()
    assert get_client() is client
    assert client.close_calls == 0


def test_a_failing_close_does_not_propagate_and_leaves_no_stale_client(live, monkeypatch):
    client = get_client()
    client.raise_on_close = True
    _reset_client_cache()          # must not raise
    assert jev_engine._cached_client is None
    assert jev_engine._cached_client_key is None

    # And the next call still gets a working client.
    fresh = get_client()
    assert fresh is not client


def test_the_replaced_client_is_closed_on_a_cache_key_change(live, monkeypatch):
    first = get_client()
    monkeypatch.setenv("TYPESAFE_API_KEY", "key-client-cache-9876543210")
    _reset_config_cache()
    second = get_client()
    assert second is not first
    assert first.close_calls == 1


def test_entering_mock_mode_closes_the_live_client(live, monkeypatch):
    client = get_client()
    monkeypatch.setenv("JEV_MCP_MOCK", "1")
    _reset_config_cache()
    assert get_client() is None
    assert client.close_calls == 1


def test_changing_the_base_url_yields_a_new_client(live, monkeypatch):
    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://a.example.com")
    first = get_client()
    assert get_client() is first

    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://b.example.com")
    second = get_client()
    assert second is not first
    assert first.close_calls == 1


def test_a_trailing_slash_is_the_same_base_url(live, monkeypatch):
    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://a.example.com")
    first = get_client()
    monkeypatch.setenv("TYPESAFE_BASE_URL", "  https://a.example.com/  ")
    assert get_client() is first


def test_changing_the_sdk_default_model_yields_a_new_client(live, monkeypatch):
    first = get_client()
    monkeypatch.setenv("TYPESAFE_DEFAULT_MODEL", "some/other-model")
    second = get_client()
    assert second is not first


def test_concurrent_get_client_still_returns_one_instance(live):
    clients = []
    errors = []

    def worker():
        try:
            clients.append(get_client())
        except Exception as e:  # pragma: no cover - surfaced by the assert below
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(clients) == 10
    assert all(c is clients[0] for c in clients)
    assert len(live) == 1, "only one client should ever be built"
