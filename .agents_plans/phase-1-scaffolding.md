# Phase 1 — Safety Scaffolding

**Goal:** make every later phase measurable and provable *before* any behavior changes.

**Closes:** finding #30 (partial — the drift guard and the benchmark harness).

**Depends on:** nothing. Start here.

**Risk:** low. No production behavior changes. One test is expected to fail on first
run (the drift test) — that failure is the deliverable, not a regression.

---

## Why this phase exists

Phase 5 makes performance claims and Phase 3 makes accuracy claims. Neither is
checkable today:

- There is no benchmark, so "faster" is unfalsifiable.
- `tests/test_plugin.mjs` asserts substrings against `config/jev-plugin.example.js`
  only. The **installed** plugin
  (`C:\Users\aalji\.config\opencode\plugins\jev-plugin.js`) is a different file —
  427 lines vs 510, different SHA256 — and **none** of the guards the example has
  are present in it. Nothing detects this.
- `.pytest_cache/v/cache/lastfailed` carries
  `tests/test_cleanup_phase8.py::TestEnsureDotenv::test_ensure_dotenv_creates_or_loads`,
  a node id that no longer exists (the test was renamed to
  `test_ensure_dotenv_loads_with_override_true`). A stale failing node means the
  suite is not being run clean.

---

## Task 1.1 — Clean the pytest cache baseline

The stale node id makes `pytest --lf` target a test that does not exist.

**Edit:** `.pytest_cache/` is generated — do not commit it. Simply clear it as part of
the first real run.

```powershell
cd D:\mcp\jev-typesafe-mcp
Remove-Item -Recurse -Force .pytest_cache -ErrorAction SilentlyContinue
& .\.venv\Scripts\python.exe -m pytest tests -q
```

Record the actual passing count in this file under **Baseline** below. The README
claims `58 passed`; the real number is almost certainly different and the README
is stale (Phase 7 fixes the docs).

**Add to `.gitignore`** (it is missing):

```
.pytest_cache/
```

**Acceptance:** `pytest tests -q` reports 0 failures and a count that is written
down.

---

## Task 1.2 — Create the benchmark harness

**New file:** `scripts/bench_jev.py`

Purpose: a deterministic, offline, repeatable measurement of the four hot paths
this audit identified. It must run with **no API key** (`JEV_MCP_MOCK=1` semantics
are fine — `mock_system_one` is itself one of the things being measured).

Required structure:

```python
"""Deterministic offline benchmark for the jev-engine hot paths.

Usage:
    & .\.venv\Scripts\python.exe scripts\bench_jev.py            # table
    & .\.venv\Scripts\python.exe scripts\bench_jev.py --json     # machine-readable
    & .\.venv\Scripts\python.exe scripts\bench_jev.py --assert   # CI regression gate

Every case is offline (JEV_MCP_MOCK=1) and seeds a synthetic workspace under
tempfile, so the numbers are reproducible across machines and do not depend on
the real repo's file count.
"""
```

### Cases to implement

| Case | Measures | Why it matters |
|---|---|---|
| `estimate_tokens_100k` | `limits.estimate_tokens` on a 100 000-char string | Finding #23 — the per-char Python loop |
| `estimate_tokens_750_options` | `estimate_tokens` on a serialized 250-option × 3-question payload | The real `search_agent_skills` question shape |
| `fit_state_no_trunc` | `limits.fit_state` on a 1 000-char state | Baseline for the fit path |
| `fit_state_trunc` | `limits.fit_state` on a 200 000-char state | Exercises `truncate_to_token_budget` + `_char_budget_for_tokens` |
| `mock_choice_250` | `mock._mock_choice` with 250 options | Finding #25 — `_overlap` re-tokenizing state 250× |
| `mock_system_one_250` | `mock.mock_system_one`, 250 options, 3 questions | Finding #25 + #26 |
| `find_agent_resources_250` | full tool call, synthetic tree of 250 `.md` files | Finding #24 + #11 (question count) |
| `find_agent_resources_250_warm` | same, called twice | Proves whether a scan cache would help |
| `select_target_files_git` | full tool call, non-git synthetic tree | Finding #24 — the `os.walk` fallback |
| `envelope_size_skills` | `len(json.dumps(result))` for `find_agent_resources` | Finding #27 — duplicated `file`/`content` |

### Implementation requirements

- Use `time.perf_counter`; report **median of N** (N=5) plus min/max. The median
  resists scheduler noise; the spread shows whether the measurement is trustworthy.
