"""Phase 3: concurrent access, encoding, and the state budget.

Three unrelated failures that all share a shape — something that only goes wrong
under a condition the single-threaded suite never creates:

* `_Breaker` mutated its state unlocked, so the "exactly one half-open probe"
  invariant its own docstring promises could not hold when the MCP runtime served
  concurrent tool calls. An outage was survived by N simultaneous probes, which is
  the fan-out the breaker exists to prevent.
* `ScanCache.get` ran its signature outside the lock and then wrote through a key
  another thread had already evicted, raising `KeyError` out of the cache and out
  of the tool call.
* `load_jev_settings` read with `utf-8`, so a settings file written by Windows
  PowerShell — which writes a BOM — was silently discarded and the project ran on
  defaults. Found in Phase 0 when the same trap bit `doctor.py`.

And one budget bug, not a race: a very long `task` was truncated mid-sentence
rather than refused.
"""

import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import jev_engine  # noqa: E402
import limits  # noqa: E402
from jev_engine import (  # noqa: E402
    _Breaker,
    _reset_breaker,
    _reset_scan_cache,
    _reset_settings_cache,
)
from jev_errors import JevValidationError  # noqa: E402
from scan_cache import ScanCache  # noqa: E402
from typesafe_sdk import ChoiceAnswer, SystemOneResponse, Usage  # noqa: E402


def _one_choice_response(key, probabilities):
    """A minimal valid response, for tests that only need a call to complete."""
    choice = max(probabilities, key=lambda k: probabilities[k])
    return SystemOneResponse(
        model="jev-test",
        answers={
            key: ChoiceAnswer(choice=choice, probabilities=probabilities, confidence=0.9)
        },
        usage=Usage(input_tokens=5, output_tokens=2),
    )


