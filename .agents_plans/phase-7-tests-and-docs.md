# Phase 7 — Close the Test Gaps, Clean Up, Sync the Docs

**Goal:** remove the false confidence, kill the dead code, and make the docs tell the
truth.

**Closes:** finding #30 (remainder), #21, and all documentation drift found during
the audit.

**Depends on:** Phases 1–6. Run last.

**Risk:** low. Mostly tests and prose — but it is where previously-hidden regressions
surface, so budget time for whatever it finds.

---

## Finding #30 — Tests give false confidence

### Vacuous tests with authoritative names

```python
# tests/test_hardening_integration.py:48-67
def test_family_prefix_sibling_matching(tmp_path, monkeypatch):
    ...builds tool-runner, tool-builder, my-tool-extra skills...
    res = find_agent_resources("run tools", root_dir=str(tmp_path))
    assert res is not None          # <- asserts nothing about clustering
```

```python
# tests/test_hardening_integration.py:122-136
def test_target_files_bounded_depth(tmp_path, monkeypatch):
    ...builds a 7-deep tree...
    res = select_target_files("find file", root_dir=str(tmp_path))
    assert res is not None          # <- asserts nothing about depth
```

The first is named for behaviour it never checks. The fixture is even correct —
`tool-runner` and `tool-builder` share no `-` prefix, so it wouldn't cluster anyway.
Both must be rewritten or deleted; a test that cannot fail is worse than no test
because it inflates the pass count the README advertises.

Also in that file:

- `test_settings_mtime_caching` (`:167-178`) — writes settings, loads twice, asserts
  `s1 is s2`. It does not test the mtime mechanism (no re-write, no mtime change).
- `test_concurrent_get_client_thread_safety` (`:70-100`) — never closes the client it
  creates, leaking a pool per run.
- `test_connection_error_never_skipped` (`:27-35`) subclasses
  `TypeSafeAPIConnectionError` and calls `super(Exception, self).__init__(...)`,
  bypassing the SDK's own `__init__`. It works by accident; make it construct
  properly.

### Plugin tests that cannot fail

```js
// tests/test_plugin.mjs:129-136
assert.ok(pluginSource.includes("RetryPolicy("), ...);
assert.ok(pluginSource.includes("timeout=3.0"), ...);
assert.ok(pluginSource.includes("maxDepth = 6"), ...);
```

These assert the *example* file contains strings. The installed file contains
**none** of them and no test noticed. Phase 1 adds the drift guard; Phase 6 replaces
these with behavioural tests.

### A currently-failing test

`.pytest_cache/v/cache/lastfailed` holds:

```json
{ "tests/test_cleanup_phase8.py::TestEnsureDotenv::test_ensure_dotenv_creates_or_loads": true }
```

That node id no longer exists — the test was renamed to
`test_ensure_dotenv_loads_with_override_true` (`test_cleanup_phase8.py:74`). A stale
failure entry means nobody ran the suite clean, or ignored the result. Phase 1 clears
the cache; Phase 2 rewrites those four tests anyway (the `override is True`
assertions become `override is False`).

---

## Task 7.1 — Make the vacuous tests real

### `test_family_prefix_sibling_matching` → two real tests

```python
def test_family_cluster_adds_siblings_for_confident_primary(tmp_path, monkeypatch):
    """A high-probability primary pulls in same-family siblings, newest first."""
    monkeypatch.setenv("JEV_MCP_MOCK", "1")
    skills = tmp_path / ".agents" / "skills"
    for name in ("godot-ui-theme", "godot-ui-layout", "godot-audio"):
        (skills / name).mkdir(parents=True)
        (skills / name / "SKILL.md").write_text(f"# {name}")

    probs = {"godot-ui-theme/SKILL.md": 0.80, "godot-ui-layout/SKILL.md": 0.10,
             "godot-audio/SKILL.md": 0.10}
    _stub_choice(monkeypatch, "primary", "godot-ui-theme/SKILL.md", probs)

    res = jev_engine.find_agent_resources("theme", str(tmp_path))
    files = [r["file"] for r in res["resources"]]
    assert ".agents/skills/godot-ui-theme/SKILL.md" in files
    assert ".agents/skills/godot-ui-layout/SKILL.md" in files   # same family
    assert ".agents/skills/godot-audio/SKILL.md" not in files    # different family
```

