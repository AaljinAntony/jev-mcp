# Performance baseline

Machine: Intel(R) Core(TM) Ultra 7 255HX, Windows, Python 3.14 (.venv)
Measured: 2026-09-25  ·  Commit: ff606b2  ·  Phase: 1 (pre-optimization)

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

## Phase 5 targets

| Case | Phase 1 | Target | Budget (`--assert`) |
|---|---|---|---|
| estimate_tokens_100k | 2.2 ms | 4× faster (~0.5 ms) | 1.25× Phase 5 |
| mock_system_one_250 | 6.8 ms | 5× faster (~1.4 ms) | 1.25× Phase 5 |
| find_agent_resources_250 | 71.2 ms | 2× faster (~35.6 ms) | 1.25× Phase 5 |
| find_agent_resources_250_warm | 73.5 ms | 8× faster (cache) (~9.2 ms) | 1.25× Phase 5 |
| envelope_size_skills | 44198 B | ~50% smaller (~22 kB) | n/a |

---

# Phase 3 — decision quality (candidate evidence)

Measured: 2026-09-26  ·  Commit: deb3ffb (before) → Phase 3 (after)
Harness: `scripts/eval_routing.py` over `tests/fixtures/routing_tasks.json`
(25 labelled cases, 18 with an expected file, 7 expected to match nothing).
Both sides ran against the same working tree; the "before" column is the parent
commit run from a `git worktree` with `--root` pointed at this repo.

Jev is stochastic, so the live rows are a range over repeated runs of the whole
fixture set (before: 2 runs, after: 3 runs). The mock judge is deterministic.

## Routing metrics — live Jev

| Metric | Before | After | Direction |
|---|---|---|---|
| top-1 accuracy | 0.4444 (2/2 runs) | 0.667 – 0.833 | **up** |
| top-3 recall | 0.7778 (2/2 runs) | **1.0000 (3/3 runs)** | up |
| false-positive rate (`matched` with no expected file) | 0.000 – 0.143 | 0.000 – 0.143 | flat |
| `exists: "partial"` rate | 0.000 – 0.080 | 0.080 – 0.160 | up (fail-closed) |
| skill top-1 accuracy | 1.0000 | 1.0000 | flat |
| input tokens / call (mean) | 878 – 909 | 6 731 – 6 765 | **7.5× worse** |
| requests / call | 1.0 | 1.0 | unchanged |

Top-3 recall is the stable result: every one of the 18 labelled files is now in
the returned ranking, where the old code missed 4 of them. The false-positive
rate did not move — the old `none` description was already discriminative enough
for the five obvious no-match cases — so the presence Noul's measured effect is
the `partial` verdicts instead: cases where the Choice named a file and the
presence Noul disagreed now report `exists: "partial"` with `matched: false`
rather than `exists: "answered"`.

## Routing metrics — mock judge (`JEV_MCP_MOCK=1`)

| Metric | Before | After |
|---|---|---|
| top-1 accuracy | 0.0000 | 0.2222 |
| top-3 recall | 0.0556 | 0.4444 |
| false-positive rate | 0.5714 | 1.0000 |
| input tokens / call | 559.3 | 6 162.8 |

The mock's false-positive rate got worse and that is expected: it scores options
by shared words, and real previews give every one of the 47 candidates text to
share words with. It is an offline stand-in for shape and wiring, not a routing
model — the live rows above are the measurement that matters.

## Token cost is a deliberate, measured trade

Adding candidate text cannot be free: the plan hoped for "flat or lower" input
tokens and that is not reachable — the old request carried 47 bare paths. The
budget is now explicit and capped rather than accidental:

| Constant | Value | Effect |
|---|---|---|
| `MAX_CANDIDATE_CHARS` | 2 000 | per-candidate preview cap |
| `MAX_PREVIEW_READS` | 120 | at most 120 file reads per call |
| `MAX_TOTAL_PREVIEW_CHARS` | 40 000 | total preview payload per call |
| `MAX_TOTAL_CRITERIA_CHARS` | 96 000 | total criteria payload (skills) |

Measured trade-off on the same fixture set (live, `search_target_files`):
`MAX_TOTAL_PREVIEW_CHARS` 40 000 → top-1 0.778, 12 885 tokens;
24 000 → top-1 0.667, 8 378 tokens; 0 (paths only, i.e. the old behaviour)
→ top-1 0.444, 1 410 tokens. 40 000 is kept because the top-3 recall is already
1.0 there and halving the budget costs 11 points of top-1. Lower it deliberately
if per-call cost matters more than the last few points of top-1.

## `bench_jev.py` — Phase 3

```
case                          median_ms   min_ms   max_ms   bytes
estimate_tokens_100k                2.2      2.2      2.3       -
estimate_tokens_750_options         1.2      1.1      2.2       -
fit_state_no_trunc                  0.0      0.0      0.1       -
fit_state_trunc                    14.0     13.6     14.7       -
mock_choice_250                     0.7      0.7      0.9       -
mock_system_one_250                 3.6      3.6      5.3       -
find_agent_resources_250          133.9    131.3    135.3   19684
find_agent_resources_250_warm      131.1    126.4    131.6   19684
select_target_files_git            20.3     19.8     21.2       -
envelope_size_skills                  -        -        -   19684
```

| Case | Phase 1 | Phase 3 | Note |
|---|---|---|---|
| find_agent_resources_250 | 71.2 ms | 133.9 ms | +63 ms for 250 previews (was 0 reads); inside the 220 ms gate |
| envelope_size_skills | 44 198 B | 19 684 B | family clustering no longer fills every slot with a sibling |
| mock_system_one_250 | 6.8 ms | 3.6 ms | mock scores shared terms instead of re-overlapping the whole state |
| select_target_files_git | 15.7 ms | 20.3 ms | +5 ms for 47 previews |

All `--assert` gates pass.

