# Phase 3 — Give the Model Evidence

**Goal:** stop asking Jev to pick from a list it cannot possibly discriminate, and
stop paying 3× question tokens for one ranking.

**Closes:** findings #7, #8, #9, #10, #11.

**Depends on:** Phase 2 (the new output models must be widened in the same commit).

**Risk:** medium-high. This changes what is sent to the API and therefore what comes
back. Accuracy should improve markedly; input tokens should land near-flat or lower
despite adding candidate text. **Prove both** with the Phase 1 benchmark and a
labelled fixture set.

---

## The core defect

Every `Choice` question currently gives the model one identical description per
option, and the state contains only the user's task text:

```python
# jev_engine.py:757-758  — search_target_files
criteria = {cand: "Candidate workspace file relevant to the task" for cand in candidates}
criteria["none"] = "None of the supplied workspace files is relevant to the task"

# jev_engine.py:554  — search_agent_resources
criteria_map = {opt: f"Agent resource: {Path(opt).name}" for opt in options}
```

For a typical skill tree, `Path(opt).name` is `"SKILL.md"` for **every** option. The
model receives 250 identical descriptions and a task string, then must guess from
bare paths. The reference implementation puts the candidate *text* into the criteria
(`reference/burnigtm-jev-mcp/src/packs/rank.ts:12`):

```ts
criteria[candidate.id] = truncateText(candidate.text, MAX_CANDIDATE_CHARS);
```

with `MAX_CANDIDATE_CHARS = 2000` (`reference/.../limits.ts:9`) and a paired
presence `Noul` (`:23-26`).

**And the questions are triplicated for nothing:**

```python
# jev_engine.py:562-573
if len(options) > 1: questions["secondary"] = Choice(criteria=secondary_map, ...)
if len(options) > 2: questions["tertiary"] = Choice(criteria=tertiary_map, ...)
```

Three parallel `Choice`s over identical 250-option criteria, all seeing the same
state, unable to see each other's answers. Per the TypeSafe guidance: "Ask
independent questions over the same state together" — these are not independent, they
are the *same question asked three times*. The full ranking is already available from
`primary.probabilities` — the code itself uses it to build `ranked`
(`jev_engine.py:626-634`).

Confirmed in the live audit: the real `search_target_files` call reported
`questions: 622` tokens against `state: 74`. **89% of the request was duplicated
criteria text.**

---

## Task 3.1 — Add `MAX_CANDIDATE_CHARS` and a preview extractor

**`limits.py`** — add, next to the existing constants (`:15-23`):

```python
#: Per-candidate evidence included in Choice criteria. Ported from
#: reference/burnigtm-jev-mcp/src/limits.ts:9.
MAX_CANDIDATE_CHARS = 2_000
#: Upper bound on candidates supplied to a tool. Matches MAX_CHOICE_OPTIONS but is
#: a separate knob: this one bounds *our* input, the other bounds the API.
MAX_RANK_CANDIDATES = 5_000
```

**New module `candidates.py`** — candidate discovery and evidence extraction, so
`jev_engine.py` stops growing. Pure functions, no network, fully testable.

```python
"""Candidate discovery and evidence extraction for the Choice questions.

A Choice can only pick from the options it is given, so the *content* of each
option's description is the whole game. This module turns a filesystem path into
a short, self-describing evidence string, and enforces the candidate bounds that
`MAX_CHOICE_OPTIONS` alone does not cover.
"""
```

Required functions:

| Function | Purpose |
|---|---|
| `front_matter_split(text) -> tuple[str, str]` | Split YAML front matter (`---\n…\n---`) from the body. Returns `(front_matter, body)`. |
| `markdown_preview(text, max_chars=MAX_CANDIDATE_CHARS) -> str` | Drop front matter, strip HTML comments, collapse blank-line runs, strip markdown syntax noise (leading `#`, `>` markers, list bullets kept, table pipes), then `truncate_text`. |
| `skill_description(text) -> str` | For a `SKILL.md`, prefer the `description:` field from front matter; fall back to the first prose paragraph. This is the single highest-value field and is nearly free. |
| `build_criteria(paths, read_text, max_chars) -> dict[str, str]` | `path -> preview`. `read_text` is injected so tests need no filesystem. |
| `bound_candidates(candidates, limit) -> tuple[list, bool]` | Returns `(kept, truncated)`. |

