# Performance baseline

Host: one consumer x86-64 laptop, Windows, CPython 3.14 (`.venv`).

> **All timings in this document are ratios, not milliseconds.** Wall-clock
> numbers measured on someone else's machine describe their machine. The ratios,
> byte counts, token counts and accuracy figures below are the portable results.
>
> The literal millisecond thresholds live in
> [`scripts/bench_jev.py`](../scripts/bench_jev.py) — the `THRESHOLDS` table
> (line 52) and `IDLE_PROBE_MAX_MS` (line 301). Those are calibrated on the
> host that wrote them; `scripts/bench_jev.py` detects a loaded machine and
> reports `INCONCLUSIVE` rather than pretending to judge it (see *Phase 7*).
>
> Index: **Phase 1 = 1.00×**. Every timing column is expressed as a multiple of
> the Phase 1 median. Bytes, tokens and accuracy are machine-independent and are
> kept absolute.

## Phase 5 targets

| Case | Target | Budget (`--assert`) |
|---|---|---|
| `estimate_tokens_100k` | 4× faster | 1.25× measured |
| `mock_system_one_250` | 5× faster | 1.25× measured |
| `find_agent_resources_250` | 2× faster | 1.25× measured |
| `find_agent_resources_250_warm` | 8× faster (cache) | 1.25× measured |
| `envelope_size_skills` | ~50% smaller | absolute byte cap |

---

# Phase 3 — decision quality (candidate evidence)

Measured 2026-09-26 · commits `deb3ffb` (before) → Phase 3 (after)
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

| `MAX_TOTAL_PREVIEW_CHARS` | top-1 | input tokens / call |
|---|---|---|
| 40 000 (kept) | 0.778 | 12 885 |
| 24 000 | 0.667 | 8 378 |
| 0 (paths only — the old behaviour) | 0.444 | 1 410 |

40 000 is kept because top-3 recall is already 1.0 there and halving the budget
costs 11 points of top-1. Lower it deliberately if per-call cost matters more
than the last few points of top-1.

## `select_mcp_tools` — measured cost

The roster is the caller's, so this tool has no filesystem work at all: the
criteria are built from strings already in the payload. Over the 13 labelled
cases in `tests/fixtures/mcp_selection_tasks.json`, live (jev-1.13):

| Metric | Value |
|---|---|
| input tokens / call (mean) | 978 |
| requests / call | 1.0 |
| top-1 accuracy | 0.90 |
| acceptable-server rate | 1.00 |
| false-positive rate | 0.00 |
| times the judge selected itself | 0 |

Four questions ride along in that one request, so the token figure is the whole
cost of the verdict, not one question's. The three candidate caps are what keep
it there: `MAX_MCP_SERVERS` 64, `MAX_MCP_TOOL_OPTIONS` 250 (the `Choice`
ceiling) and `MAX_MCP_TOOL_DESC_CHARS` 300 per tool, with the
`MAX_TOTAL_CRITERIA_CHARS` share applied across servers. A roster past any of them
sets `candidates_truncated` and blocks `action: "auto"`.

A 20-tool answer is ~1.5 kB of envelope: the returned entries carry
`{server, tool, probability}` and no description, because the agent already holds
the roster it sent.

## `bench_jev.py` — Phase 3

```
case                          vs Phase 1   envelope_bytes
estimate_tokens_100k                1.00x              -
estimate_tokens_750_options         1.00x              -
fit_state_no_trunc                  0.00x              -
fit_state_trunc                     1.01x              -
mock_choice_250                     0.35x              -
mock_system_one_250                 0.53x              -
find_agent_resources_250            1.88x           19 684
find_agent_resources_250_warm       1.78x           19 684
select_target_files_git             1.29x              -
envelope_size_skills                     -           19 684
```

| Case | Phase 1 → Phase 3 | Note |
|---|---|---|
| `find_agent_resources_250` | 1.00× → 1.88× | +250 previews (was 0 reads); inside the wall-clock gate |
| `envelope_size_skills` | 44 198 B → 19 684 B | 45% of Phase 1; family clustering no longer fills every slot with a sibling |
| `mock_system_one_250` | 1.00× → 0.53× | mock scores shared terms instead of re-overlapping the whole state |
| `select_target_files_git` | 1.00× → 1.29× | +47 previews |

