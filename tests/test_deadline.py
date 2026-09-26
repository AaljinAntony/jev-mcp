"""JEV_MCP_TIMEOUT_MS must actually bound one tool call end to end.

Regression tests for the finding that the configured timeout bounded nothing:
the client got a 30s *per-attempt* timeout and a `RetryPolicy` built without
`timeout`, which defaults to 30s, so three attempts plus backoff put the worst
case at ~91.5s for a single call — while the plugin was killing the child at 4s.
"""

import time

import pytest

import jev_engine
from config import _reset_config_cache, get_config
from jev_engine import _remaining_seconds, _reset_client_cache, get_client
from jev_errors import JevTimeoutError, error_details
from limits import fit_state
from typesafe_sdk import (
    Choice,
    ChoiceAnswer,
    Noul,
    NoulAnswer,
    RetryPolicy,
    SystemOneResponse,
    TypeSafeAPITimeoutError,
    Usage,
)


def _questions(n_options: int = 2) -> dict:
    return {
        "target_file": Choice(
            instructions="pick one",
            criteria={f"opt_{i}": f"description for option {i}" for i in range(n_options)},
        ),
        "is_relevant": Noul(instructions="is it relevant?"),
    }


QUESTIONS = _questions()


def _ok_response(questions=None):
    labels = list((questions or {}).get("target_file").criteria.keys()) or ["a"]
    chosen = labels[0]
    probs = {label: (0.9 if label == chosen else 0.1 / max(len(labels) - 1, 1)) for label in labels}
    probs[chosen] = 0.9
    return SystemOneResponse(
        model="jev-test",
        answers={
            "target_file": ChoiceAnswer(choice=chosen, probabilities=probs, confidence=0.9),
            "is_relevant": NoulAnswer(noul=0.9),
        },
        usage=Usage(input_tokens=5, output_tokens=2),
    )


class _SleepyClient:
    """A client that burns wall-clock time before answering."""

    def __init__(self, seconds, raises=None):
        self.seconds = seconds
        self.raises = raises
        self.calls = 0
        self.timeouts = []

    def system_one(self, state, questions, model=None, timeout=None, **kwargs):
        self.calls += 1
        self.timeouts.append(timeout)
        time.sleep(self.seconds)
        if self.raises is not None:
            raise self.raises
        return _ok_response(questions)

    def close(self):
        pass


class _DeadlineAwareClient:
    """A client that honours the per-call `timeout` it is handed, like the SDK.

    Without a per-call timeout it falls back to the SDK's own 30s default —
    which is exactly the behaviour that made one call take ~91.5s.
    """

    def __init__(self, client_default_s: float = 30.0):
        self.client_default_s = client_default_s
        self.calls = 0
        self.timeouts = []

    def system_one(self, state, questions, model=None, timeout=None, **kwargs):
        self.calls += 1
        self.timeouts.append(timeout)
        budget = self.client_default_s if timeout is None else timeout
        time.sleep(budget)
        raise TypeSafeAPITimeoutError("simulated deadline")

    def close(self):
        pass


def test_total_call_time_is_bounded_by_the_configured_budget(monkeypatch):
    """The regression test for #12: this took ~3x the budget before.

    The stub sleeps for whatever timeout it is given, so the elapsed time is a
    direct measurement of the deadline the engine passed down. On the old code
    no per-call timeout was passed, the client fell back to 30s, and this test
    hung.
    """
    monkeypatch.setenv("JEV_MCP_MOCK", "0")
    monkeypatch.setenv("JEV_MCP_TIMEOUT_MS", "400")
    _reset_config_cache()
    _reset_client_cache()
    jev_engine._reset_breaker()

    client = _DeadlineAwareClient()
    monkeypatch.setattr(jev_engine, "get_client", lambda: client)

    started = time.perf_counter()
    with pytest.raises(JevTimeoutError):
        jev_engine._request("some state", QUESTIONS)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.9, f"call took {elapsed:.2f}s for a 0.4s budget"
    assert client.timeouts[0] is not None
    assert client.timeouts[0] <= 0.4


def test_overrunning_client_is_reported_as_a_timeout_not_a_decision(monkeypatch):
    """A call that overshoots the budget must not return a decision as if on time."""
    monkeypatch.setenv("JEV_MCP_MOCK", "0")
    monkeypatch.setenv("JEV_MCP_TIMEOUT_MS", "200")
    _reset_config_cache()
    _reset_client_cache()
    jev_engine._reset_breaker()

    client = _SleepyClient(0.35)   # answers, but too late to count
    monkeypatch.setattr(jev_engine, "get_client", lambda: client)

    with pytest.raises(JevTimeoutError):
        jev_engine._request("some state", QUESTIONS)


