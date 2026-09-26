import os
import sys
import json
import time
import threading
from pathlib import Path
from typing import Dict, List, Any, Optional

# Direct imports from the active virtual environment SDK
from typesafe_sdk import TypeSafeClient, Choice, Noul, Score, RetryPolicy
from typesafe_sdk import (
    TypeSafeAPIConnectionError,
    TypeSafeAPIError,
    TypeSafeAPIResponseValidationError,
    TypeSafeAPITimeoutError,
    TypeSafeAuthenticationError,
)

from config import get_config
from candidates import (
    build_criteria,
    bound_candidates,
    looks_binary,
    markdown_preview,
    read_head,
    read_text_cache,
)
from jev_errors import (
    JevConfigError,
    JevError,
    JevResponseError,
    JevTimeoutError,
    JevValidationError,
    error_details,
    retryable_status,
)
from jev_logging import log_round, log_event
from jev_validation import validate_response
from limits import (
    fit_state,
    MAX_CANDIDATE_CHARS,
    MAX_CHOICE_OPTIONS,
    MAX_CONTENT_CHARS,
    MAX_PREVIEW_READS,
    MAX_TOTAL_PREVIEW_CHARS,
    truncate_text,
)
from mock import mock_system_one
from policy import (
    DEFAULT_RISK_THRESHOLD,
    DEFAULT_ESCALATE_THRESHOLD,
    FAMILY_CLUSTER_MAX_SIBLINGS,
    FAMILY_CLUSTER_MIN_PROB,
    NONE_CONFIDENCE,
    action_from_confidence,
    confidence_from_probabilities,
    guardrail_safe,
    require_complete_context,
    worst_action,
)

DEFAULT_SCAN_PATHS = [
    ".agents/skills",
    ".agents/workflows",
    ".agents/memory",
    ".opencode/skills",
    "skills",
    ".agents",
]

#: Maximum allowed length for any single tool parameter string.
MAX_INPUT_CHARS = 100_000


def _check_input_length(name: str, value: str) -> None:
    """Reject oversized string inputs before expensive processing."""
    if isinstance(value, str) and len(value) > MAX_INPUT_CHARS:
        raise JevValidationError(
            f"Parameter '{name}' is too long ({len(value):,} chars, limit {MAX_INPUT_CHARS:,})."
        )


def _is_within(candidate: Path, base: Path) -> bool:
    """True when `candidate` is `base` or lives under it.

    Uses `relative_to`, never `startswith`, so a sibling that shares a textual
    prefix (`<cwd>-evil`) is *not* inside `<cwd>`.
    """
    try:
        candidate.relative_to(base)
        return True
    except ValueError:
        return False


def _allowed_roots() -> List[Path]:
    """Directories an LLM-supplied `root_dir` may resolve inside.

    The process CWD and **its ancestors**, plus anything under
    `JEV_MCP_ALLOWED_ROOTS`. Ancestors are included deliberately: hosts launch
    the server with `cwd` set to the project root or to a temp dir, and a
    session legitimately asks about a subdirectory of, or a parent of, that.
    It is still an enormous reduction from "any path on the machine" and it
    never grants reach outside the user's own project tree.
    """
    roots: List[Path] = []
    try:
        cwd = Path.cwd().resolve()
    except OSError:
        cwd = None
    if cwd is not None:
        roots.append(cwd)
        roots.extend(cwd.parents)
    for entry in get_config().allowed_roots:
        try:
            candidate = Path(entry)
        except (TypeError, ValueError):
            continue
        if candidate.is_dir():
            roots.append(candidate)
    return roots


def _reject_system_dir(root: Path, root_dir: str) -> None:
    """Second gate: refuse the OS's own directories even if allowlisted.

    Kept separate from the allowlist so `C:\\Windows` still fails with the
    specific "system directory" wording the tests assert on.
    """
    root_lower = str(root).lower().rstrip("\\/")
    parts = root.parts
    if root.parent == root or len(parts) <= (1 if os.name == "nt" else 0):
        raise JevValidationError(f"root_dir '{root_dir}' points to a filesystem root (system directory).")

    if os.name == "nt":
        for env_var in [
            "SystemRoot",
            "windir",
            "ProgramFiles",
            "ProgramFiles(x86)",
        ]:
            val = os.environ.get(env_var)
            if not val:
                continue
            try:
                val_resolved = str(Path(val).resolve()).lower().rstrip("\\/")
            except (OSError, RuntimeError, ValueError):
                continue
            if root_lower == val_resolved or root_lower.startswith(val_resolved + "\\"):
                raise JevValidationError(
                    f"root_dir '{root_dir}' points inside a system directory ({env_var})."
                )
        # The system drive is a volume root, not a system directory: only the
        # bare drive itself is rejected, never arbitrary data on that volume.
        drive = os.environ.get("SystemDrive")
        if drive and root_lower == drive.lower().rstrip("\\/"):
            raise JevValidationError(
                f"root_dir '{root_dir}' points to a filesystem drive root (system directory)."
            )


def _validate_root_dir(root_dir: str) -> Path:
    """Resolve `root_dir` and confine it to an allowlist.

    A denylist cannot enumerate every sensitive path — the previous five-entry
    POSIX set left `/home`, `/var`, `/proc`, `C:\\Users` and `C:\\ProgramData`
    reachable from an LLM-supplied argument. The invariant is instead that a
    supplied `root_dir` carries no more privilege than the session's own working
    directory: it must be the CWD, one of its descendants, one of its ancestors,
    or inside `JEV_MCP_ALLOWED_ROOTS`. Everything else is rejected.
    """
    _check_input_length("root_dir", root_dir)
    root = Path(root_dir).resolve()

    # Belt and braces: keep the specific "system directory" rejection as a
    # second gate so it fires before the generic allowlist message.
    _reject_system_dir(root, root_dir)

    if not root.is_dir():
        raise JevValidationError(f"root_dir '{root_dir}' does not exist or is not a directory.")

    if not any(root == base or _is_within(root, base) for base in _allowed_roots()):
        raise JevValidationError(
            f"root_dir '{root_dir}' is outside the allowed roots. "
            "Pass a path inside the workspace, or set JEV_MCP_ALLOWED_ROOTS."
        )
    return root


