# jev-engine — Jev AI (TypeSafe AI) MCP Server

A Model Context Protocol (MCP) server registered as **`jev-engine`** over stdio. It wraps the Jev AI (TypeSafe AI) deterministic decision engine so any MCP-capable model (Claude, GPT, DeepSeek, local models) can run guardrail checks, skill routing, file targeting, and model-tier selection through four JSON-RPC tools.

---

## System at a Glance

| Item | Value |
|---|---|
| **Server identity** | `jev-engine` (MCP over stdio) |
| **Hosting directory** | `D:\mcp\jev-typesafe-mcp` |
| **Interpreter** | `.venv\Scripts\python.exe` (isolated virtual environment) |
| **Decision engine** | `TypeSafeClient.system_one(state=..., questions=...)` |
| **Required SDK** | `typesafe-sdk` (`TypeSafeClient`, `Choice`, `Noul`, `RetryPolicy`) — **never** `JevClient` or `typesafe_ai`. `Score` is supported by the validator but no tool asks a graded question yet. |
| **MCP SDK** | `mcp==2.2.0` — uses `MCPServer` (`from mcp.server.mcpserver import MCPServer as FastMCP`) |
| **Model compatibility** | Model-agnostic; any MCP client with stdio tool support |

> **Compatibility note:** The `mcp.server.fastmcp` module was **removed** in mcp 2.x (FastMCP renamed to `MCPServer`). `jev_mcp.py` therefore imports `MCPServer as FastMCP` directly. Do **not** pin `mcp<2` unless you intentionally move to the v1 line.

> **Reference clones:** `reference/jkudish-jev-mcp` and
> `reference/burnigtm-jev-mcp` are git submodule-style local clones used as
> design references (validation/policy/limits/errors patterns borrowed under
> MIT). They are git-ignored and not part of this server's runtime.

---

## File & Config Locations

| Path | Purpose |
|---|---|
| `D:\mcp\jev-typesafe-mcp\jev_engine.py` | Core decision logic, config loader, CLI entry point |
| `D:\mcp\jev-typesafe-mcp\jev_mcp.py` | MCP server: registers the 4 tools, delegates to `jev_engine` |
| `D:\mcp\jev-typesafe-mcp\jev_errors.py` | Typed errors + `error_details()` envelope mapping |
| `D:\mcp\jev-typesafe-mcp\jev_validation.py` | Fail-closed response envelope validation |
| `D:\mcp\jev-typesafe-mcp\policy.py` | Confidence, policy actions, thresholds |
| `D:\mcp\jev-typesafe-mcp\candidates.py` | Candidate discovery + evidence previews for the `Choice` criteria |
| `D:\mcp\jev-typesafe-mcp\limits.py` | Token budget estimation + state fitting/truncation |
| `D:\mcp\jev-typesafe-mcp\config.py` | Env config parsing + validation (`JEV_MCP_*`) |
| `D:\mcp\jev-typesafe-mcp\mock.py` | Deterministic offline judge for `JEV_MCP_MOCK=1` |
| `D:\mcp\jev-typesafe-mcp\jev_logging.py` | Filesystem logging (tool calls, provider rounds, tracebacks) |
| `D:\mcp\jev-typesafe-mcp\scripts\diag_mcp.py` | Transport-level MCP repro client for any workspace + prompt |
| `D:\mcp\jev-typesafe-mcp\scripts\bench_jev.py` | Offline timing/size benchmark with `--assert` regression gates |
| `D:\mcp\jev-typesafe-mcp\scripts\eval_routing.py` | Routing accuracy/false-positive/token harness over `tests\fixtures\routing_tasks.json` |
| `D:\mcp\jev-typesafe-mcp\scripts\doctor.py` | Read-only installation health check (interpreter, SDKs, key, settings, allowlist, plugin drift) |
| `D:\mcp\jev-typesafe-mcp\scripts\stub_mcp.js` | Stub stdio MCP server used by `tests\test_plugin.mjs` (no Python, no API key) |
| `D:\mcp\jev-typesafe-mcp\tests\` | pytest: validation, policy, limits, candidates, transport, mock tools, live smoke |
| `D:\mcp\jev-typesafe-mcp\requirements.txt` | Pinned Python dependencies (UTF-8) |
| `D:\mcp\jev-typesafe-mcp\.env` | Local secrets — holds `TYPESAFE_API_KEY` (never committed) |
| `D:\mcp\jev-typesafe-mcp\.env.example` | Template showing required + optional env keys |
| `D:\mcp\jev-typesafe-mcp\.venv\` | Isolated virtual environment (Python 3.14) |
| `D:\mcp\jev-typesafe-mcp\README.md` | This document |
| `D:\mcp\jev-typesafe-mcp\config\opencode.example.json` | Sanitized OpenCode config template (safe to commit — no secrets) |
| `D:\mcp\jev-typesafe-mcp\config\jevs_settings.example.json` | Sanitized per-project settings template (safe to commit) |
| `D:\mcp\jev-typesafe-mcp\config\jev-plugin.example.js` | Sanitized plugin template (safe to commit) |
| `D:\mcp\jev-typesafe-mcp\config\README.md` | Install guide for deploying the examples |
| `<project-root>\jevs_settings.json` | Per-project Jev settings (`enable_model_routing`, `models`, `scan_paths`) — never commit a copy containing keys |
| `C:\Users\aalji\.config\opencode\opencode.json` | OpenCode MCP server registration only (no `jev_settings`) |
| `C:\Users\aalji\.config\opencode\plugins\jev-plugin.js` | Optional OpenCode plugin (hook-based Jev routing + forced model switching) |

---

## Architecture

```
OpenCode (or any MCP client)
   │  stdio JSON-RPC
   ▼
