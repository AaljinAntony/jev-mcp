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

# Phase 2 — token cost

Measured 2026-10-02 · live, `jev-1.13.0`, harness `scripts/eval_routing.py`
over `tests/fixtures/routing_tasks.json` (23 file cases, 13 MCP cases) and
`tests/fixtures/mcp_selection_tasks.json`.

The plugin calls `search_agent_skills` on **every user message** and again on
**every Markdown read**, so this is the phase that decides what a session costs.

## Measured result

Jev is stochastic, so this is a **range over two runs after** the change against
one run before it. The stable results are the ones that cannot move by chance:
top-3 recall, and the token count, which is a property of the request rather than
of the answer.

| Metric | Before | After (2 runs) | Direction |
|---|---|---|---|
| top-1 accuracy | 0.6875 | **0.7500 – 0.8125** | up |
| top-3 recall | 1.0000 | **1.0000 – 1.0000** | held |
| false-positive rate | 0.1429 | 0.1429 | flat |
| skill top-1 accuracy | 0.5000 | 0.5000 | flat |
| **mean input tokens / call** | **12 949** | **7 807 – 7 871** | **−39%** |
| max input tokens / call | 12 955 | 8 781 – 8 797 | −32% |
| MCP selection input tokens | 978 | 978 | unchanged |

Top-1 is quoted as a range rather than a mean because two runs is too few to
average honestly. Both runs land above the single pre-change run, and top-3 recall
— the metric that did not move in Phase 3 either — is unchanged at a perfect 1.0,
which is the evidence that the narrower evidence set did not lose the answer.

| Request (118-skill tree) | Before | After |
|---|---|---|
| `search_agent_skills` — provider `input_tokens` | ~12 900 | **8 402** |
| `search_target_files` — provider `input_tokens` | ~12 950 | **8 047** |
| `search_agent_skills` — estimated (chars/4) | 16 489 | 6 892 |
| `search_target_files` — estimated (chars/4) | 12 165 | 7 439 |
| candidate reads / call (skills) | 118, **uncapped** | 40 |

Two token figures, and the gap between them is the estimator's error. `limits.
estimate_tokens` is `chars/4`, which the provider's real tokenizer beats by ~20%
on Markdown; the fixture harness reports the provider's own number, so that is
the one to trust. Both are listed rather than picking the flattering one.

## The envelope depends on what the judge picks

Skills envelope, same 118-candidate tree, mock judge:

| `max_matches` | Bytes |
|---|---|
| 5 (default) | 4 142 |
| 1 | 3 819 |

Live, the winning skill was a 2 733-character document and the envelope came back
at **10 963 B**; with `max_matches: 1` it was 7 201 B. The mock picks a different
skill than the live judge, so envelope size is a property of *the answer* as much
as of the shape — the 4 142 figure is a floor for a short winner, not a ceiling.
`scripts/bench_jev.py` pins a 7 422 B envelope on its 250-candidate fixture.

## What changed, and what it cost

**`MAX_PREVIEWED_CANDIDATES` = 40, spent by relevance.** `build_criteria` read
every candidate — up to 250 files at 16 kB each — and then divided
`MAX_TOTAL_CRITERIA_CHARS` by however many turned up, so a large skill tree spent
megabytes of I/O describing files unrelated to the task. The budget now goes to
the 40 candidates `_lexical_score` ranks highest against the task; the rest keep
their path as evidence.

**The property that makes it safe: ranking never removes an option.** A candidate
that loses its preview keeps its path, so a Choice can still be handed it and
still pick it on the name. A task sharing no vocabulary with any filename
degrades to exactly the old "first N" order — pinned by
degrades to the old "first N" order, pinned by the unrelated-task case in
`tests/test_prefilter.py`. This trades *evidence* against
cost, not *options*, and the distinction is the whole safety argument.

**The prefilter is path-only, on purpose.** Reading every file to rank better is
the cost being removed; the path is already in hand before a byte is opened. A
lexical signal is weak next to a real preview, which is why the budget is 40 and
not 10, and why lowering it needs a re-run of this harness.

**Envelope de-duplication.** `primary` was byte-identical to `resources[0]` —
2 893 of 6 957 bytes, 42% of the document. `primary` is now an identity view
(`file`, `name`) and the body appears exactly once. This is a **breaking API
change**: `primary["content"]` is gone, read `resources[0]["content"]`. The plugin
was updated in the same change, and `test_resource_body_appears_exactly_once`
pins the count at 1 where it was pinned at 2.

**`max_matches` exposed and bounded (1–20).** The engine already took the
argument; the tool did not. Each resource carries up to 6 000 characters, so an
unbounded knob was a 1.5 MB envelope waiting to be asked for.

## Under-delivered: the per-turn fixed cost

| | Planned | Actual |
|---|---|---|
| tool schemas + rules block | −38% (~590 tok) | **−8.6% (947 → 865)** |

Trimming `select_mcp_tools`' docstring and the `AGENT_DECISION_RULES` block cut
158 characters, not the ~1 400 projected. Two reasons: the docstring prose is
API contract rather than padding, and my own `search_agent_skills` addition for
`max_matches` put 154 characters back. The projection was wrong about how much of
that text was load-bearing. Recorded rather than quietly dropped — at 865 tokens
per turn it is no longer where the money is.

## Known-red, unchanged by this phase

`tests/test_routing_quality.py::test_skill_routing_is_exact_in_mock_mode` asserts
mock-judge skill top-1 = 1.0 and gets 0.5. It was 0.5 before Phase 2 and 0.5
after: the fixture predates the `.agents/skills` tree growing to 118 candidates,
and the keyword-scoring offline judge degrades as the tree grows. Live skill top-1
is 0.5 on both sides of this phase too. The prefilter is not the cause.

## Reproducing

```bash
# quality + token, live (needs an API key)
python scripts/eval_routing.py --mode live