All `--assert` gates pass.

---

# Phase 5 — performance

Measured 2026-09-26 · commit `1ea5b6d` (before) → Phase 5 (after), same host as
Phase 1.

The two `find_agent_resources` rows now mean different things: the **cold** row
clears the scan cache before every run (so it measures discovery), the **warm**
row primes it once. In Phase 1 and Phase 3 both rows measured the same warm path,
which is why they were within 3% of each other.

```
case                          vs Phase 1   envelope_bytes
estimate_tokens_100k                0.09x              -
estimate_tokens_750_options         0.08x              -
fit_state_no_trunc                  0.00x              -
fit_state_trunc                     0.35x              -
mock_choice_250                     0.15x              -
mock_system_one_250                 0.19x              -
find_agent_resources_250            1.60x           13 509
find_agent_resources_250_warm       1.15x           13 509
select_target_files_git             0.69x              -
envelope_size_skills                     -           13 509
```

| Case | Phase 1 | Phase 3 | Phase 5 | Target met |
|---|---|---|---|---|
| `estimate_tokens_100k` | 1.00× | 1.00× | **0.09×** (11× faster) | yes (target 4×) |
| `estimate_tokens_750_options` | 1.00× | 1.00× | **0.08×** | — |
| `fit_state_trunc` | 1.00× | 1.01× | **0.35×** (2.8× faster) | — |
| `mock_choice_250` | 1.00× | 0.35× | **0.15×** | — |
| `mock_system_one_250` | 1.00× | 0.53× | **0.19×** (5.2× faster) | yes (target 5×) |
| `find_agent_resources_250` | 1.00× | 1.88× | **1.60×** cold | no — see below |
| `find_agent_resources_250_warm` | 1.00× | 1.78× | **1.15×** | no — see below |
| `select_target_files_git` | 1.00× | 1.29× | **0.69×** | — |
| `envelope_size_skills` | 44 198 B | 19 684 B | **13 509 B** | yes (31% of Phase 1) |

`--assert` is now gated at **1.25× the Phase 5 column** instead of 3× the Phase 1
baseline. The size gates stay absolute (bytes do not depend on load) — currently
30 kB for `envelope_size_skills`.

## The two Phase 1 targets that were not met, and why

`find_agent_resources_250` (2× vs Phase 1) and `_warm` (8× vs Phase 1) are not
reachable any more, and the plan's targets were written before Phase 3 landed.
Phase 3 gave every candidate a real preview so the Choice could actually tell
them apart; that added 250 file reads and +88% to this call, and it bought top-1
accuracy 0.444 → 0.667–0.833 (table above). Measuring against Phase 1's 1.00×
would mean deleting the evidence that made the tool work.

Against the honest baseline — Phase 3, 1.88× — Phase 5 delivers:

| Change | Effect on this call |
|---|---|
| `.agents` + `.agents/skills` + `.agents/workflows` + `.agents/memory` collapsed to one walk | the same 250 files were being discovered **four** times per call |
| `MAX_DISCOVERED_FILES` bound on the walk | `rglob` had no depth or count limit |
| scan cache | cold 1.60× → warm 1.15× (−28% of the call; the walk itself) |
| `file`/`content` removed from the envelope | 19 684 B → 13 509 B (−31%) |
| `estimate_tokens` at C speed | 250 previews + 250 criteria no longer re-scanned character by character |
| mock: one state tokenization + memoized description tokens | judge time 0.53× → 0.19× |

The remaining warm cost is 250 file reads and 250 `markdown_preview` passes
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
  so it is slightly smaller. The live path is untouched: the provider reports its
  own usage.

---

# Phase 6 — the plugin as a stdio MCP client

Measured 2026-09-26 · Phase 6
Harness: the `chat.message` hook itself, driven from Node against the real
`jev_mcp.py` with `JEV_MCP_MOCK=1` (so the number is engine work, not network).

## Per-message plugin cost

| | before (inline Python per message) | after (one reused MCP child) |
|---|---|---|
| cold, first message of a session | 1.00× | **0.72×** (−28%) |
| warm, every message after | 1.00× | **~0.01×** (−99%) |
| interpreter start + `import typesafe_sdk` | paid **every message** | once per session |
| child processes per message | 1 | 0 |

