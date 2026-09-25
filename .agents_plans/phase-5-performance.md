# Phase 5 — Performance

**Goal:** remove the wasted CPU and I/O on every call. Use the Phase 1 benchmark to
prove each change.

**Closes:** findings #23, #24, #25, #26, #27, #28, #29.

**Depends on:** Phase 1 (benchmark), Phase 3 (preview reads make the scanner cache
and the read bound more valuable), Phase 4 (no conflicts).

**Risk:** low-medium. Mostly local changes. The two behavioural ones — dropping the
duplicated `file`/`content` keys and the log redaction rewrite — need doc updates.

---

## Finding #23 — `estimate_tokens` is a per-character Python loop

```python
# limits.py:26-36
def estimate_tokens(value) -> int:
    text = stringify_state(value)
    ascii_chars = 0
    other_chars = 0
    for char in text:
        if ord(char) <= 0x7F:
            ascii_chars += 1
        else:
            other_chars += 1
    return math.ceil(ascii_chars / 4 + other_chars)
```

One Python-level loop iteration per character, plus a per-char `ord()`. On a
100 000-char state that is ~100 k iterations, and `fit_state` calls it several times
over the state **and** every question value. A live `search_target_files` call
reported `state: 74, questions: 622` tokens — and `questions` is a 250-key dict of
long descriptions.

Call sites: `limits.py:35,76,90,91,105,113,149,175,176`, `mock.py:149`. `fit_state`
alone invokes it 3–4× per request.

### Edits

**`limits.py`** — count non-ASCII at C speed with a precompiled pattern:

```python
_NON_ASCII = re.compile(r"[^\x00-\x7f]")

def estimate_tokens(value) -> int:
    """Rough token estimate: ASCII chars / 4 + non-ASCII chars.

    Counting non-ASCII with a compiled regex keeps this at C speed; a
    per-character Python loop dominates every other cost in the request path
    once a Choice carries 250 candidate previews.
    """
    text = stringify_state(value)
    if not text:
        return 0
    non_ascii = len(_NON_ASCII.findall(text))
    ascii_chars = len(text) - non_ascii
    return math.ceil(ascii_chars / 4 + non_ascii)
```

`re` over a str is a C loop; `findall` allocates a list of matched chars, which for a
mostly-ASCII string is tiny. Where the string is expected to be nearly all non-ASCII
(CJK) the list is large — acceptable, and still far faster than the Python loop.

**Exactness matters more than speed here.** Add a pin:

```python
def test_estimate_tokens_matches_reference_implementation():
    """The fast path must be bit-identical to the original loop."""
    for text in ("", "abc", "a" * 4000, "中文字" * 500, "mix é 123",
                 "emoji 🎉 test", "\x00\x7f\x80"):
        expected = math.ceil(
            sum(len(t) for t in re.findall(r"[^\x00-\x7f]", text)) * 0 + 0
        ) or None
        # ... spelled out longhand in the real test
```

Write it longhand so the test is self-contained and does not import the thing it is
verifying.

**Also replace the linear truncation scan** (`limits.py:140-160`) with the
reference's binary search (`reference/.../limits.ts:96-112`), which is O(log n)
`estimate_tokens` calls instead of one full pass:

```python
def truncate_to_token_budget(text: str, budget: int) -> str:
    """Truncate text to fit within the estimated token budget."""
    if budget <= 0:
        return ""
    if len(text) <= budget:
        return text
    if len(text) <= budget * 4 and estimate_tokens(text) <= budget:
        return text

    marker_tokens = estimate_tokens(TRUNCATION_MARKER)
    if budget < marker_tokens:
        candidate = truncate_text(text, budget * 4)
        while estimate_tokens(candidate) > budget and len(candidate) > 0:
            candidate = candidate[:-1]
        return candidate

    # Binary search on the char cap: the reference does this in
    # reference/burnigtm-jev-mcp/src/limits.ts:96-112.
    low, high = 0, min(len(text), max(budget, budget * 4))
    best = ""
    while low <= high:
        mid = (low + high) // 2
        candidate = truncate_text(text, mid)
        if estimate_tokens(candidate) <= budget:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best
```

This **removes** `_char_budget_for_tokens` (`limits.py:140-160`) — its job is now
done by the search. Confirm nothing else imports it (`grep` first).

