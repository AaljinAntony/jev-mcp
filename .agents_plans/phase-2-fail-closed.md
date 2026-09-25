# Phase 2 — Close the Fail-Open Holes

**Goal:** make it impossible for a failure to look like a decision, and impossible
for a non-finite number to reach the wire.

**Closes:** findings #1, #2, #3, #4, #5, #6, #22.

**Depends on:** Phase 1 (needs the drift guard so plugin changes stay honest).

**Risk:** medium. This changes the MCP response surface. Every existing envelope key
is preserved — only *additions* and `is_error` semantics change. Read
`Migration notes` at the bottom before starting.

---

## Finding #1 — `NaN` passes the fail-closed validator

`jev_validation.py` has **no `math.isfinite` guards anywhere**. Every range check is
a comparison, and every comparison against `NaN` is `False`:

```python
# jev_validation.py:41-44
if not isinstance(value, (int, float)) or isinstance(value, bool):
    raise JevResponseError(...)
if value < 0 or value > 1:      # nan < 0 -> False ; nan > 1 -> False  => PASSES
    raise JevResponseError(...)
```

Same hole at `_non_negative_number` (`:32-36`), `_validate_probabilities`
(`:47-60`, where `abs(total - 1.0) > PROBABILITY_SUM_TOLERANCE` is also `False` for
`NaN`), `_validate_confidence` (`:63-69`), and `_validate_score` (`:88-124`).

The validator is explicitly documented as the fail-closed boundary
(`jev_validation.py:1-7`, `README.md:85-88`). A `NaN` that passes it then reaches
`json.dumps`, which by default emits bare `NaN` — **not valid JSON**. opencode's
parser throws, and the failure surfaces as an unrelated "Unexpected error occurred"
with no trace of the actual cause.

Two independent fixes are needed: reject `NaN` at the boundary, and refuse to
*emit* it as a backstop.

### Edits

**`jev_validation.py`** — add a helper and use it everywhere:

```python
import math

def _is_finite_number(value) -> bool:
    """True only for a real, non-bool int/float. NaN/Inf are rejected."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
```

Then replace the four guards:

| Location | Current | Replace with |
|---|---|---|
| `_non_negative_number` `:33` | `not isinstance(value, (int, float)) or isinstance(value, bool)` | `not _is_finite_number(value)` |
| `_validate_noul` `:41` | `not isinstance(value, (int, float)) or isinstance(value, bool)` | `not _is_finite_number(value)` |
| `_validate_confidence` `:65` | `not isinstance(confidence, (int, float)) or isinstance(value, bool)` | `not _is_finite_number(confidence)` |
| `_validate_score` `:94` | `not isinstance(score, (int, float)) or isinstance(score, bool)` | `not _is_finite_number(score)` |

Add explicit `math.isfinite` assertions to the two aggregate checks that a
per-element guard does not cover:

- `_validate_probabilities` — after the loop, `if not math.isfinite(total): raise JevResponseError(...)`.
  (A single `NaN` element makes `total` `NaN`, so the sum check silently passes.)
- `_validate_score` — `expected_mean` is `sum(i * p)`, same issue; guard it.

**Backstop at the serialization boundary.** The `mcp` runtime serializes the
returned dict itself (`mcpserver/utilities/func_metadata.py:226`,
`model_dump(mode="json")`), so you cannot pass `allow_nan=False` to it. Add an
explicit final serialization in `_run` (`jev_mcp.py:49-65`) that both validates
JSON strictness and produces the text the tool returns:

```python
def _assert_finite_json(value, path="result") -> None:
    """Walk the envelope and reject any non-finite float before serialization."""
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise JevResponseError(f"non-finite number at {path}")
        return
    if isinstance(value, dict):
        for k, v in value.items():
            _assert_finite_json(v, f"{path}.{k}")
        return
    if isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _assert_finite_json(v, f"{path}[{i}]")
```

Call it in `_run` after `fn()` returns, **before** logging the result, and also
inside `mock_system_one` (`mock.py:125-152`) so mock mode exercises the same path.