def test_remaining_budget_is_passed_to_system_one(monkeypatch):
    monkeypatch.setenv("JEV_MCP_MOCK", "0")
    monkeypatch.setenv("JEV_MCP_TIMEOUT_MS", "5000")
    _reset_config_cache()
    _reset_client_cache()

    client = _SleepyClient(0.0)
    monkeypatch.setattr(jev_engine, "get_client", lambda: client)

    jev_engine._request("some state", QUESTIONS)

    assert client.calls == 1
    timeout = client.timeouts[0]
    assert timeout is not None
    # The full budget minus the time already spent fitting the state.
    assert 0 < timeout <= 5.0


def test_remaining_seconds_never_goes_negative(monkeypatch):
    monkeypatch.setenv("JEV_MCP_TIMEOUT_MS", "1000")
    _reset_config_cache()
    cfg = get_config()
    assert _remaining_seconds(time.perf_counter(), cfg) == pytest.approx(1.0, abs=0.05)
    # A `started` far in the past still yields a usable, positive timeout.
    assert _remaining_seconds(time.perf_counter() - 999.0, cfg) == pytest.approx(0.001)


def test_retry_policy_carries_the_total_budget_and_no_timeout_retries(monkeypatch):
    """`RetryPolicy.timeout` defaults to 30s; it must be our budget instead."""
    monkeypatch.setenv("JEV_MCP_MOCK", "0")
    monkeypatch.setenv("TYPESAFE_API_KEY", "key-deadline-0123456789")
    monkeypatch.setenv("JEV_MCP_TIMEOUT_MS", "2500")
    _reset_config_cache()
    _reset_client_cache()

    seen = {}

    class _SpyRetryPolicy(RetryPolicy):
        def __init__(self, **kwargs):
            seen.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(jev_engine, "RetryPolicy", _SpyRetryPolicy)

    client = get_client()
    assert client is not None
    assert seen["timeout"] == 2.5
    assert seen["api_timeout_error"] is False
    assert seen["max_retries"] == 2


def test_always_timing_out_client_raises_timeout_within_budget(monkeypatch):
    monkeypatch.setenv("JEV_MCP_MOCK", "0")
    monkeypatch.setenv("JEV_MCP_TIMEOUT_MS", "2000")
    _reset_config_cache()
    _reset_client_cache()
    jev_engine._reset_breaker()

    err = TypeSafeAPITimeoutError("deadline")
    client = _SleepyClient(0.01, raises=err)
    monkeypatch.setattr(jev_engine, "get_client", lambda: client)

    started = time.perf_counter()
    with pytest.raises(JevTimeoutError):
        jev_engine._request("some state", QUESTIONS)
    elapsed = time.perf_counter() - started

    assert elapsed < 3.0
    details = error_details(JevTimeoutError())
    assert details["code"] == "TIMEOUT"
    assert details["retryable"] is True


def test_mock_mode_reports_an_overrun_instead_of_a_silent_success(monkeypatch):
    """`mock_system_one` is CPU-bound: a tiny budget must not be ignored."""
    monkeypatch.setenv("JEV_MCP_MOCK", "1")
    monkeypatch.setenv("JEV_MCP_TIMEOUT_MS", "1")
    _reset_config_cache()
    _reset_client_cache()

    big_state = "x" * 100_000
    with pytest.raises(JevTimeoutError):
        jev_engine._request(big_state, _questions(250))


def test_client_cache_key_tracks_the_sdk_base_url(monkeypatch):
    """`TYPESAFE_BASE_URL` is read by the SDK, so the client must not outlive it."""
    monkeypatch.setenv("JEV_MCP_MOCK", "0")
    monkeypatch.setenv("TYPESAFE_API_KEY", "key-cache-0123456789abcdef")
    _reset_config_cache()
    _reset_client_cache()

    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://one.example.com")
    c1 = get_client()
    c1b = get_client()
    assert c1 is c1b

    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://two.example.com")
    c2 = get_client()
    assert c2 is not c1

    # A trailing slash is the same host and must not churn the client.
    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://two.example.com/")
    assert get_client() is c2


def test_fit_state_stays_inside_the_budget_window():
    """Sanity: the state fitter is cheap relative to a normal budget."""
    started = time.perf_counter()
    fit_state("y" * 50_000, QUESTIONS)
    assert time.perf_counter() - started < 1.0