jev_mcp.py  ── MCPServer("jev-engine") ── 4 tools (typed error envelopes)
   │  delegates
   ▼
jev_engine.py  ── execute_system_one ── TypeSafeClient.system_one(state, questions)
   │                │                       (mock branch when JEV_MCP_MOCK=1)
   │                │                       (circuit breaker: fail fast while the
   │                │                        provider is down instead of retrying
   │                │                        the full budget on every call)
   │                ├─ candidates()          candidates.py (candidate previews)
   │                ├─ fit_state()            limits.py   (token budget → truncated)
   │                ├─ validate_response()    jev_validation.py (fail-closed)
   │                └─ policy                 policy.py   (confidence/action)
   │  reads
   ▼
jevs_settings.json  (enable_model_routing, models, scan_paths)
.env → TYPESAFE_API_KEY, JEV_MCP_MODEL, JEV_MCP_TIMEOUT_MS, JEV_MCP_MOCK,
       JEV_MCP_AUTO_ACCEPT, JEV_MCP_REVIEW_AT, JEV_MCP_ALLOWED_ROOTS,
       JEV_MCP_BREAKER_*
```

### Decision flow

1. A tool receives a prompt/task/command via MCP.
2. For the selection tools, `candidates.py` turns each candidate path into a short
   evidence string (a `SKILL.md` front-matter `description` where present, else
   the head of the file), bounded by `limits.MAX_CANDIDATE_CHARS` and the total
   preview budget. A `Choice` can only pick from the options it is given, so this
   is what makes the options distinguishable.
3. `jev_engine` fits `state` to the token budget (`limits.fit_state`), calls
   `TypeSafeClient.system_one(state=..., questions=...)` (or the mock judge)
   using `Noul` / `Choice` primitives.
4. `jev_validation.validate_response` verifies the response against the
   questions **before any policy number is read**. Malformed or
   self-contradictory answers raise `JevResponseError` — they are never read
   as `safe:true`.
5. `policy.py` maps probabilities to `confidence` and an `action`
   (`auto | review | escalate`); truncated context never yields `auto`.
6. A JSON-serializable dict is returned with the legacy keys plus
   `action/confidence/ranked/model/usage/truncated/coverage`.

### Result envelope & typed errors

Every live call adds `model` and `usage: {input_tokens, output_tokens}`. Tools
that make a decision add `action`, `confidence`, and (where relevant) `ranked`
and `truncated`. When anything fails — config, budget, timeout, invalid
response — the tool returns a fail-closed envelope:

```json
{ "error": { "code": "INVALID_RESPONSE", "message": "...", "retryable": false } }
```

`error_details()` (`jev_errors.py`) maps SDK failures to `{code, message, retryable}`.
The codes are stable and safe to branch on:

| Code | Meaning | `retryable` |
|---|---|---|
| `INVALID_INPUT` | Bad argument: a `root_dir` outside the allowlist, an oversized string, a system directory | `false` |
| `INVALID_RESPONSE` | The provider's answer contradicts the questions that produced it (fail-closed; **never** read as `safe: true`) | `false` |
| `CONFIG_ERROR` | A `JEV_MCP_*` value is missing or out of range | `false` |
| `INPUT_TOO_LARGE` | The questions alone exceed the estimated context budget | `false` |
| `AUTH_ERROR` | TypeSafe rejected the key (401) | `false` |
| `FORBIDDEN` | TypeSafe denied access (403) | `false` |
| `RATE_LIMITED` | Provider rate limit (429) | `true` |
| `API_ERROR` | Any other provider or transport failure | depends on status |
| `TIMEOUT` | The total call budget expired | `true` |
| `CANCELLED` | The host aborted the request | `false` |
| `INTERNAL_ERROR` | Anything unmapped | `false` |

Provider response bodies and request metadata are never relayed — a `field_path`
and an HTTP status are all that survive.

---

## Environment & Setup

```powershell
cd D:\mcp\jev-typesafe-mcp

# 1. Create/activate the venv (already committed to disk here)
.\.venv\Scripts\python.exe --version