Component costs behind those numbers, as multiples of a bare interpreter start
(`python -c pass`) on the same host:

```
bare interpreter (python -c pass)          1.0x
  + import typesafe_sdk                   8.5x   (~7.5x of it is the import)
  + import jev_mcp (whole server stack)  22.7x   (paid once, on spawn)
```

The per-message saving is the interpreter start plus SDK import that the old
`python -c "<inline program>"` paid on **every** message. The one-off cost of the
longer server import stack is amortised across the whole session, and the server
additionally keeps its client and settings caches warm, which the per-message
process threw away each time.

This is the mock engine, so the warm figure is the floor: it is JSON-RPC framing
plus the offline judge. A live call adds one HTTPS round trip to both columns;
the difference between them is unchanged.

## What else the transport bought

Not latency — correctness. The old inline program reimplemented the decision path
and inherited none of it: no `fit_state` budgeting, no `validate_response`
fail-closed check, no `action_from_confidence` threshold, no `candidates_truncated`
awareness, no `none` escape-hatch semantics, no error taxonomy. A malformed
provider response therefore read as a confident pick, and a low-confidence
judgment still injected a skill into the model's context. Driving the real server
means the plugin now applies the server's own `action` / `confidence` verdict
(both effects require `auto` and ≥ 0.6) instead of "whatever `choice` returned".

---

# Phase 7 — the gates had to learn what "slow" means

Measured 2026-09-26 · Phase 7

Phase 7 changed no hot path. It deleted an unused settings lookup and four unused
imports, so the honest expectation for `bench_jev.py --assert` was "identical".
It was not:

```
case                          Phase 5    Phase 7 (loaded)   Phase 7 (quietest)
estimate_tokens_100k              1.0x         1.4 – 1.5x          1.0x
fit_state_trunc                   1.0x         1.4 – 1.8x          1.0x
mock_system_one_250               1.0x         1.2 – 1.9x          1.0x
find_agent_resources_250          1.0x         1.3 – 1.7x          1.0x
find_agent_resources_250_warm     1.0x         1.3 – 2.3x          1.0x
select_target_files_git           1.0x         1.4 – 2.8x          1.0x
envelope_size_skills           13 509 B      13 509 B           13 509 B
```

Every **timing** row moved by 1.2–2.8×. `envelope_size_skills` did not move at
all, because it measures bytes. That asymmetry is the diagnosis: a code change
cannot slow down `fit_state_trunc`, which is a `truncate_to_token_budget` binary
search over a 1 MB string with no I/O and no module of ours in the loop. The
uniform inflation is the machine. Total CPU time read 30–44% during the worst
runs and 20–21% during the quietest, from unrelated MCP servers and a browser on
the same desktop.

## What changed in `bench_jev.py`

The thresholds were **not** moved. Re-basing a wall-clock gate on a loaded
machine bakes one afternoon's desktop into the repository and makes the next
person on a fast machine read a healthy run as a regression. Instead, `--assert`
now measures whether the machine can be judged at all:

- `fit_state_trunc` is the load probe. It is already in the table, it is pure
  in-memory, and no change in this project can move it.
- At or below `IDLE_PROBE_MAX_MS` (set just above that case's idle reading —
  ≈1.2× on the calibration host, see `scripts/bench_jev.py:301`), the wall-clock
  gates are enforced exactly as before and a breach is a `Regression`.
- Above it, the run is reported `INCONCLUSIVE`, each breached timing gate is
  printed as `SKIP: Inconclusive ...` instead of `FAIL`, and the process exits
  **2** rather than 1. The **size gates are still enforced**, because bytes do
  not depend on load.

Exit codes are now `0` pass, `1` regression, `2` machine too loaded to judge.

## What this is and is not

It is not a fix for a slow `find_agent_resources`. The 1.15× warm figure from
Phase 5 still stands on an idle machine, and the Phase 3 accuracy trade that
produced it is unchanged. What changed is that the harness can now tell the two
situations apart instead of reporting whichever one it happened to run in.

The honest limit: `IDLE_PROBE_MAX_MS` is calibrated on one host. A faster or
slower host re-calibrates it the same way the thresholds were calibrated — by
reading the table above and setting the number between that host's idle and loaded
figures for the probe case. A threshold that has never been measured on the host
running it is not a gate, it is a guess.