The surrogate-pair guard must survive. `truncate_text` (`limits.py:48-59`) already
handles it, so the binary search inherits it for free — that is a real correctness
gain over the current `_char_budget_for_tokens` + manual `end -= 1` dance.

**Tests** (`tests/test_limits.py`, extend):

- Exact-equality pin against the original loop over the strings above, including a
  lone surrogate and a 4 000-char ASCII string (the `budget * 4` fast path boundary).
- `truncate_to_token_budget` never returns more than `budget` estimated tokens
  (property check over ~200 random strings and budgets).
- A surrogate pair at the truncation boundary is never split.
- `_char_budget_for_tokens` is gone; nothing imports it.

---

## Finding #24 — Unbounded per-call filesystem work

Two scanners, no caching:

```python
# jev_engine.py:532-542  — find_agent_resources, every call
for sdir in search_dirs:
    if not sdir.exists(): continue
    for p in sdir.rglob("*.md"):
        if p.is_file():
            try: rel = p.relative_to(root).as_posix()
            except ValueError: continue
            candidate_files[rel] = p
```

```python
# jev_engine.py:692-701  — select_target_files, every call
result = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                        cwd=str(root), capture_output=True, text=True, timeout=5)
```

`DEFAULT_SCAN_PATHS` includes both `.agents` and `.agents/skills`, so the same file
is discovered repeatedly within one call (deduped by key, but still walked twice).
`rglob` has no depth bound and no file cap, so a deep `node_modules`-style tree under
`.agents` is fully traversed before the 250-cap ever applies. And `git ls-files`
forks a process per call.

`load_jev_settings` already has an mtime-keyed cache (`:179-243`) — the pattern to
copy.

### Edits

**New `scan_cache.py`** — a shared, TTL + mtime keyed cache for both scanners.

```python
"""Mtime-keyed caches for workspace scanning.

`load_jev_settings` already re-reads config files only when an mtime changes
(jev_engine.py:179-243). The file scanners had no equivalent, so every tool call
re-walked the skill tree and forked a `git ls-files` subprocess. This module
applies the same signature-keyed pattern.
"""
```

```python
DEFAULT_TTL_S = 5.0

@dataclass
class _Entry:
    value: Any
    signature: tuple
    at: float


class ScanCache:
    """Signature- and TTL-keyed cache. Thread-safe; process-local."""

    def __init__(self, ttl_s: float = DEFAULT_TTL_S) -> None:
        self._ttl = ttl_s
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}

    def get(self, key, signature_fn) -> Any | None:
        """Return the cached value, or None when absent, stale, or TTL-expired.

        `signature_fn` returns a cheap-to-compare description of the current
        on-disk state (mtimes, sizes, a git HEAD). Cheap beats exact: a
        directory mtime changes when entries are added or removed, which covers
        the overwhelming majority of edits.
        """
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if now - entry.at > self._ttl:
                self._entries.pop(key, None)
                return None
        try:
            signature = signature_fn()
        except OSError:
            return None
        if signature != entry.signature:
            with self._lock:
                self._entries.pop(key, None)
            return None
        with self._lock:
            self._entries[key].at = now      # sliding TTL
        return entry.value

    def put(self, key, signature, value) -> None:
        with self._lock:
            self._entries[key] = _Entry(value, signature, time.monotonic())

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
```

**Why both a TTL and a signature.** The TTL bounds worst-case staleness when a
signature misses a change (mtime granularity, clock skew); the signature makes the
common case — nothing changed — free after one `stat`.

**Signatures:**

- `find_agent_resources`: for each scan dir, `(dir_mtime_ns, tuple(sorted(child
  names)))`. A file *edit inside* an existing directory does not change the directory
  mtime, so **content changes are not detected** — correct here, because the scanner
  returns *paths*, and the file content is re-read separately in Phase 3
  (`_read_candidate_preview`). Note that in a comment; it is the reason this cache is
  safe.
- `select_target_files`: git output, keyed on `(HEAD sha, .git/index mtime_ns,
  .gitignore mtime_ns)`. Falls back to `(dir mtimes, TTL)` when not a repo.

**Wire in `jev_engine.py`:**

```python
_scan_cache = ScanCache()

def _reset_scan_cache() -> None:
    """Clear the scanner cache. Exposed for tests."""
    _scan_cache.clear()
```

- `find_agent_resources` (`:532-542`): wrap discovery in
  `_scan_cache.get(("md", str(root), tuple(settings["scan_paths"])), sig_fn)`.