`build_criteria` must **never raise** — an unreadable file yields a
`"<unreadable>"` placeholder, matching the existing `except Exception: content = ""`
at `jev_engine.py:613-615`.

### Front matter is the highest-leverage detail

`.agents/skills/*/SKILL.md` files carry a `description:` field written specifically
to tell an agent when the skill applies. That is exactly the signal Jev needs. Extract
it rather than feeding the whole file.

---

## Task 3.2 — `find_agent_resources`: real evidence, one question

**`jev_engine.py:527-669`**, rework the question construction.

### 3.2a — Criteria carry previews

```python
from candidates import build_criteria, bound_candidates

options_all = list(candidate_files.keys())
options, candidates_truncated = bound_candidates(options_all, MAX_CHOICE_OPTIONS)

criteria_map = build_criteria(options, _read_candidate_preview)
if candidates_truncated:
    log_event("candidates_truncated", tool="find_agent_resources",
              considered=len(options_all), kept=len(options))

questions = {
    "primary": Choice(
        criteria=criteria_map,
        instructions=(
            "Select the single agent skill, workflow, or memory document that is "
            "most directly relevant to the task described in `task`. Each option's "
            "description is that document's own summary. Judge relevance from what "
            "the document covers, not from its filename. If no supplied document "
            "addresses the task, choose the 'none' option."
        ),
    ),
}
```

Add a `none` option unconditionally, matching `search_target_files`
(`:758`) — the skill guidance is explicit: *"Include a no-match outcome when nothing
may fit."*

State should be structured rather than a prose prefix, so paths can be referenced:

```python
state = {
    "task": task,
    "goal": "Identify which specific Markdown agent resources are directly relevant.",
    "candidates_considered": len(options_all),
    "candidates_evaluated": len(options),
    "candidates_truncated": candidates_truncated,
}
res, fitted, cfg = _request(state, questions)
```

### 3.2b — Drop `secondary` / `tertiary`

Delete `:562-573` and `:592-595`, and rewrite `ranked` (`:626-634`) to source from
`primary` alone:

```python
ranked_map: Dict[str, float] = {}
for opt, p in _answer_probs(res, "primary").items():
    if opt in candidate_files and opt != "none":
        ranked_map[opt] = p
```

Top-3 by probability is exactly what `secondary`/`tertiary` were trying to produce,
and `ranked` is more faithful because it uses the full distribution rather than three
independent argmaxes. `max_matches` (default 5) still caps `resources`.

### 3.2c — Floor the family-prefix clustering

Current code (`jev_engine.py:597-604`) lets a 0.15-probability primary claim every
slot:

```python
if primary_val:
    primary_name = Path(primary_val).parent.name
    if "-" in primary_name:
        family_prefix = primary_name.rsplit("-", 1)[0] + "-"
        for opt in options:
            ... if opt_parent.startswith(family_prefix): selected_keys.append(opt)
```

Gate it on confidence and cap it:

```python
FAMILY_CLUSTER_MIN_PROB = 0.50
FAMILY_CLUSTER_MAX_SIBLINGS = 2

if primary_val and primary_prob >= FAMILY_CLUSTER_MIN_PROB:
    primary_name = Path(primary_val).parent.name
    if "-" in primary_name:
        family_prefix = primary_name.rsplit("-", 1)[0] + "-"
        siblings = sorted(
            (o for o in options
             if o != primary_val and o not in selected_keys
             and Path(o).parent.name.startswith(family_prefix)),
            key=lambda o: _answer_probs(res, "primary").get(o, 0.0),
            reverse=True,
        )
        for opt in siblings[:FAMILY_CLUSTER_MAX_SIBLINGS]:
            selected_keys.append(opt)
```

Note the behavioral improvement: siblings are now ordered by the model's own
probability rather than filesystem order, and at most 2 are added instead of
unbounded.

### 3.2d — Surface candidate truncation

Add to the result (`:653-667`) and to `coverage`:

```python
"candidates_considered": len(options_all),
"candidates_evaluated": len(options),
"candidates_truncated": candidates_truncated,
```

and force non-`auto` when truncated, reusing the existing policy primitive
(`policy.py:95-97`):

```python
action = require_complete_context(action, fitted["truncated"] or candidates_truncated)
if candidates_truncated:
    reason_codes.append("candidates_truncated")
```