def _find_settings_files() -> List[Path]:
    """Locate jevs_settings.json candidates: project-level first, user-level last."""
    candidates = []
    # 1. Check cwd (for when invoked from the project root)
    for name in ("jevs_settings.json", ".opencode/jevs_settings.json"):
        local = Path.cwd() / name
        if local.exists():
            candidates.append(local)
    # 2. Check script directory (the repo root, cwd-independent)
    script_dir = Path(__file__).resolve().parent
    if script_dir != Path.cwd().resolve():
        for name in ("jevs_settings.json", ".opencode/jevs_settings.json"):
            script_local = script_dir / name
            if script_local.exists() and script_local not in candidates:
                candidates.append(script_local)
    # 3. User-level config
    user = Path.home() / ".config" / "opencode" / "jevs_settings.json"
    if user.exists():
        candidates.append(user)
    return candidates


def _find_config_files() -> List[Path]:
    """Locate opencode.json candidates: project-level first, user-level last."""
    candidates = []
    for name in ("opencode.json", ".opencode/opencode.json"):
        local = Path.cwd() / name
        if local.exists():
            candidates.append(local)
    script_dir = Path(__file__).resolve().parent
    if script_dir != Path.cwd().resolve():
        for name in ("opencode.json", ".opencode/opencode.json"):
            script_local = script_dir / name
            if script_local.exists() and script_local not in candidates:
                candidates.append(script_local)
    user = Path.home() / ".config" / "opencode" / "opencode.json"
    if user.exists():
        candidates.append(user)
    return candidates


def _merge_jev_settings(settings: dict, jev: dict) -> bool:
    """Deep-merge one `jev_settings` dict onto `settings`. Returns True if it matched.

    Merging rather than first-file-wins matters because this repo ships a
    `jevs_settings.json` whose keys are all at their defaults. Under
    first-wins that file permanently shadowed `~/.config/opencode/jevs_settings.json`,
    so turning routing on in the user-level file silently did nothing.

    Semantics per key:
      * `enable_model_routing` — last writer wins (project overrides user).
      * `models` — union of non-empty values; an explicit `""` **removes** an
        inherited tier, which is the documented way to disable one.
      * `scan_paths` — additive union against the built-in defaults, deduped.
    """
    jev = jev or {}
    if not isinstance(jev, dict):
        return False
    matched = False
    if isinstance(jev.get("enable_model_routing"), bool):
        settings["enable_model_routing"] = jev["enable_model_routing"]
        matched = True
    models = jev.get("models")
    if isinstance(models, dict) and models:
        merged = dict(settings.get("models") or {})
        for k, v in models.items():
            if v:
                merged[k] = v
            else:
                merged.pop(k, None)   # explicit "" clears an inherited tier
        settings["models"] = merged
        matched = True
    scan_paths = jev.get("scan_paths")
    if isinstance(scan_paths, list):
        extras = [str(p) for p in scan_paths if isinstance(p, str)]
        settings["scan_paths"] = list(dict.fromkeys(list(settings["scan_paths"]) + extras))
        matched = True
    return matched


def _snapshot(settings: dict) -> dict:
    """A defensive copy, so a caller cannot mutate the module-level cache.

    `result["model_map"]` used to be a live reference into `_cached_settings`;
    any consumer that mutated it corrupted the cache for the rest of the
    process.
    """
    out = dict(settings)
    out["models"] = dict(settings.get("models") or {})
    out["scan_paths"] = list(settings.get("scan_paths") or [])
    out["sources"] = list(settings.get("sources") or [])
    return out


_cached_settings: Optional[dict] = None
_cached_settings_mtimes: dict = {}


def _reset_settings_cache() -> None:
    """Clear the cached settings. Exposed for tests."""
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
    """Read Jev settings by merging every candidate file, user config first.

    Lookup order: <cwd>/jevs_settings.json -> <cwd>/.opencode/jevs_settings.json ->
    ~/.config/opencode/jevs_settings.json -> legacy `jev_settings` block in
    opencode.json (project > user). Discovery is unchanged; precedence is now
    applied by *merging* the files in reverse discovery order, so a project file
    overrides the user file per key instead of replacing it wholesale.
    `source` is the last contributor (the project file, for display) and
    `sources` lists everything that contributed.

    Results are cached and only re-read when a candidate file's mtime changes.
    The returned dict is a fresh copy: callers cannot reach into the cache.
    """
    global _cached_settings, _cached_settings_mtimes

    settings_files = _find_settings_files()
    config_files = _find_config_files()
    current_mtimes = _get_files_mtime_signature(settings_files + config_files)

    if _cached_settings is not None and _cached_settings_mtimes == current_mtimes:
        return _snapshot(_cached_settings)

    settings = {
        "enable_model_routing": False,
        "models": {},
        "scan_paths": list(DEFAULT_SCAN_PATHS),
        "source": None,
        "sources": [],
    }

    # User-level config first, then project config, so the project overrides
    # the user and neither silently discards the other.
    ordered = [(f, True) for f in reversed(settings_files)]
    ordered += [(f, False) for f in reversed(config_files)]
    for cfg, whole_file in ordered:
        try:
            raw = json.loads(cfg.read_text(encoding="utf-8"))
        except Exception as e:
            log_event("settings_parse_error", file=str(cfg), error=str(e))
            sys.stderr.write(f"jev_engine: failed reading {cfg}: {e}\n")
            continue
        if not isinstance(raw, dict):
            continue
        # A `jevs_settings.json` *is* the settings object. `opencode.json` is a
        # full config, so only an explicit `jev_settings` block is read from it —
        # never its unrelated top-level keys.
        jev = raw if whole_file else (raw.get("jev_settings") if isinstance(raw.get("jev_settings"), dict) else {})
        if _merge_jev_settings(settings, jev):
            settings["source"] = str(cfg)
            settings["sources"].append(str(cfg))

    _cached_settings = settings
    # Re-stat after reading: a file edited between the signature and the read
    # must not leave a stale mtime paired with fresh content in the cache, which
    # would make the change invisible until the next edit.
    _cached_settings_mtimes = _get_files_mtime_signature(settings_files + config_files)
    return _snapshot(settings)


