# JEV_MCP_IMPROVEMENT_PLAN.md

## Goal
Harden and upgrade `D:\mcp\jev-typesafe-mcp` (server `jev-engine`) using patterns from `reference/jkudish-jev-mcp` and `reference/burnigtm-jev-mcp`, without breaking the OpenCode plugin or existing consumers.

## Decisions (user-confirmed)
- Reference repos cloned **inside the repo** at `reference/jkudish-jev-mcp` and `reference/burnigtm-jev-mcp`; `reference/` added to `.gitignore`.
- **Multi-provider deferred** — TypeSafe (`typesafe-sdk`) only.
- **Backward-compatible** result shapes: keep all existing keys, add `action/confidence/usage/model/ranked/...`.
- Tests: **pytest**, offline unit/mock always run; live smoke gated on `TYPESAFE_API_KEY`.

## Borrow map
| Concept | Source | New file in repo |
|---|---|---|
| Fail-closed response envelope validation | burnigtm `src/responses.ts` | `jev_validation.py` |
| Typed errors + `errorDetails` | burnigtm `src/errors.ts` | `jev_errors.py` |
| Policy actions, confidence, worst_action, complete-context | burnigtm `src/policy.ts`; jkudish `src/lib.ts` | `policy.py` |
| Token budget fit + coverage/truncation | burnigtm `src/limits.ts` | `limits.py` |
| Deterministic offline judge | burnigtm `src/mock.ts` | `mock.py` |
| Env config parsing + validation | burnigtm `src/config.ts` | extend config loader in `jev_engine.py`/new `config.py` |
| Result envelope `{model, usage, truncated, action}` | burnigtm `src/result.ts` | `jev_mcp.py` helpers |
| Decision escape hatches | jkudish `lib.ts` (`DECIDE_ESCAPE_HATCHES`) | `policy.py` / tools |
| Existence + ranked selection | jkudish `lib.ts` (`existsVerdict`, `rankCandidates`) | `search_target_files`, `search_agent_skills` |

## Target file layout
```
jev_mcp.py      MCP server (4 tools, error/result envelopes)
jev_engine.py   engine + CLI (delegates to modules below; keeps `verify/resource/files` subcommands)
jev_errors.py   JevConfigError, JevResponseError, JevTimeoutError, JevCancelledError,
                JevBudgetError + error_details() -> {code,message,retryable}
jev_validation.py  validate_response() checks answers/types/probability-sums/score-mean/usage
policy.py       confidence_from_probabilities, action_from_confidence,
                worst_action, require_complete_context, validate thresholds,
                escape_hatches
limits.py       fit_state(state, questions) -> {state, truncated, coverage.complete}
mock.py         deterministic system_one for JEV_MCP_MOCK=1
tests/          unit (offline) + mock tool tests + optional live smoke (pytest)
config/         updated .env.example, README/config docs, settings example
requirements.txt  add pytest (dev)
.gitignore      add reference/, .env (already), etc.
```

## Execution steps

### Step 0 — Prep
1. `git clone https://github.com/jkudish/jev-mcp reference/jkudish-jev-mcp`
2. `git clone https://github.com/burnigtm/jev-mcp reference/burnigtm-jev-mcp`
3. Append to `.gitignore`: `reference/`
4. Write this file to `JEV_MCP_IMPROVEMENT_PLAN.md`
5. Verify venv: `python -m py_compile jev_engine.py jev_mcp.py`

### Step 1 — Foundations: errors + validation
- **`jev_errors.py`**: classes + `error_details(err)` returning `{code, message, retryable}`; `execute_system_one` re-raises TS-SDK errors as typed ones; timeout via config.
- **`jev_validation.py`**: `validate_response(raw, questions)` — answers keys == question keys; each answer type matches (`noul/choice/score`); `choice ∈ criteria`; probabilities finite in `[0,1]` and sum ≈ 1 (tol `0.01+1e-12`); score mean consistency `|score − Σ i·pᵢ| ≤ 0.02+1e-12`; `usage` present. On failure raise `JevResponseError`.
- **`jev_mcp.py`**: wrap each tool body in try/except → on typed error return `{"error": {code,message,retryable}}` (still JSON, plus MCP text). **Invalid responses never read as `safe:true`.**
- AC: `guardrail_command` with a malformed Jev answer returns `action:"escalate"`, not `safe:true`.

### Step 2 — Policy & confidence (`policy.py`)
- `confidence_from_probabilities(probs) = (max − 1/n) / (1 − 1/n)`, 0 when uniform/none.
- `action_from_confidence(conf, auto_accept=0.8, review_at=0.5)` → `auto|review|escalate`; validate `0 ≤ review_at ≤ auto_accept ≤ 1`.
- `worst_action(actions)`, `require_complete_context(action, truncated)` (truncated never `auto`), escape-hatch criteria set.
- Backward compat: keep `safe` computed as `auto and destructive_prob<0.20 and git_modify_prob<0.20`.

### Step 3 — Tool upgrades (backward-compatible keys)
- **guardrail_command**: add `action`, `confidence`, `reason_codes`, `model`, `usage`. Keep `safe/destructive_prob/git_modify_prob`.
- **search_agent_skills**: add `action`, `confidence`, `ranked:[{file, probability}]` (primary/secondary/tertiary probs), `truncated` (content clipped), `model`, `usage`. Keep `matched/count/primary/file/content/resources/summary`.
- **search_target_files**: add `exists` verdict (`answered/partial/absent`), `probability`, `confidence`, `action`, `ranked`, `model`, `usage`; "none" escape → `matched:false`. Keep `matched/files`.
- **select_model_tier**: add `confidence`, `action`; low confidence → `action:"escalate"`, `recommended_tier` unchanged; keep `enabled/task/recommended_tier/recommended_model/model_map`.

### Step 4 — Limits, mock, usage
- **`limits.py`**: `fit_state` — estimate tokens, mark `truncated` + `coverage.complete:false` when over budget (64k total / 32k state+longest); never let truncated input yield `auto`.
- **`mock.py`**: `JEV_MCP_MOCK=1` returns deterministic plausible answers (documented, tests/demos only).
- All results include `model` + `usage:{input_tokens,output_tokens}`.

### Step 5 — (Deferred by decision) Multi-provider
- Not in this pass. Note only: future `providers.py` porting jkudish `provider.ts` (OpenRouter/compatible, 408/409/429/5xx retry+backoff, secret redaction).

### Step 6 — Tests & docs
- `pytest` suite: `tests/test_validation.py`, `test_policy.py`, `test_limits.py`, `test_mock_tools.py` (offline, no key), `tests/test_live_smoke.py` (skipped unless `TYPESAFE_API_KEY` or `JEV_MCP_LIVE=1`).
- Update `requirements.txt` (add `pytest`), `.env.example` (`JEV_MCP_MODEL`, `JEV_MCP_TIMEOUT_MS`, `JEV_MCP_MOCK`, `JEV_MCP_AUTO_ACCEPT`, `JEV_MCP_REVIEW_AT`), `README.md`, `config/README.md`, settings example.
- Verification: `py_compile`, `pytest`, engine CLI smoke (`verify "git status"`), import check.

## Risks / notes
- Plugin (`jev-plugin.js`) uses its own inline Python query — unaffected by new keys; optionally later refactor to call the engine module.
- Score/choice validation must match `typesafe-sdk` v0.7.1 response shapes exactly (verify field names `noul/choice/score/probabilities/confidence/legend/usage`) before merging.
- Keep `get_answer/get_val/get_prob` compatibility for dict answers; SDK 0.7.1 returns objects.