# 2. Install pinned dependencies
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 3. Configure the API key (copy the template, fill in the real key)
Copy-Item .env.example .env
```

### `.env` / `.env.example`

```dotenv
TYPESAFE_API_KEY=apikey_********************************
# JEV_MCP_MODEL=jev-latest          # model for system_one (default: jev-latest)
# JEV_MCP_TIMEOUT_MS=30000          # TOTAL per-tool-call budget ms, retries included
# JEV_MCP_MOCK=0                    # 1 = offline deterministic judge (tests/demos)
# JEV_MCP_AUTO_ACCEPT=0.8           # confidence to auto-accept (0..1)
# JEV_MCP_REVIEW_AT=0.5             # confidence below which we escalate (0..1)
# JEV_MCP_ALLOWED_ROOTS=            # extra root_dir allowlist entries (';'-separated)
# JEV_MCP_BREAKER_THRESHOLD=3       # consecutive provider failures before the breaker opens
# JEV_MCP_BREAKER_COOLDOWN_S=30     # seconds before one probe call is let through
# JEV_MCP_AUTH_COOLDOWN_S=300       # longer window after a 401/403
# JEV_MCP_LOG_FILE=                 # absolute log path (default: <repo>\logs\jev_engine.log)
# JEV_MCP_LOG_PREVIEW=0             # 1 = also log a 1,000-char result preview
```

`JEV_MCP_LOG_PREVIEW` and `JEV_MCP_LOG_FILE` are read by the **server**;
`JEV_PLUGIN_SCAN_ROOTS` below is read by the **plugin** and forwarded to the
server as `JEV_MCP_ALLOWED_ROOTS`, so both sides agree on what may be read.

`JEV_MCP_TIMEOUT_MS` is a **total** budget for one tool call, not a per-attempt
timeout: it is passed as the SDK retry policy's total limit, and each attempt is
clamped to whatever remains of it, so a call cannot take 3× the configured
value. The same deadline is applied in mock mode, which is CPU-bound.

Both `jev_engine.py` and `jev_mcp.py` load `.env` via `python-dotenv` with `override=False` (relative to the file's parent directory). Because the MCP config already injects `TYPESAFE_API_KEY`, injected variables take precedence and the `.env` file acts as a reliable fallback.

### Is this installation healthy?

```powershell
& .\.venv\Scripts\python.exe scripts\doctor.py
```

Read-only, no child process, no API call. It checks the interpreter, the two SDKs,
key *presence* (length and a 4-character suffix only — the value is never
printed), settings resolution and `sources`, the `root_dir` allowlist against the
current directory, log-directory writability, the resolved budget and threshold
consistency (`review_at <= auto_accept`), and whether the installed
`jev-plugin.js` still matches `config/jev-plugin.example.js` by SHA256. Exits 0
when healthy, 1 when something needs attention, and prints a one-line fix for
each finding. Add `--json` for machine-readable output.

### Verify the SDK is healthy

```powershell
& .\.venv\Scripts\python.exe -c "import typesafe_sdk; print(('TypeSafeClient','Choice','Noul','Score') , [n for n in ('TypeSafeClient','Choice','Noul','Score') if n in dir(typesafe_sdk)]); print('JevClient present?', 'JevClient' in dir(typesafe_sdk))"
```

---

## Core MCP Tools

Registered by `jev_mcp.py`; all return JSON-serializable dictionaries.

### 1. `guardrail_command` — terminal command safety

Pre-execution audit for shell commands.

- **Params:** `command` (string, required)
- **Jev primitives:** `is_destructive` (`Noul`), `modifies_git` (`Noul`)
- **Thresholds:** `safe` only if `action == "auto"` **and** `destructive_prob < 0.20` **and** `git_modify_prob < 0.20`. A confident "destructive" judgment (`>= 0.5`) escalates; `>= 0.2` reviews.

```json
{
  "safe": true, "destructive_prob": 0.01, "git_modify_prob": 0.01,
  "action": "auto", "confidence": 0.99, "reason_codes": [],
  "truncated": false,
  "coverage": { "complete": true, ... },
  "model": "jev-latest", "usage": { "input_tokens": 120, "output_tokens": 12 }
}
```

### 2. `search_agent_skills` — dynamic context pruning & skill routing

Scans workspace agent docs and returns only relevant Markdown, inlining up to 6,000 chars per resource.

- **Params:** `task` (required), `root_dir` (default `.`, confined to the allowed roots)
- **Scan dirs:** `.agents/skills`, `.agents/workflows`, `.agents/memory`, `.opencode/skills`, `skills`, `.agents` **plus** any extras from `jevs_settings.scan_paths`
- **Jev primitive:** `primary` (`Choice`, one question). `criteria` carry each document's own summary — a `SKILL.md` contributes its front-matter `description` — not its filename, so the options are actually distinguishable. `ranked` comes from `primary.probabilities`, which is the full ranking; there are no `secondary`/`tertiary` duplicates.
- **Key capabilities:** sibling expansion (probability ≥ 0.12), sibling-prefix clustering (e.g. `godot-ui-*`) but only when `primary_probability >= 0.5` and for at most 2 siblings
- **Silent-drop guard:** candidates past `MAX_CHOICE_OPTIONS` are reported in `candidates_considered` / `candidates_evaluated` / `candidates_truncated` + `reason_codes`, and a truncated candidate set forces `action != "auto"`. Discovery itself is capped at `MAX_DISCOVERED_FILES` (5 000) for the same reason.

```json
{
  "matched": true,
  "count": 2,
  "primary": { "name": "godot-ui-theme", "file": ".agents/skills/godot-ui-theme/SKILL.md", "content": "..." },
  "resources": [...],
  "summary": "Found 2 relevant agent resource(s): godot-ui-theme, godot-ui-layout",
  "primary_probability": 0.91,
  "ranked": [ { "file": ".agents/skills/godot-ui-theme/SKILL.md", "probability": 0.91 }, ... ],
  "candidates_considered": 12, "candidates_evaluated": 12, "candidates_truncated": false,
  "reason_codes": [],
  "action": "auto", "confidence": 0.87, "truncated": false,
  "coverage": { "complete": true, "candidate_fields": { ... } },
  "model": "jev-latest", "usage": { "input_tokens": 2400, "output_tokens": 24 }
}
```

`primary` **is** `resources[0]`. The flat `file` / `content` keys that duplicated it
were removed in Phase 5: they serialized the same up-to-6,000-character body a
third and fourth time in the same JSON document. Read `result["primary"]["file"]`
(or `resources[0]["file"]`) instead.

### 3. `search_target_files` — fast workspace file selector

Filters the repo tree down to task-relevant files.

- **Params:** `task` (required), `root_dir` (default `.`, confined to the allowed roots)
- **Exclusions:** `.git`, `.godot`, `.import`, `.venv`, `node_modules`, `dist`, `build` + media/binary extensions (`.png`, `.jpg`, `.wav`, `.mp3`, `.zip`, ...)
- **Jev primitives:** `target_file` (`Choice`, `criteria={path: <path + head of file>}`) **and** `is_relevant` (`Noul`). Two independent questions over the same state, one request. The Noul is what stops a forced winner among poor options from reading as a match.
- **Cost bounds:** at most `MAX_PREVIEW_READS` (120) file reads and `MAX_TOTAL_PREVIEW_CHARS` (40 000) preview characters per call; candidates past the bound keep their path alone and are counted in `coverage.candidate_fields.previews_skipped`.

```json
{
  "matched": true, "files": ["src/core/player_controller.gd"],
  "exists": "answered", "probability": 0.94, "relevance_prob": 0.88,
  "ranked": [ { "file": "src/core/player_controller.gd", "probability": 0.94 }, ... ],
  "candidates_truncated": false,
  "action": "auto", "confidence": 0.9, "truncated": false,
  "coverage": { "complete": true, "candidate_fields": { ... } },
  "model": "jev-latest", "usage": { "input_tokens": 12880, "output_tokens": 16 }
}
```

If no file fits, the model picks the `none` option: `matched:false` and `files:[]`.
`exists` says what was actually established, and it is the field to branch on:

| `exists` | Meaning |
|---|---|
| `answered` | A file was chosen **and** `relevance_prob >= 0.5` — something really has to be read or edited. |
| `partial` | A file was chosen but the presence Noul disagreed (`relevance_prob < 0.5`): the Choice was confident among options that do not fit. `matched` is `false`. This is the fail-closed direction a `none` option inside a `Choice` cannot produce on its own. |
| `absent` | The model picked `none` with high confidence — nothing in the workspace is relevant. |
| `no_candidates` | Discovery found nothing to offer (an empty or fully pruned workspace), so no Jev call was made at all. |

### 4. `select_model_tier` — dynamic model tier routing

Analyzes task complexity and assigns a tier. **Gated by `jevs_settings.enable_model_routing`** (default `false`).

- **When off:** returns immediately, disabled payload, **no Jev API call**, no model switching (opencode keeps its configured/window model).
- **When on:** runs a `Choice` over `fast` / `balanced` / `frontier`, then resolves the configured model for the chosen tier. This tool returns a **recommendation**; the actual **forced switch** is applied by the `chat.message` plugin hook.

```json
{
  "enabled": true,
  "task": "Refactor auth middleware to support OAuth2 refresh tokens",
  "recommended_tier": "frontier",
  "recommended_model": "<frontier model id>",
  "model_map": { "fast": "...", "balanced": "...", "frontier": "..." },
  "action": "auto", "confidence": 0.93, "truncated": false,
  "coverage": { "complete": true, ... },
  "model": "jev-latest", "usage": { "input_tokens": 200, "output_tokens": 12 }
}
```

```json
{ "enabled": false, "task": "...", "recommended_tier": null, "recommended_model": null, "model_map": {}, "action": "review", "confidence": null, "truncated": false, "model": null, "usage": null }
```

Low-confidence or truncated judgments report `action: "escalate"` while
keeping `recommended_tier` unchanged.

---

## Configuration — `jevs_settings.json` (per project)

File: `<project-root>/jevs_settings.json`. Discovery order:

1. `<project>/jevs_settings.json`
2. `<project>/.opencode/jevs_settings.json`
3. `<repo>/jevs_settings.json` (the server's own directory, so the CWD does not
   matter)
4. `~/.config/opencode/jevs_settings.json`

Discovery order is unchanged, but precedence is applied by **merging**, not by
first-file-wins: the user-level files are applied first, then the project files
override them **per key**. A project file that only sets
`enable_model_routing` no longer discards the user file's `models` map — which
matters because a default-valued project `jevs_settings.json` is safe to commit
and would otherwise shadow the user config permanently.

There is **no** `jev_settings` block in `opencode.json`, and the server does not
look for one: OpenCode's `opencommand` schema is strict
(`additionalProperties: false`), so an unknown top-level key invalidates the
whole config and the server would not start at all. A file that cannot be loaded
is not a settings source, and the lookup that used to read it has been deleted.
(The plugin keeps a vestigial `jev_settings` fallback in `loadSettings`; it is
unreachable for the same reason and is not load-bearing either way. The plugin
does read `opencode.json` for `mcp["jev-engine"].command`, which is legitimate —
that key is part of the schema.)

```jsonc
{
  "enable_model_routing": false,
  "models": {
    "fast":     "",      // <-- "provider/model-id" used for FORCED switching
    "balanced": "",
    "frontier": ""
  },
  "scan_paths": []        // <-- extra skill dirs, appended to the defaults
}
```

### Semantics

| Key | Type | Meaning |
|---|---|---|
| `enable_model_routing` | bool | Master switch. **Off** (default) = no Jev model call, opencode uses its `"model"` config / window-selected model. **On** = the plugin asks Jev the tier and **forces** the switch per task. |
| `models` | `{fast, balanced, frontier}` | Tier → `"provider/model-id"` mapping applied by the plugin. Values **union** across files; an explicit `""` removes an inherited tier. |
| `scan_paths` | `[relative path]` | **Additive** extras to the default skill dirs, unioned across files and deduplicated on load. |

Everything is optional; a missing/invalid file falls back to defaults (routing off,
empty models, built-in scan dirs `.agents/skills`, `.agents/workflows`,
`.agents/memory`, `.opencode/skills`, `skills`, `.agents`). Malformed JSON never
breaks the server — it logs to stderr and the remaining files are still applied.
`load_jev_settings()["source"]` is the last contributor (the project file) and
`["sources"]` lists every file that contributed; the returned dict is a
defensive copy, so a tool result can never alias the settings cache.

---

## Bounds — deadlines, `root_dir` and the circuit breaker

Three limits keep a misbehaving or hostile caller from turning a decision tool
into an unbounded read or an unbounded wait.

**`JEV_MCP_TIMEOUT_MS` is a total budget.** The SDK's per-attempt timeout is
only a slice of the real cost: with `max_retries=2` the worst case is 3 × the
timeout plus backoff. The budget is passed as `RetryPolicy.timeout` (the whole
call), each attempt is clamped to the *remaining* deadline, and timeouts are not
retried — a retry cannot beat an expired deadline, it only burns budget. Mock
mode enforces the same deadline, so the CPU-bound offline judge cannot quietly
exceed a small budget either.

**`root_dir` is confined to an allowlist.** `search_agent_skills` and
`search_target_files` both take a `root_dir`, and an LLM supplies it. Allowed:
the process working directory and anything under it, any **ancestor** of it
(hosts launch the server with `cwd` set to the project root or to a temp dir),
and anything under `JEV_MCP_ALLOWED_ROOTS`. Everything else is rejected with
`INVALID_INPUT`. Containment uses `Path.relative_to`, never `startswith`, so
`<root>-evil` and a symlink pointing outside the root are both refused. The OS's
own directories (`C:\Windows`, `C:\Program Files`, a filesystem root) are
rejected by name as a second gate.

**A circuit breaker short-circuits a failing provider.** After
`JEV_MCP_BREAKER_THRESHOLD` consecutive failures (default 3) the breaker opens
and further calls fail immediately with a `TIMEOUT` / `retryable: true` envelope
that states when to come back — instead of paying three attempts plus backoff on
every call, and again on every plugin message. Auth failures (401/403) use the
longer `JEV_MCP_AUTH_COOLDOWN_S` window, because a bad key does not fix itself in
30 seconds. After the cooldown, exactly one probe call is let through; if it
succeeds the breaker closes, and if it fails the circuit re-opens.

---

## The OpenCode Plugin — `jev-plugin.js`

Location: `C:\Users\aalji\.config\opencode\plugins\jev-plugin.js`

An auxiliary OpenCode hook (`chat.message`) that does two things per user message:

1. **Forced model routing** — when `jevs_settings.json` has `enable_model_routing: true`
   **and** at least one tier has a model ID, it asks Jev for the task tier
   (`fast` / `balanced` / `frontier`), maps it through `models`, and **forces** the
   switch by mutating `output.message.model = { providerID, modelID }`. If the
   tier Jev picks has no configured model, nothing is switched — a partial map is
   used, it does not have to be complete. opencode persists the user message
   *after* the `chat.message` hook fires and routes the next reply from
   `lastUser.model`, so this is a real, forced switch — not a recommendation. When
   routing is off, the message model is left untouched and opencode uses its
   `"model"` config / window-selected model.
2. **Skill routing** — asks Jev which single Markdown file under the workspace is
   most relevant, and **injects its content** into the conversation context
   (`[Active Capability / Skill: <path>]`).

Both decisions are gated on the server's own verdict: `action == "auto"` **and**
`confidence >= 0.6`. A `review` / `escalate` decision injects nothing and switches
nothing — a weak judgment must not put a possibly-irrelevant skill into the
model's context, where it is indistinguishable from something the user asked for.
The 0.6 is deliberately below the server's `JEV_MCP_AUTO_ACCEPT` (0.8): by the
time the server says `auto` the stricter bar is already met, so 0.6 is a second,
independent floor that a future server-side threshold change cannot silently
remove.

### How it works

1. Reads settings from `jevs_settings.json` (project → `.opencode/jevs_settings.json`
   → user `~/.config/opencode/jevs_settings.json`). It does **not** merge them:
   the first file that parses wins, which is fine for the plugin because it only
   needs `models` and `scan_paths`, and a stale user-level copy is a visible
   symptom rather than a silent one.
2. Takes the `[python, server]` argv from `mcp["jev-engine"].command` (falling
   back to `<repo>\.venv\Scripts\python.exe <repo>\jev_mcp.py`).
3. Returns early when there is nothing to do — routing off and no Markdown under
   the configured skill dirs costs no process and no API call.
4. Spawns **one** `jev_mcp.py` child and drives it as an **MCP client**:
   newline-delimited JSON-RPC 2.0 over stdio, `initialize` then `tools/call
   search_agent_skills` (+ `select_model_tier` when routing is on). No MCP SDK is
   available in opencode's plugin sandbox, so the ~200-line client is hand-rolled
   against the same wire format `scripts/diag_mcp.py` speaks.

The child is reused for the whole session and shut down after 5 idle minutes, so
the steady-state cost per message is one HTTPS round trip on a warm pooled
connection — no interpreter start, no SDK import. See `docs/perf-baseline.md`.

**The API key is never read by the plugin.** The server loads `TYPESAFE_API_KEY`
from its own env or `.env`, and the MCP `environment` block injects it. That
removes the plugin's `.env` regex, which used to capture a quoted key *with* its
quotes — a guaranteed 401.

**The plugin is not trusted with paths it was handed.** `scan_paths` comes from
`jevs_settings.json`, which is documented as safe to commit and share. Resolved
against the working directory with no containment check, an untrusted repository
could name `../../../../Users/victim` and have arbitrary `.md` files read and
injected. Scan paths are therefore confined to the workspace with a
`path.relative` check (never `startsWith`, so `<root>-evil` is refused), symlinks
are resolved before the check, and the selected file is re-checked before it is
injected. To keep skills outside the project, opt in explicitly:

```powershell
$env:JEV_PLUGIN_SCAN_ROOTS="$env:USERPROFILE\.config\opencode\skills"
```

which the plugin forwards to the server as `JEV_MCP_ALLOWED_ROOTS`, so both sides
agree on what may be read.

**Resilience.** One in-flight query per session (a concurrent message is skipped
and logged, not queued — a stale injection is worse than none), a 3-failure /
60-second circuit breaker mirroring the server's own, per-request timeouts, an
idle shutdown, and support for aborting a message the user cancelled.

All errors are caught and logged ("Execution bypassed safely") — the hook never
crashes OpenCode. The plugin log rotates at 2 MB and records a prompt's **length
and digest only**, never its text.

The hook always passes the working directory as `root_dir`, which the server
confines to the process CWD and its ancestors. Skills kept outside the workspace
only become reachable through `JEV_MCP_ALLOWED_ROOTS` above.

---

## Failure Modes to Prevent

- **No class confusion:** import `TypeSafeClient` from `typesafe_sdk` — never `JevClient` or the `typesafe_ai` module.
- **Choice criteria format:** `Choice(criteria={option: description})` is a **dictionary mapping**; a plain list (`Choice(options=...)`) raises a pydantic `ValidationError`.
- **Missing package errors:** always run inside `.venv` (`D:\mcp\jev-typesafe-mcp\.venv\Scripts\python.exe`).
- **No broken MCP import:** on mcp 2.x use `from mcp.server.mcpserver import MCPServer as FastMCP` (the `mcp.server.fastmcp` module is removed).
- **JSON-serializable returns:** every tool returns plain dicts (str / bool / float / None) — never pydantic objects.
- **Plugin isolation:** errors inside `jev-plugin.js` never propagate to OpenCode message hooks.

---

## Run & Verify

```powershell
cd D:\mcp\jev-typesafe-mcp

