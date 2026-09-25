# Phase 4 — Deadlines, Bounds and Circuit Breaking

**Goal:** make the configured timeout actually bound the request, confine `root_dir`
to a real allowlist, stop the settings file from permanently shadowing the user
config, and stop paying the full retry budget on every call while the provider is
down.

**Closes:** findings #12, #13, #17, #18, #19, #20.

**Depends on:** Phase 2 (error taxonomy and the `JevTimeoutError` mapping must exist
before the deadline logic can raise them).

**Risk:** medium. The `root_dir` allowlist is the one change that can reject calls
that work today.

---

## Finding #12 — Worst case is 3× the configured timeout

```python
# jev_engine.py:298-307
_cached_client = TypeSafeClient(
    api_key=cfg.api_key,
    timeout=cfg.timeout_ms / 1000.0,          # 30s PER ATTEMPT
    retry=RetryPolicy(
        max_retries=2,
        backoff_initial=0.5,
        backoff_max=5.0,
        backoff_jitter=0.25,
    ),
)
```

`RetryPolicy.timeout` is **not** passed, and it defaults to `30.0`
(`typesafe_sdk/_core/retry.py:82-86`):

```python
timeout: float | None = 30.0
"""Total retry budget in seconds per SDK call, including the initial attempt
and delays; `None` disables the limit."""
```

So with the documented `JEV_MCP_TIMEOUT_MS=30000`: up to 3 attempts × 30 s + backoff
(0.5 + 1.0) ≈ **91.5 s** for a single tool call. The plugin compounds this — it kills
its child at 4 s (installed) or 12 s (example) while the Python side is still
retrying, so every retry path is killed before it can report a result.

The reference solves it with a shared deadline (`reference/.../typesafe.ts:13-28,68-99`):

```ts
function attemptTimeoutMs(context, timeoutMs) {
  const deadline = context.deadline ?? Date.now() + timeoutMs;
  return Math.max(1, Math.min(timeoutMs, deadline - Date.now()));
}
// ... and
retry: { apiTimeoutError: false },
```

Two ideas: cap each attempt to the *remaining* deadline, and never retry a timeout
(it will just burn budget again).

### Edits

**`config.py`** — no new knob needed; `JEV_MCP_TIMEOUT_MS` becomes a **total**
budget. Update its docstring and the README table (`config/README.md:55`,
`README.md:130`) from "Per-request timeout" to "Total per-tool-call budget,
including retries".

**`jev_engine.py`** — add a deadline helper near `_request` (~line 438):

```python
from contextlib import contextmanager
import time as _time

@contextmanager
def _call_deadline(cfg):
    """Bound one tool call to `cfg.timeout_ms` end to end.

    The SDK's per-attempt timeout is only a slice of the real cost: with
    max_retries=2 the worst case is 3 * timeout + backoff. Ported from
    reference/burnigtm-jev-mcp/src/typesafe.ts:13-16.
    """
    start = _time.perf_counter()
    total = cfg.timeout_ms / 1000.0
    try:
        yield lambda: max(0.001, total - (_time.perf_counter() - start))
    finally:
        pass
```

Simpler as a plain helper — no context manager needed since the deadline is only
consumed once, inside `execute_system_one`:

```python
def _remaining_seconds(started: float, cfg) -> float:
    return max(0.001, (cfg.timeout_ms / 1000.0) - (time.perf_counter() - started))
```

**`get_client`** — build the retry policy with a total budget and no timeout retries:

```python
_retry = RetryPolicy(
    max_retries=2,
    backoff_initial=0.5,
    backoff_max=5.0,
    backoff_jitter=0.25,
    timeout=cfg.timeout_ms / 1000.0,   # total budget, not per attempt
    api_timeout_error=False,           # a retry cannot beat an expired deadline
)
```

**`execute_system_one`** — accept and apply the remaining budget:

