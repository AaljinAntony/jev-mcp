"""Optional live smoke tests.

Skipped unless `TYPESAFE_API_KEY` is set or `JEV_MCP_LIVE=1`. Never run in CI
without a real key; the mock-mode tests above are the offline gate.
"""

import os

import pytest

from jev_engine import verify_command

pytestmark = pytest.mark.skipif(
    not (os.getenv("TYPESAFE_API_KEY") or os.getenv("JEV_MCP_LIVE") == "1"),
    reason="set TYPESAFE_API_KEY or JEV_MCP_LIVE=1 to run live smoke tests",
)


def test_live_guardrail_shape():
    result = verify_command("git status")
    assert "safe" in result
    assert result["action"] in {"auto", "review", "escalate"}
    assert isinstance(result["reason_codes"], list)
    assert result["model"]
    assert result["usage"] and "input_tokens" in result["usage"]


def test_live_destructive_not_safe():
    result = verify_command("rm -rf /")
    assert result["safe"] is False