and the negative case Phase 3 introduces:

```python
def test_family_cluster_suppressed_for_weak_primary(tmp_path, monkeypatch):
    """A low-probability primary must not claim the sibling slots."""
    ... probs = {"godot-ui-theme/SKILL.md": 0.40, "godot-ui-layout/SKILL.md": 0.30, ...}
    assert files == [the primary only]
```

Both need a `_stub_choice` helper that monkeypatches `execute_system_one` — the same
pattern `test_mock_tools.py:183-200` and `:307-327` already use. **Extract it to
`tests/conftest.py`** as a fixture so all four test modules share one implementation
instead of three copies.

### `test_target_files_bounded_depth` → real depth + cap assertions

```python
def test_target_files_respects_depth_and_reports_overflow(tmp_path, monkeypatch):
    """Files deeper than the walk depth are not offered, and overflow is reported."""
    monkeypatch.setenv("JEV_MCP_MOCK", "1")
    deep = tmp_path
    for i in range(9):
        deep = deep / f"d{i}"
        deep.mkdir()
        (deep / "f.py").write_text("x = 1")
    shallow = tmp_path / "top.py"
    shallow.write_text("x = 1")

    res = jev_engine.select_target_files("find top", str(tmp_path))
    assert "top.py" in res["ranked"] or res["files"] == ["top.py"]
    assert not any("d0" in f for f in res["ranked"]), "depth bound not enforced"
```

Plus a `MAX_DISCOVERED_FILES` overflow test asserting `candidates_truncated is True`
and `res["action"] != "auto"`.

### `test_settings_mtime_caching` → actually test the mtime

```python
def test_settings_cache_invalidates_on_mtime_change(tmp_path, monkeypatch):
    cfg = tmp_path / "jevs_settings.json"
    cfg.write_text(json.dumps({"enable_model_routing": False}))
    monkeypatch.chdir(tmp_path)
    jev_engine._reset_settings_cache()

    assert jev_engine.load_jev_settings()["enable_model_routing"] is False

    cfg.write_text(json.dumps({"enable_model_routing": True}))
    # Filesystem mtime granularity can be coarse; force the observable change.
    os.utime(cfg, (time.time() + 2, time.time() + 2))

    assert jev_engine.load_jev_settings()["enable_model_routing"] is True
```

The `os.utime` bump is required, or a same-second write may not change `st_mtime`
and the test is flaky.

### Client-cache test

Close the client in a `finally`, and assert `close()` was called (Phase 4 adds that
assertion in `tests/test_client_cache.py`; this one just stops leaking).

---

## Task 7.2 — The missing test matrix

Phases 2–6 add tests as they go. This is the consolidated list — check each is
present before declaring done.

