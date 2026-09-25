# Phase 2: Fix Remaining Correctness Bugs

> **Phase**: 02  
> **Target Files**:
> - [`policy.py`](file:///d:/mcp/jev-typesafe-mcp/policy.py)
> - [`jev_engine.py`](file:///d:/mcp/jev-typesafe-mcp/jev_engine.py)
> - [`tests/test_policy.py`](file:///d:/mcp/jev-typesafe-mcp/tests/test_policy.py)  
> **Parallel Execution Track**:
> - In parallel mode:
>   - **Track A Agent**: Edits `policy.py` and `tests/test_policy.py`.
>   - **Track E Agent**: Edits `jev_engine.py`.
> - In sequential mode: Single agent applies all changes.

---

## 1. Problem Context & Rationale

### Bug B: Desynchronized Risk Thresholds
In `jev_engine.py`:
```python
def _risk_action(prob: float, cfg) -> str:
    if prob >= 0.50:
        return "escalate"
    if prob >= 0.20:
        return "review"
    return action_from_confidence(_noul_confidence(prob), cfg.auto_accept, cfg.review_at)
```
In `policy.py`:
```python
def guardrail_safe(
    action: PolicyAction,
    destructive_prob: float,
    git_modify_prob: float,
    destructive_threshold: float = 0.20,
    git_threshold: float = 0.20,
) -> bool:
...
```
`0.20` and `0.50` are magic numbers hardcoded in two separate files. If an engineer changes `DEFAULT_AUTO_ACCEPT` or adjusts safety thresholds in `policy.py`, `_risk_action` in `jev_engine.py` will quietly drift out of sync.

### Bug C: Overly Greedy Family-Prefix Resource Matching
In `jev_engine.py` (lines 517–523):
```python
if primary_val:
    primary_name = Path(primary_val).parent.name
    if "-" in primary_name:
        family_prefix = primary_name.rsplit("-", 1)[0] + "-"
        for opt in options:
            if opt not in selected_keys and family_prefix in opt:
                selected_keys.append(opt)
```
This uses substring matching: `family_prefix in opt`.
If the primary skill is `.agents/skills/deploy-aws/SKILL.md`:
- `primary_name` = `"deploy-aws"`
- `family_prefix` = `"deploy-"`
- `family_prefix in opt` matches **any** path containing `"deploy-"` anywhere! For example, `src/deploy-helper.py` or `.agents/skills/custom-deploy-pipeline/SKILL.md`.
Instead, only sibling resources whose directory name starts with `deploy-` should be included.

### Bug D: Race Condition in `get_client()`
`jev_engine.py` maintains `_cached_client` and `_cached_client_key` at the module level.
In a multi-threaded MCP server environment (such as SSE with FastAPI/uvicorn worker threads or asynchronous tool handlers), concurrent calls to `get_client()` can evaluate `_cached_client is None` simultaneously, creating multiple `TypeSafeClient` instances and leaking connection pools.

---

## 2. Step-by-Step Code Changes

### Step 2.1: `policy.py`
Define named constants and update default function parameters.

**Target lines** (`policy.py` lines 14–22):
```python
DEFAULT_AUTO_ACCEPT = 0.8
DEFAULT_REVIEW_AT = 0.5
DEFAULT_RISK_THRESHOLD = 0.20
DEFAULT_ESCALATE_THRESHOLD = 0.50
```

Update `guardrail_safe()` in `policy.py` (lines 105–111):
```python
def guardrail_safe(
    action: PolicyAction,
    destructive_prob: float,
    git_modify_prob: float,
    destructive_threshold: float = DEFAULT_RISK_THRESHOLD,
    git_threshold: float = DEFAULT_RISK_THRESHOLD,
) -> bool:
    """Backward-compatible ``safe``: auto AND low destructive/git probabilities."""
    return (
        action == "auto"
        and destructive_prob < destructive_threshold
        and git_modify_prob < git_threshold
    )
```

---

### Step 2.2: `jev_engine.py`

#### 1. Import named constants and `threading`:
**Target lines** (`jev_engine.py` lines 1–35):
```python
import threading
```
Update policy import:
```python
from policy import (
    DEFAULT_RISK_THRESHOLD,
    DEFAULT_ESCALATE_THRESHOLD,
    action_from_confidence,
    confidence_from_probabilities,
    guardrail_safe,
    min_confidence,
    require_complete_context,
    worst_action,
)
```

#### 2. Thread-safe `get_client()`:
**Target lines** (`jev_engine.py` lines 184–231):
```python
# Module-level cache with thread safety lock
_cached_client: Optional[TypeSafeClient] = None
_cached_client_key: Optional[tuple] = None
_client_lock = threading.Lock()


def _reset_client_cache() -> None:
    """Clear the cached client. Exposed for tests."""
    global _cached_client, _cached_client_key
    with _client_lock:
        _cached_client = None
        _cached_client_key = None


def get_client() -> Optional[TypeSafeClient]:
    """Return a configured TypeSafe client, or None in mock mode.

    Thread-safe client caching reusing instances across calls.
    """
    global _cached_client, _cached_client_key

    cfg = get_config()
    if cfg.mock:
        with _client_lock:
            _cached_client = None
            _cached_client_key = None
        return None
    if not cfg.api_key:
        raise JevConfigError("TYPESAFE_API_KEY environment variable is not configured.")

    cache_key = (cfg.api_key, cfg.timeout_ms, cfg.model)
    with _client_lock:
        if _cached_client is not None and _cached_client_key == cache_key:
            return _cached_client

        _cached_client = TypeSafeClient(
            api_key=cfg.api_key,
            timeout=cfg.timeout_ms / 1000.0,
            retry=RetryPolicy(
                max_retries=2,
                backoff_initial=0.5,
                backoff_max=5.0,
                backoff_jitter=0.25,
            ),
        )
        _cached_client_key = cache_key
        return _cached_client
```

#### 3. Update `_risk_action` to use named constants:
**Target lines** (`jev_engine.py` lines 345–356):
```python
def _risk_action(prob: float, cfg) -> str:
    """Guardrail action from a risk noul.

    A confident destructive_prob >= DEFAULT_ESCALATE_THRESHOLD never runs on its own,
    and the DEFAULT_RISK_THRESHOLD boundary matches backward-compatible safe thresholds.
    """
    if prob >= DEFAULT_ESCALATE_THRESHOLD:
        return "escalate"
    if prob >= DEFAULT_RISK_THRESHOLD:
        return "review"
    return action_from_confidence(_noul_confidence(prob), cfg.auto_accept, cfg.review_at)
```

#### 4. Fix greedy family-prefix resource matching:
**Target lines** (`jev_engine.py` lines 517–524):
```python
    if primary_val:
        primary_name = Path(primary_val).parent.name
        if "-" in primary_name:
            family_prefix = primary_name.rsplit("-", 1)[0] + "-"
            for opt in options:
                # Match sibling directory name prefix, not arbitrary substring anywhere in path
                opt_parent_name = Path(opt).parent.name
                if opt not in selected_keys and opt_parent_name.startswith(family_prefix):
                    selected_keys.append(opt)
```

---

### Step 2.3: `tests/test_policy.py`
Add tests verifying the unified constants and boundary conditions:
```python
from policy import DEFAULT_RISK_THRESHOLD, DEFAULT_ESCALATE_THRESHOLD, guardrail_safe

def test_risk_threshold_constants():
    assert DEFAULT_RISK_THRESHOLD == 0.20
    assert DEFAULT_ESCALATE_THRESHOLD == 0.50
    # Boundary test: exactly at threshold must be unsafe
    assert guardrail_safe("auto", destructive_prob=0.199, git_modify_prob=0.199) is True
    assert guardrail_safe("auto", destructive_prob=0.20, git_modify_prob=0.0) is False
    assert guardrail_safe("auto", destructive_prob=0.0, git_modify_prob=0.20) is False
```

---

## 3. Verification Commands

Run policy and engine tests:
```powershell
.venv\Scripts\pytest tests/test_policy.py tests/test_mock_tools.py -v
```

Expected output:
- All policy tests pass.
- All mock tool tests pass without regressions.

---

## 4. Acceptance Criteria
- [ ] `policy.py` exports `DEFAULT_RISK_THRESHOLD` and `DEFAULT_ESCALATE_THRESHOLD`.
- [ ] `guardrail_safe()` defaults to `DEFAULT_RISK_THRESHOLD`.
- [ ] `_risk_action()` in `jev_engine.py` uses the shared constants.
- [ ] `find_agent_resources()` tests sibling directory prefixes via `Path(opt).parent.name.startswith(...)`.
- [ ] `get_client()` synchronizes with `threading.Lock()`.
- [ ] `pytest tests/test_policy.py` passes cleanly.