def get_scan_paths(root: Path) -> List[Path]:
    """Return resolved scan directories (defaults + configured extras, deduped)."""
    settings = load_jev_settings()
    seen, paths = set(), []
    for rel in settings["scan_paths"]:
        p = (root / rel).resolve()
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            paths.append(p)
    return paths


# Module-level cache
_cached_client: Optional[TypeSafeClient] = None
_cached_client_key: Optional[tuple] = None
_client_lock = threading.Lock()


def _close_cached_client() -> None:
    """Drop the cached client *and* close it so its connection pool is released.

    `TypeSafeClient` owns an `httpx2.Client` with a connection pool. Simply
    nulling the reference leaks the pool and its sockets — once per test here,
    once per `JEV_MCP_MOCK` toggle in the server. The `close()` runs outside the
    lock so a slow close cannot block other callers.
    """
    global _cached_client, _cached_client_key
    with _client_lock:
        client, _cached_client, _cached_client_key = _cached_client, None, None
    if client is not None:
        try:
            client.close()
        except Exception as e:
            log_event("client_close_failed", error=type(e).__name__)


def _reset_client_cache() -> None:
    """Clear the cached client. Exposed for tests."""
    _close_cached_client()


def get_client() -> Optional[TypeSafeClient]:
    """Return a configured TypeSafe client, or None in mock mode.

    Caches the client and reuses it across calls as long as the
    client-relevant configuration hasn't changed. Raises `JevConfigError` when
    `TYPESAFE_API_KEY` is missing (unless `JEV_MCP_MOCK=1`, which never needs a
    key).
    """
    global _cached_client, _cached_client_key

    cfg = get_config()
    if cfg.mock:
        _close_cached_client()
        return None
    if not cfg.api_key:
        raise JevConfigError("TYPESAFE_API_KEY environment variable is not configured.")

    # Cache key: invalidate when any client-relevant config changes. The SDK
    # also reads TYPESAFE_BASE_URL and TYPESAFE_DEFAULT_MODEL from the
    # environment, so a mid-process change to either must not leave a stale
    # client pointed at the old host or model.
    cache_key = (
        cfg.api_key,
        cfg.timeout_ms,
        cfg.model,
        (os.getenv("TYPESAFE_BASE_URL") or "").strip().rstrip("/"),
        (os.getenv("TYPESAFE_DEFAULT_MODEL") or "").strip(),
    )
    with _client_lock:
        if _cached_client is not None and _cached_client_key == cache_key:
            return _cached_client
        stale, _cached_client, _cached_client_key = _cached_client, None, None
        if stale is not None:
            try:
                stale.close()
            except Exception as e:
                log_event("client_close_failed", error=type(e).__name__)

        budget = cfg.timeout_ms / 1000.0
        _cached_client = TypeSafeClient(
            api_key=cfg.api_key,
            timeout=budget,
            retry=RetryPolicy(
                max_retries=2,
                backoff_initial=0.5,
                backoff_max=5.0,
                backoff_jitter=0.25,
                # `timeout` is the TOTAL budget for the call, not a per-attempt
                # limit: without it the SDK default of 30s applied on top of a
                # 30s per-attempt timeout, so 3 attempts + backoff took ~91.5s.
                timeout=budget,
                # A retry cannot beat an expired deadline; it only burns budget.
                api_timeout_error=False,
            ),
        )
        _cached_client_key = cache_key
        return _cached_client


def _remaining_seconds(started: float, cfg) -> float:
    """Seconds left in the `JEV_MCP_TIMEOUT_MS` budget for this tool call."""
    return max(0.001, (cfg.timeout_ms / 1000.0) - (time.perf_counter() - started))


# ----------------------------------------------------------------------
# Circuit breaker
# ----------------------------------------------------------------------
class _Breaker:
    """A minimal circuit breaker. Deliberately dependency-free.

    Without it, a revoked key or a 5xx outage means every tool call pays the
    full retry budget — and the plugin pays it again on every single message.
    The breaker turns "repeat the same expensive failure" into "fail
    immediately until it is worth probing again".

    After a cooldown exactly one half-open probe is admitted; anything that
    arrives while the probe is in flight is rejected, so a burst of callers
    cannot fan back out at once.
    """

    def __init__(self, threshold: int, cooldown_s: float, auth_cooldown_s: float) -> None:
        self.threshold = max(1, threshold)
        self.cooldown_s = cooldown_s
        self.auth_cooldown_s = auth_cooldown_s
        self.failures = 0
        self.opened_at = 0.0
        self.opened_cooldown = cooldown_s
        self.probing = False

    def cooldown_for(self, retryable: bool) -> float:
        """A bad API key does not fix itself in 30 seconds; a 5xx might."""
        return self.cooldown_s if retryable else self.auth_cooldown_s

    def allow(self) -> bool:
        if self.failures < self.threshold:
            return True
        if self.probing:
            return False
        if time.monotonic() - self.opened_at >= self.opened_cooldown:
            self.probing = True   # half-open: exactly one probe gets through
            return True
        return False

    def record_success(self) -> None:
        """A success closes the circuit and clears the consecutive-failure count."""
        self.failures = 0
        self.opened_at = 0.0
        self.opened_cooldown = self.cooldown_s
        self.probing = False

    def record_failure(self, retryable: bool) -> None:
        self.failures += 1
        self.probing = False
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()
            # A 401 must not park the breaker in a 30s window, but it must still
            # short-circuit: a bad key does not fix itself in 30 seconds.
            self.opened_cooldown = self.cooldown_for(retryable)

    def remaining(self) -> float:
        """Seconds until the next probe is allowed."""
        return max(0.0, self.opened_cooldown - (time.monotonic() - self.opened_at))