This is finding #9: today a relevant skill at position 251 is invisible with **no
signal at all**. After this change it is reported and the action degrades to
`review`. The reference solves the same problem more thoroughly with chunk-and-rerank
(`reference/.../tools/rank.ts:115-133`) — out of scope, but this is the
safety-relevant half.

Also extend `coverage` with the reference's `candidate_fields` shape
(`reference/.../tools/output-schemas.ts:108-110`):

```python
coverage["candidate_fields"] = {
    "complete": not candidates_truncated,
    "candidates_considered": len(options_all),
    "original_chars": sum(preview_lengths),
    "evaluated_chars": sum(len(v) for v in criteria_map.values()),
}
```

Widen the Phase 2 output model accordingly.

### 3.2e — Read each file once

Currently every selected file is read a second time at `:608-622` after the
previews were built for the criteria. Build a `preview_cache: dict[Path, str]` in
3.2a and reuse it — the content is byte-identical, so the second read is pure waste
(5 × 6000 chars per call).

Careful: `MAX_CONTENT_CHARS` is 6000 and `MAX_CANDIDATE_CHARS` is 2000. Cache the
**full** text once, derive the 2000-char preview from it, and slice 6000 for the
result. That keeps both honest with one read.

---

## Task 3.3 — `select_target_files`: previews + a real presence judgment

**`jev_engine.py:722-799`.**

### 3.3a — Criteria carry path + head of file

```python
criteria = {}
for cand in candidates:
    preview = _read_candidate_preview(cand)
    criteria[cand] = (
        f"{cand}\n---\n{preview}" if preview else cand
    )
criteria["none"] = "None of the supplied workspace files must be inspected or edited for this task."
```

Leading with the path keeps the signal the model already relied on; the preview adds
what the file actually contains. Do not drop the path — for source files the path
carries a lot (module name, layer, convention).

**Skip binary-ish and generated files' previews** even though their extension passed
the filter: if a preview is empty or contains a high proportion of NUL, fall back to
the path alone rather than sending noise.

### 3.3b — Add the presence Noul

Replace the `none`-only mechanism with the reference's pattern
(`reference/.../packs/rank.ts:19-26`):

```python
questions = {
    "target_file": Choice(
        criteria=criteria,
        instructions=(
            "Select the single workspace file that must be inspected or edited to "
            "perform the task described in `task`. Each option's description is that "
            "file's path followed by its opening content. Choose 'none' if no supplied "
            "file is relevant."
        ),
    ),
    "is_relevant": Noul(
        instructions=(
            "Does at least one of the supplied workspace files actually have to be "
            "inspected or edited to perform the task, or is the top-ranked file a "
            "forced winner among files that are all poor matches?"
        ),
        criteria={
            "true": "At least one supplied file is genuinely required for the task",
            "false": "No supplied file is genuinely required; the top choice is a forced winner",
        },
    ),
}
```

One request, two independent questions, run in parallel over the same state — exactly
what the guidance recommends. The `Noul` is what finding #8 is about: with only a
`Choice`, a distribution concentrated on an irrelevant file still reads as `matched:
true`. The `Noul` is independently useful here because it should also drive the
`exists` verdict.

### 3.3c — Derive the verdict from the Noul

```python
target_ans = get_answer(res, "target_file")
chosen = get_val(target_ans)
probs = _answer_probs(res, "target_file")
conf = _slot_confidence(target_ans)

relevance_prob = round(float(get_prob(get_answer(res, "is_relevant"))), 4)

escaped = (not chosen) or chosen == "none" or relevance_prob < NONE_CONFIDENCE
```

with `NONE_CONFIDENCE = 0.5` module-level. Then:

```python
def _exists_verdict(chosen, confidence, relevance_prob) -> str:
    """What the model actually established about workspace relevance."""
    if chosen and chosen != "none":
        return "answered" if relevance_prob >= NONE_CONFIDENCE else "partial"
    return "absent" if confidence >= 0.35 else "partial"
```

So a confident file choice paired with a low presence Noul reports `partial`, not
`answered`. That is the fail-closed direction: the caller is told the answer is weak
rather than being handed a file that is probably irrelevant.

Keep `"absent"` only for a genuine `none` **with** a low presence probability, and
`"no_candidates"` for the zero-candidate path (Phase 2).

### 3.3d — Don't read 250 files

`select_target_files` currently reads nothing per candidate, which is why adding
previews here costs 250 file reads per call. Mitigations, in order:

1. **Bound the reads.** Only build previews for the first `MAX_PREVIEW_READS = 120`
   candidates, and for the rest use the path alone. Report
   `previews_built` / `previews_skipped` in `coverage.candidate_fields`.
2. **Cap total preview bytes** at `MAX_TOTAL_PREVIEW_CHARS = 40_000`, allocating
   round-robin so early candidates do not starve later ones.
3. **Reuse a single directory listing.** Read via one `os.scandir` pass where
   possible instead of 250 `open()` calls.

Better still, and the real fix: **use a deterministic lexical prefilter** to cut 250
candidates to ~40 *before* any read, then build previews only for those. A ~30-line
token-overlap scorer is enough and costs no API call. This is listed as an option in
the plan; if Phase 5's `scan_cache` lands first it makes the prefilter much cheaper to
justify. **Decision: implement the read bound + byte cap now (cheap, safe); treat the
prefilter as a Phase 5 follow-up** so Phase 3 stays reviewable.

---

## Task 3.4 — `select_model_tier`

Leave the questions alone. The tier question is 3 options with meaningful
descriptions (`jev_engine.py:805-809`) and a task-only state — that is already a well-formed
judgement. Only widen the Phase 2 output model if needed.

---

## Proving the change worked

### Fixture set

**New file:** `tests/fixtures/routing_tasks.json` — 20 labelled cases, each:

```json
{
  "task": "add a dark mode toggle to the settings screen",
  "expected_file": "src/ui/settings_screen.tsx",
  "expected_skill": ".agents/skills/theme-toggles/SKILL.md"
}
```

Cover the shapes that break guessing: a file whose name is uninformative
(`src/handler.py` for a bug fix), a skill directory with a misleading name, a task
matching nothing in the tree, and a task matching two families.

### Metrics to record

Create `scripts/eval_routing.py` (mock **and** live modes):

| Metric | Definition | Expectation |
|---|---|---|
| top-1 accuracy | `files[0] == expected_file` | materially up from the Phase 1 baseline |
| top-3 recall | `expected_file in ranked[:3]` | up |
| false-positive rate | `matched: true` when `expected_file is null` | **down** (this is the presence Noul working) |
| input tokens/call | `usage.input_tokens` | flat or lower |
| request count | Jev calls per tool call | **1** (unchanged) |

Run it before and after. Paste both tables into `docs/perf-baseline.md`.

```powershell
$env:JEV_MCP_MOCK="1"; & .\.venv\Scripts\python.exe scripts\eval_routing.py --mode mock
Remove-Item Env:\JEV_MCP_MOCK
& .\.venv\Scripts\python.exe scripts\eval_routing.py --mode live
& .\.venv\Scripts\python.exe scripts\bench_jev.py --assert
```

---

## Out of scope

- **Chunk-and-rerank for >250 candidates** (`reference/.../tools/rank.ts:115-133`).
  This phase makes truncation visible and non-`auto`; chunking costs multiple API
  calls. Revisit only if the fixture set shows >250-candidate repos are common.
- **Embedding or BM25 retrieval.** Jev is the ranking primitive; a lexical prefilter
  (3.3d) is the only pre-Jev stage, and only if read cost becomes a problem.