`math.isfinite` on a `Decimal`/numpy scalar would `TypeError`; the
`isinstance(value, float)` gate above excludes those, and anything else falls
through harmlessly.

**Tests** (new file `tests/test_validation_nan.py`):

- `NaN` in `noul`, `confidence`, each `probabilities` value, probability `total`,
  `score`, and `expected_mean` → `JevResponseError`.
- `float("inf")` / `float("-inf")` in the same positions → `JevResponseError`.
- `bool` is still rejected (`True` is `1`, an `int` — the `not isinstance(value, bool)`
  half of the guard must survive the refactor).
- `_assert_finite_json` catches a nested `NaN` at `result.coverage.estimated_tokens.state`.
- `json.dumps(monkeypatched_response)` never emits `NaN`: assert the literal string
  `"NaN"` is absent from the serialized envelope.

---

## Finding #2 — Three envelope shapes per tool

Confirmed live during the audit: `select_model_tier` with routing off returned **no
`coverage` key**. The three no-work early returns skip the whole envelope:

| Site | Returns | Missing |
|---|---|---|
| `jev_engine.py:544-550` (`find_agent_resources`, no candidates) | `matched/count/resources/summary` | `action/confidence/truncated/coverage/model/usage/file/content/primary/primary_probability/ranked` |
| `jev_engine.py:754-755` (`select_target_files`, no candidates) | `matched/files/exists` | `action/confidence/truncated/coverage/model/usage/probability/ranked` |
| `jev_engine.py:820-833` (`select_model_tier`, routing off) | 10 keys | `coverage` |

An LLM consuming this sees two different contracts from the same tool depending on
an invisible branch.

### Edits

Build one envelope per tool and use it on **every** path. Keep the module-local
builders pure (no I/O) so they are trivially testable.

**`jev_engine.py`** — add near the envelope helpers (after `_response_meta`, ~line 394):

```python
def _coverage_envelope(fitted: Optional[dict]) -> dict:
    """A coverage block for paths where no Jev request was made."""
    if fitted is not None:
        return fitted["coverage"]
    return {
        "complete": True,
        "original_chars": 0,
        "evaluated_chars": 0,
        "estimated_tokens": {"state": 0, "questions": 0, "longest_question": 0},
        "estimator": "chars/4",
    }
```

**`find_agent_resources`** — replace the `:544-550` early return with:

```python
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
        "action": "auto",          # nothing to decide; see note below
        "confidence": None,
        "truncated": False,
        "coverage": _coverage_envelope(None),
        "model": None,
        "usage": None,
    }
    return result
```

> **On `action` for the empty case:** `"auto"` is correct — there is no decision to
> make, and returning `"escalate"` would train the calling model to distrust a
> routine empty result. `confidence: None` (not `0.0`) is what distinguishes "not
> judged" from "judged with no confidence", and it matches what
> `select_model_tier` already does on the disabled path
> (`confidence: None` at `jev_engine.py:827`).

**`select_target_files`** — replace `:754-755`:

```python
if not candidates:
    result = {
        "matched": False,
        "files": [],
        "exists": "no_candidates",     # was "absent" — see note
        "probability": 0.0,
        "ranked": [],
        "action": "auto",
        "confidence": None,
        "truncated": False,
        "coverage": _coverage_envelope(None),
        "model": None,
        "usage": None,
    }
    return result
```

> **`exists` semantics.** `_exists_verdict` (`:675-678`) already returns
> `"absent"` for a genuine "the model says nothing fits". Reusing `"absent"` for
> "there were zero candidates" conflates *no evidence* with *evidence of absence* —
> the workspace might simply be empty or fully filtered. Introducing
> `"no_candidates"` makes that distinguishable.
> **This is a breaking change to the `exists` value set.** Documented in
> `Migration notes`. `test_mock_tools.py:204` asserts
> `result["exists"] in {"absent", "partial"}` on the *escape-hatch* path (which
> still returns `"absent"`), so it keeps passing.

**`select_model_tier`** — add `"coverage": _coverage_envelope(None)` to the
initial `result` dict at `:820-831` so the disabled return has it.