# Syntax check (every module, not just the two entry points)
& .\.venv\Scripts\python.exe -m py_compile jev_engine.py jev_mcp.py jev_validation.py jev_errors.py policy.py limits.py config.py mock.py jev_logging.py candidates.py scan_cache.py scripts\diag_mcp.py scripts\bench_jev.py scripts\eval_routing.py scripts\doctor.py

# Offline test suite (no API key needed — mock mode covers the tools)
& .\.venv\Scripts\python.exe -m pytest tests -q

# Offline plugin tests (spawns scripts\stub_mcp.js; no API key, no opencode)
node tests\test_plugin.mjs

# Offline performance gates (deterministic; see docs/perf-baseline.md)
# exit 0 = pass, 1 = real regression, 2 = machine too loaded to judge
& .\.venv\Scripts\python.exe scripts\bench_jev.py --assert

# Routing quality: accuracy, false positives, input tokens (mock, then live)
& .\.venv\Scripts\python.exe scripts\eval_routing.py --mode mock
& .\.venv\Scripts\python.exe scripts\eval_routing.py --mode live

# Installation health: interpreter, SDKs, key presence, settings, plugin drift
& .\.venv\Scripts\python.exe scripts\doctor.py

# Import check
& .\.venv\Scripts\python.exe -c "import jev_mcp; print('MCP import successful!')"

