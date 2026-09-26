"""Logging must stay cheap and must not become a leak.

Two regressions are covered here: `log_tool_call` serialized the entire result on
every call (and the MCP runtime serialized it again), and `_redact` either threw
away a whole field or let a credential through to `result_preview`.
"""

import json

import pytest

import config
import jev_logging
import jev_mcp
from jev_logging import log_tool_call


@pytest.fixture
def log_file(tmp_path, monkeypatch):
    import logging

    path = tmp_path / "engine.log"
    monkeypatch.setenv("JEV_MCP_LOG_FILE", str(path))
    jev_logging._logger = None
    logger = logging.getLogger("jev_engine")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    yield path
    jev_logging._logger = None
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def _records(path):
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    parsed = []
    for line in lines:
        body = line.split(" INFO ", 1)[-1]
        parsed.append(json.loads(body))
    return parsed


class TestToolCallLogging:
    def test_logs_result_keys_and_counts_not_the_result(self, log_file):
        result = {
            "matched": True,
            "count": 2,
            "primary": {"name": "x", "file": "a.md", "content": "y" * 4000},
            "resources": [{"name": "x"}] * 2,
            "ranked": [{"file": "a.md", "probability": 0.9}],
        }
        log_tool_call("search_agent_skills", 12.0, args={"task": "t"}, result=result)
        record = _records(log_file)[-1]
        assert record["evt"] == "tool_call"
        assert record["tool"] == "search_agent_skills"
        assert record["result_keys"] == sorted(result.keys())
        assert record["resources_count"] == 2
        assert record["ranked_count"] == 1
        assert "result_preview" not in record
        # The 4,000-character blob never reaches the log file.
        assert "y" * 100 not in log_file.read_text(encoding="utf-8")

    def test_preview_is_opt_in(self, log_file, monkeypatch):
        monkeypatch.setenv("JEV_MCP_LOG_PREVIEW", "1")
        config._reset_config_cache()
        log_tool_call(
            "search_agent_skills", 5.0,
            result={"matched": True, "primary": {"content": "z" * 4000}},
        )
        record = _records(log_file)[-1]
        assert "result_preview" in record
        assert record["result_preview"].startswith("{")
        assert len(record["result_preview"]) <= 1000

    def test_preview_is_redacted(self, log_file, monkeypatch):
        monkeypatch.setenv("JEV_MCP_LOG_PREVIEW", "1")
        config._reset_config_cache()
        log_tool_call(
            "guardrail_command", 5.0,
            result={"echoed": "api_key=SUPERSECRETVALUE"},
        )
        text = log_file.read_text(encoding="utf-8")
        assert "SUPERSECRETVALUE" not in text
        assert "<redacted>" in text

    def test_secret_in_a_result_never_reaches_the_log(self, log_file):
        log_tool_call(
            "search_target_files", 5.0,
            args={"task": "fix it"},
            result={"matched": True, "summary": "found api_key=SUPERSECRETVALUE"},
        )
        assert "SUPERSECRETVALUE" not in log_file.read_text(encoding="utf-8")

    def test_args_are_redacted_and_context_kept(self, log_file):
        log_tool_call(
            "guardrail_command", 5.0,
            args={"command": "curl -H 'Authorization: Bearer sk-abc123def456' https://x.test"},
            result={},
        )
        record = _records(log_file)[-1]
        assert "sk-abc123def456" not in record["args"]["command"]
        assert "curl" in record["args"]["command"]

    def test_non_dict_result(self, log_file):
        log_tool_call("odd", 1.0, result="a string")
        record = _records(log_file)[-1]
        assert record["result_type"] == "str"

    def test_error_path(self, log_file):
        log_tool_call("odd", 1.0, error={"error": {"code": "X"}})
        record = _records(log_file)[-1]
        assert record["error"] == {"error": {"code": "X"}}


class TestSingleTraceback:
    def test_one_failure_through_mcp_run_logs_one_traceback(self, log_file, monkeypatch):
        monkeypatch.setenv("JEV_MCP_MOCK", "1")
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

        def _boom():
            raise RuntimeError("provider exploded")

        with pytest.raises(Exception):
            jev_mcp._run("guardrail_command", _boom)

        text = log_file.read_text(encoding="utf-8")
        assert text.count('"traceback"') == 1, "the failure was logged more than once"
        # The round record still names the failure, without the traceback body.
        rounds = [r for r in _records(log_file) if r["evt"] == "round_error"]
        for record in rounds:
            assert "traceback" not in record
            assert record["error_type"] == "RuntimeError"
            assert "provider exploded" in record["error_message"]

    def test_engine_round_error_has_no_traceback(self, log_file, monkeypatch):
        import jev_engine

        monkeypatch.setenv("JEV_MCP_MOCK", "1")
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

        def _boom(*args, **kwargs):
            raise RuntimeError("provider exploded")

        monkeypatch.setattr(jev_engine, "execute_system_one", _boom)
        with pytest.raises(RuntimeError):
            jev_engine.verify_command("git status")

        text = log_file.read_text(encoding="utf-8")
        assert '"traceback"' not in text
        record = [r for r in _records(log_file) if r["evt"] == "round_error"][-1]
        assert record["error_type"] == "RuntimeError"
        assert record["error_message"] == "provider exploded"
        assert record["questions"] == ["is_destructive", "modifies_git"]

    def test_traceback_true_still_logs_one(self, log_file):
        jev_logging.log_round(1.0, ["q"], 10, error=ValueError("boom"), traceback=True)
        record = _records(log_file)[-1]
        assert record["evt"] == "round_error"
        assert "ValueError" in (record["traceback"] or "")

    def test_round_ok_has_no_error_fields(self, log_file):
        jev_logging.log_round(1.0, ["q"], 10)
        record = _records(log_file)[-1]
        assert record["evt"] == "round_ok"
        assert "traceback" not in record and "error_type" not in record


class TestRedaction:
    @pytest.mark.parametrize(
        "text,gone",
        [
            ("sk-abcdef123456", "sk-abcdef123456"),
            ("apikey_1234567890", "apikey_1234567890"),
            ("api_key=SUPERSECRET", "SUPERSECRET"),
            ("api_key: SUPERSECRET", "SUPERSECRET"),
            ("TYPESAFE_API_KEY=supersecretvalue", "supersecretvalue"),
            ("Bearer eyJhbGciOiJIUzI1NiJ9", "eyJhbGciOiJIUzI1NiJ9"),
            ("Authorization: dXNlcjpwYXNzd29yZA", "dXNlcjpwYXNzd29yZA"),
        ],
    )
    def test_credentials_removed(self, text, gone):
        assert gone not in jev_logging._redact(text)

    @pytest.mark.parametrize(
        "text",
        [
            "a" * 41,
            "0123456789abcdef0123456789abcdef01234567",          # git sha
            "This is a normal log message describing a long operation.",
            "d:/mcp/jev-typesafe-mcp/subdir/another/file.txt",
            "git checkout -- src/very/long/path/to/a/module/that/is/deep.py",
            "disk-backup-options",
            "hello",
        ],
    )
    def test_ordinary_values_survive(self, text):
        assert jev_logging._redact(text) == text

    def test_prefix_is_kept(self):
        out = jev_logging._redact("git log --oneline && export TOKEN=sk-abcdef123456")
        assert out.startswith("git log --oneline && export TOKEN=")
        assert "sk-abcdef123456" not in out