**Tests:** a table-driven test asserting `set(result.keys())` is **identical** for
every branch of every tool:

- `find_agent_resources`: no-candidates / no-match / match
- `select_target_files`: no-candidates / escape-hatch / match
- `select_model_tier`: routing-off / routing-on-low-confidence / routing-on

---

## Finding #3 — Errors are reported as successes

`jev_mcp.py:49-65`:

```python
def _run(tool, fn, **args):
    try:
        result = fn()
        ...
        return result          # <- normal CallToolResult, is_error NOT set
    except Exception as err:
        envelope = {"error": error_details(err)}
        ...
        return envelope        # <- still a SUCCESS from the harness's view
```

Verified in the installed runtime: `is_error` is set **only** when the tool raises
(`mcp/server/mcpserver/server.py:437-447`), and `convert_result` returns a bare
`CallToolResult(content=...)` with no `is_error` for a plain dict return
(`mcp/server/mcpserver/utilities/func_metadata.py:211-212`).

Consequence: an LLM orchestrator sees `isError: false` and a JSON blob containing
`{"error": ...}`. Nothing surfaces `retryable`, and a harness that keys off
`isError` will treat a 500 as a success and move on.

### Edits

The SDK offers `mcp.server.mcpserver.exceptions.ToolError`, which the server
converts to `CallToolResult(is_error=True)` with `str(exc)` as the text
(`server.py:447`). So raise, but carry the machine-readable body in the message.

**`jev_errors.py`** — add:

```python
class JevToolError(Exception):
    """A typed Jev failure that must reach the client as isError=true.

    The JSON envelope is embedded in the message so the structured body
    survives the SDK's string-only error text.
    """
    def __init__(self, envelope: dict) -> None:
        super().__init__(json.dumps(envelope, ensure_ascii=False))
        self.envelope = envelope
```

**`jev_mcp.py`** — rework `_run`:

```python
def _run(tool, str_fn, **args):
    start = time.perf_counter()
    try:
        result = str_fn()
        _assert_finite_json(result)
        log_tool_call(tool, (time.perf_counter() - start) * 1000, args=args, result=result)
        return result
    except Exception as err:
        envelope = {"error": error_details(err)}
        ms = (time.perf_counter() - start) * 1000
        log_tool_call(tool, ms, args=args, error=envelope)
        log_exception("tool_error", err, tool=tool, args=args)
        raise JevToolError(envelope) from err
```

Two things to verify before committing:

1. `func_metadata.convert_result` is called at `tools/base.py:188-189` and wraps
   the tool body. `ToolError` is an `MCPError` subclass, so `tools/base.py:192-198`
   re-raises it untouched, and `_handle_call_tool` catches it at `server.py:437`
   and produces `is_error=True`. **Confirm this with a real
   `scripts/diag_mcp.py` run** rather than assuming — if `is_error` does not come
   back `true`, fall back to returning `CallToolResult(is_error=True, ...)`
   directly.
2. `scripts/diag_mcp.py:126-129` currently checks for the envelope inside
   `result.content[0].text`. It still works (the JSON is now the error text), but
   the printed `result keys:` line changes. Update the script to also report
   `isError` so the regression is visible.

**Tests:**
- `jev_mcp.guardrail_command("x" * 200_000)` raises `JevToolError`; its
  `str(e)` parses as JSON with `error.code == "INVALID_INPUT"`.
- Over `diag_mcp.py`, a bad `root_dir` produces `isError: true`.
- Success paths still return a plain dict and `isError` absent/false.
- **`test_mock_tools.py` needs updating**: it calls `jev_mcp.guardrail_command(...)`
  and `jev_mcp.search_agent_skills(...)` expecting a returned dict
  (`:147-149`, `:364-367`, `:417-419`). Wrap those in `pytest.raises(JevToolError)`
  and assert on the parsed message. This is a real, intentional test change.

---

## Finding #4 — `.env` overrides injected env

`config.py:21-31`:

```python
def ensure_dotenv():
    try:
        from dotenv import load_dotenv
        env_path = Path(__file__).resolve().parent / ".env"
        if env_path.exists():
            load_dotenv(dotenv_path=env_path, override=True)   # <-- .env WINS
        else:
            load_dotenv(override=True)                          # <-- walks up from cwd
    except ImportError:
        pass
```

`override=True` means the file **overwrites** variables already in the process
environment. opencode injects `TYPESAFE_API_KEY` (and possibly `JEV_MCP_MOCK`,
`JEV_MCP_MODEL`, …) via the MCP `environment` block
(`config/opencode.example.json:11-18`). With `override=True` a stale `.env` beats
the injected value — the opposite of a fallback, which is how `README.md:135`
describes it.

Worst case: a `.env` containing `JEV_MCP_MOCK=1` silently runs the mock judge in a
"live" deployment, returning plausible-looking fabricated answers.

The `else` branch is worse: `load_dotenv()` with no path searches from the CWD
upward, so an unrelated `.env` anywhere above the MCP server's working directory
can take effect.

### Edits

```python
def ensure_dotenv() -> bool:
    """Load the repo `.env` as a *fallback* only. Returns True if a file was loaded.

    `override=False` is essential: opencode injects TYPESAFE_API_KEY and the
    JEV_MCP_* knobs through the MCP `environment` block, and those are the
    authoritative source. A stale `.env` must never win — least of all
    JEV_MCP_MOCK, which would silently replace live decisions with the mock judge.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return False
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return False   # never search upward from an unrelated CWD
    return bool(load_dotenv(dotenv_path=env_path, override=False))
```

Changed the return type from `None` to `bool` (callers ignore it today; the bool is
for the Phase 7 diagnostics).

**Update the README** (`:135`) — it currently says the `.env` is "a reliable
fallback", which was aspirational rather than actual. It becomes true with this fix.

**Tests** (`tests/test_config_dotenv.py`, or extend `test_cleanup_phase8.py`):

- `load_dotenv` called with `override=False` — **the existing
  `test_ensure_dotenv_loads_with_override_true` (line 74) and its three siblings
  assert `override is True` and will now fail.** Rewrite them:
  - `test_ensure_dotenv_does_not_override` → asserts `override is False`
  - `test_ensure_dotenv_passes_env_path_when_exists` → keeps the `dotenv_path` assertion, drops the `override is True` one
  - `test_ensure_dotenv_skips_when_missing` → asserts `load_dotenv` is **not called at all**
  - `test_ensure_dotenv_handles_import_error` → unchanged
- A behavioural test with a real `dotenv_values` parse: set
  `os.environ["TYPESAFE_API_KEY"] = "from-env"`, point `ensure_dotenv` at a
  temp `.env` containing `TYPESAFE_API_KEY=from-file`, call it, assert the
  environment still holds `from-env`.

---

## Finding #5 — Score tolerance stricter than the reference

`policy.py:22-24`:

```python
#: Covers two-decimal score and probability reporting: 0.005 drift on the
#: score itself plus 0.005 * (0 + 1 + 2) = 0.015 on the distribution mean.
SCORE_MEAN_TOLERANCE = 0.02 + 1e-12
```

The comment already reasons about rubric size — but then hardcodes `0.02`, which
only happens to be right for a 3-level rubric (`0.005 * (0+1+2) = 0.015`).
The reference scales it (`reference/burnigtm-jev-mcp/src/responses.ts:39`):

```ts
if (... Math.abs(answer.score - expectedScore) > 0.01 * (expected.length - 1) || ...)
```

For a 7-level rubric the reference allows `0.06`; this code allows `0.02`. A
legitimate response is rejected → `JevResponseError` → the whole API call is
wasted and the tool returns `INVALID_RESPONSE`.

### Edits

**`policy.py`** — replace the constant with a function:

```python
#: Per-level drift allowed between a reported score and its distribution mean.
#: Two-decimal reporting gives 0.005 on the score and 0.005 on each level's
#: probability, so the total drifts by 0.005 * (1 + sum of levels).
SCORE_MEAN_PER_LEVEL_TOLERANCE = 0.01

def score_mean_tolerance(levels: int) -> float:
    """Absolute tolerance between `score` and its distribution mean.

    Scaled by rubric size: a 7-level rubric accumulates more rounding drift than
    a 2-level one, and an absolute cap rejects valid long-rubric responses.
    Mirrors reference/burnigtm-jev-mcp/src/responses.ts:39.
    """
    if levels <= 0:
        return 0.0
    return SCORE_MEAN_PER_LEVEL_TOLERANCE * (levels - 1) + 1e-12
```

Keep `SCORE_MEAN_TOLERANCE` as a deprecated alias only if something imports it —
grep first. `jev_validation.py:12` is the only importer; update it to import the
function.

**`jev_validation.py:120`**:

```python
tolerance = score_mean_tolerance(n)
if abs(float(score) - expected_mean) > tolerance:
    raise JevResponseError(
        f"answer '{name}' score {score:.4f} contradicts its distribution mean "
        f"{expected_mean:.4f} (tolerance {tolerance:.4f})"
    )
```

**Tests:**
- 3-level rubric: mean drift of `0.019` accepted, `0.021` rejected (pins the old boundary).
- **7-level rubric: drift of `0.05` accepted** — the case that fails today.
- 2-level rubric: drift of `0.019` rejected (tolerance is `0.01`).
- `score_mean_tolerance(0) == 0.0`.

---

## Finding #6 — Two attribute-access paths disagree

Inside `find_agent_resources`:

```python
# :586  — plain getattr, ignores dict-shaped answers
probs = getattr(primary_ans, "probabilities", {}) or {}
if isinstance(probs, dict):
    for opt, p in probs.items():
        if opt in candidate_files and opt not in selected_keys and p >= 0.12:
            selected_keys.append(opt)

# :628  — _attr_opt, handles BOTH SDK objects and raw dicts
for f, p in _answer_probs(res, slot).items():
```

`validate_response` explicitly supports raw dict answers
(`jev_validation.py:17-21`, and `tests/test_validation.py:9-15` builds dict-shaped
fixtures). So the two paths disagree for a legitimate input shape: the ≥0.12
sibling augmentation silently produces nothing while `ranked` still works.

### Edits

`jev_engine.py:586` → use the existing helper:

```python
probs = _answer_probs(res, "primary")
for opt, p in probs.items():
    if opt in candidate_files and opt not in selected_keys and p >= 0.12:
        selected_keys.append(opt)
```

While here, delete the now-redundant `isinstance(probs, dict)` guard —
`_answer_probs` already returns `{}` for a non-dict.