# Engine CLI smoke test — live (default) or offline with JEV_MCP_MOCK=1
& .\.venv\Scripts\python.exe jev_engine.py verify "git status"
$env:JEV_MCP_MOCK="1"; & .\.venv\Scripts\python.exe jev_engine.py verify "git status"

# Print resolved jev_settings + scan dirs
& .\.venv\Scripts\python.exe -c "from jev_engine import load_jev_settings, get_scan_paths; from pathlib import Path; print(load_jev_settings()); print([str(p) for p in get_scan_paths(Path('.'))])"

# Reproduce a tool call over the real MCP transport (same as opencode):
& .\.venv\Scripts\python.exe scripts\diag_mcp.py --tool search_agent_skills --task "fix ui bug" --root_dir D:\Godot_projects\flux-wall

# Start the server (blocks on stdio, waits for an MCP client)
& .\.venv\Scripts\python.exe jev_mcp.py
```

Expected outputs:

```
py_compile OK
394 passed, 14 skipped
ALL PLUGIN TESTS PASSED
All assert gates passed.
healthy
```

`bench_jev.py --assert` exits **2** instead of 1 when the machine is too busy for
its wall-clock gates to mean anything — it detects this with `fit_state_trunc`, a
pure in-memory row no change in this project can move, and still enforces the
size gates. See `docs/perf-baseline.md`.

A `tools/list` handshake against a running `jev_mcp.py` returns exactly four tools:
`guardrail_command`, `search_agent_skills`, `search_target_files`, `select_model_tier`.

### Diagnostics & logging

- Server log (JSON lines): `<repo>\logs\jev_engine.log` (override with
  `JEV_MCP_LOG_FILE`). Records each tool call (redacted args, duration, the
  result's **key set** and coarse list sizes), each provider round, and one
  full traceback on failure. Written from an absolute, workspace-independent
  path. Set `JEV_MCP_LOG_PREVIEW=1` to also log a 1,000-character `result_preview`
  while debugging — off by default because the MCP runtime serializes the result
  again and a skill result carries kilobytes of file content.
- Redaction removes the credential and keeps the rest of the line:
  `curl -H 'Authorization: Bearer sk-…' https://x` keeps its command and URL. It is
  applied to args, to `result_preview`, and to error messages.