_cached_breaker: Optional[_Breaker] = None
_cached_breaker_key: Optional[tuple] = None


def _breaker() -> _Breaker:
    """The process-wide breaker, rebuilt when the configured knobs change."""
    global _cached_breaker, _cached_breaker_key
    cfg = get_config()
    key = (cfg.breaker_threshold, cfg.breaker_cooldown_s, cfg.auth_cooldown_s)
    if _cached_breaker is None or _cached_breaker_key != key:
        _cached_breaker = _Breaker(cfg.breaker_threshold, cfg.breaker_cooldown_s, cfg.auth_cooldown_s)
        _cached_breaker_key = key
    return _cached_breaker


def _reset_breaker() -> None:
    """Clear the circuit breaker state. Exposed for tests."""
    global _cached_breaker, _cached_breaker_key
    _cached_breaker = None
    _cached_breaker_key = None


def _breaker_guard(breaker: _Breaker) -> None:
    """Raise a retryable error when the circuit is open, stating when to retry."""
    if breaker.allow():
        return
    wait = breaker.remaining()
    log_event("circuit_open", retry_in=round(wait, 3))
    raise JevTimeoutError(
        "TypeSafe is failing repeatedly; the circuit is open. "
        f"Retry in {int(wait) + 1}s."
    )


def execute_system_one(client, state: Any, questions: dict, timeout_s: Optional[float] = None) -> Any:
    """Run `system_one`, re-raising SDK failures as typed Jev errors.

    `timeout_s` is the *remaining* budget for this tool call, so an attempt that
    starts late in the call cannot run past the deadline (ported from
    `reference/burnigtm-jev-mcp/src/typesafe.ts:13-16`).

    In mock mode (or with a None client) a deterministic offline judge answers,
    so the server and tests run without a network round-trip; that path is
    CPU-bound, so the caller checks it against the budget instead.
    """
    cfg = get_config()
    model = cfg.model or None
    if cfg.mock or client is None:
        return mock_system_one(state, questions, model=model or "jev-latest")

    breaker = _breaker()
    _breaker_guard(breaker)
    kwargs: Dict[str, Any] = {}
    if timeout_s is not None:
        kwargs["timeout"] = timeout_s
    try:
        try:
            if hasattr(client, "system_one"):
                res = client.system_one(state=state, questions=questions, model=model, **kwargs)
            elif hasattr(client, "decide"):
                res = client.decide(state=state, decisions=questions, model=model, **kwargs)
            else:
                raise JevConfigError("Configured client does not support system_one or decide")
        except TypeSafeAPITimeoutError as e:
            breaker.record_failure(retryable=True)
            raise JevTimeoutError() from e
        except TypeSafeAPIConnectionError as e:
            breaker.record_failure(retryable=True)
            raise JevTimeoutError("Could not connect to TypeSafe.") from e
        except TypeSafeAPIResponseValidationError as e:
            # A malformed body is the provider's fault and is not an auth
            # problem, so it must not extend the auth cooldown.
            breaker.record_failure(retryable=True)
            raise JevResponseError() from e
        except TypeSafeAuthenticationError:
            # 401/403: not retryable, but it must still short-circuit for the
            # longer auth window.
            breaker.record_failure(retryable=False)
            raise
        except TypeSafeAPIError as e:
            breaker.record_failure(retryable=retryable_status(getattr(e, "status", 500)))
            raise
    except JevError:
        raise
    except Exception:
        # A local programming error is not provider failure; do not open the
        # circuit on it.
        breaker.probing = False
        raise
    breaker.record_success()
    return res


def get_answer(response: Any, key: str) -> Any:
    if hasattr(response, "answers") and isinstance(response.answers, dict):
        return response.answers.get(key)
    return getattr(response, key, None)


def get_val(ans_obj: Any) -> Any:
    if ans_obj is None:
        return None
    keys = ["choice", "value", "key", "selected", "noul", "score"]
    if isinstance(ans_obj, dict):
        for attr in keys:
            v = ans_obj.get(attr)
            if v is not None:
                return v
        return ans_obj
    for attr in keys:
        v = getattr(ans_obj, attr, None)
        if v is not None:
            return v
    return ans_obj


def get_prob(ans_obj: Any) -> float:
    if ans_obj is None:
        return 0.0
    keys = ["noul", "probability", "confidence", "value", "score"]
    if isinstance(ans_obj, dict):
        for attr in keys:
            v = ans_obj.get(attr)
            if isinstance(v, (int, float)):
                return float(v)
        return 0.0
    for attr in keys:
        v = getattr(ans_obj, attr, None)
        if isinstance(v, (int, float)):
            return float(v)
    if isinstance(ans_obj, (int, float)):
        return float(ans_obj)
    return 0.0


# ----------------------------------------------------------------------
# Result / envelope helpers
# ----------------------------------------------------------------------
def _attr_opt(obj: Any, name: str):
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _usage_dict(usage: Any) -> Optional[dict]:
    if usage is None:
        return {"input_tokens": None, "output_tokens": None}
    return {
        "input_tokens": _attr_opt(usage, "input_tokens"),
        "output_tokens": _attr_opt(usage, "output_tokens"),
    }


def _response_meta(res: Any, cfg) -> dict:
    """Shared `model`, `usage` envelope from a validated response."""
    return {
        "model": getattr(res, "model", None) or cfg.model or None,
        "usage": _usage_dict(_attr_opt(res, "usage")),
    }


def _candidate_fields(
    complete: bool,
    considered: int,
    original_chars: int,
    evaluated_chars: int,
    **extra: int,
) -> dict:
    """The `coverage.candidate_fields` block (reference output-schemas shape).

    Reports how much candidate evidence actually reached the model, so a
    truncated candidate set is visible in the envelope instead of silent.
    """
    fields = {
        "complete": complete,
        "candidates_considered": considered,
        "original_chars": original_chars,
        "evaluated_chars": evaluated_chars,
    }
    fields.update(extra)
    return fields


