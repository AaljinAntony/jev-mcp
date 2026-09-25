# Parallel Worker 2: Configuration & Logging Performance

> **Target Agent**: Agent Chat 2 (runs concurrently with Agents 1, 3, 4)  
> **Exclusive File Ownership**:
> - [`config.py`](file:///d:/mcp/jev-typesafe-mcp/config.py)
> - [`jev_logging.py`](file:///d:/mcp/jev-typesafe-mcp/jev_logging.py)  
> ⚠️ **CRITICAL LOCK RULE**: Do NOT touch `jev_engine.py`, `policy.py`, or any other file.

---

## 1. Tasks Overview
1. Cache `get_config()` in `config.py` to eliminate redundant environment variable lookups.
2. Provide `_reset_config_cache()` in `config.py` for testing.
3. Move `math` import to top-level in `config.py`.
4. Optimize `log_tool_call` in `jev_logging.py` so `json.dumps(result)` is executed only once instead of twice.

---

## 2. Exact Changes

### 2.1 `config.py`
Add `math` and `Optional` to imports:
```python
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from jev_errors import JevConfigError
```

Update `_is_finite`:
```python
def _is_finite(value: float) -> bool:
    return math.isfinite(value)
```

Add caching to `get_config()` and expose `_reset_config_cache()`:
```python
_cached_config: Optional[JevConfig] = None


def _reset_config_cache() -> None:
    """Clear the cached config. Exposed for tests."""
    global _cached_config
    _cached_config = None


def get_config() -> JevConfig:
    """Parse + validate the environment into a cached `JevConfig`."""
    global _cached_config
    if _cached_config is not None:
        return _cached_config

    config = JevConfig(
        api_key=(os.getenv("TYPESAFE_API_KEY") or "").strip(),
        model=(os.getenv("JEV_MCP_MODEL") or DEFAULT_MODEL).strip(),
        mock=_bool_env("JEV_MCP_MOCK"),
        auto_accept=_num_env("JEV_MCP_AUTO_ACCEPT", 0.8),
        review_at=_num_env("JEV_MCP_REVIEW_AT", 0.5),
        timeout_ms=_timeout_env(),
    )
    if config.review_at > config.auto_accept:
        raise JevConfigError("JEV_MCP_REVIEW_AT must not exceed JEV_MCP_AUTO_ACCEPT.")

    _cached_config = config
    return config
```

### 2.2 `jev_logging.py`
In `log_tool_call` (lines 102–111), replace duplicate `json.dumps`:
```python
def log_tool_call(tool: str, ms: int, args=None, result=None, error=None) -> None:
    """Log an MCP tool invocation (args/value serialized, secrets redacted)."""
    if error is not None:
        payload = {"error": error}
    else:
        # Serialize once, derive both length and preview
        dumped = json.dumps(result, default=str)
        payload = {
            "result_len": len(dumped),
            "result_preview": dumped[:1000],
        }
    args_safe = {k: _redact(v) for k, v in (args or {}).items()}
    log_event("tool_call", tool=tool, args=args_safe, ms=int(ms), **payload)
```

---

## 3. Verification Command
```powershell
.venv\Scripts\python.exe -c "from config import get_config, _reset_config_cache; c=get_config(); assert c is get_config(); _reset_config_cache(); print('Config cache OK')"
```