**Test:** a dict-shaped `primary` answer (mirroring `test_validation.py`'s style)
with a secondary option at `0.20` → that option appears in `resources`.

---

## Finding #22 — Error mapping gaps and metadata leak

`jev_engine.py:328-333` catches only `TypeSafeAPITimeoutError`,
`TypeSafeAPIConnectionError` and `TypeSafeAPIResponseValidationError`. A
`TypeSafeAPIError` (400/401/403/422/429/5xx) propagates raw to `_run`. That is
handled, but the mapping is wrong:

- **401 `TypeSafeAuthenticationError`** → `{"code": "API_ERROR", "retryable": false}`.
  A caller cannot distinguish "your key is wrong, stop calling" from "the server
  hiccupped". The SDK exports dedicated types for exactly this.
- **400 `TypeSafeBadRequestError`** → `API_ERROR`, not `INVALID_INPUT`. The request
  we built was wrong, which is a caller-fixable condition.
- **429** → correct today (`_retryable_status(429)` is `True`).

And `jev_errors.py:87` relays raw SDK text:

```python
if isinstance(err, TypeSafeAPIResponseValidationError):
    return {"code": "INVALID_RESPONSE", "message": str(err), ...}
```

`TypeSafeAPIError.__str__` includes the endpoint URL and the request id
(`typesafe_sdk/_core/errors.py:97-104`). That is provider request metadata reaching
the model and the log for no benefit — and `jev_errors.py:88`'s own comment says
"Provider errors can carry response bodies or request metadata; do not relay them",
which this line contradicts.

### Edits

**`jev_errors.py`** — widen the status mapping:

```python
def error_details(err: Exception) -> dict:
    ...
    if isinstance(err, TypeSafeAPIResponseValidationError):
        # field_path names the offending field and is safe; str(err) also
        # carries the endpoint URL and request id, which must not be relayed.
        return {
            "code": "INVALID_RESPONSE",
            "message": f"TypeSafe returned a response this server could not accept (at {err.field_path!r}).",
            "retryable": False,
        }
    if isinstance(err, TypeSafeAuthenticationError):
        return {
            "code": "AUTH_ERROR",
            "message": "TypeSafe rejected the API key. Check TYPESAFE_API_KEY.",
            "retryable": False,
        }
    if isinstance(err, TypeSafePermissionDeniedError):
        return {"code": "FORBIDDEN", "message": "TypeSafe denied access to this resource.", "retryable": False}
    if isinstance(err, TypeSafeBadRequestError):
        return {"code": "INVALID_INPUT", "message": "TypeSafe rejected the request as malformed.", "retryable": False}
    if isinstance(err, TypeSafeUnprocessableEntityError):
        return {"code": "INVALID_INPUT", "message": "TypeSafe rejected the request payload.", "retryable": False}
    if isinstance(err, TypeSafeNotFoundError):
        return {"code": "API_ERROR", "message": f"TypeSafe resource not found (HTTP {err.status}).", "retryable": False}
    if isinstance(err, TypeSafeRateLimitError):
        return {"code": "RATE_LIMITED", "message": "TypeSafe rate limit reached.", "retryable": True}
    if isinstance(err, TypeSafeAPIError):     # keep the generic fallback last
        return {"code": "API_ERROR", "message": f"TypeSafe API request failed (HTTP {err.status}).",
                "retryable": _retryable_status(err.status)}
```

**Ordering matters.** `TypeSafeAPIResponseValidationError` subclasses
`TypeSafeAPIError` (`typesafe_sdk/_core/errors.py:176`), and every status subclass
inherits from it too — so the validation check and the status checks must both
precede the generic `TypeSafeAPIError` branch. The SDK's own `api_error()` maps
401→`TypeSafeAuthenticationError` etc. (`errors.py:188-200`), so the concrete type
is reliable. Add a comment block preserving the inheritance documentation already
at `jev_errors.py:57-63` — it is the single most fragile part of this file.

**`jev_engine.py`** — add `TypeSafeAPIError` as a catch-all after the three
specific ones, re-raising unchanged so `error_details` does the mapping. Actually
it already propagates correctly; add a `# HTTP status errors map in error_details()`
comment at `:333` so the omission reads as deliberate.

**Tests:** table-driven over each SDK error class → assert `code` and `retryable`.
Add a regression asserting the `INVALID_RESPONSE` message does **not** contain the
endpoint URL or the `request_id=` substring.

---

## Out of scope for this phase

- **`is_error` propagation into the plugin.** Phase 6 rewrites the plugin and gives
  it error-envelope handling.
- **The dead `jev_settings`-in-`opencode.json` lookup** (`_find_config_files`,
  `jev_engine.py:132-148`). `config/README.md:38-41` documents that opencode's
  config schema is strict, so this path is unreachable. Phase 7.

---

## Files touched

| File | Change |
|---|---|
| `jev_validation.py` | `math.isfinite` guards ×6, `score_mean_tolerance`, `_attr` unchanged |
| `jev_errors.py` | `JevToolError`, widened status mapping, stop relaying `str(err)` |
| `jev_engine.py` | `_coverage_envelope`, unified envelopes, `_answer_probs` at `:586`, `TypeSafeAPIError` comment |
| `jev_mcp.py` | `_run` raises `JevToolError`, `_assert_finite_json` |
| `policy.py` | `SCORE_MEAN_TOLERANCE` → `score_mean_tolerance()` |
| `config.py` | `ensure_dotenv` → `override=False`, no upward search, returns `bool` |
| `mock.py` | `_assert_finite_json` on the mock response |
| `scripts/diag_mcp.py` | report `isError` |
| `tests/test_validation_nan.py` | **new** |
| `tests/test_config_dotenv.py` | **new** |
| `tests/test_errors.py` | widen the status-mapping table |
| `tests/test_mock_tools.py` | update the 3 error-return call sites for `JevToolError` |
| `tests/test_cleanup_phase8.py` | rewrite the 3 `override is True` assertions |
| `README.md` | correct the `.env` precedence claim (`:135`) |

## Verification

```powershell
cd D:\mcp\jev-typesafe-mcp
& .\.venv\Scripts\python.exe -m py_compile jev_validation.py jev_errors.py jev_engine.py jev_mcp.py policy.py config.py mock.py
& .\.venv\Scripts\python.exe -m pytest tests -q
& .\.venv\Scripts\python.exe scripts\bench_jev.py --assert      # must not regress
& .\.venv\Scripts\python.exe scripts\diag_mcp.py --tool search_agent_skills --task "fix ui bug" --root_dir . --mock
node tests/test_plugin.mjs
```

Confirm by hand that `diag_mcp.py` now prints `isError: true` for a bad `root_dir`:

```powershell
& .\.venv\Scripts\python.exe scripts\diag_mcp.py --tool search_agent_skills --task "x" --root_dir "C:\Windows" --mock
```

## Definition of done

- [ ] No `NaN`/`Inf` survives `validate_response` for any primitive
- [ ] `set(result.keys())` identical across every branch of every tool
- [ ] `diag_mcp.py` reports `isError: true` for every error path
- [ ] `.env` can no longer override an injected `TYPESAFE_API_KEY` or `JEV_MCP_MOCK`
- [ ] A 7-level score with 0.05 mean drift validates
- [ ] Dict-shaped answers get the ≥0.12 augmentation
- [ ] 401/400/429 map to `AUTH_ERROR`/`INVALID_INPUT`/`RATE_LIMITED`
- [ ] `pytest tests -q` green; `bench_jev.py --assert` green

## Migration notes

- `isError` is now `true` on failures. A harness that ignored it and parsed
  `{"error": ...}` from a successful result still works — the JSON body is
  unchanged — but will now also see the error flag.
- `exists` gained `"no_candidates"` for the zero-candidate path of
  `search_target_files`. Existing `"answered"` / `"absent"` / `"partial"` values
  are unchanged.
- `error.code` gained `AUTH_ERROR`, `FORBIDDEN`, `RATE_LIMITED`; `400` moved from
  `API_ERROR` to `INVALID_INPUT`. Any switch on `code` needs a new arm.
- The `INVALID_RESPONSE` message no longer contains the endpoint or request id.

## Commit

```
fix(engine): close fail-open holes in validation, envelopes and error reporting

- Reject NaN/Inf in every jev_validation range check. All four were pure
  comparisons, and every comparison against NaN is False, so a non-finite
  noul/confidence/probability/score passed the fail-closed boundary and then
  reached json.dumps, which emits bare NaN -- invalid JSON that opencode's
  parser rejects with no trace of the cause. Adds _assert_finite_json as a
  boundary backstop.
- Return one envelope shape per tool. The three no-work early returns omitted
  action/confidence/truncated/coverage/model/usage, so callers saw two
  contracts from the same tool depending on an invisible branch.
- Raise JevToolError from _run so failures reach the client with isError=true
  and a parsed envelope, instead of a successful result whose body happened to
  contain "error".
- ensure_dotenv now uses override=False and never searches upward from an
  unrelated CWD. With override=True the repo .env overrode the TYPESAFE_API_KEY
  opencode injects, and could silently enable JEV_MCP_MOCK.
- Scale the score-mean tolerance by rubric size, mirroring the reference's
  0.01 * (n-1). The fixed 0.02 rejected valid 7-level responses.
- Read probabilities through _answer_probs in find_agent_resources, matching
  the sibling call site; dict-shaped answers lost the >=0.12 augmentation.
- Map 401/403/400/422/404/429 to specific codes and stop relaying the SDK's
  str(err), which carries the endpoint URL and request id.
```