| Area | Test | File |
|---|---|---|
| Validation | `NaN` / `inf` in noul, confidence, each probability, probability total, score, expected mean | `test_validation_nan.py` |
| | `bool` still rejected as a number | same |
| | 7-level score with 0.05 mean drift accepted; 2-level with 0.019 rejected | same |
| Envelope | `set(result.keys())` identical across every branch of all 3 tools | `test_envelope_shapes.py` |
| | no `NaN` in the serialized envelope | same |
| Transport | `isError: true` over the real stdio transport for a bad `root_dir` | `test_mcp_transport.py` |
| | all 4 tools appear in `tools/list` | same |
| | `initialize` + `tools/call` round trip | same |
| Deadline | total time ≤ `JEV_MCP_TIMEOUT_MS` when the client always times out | `test_deadline.py` |
| | `RetryPolicy` gets `timeout` and `api_timeout_error=False` | same |
| | mock mode honours a 1 ms budget | same |
| Bounds | `root_dir` allowlist: allowed, rejected, sibling-prefix, symlink escape | `test_root_dir_allowlist.py` |
| | `JEV_MCP_ALLOWED_ROOTS` entry honoured; nonexistent entry ignored | same |
| | drive roots and system dirs still rejected with the original message | same |
| Settings | user+project merge per key; `""` clears; `sources` list; malformed file skipped | `test_settings_merge.py` |
| | post-read re-stat prevents the stale-cache race | same |
| Breaker | 3 failures open it; 4th call never reaches the client; cooldown probes; success closes | `test_breaker.py` |
| | a 401 opens the longer auth cooldown | same |
| Client cache | 200 cycles → 200 `close()` calls; base URL change → new instance; concurrency intact | `test_client_cache.py` |
| Limits | `estimate_tokens` bit-identical to the original loop on a pinned corpus | `test_limits.py` |
| | truncation never exceeds budget; surrogate pairs never split | same |
| Cache | second call does not re-walk; dir change invalidates; TTL expires | `test_scan_cache.py` |
| | `_minimal_scan_dirs` collapses nested, preserves siblings | same |
| | concurrent access consistent | same |
| Mock | 250-option output pinned to a fixture | `test_mock_perf.py` |
| | `_token_set` called exactly once | same |
| | presence-Noul branch is plausible and never 0.97 for benign input | same |
| Logging | `_redact` removes the secret, keeps context; no over-redaction | `test_logging.py` |
| | no secret reaches the log via the result path | same |
| | one traceback per failure | same |
| Candidates | front matter split; `description:` preferred; truncation; unreadable → placeholder | `test_candidates.py` |
| | `bound_candidates` returns `(kept, truncated)` | same |
| Discovery | evidence present in criteria; presence Noul drives `exists`; truncation blocks `auto`; read-once | `test_mock_tools.py` |
| Plugin | client framing, `isError` unwrap, reconnect, timeout | `test_plugin.mjs` |
| | `applyTier` / `injectSkill` gating tables | same |
| | `isInside` traversal table; scan depth bound; log rotation | same |
| | drift guard | same (Phase 1) |
| Routing | 20 labelled cases; top-1, top-3, false-positive rate | `scripts/eval_routing.py` |

---

## Task 7.3 — Delete the dead code

### Finding #21

**`Score` is imported and never used.** No tool builds a `Score` question, so the
entire Score validation path (`jev_validation.py:88-124`) has never run against a real
response — yet it is the most intricate validator in the file, with int-key coercion
and a mean-consistency check.

Two options:

- **(a) Delete** `_validate_score`, `_mock_score`, and the Score branch in
  `validate_response`. Smallest, and honest about what the server does.
- **(b) Keep and cover** — the SDK supports `Score`, and a graded judgment is a
  natural future addition (the reference uses Score for graded ranking). Add the
  fixture test and leave the code.

**Recommendation: (b).** The code is correct, tested by `test_validation.py:55-82`,
and deleting it removes a capability the design documents. But the *unused imports*
must go:

```python
# jev_validation.py:9
from typesafe_sdk import Answer, Choice, Noul, Score   # none are used
```

`Answer`, `Choice`, `Noul` are all unused (only `_attr` and the primitives' runtime
attributes are read). Reduce to:

```python
from jev_validation import ...   # nothing from typesafe_sdk
```

Same in `jev_engine.py:10`: `Score` is imported and never used.

Run a real linter rather than eyeballing it — if `ruff` is not in the venv, use:

```powershell
& .\.venv\Scripts\python.exe -c "import ast,sys;[print(f'{f}:{[n for n in (ast.walk(ast.parse(open(f,encoding='utf-8').read())) if isinstance(n,ast.Name) else None) if False]}' ) for f in []]"
```

or simply `python -m pyflakes` if available, else a 10-line AST script. Do not add a
linter dependency just for this.

### `ESCAPE_HATCHES` is dead

```python
# policy.py:28-32
ESCAPE_HATCHES = {
    "ask_user": "...",
    "investigate": "...",
    "none": "None of the supplied candidates fits the known requirements",
}
```

Never referenced outside its definition (`grep` to confirm). Only `"none"` is used,
and it is hardcoded in `search_target_files` (`:758`) and Phase 3's skills question.

Either wire the other two or delete them. The TypeSafe guidance supports the idea —
*"Include a no-match outcome when nothing may fit"* — and `investigate` is
meaningfully distinct from `none` (evidence may exist but was not supplied). But
adding two more Choice options changes the model's behaviour and needs evaluation data
before shipping.