- Set `JEV_MCP_MOCK=1` in the process env and call
  `config._reset_config_cache()`, `jev_engine._reset_client_cache()`,
  `jev_engine._reset_settings_cache()` once at startup — the same three the
  `conftest.py` autouse fixture resets (`tests/conftest.py:24-28`).
- Build the synthetic workspace in a `tempfile.TemporaryDirectory`, cleaned up in
  `finally`.
- `find_agent_resources`/`select_target_files` take a `root_dir`, so they are
  directly benchmarkable — pass the temp dir, never the real repo.
- **`--assert`** mode: compare against thresholds stored in the script and exit 1 on
  regression. Start with generous thresholds (3× the recorded Phase 1 baseline).
  Phase 5 tightens them to 1.25× the improved numbers. This is the mechanism that
  makes Phase 5's optimizations *provable* rather than asserted.

### Output

A plain table plus a JSON blob on `--json`:

```
case                        median_ms   min_ms   max_ms   bytes
estimate_tokens_100k             22.1     21.4     24.0        -
estimate_tokens_750_options      31.8     30.2     33.9        -
fit_state_no_trunc                0.4      0.4      0.5        -
fit_state_trunc                  41.7     40.9     44.1        -
mock_choice_250                  58.3     57.1     61.0        -
mock_system_one_250              63.9     62.0     66.4        -
find_agent_resources_250         118.4    116.2    121.7   41022
find_agent_resources_250_warm    115.9    114.0    119.8   41022
select_target_files_git           34.2     33.0     35.8        -
envelope_size_skills               -         -        -     41022
```

(The numbers above are **illustrative placeholders** — replace them with the real
measured output.)

**Acceptance:**
- `scripts\bench_jev.py` runs offline with no key and exits 0.
- `--json` emits parseable JSON.
- `--assert` exits 0 and would exit 1 if a case were made 5× slower.
- The real measured table is pasted into **Baseline** below and into
  `docs/perf-baseline.md`.

---

## Task 1.3 — Add the plugin drift guard

**Edit:** `tests/test_plugin.mjs`

The current hardening checks (lines 129–136) read the *example* file and assert
source substrings. That proves the example contains certain strings; it says
nothing about what is installed. Add a real drift test.

### What to add

A test that compares the installed plugin against the example by content hash.

```js
// --- 8. Installed plugin must match the committed example -----------------
console.log("--- 8. Testing installed plugin drift ---");

const homePlugin = path.join(os.homedir(), ".config", "opencode", "plugins", "jev-plugin.js");
if (!fs.existsSync(homePlugin)) {
  console.warn(
    `SKIP: no installed plugin at ${homePlugin}. ` +
    `Copy config\\jev-plugin.example.js there per config\\README.md step 5.`
  );
} else {
  const installed = fs.readFileSync(homePlugin, "utf-8");
  const example = fs.readFileSync(pluginPath, "utf-8");
  if (installed !== example) {
    const instLines = installed.split("\n").length;
    const exLines = example.split("\n").length;
    console.error(
      `\nFAIL: installed plugin has drifted from config/jev-plugin.example.js\n` +
      `  installed: ${homePlugin} (${instLines} lines)\n` +
      `  example:   ${pluginPath} (${exLines} lines)\n` +
      `  Fix: copy the example over the installed file, or move the shared logic\n` +
      `  into a module both import. See .agents_plans/phase-6-plugin-as-mcp-client.md\n`
    );
    process.exit(1);
  }
  console.log("Installed plugin matches the committed example.");
}
```

**This test fails today.** That is correct and intended — it is the first automated
detection of finding #14.

### Two ways to resolve the failure

Pick one now so Phase 1 lands green:

- **(A) Immediate** — copy the hardened example over the installed file:
  ```powershell
  Copy-Item D:\mcp\jev-typesafe-mcp\config\jev-plugin.example.js `
            "$env:USERPROFILE\.config\opencode\plugins\jev-plugin.js" -Force
  ```
  Correct, but the installed file still re-implements Jev logic in Python and can
  drift again. Phase 6 deletes that duplication.

- **(B) Mark known-drift, fail later** — add `process.env.JEV_ALLOW_PLUGIN_DRIFT`
  as a temporary escape hatch, land green, and remove it in Phase 6.

**Recommendation: (A).** It costs one command, immediately makes the live plugin
safe, and the drift test then guards every subsequent phase.

### Also convert the substring assertions

Lines 129–136 assert the source *contains* `RetryPolicy(`, `timeout=3.0`,
`maxDepth = 6`, etc. Keep them — they are cheap documentation of intent — but add
*behavioral* tests alongside (Phase 6 replaces them with real hook tests against a
stubbed MCP child; Phase 1 only needs the guard).

**Acceptance:**
- `node tests/test_plugin.mjs` passes with the installed plugin in sync.
- Commenting out the `Copy-Item` (restoring the stale file) makes it fail with the
  diagnostic message.

---

## Task 1.4 — Record the baseline

**New file:** `docs/perf-baseline.md`

```markdown
# Performance baseline

Machine: <cpu>, Windows, Python 3.14 (.venv)
Measured: <date>  ·  Commit: <sha>  ·  Phase: 1 (pre-optimization)

<!-- Paste the real `scripts/bench_jev.py` table here. -->

## Phase 5 targets

| Case | Phase 1 | Target | Budget (`--assert`) |
|---|---|---|---|
| estimate_tokens_100k | — | 4× faster | 1.25× Phase 5 |
| mock_system_one_250 | — | 5× faster | 1.25× Phase 5 |
| find_agent_resources_250 | — | 2× faster | 1.25× Phase 5 |
| find_agent_resources_250_warm | — | 8× faster (cache) | 1.25× Phase 5 |
| envelope_size_skills | — | ~50% smaller | n/a |
```

`.gitignore` currently has no entry for `docs/` — it is not ignored, so this file
commits cleanly.

**Acceptance:** `docs/perf-baseline.md` exists with real measured numbers and
per-case Phase 5 targets.

---

## Files touched

| File | Change |
|---|---|
| `.gitignore` | add `.pytest_cache/` |
| `scripts/bench_jev.py` | **new** — offline benchmark with `--json` / `--assert` |
| `tests/test_plugin.mjs` | add drift guard (section 8) |
| `docs/perf-baseline.md` | **new** — recorded numbers + Phase 5 targets |
| `~/.config/opencode/plugins/jev-plugin.js` | overwrite with the hardened example |
| `.pytest_cache/` | deleted (generated) |

## Verification

```powershell
cd D:\mcp\jev-typesafe-mcp
& .\.venv\Scripts\python.exe -m pytest tests -q
& .\.venv\Scripts\python.exe scripts\bench_jev.py
& .\.venv\Scripts\python.exe scripts\bench_jev.py --assert
& .\.venv\Scripts\python.exe scripts\bench_jev.py --json | ConvertFrom-Json | Measure-Object
node tests/test_plugin.mjs
```

## Definition of done

- [x] `pytest tests -q` green, 0 failures, count recorded here
- [x] `bench_jev.py` runs offline, `--json` parses, `--assert` gates
- [x] Real numbers pasted into `docs/perf-baseline.md` with Phase 5 targets
- [x] `node tests/test_plugin.mjs` green, including the drift guard
- [x] `.gitignore` ignores `.pytest_cache/`

## Commit

```
chore(perf): add offline benchmark harness and plugin drift guard

Establish a measurable baseline before the reliability/performance work in
.agents_plans/. Adds scripts/bench_jev.py (10 offline hot-path cases with
--json/--assert modes), records real numbers in docs/perf-baseline.md, and
adds a content-hash drift test proving the installed OpenCode plugin matches
config/jev-plugin.example.js — the drift was undetected until now.

The installed plugin is overwritten with the hardened example: it was 427
lines against the example's 510 and was missing the recursion depth bound,
log rotation, prompt cap, RetryPolicy/timeout, skill-content truncation and
the null-safe splitModelId guard.
```

---

## Baseline

- pytest: `148 passed, 3 skipped`
- bench table:

```
case                          median_ms   min_ms   max_ms   bytes
estimate_tokens_100k                2.2      2.2      2.3       -
estimate_tokens_750_options         1.2      1.1      1.2       -
fit_state_no_trunc                  0.0      0.0      0.1       -
fit_state_trunc                    13.9     13.6     14.3       -
mock_choice_250                     2.0      1.9      2.2       -
mock_system_one_250                 6.8      6.8      8.1       -
find_agent_resources_250           71.2     69.7     71.7   44198
find_agent_resources_250_warm       73.5     71.2     77.4   44198
select_target_files_git            15.7     14.7     16.6       -
envelope_size_skills                  -        -        -   44198
```