- **Removing `policy.ESCAPE_HATCHES`.** `ask_user`/`investigate` are still unused
  (finding #21) but the `none` escape hatch this phase relies on is the same idea.
  Cleanup is Phase 7.

---

## Files touched

| File | Change |
|---|---|
| `candidates.py` | **new** — previews, front matter, bounds, `build_criteria` |
| `limits.py` | `MAX_CANDIDATE_CHARS`, `MAX_RANK_CANDIDATES` |
| `jev_engine.py` | drop secondary/tertiary; preview criteria; presence Noul; cluster floor; truncation reporting; read cache; `_exists_verdict` signature |
| `mock.py` | handle the new `is_relevant` Noul; score previews |
| `policy.py` | `NONE_CONFIDENCE`, `FAMILY_CLUSTER_*` |
| `tests/fixtures/routing_tasks.json` | **new** — 20 labelled cases |
| `scripts/eval_routing.py` | **new** — accuracy/token harness |
| `tests/test_candidates.py` | **new** |
| `tests/test_mock_tools.py` | update `exists` expectations; add presence-Noul cases |
| `tests/test_hardening_integration.py` | rewrite `test_family_prefix_sibling_matching` to actually assert clustering |
| `docs/perf-baseline.md` | before/after accuracy + token tables |

## Verification

```powershell
cd D:\mcp\jev-typesafe-mcp
& .\.venv\Scripts\python.exe -m py_compile candidates.py jev_engine.py mock.py policy.py
& .\.venv\Scripts\python.exe -m pytest tests -q
& .\.venv\Scripts\python.exe scripts\bench_jev.py --assert
$env:JEV_MCP_MOCK="1"; & .\.venv\Scripts\python.exe scripts\eval_routing.py --mode mock
& .\.venv\Scripts\python.exe scripts\eval_routing.py --mode live
& .\.venv\Scripts\python.exe scripts\diag_mcp.py --tool search_agent_skills --task "fix ui bug" --root_dir . --mock
& .\.venv\Scripts\python.exe scripts\diag_mcp.py --tool search_target_files --task "where is the retry policy" --root_dir . --mock
node tests/test_plugin.mjs
```

## Definition of done

- [x] `Choice` criteria contain candidate text, not just basenames
- [x] `SKILL.md` front-matter `description` is used when present
- [x] `secondary`/`tertiary` removed; `ranked` derived from `primary` alone
- [x] `search_target_files` sends an `is_relevant` Noul alongside the Choice
- [x] A confident Choice + low presence Noul yields `exists: "partial"`
- [x] `candidates_truncated` is reported and forces `action != "auto"`
- [x] Family clustering requires `primary_prob >= 0.5` and adds at most 2 siblings
- [x] Each selected file is read exactly once
- [ ] `input_tokens` per call is flat or lower vs the Phase 1 baseline — **not
      met, and not reachable**: the old request sent 47 bare paths and no content.
      Measured 878 → 6 731 tokens per call, now bounded by `MAX_PREVIEW_READS`
      (120) and `MAX_TOTAL_PREVIEW_CHARS` (40 000). Halving the preview budget to
      24 000 costs 11 points of top-1 (0.778 → 0.667); see `docs/perf-baseline.md`.
- [~] top-1 accuracy **0.444 → 0.667–0.833** and top-3 recall **0.778 → 1.000**
      (3/3 live runs). The false-positive rate is flat at 0.00–0.14, because the
      old `none` description already handled the obvious no-match cases; what the
      presence Noul measurably changes is that a confident Choice with weak
      presence now reports `exists: "partial"` (0.00 → 0.08–0.16 of all cases)
- [x] `pytest`, `bench_jev.py --assert`, `node tests/test_plugin.mjs` all green

## Commit

```
perf(engine): give the Choice questions real candidate evidence

The Choice criteria carried one identical description per option -- for
search_target_files literally "Candidate workspace file relevant to the task"
repeated 250 times, and for search_agent_skills just Path(opt).name, which is
"SKILL.md" for every skill in the tree. The state contained only the task text.
The model was asked to pick from a list it had no way to discriminate.

Port the reference rank recipe (packs/rank.ts):
- new candidates.py builds a short self-describing preview per candidate,
  preferring the SKILL.md front-matter description and falling back to the
  first prose paragraph, bounded by MAX_CANDIDATE_CHARS=2000
- search_target_files gains a paired is_relevant Noul so a forced winner among
  poor options can no longer read as matched:true; exists becomes "partial"
  when the Choice is confident but presence is not
- drop the secondary/tertiary questions. Three parallel Choices over identical
  250-option criteria tripled question tokens for one ranking that
  primary.probabilities already returns. A live call showed 622 of 696 tokens
  were duplicated criteria.
- candidates past MAX_CHOICE_OPTIONS were dropped with no signal at all; report
  candidates_considered/evaluated/truncated and degrade the action to review
- family-prefix clustering now needs primary_prob >= 0.5 and adds at most 2
  siblings, ordered by the model's own probability instead of filesystem order
- each selected file is read once and reused for both the criteria preview and
  the returned content

Adds tests/fixtures/routing_tasks.json and scripts/eval_routing.py to measure
top-1 accuracy, false-positive rate and input tokens before and after.
```

## Post-commit manual check

The plugin injects `[Active Capability / Skill: <path>]` from `resources[0]`. After
this change, send a real message in opencode and confirm from
`~/.config/opencode/logs/jev-plugin.log` and `<repo>/logs/jev_engine.log` that:

- the selected skill is genuinely relevant (not a sibling from a mis-fired cluster),
- `input_tokens` did not jump,
- the injected block is the same shape as before (`\n\n[Active Capability / Skill: …]`).