**Decision: delete the dict, add a comment recording why.** The concepts live in
Phase 3's instructions text. Leaving unused policy constants around invites someone
to trust that they are enforced.

### The unreachable `opencode.json` lookup

`config/README.md:38-41`:

> OpenCode's `opencommand` schema is strict (`additionalProperties: false`): unknown
> top-level keys (e.g. a `jev_settings` block) invalidate the whole config...

So `_find_config_files` (`:132-148`) and the legacy branch in `load_jev_settings`
can never find a match. **Delete** both, plus the loop at `:228-239`, and note the
removal in the README. `tests/test_mock_tools.py:445-451` asserts
`_find_config_files` includes the script dir — **update that test** to assert the
function no longer exists, or just delete it.

Confirm no deployed `opencode.json` has a `jev_settings` block first:

```powershell
Get-Content "$env:USERPROFILE\.config\opencode\opencode.json" | Select-String "jev_settings"
Select-String -Path "D:\**\opencode.json" -Pattern "jev_settings" -ErrorAction SilentlyContinue
```

If any hit, keep the lookup and file that as a follow-up.

### `MAX_INPUT_CHARS` vs the docstring

`MAX_INPUT_CHARS = 100_000` (`jev_engine.py:50`) is a hard cap on a task string.
Phase 3 caps candidates but the task itself can still be 100 k chars ≈ 25 k tokens,
which alone can exceed `MAX_STATE_PLUS_LONGEST_QUESTION_TOKENS` when the criteria are
large. `fit_state` then truncates the state from the *right*, cutting the
`Goal:`/`candidates` tail rather than the task head. Consider truncating the task
head-first. Low priority — note it, fix it if a test shows it.

---

## Task 7.4 — Sync the documentation

The README has drifted from the code in at least six places. Verify each claim
against the code rather than trusting the current text.

| Location | Current claim | Reality |
|---|---|---|
| `README.md:135` | "Both `jev_mcp.py` and `jev_engine.py` load `.env` … with `override=True` … the `.env` file acts as a reliable fallback" | Inverted. Phase 2 fixes it to `override=False`, making the claim true. |
| `README.md:306` | "Spawns Python via `spawnSync`" | It has been async `spawn` + `await` for a while. Phase 6 makes it a reused MCP child. |
| `README.md:359` | `58 passed` | Actual count differs; Phase 1 records it. |
| `README.md:176` / `:196` | ignore-dir and exclusion lists | Verify against `select_target_files` after Phase 5's changes; the sets may have moved. |
| `README.md:130`, `config/README.md:55` | "Per-request timeout" | Phase 4 makes it a total budget. |
| `README.md:244-249`, `config/README.md:131-134` | "the first file that contains any Jev key wins" | Phase 4 makes it a user→project merge. |
| `README.md:212` / `config/README.md:143-151` | "all three tiers have model IDs" | The code checks `configuredModels.length > 0` (`:441`), not all three. |
| `README.md:357` | `py_compile` of two files | Extend to the full module list. |
| `README.md:105` | "`errorDetails()` (`jev_errors.py`)" | No such identifier; the function is `error_details`. |

Also:

- **`README.md` §2 `search_agent_skills` example** — remove `file`/`content` (Phase 5).
- **`README.md` §Tool envelope** — document the Phase 2 additions:
  `candidates_considered`, `candidates_evaluated`, `candidates_truncated`
  (Phase 3); `coverage.candidate_fields`; the `AUTH_ERROR` / `RATE_LIMITED` /
  `FORBIDDEN` codes; `exists: "no_candidates"`.
- **`README.md` §Diagnostics** — `result_preview` is now opt-in via
  `JEV_MCP_LOG_PREVIEW=1`; `tool_call` records `result_keys`.
- **`README.md` §Environment** — add `JEV_MCP_ALLOWED_ROOTS`,
  `JEV_MCP_BREAKER_THRESHOLD`, `JEV_MCP_BREAKER_COOLDOWN_S`,
  `JEV_MCP_AUTH_COOLDOWN_S`, `JEV_MCP_LOG_PREVIEW`, `JEV_PLUGIN_SCAN_ROOTS`.