def _coverage_envelope(fitted: Optional[dict], candidate_fields: Optional[dict] = None) -> dict:
    """A coverage block for paths where no Jev request was made."""
    if isinstance(fitted, dict) and isinstance(fitted.get("coverage"), dict):
        coverage = dict(fitted["coverage"])
    else:
        coverage = {
            "complete": True,
            "original_chars": 0,
            "evaluated_chars": 0,
            "estimated_tokens": {"state": 0, "questions": 0, "longest_question": 0},
            "estimator": "chars/4",
        }
    if candidate_fields is not None:
        coverage["candidate_fields"] = candidate_fields
    return coverage


def _slot_confidence(ans_obj: Any) -> float:
    """Confidence of a single answer; falls back to the distribution."""
    if ans_obj is None:
        return 0.0
    conf = _attr_opt(ans_obj, "confidence")
    if isinstance(conf, (int, float)):
        return float(conf)
    probs = _attr_opt(ans_obj, "probabilities")
    if isinstance(probs, dict) and probs:
        return confidence_from_probabilities(probs)
    prob = get_prob(ans_obj)
    if prob:
        return max(prob, 1.0 - prob)
    return 0.0


def _noul_confidence(prob: float) -> float:
    return max(prob, 1.0 - prob)


def _risk_action(prob: float, cfg) -> str:
    """Guardrail action from a risk noul.

    A confident ``destructive_prob >= DEFAULT_ESCALATE_THRESHOLD`` never runs on
    its own, and the DEFAULT_RISK_THRESHOLD boundary matches the
    backward-compatible ``safe`` thresholds.
    """
    if prob >= DEFAULT_ESCALATE_THRESHOLD:
        return "escalate"
    if prob >= DEFAULT_RISK_THRESHOLD:
        return "review"
    return action_from_confidence(_noul_confidence(prob), cfg.auto_accept, cfg.review_at)


def _request(state_text: str, questions: dict):
    """Fit + execute + validate one Jev round. Returns (res, fitted, cfg).

    The whole call is bounded by `JEV_MCP_TIMEOUT_MS`, not just the HTTP leg:
    `fit_state` and the offline judge are inside the same budget, so a call that
    spends its time locally cannot then spend it again on the network.
    """
    cfg = get_config()
    started = time.perf_counter()
    try:
        fitted = fit_state(state_text, questions)
        res = execute_system_one(
            get_client(),
            state=fitted["state"],
            questions=questions,
            timeout_s=_remaining_seconds(started, cfg),
        )
        # The mock judge is CPU-bound Python: a 100k-char state with 250 options
        # can exceed a small budget, and reporting a decision as if it had been
        # produced in time is worse than reporting the overrun.
        if time.perf_counter() - started > cfg.timeout_ms / 1000.0:
            raise JevTimeoutError()
        validate_response(res, questions)
        log_round(
            (time.perf_counter() - started) * 1000,
            question_keys=questions.keys(),
            state_len=len(fitted["state"]),
        )
        return res, fitted, cfg
    except Exception as err:
        log_round(
            (time.perf_counter() - started) * 1000,
            question_keys=questions.keys(),
            state_len=len(state_text),
            error=err,
        )
        raise


# ----------------------------------------------------------------------
# 1. Command Verification Guardrail
# ----------------------------------------------------------------------
def verify_command(command: str) -> dict:
    _check_input_length("command", command)
    state = f"Terminal shell command to execute: {command}"
    questions = {
        "is_destructive": Noul(
            instructions="Does this command permanently delete files, drop tables, or wipe directories?"
        ),
        "modifies_git": Noul(
            instructions="Does this command modify or delete git configuration or history (e.g. force push, rm -rf .git)?"
        )
    }

    res, fitted, cfg = _request(state, questions)

    dest_prob = round(float(get_prob(get_answer(res, "is_destructive"))), 2)
    git_prob = round(float(get_prob(get_answer(res, "modifies_git"))), 2)
    dest_conf = round(_noul_confidence(dest_prob), 4)
    git_conf = round(_noul_confidence(git_prob), 4)
    per_action = [
        _risk_action(a, cfg)
        for a in (dest_prob, git_prob)
    ]
    action = require_complete_context(worst_action(per_action), fitted["truncated"])
    confidence = round(min(dest_conf, git_conf), 4)
    safe = guardrail_safe(action, dest_prob, git_prob)

    reason_codes = []
    if dest_prob >= 0.20:
        reason_codes.append(f"destructive_prob={dest_prob:.2f}")
    if git_prob >= 0.20:
        reason_codes.append(f"git_modify_prob={git_prob:.2f}")
    if confidence < cfg.auto_accept:
        reason_codes.append(f"confidence={confidence:.2f}<{cfg.auto_accept:.2f}")
    if fitted["truncated"]:
        reason_codes.append("context_truncated")
    if action != "auto":
        reason_codes.append(f"action={action}")

    result = {
        "safe": safe,
        "destructive_prob": dest_prob,
        "git_modify_prob": git_prob,
        "action": action,
        "confidence": confidence,
        "reason_codes": reason_codes,
        "truncated": fitted["truncated"],
        "coverage": fitted["coverage"],
    }
    result.update(_response_meta(res, cfg))
    return result


# ----------------------------------------------------------------------
# 2. Agent Resource & Skill Finder (.agents & .opencode)
# ----------------------------------------------------------------------
def _answer_probs(res: Any, slot: str) -> Dict[str, float]:
    probs = _attr_opt(get_answer(res, slot), "probabilities")
    if not isinstance(probs, dict):
        return {}
    return {k: float(v) for k, v in probs.items()}


#: Probability at or above which a non-primary candidate is reported as a hit.
RESOURCE_SIBLING_MIN_PROB = 0.12

