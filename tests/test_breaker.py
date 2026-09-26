"""A provider outage must not cost the full retry budget on every single call.

Without a breaker, a revoked key means one fast non-retryable 401 per call, but
a 5xx or a connection failure pays three attempts plus backoff *every time*, for
as long as the outage lasts — and the plugin pays it again on every message.
"""

import time

import pytest

import jev_engine
from config import _reset_config_cache, get_config
from jev_engine import _Breaker, _reset_breaker, _reset_client_cache
from jev_errors import JevTimeoutError, error_details
from typesafe_sdk import (
    Choice,
    ChoiceAnswer,
    Noul,
    NoulAnswer,
    SystemOneResponse,
    TypeSafeAPIConnectionError,
    TypeSafeAPIError,
    TypeSafeAuthenticationError,
    Usage,
)

QUESTIONS = {
    "target_file": Choice(instructions="pick", criteria={"a": "first", "b": "second"}),
    "is_relevant": Noul(instructions="relevant?"),
}


def _ok_response():
    return SystemOneResponse(
        model="jev-test",
        answers={
            "target_file": ChoiceAnswer(choice="a", probabilities={"a": 0.9, "b": 0.1}, confidence=0.9),
            "is_relevant": NoulAnswer(noul=0.9),
        },
        usage=Usage(input_tokens=5, output_tokens=2),
    )


class _FakeClient:
    """Answers from a script; counts only the calls that actually reached it.

    A scripted entry that is an exception instance is raised, anything else is
    returned as the response.
    """

    def __init__(self, error=None, results=None):
        self.error = error
        self.results = list(results or [])
        self.calls = 0

    def system_one(self, state, questions, model=None, timeout=None, **kwargs):
        self.calls += 1
        if self.results:
            item = self.results.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        if self.error is not None:
            raise self.error
        return _ok_response()

    def close(self):
        pass


def _live(monkeypatch, client, **env):
    monkeypatch.setenv("JEV_MCP_MOCK", "0")
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    _reset_config_cache()
    _reset_client_cache()
    _reset_breaker()
    monkeypatch.setattr(jev_engine, "get_client", lambda: client)


def _conn_error():
    return TypeSafeAPIConnectionError("connection refused")


def _auth_error():
    return TypeSafeAuthenticationError(401, {"error": "unauthorized"}, _headers())


def _server_error():
    return TypeSafeAPIError(503, {"error": "unavailable"}, _headers())


def _headers():
    import httpx2

    return httpx2.Headers()


# ----------------------------------------------------------------------
# Unit behaviour
# ----------------------------------------------------------------------
def test_breaker_starts_closed_and_opens_on_consecutive_failures():
    b = _Breaker(threshold=3, cooldown_s=30.0, auth_cooldown_s=300.0)
    assert b.allow() is True
    b.record_failure(retryable=True)
    b.record_failure(retryable=True)
    assert b.allow() is True, "two failures are below the threshold"
    b.record_failure(retryable=True)
    assert b.allow() is False, "three failures open the circuit"


def test_a_single_success_resets_the_counter():
    b = _Breaker(threshold=3, cooldown_s=30.0, auth_cooldown_s=300.0)
    b.record_failure(retryable=True)
    b.record_failure(retryable=True)
    b.record_success()
    assert b.failures == 0
    b.record_failure(retryable=True)
    assert b.allow() is True


def test_auth_failure_uses_the_longer_cooldown():
    b = _Breaker(threshold=1, cooldown_s=30.0, auth_cooldown_s=300.0)
    b.record_failure(retryable=False)
    assert b.opened_cooldown == 300.0
    assert b.allow() is False
    # 60s later the short cooldown would have expired; the auth one has not.
    b.opened_at = time.monotonic() - 60.0
    assert b.allow() is False
    b.opened_at = time.monotonic() - 301.0
    assert b.allow() is True


def test_only_one_probe_is_admitted_after_the_cooldown():
    b = _Breaker(threshold=2, cooldown_s=10.0, auth_cooldown_s=300.0)
    b.record_failure(retryable=True)
    b.record_failure(retryable=True)
    b.opened_at = time.monotonic() - 11.0
    assert b.allow() is True    # the probe
    assert b.allow() is False   # anyone else is rejected while it is in flight


# ----------------------------------------------------------------------
# Wiring through execute_system_one / _request
# ----------------------------------------------------------------------
def test_three_connection_failures_open_the_breaker_and_the_fourth_never_reaches_the_client(monkeypatch):
    client = _FakeClient(error=_conn_error())
    _live(monkeypatch, client, JEV_MCP_BREAKER_THRESHOLD=3, JEV_MCP_BREAKER_COOLDOWN_S=60)

    for _ in range(3):
        with pytest.raises(JevTimeoutError):
            jev_engine._request("state", QUESTIONS)
    assert client.calls == 3

    with pytest.raises(JevTimeoutError) as exc:
        jev_engine._request("state", QUESTIONS)
    assert client.calls == 3, "the 4th call must not touch the client"

    details = error_details(exc.value)
    assert details["code"] == "TIMEOUT"
    assert details["retryable"] is True
    assert "circuit is open" in details["message"]
    assert "Retry in" in details["message"]