```python
def execute_system_one(client, state, questions, timeout_s=None):
    cfg = get_config()
    model = cfg.model or None
    if cfg.mock or client is None:
        return mock_system_one(state, questions, model=model or "jev-latest")
    try:
        kwargs = {}
        if timeout_s is not None:
            kwargs["timeout"] = timeout_s
        if hasattr(client, "system_one"):
            return client.system_one(state=state, questions=questions, model=model, **kwargs)
        elif hasattr(client, "decide"):
            return client.decide(state=state, decisions=questions, model=model, **kwargs)
        raise JevConfigError("Configured client does not support system_one or decide")
    except TypeSafeAPITimeoutError as e:
        raise JevTimeoutError() from e
    except TypeSafeAPIConnectionError as e:
        raise JevTimeoutError("Could not connect to TypeSafe.") from e
    except TypeSafeAPIResponseValidationError as e:
        raise JevResponseError() from e
    # HTTP status errors (400/401/429/5xx) map in error_details(); see #22.
```

`system_one` accepts a per-call `timeout` keyword
(`typesafe_sdk/_core/client/sync/client.py:129-139`), so this is supported.

**`_request`** — thread the deadline through:

```python
def _request(state_text, questions):
    cfg = get_config()
    started = time.perf_counter()
    try:
        fitted = fit_state(state_text, questions)
        res = execute_system_one(
            get_client(), state=fitted["state"], questions=questions,
            timeout_s=_remaining_seconds(started, cfg),
        )
        validate_response(res, questions)
        log_round(...)
        return res, fitted, cfg
    except Exception as err:
        log_round(...)
        raise
```

**Also guard the mock path.** `mock_system_one` is CPU-bound Python; for a 100 k-char
state with 250 options it can exceed a small `JEV_MCP_TIMEOUT_MS`. Add an elapsed
check after it returns:

```python
if time.perf_counter() - started > cfg.timeout_ms / 1000.0:
    raise JevTimeoutError()
```

**Tests** (`tests/test_deadline.py`):

- With a fake client that sleeps, assert the total call stays within
  `JEV_MCP_TIMEOUT_MS + 0.5s`. This is the regression test for #12 — it fails today
  at roughly 3× the budget.
- Assert `RetryPolicy` receives `timeout=cfg.timeout_ms/1000.0` and
  `api_timeout_error=False` (inspect via monkeypatched `RetryPolicy`).
- `JEV_MCP_TIMEOUT_MS=2000` + a client that always times out → `JevTimeoutError`
  within ~2 s, and `error_details(...)["code"] == "TIMEOUT"`, `retryable is True`.
- Mock mode: a 100 k-char state with `JEV_MCP_TIMEOUT_MS=1` → `TIMEOUT`, not a
  silent success.
