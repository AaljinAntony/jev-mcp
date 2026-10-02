# Architecture

Internal reference for `jev-engine`: how the modules fit together, what each
bound and invariant is for, and what the test suite pins. For installation and
day-to-day use see the [README](../README.md); for measurements see
[perf-baseline.md](perf-baseline.md).

- [Module map](#module-map)
- [Decision flow](#decision-flow)
- [Envelope and typed errors](#envelope-and-typed-errors)
- [Response validation invariants](#response-validation-invariants)
- [Policy: confidence and action](#policy-confidence-and-action)
- [Bounds](#bounds)
- [The `root_dir` allowlist](#the-root_dir-allowlist)
- [Deadline and the circuit breaker](#deadline-and-the-circuit-breaker)
- [Settings resolution](#settings-resolution)
- [Candidate discovery and the scan cache](#candidate-discovery-and-the-scan-cache)
- [The OpenCode plugin](#the-opencode-plugin)
- [Offline mock mode](#offline-mock-mode)
- [Logging and redaction](#logging-and-redaction)
- [CLI and scripts](#cli-and-scripts)
- [Test suite](#test-suite)
- [Failure modes to avoid](#failure-modes-to-avoid)

---

## Module map

No package, no `pyproject.toml` — the modules are flat at the repo root and
`sys.path` is set from `Path(__file__).resolve().parent` in the two entry points.

| Module | Lines | Role |
|---|---|---|
| `jev_mcp.py` | 193 | The MCP server. Registers the 5 tools, wraps every body in `_run`, maps exceptions to a JSON error envelope, logs `server_start` / `server_stop`. |
| `jev_engine.py` | 2 354 | The engine: settings, discovery, client/retry/breaker, the 5 tool bodies, the CLI. |
| `config.py` | 216 | Env parsing and validation, cached in a frozen `JevConfig`. `.env` loaded as a fallback only. |
| `policy.py` | 168 | Pure decision arithmetic: confidence → action, threshold validation, combining. |
| `jev_validation.py` | 225 | Fail-closed validation of a `system_one` response, before any policy number is read. |
| `jev_errors.py` | 171 | Exception taxonomy and `error_details()` → `{code, message, retryable}`. |
| `limits.py` | 220 | Every size cap constant, token estimation, truncation, budget fitting. |
| `candidates.py` | 232 | Turns a file path into short evidence for a `Choice` option. |
| `scan_cache.py` | 84 | mtime/signature-keyed + TTL cache for the two filesystem scanners. Caches **paths only**. |
| `mock.py` | 322 | Deterministic offline judge for `JEV_MCP_MOCK=1`; returns real SDK answer objects. |
| `jev_logging.py` | 167 | Rotating-file + stderr JSON-lines logging, credential redaction. |

Key entry points:

| Symbol | Location |
|---|---|
| `SERVER_NAME = "jev-engine"` | `jev_engine.py:77` |
| `mcp = FastMCP(SERVER_NAME)` | `jev_mcp.py:31` |
| `_run()` — the envelope wrapper | `jev_mcp.py:53` |
| `execute_system_one` | `jev_engine.py:851` |
| `_request` — the single network seam | `jev_engine.py:1063` |
| `_allowed_roots` | `jev_engine.py:217` |
| `_reject_system_dir` — the second gate | `jev_engine.py:267` |
| `_validate_root_dir` | `jev_engine.py:325` |
| `load_jev_settings` | `jev_engine.py:462` |
| `_Breaker` | `jev_engine.py:760` |
| `_cli_dispatch` | `jev_engine.py:2365` |
| `error_details` | `jev_errors.py:90` |
| `validate_response` | `jev_validation.py:180` |
| `confidence_from_probabilities` | `policy.py:98` |
| `action_from_confidence` | `policy.py:118` |
| `fit_state` | `limits.py:131` |
| `build_criteria` | `candidates.py:133` |
| `mock_system_one` | `mock.py:269` |
| `_redact` | `jev_logging.py:47` |

### Call graph

```
MCP client (stdio JSON-RPC 2.0)
   │
   ▼
jev_mcp.py ── MCPServer("jev-engine") ── 5 tools
   │   _run(): body → _assert_finite_json → log → dict
   │           on exception → {"error": {...}} → JevToolError
   ▼
jev_engine.py
   ├─ candidates.py    candidate discovery + evidence previews
   ├─ limits.py        fit_state() token budget
   ├─ jev_validation.py validate_response() fail-closed
   ├─ policy.py        confidence → action
   ├─ _Breaker         fail fast while the provider is down
   └─ _request ──────► TypeSafeClient.system_one(state, questions)
                       or mock.system_one() when JEV_MCP_MOCK=1
```

Transport is **stdio only** — `mcp.run()` is called with no arguments
(`jev_mcp.py:186`), so the SDK default applies. Newline-delimited JSON-RPC 2.0.

---

## Decision flow

1. A tool receives a `task` / `command` / roster.
2. For the workspace tools, `candidates.py` turns each candidate path into short
   evidence — a `SKILL.md` front-matter `description` where present, else the
   head of the file — bounded by `MAX_CANDIDATE_CHARS` and the total preview
   budget. A `Choice` can only pick from the options it is given; this evidence
   is what makes them distinguishable. `select_mcp_tools` takes its evidence from
   the caller (server name, purpose, tool roster) and bounds it the same way.
3. `fit_state` fits `state` to the token budget, then **one** request per tool
   call goes out regardless of how many questions ride along, using `Noul` /
   `Choice` / `Score`.
4. `validate_response` verifies the response against the questions **before any
   policy number is read**. Malformed or self-contradictory answers raise
   `JevResponseError` and are never read as `safe: true`.
5. `policy.py` maps probabilities to `confidence` and an `action`.
6. `_assert_finite_json` rejects any `NaN` / `inf` in the result before it is
   serialized.
7. A JSON-serializable dict is returned.

### The `none` escape hatch

Every `Choice` is offered a `none` option. It scores 0 in the mock and is
boosted to win only when nothing else reaches `EVIDENCE_MIN_SCORE`. This is the
mechanism by which "nothing in this workspace is relevant" becomes a typed
answer rather than a confident wrong pick.

`ask_user` and `investigate` escape hatches are **deliberately unimplemented**
(`policy.py:73-81`); only `none` is offered.

---

## Envelope and typed errors

Every live call adds `model` and `usage: {input_tokens, output_tokens}`. Tools
that make a decision add `action`, `confidence`, and where relevant `ranked` and
`truncated`. On failure the client sees MCP `isError: true` whose text is the
JSON envelope (the MCP runtime prefixes `Error executing tool <name>: `):

```json
{ "error": { "code": "INVALID_RESPONSE", "message": "...", "retryable": false } }
```

`error_details()` (`jev_errors.py:90`) always returns exactly `{code, message, retryable}`.

| Code | Retryable | Trigger |
|---|---|---|
| `INPUT_TOO_LARGE` | false | `JevBudgetError` — state/questions exceed the context budget |
| `INVALID_INPUT` | false | `JevValidationError` — bad thresholds, oversized argument, `root_dir` outside the allowlist, bad `max_tools`/`max_servers`, unusable `mcps` roster |
| `CONFIG_ERROR` | false | `JevConfigError` — missing key, invalid env value, `review_at > auto_accept` |
| `INVALID_RESPONSE` | false | `JevResponseError` / `TypeSafeAPIResponseValidationError` |
| `TIMEOUT` | **true** | `JevTimeoutError` / `TypeSafeAPITimeoutError`, **and** an open circuit |
| `CANCELLED` | false | `JevCancelledError` (mapped; not currently raised by any tool) |
| `AUTH_ERROR` | false | `TypeSafeAuthenticationError` |
| `FORBIDDEN` | false | `TypeSafePermissionDeniedError` |
| `RATE_LIMITED` | **true** | `TypeSafeRateLimitError` |
| `API_ERROR` | status-dependent | 404 → false; 408/429/5xx and connection errors → true; generic → false |
| `INTERNAL_ERROR` | false | anything unmapped |

`RETRYABLE_STATUSES = {408, 429}` (`jev_errors.py:33`).

Provider response bodies and request metadata are **never** relayed. A
`field_path` and an HTTP status are all that survive — an SDK response-validation
error reports only `field_path`, never the URL or request id.

The key set of every deciding tool is identical across all of its branches. That
is a pinned table test (`tests/test_envelope_shapes.py`), including all five
`select_mcp_tools` verdicts — so a caller can branch on `result["exists"]`
without a `KeyError` on the path it did not expect.

---

## Response validation invariants

`jev_validation.validate_response(raw, questions)`:

- answer keys equal question keys exactly;
- `answers` and non-empty `questions` required;
- `model` non-empty; `usage` present with non-negative tokens;
- each answer's `type` matches its question's and is one of `noul | choice | score`;
- `noul` ∈ [0, 1];
- `choice` ∈ criteria, probabilities cover exactly the criteria, each ∈ [0, 1],
  summing to 1 ± `PROBABILITY_SUM_TOLERANCE`, and **the selected choice must be
  the argmax**;
- `score` ∈ [0, n−1], probabilities and legend keyed by levels `0..n−1`, and the
  score agrees with the distribution mean within `score_mean_tolerance(n)`.

`_assert_finite_json` then rejects any `NaN` / `inf` anywhere in the result. It is
the last step in `_run`, so an invalid result can never be serialized as a good
one. `bool` is refused where a number is expected (`tests/test_validation_nan.py`).

The score-mean tolerance is level-count dependent: 7 levels and 2 levels do not
share one epsilon.

---

## Policy: confidence and action

`confidence_from_probabilities` (`policy.py:98`) normalises a distribution over
`n` options as `(top − 1/n) / (1 − 1/n)`: 0 when uniform or empty, 1.0 for a
single option at ≥ 1.0. `_slot_confidence` (`jev_engine.py:981`) prefers the
provider's own `confidence` field, falls back to that formula, then to
`max(p, 1−p)`.

`action_from_confidence` (`policy.py:118`):

```
confidence >= auto_accept (0.8)  ->  "auto"
confidence >= review_at  (0.5)  ->  "review"
otherwise                         ->  "escalate"
```

Thresholds must satisfy `0 <= review_at <= auto_accept <= 1`; a violation fails
at config load with `CONFIG_ERROR` rather than silently mis-routing.

Fail-closed modifiers:

| Function | Effect |
|---|---|
| `worst_action` (`policy.py:134`) | combining independent judgments never softens the strictest |
| `require_complete_context` (`policy.py:144`) | truncated input downgrades `auto` → `review`; truncation includes context truncation, candidate truncation, and `select_mcp_tools` tool-ceiling overflow |
| `guardrail_safe` (`policy.py:157`) | `safe` is `auto` **and** both risk Nouls < `DEFAULT_RISK_THRESHOLD` (0.20) |
| `_risk_action` (`jev_engine.py:1001`) | a risk Noul ≥ 0.50 is `escalate`, ≥ 0.20 is `review`, otherwise the normal map on `max(p, 1−p)` |

Numeric constants:

| Constant | Value | Meaning |
|---|---|---|
| `DEFAULT_RISK_THRESHOLD` | 0.20 | below this a risk Noul is not raised |
| `DEFAULT_ESCALATE_THRESHOLD` | 0.50 | at or above this a risk Noul escalates |
| `NONE_CONFIDENCE` | 0.50 | presence probability at/above which a winner counts as a real match |
| `FAMILY_CLUSTER_MIN_PROB` | 0.50 | sibling-prefix clustering needs a confident primary |
| `FAMILY_CLUSTER_MAX_SIBLINGS` | 2 | clustering cap |
| `MCP_TOOL_MIN_PROB` | 0.12 | floor for a tool to be returned |
| `MCP_CONTENDER_MIN_PROB` | 0.15 | floor for a server to count as a contender |
| `AMBIGUITY_GAP` | 0.10 | gap between top-two server probabilities that reads as ambiguous |
| `PROBABILITY_SUM_TOLERANCE` | 0.01 + 1e-12 | validation |
| `RESOURCE_SIBLING_MIN_PROB` | 0.12 | sibling expansion (`jev_engine.py:1127`) |
| `EVIDENCE_MIN_SCORE` | 0.5 | mock judge: below this `none` wins |

`select_mcp_tools` decides `ambiguous` from the **gap** between the top two
server probabilities (`< AMBIGUITY_GAP`) or `is_decisive < 0.50`, deliberately
not from confidence: a tight 0.46/0.42 split normalises to *high* confidence while
a flat five-way split normalises to a low one. `ambiguous` forces `action` to at
least `review` (`jev_engine.py:2202`).

---

## Bounds

`limits.py` owns every cap:

| Constant | Value |
|---|---|
| `MAX_TOTAL_TOKENS` | 64 000 |
| `MAX_STATE_PLUS_LONGEST_QUESTION_TOKENS` | 32 000 |
| `MAX_CHOICE_OPTIONS` | 250 |
| `MAX_CONTENT_CHARS` | 6 000 |
| `MAX_CANDIDATE_CHARS` | 2 000 |
| `MAX_RANK_CANDIDATES` | 5 000 |
| `MAX_TOTAL_CRITERIA_CHARS` | 96 000 |
| `MAX_PREVIEW_READS` | 120 |
| `MAX_TOTAL_PREVIEW_CHARS` | 40 000 |
| `MAX_PREVIEW_READ_CHARS` | 16 000 |
| `MAX_DISCOVERED_FILES` | 5 000 |
| `MAX_MCP_SERVERS` | 64 |
| `MAX_MCP_TOOL_OPTIONS` | 250 |
| `MAX_MCP_TOOL_DESC_CHARS` | 300 |
| `MAX_MCP_SERVER_DESC_CHARS` | 1 500 |

In `jev_engine.py`: `MAX_INPUT_CHARS` = 100 000, `MAX_TASK_CHARS` = 32 000,
`MAX_TASK_FILE_CHARS` = 8 000, `MAX_MAX_MCP_TOOLS` = 50,
`MAX_MAX_MCP_SERVERS` = 20, `MAX_MAX_MATCHES` = 20.

`estimate_tokens` is `ceil(ascii/4 + non_ascii)`. `fit_state` truncates the
state to fit and returns `{state, truncated, coverage}`; questions that alone
exceed the budget raise `JevBudgetError` → `INPUT_TOO_LARGE`. Truncation uses a
binary search and never splits a surrogate pair.

**`task` has a lower cap than the other parameters, on purpose.** `fit_state`
truncates the *state* from the right, and `task` is the largest key in it: at the
generic 100 000-character limit a task is ~25 000 estimated tokens against a state
budget of 32 000 minus the longest question, so the truncation landed inside the
task and the tool judged a question the caller never asked. `MAX_TASK_CHARS` =
32 000 keeps that budget for evidence, and the refusal names `task_file`, whose
head is read head-only for exactly this case.

---

## The `root_dir` allowlist

`root_dir` and `task_file` are LLM-supplied, so they are untrusted input. The
rule is an **allowlist**, because a denylist cannot enumerate every sensitive
path: a supplied path should carry no more privilege than the session's own
working directory.

`_allowed_roots()` (`jev_engine.py:217`) offers:

- the process CWD;
- the CWD's **ancestors, with two stop conditions**;
- anything under `JEV_MCP_ALLOWED_ROOTS`.

Ancestors are included deliberately — hosts launch the server with `cwd` set
below the project root, and a session legitimately asks about a parent of that.
The walk stops at:

| Stops before | Why |
|---|---|
| the filesystem root | `_is_within` uses `relative_to`, so a base of `/` makes **every** absolute path on the machine a member. Including it turns the allowlist into no allowlist: `/etc` and `/proc` pass. Same bug on Windows, scoped to a volume, where `D:\` was a base. |
| `$HOME` and above | the ordinary checkout is `~/code/project`, which makes the whole home directory an ancestor of the CWD. A prompt-injected `root_dir=$HOME` would otherwise walk it. Ancestors *strictly below* `$HOME` stay allowed, so `~/code/repo` can still be addressed as `~/code`. |

The CWD itself is always allowed, even when it is `$HOME`: that is the one
directory the session already has.

Containment is `Path.relative_to`, never `startswith`, so `<root>-evil` and a
symlink pointing outside the root are both refused — `Path.resolve()` follows
the link before the comparison.

`_reject_system_dir` (`jev_engine.py:267`) is a **second gate**: it vetoes
system trees by name so an explicit `JEV_MCP_ALLOWED_ROOTS` entry cannot hand
over the machine's configuration, and so the failure keeps a specific wording.

| Platform | Vetoed |
|---|---|
| Windows | `C:\Windows`, a drive root, and anything under `SystemRoot`, `windir`, `ProgramFiles`, `ProgramFiles(x86)` |
| POSIX | `/etc`, `/proc`, `/sys`, `/dev`, `/var`, `/opt`, `/srv`, `/root` |

`/home` and `/Users` are deliberately **not** vetoed. They are not system
trees, they are where work lives, and the server's own CWD is usually under one
— vetoing them would refuse the repo itself. `$HOME` is handled by the stop
condition above instead. `/tmp` is not vetoed either, because hosts
legitimately launch with `cwd` set to a temporary directory.

Pinned by `tests/test_root_dir_allowlist.py`, whose POSIX block is behind
`skipif(os.name == "nt")`. A Windows-only development loop never runs it, which
is how the `/`-as-a-base bug survived here.

---

## Deadline and the circuit breaker

`JEV_MCP_TIMEOUT_MS` (30 s default) bounds the **whole** call, not one HTTP
attempt. `_request` (`jev_engine.py:1015`) starts a clock, fits the state, passes
the remaining budget as the per-attempt timeout, and re-checks the clock after
the call. The SDK `RetryPolicy` is `max_retries=2`, `backoff_initial=0.5`,
`backoff_max=5.0`, `backoff_jitter=0.25`, `timeout=<total budget>`,
`api_timeout_error=False` (`jev_engine.py:687`) — a retry can never outlive the
deadline, and timeouts are not retried because a retry only burns budget.

Mock mode enforces the same deadline even though it is CPU-bound.

`_Breaker` (`jev_engine.py:712`) is dependency-free, process-wide, and rebuilt
when the three knobs change:

- opens after `JEV_MCP_BREAKER_THRESHOLD` **consecutive** failures; a success
  resets the counter and closes it;
- after the cooldown exactly **one** half-open probe is admitted; concurrent
  callers are rejected meanwhile. Every transition is under a per-breaker lock,
  which is what makes that hold: the MCP runtime serves concurrent tool calls and
  the plugin runs several messages at once, so without it every caller in the
  read-then-write gap saw `probing == False` and was admitted. The lock is never
  held across a network call, so it cannot serialise real work
  (`tests/test_concurrency_and_encoding.py`);
- cooldown is `JEV_MCP_BREAKER_COOLDOWN_S` for retryable failures (5xx/408/429/
  connection) and the longer `JEV_MCP_AUTH_COOLDOWN_S` for 401/403, because a bad
  key does not fix itself in 30 seconds. A malformed response body is recorded as
  *retryable* so it does not extend the auth window (`jev_engine.py:850`);
- a local programming error clears `probing` and re-raises without opening the
  circuit (`jev_engine.py:863`);
- while open, tools fail immediately with a retryable `TIMEOUT` envelope that
  states when to come back.

---

## Settings resolution

`load_jev_settings` (`jev_engine.py:414`) merges these, in order:

1. `<cwd>/jevs_settings.json`
2. `<cwd>/.opencode/jevs_settings.json`
3. `<repo>/jevs_settings.json`
4. `~/.config/opencode/jevs_settings.json`

Precedence is applied by **merging per key**, not first-file-wins: user-level
files first, then project files override per key. A project file that only sets
`enable_model_routing` does not discard the user's `models` map — which matters
because a default-valued project `jevs_settings.json` is safe to commit and would
otherwise shadow the user config permanently.

| Key | Merge behaviour |
|---|---|
| `enable_model_routing` | last-writer-wins |
| `models` | union; an explicit `""` **removes** an inherited tier |
| `scan_paths` | additive union over `DEFAULT_SCAN_PATHS` |
| `ignore_mcps` | additive union, seeded with `["jev-engine*"]` unconditionally |
| `judge_read_prompts` | plugin only; only an explicit `false` disables |
| `inject_agent_instructions` | plugin only; only an explicit `false` disables |

`DEFAULT_SCAN_PATHS` = `.agents/skills`, `.agents/workflows`, `.agents/memory`,
`.opencode/skills`, `skills`, `.agents`. A scan path already inside another
configured scan path is collapsed to the ancestor, so those files are walked once
rather than four times.

The returned dict is a **defensive copy** — mutating a tool result cannot corrupt
the settings cache. `["source"]` is the last contributor, `["sources"]` lists
every file that contributed.

`opencode.json` is deliberately **not** a settings source. OpenCode's schema is
strict (`additionalProperties: false`), so a `jev_settings` block there
invalidates the whole config and the MCP server silently disappears. A file that
cannot be loaded is not a settings source.

A `scan_paths` entry that is already inside another configured path is collapsed;
entries are resolved against the workspace and deduplicated on load.

---

## Candidate discovery and the scan cache

`candidates.py` builds evidence: front matter split, `description:` preferred,
`markdown_preview`, binary detection (`looks_binary`, `NUL_RATIO_LIMIT` = 0.02),
and `read_head`. `bound_candidates` returns `(kept, truncated)` so a caller can
never silently lose options.

### The prefilter

`MAX_PREVIEWED_CANDIDATES` (40) bounds how many candidates are *read* and turned
into preview text. `limits.MAX_TOTAL_CRITERIA_CHARS` then divides what is left
among them, and everyone else contributes their path alone.

`jev_engine._lexical_score` decides who gets the budget, from the **path only** ?
reading every file to rank better is the cost being removed, and a path is already
in hand before a byte is opened. camelCase, `snake_case`, `kebab-case` and
extensions all split to the same terms, and generic directory names (`src`,
`docs`, `reference`, `.agents`, ?) are ignored so a deep tree does not score every
file it contains.

Two properties make the bound safe rather than lossy, and both are tested:

- **Ranking never removes an option.** A candidate that loses its preview keeps
  its path as evidence, so the Choice can still be handed it and pick it on the
  name. The bound narrows *evidence*, not *options* ? a Choice cannot select a
  value it was never given.
- **It degrades to the old behaviour.** A task sharing no vocabulary with any
  filename produces the old "first N" order exactly, so the worst case is the
  pre-prefilter cost, not a worse answer.

Measured live over `tests/fixtures/routing_tasks.json`: top-1 0.6875 ? 0.7500 ?
0.8125, top-3 recall held at 1.0, mean input tokens 12 949 ? ~7 840 (?39%). See
the Phase 2 section of `docs/perf-baseline.md`.

Discovery uses `git ls-files` with a 5-second timeout when a git repo is
available, falling back to a bounded walk. Discovery itself is capped at
`MAX_DISCOVERED_FILES`. Excluded directories: `.git`, `.godot`, `.import`,
`.venv`, `node_modules`, `dist`, `build`, plus media/binary extensions.

**All three discovery paths refuse symlinks** (`candidates.is_link`): `git
ls-files`, the bounded walk, and the `rglob` over the skill directories. The
allowlist above cannot cover this case, because it checks the *string* an LLM
supplied while a link's target is invisible in its path — `Path.is_file()`
follows it, so a repository can track a link to `~/.ssh/id_rsa`, pass every
textual check, and have its content read into the Choice criteria and sent to
the provider. The check sits at discovery rather than in the reader because
resolving each candidate to compare it against the root measured ~30 ms per call
at 118 candidates on Windows (38% of the whole `search_agent_skills` request)
against ~3 ms for `is_symlink`, paid once per cached walk.
`tests/test_symlink_containment.py` pins all three paths agreeing.

`scan_cache.ScanCache` caches **paths only**, never content:

- keyed on a bounded directory-mtime signature (`SIG_MAX_LEVELS` = 3 levels,
  2 000 directories max, `MAX_ENTRIES`);
- the `git ls-files` result is additionally keyed on HEAD, the ref, the index
  mtime and `.gitignore`;
- `DEFAULT_TTL_S` = 5 s sliding window bounds staleness for a paused sequence;
- the signature is recomputed on **every** call ? never memoized. Reusing it for
  1 s was measured and rejected: 5.4 ms saved against a 388 ms live call (1.4%),
  in exchange for breaking immediate invalidation. See Phase 4 in
  `docs/perf-baseline.md` and `tests/test_dir_signature.py`;
- a file edited *in place* does not invalidate the cache, and does not need to —
  the preview is rebuilt from a fresh read on every call.

`select_mcp_tools` does no filesystem work at all: its criteria come from
strings already in the caller's roster.

### Self-exclusion

`select_mcp_tools` never offers the judge as a candidate. `SERVER_NAME`
(`jev-engine`) is excluded before any criteria are built, by name, by prefix and
case-insensitively, so `jev-engine-local` is out too
(`jev_engine.py:1886`). A judge that recommends itself sends the agent straight
back into the judge and the loop never terminates. Every excluded server is named
in `excluded` with a `reason` (`invalid | duplicate | self | configured |
over_limit`) — never dropped silently.

---

## The OpenCode plugin

`config/jev-plugin.example.js` is a hand-rolled OpenCode plugin. No MCP SDK is
available in OpenCode's plugin sandbox, so the JSON-RPC client is written against
the wire format `scripts/diag_mcp.py` speaks. The plugin holds no Python and no
TypeSafe SDK access; it drives the same `jev_mcp.py` as a client, so there is no
second code path to keep in step.

### Three hooks

| Hook | What it does | Why there |
|---|---|---|
| `chat.message` | judges the prompt, injects the winning skill, applies the model switch | the only hook whose payload is the user message, so the only place a skill can be injected and the only place the model can be changed |
| `tool.execute.after` | when the agent reads a `.md` / `.txt` / `.markdown` / `.prompt` / `.plan` file in the workspace, judges *that file* and appends the ranking to the tool result | at `chat.message` the file has not been read yet, so the judge had only its path |
| `experimental.chat.system.transform` | appends the "where to ask Jev" rules block to the system prompt every turn | the plugin can judge and advise but cannot make the agent act |

### Two host constraints shape what those hooks may do

Both verified against the host rather than assumed, both pinned by
`tests/test_plugin.mjs`:

- **A hook's return value is discarded.** `Plugin.trigger` returns the *same*
  object it was handed, so every effect must be an in-place mutation.
- **A pushed message part crashes the session.** OpenCode validates every part
  against `PartV2` when saving, and a bare `{type:"text", text}` fails hard enough
  to stop the task. So nothing is pushed: the existing text part is mutated, and a
  tool result's last existing text part is replaced by a copy of itself with a
  longer `text`, keeping every id, `messageID`, `sessionID` and `synthetic` flag.

### Decision gates

The two effects have **different** gates, and the difference is deliberate:

| Effect | Gate | Why |
|---|---|---|
| skill injection | `action` is `auto` **or** `review`, and `confidence >= 0.6` | a skill note is advisory text in the user's own message, not an action |
| model switch | `action` is `auto` and `confidence >= 0.6`, plus a well-formed `provider/model` id and an existing `output.message.model` | moving the model changes how the reply is produced, so it takes the server's unreserved verdict |

`escalate` is refused by both — below `JEV_MCP_REVIEW_AT` (0.5) the model is
guessing among options that do not fit.

`PLUGIN_MIN_CONFIDENCE` = 0.6 sits deliberately below the server's
`JEV_MCP_AUTO_ACCEPT` (0.8): by the time the server says `auto` the stricter bar
is already met, so 0.6 is a second, independent floor that a future server-side
threshold change cannot silently remove.

### Client

`JevMcpClient` (`config/jev-plugin.example.js:466`): lazily spawns one child per
session, `initialize` with `protocolVersion: "2025-06-18"` then
`notifications/initialized`, newline framing, `structuredContent` preferred over
the text body, error envelopes parsed out from under the `Error executing tool
…:` prefix, 8 MB buffer cap, per-owner `abort`, 5-minute idle shutdown with a 2 s
kill grace, and a mirroring breaker (3 failures → 60 s open, child stopped).

The plugin's breaker mirrors the server's but does not share it.

### Concurrency and privacy

- `MAX_CONCURRENT_QUERIES` = 3; past that a message is **skipped and logged**,
  never queued.
- Staleness is tracked **per session** (`beginTurn` / `isCurrentTurn`,
  `MAX_TRACKED_SESSIONS` = 64), so a newer message in the same session drops the
  older result while other sessions are unaffected.
- `scan_paths` from `jevs_settings.json` is confined to the workspace unless
  `JEV_PLUGIN_ALLOW_OUTSIDE=1`. Containment uses `path.relative`, never
  `startsWith`, with symlinks resolved first. The selected file is re-checked
  before injection.
- **The API key is never read by the plugin.** The plugin forwards
  `process.env` untouched and adds no key of its own.
- Every hook is wrapped in try/catch; the plugin never crashes OpenCode.
- The prompt-file judge is memoised per `(session, path, size, mtime)` for 10
  minutes, max 64 entries — one round trip per file revision, not one per read.

### Two consequences worth knowing

- **`cwd`, not the session's directory.** The `chat.message` payload carries
  `{sessionID, agent, model, messageID, variant}` and no directory, so the plugin
  uses `process.cwd()`. A session asked about a *different* project than the one
  OpenCode was started in will search the launch directory.
- **The guardrail is advisory.** This build of OpenCode has no `permission.ask`
  plugin hook, so a plugin cannot deny a tool call — it can only judge and tell
  the agent. Making a destructive command *impossible* needs an OpenCode
  permission rule or a wrapper.

---

## Offline mock mode

`JEV_MCP_MOCK=1` runs the same tools end-to-end through `mock.py`, which returns
real `SystemOneResponse` / `ChoiceAnswer` / `NoulAnswer` / `ScoreAnswer` / `Usage`
objects, so the **real** validation and policy layers execute.

How it decides:

- tokenises the state once (`_token_set`) and memoises description token sets
  (`lru_cache(4096)`);
- scores each option as `3.0 × Σ 1/freq(token)` over task terms present in the
  option — inverse document frequency, so a task-specific word dominates a
  ubiquitous one;
- `none` scores 0 and is boosted to win only when nothing else reaches
  `EVIDENCE_MIN_SCORE` = 0.5;
- tier boosts: `frontier` +3.0 for architecture/refactor/design/complex/
  multi-file/race/deadlock; `balanced` +1.5 for bug/test/feature/isolated;
  `fast` +1.5 for typo/lookup/docstring/rename/format;
- a non-presence `Noul` returns a hard `0.97` if the state matches a destructive
  regex (`delete|drop table|wipe|truncate|rm -rf|force push|reset --hard|
  filter-branch|git config --|clean -fdx`), `0.03` for benign commands, else
  `clamp01(0.35 + 0.5 × overlap)`.

`model` is reported as `"{model}+mock"`; `usage.output_tokens = len(questions) × 8`.

`mock.py` states in its own docstring that it is documentation and tests only —
never a substitute for a real TypeSafe decision. Measured routing accuracy is far
below live Jev; see [perf-baseline.md](perf-baseline.md).

---

## Logging and redaction

`log_path()` is `JEV_MCP_LOG_FILE` or `<repo>/logs/jev_engine.log`, absolute and
CWD-independent, so the same install behaves identically in every workspace. A
`RotatingFileHandler(maxBytes=2_000_000, backupCount=3, encoding="utf-8")` plus a
stderr `StreamHandler`; level INFO, `propagate = False`. Handler detection keys
on handler *type*, so a host's own handler cannot silently suppress the file.
Every logging function degrades silently — an unwritable log directory never
crashes the server.

Format: `%(asctime)s %(levelname)s %(message)s`, `%Y-%m-%dT%H:%M:%S%z`, the
message being one-line JSON whose first key is `evt`.

Events: `server_start` (pid, log_file, server), `server_stop`, `tool_call`,
`round_ok` / `round_error`, `settings_parse_error`, `candidates_truncated`,
`circuit_open`, `mcp_no_candidates`, `client_close_failed`.

`_redact` (`jev_logging.py:47`) substitutes only the credential run and keeps the
surrounding context, so `curl -H 'Authorization: Bearer sk-…' https://x` keeps its
command and URL. It matches `(sk|ts|apikey|typesafe)[-_]…{8,}` and
`api[_-]?key|authorization|bearer` followed by a value, and is applied to tool
args, error messages and `result_preview`.

`log_tool_call` (`jev_logging.py:118`) records the tool name, redacted args,
integer `ms`, and — instead of the serialized result — `result_keys` (sorted),
`<key>_count` for each list, and `content_chars` for a `content` string. The body
is opt-in via `JEV_MCP_LOG_PREVIEW=1`, which adds a 1 000-character redacted
`result_preview`; off by default because the MCP runtime serializes the result
again and a skill result carries kilobytes of file content.

Exactly one traceback per failure: `_run` logs it via `log_exception` (traceback
truncated to the last 2 000 chars) and `_request` logs the round with
`traceback=False`, so the same 2 KB is not written twice.

`select_mcp_tools` does **not** log the raw roster. `_mcp_log_summary`
(`jev_mcp.py:107`) writes `"N servers: name, name, …"`, capped at 64 names × 40
chars.

Plugin log: `~/.config/opencode/logs/jev-plugin.log` (or `JEV_PLUGIN_LOG_DIR`),
rotated at 2 MB with exactly one `.1` backup. Prompts are **never** written —
only `len=N sha256=<12 hex>` (`describeText`, `jev-plugin.example.js:145`).

`scripts/doctor.py` never prints a key: length plus a redacted 4-character suffix.

---

## CLI and scripts

`python jev_engine.py [verify|resource|files|tier|mcps] [args]`
(`_cli_dispatch`, `jev_engine.py:2317`) prints one JSON document. Not MCP.

| Script | Purpose |
|---|---|
| `scripts/doctor.py` | read-only install health: interpreter, both SDKs, key *presence*, settings resolution + `sources`, `root_dir` allowlist, log writability, `review_at <= auto_accept`, the OpenCode MCP config (both config shapes, `command` paths, unsubstituted `<REPO_DIR>`, shape vs installed version), plugin SHA256 drift. Exit 0/1, `--json` |
| `scripts/diag_mcp.py` | spawns `jev_mcp.py` and runs one tool call over stdio, against any `root_dir`. `--mock`, `--mcps @file` |
| `scripts/bench_jev.py` | deterministic offline benchmark; `--json`, `--assert`. Exit 0 pass / 1 regression / 2 machine too loaded |
| `scripts/eval_routing.py` | labelled routing quality over both fixture sets; `--mode mock\|live`, `--only mcp` |
| `scripts/stub_mcp.js` | env-driven fake `jev-engine` for the plugin tests (no Python, no key) |

`diag_mcp.py` needs the roster in a **file** for `select_mcp_tools`: an inline JSON
literal loses its double quotes on the way to a native executable under
PowerShell.

---

## Test suite

No `pytest.ini` / `tox.ini` / `setup.py` — plain `pytest` on `tests/`.
25 Python modules, 2 fixture sets, 1 Node file.

`tests/conftest.py` does three things:

- puts the repo root on `sys.path` (the modules are not a package);
- an autouse fixture grants `tempfile.gettempdir()` through
  `JEV_MCP_ALLOWED_ROOTS`, so pytest's `tmp_path` stays a legitimate `root_dir`
  without weakening the allowlist;
- another autouse fixture clears the config / client / settings / breaker / scan
  caches before **and** after every test.

The shared `stub_choice` fixture patches `jev_engine._request` — the single seam
every tool funnels through — so a test supplies a hand-built `ChoiceAnswer` while
discovery, validation and policy still run for real.

| Module | Pins |
|---|---|
| `test_policy.py` | confidence formula, actions, thresholds, no resurrected escape-hatch constant |
| `test_validation.py` | fail-closed structure and failure paths, including `Score` |
| `test_validation_nan.py` | `NaN`/`inf` everywhere, `bool` refused as a number, 7-level vs 2-level tolerances |
| `test_envelope_shapes.py` | identical key sets across every branch of all four deciding tools (five verdicts for `select_mcp_tools`); no `NaN` serialized |
| `test_mcp_selection.py` | self-exclusion incl. suffixed/case variants, `ignore_mcps` names and globs, roster normalisation, all verdicts, the `0.12` floor, the 20-tool ceiling and its reason code, both caller ceilings |
| `test_mcp_transport.py` | the **real** `jev_mcp.py` over real stdio JSON-RPC: advertisement (5 tools), a round trip per tool, fail-closed arriving as `isError: true` |
| `test_limits.py` | `estimate_tokens` bit-identical to the original loop on a pinned corpus, never over budget, no split surrogates |
| `test_mock_tools.py` | offline tool runs: backward-compat keys, envelope keys, malformed→`INVALID_RESPONSE`, the `none` option, candidate truncation |
| `test_candidates.py` | front-matter split, `description:` preferred, truncation, unreadable files, `bound_candidates` returning `(kept, truncated)` |
| `test_deadline.py` | `JEV_MCP_TIMEOUT_MS` really bounds a call; the retry policy carries the total budget and does not retry timeouts |
| `test_breaker.py` | opens after N failures, admits one probe after the cooldown, short-circuits auth failures for longer |
| `test_root_dir_allowlist.py` | allowed/denied `root_dir`, sibling-prefix and symlink escapes, `JEV_MCP_ALLOWED_ROOTS` |
| `test_doctor.py` | the MCP config check: both OpenCode config shapes, placeholder and missing-path detection, enable-key and `codemode` validity, BOM tolerance |
| `test_symlink_containment.py` | all three discovery paths refusing symlinks, and agreeing |
| `test_prefilter.py` | term splitting, path scoring, ranking degradation, and that the preview bound narrows evidence without removing options |
| `test_concurrency_and_encoding.py` | the single half-open probe under real threads, the `ScanCache` eviction race, BOM'd settings files, and the `task` length cap |
| `test_settings_merge.py` | per-key merge, `""` clears a tier, post-read re-stat, the defensive snapshot, `opencode.json` not a source |
| `test_client_cache.py` | the cached client is closed on invalidation and keyed on everything the SDK reads |
| `test_scan_cache.py` | a warm call never re-walks or forks `git ls-files`, a new file invalidates, nested default scan paths collapse, `MAX_DISCOVERED_FILES` blocks `auto` |
| `test_hardening_integration.py` | family clustering with a confident primary and suppression with a weak one, walk-depth bound, settings mtime cache, client pool closed rather than leaked |
| `test_mock_perf.py` | offline judge answers pinned to a golden recorded pre-optimization; the state is tokenized once |
| `test_routing_quality.py` | fixture invariants; live accuracy floors only with `JEV_ROUTING_LIVE=1` |
| `test_logging.py` | `result_keys` instead of a serialized result, the opt-in preview, credential redaction, exactly one traceback |
| `test_errors.py`, `test_cleanup_phase8.py`, `test_config_dotenv.py` | error-envelope mapping, dotenv fallback semantics, fail-closed seams |
| `test_task_file.py` | `task_file` confinement, head-only read, combination with `task` |
| `test_live_smoke.py` | skipped unless `TYPESAFE_API_KEY` or `JEV_MCP_LIVE=1` |

Fixtures: `tests/fixtures/routing_tasks.json` (25 labelled cases) and
`tests/fixtures/mcp_selection_tasks.json` (13), both consumed by
`scripts/eval_routing.py`.

`node tests/test_plugin.mjs` has 16 numbered sections, the notable ones being
the client driven against a **real child process** (`scripts/stub_mcp.js`):
handshake, child reuse, split-write reassembly, `isError` unwrapping,
`parseEnvelope` under the prefix, mid-call death and respawn, timeout, `abort`,
breaker open/close, two sessions judged at once, overflow skipped, scoped abort,
per-session supersede. Plus a **source-level** assertion that the plugin contains
no `typesafe_sdk`, `TypeSafeClient`, `pythonScript`, `spawnSync` or
`TYPESAFE_API_KEY =`, and exactly one `spawn(`. Section 16 byte-compares the
installed plugin against `config/jev-plugin.example.js` and fails on any drift,
skipping with a warning if the installed copy is absent.

---

## Failure modes to avoid

- **No class confusion:** import `TypeSafeClient` from `typesafe_sdk` — never
  `JevClient` or the `typesafe_ai` module.
- **Choice criteria format:** `Choice(criteria={option: description})` is a
  **dictionary mapping**. A plain list (`Choice(options=...)`) raises a pydantic
  `ValidationError`.
- **mcp 2.x import:** `from mcp.server.mcpserver import MCPServer as FastMCP`.
  The `mcp.server.fastmcp` module was removed in mcp 2.x.
- **Always run inside the venv.** A missing-package error is almost always a
  system Python rather than `.venv`.
- **JSON-serializable returns:** every tool returns a plain `dict` of
  `str`/`bool`/`float`/`None` — never pydantic objects.
- **Plugin isolation:** errors inside `jev-plugin.js` must never propagate to
  OpenCode message hooks.
- **`JEV_MCP_ALLOWED_ROOTS` is `os.pathsep`-separated** — `;` on Windows, `:` on
  POSIX. Splitting on the wrong one produces one nonsense entry.
- **Not implemented, deliberately:** `ask_user` / `investigate` escape hatches;
  a lexical prefilter over candidate previews (`_preview_criteria`,
  `jev_engine.py:1527`).