AGENT_RESOURCE_INSTRUCTIONS = (
    "Select the single agent skill, workflow, or memory document that is most "
    "directly relevant to the task described in `task`. Each option's "
    "description is that document's own summary. Judge relevance from what the "
    "document covers, not from its filename. If no supplied document addresses "
    "the task, choose the 'none' option."
)


def find_agent_resources(task: str, root_dir: str = ".", max_matches: int = 5) -> dict:
    _check_input_length("task", task)
    root = _validate_root_dir(root_dir)
    search_dirs = get_scan_paths(root)

    candidate_files: Dict[str, Path] = {}
    for sdir in search_dirs:
        if not sdir.exists():
            continue
        for p in sdir.rglob("*.md"):
            if p.is_file():
                try:
                    rel = p.relative_to(root).as_posix()
                except ValueError:
                    continue  # path is outside root, skip it
                candidate_files[rel] = p

    if not candidate_files:
        result = {
            "matched": False,
            "count": 0,
            "resources": [],
            "summary": "No Markdown resources or skills found in candidate directories.",
            "primary": None,
            "file": None,
            "content": None,
            "primary_probability": None,
            "ranked": [],
            "candidates_considered": 0,
            "candidates_evaluated": 0,
            "candidates_truncated": False,
            "reason_codes": [],
            "action": "auto",          # nothing to decide; see note below
            "confidence": None,
            "truncated": False,
            "coverage": _coverage_envelope(
                None, _candidate_fields(True, 0, 0, 0)
            ),
            "model": None,
            "usage": None,
        }
        return result

    options_all = list(candidate_files.keys())
    options, candidates_truncated = bound_candidates(options_all, MAX_CHOICE_OPTIONS)
    if candidates_truncated:
        log_event(
            "candidates_truncated",
            tool="find_agent_resources",
            considered=len(options_all),
            kept=len(options),
        )

    # One read per candidate, reused for both the criteria preview and the
    # resource content returned below.
    text_cache, read_text = read_text_cache(root)
    criteria_map = build_criteria(options, read_text)
    if candidates_truncated:
        criteria_map["none"] = (
            "None of the supplied agent resources addresses the task; a further "
            f"{len(options_all) - len(options)} candidates were not evaluated"
        )
    else:
        criteria_map["none"] = "None of the supplied agent resources addresses the task"
    criteria_map = dict(sorted(criteria_map.items()))

    questions = {
        "primary": Choice(
            criteria=criteria_map,
            instructions=AGENT_RESOURCE_INSTRUCTIONS,
        ),
    }

    state = {
        "task": task,
        "goal": "Identify which specific Markdown agent resources are directly relevant.",
        "candidates_considered": len(options_all),
        "candidates_evaluated": len(options),
        "candidates_truncated": candidates_truncated,
    }
    res, fitted, cfg = _request(state, questions)

    probs = _answer_probs(res, "primary")
    primary_ans = get_answer(res, "primary")
    primary_val = get_val(primary_ans)
    if primary_val == "none":
        primary_val = None
    primary_prob = probs.get(primary_val, 0.0) if primary_val else 0.0

    selected_keys: List[str] = []
    if primary_val and primary_val in candidate_files:
        selected_keys.append(primary_val)
    for opt, p in sorted(probs.items(), key=lambda kv: -kv[1]):
        if opt in candidate_files and opt not in selected_keys and p >= RESOURCE_SIBLING_MIN_PROB:
            selected_keys.append(opt)

    # Family clustering is a convenience for hyphenated skill trees. It only runs
    # on a decision the model actually made, and it can never claim every slot.
    if primary_val and primary_prob >= FAMILY_CLUSTER_MIN_PROB:
        primary_name = Path(primary_val).parent.name
        if "-" in primary_name:
            family_prefix = primary_name.rsplit("-", 1)[0] + "-"
            siblings = sorted(
                (
                    opt for opt in options
                    if opt != primary_val
                    and opt not in selected_keys
                    and Path(opt).parent.name.startswith(family_prefix)
                ),
                key=lambda opt: probs.get(opt, 0.0),
                reverse=True,
            )
            for opt in siblings[:FAMILY_CLUSTER_MAX_SIBLINGS]:
                selected_keys.append(opt)

    selected_keys = selected_keys[:max_matches]

    resources = []
    for rel_path in selected_keys:
        full_path = candidate_files[rel_path]
        # The bytes are already in `text_cache` from the criteria build: the
        # content is sliced from the same read, not fetched again.
        content = truncate_text(text_cache.get(rel_path, ""), MAX_CONTENT_CHARS)

        name = full_path.parent.name if full_path.name.lower() == "skill.md" else full_path.stem
        resources.append({
            "name": name,
            "file": rel_path,
            "content": content
        })

    summary_names = ", ".join(r["name"] for r in resources)

    ranked_map: Dict[str, float] = {
        opt: p for opt, p in probs.items() if opt in candidate_files and opt != "none"
    }
    ranked = sorted(
        ({"file": f, "probability": round(p, 4)} for f, p in ranked_map.items()),
        key=lambda r: -r["probability"],
    )[:max_matches]

    confidence = round(_slot_confidence(primary_ans), 4)
    action = require_complete_context(
        action_from_confidence(confidence, cfg.auto_accept, cfg.review_at),
        fitted["truncated"] or candidates_truncated,
    )

    reason_codes = []
    if candidates_truncated:
        reason_codes.append("candidates_truncated")
    if fitted["truncated"]:
        reason_codes.append("context_truncated")
    if confidence < cfg.auto_accept:
        reason_codes.append(f"confidence={confidence:.2f}<{cfg.auto_accept:.2f}")
    if action != "auto":
        reason_codes.append(f"action={action}")

    coverage = _coverage_envelope(
        fitted,
        _candidate_fields(
            complete=not candidates_truncated,
            considered=len(options_all),
            original_chars=sum(len(text_cache.get(o, "")) for o in options),
            evaluated_chars=sum(len(v) for v in criteria_map.values()),
        ),
    )

    result = {
        "matched": len(resources) > 0,
        "count": len(resources),
        "primary": resources[0] if resources else None,
        "file": resources[0]["file"] if resources else None,
        "content": resources[0]["content"] if resources else None,
        "resources": resources,
        "summary": f"Found {len(resources)} relevant agent resource(s): {summary_names}",
        "primary_probability": round(primary_prob, 4) if primary_val else None,
        "ranked": ranked,
        "candidates_considered": len(options_all),
        "candidates_evaluated": len(options),
        "candidates_truncated": candidates_truncated,
        "reason_codes": reason_codes,
        "action": action,
        "confidence": confidence,
        "truncated": fitted["truncated"],
        "coverage": coverage,
    }
    result.update(_response_meta(res, cfg))
    return result