- `select_target_files` (`:729`): wrap `_discover_files_git` similarly.
- The `os.walk` fallback (`:732-752`) gets the same treatment.
- **Also bound the walk**: cap `rglob` results. Phase 3 caps previews at
  `MAX_PREVIEW_READS`; do the same for discovery — a repo with 50 000 `.md` files
  under `.agents` should not be fully traversed. Add `MAX_DISCOVERED_FILES = 5_000`
  in `limits.py`, stop at the cap, and report `candidates_truncated`.

**Deduplicate nested default scan paths.** `DEFAULT_SCAN_PATHS`
(`jev_engine.py:40-47`) contains `.agents/skills`, `.agents/workflows`,
`.agents/memory` **and** `.agents` — so `.agents/skills/x/SKILL.md` is discovered
three times per call. Filter any scan dir that is a descendant of another configured
scan dir (keep the most specific for *file* discovery, since `rglob` covers the
rest):

```python
def _minimal_scan_dirs(dirs: list[Path]) -> list[Path]:
    """Drop scan dirs already covered by an ancestor in the list.

    DEFAULT_SCAN_PATHS lists `.agents/skills` and `.agents`; scanning both walks
    the same files twice.
    """
    resolved = [d.resolve() for d in dirs if d.exists()]
    out = []
    for d in resolved:
        if not any(d != o and _is_within(d, o) for o in resolved):
            out.append(d)
    return out
```

Reuse `_is_within` from Phase 4. **Careful:** this changes which paths are relative to
`root` if a user configures both `.agents` and `docs` — `.agents` is not a descendant
of `docs`, so only genuinely-nested entries collapse. Verify with a test that adds
`docs/skills` plus `docs` and asserts `docs/skills/x.md` is still found.

Add `_reset_scan_cache()` to the autouse fixture in `tests/conftest.py` (it already
resets the other three).

**Tests** (`tests/test_scan_cache.py`):

- Two identical calls → the second hits the cache (spy on the walker, assert 1 call).
- Touching a scan dir (add a file) → the cache misses and re-walks.
- TTL expiry via a monkeypatched `monotonic` → miss.
- `_minimal_scan_dirs` collapses `.agents` + `.agents/skills` to one entry; leaves
  `docs` + `docs/skills` correct; leaves siblings alone.
- A `MAX_DISCOVERED_FILES` overflow sets `candidates_truncated` and keeps
  `action != "auto"`.
- Concurrent access from 10 threads returns one consistent result (the cache is
  shared process state).

---

## Finding #25 — Mock is O(n·m)

```python
# mock.py:36-42
def _overlap(a: str, b: str) -> float:
    left = set(_tokenize(a))          # re-tokenized EVERY call
    right = [t for t in _tokenize(b) if len(t) > 1]
    ...
```

```python
# mock.py:87-100
def _mock_choice(state_text, question):
    ...
    scores = [_score_option(state_text, label, ...) for label in labels]   # N calls
```

`_score_option` (`:68-83`) calls `_overlap` up to 3 times, and **every one of those
re-tokenizes the full state text**. With 250 options × 3 questions that is up to
2 250 full tokenizations of the state per call. `mock_system_one` is what the test
suite and every demo use, so this is the measured path.

### Edits

Precompute the state token set once per call.

```python
def _token_set(text: str) -> frozenset[str]:
    return frozenset(_tokenize(text))


def _overlap_set(left: frozenset[str], right_text: str) -> float:
    """Fraction of `right_text`'s meaningful tokens present in `left`."""
    right = [t for t in _tokenize(right_text) if len(t) > 1]
    if not right:
        return 0.0
    hits = sum(1 for token in right if token in left)
    return hits / len(right)
```

Keep `_overlap(a, b)` as a thin wrapper (`_overlap_set(_token_set(a), b)`) so
`mock.py:58-64`'s `_mock_noul` and any test importing it keep working.

Then thread the set through:

```python
def _score_option(state_text, state_tokens, label, description, instructions) -> float:
    score = _overlap_set(state_tokens, f"{label} {description}") * 3 + _overlap_set(state_tokens, instructions)
    ...
    if label in ("primary", "target_file"):
        score += _overlap_set(state_tokens, label) * 1.5
    ...

def _mock_choice(state_text, question, state_tokens=None):
    state_tokens = state_tokens or _token_set(state_text)
    ...
```

