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

---

# Phase 5 — performance

Measured: 2026-09-26  ·  Commit: 1ea5b6d (before) → Phase 5 (after)
Machine unchanged from Phase 1. The two `find_agent_resources` rows now mean
different things: the **cold** row clears the scan cache before every run (so it
measures discovery), the **warm** row primes it once. In Phase 1 and Phase 3 both
rows measured the same warm path, which is why they were within 3 ms of each
other.

```
case                          median_ms   min_ms   max_ms   bytes
estimate_tokens_100k                0.2      0.2      0.2       -
estimate_tokens_750_options         0.1      0.1      0.1       -
fit_state_no_trunc                  0.0      0.0      0.0       -
fit_state_trunc                     4.9      4.8      5.9       -
mock_choice_250                     0.3      0.3      0.8       -
mock_system_one_250                 1.3      1.2      2.4       -
find_agent_resources_250          113.6    110.2    125.0   13509
find_agent_resources_250_warm      84.4     81.4     86.1   13509
select_target_files_git            10.8      8.8     19.8       -
envelope_size_skills                  -        -        -   13509
```

| Case | Phase 1 | Phase 3 | Phase 5 | Target met |
|---|---|---|---|---|
| estimate_tokens_100k | 2.2 ms | 2.2 ms | **0.2 ms** (11×) | yes (target 4×) |
| estimate_tokens_750_options | 1.2 ms | 1.2 ms | **0.1 ms** | — |
| fit_state_trunc | 13.9 ms | 14.0 ms | **4.9 ms** (2.8×) | — |
| mock_choice_250 | 2.0 ms | 0.7 ms | **0.3 ms** | — |
| mock_system_one_250 | 6.8 ms | 3.6 ms | **1.3 ms** (5.2×) | yes (target 5×) |
| find_agent_resources_250 | 71.2 ms | 133.9 ms | **113.6 ms** cold | no — see below |
| find_agent_resources_250_warm | 73.5 ms | 131.1 ms | **84.4 ms** | no — see below |
| select_target_files_git | 15.7 ms | 20.3 ms | **10.8 ms** | — |
| envelope_size_skills | 44 198 B | 19 684 B | **13 509 B** | yes (31% of Phase 1) |

`--assert` is now gated at 1.25× these numbers instead of 3× the Phase 1
baseline: 0.5 / 0.4 / 0.5 / 12 / 1.5 / 3 / 145 / 105 / 25 ms and 30 kB.

## The two Phase 1 targets that were not met, and why

`find_agent_resources_250` (2× vs Phase 1) and `_warm` (8× vs Phase 1) are not
reachable any more, and the plan's targets were written before Phase 3 landed.
Phase 3 gave every candidate a real preview so the Choice could actually tell
them apart; that added 250 file reads and ~60 ms to this call, and it bought
top-1 accuracy 0.444 → 0.667–0.833 (table above). Measuring against the Phase 1
71.2 ms would mean deleting the evidence that made the tool work.

Against the honest baseline — Phase 3, 133.9 ms — Phase 5 delivers:

| Change | Effect on this call |
|---|---|
| `.agents` + `.agents/skills` + `.agents/workflows` + `.agents/memory` collapsed to one walk | the same 250 files were being discovered **four** times per call |
| `MAX_DISCOVERED_FILES` bound on the walk | `rglob` had no depth or count limit |
| scan cache | cold 113.6 ms → warm 84.4 ms (−29 ms, the walk itself) |
| `file`/`content` removed from the envelope | 19 684 B → 13 509 B |
| `estimate_tokens` at C speed | 250 previews + 250 criteria no longer re-scanned character by character |
| mock: one state tokenization + memoized description tokens | 3.6 ms → 1.3 ms of judge time |

The remaining 84 ms is 250 file reads and 250 `markdown_preview` passes
(`build_criteria` is 66% of the call under `cProfile`) — that is the Phase 3
trade, and it is bounded by `MAX_TOTAL_CRITERIA_CHARS`.