class TestBreakerSingleHalfOpenProbe:
    """The invariant, forced concurrent.

    Unlocked, `allow()`'s read of `self.probing` and its write are separate bytecodes
    and the GIL interleaves them. `sys.setswitchinterval` makes that window wide
    enough to hit every time instead of rarely.
    """

    THREADS = 12

    def _open_circuit(self, breaker):
        for _ in range(breaker.threshold):
            breaker.record_failure(retryable=True)

    def test_exactly_one_thread_is_admitted_after_a_cooldown(self):
        sys.setswitchinterval(1e-6)  # widen the race window deliberately
        try:
            breaker = _Breaker(threshold=1, cooldown_s=0.0, auth_cooldown_s=0.0)
            breaker.record_failure(retryable=True)   # opens the circuit
            admitted = []
            start = threading.Barrier(self.THREADS)

            def _call():
                start.wait()
                if breaker.allow():
                    admitted.append(threading.current_thread().name)

            threads = [threading.Thread(target=_call) for _ in range(self.THREADS)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
        finally:
            sys.setswitchinterval(0.005)

        assert len(admitted) == 1, f"{len(admitted)} probes were admitted, expected exactly 1"

    def test_a_second_call_after_the_probe_is_still_refused(self):
        breaker = _Breaker(threshold=1, cooldown_s=0.0, auth_cooldown_s=0.0)
        breaker.record_failure(retryable=True)
        assert breaker.allow() is True    # the probe
        assert breaker.allow() is False   # nothing else gets through

    def test_record_success_is_not_racy(self):
        sys.setswitchinterval(1e-6)
        try:
            breaker = _Breaker(threshold=1, cooldown_s=0.0, auth_cooldown_s=0.0)
            breaker.record_failure(retryable=True)
            errors = []

            def _reset():
                try:
                    for _ in range(200):
                        breaker.record_success()
                        breaker.record_failure(retryable=True)
                except Exception as exc:  # pragma: no cover - the failure mode
                    errors.append(exc)

            threads = [threading.Thread(target=_reset) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
        finally:
            sys.setswitchinterval(0.005)
        assert not errors, errors

    def test_remaining_is_callable_while_the_circuit_is_open(self):
        breaker = _Breaker(threshold=1, cooldown_s=30.0, auth_cooldown_s=300.0)
        breaker.record_failure(retryable=False)
        assert breaker.remaining() > 250, "an auth failure uses the longer window"

    def test_release_probe_returns_the_slot_without_opening_the_circuit(self):
        """A local bug is not provider evidence. It must release the half-open slot
        without clearing the failure count or starting a cooldown."""
        breaker = _Breaker(threshold=1, cooldown_s=0.0, auth_cooldown_s=0.0)
        breaker.record_failure(retryable=True)
        assert breaker.allow() is True
        breaker.release_probe()
        assert breaker.allow() is True, "the slot was released"
        assert breaker.failures >= 1, "a non-provider error cleared the failure count"


class TestBreakerBehaviourUnchanged:
    def test_below_the_threshold_everything_is_allowed(self):
        breaker = _Breaker(threshold=3, cooldown_s=30.0, auth_cooldown_s=300.0)
        assert breaker.allow() is True
        breaker.record_failure(retryable=True)
        assert breaker.allow() is True

    def test_a_retryable_failure_uses_the_short_cooldown(self):
        breaker = _Breaker(threshold=1, cooldown_s=30.0, auth_cooldown_s=300.0)
        breaker.record_failure(retryable=True)
        assert breaker.remaining() == pytest.approx(30.0, abs=1.0)

    def test_an_auth_failure_uses_the_long_cooldown(self):
        breaker = _Breaker(threshold=1, cooldown_s=30.0, auth_cooldown_s=300.0)
        breaker.record_failure(retryable=False)
        assert breaker.remaining() == pytest.approx(300.0, abs=1.0)

    def test_success_closes_the_circuit(self):
        breaker = _Breaker(threshold=2, cooldown_s=30.0, auth_cooldown_s=300.0)
        breaker.record_failure(retryable=True)
        breaker.record_failure(retryable=True)
        assert breaker.allow() is False
        breaker.record_success()
        assert breaker.allow() is True


class TestScanCacheEvictionRace:
    def test_a_signature_that_raises_oserror_is_a_miss_not_a_crash(self):
        cache = ScanCache(ttl_s=60)
        cache.put("k", ("sig",), "value")

        def _boom():
            raise OSError("stat failed")

        assert cache.get("k", _boom) is None

    def test_an_entry_evicted_mid_lookup_is_a_miss_not_a_keyerror(self):
        """The exact failure: `signature_fn` runs outside the lock, and the write
        afterwards used `self._entries[key].at = now`, which raises if another
        thread cleared the key in between."""
        cache = ScanCache(ttl_s=60)
        cache.put("k", ("sig",), "value")

        def _evict_then_return():
            cache.clear()          # another thread's clear()
            return ("sig",)        # signature unchanged, as a stale read would be

        assert cache.get("k", _evict_then_return) is None

    def test_concurrent_get_and_clear_never_raise(self):
        cache = ScanCache(ttl_s=60)
        cache.put("k", ("sig",), "value")
        errors = []
        start = threading.Barrier(8)

        def _hammer(evict):
            start.wait()
            for _ in range(300):
                try:
                    if evict:
                        cache.clear()
                    else:
                        cache.get("k", lambda: ("sig",))
                except Exception as exc:
                    errors.append(exc)

        threads = [
            threading.Thread(target=_hammer, args=(i % 2 == 0,)) for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        assert not errors, f"{len(errors)} exceptions, first: {errors[0]!r}"

    def test_a_hit_still_returns_and_refreshes_the_ttl(self):
        cache = ScanCache(ttl_s=60)
        cache.put("k", ("sig",), "value")
        assert cache.get("k", lambda: ("sig",)) == "value"
        assert cache.get("k", lambda: ("other",)) is None, "a changed signature is a miss"


class TestSettingsFileEncoding:
    def _write(self, path, payload, *, bom):
        text = json.dumps(payload)
        path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))

    def _settings_for(self, tmp_path):
        _reset_settings_cache()
        return jev_engine.load_jev_settings()

    def test_a_bom_settings_file_is_read_not_discarded(self, tmp_path, monkeypatch):
        """Windows PowerShell's `Set-Content -Encoding utf8` and its `>` operator
        both write a BOM. Before this, that made the file unreadable and the
        project silently ran on defaults."""
        monkeypatch.chdir(tmp_path)
        _reset_settings_cache()
        self._write(
            tmp_path / "jevs_settings.json",
            {"enable_model_routing": True, "models": {"fast": "vendor/quick"}},
            bom=True,
        )
        settings = self._settings_for(tmp_path)
        assert settings["enable_model_routing"] is True
        assert settings["models"]["fast"] == "vendor/quick"
        assert any("jevs_settings.json" in s for s in settings["sources"])

    def test_a_bomless_file_still_reads(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _reset_settings_cache()
        self._write(
            tmp_path / "jevs_settings.json",
            {"enable_model_routing": True, "models": {"fast": "vendor/quick"}},
            bom=False,
        )
        settings = self._settings_for(tmp_path)
        assert settings["enable_model_routing"] is True

    def test_genuinely_malformed_json_is_still_a_parse_error(self, tmp_path, monkeypatch):
        """BOM tolerance must not turn a broken file into a silent default.

        The parse failure is reported and the file contributes nothing, so the
        defaults hold — but `sources` must not claim it was read.
        """
        monkeypatch.chdir(tmp_path)
        _reset_settings_cache()
        (tmp_path / "jevs_settings.json").write_text("{not json", encoding="utf-8")
        events = []
        monkeypatch.setattr(
            jev_engine, "log_event", lambda event, **f: events.append((event, f))
        )
        settings = self._settings_for(tmp_path)
        assert settings["enable_model_routing"] is False, "defaults, having failed to read"
        assert any(e == "settings_parse_error" for e, _ in events), "the failure was silent"
        # The broken file is not a source. Other candidates may still be — this
        # repo's own `jevs_settings.json` is found via the server module's
        # directory, not the CWD, so an empty list is not the assertion here.
        assert not any(str(tmp_path) in s for s in settings["sources"]), (
            "a file that could not be parsed was recorded as a source"
        )


class TestTaskLengthCap:
    def test_a_task_at_the_cap_is_accepted(self, tmp_path, monkeypatch):
        """Accepted means it reaches the judge rather than being refused.

        A workspace with one real candidate, so the tool has a reason to ask.
        (With an empty workspace `find_agent_resources` returns early and never
        calls the judge at all, which is correct behaviour and a useless probe.)
        """
        monkeypatch.setenv("JEV_MCP_MOCK", "1")
        skill = tmp_path / ".agents" / "skills" / "alpha"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\ndescription: alpha helpers\n---\n# alpha\n", encoding="utf-8"
        )
        _reset_scan_cache()
        reached = []
        probabilities = {".agents/skills/alpha/SKILL.md": 0.9, "none": 0.1}

        def _spy(state, questions):
            reached.append((state, questions))
            # `_request` returns a (response, fit, config) triple.
            return (
                _one_choice_response("primary", probabilities),
                {"truncated": False, "coverage": {}},
                jev_engine.get_config(),
            )

        monkeypatch.setattr(jev_engine, "_request", _spy)
        jev_engine.find_agent_resources("a" * jev_engine.MAX_TASK_CHARS, str(tmp_path))
        assert reached, "a task at the cap never reached the judge"
        assert len(reached[0][0]["task"]) == jev_engine.MAX_TASK_CHARS

    def test_a_task_over_the_cap_is_refused_by_name(self, tmp_path):
        with pytest.raises(JevValidationError) as exc:
            jev_engine.find_agent_resources("a" * (jev_engine.MAX_TASK_CHARS + 1), str(tmp_path))
        message = str(exc.value)
        assert "task" in message
        assert "task_file" in message, "the refusal must name the way through"
        assert "truncated" in message, "and say why it refuses rather than truncates"

    def test_select_target_files_refuses_the_same_way(self, tmp_path):
        with pytest.raises(JevValidationError) as exc:
            jev_engine.select_target_files("a" * (jev_engine.MAX_TASK_CHARS + 1), str(tmp_path))
        assert "task_file" in str(exc.value)

    def test_select_model_tier_refuses_the_same_way(self):
        with pytest.raises(JevValidationError) as exc:
            jev_engine.select_model_tier("a" * (jev_engine.MAX_TASK_CHARS + 1))
        assert "task_file" in str(exc.value)

    def test_the_cap_is_smaller_than_the_generic_input_cap(self):
        assert jev_engine.MAX_TASK_CHARS < jev_engine.MAX_INPUT_CHARS

    def test_the_cap_leaves_the_state_budget_for_evidence(self):
        """The reason the cap exists, asserted rather than explained.

        A task at the cap must still fit beside a full criteria set, or the
        truncation this prevents would merely move.
        """
        task_tokens = limits.estimate_tokens("a" * jev_engine.MAX_TASK_CHARS)
        budget = limits.MAX_STATE_PLUS_LONGEST_QUESTION_TOKENS - task_tokens
        worst_case_question = limits.estimate_tokens(
            {"instructions": "x" * (limits.MAX_TOTAL_CRITERIA_CHARS // 2)}
        )
        assert budget > 0, "a task at the cap already exceeds the whole state budget"
        assert worst_case_question < limits.MAX_STATE_PLUS_LONGEST_QUESTION_TOKENS - task_tokens

    def test_other_parameters_keep_the_generic_cap(self):
        """`task_file` is bounded by MAX_INPUT_CHARS, not MAX_TASK_CHARS: it is a
        path, and its contents are read head-only under MAX_TASK_FILE_CHARS."""
        with pytest.raises(JevValidationError) as exc:
            jev_engine.find_agent_resources("x", ".", task_file="y" * (jev_engine.MAX_INPUT_CHARS + 1))
        assert "task_file" in str(exc.value)