- **`.env.example`** — mirror all of the above with the defaults.
- **`config/README.md`** — new plugin lifecycle, the drift guard as a required step,
  and the "why you should run `node tests/test_plugin.mjs` after copying" note.
- **`README.md` §Tests** — list the new test modules.
- **New `docs/perf-baseline.md`** — the Phase 1 + Phase 5 tables and the Phase 3
  accuracy table. Already created in Phase 1.

### A doctor script

`scripts/doctor.py` — one command that answers "is this installation healthy?":

```python
"""Check the jev-engine installation: interpreter, deps, config, settings, plugin drift.

Read-only. Exits 0 when healthy, 1 when something needs attention.
"""
```

Checks:

1. Interpreter path and version; `typesafe_sdk` importable, version reported.
2. `mcp` importable; `MCPServer` present (not the removed `mcp.server.fastmcp`).
3. `TYPESAFE_API_KEY` present — **print only its length and a 4-char suffix**, never
   the value. (Apply `_redact` from `jev_logging`.)
4. `load_jev_settings()` and `get_scan_paths()` resolved; `sources` listed.
5. `JEV_MCP_ALLOWED_ROOTS` parsed; cwd is inside the allowlist.
6. The log directory is writable.
7. `~/.config/opencode/plugins/jev-plugin.js` matches `config/jev-plugin.example.js`
   by SHA256 — the same check as the test, so a user can run it without Node.
8. Resolved `JEV_MCP_TIMEOUT_MS`, `AUTO_ACCEPT`, `REVIEW_AT` and whether they are
   consistent (`review_at <= auto_accept`).

Print a checklist with `[ok]` / `[!!]` and a one-line fix for each failure. This
replaces a lot of ad-hoc troubleshooting in `config/README.md:76-94`.

---

## Task 7.5 — Final verification

The whole thing, end to end, from a clean shell.

```powershell
cd D:\mcp\jev-typesafe-mcp

# 1. Compile and import
& .\.venv\Scripts\python.exe -m py_compile jev_engine.py jev_mcp.py jev_validation.py jev_errors.py policy.py limits.py config.py mock.py jev_logging.py candidates.py scan_cache.py scripts\diag_mcp.py scripts\bench_jev.py scripts\eval_routing.py scripts\doctor.py
& .\.venv\Scripts\python.exe -c "import jev_mcp; print('MCP import OK')"

# 2. Suites
& .\.venv\Scripts\python.exe -m pytest tests -q --durations=10
node tests\test_plugin.mjs

# 3. Doctor
& .\.venv\Scripts\python.exe scripts\doctor.py

# 4. Benchmarks and gates
& .\.venv\Scripts\python.exe scripts\bench_jev.py --assert
$env:JEV_MCP_MOCK="1"; & .\.venv\Scripts\python.exe scripts\eval_routing.py --mode mock
Remove-Item Env:\JEV_MCP_MOCK

# 5. All four tools, mock then live, over the real transport
foreach ($t in @("guardrail_command","search_agent_skills","search_target_files","select_model_tier")) {
  & .\.venv\Scripts\python.exe scripts\diag_mcp.py --tool $t --task "verify the engine" --command "git status" --root_dir . --mock
}
# ... then without --mock for the live path

# 6. Fail-closed spot checks: each must exit 1 with the right code
& .\.venv\Scripts\python.exe scripts\diag_mcp.py --tool search_target_files --task "x" --root_dir "C:\Users" --mock
$env:JEV_MCP_MOCK="1"
& .\.venv\Scripts\python.exe -c "import jev_validation as v; from typesafe_sdk import Noul; v._validate_noul(type('A',(),{'noul':float('nan')})(), 'x')"
Remove-Item Env:\JEV_MCP_MOCK

# 7. Plugin round trip in real opencode
#    restart opencode, send a message, check both logs
```

**No secret may appear in `git status`, `git diff`, or any committed file.**

```powershell
git status --short
git diff --stat
Select-String -Path (git diff --name-only) -Pattern "apikey_|sk-[A-Za-z0-9]{16,}" -ErrorAction SilentlyContinue
```

Update the `58 passed` figure in `README.md` to the real count.

---

## Files touched