# the size and timing gates, offline
python scripts/bench_jev.py --assert
```

`MAX_PREVIEWED_CANDIDATES` is a trade, not a free win. Change it and re-run both
harnesses; `scripts/bench_jev.py` enforces the envelope size but knows nothing
about which file the judge picks.

---

# Phase 3 — reliability under concurrency, encoding, and budget

Measured 2026-10-02 · no change to any hot path. Three defects that only appear
under a condition the single-threaded suite never creates, and one budget bug.

| Fix | Failure before | Failure after |
|---|---|---|
| `_Breaker` transitions under a lock | N concurrent half-open probes | exactly 1 |
| `ScanCache.get` re-checks under the lock | `KeyError` out of the tool call | clean miss |
| `load_jev_settings` reads `utf-8-sig` | BOM settings file silently ignored | read normally |
| `MAX_TASK_CHARS` = 32 000 | task truncated mid-sentence | refused by name |

## The breaker could not hold its own invariant

`_Breaker`'s docstring promised that after a cooldown *exactly one* half-open probe
is admitted. Unlocked, it could not: `allow()`'s read of `self.probing` and its
assignment are separate bytecodes, so with concurrent tool calls — which the MCP
runtime serves, and which the plugin provokes with `MAX_CONCURRENT_QUERIES`
messages at once — every thread in that gap saw `False` and was admitted. An
outage was survived by up to 12 simultaneous retries, which is the fan-out the
half-open state exists to prevent.

Pinned by `test_exactly_one_thread_is_admitted_after_a_cooldown`, which sets
`sys.setswitchinterval(1e-6)` to make the window deterministic instead of rare.
The lock is per-breaker and never held across a network call, so it cannot
serialise real work.

One related change: a non-provider exception used to do `breaker.probing = False`
inline. That is now `release_probe()`, which releases the half-open slot without
touching the failure count — the old line reached into another object's state,
which is what made the lock awkward to introduce.

## The cache could raise out of a tool call

`ScanCache.get` ran `signature_fn()` outside the lock (correct — it is I/O), then
wrote `self._entries[key].at = now` back under it. If another thread cleared the
cache in between, that subscript raised `KeyError` — out of the cache, out of the
tool body, and to the caller as an `INTERNAL_ERROR`. Every write now re-checks
membership and re-reads the value under the lock.

## A BOM made a valid settings file invisible

Found during Phase 0, when the same trap bit `doctor.py`: Windows PowerShell's
`Set-Content -Encoding utf8` and its `>` operator both write a BOM, and
`json.loads` rejects one. `load_jev_settings` logged a parse error and moved on,
so the project silently ran on defaults. Now `utf-8-sig`, which is identical to
`utf-8` when there is no BOM. Genuinely malformed JSON is still reported and still
contributes nothing — BOM tolerance must not become silent tolerance.

## A long task was truncated rather than refused

`MAX_INPUT_CHARS` was 100 000 for every string parameter, including `task`. A
100 000-character task is ~25 000 estimated tokens, and the whole state budget is
`MAX_STATE_PLUS_LONGEST_QUESTION_TOKENS` (32 000) *minus* the longest question.
Measured on this repo: a 100k task left **7 000** tokens of state budget against a
criteria set worth ~24 000, so `fit_state` cut the candidate tail first and then
truncated the task mid-sentence — the tool judged a question the caller never
asked.

`task` now has its own cap, `MAX_TASK_CHARS` = 32 000 (~8 000 tokens), four times
`MAX_TASK_FILE_CHARS`, which leaves the budget for evidence. Over it, the refusal
names `task_file`, whose head is read for exactly this case. Other parameters keep
`MAX_INPUT_CHARS`: `task_file` is a path, and its *contents* are bounded by
`MAX_TASK_FILE_CHARS` separately.

---

# Phase 4 — performance: two candidates, both rejected

Measured 2026-10-02 · **no code change shipped.** Both candidates from the plan
were measured, and neither survived contact with a real timing. Recorded because
"we measured it and left it alone" is a result, and because the rejection is
instructive: one of them looked like a 40% win.

## 4a — the `deepcopy` in `fit_state`: rejected, too small

`fit_state` deep-copied the state on every untruncated call. Measured:

| State | `copy.deepcopy` |
|---|---|
| typical (task + goal + counts) | **0.8 µs** |
| 40-criteria | **4.6 µs** |

Against a live call of **388 ms**, that is four thousandths of one percent. Worse,
the copy is load-bearing: `test_fit_state_defensive_copy` asserts that mutating
the returned state cannot reach the caller's dict, and the state does reach the
provider. Removing it to save 0.8 µs would trade a tested guarantee for nothing.

Kept, unchanged.

## 4b — memoizing `_dir_signature`: rejected, broke a contract

`_dir_signature` stats up to 2 000 directories. On this repo that is 240 `os.stat`
calls at ~22 µs each:

| | mock, isolated |
|---|---|
| skill-directory signature | 5.4 ms |
| root signature (ignored dirs) | 1.1 ms |
| warm `search_agent_skills`, memo on | 7.7 ms |
| warm `search_agent_skills`, memo off | 14.2 ms |

A 1-second reuse window looked like **−46% on a warm call**. It was implemented,
tested, and then removed. Two reasons:

**It was 1.4% of wall clock.** Every number above is a mock-path measurement with
no network in it. A live `search_agent_resources` is **388 ms** warm and **715 ms**
cold, dominated by the provider round trip. The signature is 5.4 ms of that. The
plan's "40% of the call" was an artefact of measuring only the part I could time
cheaply — the same trap Phase 7 documents for the bench gates, hit from the other
direction.

**It broke a real contract.** Three existing tests in `test_scan_cache.py` assert
that a directory change invalidates the cache on the *next* call:
`test_adding_a_skill_invalidates_the_cache`,
`test_adding_a_file_invalidates_the_git_cache`,
`test_a_new_file_invalidates_the_walk_cache`. A reuse window makes that false for
up to a second. Trading an immediate invalidation guarantee for 5 ms inside a
388 ms round trip is a bad trade, and the tests were right to object.

**What was kept instead:** `tests/test_dir_signature.py` pins the properties that
made the memo unsafe — bounded depth, the entry cap, and immediate invalidation —
including `test_there_is_no_reuse_window_to_configure`, so the next attempt meets
the objection instead of rediscovering it.

## Why Phase 4 shipped nothing

Both items were on the plan as wins and neither was. The honest summary of the
local hot path after Phases 1–3: `find_agent_resources` warm is ~14 ms against a
388 ms live call, so **~3.6% of the request is local work**. Phase 2 already took
the large win (−39% input tokens); what remains locally is not worth trading a
correctness property for. If this is revisited, the honest framing is "make the
live call faster", not "make the mock benchmark faster".

---

# Phase 5 — simplification: three deletions, one rejection

Measured 2026-10-02 · **−25 lines.** Dead code only; no behaviour change, verified
by comparing every tool's envelope key set against `HEAD` (17 / 13 / 21 keys,
identical).

| Deleted | Was | Evidence |
|---|---|---|
| `limits._stringify_json` | 6 lines | byte-identical to `stringify_state`; 1 occurrence repo-wide |
| `limits.longest_question_tokens` | 8 lines | never called; 1 occurrence repo-wide |
| `policy.min_confidence` | 6 lines | no production caller, only `test_policy.py` |
| `TestMinConfidence` | 9 lines | tested the above, so it goes with it |

Also removed: the now-unused `List` from `policy.py`'s typing import.

**This is 0.5% of the 6 142 lines of engine and script code.** Worth doing because
dead code is a maintenance cost rather than a performance one — nobody can reason
about a second copy of a function that has one caller-less definition — but it is
not a simplification of the design, and it should not be reported as one.

## 5d — collapsing the no-decision envelopes: rejected

The plan proposed routing the two hand-rolled empty envelopes through the existing
`_mcp_result` helper. Measured the actual overlap first:

| Envelope | Keys | Tool-specific |
|---|---|---|
| `find_agent_resources` | 16 | 9 |
| `select_target_files` | 10 | 3 |
| `_mcp_result` | 21 | 14 |

**Seven keys are shared** — `action`, `candidates_truncated`, `confidence`,
`coverage`, `model`, `truncated`, `usage`. A helper returning seven keys would
force each call site to merge the rest, turning one literal into two places and
adding a merge point where a key can be forgotten. The three envelopes are
different *shapes*, and `tests/test_envelope_shapes.py` asserts each tool returns
the same key set across all of its own branches — a property a shared builder
would make harder to see, not easier.

Rejected. The three branches stay explicit, which is also why they are cheap to
read: each one tells you exactly what that tool returns when it decided nothing.

## Where the codebase actually stands

| | Lines |
|---|---|
| engine modules | 4 644 |
| `scripts/` | 1 498 |
| tests | 7 757 |

Tests are **1.26×** the code. For a security-sensitive judge whose whole argument
is that it fails closed on invalid input, that ratio is the feature rather than
the problem — and every phase in this document added to it.

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