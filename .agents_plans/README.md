# jev-typesafe-mcp — Reliability & Performance Remediation Plan

Audit of the Python MCP server (`jev_mcp.py`, `jev_engine.py`, and support modules)
and the OpenCode plugin (`config/jev-plugin.example.js` +
`~/.config/opencode/plugins/jev-plugin.js`).

**Scope:** 30 findings, 7 phases. Each phase is independently shippable, ends green
on the full test suite, and closes a named subset of findings.

**Status:** PLAN ONLY. No code has been changed. Each phase file is a self-contained
work order for a fresh session.

---

## How to use this folder

Start a new chat, state the phase number, and the agent should read that file plus
this index. Files are ordered by dependency — do not start Phase 3 before Phase 2
(Phase 3 changes the question shapes that Phase 2's output models will validate).

| Phase | File | Closes | Depends on |
|---|---|---|---|
| 1 | [phase-1-scaffolding.md](phase-1-scaffolding.md) | #30 (partial) | — |
| 2 | [phase-2-fail-closed.md](phase-2-fail-closed.md) | #1–#6, #22 | 1 |
| 3 | [phase-3-decision-quality.md](phase-3-decision-quality.md) | #7–#11 | 2 |
| 4 | [phase-4-deadlines-and-bounds.md](phase-4-deadlines-and-bounds.md) | #12, #13, #17–#20 | 2 |
| 5 | [phase-5-performance.md](phase-5-performance.md) | #23–#29 | 1, 3, 4 |
| 6 | [phase-6-plugin-as-mcp-client.md](phase-6-plugin-as-mcp-client.md) | #14–#16 | 2, 3 |
| 7 | [phase-7-tests-and-docs.md](phase-7-tests-and-docs.md) | #30 (remainder), docs drift | all |

---

## Findings index

### P0 — Fail-open / contract bugs

| # | Finding | Evidence | Phase |
|---|---|---|---|
| 1 | `NaN` passes the fail-closed validator; no `math.isfinite` guards. `nan < 0` → False, `nan > 1` → False, `abs(nan-1.0) > 0.01` → False. NaN then reaches `json.dumps`, which emits bare `NaN` — invalid JSON. | `jev_validation.py:32-36,41-44,47-60,63-69,88-124` | 2 |
| 2 | Three different envelope shapes per tool. The no-work early returns omit `action`/`confidence`/`truncated`/`coverage`/`model`/`usage`. Confirmed live: `select_model_tier` returns no `coverage` key. | `jev_engine.py:544-550,754-755,820-833` | 2 |
| 3 | Errors are reported as successes — `_run` returns the error envelope as a normal result, so `is_error=False`. Verified: `mcp` sets `is_error` only on raise (`mcp/server/mcpserver/server.py:447`); `convert_result` returns a bare `CallToolResult` (`utilities/func_metadata.py:211-212`). | `jev_mcp.py:61-65` | 2 |
| 4 | `.env` overrides injected env, not the reverse. `override=True` means a stale `.env` beats the `TYPESAFE_API_KEY` opencode injects, and can silently force `JEV_MCP_MOCK=1`. README calls it a "fallback"; it is the opposite. | `config.py:27,29` | 2 |
| 5 | Score tolerance stricter than the reference → legitimate long-rubric responses rejected as `INVALID_RESPONSE`, wasting an API call. Absolute `0.02` vs reference `0.01 * (n-1)`. | `policy.py:24`, `jev_validation.py:120` vs `reference/burnigtm-jev-mcp/src/responses.ts:39` | 2 |
| 6 | Two attribute-access paths disagree inside one function: `getattr(ans,"probabilities")` vs `_attr_opt(...)`. Dict-shaped answers silently lose the ≥0.12 augmentation while `ranked` keeps it. | `jev_engine.py:586` vs `:628` | 2 |
| 22 | `TypeSafeAPIError` is not caught in `execute_system_one`, so 400/401 surface as generic `API_ERROR`; `error_details` leaks endpoint + request_id via `str(err)`. | `jev_engine.py:328-333`, `jev_errors.py:87` | 2 |

### P1 — The model is given almost no evidence (biggest quality bug)

| # | Finding | Evidence | Phase |
|---|---|---|---|
| 7 | Choice criteria carry no discriminative content. `search_target_files` uses one identical string for all 250 candidates; `search_agent_skills` uses the basename — literally `"SKILL.md"` for every skill. State is task text only. The reference puts candidate *text* into criteria. | `jev_engine.py:757-758,554` vs `reference/.../packs/rank.ts:12` | 3 |
| 8 | No presence judgment. A `none` option inside the Choice is the weak pattern; the reference pairs the Choice with a separate `Noul` so "a forced winner among poor options" cannot masquerade as a match. | `jev_engine.py:758` vs `reference/.../packs/rank.ts:23-26` | 3 |
| 9 | Silent 250-candidate truncation. Candidates past the cap are dropped with no signal — `truncated` only reflects token budget. A relevant file at `git ls-files` position 251 is invisible, and git returns alphabetical order. | `jev_engine.py:552,729` | 3, 4 |
| 10 | Family-prefix clustering has no confidence floor. A 0.15-probability primary can claim all 5 slots with same-family files — each then read from disk. | `jev_engine.py:597-604` | 3 |
| 11 | `secondary`/`tertiary` are pure waste: 3 parallel Choices over identical 250-option criteria triple question tokens for zero information, since `primary.probabilities` already yields the full ranking. | `jev_engine.py:562-573` vs `:626-634` | 3 |

### P2 — Reliability / resilience

| # | Finding | Evidence | Phase |
|---|---|---|---|
| 12 | Worst case is 3× the configured timeout. `RetryPolicy` is built without `timeout`, which defaults to 30 s; per-attempt timeout is 30 s. `JEV_MCP_TIMEOUT_MS=30000` → up to ~91.5 s. The reference caps each attempt to the remaining deadline and sets `apiTimeoutError:false`. | `jev_engine.py:298-307`; `typesafe_sdk/_core/retry.py:82`; cf. `reference/.../typesafe.ts:13-28,68-99` | 4 |
| 13 | No circuit breaker. A 401 or outage pays the full retry budget on every call — and on every message via the plugin. | `jev_engine.py:273-309` | 4 |
| 14 | Plugin drift is real and measured: installed SHA ≠ example SHA (427 vs 510 lines). Installed lacks depth bound, log rotation, prompt cap, `RetryPolicy`/timeout, skill truncation, `null`-safe `splitModelId`; has hardcoded `D:\` paths, a `.env` regex that breaks on quoted keys, and a 4 s kill against the SDK's 30 s default. | installed `jev-plugin.js` vs `config/jev-plugin.example.js` | 6 |
| 15 | Plugin path traversal — the Python side is safe, so the asymmetry is the bug. `scan_paths` resolves against cwd with no containment check; `path.relative(cwd,file)` yields `../../..` and the model can select it → `fs.readFileSync` outside the workspace. Python skips these via `relative_to` `ValueError`. | `jev-plugin.example.js:445-452` vs `jev_engine.py:540-541` | 6 |
| 16 | No concurrency or cancellation control in the plugin: fresh Python process per message, no in-flight dedupe, no queue, no abort, no cancel-aware timeout. | `jev-plugin.example.js:245-380,460` | 6 |
| 17 | `root_dir` guard is a denylist. POSIX blocks only `/ /etc /usr /bin /sbin` — `/var`, `/home`, `/proc`, `/root` all pass. Windows blocks only SystemRoot/ProgramFiles + drive roots — `C:\Users`, `C:\ProgramData` pass. The `blocked_posix` set is also built unconditionally (dead on Windows). | `jev_engine.py:61-107` | 4 |
| 18 | Settings precedence footgun. First-wins, so this repo's always-present all-defaults `jevs_settings.json` permanently shadows `~/.config/opencode/jevs_settings.json`. | `jev_engine.py:215-239` | 4 |
| 19 | Socket leak. Both the mock branch and `_reset_client_cache` drop the `httpx2.Client` reference without `.close()`. | `jev_engine.py:265-270,284-288` | 4 |
| 20 | Cached settings aliased into results: `result["model_map"]` is the live cached dict. | `jev_engine.py:820-825,849` | 4 |
| 21 | Dead code / untested paths. `Score` imported unused; `ESCAPE_HATCHES` (`ask_user`/`investigate`) never offered; no tool uses `Score`, so the whole Score validation path is untested in production. | `jev_engine.py:10`, `jev_validation.py:9`, `policy.py:28-32` | 7 |

### P3 — Performance

| # | Finding | Evidence | Phase |
|---|---|---|---|
| 23 | `estimate_tokens` is a per-character Python loop, run 3–4× per request over state + 750 option descriptions. A precompiled-regex non-ASCII count runs at C speed. | `limits.py:26-36` | 5 |
| 24 | Unbounded per-call filesystem work — full tree walk + up to 5×6000-char reads, and a `git ls-files` subprocess, on every call. Settings has an mtime cache; the scanners have none. | `jev_engine.py:532-542,608-622,692-701` | 5 |
| 25 | Mock is O(n·m). `_overlap` re-tokenizes the whole state for every option × question → 750 full tokenizations for 250 options × 3 questions. | `mock.py:36-42,87-100` | 5 |
| 26 | Redundant serialization. Mock re-serializes state+questions just to count usage; `log_tool_call` `json.dumps` the whole result again per call. | `mock.py:149`, `jev_logging.py:107-111` | 5 |
| 27 | Envelope duplication — `content`/`file` returned *and* `primary`/`resources` → 2× the 6000-char payload on the wire. | `jev_engine.py:653-667` | 5 |
| 28 | `_redact` is over- and under-aggressive: nukes a whole field if any marker appears anywhere; redacts ordinary long alnum strings (>40 chars), destroying diagnostics; misses other key prefixes; never applied to `result_preview`. | `jev_logging.py:45-54,110` | 5 |
| 29 | Double traceback per failure — `_request` and `_run` each log one. | `jev_engine.py:446-459`, `jev_mcp.py:63-64` | 5 |

### P4 — Tests give false confidence

| # | Finding | Evidence | Phase |
|---|---|---|---|
| 30 | `test_family_prefix_sibling_matching` sets up 3 skills and asserts only `res is not None` — it does not test family-prefix at all. Same for `test_target_files_bounded_depth`. `test_plugin.mjs` only greps source substrings, and only against the example — the installed file passes none of those guards and is untested. `.pytest_cache` shows a currently failing `test_cleanup_phase8.py::test_ensure_dotenv_creates_or_loads` (stale node id; test was renamed). No coverage for NaN, path traversal, plugin drift, envelope shape, `isError`, or any performance regression. | `tests/test_hardening_integration.py:48-67,122-136`, `tests/test_plugin.mjs:129-136`, `.pytest_cache/v/cache/lastfailed` | 1, 7 |

---

## Deliberately out of scope

Flagged rather than assumed. Raise these if you want them in a later phase.

1. **Switching to `AsyncTypeSafeClient`.** Unnecessary — the MCP SDK already runs
   sync tools in a thread pool (`mcpserver/resolve.py:556`), so there is no
   event-loop blocking. A rewrite would be pure risk.
2. **Full chunk-and-rerank for >250 candidates** (reference `batchesThatFit` in
   `src/tools/rank.ts:115-133`). Phase 3 makes truncation *visible* and non-`auto`,
   which is the safety-relevant part. Chunking multiplies API calls and cost.
3. **Removing the dead `jev_settings`-in-`opencode.json` legacy lookup.** `config/README.md`
   notes opencode's config schema is strict (`additionalProperties: false`), so this
   path is unreachable. Cheap cleanup; not urgent.

---

## Global verification (run after every phase)

```powershell
cd D:\mcp\jev-typesafe-mcp

# Syntax + imports
& .\.venv\Scripts\python.exe -m py_compile jev_engine.py jev_mcp.py jev_validation.py jev_errors.py policy.py limits.py config.py mock.py jev_logging.py
& .\.venv\Scripts\python.exe -c "import jev_mcp; print('MCP import OK')"

# Offline suite (no API key needed)
& .\.venv\Scripts\python.exe -m pytest tests -q

# Plugin suite
node tests/test_plugin.mjs

# Transport-level repro, all four tools, mock then live
& .\.venv\Scripts\python.exe scripts\diag_mcp.py --tool guardrail_command --command "git status" --mock
& .\.venv\Scripts\python.exe scripts\diag_mcp.py --tool search_agent_skills --task "fix ui bug" --root_dir . --mock
& .\.venv\Scripts\python.exe scripts\diag_mcp.py --tool search_target_files --task "find config loader" --root_dir . --mock
& .\.venv\Scripts\python.exe scripts\diag_mcp.py --tool select_model_tier --task "refactor auth middleware" --mock
```

`diag_mcp.py` exits 0 on a clean call, 1 on an error envelope or transport failure.

## Ground rules for every phase

- **Fail closed.** No path may return a decision-shaped payload after a failure.
  An error must be visibly an error (`is_error=true` + `{"error": {...}}` body).
- **No silent truncation.** If anything is dropped, capped, or clipped, it must be
  reported in the envelope and must prevent `action: "auto"`.
- **Every JSON number must be finite.** `json.dumps(..., allow_nan=False)` at the
  boundary as a last-resort guard, on top of validator-level `math.isfinite`.
- **One envelope shape per tool**, declared as a Pydantic output model so the MCP
  runtime publishes a real `outputSchema` and validates it server-side.
- **Keep all existing envelope keys.** Phase 2 unifies shapes; it does not remove
  keys. The only sanctioned removals are noted inline in the phase file that does
  them (Phase 5 removes the duplicated `file`/`content` in `search_agent_skills`).
- **Tests ship with the code they cover.** A phase is not done until the new tests
  fail on the old code and pass on the new.
- **Record before/after numbers** in `docs/perf-baseline.md` for any phase touching
  `limits.py`, `mock.py`, `jev_engine.py` scanners, or `jev_logging.py`.
- **Update the drift test** in `tests/test_plugin.mjs` whenever
  `config/jev-plugin.example.js` changes; copy it to the installed path in the same
  commit or the test will (correctly) fail.