| File | Change |
|---|---|
| `tests/test_hardening_integration.py` | rewrite the 4 vacuous tests |
| `tests/test_cleanup_phase8.py` | already rewritten in Phase 2; confirm |
| `tests/test_mock_tools.py` | remove the dead `_find_config_files` test; refresh counts |
| `tests/conftest.py` | extract the shared `_stub_choice` fixture; reset the scan cache |
| `tests/test_envelope_shapes.py` | **new** |
| `tests/test_mcp_transport.py` | **new** — real stdio transport |
| `tests/test_routing_quality.py` | **new** — wraps `scripts/eval_routing.py` in pytest |
| `scripts/doctor.py` | **new** |
| `jev_validation.py`, `jev_engine.py` | remove unused SDK imports |
| `policy.py` | delete `ESCAPE_HATCHES` with a comment |
| `jev_engine.py` | delete `_find_config_files` + the legacy branch (after confirming no live `jev_settings` block) |
| `README.md` | the 9 corrections above + new env vars + new keys |
| `config/README.md` | new plugin lifecycle, drift guard, doctor |
| `.env.example` | new env vars |
| `docs/perf-baseline.md` | final numbers |

## Verification

The Task 7.5 block, run in full from a clean shell, plus:

- [ ] No test asserts only `is not None`, `is None`, or `x is x`
- [ ] Every name in a test name is checked in the body
- [ ] `pytest --durations=10` shows no test above ~2 s
- [ ] `node tests/test_plugin.mjs` has no `pluginSource.includes(` assertions left
- [ ] `python scripts/doctor.py` exits 0 on this machine
- [ ] `git grep -n "spawnSync\|58 passed\|errorDetails()"` returns nothing
- [ ] `git status` shows no `.env`, `opencode.json`, or `.log`

## Definition of done

- [ ] All 4 vacuous tests rewritten or deleted
- [ ] The full test matrix from Task 7.2 is present
- [ ] Unused imports removed; `ESCAPE_HATCHES` and the dead config lookup gone
- [ ] Every README/config/README claim re-verified against the code
- [ ] `scripts/doctor.py` exists and is clean
- [ ] `pytest`, `node tests/test_plugin.mjs`, `bench_jev.py --assert` all green
- [ ] All four tools work over the real transport, mock and live
- [ ] Fail-closed spot checks produce the right codes
- [ ] No secret in the working tree
- [ ] `docs/perf-baseline.md` has the final before/after numbers

## Commit

```
test: make the suite able to fail, and sync the docs

Several tests asserted only `is not None` while named for behaviour they never
checked. test_family_prefix_sibling_matching built a three-skill fixture and
verified nothing about clustering; its fixture would not have clustered anyway.
test_target_files_bounded_depth built a 7-deep tree and verified no depth
bound. test_settings_mtime_caching never changed an mtime. Rewrite all four to
assert the behaviour in their names, and extract the repeated
execute_system_one stub from test_mock_tools into a shared conftest fixture.

The plugin tests asserted substrings against the example file, which is why a
427-line installed plugin missing every one of those guards went unnoticed.
Phase 6 replaces them with behavioural tests; keep the drift guard from Phase 1
as the regression net.

Add the consolidated coverage matrix: NaN, envelope-shape equality,
isError over the real transport, deadline bounds, the root_dir allowlist
including symlink and sibling-prefix escapes, settings merge, the circuit
breaker, client cache closure, scan caching, mock call counts, log redaction,
and the candidate-evidence path.

Delete the dead code: Score is imported in jev_validation and jev_engine and
never used by any tool; Answer/Choice/Noul are imported and never referenced;
ESCAPE_HATCHES is defined and never read; and the jev_settings-in-opencode.json
lookup cannot match because opencode's schema rejects unknown top-level keys
(config/README.md documents this). Keep the Score validator -- it is tested and
is a natural fit for a future graded judgement -- but stop importing it.

Nine README and config/README claims no longer match the code, including the
inverted .env override claim, a spawnSync reference, the test count, and an
errorDetails() that does not exist. Re-verify every claim against the source
and correct it.

Add scripts/doctor.py: a read-only health check for the interpreter, SDKs, key
presence, settings resolution, the root_dir allowlist, log writability, and
plugin drift, so installation problems are one command instead of a
troubleshooting section.
```