- Set the SDK env `TYPESAFE_BASE_URL` mid-process and assert the client cache key
  changes (see #19's cache-key note below).

---

## Finding #13 — No circuit breaker

Today, a revoked API key means every tool call pays: 1 non-retryable 401 (fast), but
a 5xx or connection failure pays 3 attempts + backoff, **every time**, for as long as
the outage lasts. The plugin pays it on **every message**.

A circuit breaker turns "repeat the same expensive failure" into "fail immediately
until it is worth retrying".

### Edits

**`jev_engine.py`** — a small module-level breaker, deliberately minimal (no external
dependency; `tenacity` is installed but is for retries, not circuit state):

```python
@dataclass
class _Breaker:
    threshold: int = 3            # consecutive failures before opening
    cooldown_s: float = 30.0      # stay open this long
    failures: int = 0
    opened_at: float = 0.0

    def allow(self) -> bool:
        if self.failures < self.threshold:
            return True
        if time.monotonic() - self.opened_at >= self.cooldown_s:
            return True   # half-open: let one call through to probe
        return False

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = 0.0

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()

_breaker = _Breaker()
```

Only **retryable** failures count. A 401 must *not* open the breaker into a 30 s
window — but it *should* short-circuit too, because a bad key never fixes itself in
30 s. Split it:

```python
@dataclass
class _Breaker:
    retryable_threshold: int = 3
    auth_cooldown_s: float = 300.0     # a bad key does not fix itself in 30s
    ...
```

Wire it in `execute_system_one` (around the client call) and `get_client` (for the
`JevConfigError` path, which is the missing-key case and is deterministic).

When open, raise a distinct, clearly-labelled error:

```python
raise JevTimeoutError(
    "TypeSafe is failing repeatedly; the circuit is open. "
    f"Retry in {int(_breaker.cooldown_s - (time.monotonic() - _breaker.opened_at))}s."
)
```

`JevTimeoutError` is already `retryable: True` (`jev_errors.py:72-77`), which is the
correct signal for "come back later". Add the remaining cooldown to the message so
the caller knows when.

**Tests:**

- 3 consecutive `TypeSafeAPIConnectionError` → 4th call raises without touching the
  client (assert a spy client saw exactly 3 calls).
- After the cooldown elapses (monkeypatch `time.monotonic`), one probe call is
  allowed; on success the breaker closes and subsequent calls are unthrottled.
- A 401 opens the *auth* breaker with the longer cooldown, and a 4th call does not
  reach the client.
- A single failure followed by a success resets the counter.

**Config:** expose `JEV_MCP_BREAKER_THRESHOLD` (default 3) and
`JEV_MCP_BREAKER_COOLDOWN_S` (default 30) in `config.py`, validated the same way as
the other knobs, and document them in `config/README.md` and `README.md`.

---

## Finding #17 — `root_dir` is a denylist

```python
# jev_engine.py:66-72
blocked_posix = {
    Path("/").resolve(), Path("/etc").resolve(), Path("/usr").resolve(),
    Path("/bin").resolve(), Path("/sbin").resolve(),
}
if root in blocked_posix:
    raise JevValidationError(...)
```

Gaps, on both platforms:

| Path | POSIX | Windows |
|---|---|---|
| `/var`, `/home`, `/root`, `/opt`, `/srv` | **allowed** | n/a |
| `/proc`, `/sys`, `/dev` | **allowed** | n/a |
| `C:\Users`, `C:\Users\<user>` | n/a | **allowed** |
| `C:\ProgramData` | n/a | **allowed** |
| `C:\Windows\System32\...` | n/a | blocked (SystemRoot prefix) |

The `blocked_posix` set is also computed unconditionally, so on Windows those five
`Path("/x").resolve()` calls resolve to drive-relative paths (`C:\etc`, …) and the
check is dead weight. Meanwhile the blocklist approach is unbounded: anything not
named is permitted.

There is already a test asserting the *Python* side is safe against out-of-root
symlinks (`test_mock_tools.py:131-141`) — and it passes, because
`p.relative_to(root)` raises `ValueError` at `jev_engine.py:540-541`. The gap is
that `root_dir` itself may be *any* directory: a prompt-injected agent can pass
`root_dir="C:\\Users\\aalji"` and get a file listing plus up to 5 files of content.

### Edits — allowlist, not denylist

The right invariant: **`root_dir` must be inside the workspace, or inside an
explicitly configured root.** An LLM-supplied path gets no privileges beyond the
session's own working directory.

**`config.py`** — add:

```python
JEV_MCP_ALLOWED_ROOTS: tuple[str, ...]   # from JEV_MCP_ALLOWED_ROOTS, os.pathsep-separated
```

```python
def _roots_env() -> tuple[str, ...]:
    raw = os.getenv("JEV_MCP_ALLOWED_ROOTS", "").strip()
    if not raw:
        return ()
    out = []
    for part in raw.split(os.pathsep):
        p = Path(part.strip()).expanduser()
        if p.is_absolute():
            out.append(str(p.resolve()))
    return tuple(out)
```

**`jev_engine.py`** — replace the body of `_validate_root_dir`:

```python
def _validate_root_dir(root_dir: str) -> Path:
    """Resolve root_dir and confine it to an allowlist.

    A denylist cannot enumerate every sensitive path, and an LLM-supplied
    root_dir should carry no more privilege than the session's own working
    directory. Allowed: the process CWD, any ancestor of it, and anything under
    JEV_MCP_ALLOWED_ROOTS. Everything else is rejected.
    """
    _check_input_length("root_dir", root_dir)
    root = Path(root_dir).resolve()

    if not root.is_dir():
        raise JevValidationError(f"root_dir '{root_dir}' does not exist or is not a directory.")

    # Belt and braces: a volume/posix root is never a legitimate workspace.
    if root.parent == root or len(root.parts) <= (1 if os.name == "nt" else 0):
        raise JevValidationError(f"root_dir '{root_dir}' points to a filesystem root (system directory).")

    # Keep the existing SystemRoot / ProgramFiles rejection as a second gate.
    _reject_system_dir(root, root_dir)

    allowed = _allowed_roots()
    if not any(root == base or _is_within(root, base) for base in allowed):
        raise JevValidationError(
            f"root_dir '{root_dir}' is outside the allowed roots. "
            "Pass a path inside the workspace, or set JEV_MCP_ALLOWED_ROOTS."
        )
    return root
```

with:

```python
def _is_within(candidate: Path, base: Path) -> bool:
    """True when `candidate` is `base` or lives under it. Resolved on both sides."""
    try:
        candidate.relative_to(base)
        return True
    except ValueError:
        return False


def _allowed_roots() -> list[Path]:
    """CWD, its ancestors, plus JEV_MCP_ALLOWED_ROOTS."""
    roots = [Path.cwd().resolve()]
    roots.extend(Path(p) for p in get_config().allowed_roots)
    return roots
```

Add CWD's **ancestors** deliberately: opencode sessions may launch the server with
`cwd` = the project root while the agent legitimately asks about a subdirectory, and
some hosts set `cwd` to a temp dir. Including ancestors is still a huge reduction
from "any path on the machine", and it never grants access outside the user's own
project tree or explicitly configured roots.

**Keep** the existing system-dir rejection as a belt-and-braces gate so
`C:\Windows` fails with the specific existing message that
`test_mock_tools.py:377-381` asserts on ("system directory" in the message).

**Breakage to expect:** `tests/test_hardening_integration.py:115-119` iterates
`["C:\\", "D:\\", "E:\\", "Z:\\"]` expecting `JevValidationError` — still passes.
`test_mock_tools.py` uses `tmp_path`, which pytest places under the system temp dir
and is **not** under the repo CWD, so those tests will now fail with the new
"outside the allowed roots" message. **Fix:** have the autouse fixture in
`tests/conftest.py` set `JEV_MCP_ALLOWED_ROOTS` to include `tempfile.gettempdir()`.
Cleaner still: add an autouse fixture that sets it to the current test's `tmp_path`
parent, so each test gets its own scope.

**Tests** (`tests/test_root_dir_allowlist.py`):

- Allowed: the repo root, a subdirectory of it, an ancestor, a path under
  `JEV_MCP_ALLOWED_ROOTS`.
- Rejected: `C:\Users\<user>`, `C:\ProgramData`, `/home/<user>`, `/var`, `/etc`,
  `/proc`, `C:\`, `/`, a path that is a *sibling* with a shared prefix
  (`<cwd>-evil` must not pass a naive `startswith` — use `relative_to`).
- Symlink/junction escape: create `<allowed>/link -> C:\Users`, confirm rejection.
  This is the case a `startswith` check would miss.
- A `JEV_MCP_ALLOWED_ROOTS` entry that does not exist is ignored, not fatal.

**Config:** add `JEV_MCP_ALLOWED_ROOTS` to `config.py`, `.env.example`,
`config/README.md`, and `README.md`. Default empty = CWD and ancestors only.

> ⚠️ **The plugin must be updated in the same commit** or it will break:
> `config/jev-plugin.example.js:457-460` calls `search_agent_skills(task, root_dir)`.
> It passes no `root_dir` (so `"."` → CWD → allowed), so it is fine. But if a user
> ever configured a `root_dir`, the semantics change. Note it in the Phase 6 file.

---

## Finding #18 — Settings precedence footgun

`load_jev_settings` (`jev_engine.py:189-243`) is **first-file-wins**:

```python
for cfg in settings_files:
    ...
    if _apply_jev_settings(settings, raw.get("jev_settings") or raw):
        settings["source"] = str(cfg)
        return settings
```

and `_apply_jev_settings` (`:151-165`) returns `True` if the file has *any* Jev key.
This repo ships `jevs_settings.json` with all three keys at defaults:

```json
{ "enable_model_routing": false, "models": {"fast":"", "balanced":"", "frontier":""}, "scan_paths": [] }
```

So inside this repo, `~/.config/opencode/jevs_settings.json` is **never read**.
Turning routing on in the user-level file silently does nothing. The docs say
"project wins over user" (`README.md:244-249`) which sounds intentional, but the
practical effect — a default-valued project file permanently shadowing user
configuration — is a trap, especially since `jevs_settings.json` is explicitly
documented as safe to commit and share (`:52`).

### Edits — deep-merge user → project

Replace the first-wins loop with an accumulating merge. Keep
`settings["source"]` as a list of every file that contributed, which is more useful
for diagnostics than a single path.

```python
def _merge_jev_settings(target: dict, jev: dict) -> bool:
    """Deep-merge a jev_settings dict onto `target`. Returns True if it matched.

    Merging rather than first-wins means a project file that only overrides
    `enable_model_routing` no longer discards the user file's `models` map.
    """
    jev = jev or {}
    if not isinstance(jev, dict):
        return False
    matched = False
    if isinstance(jev.get("enable_model_routing"), bool):
        target["enable_model_routing"] = jev["enable_model_routing"]
        matched = True
    models = jev.get("models")
    if isinstance(models, dict) and models:
        merged = dict(target["models"])
        merged.update({k: v for k, v in models.items() if v})
        target["models"] = merged
        matched = True
    scan_paths = jev.get("scan_paths")
    if isinstance(scan_paths, list):
        extras = [str(p) for p in scan_paths if isinstance(p, str)]
        target["scan_paths"] = list(dict.fromkeys(list(target["scan_paths"]) + extras))
        matched = True
    return matched
```

Note `models` now **unions non-empty** values rather than replacing. That is the
right semantic: a project can add a tier without deleting the user's others, and an
empty `""` placeholder (the documented way to disable a tier —
`config/README.md:141`) still means "ignore". Preserve that: an explicit `""` should
*remove* an inherited value. Handle it:

```python
    if isinstance(models, dict) and models:
        merged = dict(target["models"])
        for k, v in models.items():
            if v:
                merged[k] = v
            else:
                merged.pop(k, None)   # explicit "" clears an inherited tier
        target["models"] = merged
```

Restructure the loader to iterate **user files first, then project files**, applying
each:

```python
def load_jev_settings() -> dict:
    global _cached_settings, _cached_settings_mtimes
    settings_files = _find_settings_files()     # project first (unchanged discovery)
    config_files = _find_config_files()
    current_mtimes = _get_files_mtime_signature(settings_files + config_files)
    if _cached_settings is not None and _cached_settings_mtimes == current_mtimes:
        return _cached_settings

    settings = {
        "enable_model_routing": False,
        "models": {},
        "scan_paths": list(DEFAULT_SCAN_PATHS),
        "source": None,
        "sources": [],
    }
    # Apply user-level config first, then project config, so the project
    # overrides the user and neither silently discards the other.
    ordered = list(reversed(settings_files)) + list(reversed(config_files))
    for cfg in ordered:
        try:
            raw = json.loads(cfg.read_text(encoding="utf-8"))
        except Exception as e:
            log_event("settings_parse_error", file=str(cfg), error=str(e))
            sys.stderr.write(f"jev_engine: failed reading {cfg}: {e}\n")
            continue
        if not isinstance(raw, dict):
            continue
        if _merge_jev_settings(settings, raw.get("jev_settings") or raw):
            settings["source"] = str(cfg)
            settings["sources"].append(str(cfg))

    _cached_settings = settings
    _cached_settings_mtimes = current_mtimes
    return settings
```

Keep `settings["source"]` as the **last** contributor (project wins for display) and
add `sources` for the full list. That preserves every existing consumer —
`select_model_tier` reads `enable_model_routing`, `models`, `scan_paths`;
`get_scan_paths` reads `scan_paths`.

**Also fix the cache race** (`:199-206`): mtimes are stat'd *before* the files are
read, so an edit landing between the stat and the read stores old mtimes with new
content — and the change is then never picked up. Re-stat after reading:

```python
    for cfg in ordered:
        ...read...
    # Re-stat after reading: a file edited between the signature and the read
    # must not leave a stale mtime paired with fresh content in the cache.
    _cached_settings_mtimes = _get_files_mtime_signature(settings_files + config_files)
```

**Tests** (`tests/test_settings_merge.py`):

- User has `models: {fast: "a/f"}`, project has only
  `{"enable_model_routing": true}` → result has `fast` **and** routing on.
- User has `models: {fast: "a/f", balanced: "a/b"}`, project has
  `models: {"fast": "", "frontier": "a/x"}` → result is `{balanced, frontier}`.
  (`""` clears.)
- Project routing `false` overrides user routing `true`.
- `scan_paths` **union** across files, deduped, defaults preserved.
- A malformed project file does not prevent the user file from being applied.
- `sources` lists both; `source` is the project file.
- The cache-race case: write a file, load, edit, load → new content. Hard to test
  deterministically; instead assert the post-read re-stat is what gets stored by
  monkeypatching `_get_files_mtime_signature` to return different values per call.

**Docs:** update `README.md:244-249` and `config/README.md:131-134` from
"first file with any Jev key wins" to "user config is merged first, then project
config overrides per key".

**Compatibility risk to flag:** `tests/test_hardening_integration.py:167-178` and
`tests/test_mock_tools.py:425-459` assert on `settings["source"]` and on
`_find_settings_files` ordering. `_find_settings_files` ordering is unchanged (the
merge handles precedence, not discovery). The `source` assertion becomes
last-writer-wins — check `test_load_jev_settings_finds_script_dir_settings_from_different_cwd`
(`test_mock_tools.py:453-459`) still holds.

---

## Finding #19 — Socket leak

```python
# jev_engine.py:284-288  — mock mode
if cfg.mock:
    with _client_lock:
        _cached_client = None      # httpx2.Client dropped without .close()
        _cached_client_key = None
    return None

# jev_engine.py:265-270  — test reset
def _reset_client_cache() -> None:
    global _cached_client, _cached_client_key
    with _client_lock:
        _cached_client = None      # same
        _cached_client_key = None
```

`TypeSafeClient` wraps an `httpx2.Client`
(`typesafe_sdk/_core/client/sync/client.py:87`) with a connection pool.
`_reset_client_cache` runs on **every test** via the autouse fixture
(`tests/conftest.py:24-28`), so the suite leaks a pool per test. In the server, a
`JEV_MCP_MOCK` toggle leaks one pool. `TypeSafeClient.close()` exists
(`client.py:225-227`).

### Edits

```python
def _close_cached_client() -> None:
    """Close the cached client so its connection pool is released."""
    global _cached_client, _cached_client_key
    with _client_lock:
        client, _cached_client, _cached_client_key = _cached_client, None, None
    if client is not None:
        try:
            client.close()
        except Exception:
            log_event("client_close_failed", error=type(client).__name__)


def _reset_client_cache() -> None:
    """Clear the cached client. Exposed for tests."""
    _close_cached_client()
```

The `close()` happens **outside** the lock so a slow close cannot block other
callers. Route the mock branch and the cache-key-miss path through the same helper.

**Cache key gap:** `(cfg.api_key, cfg.timeout_ms, cfg.model)` (`:293`) omits
`TYPESAFE_BASE_URL` and `TYPESAFE_DEFAULT_MODEL`. The SDK reads both
(`typesafe_sdk/_core/config.py:50-60,61-62`), so changing the base URL leaves a
stale client pointed at the old host. Add both to the key:

```python
cache_key = (
    cfg.api_key,
    cfg.timeout_ms,
    cfg.model,
    (os.getenv("TYPESAFE_BASE_URL") or "").strip().rstrip("/"),
    (os.getenv("TYPESAFE_DEFAULT_MODEL") or "").strip(),
)
```

**Tests:**

- 200 sequential `get_client()` / `_reset_client_cache()` cycles with a spy client
  → `close()` called 200 times.
- A `close()` that raises does not propagate and does not leave a stale client.
- Setting `TYPESAFE_BASE_URL` between calls yields a **different** client instance.
- Concurrent `get_client()` still returns one shared instance (the existing
  `test_concurrent_get_client_thread_safety` must keep passing).

---

## Finding #20 — Cached settings aliased into results

```python
# jev_engine.py:820-831
result = {
    ...
    "model_map": settings["models"],   # the LIVE cached dict
}
```

`load_jev_settings` returns `_cached_settings` itself — the same object on every
call (`test_hardening_integration.py:167-178` asserts `s1 is s2` deliberately). So
`result["model_map"]` is a live reference into module state. Any consumer that mutates
it corrupts the cache for the process.

### Edits

```python
"model_map": dict(settings["models"]),
```

And in `get_scan_paths` (`:246-256`), the returned list is already fresh, so it is
fine. But `load_jev_settings` returning shared mutable state is the underlying smell —
have it return a shallow copy plus a copied `models`:

```python
    if _cached_settings is not None and _cached_settings_mtimes == current_mtimes:
        return _snapshot(_cached_settings)
```

```python
def _snapshot(settings: dict) -> dict:
    """A defensive copy so callers cannot mutate the module-level cache."""
    out = dict(settings)
    out["models"] = dict(settings.get("models") or {})
    out["scan_paths"] = list(settings.get("scan_paths") or [])
    out["sources"] = list(settings.get("sources") or [])
    return out
```

**This breaks the `s1 is s2` assertion** at
`tests/test_hardening_integration.py:175`. Update it to `assert s1 == s2` plus
`assert s1 is not s2`, and add a mutation test. The identity check was testing cache
*identity*; equality plus a not-identity assertion tests cache *behaviour*, which is
what actually matters.

**Tests:**
- Mutating `result["model_map"]` then reloading leaves the cache intact.
- Mutating a returned `scan_paths` list does not affect the next call.
- `s1 == s2 and s1 is not s2` after two loads.

---

## Files touched

| File | Change |
|---|---|
| `jev_engine.py` | `_remaining_seconds`, `_request` deadline, `execute_system_one(timeout_s=)`, `_Breaker`, `_validate_root_dir` allowlist, `_is_within`, `_allowed_roots`, `_close_cached_client`, cache key, `_snapshot`, `_merge_jev_settings`, post-read re-stat |
| `config.py` | `JEV_MCP_ALLOWED_ROOTS`, `JEV_MCP_BREAKER_THRESHOLD`, `JEV_MCP_BREAKER_COOLDOWN_S`, `JEV_MCP_AUTH_COOLDOWN_S` |
| `tests/conftest.py` | autouse fixture granting `tempfile.gettempdir()` in `JEV_MCP_ALLOWED_ROOTS` |
| `tests/test_deadline.py` | **new** |
| `tests/test_root_dir_allowlist.py` | **new** |
| `tests/test_settings_merge.py` | **new** |
| `tests/test_breaker.py` | **new** |
| `tests/test_client_cache.py` | **new** — close + cache-key |
| `tests/test_hardening_integration.py` | `s1 is s2` → `==` and `is not` |
| `.env.example`, `config/README.md`, `README.md` | new env vars; timeout semantics; settings-merge semantics |

## Verification

```powershell
cd D:\mcp\jev-typesafe-mcp
& .\.venv\Scripts\python.exe -m py_compile jev_engine.py config.py
& .\.venv\Scripts\python.exe -m pytest tests -q
& .\.venv\Scripts\python.exe -m pytest tests/test_deadline.py -v --durations=5
& .\.venv\Scripts\python.exe scripts\bench_jev.py --assert
& .\.venv\Scripts\python.exe -c "from jev_engine import load_jev_settings; print(load_jev_settings())"
& .\.venv\Scripts\python.exe scripts\diag_mcp.py --tool search_target_files --task "x" --root_dir . --mock
$env:JEV_MCP_ALLOWED_ROOTS="D:\some\other\project"
& .\.venv\Scripts\python.exe scripts\diag_mcp.py --tool search_target_files --task "x" --root_dir "D:\some\other\project" --mock
Remove-Item Env:\JEV_MCP_ALLOWED_ROOTS
node tests/test_plugin.mjs
```

## Definition of done

- [ ] Total tool-call time is bounded by `JEV_MCP_TIMEOUT_MS` (regression test fails on old code)
- [ ] `RetryPolicy` carries `timeout` and `api_timeout_error=False`
- [ ] `root_dir` outside CWD/ancestors/`JEV_MCP_ALLOWED_ROOTS` is rejected
- [ ] Sibling-prefix (`<cwd>-evil`) and symlink escapes are rejected
- [ ] 3 consecutive provider failures open a breaker; the 4th call never reaches the client
- [ ] User + project settings merge per key; `""` clears an inherited tier
- [ ] `_reset_client_cache` calls `close()`; base URL change yields a new client
- [ ] `result["model_map"]` is a copy, not the cached dict
- [ ] `pytest`, `bench_jev.py --assert`, `node tests/test_plugin.mjs` all green

## Commit

```
fix(engine): bound tool-call latency and confine root_dir to an allowlist

JEV_MCP_TIMEOUT_MS did not bound anything. TypeSafeClient got a 30s
per-attempt timeout and a RetryPolicy built without `timeout`, which defaults
to 30s, so three attempts plus backoff put the worst case at ~91.5s for one
tool call. Pass the total budget to RetryPolicy.timeout, stop retrying timeouts
(they only burn budget), and clamp each attempt to the remaining deadline --
the approach the reference uses in typesafe.ts:13-16. Enforce the same
deadline in mock mode, which is CPU-bound and can exceed a small budget.

root_dir was a denylist of five POSIX paths and two Windows environment
variables, so /home, /var, /proc, C:\Users and C:\ProgramData were all
reachable from an LLM-supplied argument. Replace it with an allowlist: the
process CWD and its ancestors, plus anything under JEV_MCP_ALLOWED_ROOTS.
The existing system-dir rejection stays as a second gate. Containment uses
relative_to, not startswith, so <cwd>-evil and symlinked escapes are rejected.

Also: a circuit breaker so a revoked key or a 5xx outage short-circuits instead
of paying the full retry budget on every call (and on every plugin message);
deep-merge user and project jev_settings instead of first-wins, so a
default-valued project file stops permanently shadowing the user config; close
the cached httpx client on invalidation and add base URL to the cache key; and
return a defensive snapshot so tool results cannot alias the settings cache.
```