def test_a_probe_after_the_cooldown_closes_the_circuit_on_success(monkeypatch):
    client = _FakeClient(error=_conn_error())
    _live(monkeypatch, client, JEV_MCP_BREAKER_THRESHOLD=3, JEV_MCP_BREAKER_COOLDOWN_S=0.05)

    for _ in range(3):
        with pytest.raises(JevTimeoutError):
            jev_engine._request("state", QUESTIONS)
    assert client.calls == 3

    time.sleep(0.06)
    client.error = None          # the provider recovers
    jev_engine._request("state", QUESTIONS)
    assert client.calls == 4, "exactly one probe got through"

    # Closed again: further calls are unthrottled.
    jev_engine._request("state", QUESTIONS)
    jev_engine._request("state", QUESTIONS)
    assert client.calls == 6
    assert jev_engine._breaker().failures == 0


def test_a_failed_probe_reopens_the_circuit(monkeypatch):
    client = _FakeClient(error=_conn_error())
    _live(monkeypatch, client, JEV_MCP_BREAKER_THRESHOLD=2, JEV_MCP_BREAKER_COOLDOWN_S=0.05)

    for _ in range(2):
        with pytest.raises(JevTimeoutError):
            jev_engine._request("state", QUESTIONS)
    time.sleep(0.06)
    with pytest.raises(JevTimeoutError):
        jev_engine._request("state", QUESTIONS)   # the probe fails
    assert client.calls == 3
    with pytest.raises(JevTimeoutError):
        jev_engine._request("state", QUESTIONS)
    assert client.calls == 3, "still open after a failed probe"


def test_a_single_failure_then_a_success_does_not_open_the_breaker(monkeypatch):
    client = _FakeClient(results=[_conn_error(), _ok_response(), _ok_response()])
    _live(monkeypatch, client, JEV_MCP_BREAKER_THRESHOLD=3, JEV_MCP_BREAKER_COOLDOWN_S=60)

    with pytest.raises(JevTimeoutError):
        jev_engine._request("state", QUESTIONS)
    assert jev_engine._breaker().failures == 1

    # Three more successes would open the circuit if the counter had not reset.
    jev_engine._request("state", QUESTIONS)
    jev_engine._request("state", QUESTIONS)
    jev_engine._request("state", QUESTIONS)
    assert client.calls == 4
    assert jev_engine._breaker().failures == 0


def test_an_auth_error_opens_the_auth_window(monkeypatch):
    client = _FakeClient(error=_auth_error())
    _live(
        monkeypatch,
        client,
        JEV_MCP_BREAKER_THRESHOLD=3,
        JEV_MCP_BREAKER_COOLDOWN_S=30,
        JEV_MCP_AUTH_COOLDOWN_S=600,
    )

    for _ in range(3):
        with pytest.raises(TypeSafeAuthenticationError):
            jev_engine._request("state", QUESTIONS)
    assert client.calls == 3
    assert jev_engine._breaker().opened_cooldown == 600.0

    with pytest.raises(JevTimeoutError) as exc:
        jev_engine._request("state", QUESTIONS)
    assert client.calls == 3
    assert "Retry in" in error_details(exc.value)["message"]


def test_a_server_error_opens_the_short_window(monkeypatch):
    client = _FakeClient(error=_server_error())
    _live(
        monkeypatch,
        client,
        JEV_MCP_BREAKER_THRESHOLD=2,
        JEV_MCP_BREAKER_COOLDOWN_S=30,
        JEV_MCP_AUTH_COOLDOWN_S=600,
    )
    for _ in range(2):
        with pytest.raises(TypeSafeAPIError):
            jev_engine._request("state", QUESTIONS)
    assert jev_engine._breaker().opened_cooldown == 30.0


def test_mock_mode_is_never_throttled(monkeypatch):
    """The offline judge cannot fail, so the breaker must not interfere."""
    monkeypatch.setenv("JEV_MCP_MOCK", "1")
    _reset_config_cache()
    _reset_client_cache()
    _reset_breaker()
    for _ in range(10):
        res = jev_engine._request("state", QUESTIONS)
        assert res is not None


def test_breaker_is_rebuilt_when_the_knobs_change(monkeypatch):
    _live(monkeypatch, _FakeClient(), JEV_MCP_BREAKER_THRESHOLD=3)
    first = jev_engine._breaker()
    assert get_config().breaker_threshold == 3
    assert jev_engine._breaker() is first

    monkeypatch.setenv("JEV_MCP_BREAKER_THRESHOLD", "5")
    _reset_config_cache()
    second = jev_engine._breaker()
    assert second is not first
    assert second.threshold == 5
