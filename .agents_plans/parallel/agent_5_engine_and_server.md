# Parallel Wave 2: Core Engine & MCP Server Hardening

> **Target Agent**: Agent Chat 5  
> **Prerequisites**: Agents 1, 2, and 3 must have completed their tasks.  
> **Exclusive File Ownership**:
> - [`jev_engine.py`](file:///d:/mcp/jev-typesafe-mcp/jev_engine.py)
> - [`jev_mcp.py`](file:///d:/mcp/jev-typesafe-mcp/jev_mcp.py)

---

## 1. Tasks Overview
1. **Thread safety in `get_client()`**: Add `_client_lock = threading.Lock()`.
2. **RetryPolicy in `get_client()`**: Pass `retry=RetryPolicy(...)` when instantiating `TypeSafeClient`.
3. **Threshold Unification**: Import `DEFAULT_RISK_THRESHOLD` and `DEFAULT_ESCALATE_THRESHOLD` from `policy.py` and use them in `_risk_action()`.
4. **Greedy Prefix Matching**: Use `Path(opt).parent.name.startswith(family_prefix)` in `find_agent_resources`.
5. **Dynamic Windows Drive & System Path Blocking**: Reject any drive root (`C:\` through `Z:\`) and case-insensitive Windows system directories in `_validate_root_dir`.
6. **Depth-Bounded Fallback Walk**: Replace unbounded `root.rglob("*")` in `select_target_files` with `os.walk` bounded to depth 5.
7. **Redundant Git Walk Removal**: Remove redundant parent `.git` walk in `_discover_files_git`.
8. **Structured Settings Parse Error Logging**: Call `log_event("settings_parse_error", ...)` on corrupted JSON.
9. **Settings Mtime Caching**: Cache parsed settings and check candidate file mtimes before re-reading from disk.
10. **Graceful Shutdown**: Register `atexit` in `jev_mcp.py` to flush logs on server stop.

---

## 2. Exact Changes

### 2.1 `jev_engine.py`

#### Imports:
```python
import os
import sys
import json
import time
import threading
from pathlib import Path
from typing import Dict, List, Any, Optional

from typesafe_sdk import TypeSafeClient, Choice, Noul, Score, RetryPolicy
from typesafe_sdk import (
    TypeSafeAPIConnectionError,
    TypeSafeAPIResponseValidationError,
    TypeSafeAPITimeoutError,
)

from config import get_config
from jev_errors import (
    JevConfigError,
    JevResponseError,
    JevTimeoutError,
    JevValidationError,
    error_details,
)
from jev_logging import log_round, log_event
from jev_validation import validate_response
from limits import fit_state, MAX_CHOICE_OPTIONS, MAX_CONTENT_CHARS
from mock import mock_system_one
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

#### Windows Drive Blocking in `_validate_root_dir`:
```python
def _validate_root_dir(root_dir: str) -> Path:
    _check_input_length("root_dir", root_dir)
    root = Path(root_dir).resolve()

    blocked_posix = {Path("/").resolve(), Path("/etc").resolve(), Path("/usr").resolve(), Path("/bin").resolve(), Path("/sbin").resolve()}
    if root in blocked_posix:
        raise JevValidationError(f"root_dir '{root_dir}' points to a system directory.")

    if os.name == "nt":
        if root.parent == root or len(root.parts) <= 1:
            raise JevValidationError(f"root_dir '{root_dir}' points to a filesystem drive root.")
        root_lower = str(root).lower().rstrip("\\")
        for env_var in ["SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "SystemDrive"]:
            val = os.environ.get(env_var)
            if val:
                val_resolved = str(Path(val).resolve()).lower().rstrip("\\")
                if root_lower == val_resolved or root_lower.startswith(val_resolved + "\\"):
                    raise JevValidationError(f"root_dir '{root_dir}' points inside a system directory ({env_var}).")

    if root.parent == root:
        raise JevValidationError(f"root_dir '{root_dir}' points to a system directory.")
    if not root.is_dir():
        raise JevValidationError(f"root_dir '{root_dir}' does not exist or is not a directory.")
    return root
```

#### Settings Mtime Caching & Structured Error Logging:
```python
_cached_settings: Optional[dict] = None
_cached_settings_mtimes: dict = {}


def _reset_settings_cache() -> None:
    global _cached_settings, _cached_settings_mtimes
    _cached_settings = None
    _cached_settings_mtimes = {}


def _get_files_mtime_signature(files: List[Path]) -> dict:
    mtimes = {}
    for f in files:
        try:
            mtimes[str(f)] = f.stat().st_mtime
        except (OSError, FileNotFoundError):
            mtimes[str(f)] = None
    return mtimes


def load_jev_settings() -> dict:
    global _cached_settings, _cached_settings_mtimes

    candidates = _find_settings_files() + _find_config_files()
    current_mtimes = _get_files_mtime_signature(candidates)

    if _cached_settings is not None and _cached_settings_mtimes == current_mtimes:
        return _cached_settings

    settings = {
        "enable_model_routing": False,
        "models": {},
        "scan_paths": list(DEFAULT_SCAN_PATHS),
        "source": None,
    }

    for cfg in _find_settings_files():
        try:
            raw = json.loads(cfg.read_text(encoding="utf-8"))
        except Exception as e:
            log_event("settings_parse_error", file=str(cfg), error=str(e))
            sys.stderr.write(f"jev_engine: failed reading {cfg}: {e}\n")
            continue
        if _apply_jev_settings(settings, raw.get("jev_settings") or raw):
            settings["source"] = str(cfg)
            _cached_settings = settings
            _cached_settings_mtimes = current_mtimes
            return settings

    for cfg in _find_config_files():
        try:
            raw = json.loads(cfg.read_text(encoding="utf-8"))
        except Exception as e:
            log_event("settings_parse_error", file=str(cfg), error=str(e))
            sys.stderr.write(f"jev_engine: failed reading {cfg}: {e}\n")
            continue
        if _apply_jev_settings(settings, raw.get("jev_settings") or {}):
            settings["source"] = str(cfg)
            _cached_settings = settings
            _cached_settings_mtimes = current_mtimes
            return settings

    _cached_settings = settings
    _cached_settings_mtimes = current_mtimes
    return settings
```

#### Thread-Safe Client with `threading.Lock`:
```python
_cached_client: Optional[TypeSafeClient] = None
_cached_client_key: Optional[tuple] = None
_client_lock = threading.Lock()


def _reset_client_cache() -> None:
    global _cached_client, _cached_client_key
    with _client_lock:
        _cached_client = None
        _cached_client_key = None


def get_client() -> Optional[TypeSafeClient]:
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

#### Unified `_risk_action`:
```python
def _risk_action(prob: float, cfg) -> str:
    if prob >= DEFAULT_ESCALATE_THRESHOLD:
        return "escalate"
    if prob >= DEFAULT_RISK_THRESHOLD:
        return "review"
    return action_from_confidence(_noul_confidence(prob), cfg.auto_accept, cfg.review_at)
```

#### Fix Sibling Prefix in `find_agent_resources`:
```python
    if primary_val:
        primary_name = Path(primary_val).parent.name
        if "-" in primary_name:
            family_prefix = primary_name.rsplit("-", 1)[0] + "-"
            for opt in options:
                opt_parent = Path(opt).parent.name
                if opt not in selected_keys and opt_parent.startswith(family_prefix):
                    selected_keys.append(opt)
```

#### Streamline `_discover_files_git` & Bounded `select_target_files`:
In `_discover_files_git`: remove parent `.git` check.
In `select_target_files`: replace `rglob("*")` fallback with `os.walk` max depth 5:
```python
    if candidates is None:
        candidates = []
        max_depth = 5
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = Path(dirpath).relative_to(root)
            if len(rel_dir.parts) >= max_depth:
                dirnames.clear()
                continue
            dirnames[:] = [d for d in dirnames if d not in ignore_dirs and not d.startswith(".")]
            for fname in filenames:
                if fname.startswith("."):
                    continue
                p = Path(dirpath) / fname
                if p.is_symlink():
                    continue
                if p.suffix.lower() not in ignore_exts:
                    candidates.append(p.relative_to(root).as_posix())
                    if len(candidates) >= MAX_CHOICE_OPTIONS:
                        break
            if len(candidates) >= MAX_CHOICE_OPTIONS:
                break
```

### 2.2 `jev_mcp.py`
Add shutdown handler:
```python
import atexit
import logging

mcp = FastMCP("jev-engine")

log_event("server_start", pid=os.getpid(), log_file=str(log_path()))


def _on_shutdown():
    try:
        log_event("server_stop", pid=os.getpid())
        for handler in logging.getLogger("jev_engine").handlers:
            handler.flush()
    except Exception:
        pass


atexit.register(_on_shutdown)
```

---

## 3. Verification Command
```powershell
.venv\Scripts\pytest tests/test_mock_tools.py -v
```
All mock tools tests must pass.
