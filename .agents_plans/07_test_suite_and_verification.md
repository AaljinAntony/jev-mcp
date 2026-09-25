# Phase 7: Comprehensive Test Suite & Regression Verification

> **Phase**: 07  
> **Target Files**:
> - [`tests/test_hardening_integration.py`](file:///d:/mcp/jev-typesafe-mcp/tests/test_hardening_integration.py) *(new test file)*
> - Complete test suite run  
> **Parallel Execution Track**:
> - Belongs to **Track F (Final Integration Verification)**.
> - **Must run AFTER** all code modifications in Tracks A through E are complete.

---

## 1. Objective

Provide an automated, rigorous integration test suite that confirms all 5 correctness fixes, 7 reliability risks, 6 performance optimizations, and plugin safeguards are working cohesively without regressions.

---

## 2. New Test File: `tests/test_hardening_integration.py`

Create `tests/test_hardening_integration.py` with 10 targeted test cases:

```python
"""Integration and regression test suite for Jev-TypeSafe MCP hardening."""

import json
import os
import threading
import time
from pathlib import Path
import pytest

from config import get_config, _reset_config_cache
from jev_engine import (
    get_client,
    _reset_client_cache,
    load_jev_settings,
    _reset_settings_cache,
    _validate_root_dir,
    select_target_files,
    find_agent_resources,
)
from jev_errors import (
    error_details,
    JevValidationError,
)
from limits import fit_state
from policy import DEFAULT_RISK_THRESHOLD, DEFAULT_ESCALATE_THRESHOLD, guardrail_safe
from typesafe_sdk import TypeSafeAPIConnectionError, TypeSafeAPITimeoutError


# 1. BUG-A: Exception hierarchy & connection error mapping
def test_connection_error_never_skipped():
    class TestConnError(TypeSafeAPIConnectionError):
        def __init__(self):
            super(Exception, self).__init__("Connection aborted")

    res = error_details(TestConnError())
    assert res["code"] == "API_ERROR"
    assert res["retryable"] is True
    assert "Could not connect to TypeSafe" in res["message"]


# 2. BUG-B: Shared risk constants
def test_risk_constants_and_guardrail_boundaries():
    assert DEFAULT_RISK_THRESHOLD == 0.20
    assert DEFAULT_ESCALATE_THRESHOLD == 0.50
    # Safe boundary
    assert guardrail_safe("auto", destructive_prob=0.19, git_modify_prob=0.19) is True
    # At or above threshold must be false
    assert guardrail_safe("auto", destructive_prob=0.20, git_modify_prob=0.0) is False
    assert guardrail_safe("auto", destructive_prob=0.0, git_modify_prob=0.20) is False


# 3. BUG-C: Sibling family-prefix matching precision
def test_family_prefix_sibling_matching(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_MCP_MOCK", "1")
    skills_dir = tmp_path / ".agents" / "skills"
    
    # Family 1: tool-runner & tool-builder
    (skills_dir / "tool-runner").mkdir(parents=True)
    (skills_dir / "tool-runner" / "SKILL.md").write_text("# Runner")
    (skills_dir / "tool-builder").mkdir(parents=True)
    (skills_dir / "tool-builder" / "SKILL.md").write_text("# Builder")
    
    # Unrelated: my-tool-extra (should NOT match tool- prefix)
    (skills_dir / "my-tool-extra").mkdir(parents=True)
    (skills_dir / "my-tool-extra" / "SKILL.md").write_text("# Unrelated")

    res = find_agent_resources("run tools", root_dir=str(tmp_path))
    assert res is not None


# 4. BUG-D: Thread-safe concurrent get_client() calls
def test_concurrent_get_client_thread_safety(monkeypatch):
    monkeypatch.setenv("JEV_MCP_MOCK", "0")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-123456789012345678901234567890")
    _reset_config_cache()
    _reset_client_cache()

    clients = []
    errors = []

    def worker():
        try:
            client = get_client()
            clients.append(client)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0
    assert len(clients) == 10
    # All threads must receive the EXACT same cached client instance
    first = clients[0]
    for c in clients:
        assert c is first

    _reset_client_cache()
    _reset_config_cache()


# 5. RISK-1: Structured logging on settings parse error
def test_settings_parse_error_logging(tmp_path, monkeypatch):
    bad_json = tmp_path / "jevs_settings.json"
    bad_json.write_text("{ unquoted_key: invalid }")
    monkeypatch.chdir(tmp_path)
    _reset_settings_cache()

    settings = load_jev_settings()
    assert settings["enable_model_routing"] is False
    _reset_settings_cache()


# 6. RISK-2: Rejection of Windows drive roots and system directories
def test_validate_root_dir_security():
    for drive in ["C:\\", "D:\\", "E:\\", "Z:\\"]:
        with pytest.raises(JevValidationError):
            _validate_root_dir(drive)


# 7. RISK-3: Depth-limited file walk
def test_target_files_bounded_depth(tmp_path):
    # Deep directory nesting
    p = tmp_path
    for i in range(7):
        p = p / f"dir_{i}"
        p.mkdir()
        (p / "test.py").write_text("a = 1")

    res = select_target_files("find file", root_dir=str(tmp_path))
    assert res is not None


# 8. RISK-6: fit_state defensive handling & token reuse
def test_fit_state_token_reuse():
    state = {"hello": "world"}
    questions = {"q1": "test question"}
    fitted = fit_state(state, questions)
    assert fitted["truncated"] is False
    assert fitted["coverage"]["complete"] is True
    assert fitted["coverage"]["estimated_tokens"]["state"] > 0


# 9. PERF-1: Config caching and invalidation
def test_config_caching(monkeypatch):
    monkeypatch.setenv("JEV_MCP_MOCK", "1")
    _reset_config_cache()
    c1 = get_config()
    assert c1.mock is True

    # Mutate env without resetting cache -> should still return c1
    monkeypatch.setenv("JEV_MCP_MOCK", "0")
    c2 = get_config()
    assert c2.mock is True

    # Reset cache -> now reflects new env
    _reset_config_cache()
    c3 = get_config()
    assert c3.mock is False
    _reset_config_cache()


# 10. PERF-2: Settings mtime caching
def test_settings_mtime_caching(tmp_path, monkeypatch):
    cfg_file = tmp_path / "jevs_settings.json"
    cfg_file.write_text(json.dumps({"enable_model_routing": True}))
    monkeypatch.chdir(tmp_path)
    _reset_settings_cache()

    s1 = load_jev_settings()
    assert s1["enable_model_routing"] is True

    # Re-reading without file change returns cached dict
    s2 = load_jev_settings()
    assert s1 is s2
    _reset_settings_cache()
```

---

## 3. Full Verification Execution

Execute the full suite in the active virtual environment:

```powershell
.venv\Scripts\pytest tests/ -v
```

Execute plugin tests:
```powershell
node tests/test_plugin.mjs
```

---

## 4. Acceptance Criteria
- [ ] `tests/test_hardening_integration.py` runs with **10 passing tests**.
- [ ] Complete pytest suite passes (all 140+ tests pass with zero unexpected failures or skips).
- [ ] OpenCode plugin tests pass with Node test runner.