# ----------------------------------------------------------------------
# 3. Fast Workspace File Selector
# ----------------------------------------------------------------------
def _exists_verdict(chosen, confidence: float, relevance_prob: float) -> str:
    """What the model actually established about workspace relevance.

    A confident Choice paired with a low presence Noul is a forced winner among
    poor options, not an answer: that reports `partial`, which is the fail-closed
    direction. `absent` is reserved for a genuine `none`.
    """
    if chosen and chosen != "none":
        return "answered" if relevance_prob >= NONE_CONFIDENCE else "partial"
    return "absent" if confidence >= 0.35 else "partial"


def _discover_files_git(
    root: Path,
    ignore_exts: set,
    max_count: int,
    ignore_dirs: Optional[set] = None,
) -> Optional[List[str]]:
    """Use git ls-files for fast, .gitignore-aware file discovery. Returns None if not a git repo."""
    import subprocess

    if not (root / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        candidates = []
        dirs_to_ignore = ignore_dirs or set()
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            p = Path(line)
            if any(ignored in p.parts for ignored in dirs_to_ignore):
                continue
            if p.suffix.lower() in ignore_exts:
                continue
            if (root / p).is_file():
                candidates.append(line.replace("\\", "/"))
            if len(candidates) >= max_count:
                break
        return candidates
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


TARGET_FILE_INSTRUCTIONS = (
    "Select the single workspace file that must be inspected or edited to perform "
    "the task described in `task`. Each option's description is that file's path "
    "followed by its opening content. Choose 'none' if no supplied file is relevant."
)

TARGET_FILE_STATE_GOAL = "Identify which single workspace file must be inspected or edited."


def _preview_criteria(root: Path, candidates: List[str]):
    """Criteria for the workspace files: `path`, then a preview of its head.

    Three bounds keep this honest on a 250-candidate tree: at most
    `MAX_PREVIEW_READS` files are opened, the total preview payload is capped at
    `MAX_TOTAL_PREVIEW_CHARS` and shared fairly across the candidates, and each
    preview is at most `MAX_CANDIDATE_CHARS`. Candidates that get no preview
    keep their path alone, which is still selectable evidence. Binary or empty
    heads fall back to the path rather than sending noise.

    A deterministic lexical prefilter would beat "first N" here: it could spend
    the read budget on the candidates that actually mention the task. That is
    deliberately left to the scan-cache phase; this bound is cheap and safe.
    """
    total = len(candidates)
    remaining = MAX_TOTAL_PREVIEW_CHARS
    criteria: Dict[str, str] = {}
    previews_built = 0
    previews_skipped = 0
    original_chars = 0

    for index, cand in enumerate(candidates):
        if previews_built >= MAX_PREVIEW_READS or remaining <= 0:
            previews_skipped += 1
            criteria[cand] = cand
            continue
        allowance = min(
            MAX_CANDIDATE_CHARS,
            remaining // max(total - index, 1),
        )
        text = read_head(root / cand)
        preview = markdown_preview(text, allowance) if text and not looks_binary(text) else ""
        if preview:
            criteria[cand] = f"{cand}\n---\n{preview}"
            previews_built += 1
            remaining -= len(preview)
            original_chars += len(text)
        else:
            criteria[cand] = cand
    return criteria, previews_built, previews_skipped, original_chars


def select_target_files(task: str, root_dir: str = ".", max_results: int = 5) -> dict:
    _check_input_length("task", task)
    root = _validate_root_dir(root_dir)
    ignore_dirs = {".git", ".godot", ".import", ".venv", "node_modules", "dist", "build"}
    ignore_exts = {".png", ".jpg", ".jpeg", ".webp", ".wav", ".ogg", ".mp3", ".ttf", ".import", ".zip"}

    # Fast path: use git if available
    candidates = _discover_files_git(root, ignore_exts, MAX_CHOICE_OPTIONS, ignore_dirs)

    # Fallback: manual directory walk, bounded by depth
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
                    continue  # skip symlinks to prevent loops
                if p.suffix.lower() not in ignore_exts:
                    candidates.append(p.relative_to(root).as_posix())
                    if len(candidates) >= MAX_CHOICE_OPTIONS:
                        break
            if len(candidates) >= MAX_CHOICE_OPTIONS:
                break

    if not candidates:
        result = {
            "matched": False,
            "files": [],
            "exists": "no_candidates",     # was "absent" — see note
            "probability": 0.0,
            "relevance_prob": 0.0,
            "ranked": [],
            "candidates_truncated": False,
            "action": "auto",
            "confidence": None,
            "truncated": False,
            "coverage": _coverage_envelope(
                None, _candidate_fields(True, 0, 0, 0, previews_built=0, previews_skipped=0)
            ),
            "model": None,
            "usage": None,
        }
        return result

    # Building a preview means reading a file, so the read count and the total
    # preview bytes are both bounded. Anything past the bound keeps its path
    # only — still a selectable option, just without content evidence.
    candidates_truncated = len(candidates) >= MAX_CHOICE_OPTIONS
    criteria, previews_built, previews_skipped, original_chars = _preview_criteria(root, candidates)
    criteria["none"] = "None of the supplied workspace files must be inspected or edited for this task."

    questions = {
        "target_file": Choice(criteria=criteria, instructions=TARGET_FILE_INSTRUCTIONS),
        "is_relevant": Noul(
            instructions=(
                "Does at least one of the supplied workspace files actually have to be "
                "inspected or edited to perform the task, or is the top-ranked file a "
                "forced winner among files that are all poor matches?"
            ),
            criteria={
                "true": "At least one supplied file is genuinely required for the task",
                "false": "No supplied file is genuinely required; the top choice is a forced winner",
            },
        ),
    }

    res, fitted, cfg = _request(
        {
            "task": task,
            "goal": TARGET_FILE_STATE_GOAL,
            "candidates_evaluated": len(candidates),
        },
        questions,
    )

    target_ans = get_answer(res, "target_file")
    chosen = get_val(target_ans)
    probs = _answer_probs(res, "target_file")
    conf = _slot_confidence(target_ans)
    relevance_prob = round(float(get_prob(get_answer(res, "is_relevant"))), 4)
    action = require_complete_context(
        action_from_confidence(conf, cfg.auto_accept, cfg.review_at),
        fitted["truncated"] or candidates_truncated,
    )

    # A low presence probability means the top file is a forced winner among
    # options that do not fit, so it is not reported as a match.
    escaped = (not chosen) or chosen == "none" or relevance_prob < NONE_CONFIDENCE
    if not escaped:
        probability = round(probs.get(chosen, 0.0), 4)
        files = [chosen]
    else:
        probability = 0.0
        files = []

    ranked = sorted(
        ({"file": f, "probability": round(p, 4)} for f, p in probs.items() if f != "none"),
        key=lambda r: -r["probability"],
    )[:max_results]

    result = {
        "matched": not escaped,
        "files": files if not escaped else [],
        "exists": _exists_verdict(chosen, conf, relevance_prob),
        "probability": probability,
        "relevance_prob": relevance_prob,
        "ranked": ranked,
        "candidates_truncated": candidates_truncated,
        "action": action,
        "confidence": round(conf, 4),
        "truncated": fitted["truncated"],
        "coverage": _coverage_envelope(
            fitted,
            _candidate_fields(
                complete=not candidates_truncated,
                considered=len(candidates),
                original_chars=original_chars,
                evaluated_chars=sum(len(v) for v in criteria.values()),
                previews_built=previews_built,
                previews_skipped=previews_skipped,
            ),
        ),
    }
    result.update(_response_meta(res, cfg))
    return result


# ----------------------------------------------------------------------
# 4. Model Tier Selection
# ----------------------------------------------------------------------
TIER_CRITERIA = {
    "fast": "Typos, simple lookups, docstrings, boilerplate",
    "balanced": "Standard bugs, test cases, isolated feature changes",
    "frontier": "Multi-file refactors, architecture design, complex algorithmic logic",
}


def select_model_tier(task: str) -> dict:
    """Select the optimal LLM model tier (fast, balanced, or frontier).

    Gated by `jevs_settings.enable_model_routing`. Low-confidence or truncated
    judgments report `action: "escalate"` while keeping `recommended_tier`.
    """
    _check_input_length("task", task)
    settings = load_jev_settings()
    result = {
        "enabled": settings["enable_model_routing"],
        "task": task,
        "recommended_tier": None,
        "recommended_model": None,
        "model_map": dict(settings["models"]),
        "action": "review",
        "confidence": None,
        "truncated": False,
        "coverage": _coverage_envelope(None),
        "model": None,
        "usage": None,
    }
    if not settings["enable_model_routing"]:
        return result

    res, fitted, cfg = _request(
        f"User Task: {task}\nDetermine the appropriate model tier based on task complexity and reasoning required.",
        {"tier": Choice(criteria=TIER_CRITERIA, instructions="Select the appropriate model capability tier for this task.")},
    )

    tier_ans = get_answer(res, "tier")
    selected_tier = get_val(tier_ans) or "balanced"
    conf = _slot_confidence(tier_ans)
    action = require_complete_context(
        action_from_confidence(conf, cfg.auto_accept, cfg.review_at),
        fitted["truncated"],
    )

    result["recommended_tier"] = selected_tier
    result["recommended_model"] = settings["models"].get(selected_tier)
    result["action"] = action
    result["confidence"] = round(conf, 4)
    result["truncated"] = fitted["truncated"]
    result["coverage"] = fitted["coverage"]
    result["model"] = getattr(res, "model", None) or cfg.model or None
    result["usage"] = _usage_dict(_attr_opt(res, "usage"))
    return result


# ----------------------------------------------------------------------
# 5. CLI Entry Point
# ----------------------------------------------------------------------
def _cli_dispatch(argv) -> int:
    if len(argv) < 2:
        print(json.dumps({"error": {"code": "USAGE", "message": "Usage: python jev_engine.py [verify|resource|files] [args...]", "retryable": False}}))
        return 1
    action = argv[0].lower()
    try:
        if action == "verify":
            print(json.dumps(verify_command(argv[1])))
        elif action in ["resource", "find_resource"]:
            root_path = argv[2] if len(argv) > 2 else "."
            print(json.dumps(find_agent_resources(argv[1], root_path)))
        elif action in ["files", "target_files"]:
            root_path = argv[2] if len(argv) > 2 else "."
            print(json.dumps(select_target_files(argv[1], root_path)))
        elif action in ["tier", "model_tier"]:
            print(json.dumps(select_model_tier(argv[1])))
        else:
            print(json.dumps({"error": {"code": "USAGE", "message": f"Unknown action: {action}", "retryable": False}}))
            return 1
        return 0
    except Exception as e:
        print(json.dumps({"error": error_details(e)}))
        return 1


if __name__ == "__main__":
    from config import ensure_dotenv
    ensure_dotenv()
    sys.exit(_cli_dispatch(sys.argv[1:]))