## What the cache does and does not see

Both scanners cache **paths**, never content, and the signature is a bounded
directory-mtime list (`scan_cache.SIG_MAX_LEVELS` = 3 levels, 2 000 directories
max, `MAX_ENTRIES`):

- a skill added, removed or renamed under `.agents/<x>/<y>/` invalidates it;
- a file *edited in place* does not — and does not need to, because the preview
  is rebuilt from a fresh read on every call;
- the `git ls-files` result is additionally keyed on HEAD, the ref, the index
  mtime and `.gitignore`, **plus** the same directory signature, because
  `ls-files --others` also reports untracked files that leave HEAD and the index
  untouched.

A 5-second sliding TTL (`scan_cache.DEFAULT_TTL_S`) bounds staleness for a
paused sequence. The cost is real and is paid on the cold row above: the
signature is not free, which is why the markdown cache is close to break-even in
a tree with one file per directory and a clear win in a tree with many.

## Behaviour changes verified, not assumed

- `estimate_tokens` is bit-identical to the old per-character loop on a pinned
  corpus (`tests/test_limits.py::TestEstimateTokensExactness`).
- `mock_system_one` answers are pinned to a golden recorded from the pre-Phase-5
  implementation and re-verified by running both modules over the same 250-option
  request (`tests/test_mock_perf.py::GOLDEN`). The optimization is only allowed
  to change how fast it gets there.
- Truncation via the binary search returns a **longer** prefix than the old
  linear scan at the same budget: the old code reserved marker tokens up front
  and then appended the marker again, double-counting them.
- Mock `usage.input_tokens` is now `coverage.estimated_tokens.state + .questions`
  from `fit_state` instead of a re-serialization of `{"state": …, "questions": …}`,
  so it is slightly smaller (e.g. 15 585 → 15 568 on the 250-option case). The
  live path is untouched: the provider reports its own usage.


---

# Phase 6 - the plugin as a stdio MCP client

Measured: 2026-09-26  |  Commit: (this change)  |  Phase: 6
Harness: the `chat.message` hook itself, driven from Node against the real
`jev_mcp.py` with `JEV_MCP_MOCK=1` (so the number is engine work, not network).

## Per-message plugin cost

| | before (inline Python per message) | after (one reused MCP child) |
|---|---|---|
| cold, first message of a session | ~1.3 s | **933 ms** (spawn + `initialize` + one call) |
| warm, every message after | ~1.3 s | **13-14 ms** |
| interpreter start + `import typesafe_sdk` | ~360 ms **per message** | once per session |
| child processes per message | 1 | 0 |

Component costs behind those numbers, same machine:

```
bare interpreter (python -c pass)            42 ms
  + import typesafe_sdk                     358 ms   (~316 ms of import)
  + import jev_mcp (whole server stack)     953 ms   (paid once, on spawn)
```

The per-message saving is the ~360 ms of interpreter start and SDK import that
the old `python -c "<inline program>"` paid on **every** message. The one-off
cost of the longer server import stack is amortised across the whole session, and
the server additionally keeps its client and settings caches warm, which the
per-message process threw away each time.

This is the mock engine, so 13-14 ms is the floor: it is JSON-RPC framing plus the
offline judge. A live call adds one HTTPS round trip to both columns; the
difference between them is unchanged.

## What else the transport bought

Not latency - correctness. The old inline program reimplemented the decision path
and inherited none of it: no `fit_state` budgeting, no `validate_response`
fail-closed check, no `action_from_confidence` threshold, no `candidates_truncated`
awareness, no `none` escape-hatch semantics, no error taxonomy. A malformed
provider response therefore read as a confident pick, and a low-confidence
judgment still injected a skill into the model's context. Driving the real server
means the plugin now applies the server's own `action` / `confidence` verdict
(both effects require `auto` and >= 0.6) instead of "whatever `choice` returned".