and in `mock_system_one` (`:125-152`) compute it **once** and pass it down to
`_mock_noul`, `_mock_choice` and `_mock_score`.

**Add `is_relevant` support.** Phase 3 adds a second `Noul` to
`search_target_files`; the mock's `_mock_noul` (`:56-65`) keys off regexes tuned for
shell commands. Add a branch so a presence question in mock mode is plausible:

```python
def _mock_noul(state_text, instructions) -> float:
    hay = state_text.lower()
    if "forced winner" in instructions.lower() or "actually" in instructions.lower():
        # presence question: score how well the best candidate overlaps the task
        return _clamp01(0.4 + 0.5 * _overlap_set(_token_set(state_text), instructions))
    if re.search(r"delete|drop table|...", hay):
        return 0.97
    ...
```

Keep the existing command regexes **first** so `test_mock_tools.py`'s
`test_destructive_command_never_safe` (`:50-53`) keeps passing — the guardrail
question must never be diluted.

**Tests** (`tests/test_mock_perf.py`):

- `mock_system_one` with 250 options produces **byte-identical** output before and
  after the refactor (pin the answers in a fixture). This is the critical test — the
  optimization must not change any judgment.
- `_token_set` is computed once: monkeypatch it with a counting spy and assert
  1 call for a 250-option, 3-question `mock_system_one` (today: hundreds).
- The benchmark case `mock_system_one_250` improves by the Phase 5 target.
- `_mock_noul` with a presence-style instruction returns a value in (0, 1) and is
  **not** 0.97 for a benign command.

---

## Finding #26 — Redundant serialization

Two places re-serialize large payloads for information already computed.

**`mock.py:145-151`** — the mock's `usage` re-walks everything:

```python
usage=Usage(
    input_tokens=estimate_tokens({"state": state, "questions": questions}),
    output_tokens=len(questions) * 8,
),
```