- Plugin log: `~\.config\opencode\logs\jev-plugin.log` (hook fired, client
  lifecycle, per-tool `action`/`confidence`, injection decision). It rotates at
  2 MB and never contains user prompt text — only its length and a short digest.
- `scripts\diag_mcp.py` reproduces a single tool call over stdio (identical to
  opencode's transport) against any `root_dir` + task; exit 0 = clean, 1 = error
  envelope or transport failure.

### Per-call cost

`search_agent_skills` and `search_target_files` both cache their filesystem
discovery (`scan_cache.py`): a second identical call costs a few `stat`s instead
of a full `rglob` of every configured skill directory or a forked
`git ls-files`. The cache stores **paths only** — file content is re-read on
every call — and a 5-second sliding TTL bounds staleness. Adding a skill,
changing `HEAD`/the git index/`.gitignore`, or creating a workspace file all
invalidate it. See `docs/perf-baseline.md` for the measured before/after.

### Mock mode (`JEV_MCP_MOCK=1`)

Runs the same tools end-to-end through a **deterministic offline judge**
(`mock.py`) that returns plausible `Noul`/`Choice` answers from keyword/overlap
heuristics. The mock scores options by inverse-frequency shared terms with the
task, and answers a presence `Noul` from the same candidate evidence, so it
behaves sensibly against real candidate previews. It exercises the **full
validation and policy pipeline** with the exact SDK object shapes. It exists for
tests, demos, and development without an API key — never as a production
decision engine: its routing accuracy is far below live Jev (see
`docs/perf-baseline.md`).

### Tests

`pytest` in `tests/`:

- `test_policy.py` — confidence formula, actions, thresholds, and that no
  unused escape-hatch constant came back.
- `test_validation.py` — fail-closed response validation (structure + failures),
  including the graded `Score` path.
- `test_validation_nan.py` — `NaN` / `inf` rejected in every numeric field, `bool`
  refused as a number, and the 7-level vs 2-level score mean tolerances.
- `test_envelope_shapes.py` — `set(result.keys())` is identical across every
  branch of all three deciding tools, and no `NaN` reaches the serialized
  envelope.
- `test_mcp_transport.py` — the **real** `jev_mcp.py` over real stdio JSON-RPC:
  `initialize`, all four tools in `tools/list`, a round trip per tool, and a
  refused `root_dir` arriving as `isError: true` rather than as a result.
- `test_limits.py` — `estimate_tokens` bit-identical to the original loop on a
  pinned corpus, truncation never over budget, surrogate pairs never split.
- `test_mock_tools.py` — offline tool runs (no key): backward-compat keys, new
  envelope keys, malformed→`INVALID_RESPONSE`, the `none` option, candidate
  truncation, and the evidence that reaches the model.
- `test_candidates.py` — front-matter split, `description:` preferred,
  truncation, unreadable files, `bound_candidates` returning `(kept, truncated)`.
- `test_deadline.py` — `JEV_MCP_TIMEOUT_MS` really bounds one call; the retry
  policy carries the total budget and does not retry timeouts.
- `test_breaker.py` — the breaker opens after N failures, admits one probe after
  the cooldown, and short-circuits auth failures for longer.
- `test_root_dir_allowlist.py` — allowed/denied `root_dir` cases, sibling-prefix
  and symlink escapes, `JEV_MCP_ALLOWED_ROOTS` handling.
- `test_settings_merge.py` — user/project merge per key, `""` clears a tier,
  post-read re-stat, the defensive snapshot, and that `opencode.json` is not a
  settings source.
- `test_client_cache.py` — the cached client is closed on invalidation and keyed
  on everything the SDK reads from the environment.
- `test_scan_cache.py` — the scanners do not repeat their work: a warm call
  never re-walks the tree or forks `git ls-files`, a new file invalidates the
  cache, nested default scan paths collapse, and `MAX_DISCOVERED_FILES` blocks
  `action: "auto"`.
- `test_hardening_integration.py` — family clustering with a confident primary
  and suppression with a weak one, the walk-depth bound, the settings mtime
  cache, and the client pool being closed rather than leaked.
- `test_mock_perf.py` — the offline judge's answers are pinned to a golden
  recorded before the optimization, and the state is tokenized once.
- `test_routing_quality.py` — the labelled fixture set over
  `scripts/eval_routing.py`: one Jev round trip per call, self-consistent
  rankings, a bounded input budget, and (with `JEV_ROUTING_LIVE=1`) live accuracy
  floors.
- `test_logging.py` — `result_keys` instead of a serialized result, the opt-in
  preview, credential redaction on the result path, and exactly one traceback
  per failure.
- `test_cleanup_phase8.py`, `test_config_dotenv.py`, `test_errors.py` — dotenv
  fallback semantics, error-envelope mapping, and the fail-closed seams.
- `test_live_smoke.py` — skipped unless `TYPESAFE_API_KEY` or `JEV_MCP_LIVE=1`.

`node tests\test_plugin.mjs` covers the plugin: client framing against a real
child process, `isError` unwrapping, reconnect, `applyTier` / `injectSkill` gating
tables, `isInside` traversal, the bounded scan, log rotation, and the drift check
against the installed copy.

`tests/conftest.py` grants `tempfile.gettempdir()` through
`JEV_MCP_ALLOWED_ROOTS` for every test, so pytest's `tmp_path` remains a
legitimate `root_dir` without weakening the allowlist, and clears the
config/client/settings/breaker/scan caches around every test. It also provides
the shared `stub_choice` fixture that answers a `Choice` question with a
hand-built distribution, so no test module re-implements that stub.

---

## Deploying the config (examples for GitHub)

The real OpenCode config files contain live API keys and live outside this repo, so the
repo carries **sanitized example copies** under `config/`:

| Example (committed) | Real install location |
|---|---|
| `config/opencode.example.json` | `C:\Users\<you>\.config\opencode\opencode.json` |
| `config/jevs_settings.example.json` | `<project-root>\jevs_settings.json` |
| `config/jev-plugin.example.js` | `C:\Users\<you>\.config\opencode\plugins\jev-plugin.js` |

See [`config/README.md`](config/README.md) for the full install guide.

The example intentionally omits the top-level `model` key so the default model comes
from your global OpenCode config (config files merge); add it back only to pin a
project-specific model — see `config/README.md`.

**Security:** never commit your real `opencode.json` or `.env` — both contain live keys
(`sk-...`, `apikey_...`). The `.gitignore` blocks `opencode.json` / `.opencode/` /
`config/opencode.local.json` so a real config can't accidentally slip into a commit.