`fit_state` already computed `coverage.estimated_tokens.state` and `.questions` and
handed them to `_request` as `fitted["coverage"]`. The mock recomputes both from
scratch, then `estimate_tokens` walks the serialized string char by char (see #23).

**`jev_logging.py:107-111`** — every successful call dumps the whole result:

```python
dumped = json.dumps(result, default=str)
payload = {"result_len": len(dumped), "result_preview": dumped[:1000]}
```

For `find_agent_resources` that is a 6 000-char file content plus a duplicate
(`file`/`content` and `primary`/`resources` — see #27) serialized per call, and the
result is serialized **again** by the MCP runtime.

### Edits

**`mock.py`** — accept the precomputed token counts:

```python
def mock_system_one(state, questions, model="jev-latest", input_tokens=None):
    """Deterministic `system_one` judge for mock mode.

    `input_tokens` may be supplied by the caller from fit_state's coverage
    (limits.py:129-134), which already counted the state and the questions.
    Recomputing them here re-serializes and re-scans the whole payload.
    """
    ...
    if input_tokens is None:
        input_tokens = (
            estimate_tokens(state) + estimate_tokens(questions)
        )
    return SystemOneResponse(
        model=f"{model}+mock",
        answers=answers,
        usage=Usage(input_tokens=input_tokens, output_tokens=len(questions) * 8),
    )
```

**`jev_engine.py`** — pass it from `_request`:

```python
def _request(state_text, questions):
    cfg = get_config()
    started = time.perf_counter()
    try:
        fitted = fit_state(state_text, questions)
        counts = fitted["coverage"]["estimated_tokens"]
        res = execute_system_one(
            get_client(), state=fitted["state"], questions=questions,
            timeout_s=_remaining_seconds(started, cfg),
            input_tokens=counts["state"] + counts["questions"],
        )
```

`execute_system_one` forwards `input_tokens` to `mock_system_one` only; the live
client ignores it (the provider reports its own usage). Keep the live path reading
`res.usage` exactly as today — never fabricate provider usage.

Note the mock's token count now differs slightly from before (it counted the
combined dict, which includes JSON punctuation). Tests asserting an exact
`input_tokens` in mock mode must be relaxed to a range. `grep` for
`input_tokens ==` in `tests/`.

**`jev_logging.py`** — drop the eager dump, log only the shape:

```python
def log_tool_call(tool, ms, args=None, result=None, error=None) -> None:
    """Log an MCP tool invocation.

    The result is NOT serialized here: the MCP runtime serializes it again
    (mcpserver/utilities/func_metadata.py:226) and a skill result carries up to
    6,000 characters. Log the key set and a coarse size instead.
    """
    if error is not None:
        payload = {"error": error}
    elif isinstance(result, dict):
        payload = {"result_keys": sorted(result.keys())}
        if "content" in result and isinstance(result["content"], str):
            payload["content_chars"] = len(result["content"])
        if isinstance(result.get("resources"), list):
            payload["resource_count"] = len(result["resources"])
        if isinstance(result.get("ranked"), list):
            payload["ranked_count"] = len(result["ranked"])
    else:
        payload = {"result_type": type(result).__name__}
    args_safe = {k: _redact(v) for k, v in (args or {}).items()}
    log_event("tool_call", tool=tool, args=args_safe, ms=int(ms), **payload)
```

Losing `result_preview` costs debuggability. Compensate: Phase 3's `ranked` list plus
the `candidates_*` counters are the diagnostic signal, and a `JEV_MCP_LOG_PREVIEW=1`
opt-in can restore the old behaviour for debugging:

```python
    if _preview_enabled() and isinstance(result, dict):
        payload["result_preview"] = json.dumps(result, default=str)[:1000]
```

Documented in `config/README.md`.

**Tests:** `log_tool_call` emits `result_keys`; with
`JEV_MCP_LOG_PREVIEW=1` it also emits `result_preview`; the mock's `input_tokens`
equals `coverage.estimated_tokens.state + .questions` when supplied.

---

## Finding #27 — Envelope duplication

```python
# jev_engine.py:653-667
result = {
    "primary": resources[0] if resources else None,     # full {name,file,content}
    "file": resources[0]["file"] if resources else None,   # duplicate
    "content": resources[0]["content"] if resources else None,  # 6,000-char duplicate
    "resources": resources,                              # includes resources[0] again
    ...
}
```

The 6 000-char content is serialized **three times** in one response. The plugin reads
`result["target"]`/`resources[0]`, and `diag_mcp.py` prints key names — nothing needs
the flat duplicates.

### Edits

Remove `file` and `content`; keep `primary` and `resources`.

```python
    result = {
        "matched": len(resources) > 0,
        "count": len(resources),
        "primary": resources[0] if resources else None,
        "resources": resources,
        ...
    }
```

**This is a breaking change.** `README.md:180-188` documents `primary` only, but the
`file`/`content` keys have existed since the flat shape was the original API. Before
removing:

```powershell
Select-String -Path "C:\Users\aalji\.config\opencode\plugins\jev-plugin.js","D:\mcp\jev-typesafe-mcp\config\jev-plugin.example.js" -Pattern '\.file|\.content|\["file"\]|\["content"\]'
```

The Phase 6 rewrite removes the plugin's dependence entirely. If any consumer needs
the flat keys, the correct move is to add a `files: [str]` array (small, no content)
rather than keep a duplicated blob.

Update `README.md` (§2 `search_agent_skills` example and the envelope key list) and
note it in the phase commit message as a deliberate removal.

**Tests:** assert `file` and `content` are **absent**; assert
`result["primary"]["file"] == result["resources"][0]["file"]`; assert the serialized
envelope is under 60% of the Phase 1 `envelope_size_skills` benchmark.

---

## Findings #28 / #29 — Logging

**#28 — `_redact` is both over- and under-aggressive.**

```python
# jev_logging.py:45-54
def _redact(value) -> str:
    text = str(value)
    lowered = text.lower()
    for marker in _SECRET_MARKERS:
        if marker in lowered:
            return "<redacted>"          # nukes the WHOLE field
    if len(text) > 40 and text.replace("-", "").replace("_", "").isalnum():
        return "<redacted>"              # kills ordinary long strings
    return text
```

Three problems:

1. **Over-broad.** A shell command containing the substring `bearer ` — or any text
   with `api_key=` — discards the *entire* value, so the log loses the one thing it
   exists to record. `test_cleanup_phase8.py:45-63` even asserts that ordinary
   long strings survive, but only because they happen to contain spaces or
   punctuation; a 41-char single token of real diagnostic value is redacted.
2. **Under-broad.** Only 8 markers plus one heuristic. A `TypeSafeClient` error
   message, an `Authorization` header variant, or a key with an unlisted prefix
   passes straight through.
3. **Not applied to results.** `log_tool_call` redacts `args` (`:112`) but writes
   `result_preview` verbatim (`:110`) — so a secret that reaches a result body is
   logged in the clear. The module docstring's promise ("secrets redacted") does not
   hold for the result path.

**#29 — double traceback.** `_request` logs one
(`jev_engine.py:446-459` → `log_round(error=...)` → `log_exception`), then `_run`
logs another (`jev_mcp.py:63-64`). Two full tracebacks per failure, and the server
log triples in size during an incident.

### Edits

**`jev_logging.py`** — redact the *value*, keep the field:

```python
_SECRET_RE = re.compile(
    r"(sk-|ts_|apikey_|apikey-|key-)"
    r"[A-Za-z0-9_\-]{8,}"                       # key-shaped token
    r"|(?i:api[_-]?key|authorization|bearer)"
    r"\s*[:=]?\s*['\"]?[A-Za-z0-9._\-]{6,}",
)

def _redact(value) -> str:
    """Redact credential-shaped substrings, preserving surrounding context.

    The previous implementation returned "<redacted>" for the whole field when
    any marker appeared anywhere, which destroyed the diagnostics a command or
    task line exists to provide, while missing credential shapes outside the
    hardcoded marker list.
    """
    text = str(value)
    return _SECRET_RE.sub("<redacted>", text)
```

Keep the module's own redaction on `log_exception`'s `error_message` (`:96`).

**Remove the >40-char heuristic.** It is the over-broad case and no test should
depend on it — but `test_cleanup_phase8.py:32-43` asserts it does. Update those tests
to assert the *new* behaviour: a 41-char non-secret survives, a
`sk-`-prefixed 41-char token is redacted.

**Apply redaction to the result path.** With #26's change, `result_preview` is
opt-in; when it is on, redact it:

```python
        payload["result_preview"] = _redact(json.dumps(result, default=str)[:1000])
```

**`log_round` / `_request`** — log the error once, with a reference to the other:

**`jev_engine.py:452-459`** — keep the round timing (it is the useful per-round
signal) but stop attaching the traceback:

```python
    except Exception as err:
        log_round(
            (time.perf_counter() - started) * 1000,
            question_keys=questions.keys(),
            state_len=len(state_text),
            error=err,          # -> type + message only
            traceback=False,
        )
        raise
```

**`jev_logging.py:116-126`**:

```python
def log_round(ms, question_keys=(), state_len=0, error=None, traceback=False) -> None:
    """Log one provider round, or the failure it raised.

    The full traceback is logged once by the caller that owns the failure
    (jev_mcp._run); pass traceback=False here to avoid logging it twice.
    """
    fields = {
        "ms": int(ms),
        "questions": list(question_keys),
        "state_len": int(state_len),
    }
    if error is not None:
        if traceback:
            log_exception("round_error", error, **fields)
        else:
            log_event("round_error", error_type=type(error).__name__,
                      error_message=_redact(str(error)), **fields)
    else:
        log_event("round_ok", **fields)
```

**Tests** (`tests/test_logging.py`):

- `_redact` redacts the value, keeps the prefix: `"curl -H 'Authorization: Bearer
  sk-abc123def456' https://x"` → the token is gone, `curl` and the URL remain.
- A 41-char non-secret is **not** redacted.
- `sk-` + 40 chars is redacted; a bare 40-char git SHA is not.
- `log_tool_call` with a result containing `"api_key=SUPERSECRET"` writes no
  occurrence of `SUPERSECRET` to the log file (read the temp log back).
- One failure through `jev_mcp._run` produces exactly **one** `traceback` field
  across all log records.

---

## Files touched

| File | Change |
|---|---|
| `limits.py` | regex `estimate_tokens`, binary-search truncation, drop `_char_budget_for_tokens`, `MAX_DISCOVERED_FILES` |
| `scan_cache.py` | **new** — TTL + signature cache |
| `jev_engine.py` | `_minimal_scan_dirs`, wire the cache, `MAX_DISCOVERED_FILES` cap, pass `input_tokens` to the mock, drop `file`/`content`, `log_round(traceback=False)` |
| `mock.py` | `_token_set` threading, `input_tokens` param, presence-Noul branch |
| `jev_logging.py` | regex redaction, `result_keys` logging, `JEV_MCP_LOG_PREVIEW`, single traceback |
| `config.py` | `JEV_MCP_LOG_PREVIEW` |
| `tests/conftest.py` | reset the scan cache |
| `tests/test_limits.py` | exactness pin, property checks |
| `tests/test_scan_cache.py` | **new** |
| `tests/test_mock_perf.py` | **new** — output pin + call-count spy |
| `tests/test_logging.py` | **new** |
| `tests/test_cleanup_phase8.py` | rewrite the redaction assertions |
| `scripts/bench_jev.py` | tighten `--assert` thresholds to the measured values |
| `docs/perf-baseline.md` | before/after table |
| `README.md`, `config/README.md` | removed keys, new env var, timeout wording |

## Verification

```powershell
cd D:\mcp\jev-typesafe-mcp
& .\.venv\Scripts\python.exe -m py_compile limits.py scan_cache.py jev_engine.py mock.py jev_logging.py config.py
& .\.venv\Scripts\python.exe -m pytest tests -q
& .\.venv\Scripts\python.exe scripts\bench_jev.py
& .\.venv\Scripts\python.exe scripts\bench_jev.py --assert
$env:JEV_MCP_MOCK="1"; & .\.venv\Scripts\python.exe scripts\eval_routing.py --mode mock
Remove-Item Env:\JEV_MCP_MOCK
& .\.venv\Scripts\python.exe scripts\diag_mcp.py --tool search_agent_skills --task "fix ui bug" --root_dir . --mock
node tests/test_plugin.mjs
```

Compare the benchmark table against `docs/perf-baseline.md` and confirm every Phase 1
target is met. Re-run twice on a warm cache — the `_warm` rows are the ones that show
the cache working.

## Definition of done

- [ ] `estimate_tokens` is bit-identical to the old loop on a pinned corpus
- [ ] `_char_budget_for_tokens` removed; truncation is a binary search
- [ ] Second identical `find_agent_resources` call does not re-walk the tree
- [ ] Second identical `select_target_files` call does not fork `git ls-files`
- [ ] `.agents` + `.agents/skills` collapse to one walk
- [ ] `MAX_DISCOVERED_FILES` overflow is reported and blocks `action: "auto"`
- [ ] `mock_system_one` computes the state token set **once** and returns identical answers
- [ ] Mock `input_tokens` reuses `fit_state`'s counts
- [ ] `log_tool_call` does not serialize the full result by default
- [ ] `file`/`content` removed; envelope under 60% of Phase 1 size
- [ ] One traceback per failure; secrets never reach the log
- [ ] Every Phase 1 benchmark target met; `--assert` tightened to the new numbers
- [ ] `pytest`, `node tests/test_plugin.mjs` green

## Commit

```
perf(engine): remove repeated tokenization, I/O and serialization

Three costs were paid on every single call.

estimate_tokens walked the payload one Python-level character at a time, and
fit_state calls it 3-4 times over the state plus every question value. Count
non-ASCII with a compiled regex instead -- identical output, pinned by a test
against the original loop -- and replace the linear truncation scan with the
reference's binary search, which also inherits its surrogate-pair guard.

Both file scanners ran unconditionally: a full rglob of every configured skill
dir, and a git ls-files subprocess, per tool call. Add scan_cache.py, a
signature+TTL cache modelled on the one load_jev_settings already uses, and
collapse the redundant nested default scan paths (.agents and .agents/skills
walk the same files). Bound discovery at MAX_DISCOVERED_FILES and report the
overflow instead of dropping candidates silently.

mock's _overlap re-tokenized the full state for every option of every question
-- 2,250 tokenizations for 250 options across 3 questions. Precompute the
state token set once. Output is pinned by a fixture so the judgments are
unchanged.

The mock also recomputed usage by re-serializing state and questions, and
log_tool_call serialized the whole result on every call before the MCP runtime
serialized it again. Reuse fit_state's token counts, and log the key set plus
coarse sizes instead (opt the old preview back in with JEV_MCP_LOG_PREVIEW=1).

Redaction becomes a regex that removes the credential and keeps the surrounding
context; the old marker scan discarded whole fields, and the >40-char heuristic
redacted ordinary long strings. It is now also applied to the result path. One
traceback per failure instead of two.
```

## Post-commit check

Send a real message in opencode and confirm from the two logs that:

- `<repo>\logs\jev_engine.log` records `result_keys` (not a 6 KB preview) per call,
- `~/.config/opencode/logs/jev-plugin.log` shows the skill still selected, and
- the second identical call is measurably faster (`bench_jev.py`'s `_warm` row).
