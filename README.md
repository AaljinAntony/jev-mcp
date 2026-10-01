# jev-engine

**An MCP server that turns "which one?" into a number.**

`jev-engine` gives any MCP-capable agent five tools backed by
[Jev](https://github.com/typesafe-ai), a cheap and deterministic decision
engine from [TypeSafe AI](https://pypi.org/project/typesafe-sdk/). Each tool asks
one question and returns the odds, not a yes or no. Nothing is executed,
nothing is fetched, no code is written.

- **Server name:** `jev-engine` · **Transport:** stdio (JSON-RPC 2.0)
- **Language:** Python 3.11+ · **License:** MIT
- **Offline:** the whole test suite and a deterministic mock judge run without an
  API key

## Table of contents

- [Why this exists](#why-this-exists)
- [What you get](#what-you-get)
- [Support matrix](#support-matrix)
- [Quickstart](#quickstart)
- [MCP configuration by harness](#mcp-configuration-by-harness)
- [The five tools](#the-five-tools)
- [How a decision is made](#how-a-decision-is-made)
- [Configuration](#configuration)
- [Safety and privacy](#safety-and-privacy)
- [Cost and limits](#cost-and-limits)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [License](#license)

## Why this exists

An agent in a real repository keeps hitting the same four questions:

| Question | What happens without a judge | What happens with one |
|---|---|---|
| "Which of these 40 skills applies?" | Grep the filenames, inject three, hope | One ranked distribution, with a `none` option that means *none of these* |
| "Which files do I actually need to touch?" | Glob everything, blow the context window | A short ranked list, or an honest "nothing here is relevant" |
| "Is this shell command destructive?" | A blocklist of `rm -rf` patterns, always wrong somewhere | A calibrated risk probability you can threshold yourself |
| "I am connected to six MCP servers. Which one?" | Re-read every tool description in-context, every turn | One verdict, plus an `ambiguous` verdict when the honest answer is *ask* |

Every row fails the same way. A model asked to pick one option from a long list
will pick one, fluently, whether or not the evidence supports it. A decision
engine returns a distribution instead, and a distribution can be checked.

`jev-engine` makes that call, returns a typed verdict with an explicit
`confidence`, and fails closed: when the evidence is incomplete or the response
does not validate, it says so rather than guessing.

## What you get

Five tools, one network request each:

| Tool | Question it answers |
|---|---|
| [`guardrail_command`](#guardrail_command) | Is this shell command safe to run? |
| [`search_agent_skills`](#search_agent_skills) | Which skill / workflow / memory file applies to this task? |
| [`search_target_files`](#search_target_files) | Which workspace files are relevant to this task? |
| [`select_mcp_tools`](#select_mcp_tools) | Which MCP server, and which of its tools, fits this task? |
| [`select_model_tier`](#select_model_tier) | Is this a `fast`, `balanced` or `frontier` task? (off by default) |

There is also an optional **OpenCode plugin** that wires the judge into the
agent loop: it injects the right skill, judges prompt files the agent reads, and
can force the model tier per task. See [Support matrix](#support-matrix).

## Support matrix

| Tier | Meaning |
|---|---|
| ✅ **Verified** | Working end to end, exercised regularly |
| ⚠️ **Config provided, unverified** | The config dialect is correct and the server is harness-agnostic stdio, but not yet run by the maintainer on that harness |
| 🧪 **In testing** | Being brought up; expect rough edges |

| Harness | MCP tools | Plugin (auto injection + forced model routing) | Tier |
|---|---|---|---|
| **OpenCode** | works | works | ✅ Verified |
| **Antigravity** | works | n/a | ⚠️ Config provided |
| **Claude Code** | works | no plugin API | ⚠️ Config provided |
| **Cursor** | works | no plugin API | ⚠️ Config provided |
| **VS Code / Copilot** | works | no plugin API | ⚠️ Config provided |
| **Codex CLI** | works | no plugin API | ⚠️ Config provided |
| **Hermes Agent** | works | no plugin API | ⚠️ Config provided |

The five MCP tools are plain stdio, so they should work in any MCP client. What
is *not* portable is the **OpenCode plugin**, because OpenCode is currently the
only harness here with a plugin API that can read the user message, see tool
results, and change the model.

- Automatic skill injection, prompt-file judging, the system-prompt rules block
  and **automatic model switching** exist only in the OpenCode plugin, and are
  verified there.
- On every other harness those behaviours are **in testing**. Until they land,
  call the tools explicitly. `select_model_tier` and `search_agent_skills` work
  fine as on-demand tools anywhere.
- **Automatic model switching is the least mature feature in this project.**
  Turning on `enable_model_routing` forces `output.message.model` on every
  message. It is off by default. Read
  [what "routing on" actually does](config/README.md#what-routing-on-actually-does)
  before enabling it anywhere.

If you get a harness working that is listed as ⚠️, please open a PR moving it to
✅. That table should reflect reality.

## Quickstart

**Requirements**

- Python 3.11 or newer. This is a real floor, not a guess: `rpds-py`, a pinned
  transitive dependency, declares `Requires-Python >=3.11`, so on 3.10 the
  install fails outright.
- A [TypeSafe](https://pypi.org/project/typesafe-sdk/) API key.
- On Debian/Ubuntu: `python3-venv` (`sudo apt install python3-venv`). Ubuntu
  splits `ensurepip` into that separate package, so without it `python3 -m venv`
  fails with *"You may need to use sudo with that command"* before it creates
  anything. `virtualenv` works too: `pip install --user virtualenv`, then
  `virtualenv .venv`.
- `git` on `PATH` (optional; improves file discovery. The server falls back to a
  bounded filesystem walk).

Verified on Windows and Ubuntu 22.04: the install, the full offline suite, the
plugin suite and the performance gates all pass on both, and CI runs them on
`windows-latest` and `ubuntu-latest`. **macOS is untested.** The code takes no
platform-specific path outside the documented gates, but no run has confirmed
it.

### 1. Install

```bash
# macOS / Linux
git clone https://github.com/AaljinAntony/jev-mcp.git
cd jev-mcp
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

```powershell
# Windows
git clone https://github.com/AaljinAntony/jev-mcp.git
cd jev-mcp
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

### 2. Add your key

Edit `.env` and set one required variable:

```dotenv
TYPESAFE_API_KEY=<your TypeSafe API key>
```

The server loads `.env` **relative to its own module directory**, not the
working directory. One `.env` in the repo therefore works in every workspace,
every harness and every session.

### 3. Check the installation

```bash
.venv/bin/python scripts/doctor.py
```

```powershell
& .\.venv\Scripts\python.exe scripts\doctor.py
```

Read-only, no child process, no API call. Verifies the interpreter, both SDKs,
key *presence* (length and a 4-character suffix, never the value), settings
resolution, the `root_dir` allowlist, log writability, threshold consistency,
and plugin drift. Exits `0` when healthy, `1` otherwise, with a one-line fix per
finding.

```
interpreter
  [ok] python 3.14.7 at /path/to/jev-mcp/.venv/Scripts/python.exe
dependencies
  [ok] typesafe_sdk importable
  [ok] mcp.MCPServer importable
  [ok] jev_mcp imports
        5 tools registered
...
  healthy
```

### 4. Wire it into your harness

Find your harness in
[MCP configuration by harness](#mcp-configuration-by-harness) and paste one
block. Ready-made copies live in [`config/`](config/README.md).

### 5. Try it

```bash
# Judge a shell command
.venv/bin/python jev_engine.py verify "git reset --hard HEAD~1"
```

Or from inside your agent, in any MCP client:

```
Use guardrail_command on: git reset --hard HEAD~1
Use select_mcp_tools to pick a server for: create a pull request for my staged changes
```

## MCP configuration by harness

There are only **five config formats** across every harness. Find yours, paste
its block.

Two rules apply to all of them:

1. **The key goes in `<REPO_DIR>/.env`, never in the harness config.** Putting
   `TYPESAFE_API_KEY` in an `env` / `environment` block makes harnesses that
   interpolate a missing variable pass an **empty string**, which shadows the
   `.env` and breaks every live call. There is a full explanation in
   [`config/README.md`](config/README.md#the-api-key-goes-in-env-never-in-the-harness-config).
2. Replace `<REPO_DIR>` with the absolute path to your clone. Forward slashes
   are safest inside JSON on every platform.

### Claude Code, Cursor, Antigravity, Claude Desktop

Format: `mcpServers` (JSON).

```json
{
  "mcpServers": {
    "jev-engine": {
      "command": "<REPO_DIR>/.venv/bin/python",
      "args": ["<REPO_DIR>/jev_mcp.py"]
    }
  }
}
```

On Windows, `command` is `<REPO_DIR>/.venv/Scripts/python.exe`.

| Harness | File |
|---|---|
| Claude Code | `.mcp.json` in the project, or `~/.claude.json` for all projects |
| Cursor | `.cursor/mcp.json` in the project, or `~/.cursor/mcp.json` |
| Antigravity | `~/.gemini/config/mcp_config.json` |
| Claude Desktop | `%APPDATA%\Claude\claude_desktop_config.json` |

Templates: [`config/claude-code.example.json`](config/claude-code.example.json),
[`config/cursor.example.json`](config/cursor.example.json),
[`config/antigravity.example.json`](config/antigravity.example.json)

> **Antigravity** launches the server from the repository root rather than the
> active workspace. Add your project roots to `JEV_MCP_ALLOWED_ROOTS` in `.env`:
> `JEV_MCP_ALLOWED_ROOTS=/home/you/code/project-one;/home/you/code/project-two`
> (`;` on Windows, `:` on POSIX).

### VS Code, GitHub Copilot

Format: `servers` (JSON).

> **The key is `servers`, not `mcpServers`.** This is the single most common
> reason a VS Code MCP server "does not exist". The schema is also strict:
> unknown keys invalidate the file.

```json
{
  "servers": {
    "jev-engine": {
      "type": "stdio",
      "command": "<REPO_DIR>/.venv/bin/python",
      "args": ["<REPO_DIR>/jev_mcp.py"]
    }
  }
}
```

Install to `.vscode/mcp.json` in the workspace (committable. It contains no
secrets), or use **MCP: Open User Configuration** for a per-user copy. VS Code
adds a **Start** button in the file once it parses.

Template: [`config/vscode.example.json`](config/vscode.example.json)

### OpenCode

Format: `mcp` object (JSON). ✅ Verified, and the only harness with plugin
support.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "jev-engine": {
      "type": "local",
      "enabled": true,
      "command": [
        "<REPO_DIR>/.venv/bin/python",
        "<REPO_DIR>/jev_mcp.py"
      ],
      "environment": {
        "JEV_MCP_MODEL": "jev-latest",
        "JEV_MCP_TIMEOUT_MS": "30000",
        "JEV_MCP_AUTO_ACCEPT": "0.8",
        "JEV_MCP_REVIEW_AT": "0.5"
      }
    }
  }
}
```

Install to `~/.config/opencode/opencode.json`. Note `command` is an **array**
here, unlike every other format, and the server key is `type: "local"`.

> **OpenCode's config schema is strict** (`additionalProperties: false`). An
> unknown top-level key, a `jev_settings` block for instance, invalidates the
> entire file and the MCP server silently disappears from the server list. Jev
> settings go in `jevs_settings.json`, never in `opencode.json`.

Template: [`config/opencode.example.json`](config/opencode.example.json)

### Codex CLI

Format: TOML. Its IDE extension shares the same file.

```toml
[mcp_servers.jev-engine]
command = "<REPO_DIR>/.venv/bin/python"
args = ["<REPO_DIR>/jev_mcp.py"]
```

```bash
codex mcp add jev-engine -- "<REPO_DIR>/.venv/bin/python" "<REPO_DIR>/jev_mcp.py"
```

Note the **snake_case** table name (`mcp_servers`, not `mcp-servers`) and that
TOML uses `=`, not `:`. A syntax error here breaks the CLI *and* the extension.

Template: [`config/codex.example.toml`](config/codex.example.toml)

### Hermes Agent

Format: YAML.

```yaml
mcp_servers:
  jev-engine:
    command: <REPO_DIR>/.venv/bin/python
    args:
      - <REPO_DIR>/jev_mcp.py
```

```bash
hermes mcp add jev-engine --command "<REPO_DIR>/.venv/bin/python"
```

Snake_case again. Hermes also imports a Claude Code `mcpServers` block with
`hermes import-agent claude-code`.

Template: [`config/hermes.example.yaml`](config/hermes.example.yaml)

### Verify the wiring

```bash
.venv/bin/python scripts/doctor.py                       # install health
.venv/bin/python scripts/diag_mcp.py --tool search_agent_skills \
    --task "fix ui bug" --root_dir <workspace> --mock   # one real stdio call
```

`diag_mcp.py` spawns `jev_mcp.py` and speaks the same protocol your harness
does. Add `--mock` to run it with no API key at all.

## The five tools

Two terms show up in the output fields. **`Choice`** is a question with named
options, and the model can only pick from the options it is given. **`Noul`** is
a yes/no question that returns a probability instead of a boolean.

Every tool returns a JSON object. When something fails you get MCP
`isError: true` whose text is a typed envelope. The runtime prefixes
`Error executing tool <name>: `, so parse past that:

```json
{ "error": { "code": "TIMEOUT", "message": "...", "retryable": true } }
```

Every tool also reports **`action`** (`auto` / `review` / `escalate`) and
**`confidence`**. Below `auto`, treat the result as a hint rather than an
instruction.

### `guardrail_command`

Is this shell command safe to run?

| Parameter | Type | Required |
|---|---|---|
| `command` | string | yes |

Asks two questions: is it `is_destructive`, and does it `modifies_git`.

```json
{
  "safe": true,
  "destructive_prob": 0.01,
  "git_modify_prob": 0.01,
  "action": "auto",
  "confidence": 0.99,
  "reason_codes": [],
  "truncated": false,
  "coverage": { "complete": true, "original_chars": 32, "evaluated_chars": 32 },
  "model": "jev-latest",
  "usage": { "input_tokens": 120, "output_tokens": 12 }
}
```

`safe` is true only when `action == "auto"` **and** both risk probabilities are
below `0.20`. A confident "destructive" judgment (≥ 0.50) escalates; ≥ 0.20
reviews.

> **This is advisory, not a permission system.** It tells your agent what the
> judge thinks; it cannot stop a tool call. Real enforcement needs a harness
> permission rule or a wrapper. See [Safety and privacy](#safety-and-privacy).

### `search_agent_skills`

Which skill, workflow or memory file applies to this task?

| Parameter | Type | Default | Required |
|---|---|---|---|
| `task` | string | — | yes |
| `root_dir` | string | `"."` | no |
| `task_file` | string | `""` | no |

Scans `.agents/skills`, `.agents/workflows`, `.agents/memory`, `.opencode/skills`,
`skills` and `.agents`, plus any `scan_paths` from `jevs_settings.json`. Returns
the winning Markdown, inlined up to 6 000 characters per resource.

```json
{
  "matched": true,
  "count": 2,
  "primary": { "name": "godot-ui-theme", "file": ".agents/skills/godot-ui-theme/SKILL.md", "content": "..." },
  "resources": [ { "name": "...", "file": "...", "content": "..." } ],
  "summary": "Found 2 relevant agent resource(s): godot-ui-theme, godot-ui-layout",
  "primary_probability": 0.91,
  "ranked": [ { "file": ".agents/skills/godot-ui-theme/SKILL.md", "probability": 0.91 } ],
  "candidates_considered": 12,
  "candidates_evaluated": 12,
  "candidates_truncated": false,
  "reason_codes": [],
  "action": "auto",
  "confidence": 0.87,
  "model": "jev-latest",
  "usage": { "input_tokens": 2400, "output_tokens": 24 }
}
```

`primary` **is** `resources[0]`. `ranked` is the full ranking, not just the
winner.

Each `Choice` option carries the document's own summary, and a `SKILL.md`
contributes its front-matter `description`. Not its filename: options that all
read `SKILL.md` are indistinguishable, and a `Choice` can only pick from the
options it is given.

### `search_target_files`

Which workspace files are relevant?

Same parameters as `search_agent_skills`. Asks two questions over one state:
`target_file` (a `Choice`) and `is_relevant` (a presence `Noul`). The `Noul` is
what stops a confident winner among poor options from reading as a match.

```json
{
  "matched": true,
  "files": ["src/core/player_controller.gd"],
  "exists": "answered",
  "probability": 0.94,
  "relevance_prob": 0.88,
  "ranked": [ { "file": "src/core/player_controller.gd", "probability": 0.94 } ],
  "candidates_truncated": false,
  "action": "auto",
  "confidence": 0.90
}
```

`exists` is the field to branch on. `matched` alone is not enough:

| `exists` | Meaning | `matched` |
|---|---|---|
| `answered` | A file was chosen **and** `relevance_prob >= 0.5` | `true` |
| `partial` | A file was chosen but the presence `Noul` disagreed: confident among options that do not fit | `false` |
| `absent` | The model picked `none` confidently, so nothing here is relevant | `false` |
| `no_candidates` | Discovery found nothing to offer, so **no API call was made** | `false` |

`partial` is the fail-closed answer a `none` option cannot reach: the model
named a file it does not actually endorse.

### `select_mcp_tools`

Which MCP server, and which of its tools, fits this task?

| Parameter | Type | Default | Required |
|---|---|---|---|
| `task` | string | — | yes |
| `mcps` | array of objects, or `null` | `null` | no |
| `mcps_json` | string | `""` | no |
| `root_dir` | string | `"."` | no |
| `task_file` | string | `""` | no |
| `max_tools` | int (1–50) | `20` | no |
| `max_servers` | int (1–20) | `5` | no |

**You must pass the roster.** A `Choice` can only pick from the options it is
given, and the agent already holds this list:

```json
[
  { "name": "git", "description": "Local git operations",
    "tools": [ { "name": "git_commit", "description": "Create a commit" } ] },
  { "name": "github", "description": "GitHub API",
    "tools": [ { "name": "create_pull_request", "description": "Open a PR" } ] }
]
```

If your client cannot send an array parameter, send the same JSON as
`mcps_json`.

```json
{
  "matched": true,
  "exists": "answered",
  "primary": { "server": "github", "name": "github", "probability": 0.87 },
  "servers": [ { "server": "github", "name": "github", "probability": 0.87 },
               { "server": "git", "name": "git", "probability": 0.09 } ],
  "tools": [ { "server": "github", "tool": "create_pull_request", "probability": 0.72 } ],
  "ranked_tools": [ { "server": "github", "tool": "create_pull_request", "probability": 0.72 } ],
  "excluded": [ { "server": "jev-engine", "reason": "self" } ],
  "probability": 0.87,
  "relevance_prob": 0.91,
  "decisive_prob": 0.84,
  "action": "auto",
  "confidence": 0.81
}
```

Read `exists`:

| `exists` | Meaning | What to do |
|---|---|---|
| `answered` | One server is the right one | Use `primary` and `tools` |
| `ambiguous` | Two or more are comparably usable. `decisive_prob` is low, or the top two are within 0.10 | No tool chosen. Decide yourself, or ask |
| `absent` | The `Choice` picked `none` confidently | No supplied server has what this needs; use your built-in tools |
| `partial` | A server was chosen but `is_relevant` disagreed | Treat as absent |
| `no_candidates` | Nothing was left to choose from | `excluded` says why; no API call was made |

`ambiguous` is decided from the **gap** between the top two probabilities
rather than from confidence. A tight 0.46/0.42 split normalises to a *high*
confidence, while a flat five-way split normalises to a low one, so the gap is
what actually distinguishes them.

> **The judge never recommends itself.** `jev-engine` is excluded from its own
> candidates by name, prefix and case, so `jev-engine-local` is out too. A judge
> that suggests itself sends your agent back into the judge and the loop never
> terminates. Every exclusion is reported in `excluded`, never dropped silently.

Add servers to `ignore_mcps` in `jevs_settings.json` to exclude more, by exact
name or glob (`git*`).

### `select_model_tier`

`fast`, `balanced` or `frontier`?

| Parameter | Type | Required |
|---|---|---|
| `task` | string | yes |

**Disabled by default.** It is gated on `enable_model_routing` in
`jevs_settings.json`. When off it returns immediately with **no API call**:

```json
{ "enabled": false, "recommended_tier": null, "recommended_model": null,
  "model_map": {}, "action": "review", "confidence": null }
```

When on:

```json
{
  "enabled": true,
  "task": "Refactor auth middleware to support OAuth2 refresh tokens",
  "recommended_tier": "frontier",
  "recommended_model": "<your frontier model id>",
  "model_map": { "fast": "...", "balanced": "...", "frontier": "..." },
  "action": "auto",
  "confidence": 0.93
}
```

This tool only ever **recommends**. Applying the switch requires the OpenCode
plugin, which is the only harness here that can change the model
programmatically. Everywhere else, call it and act on the result yourself.

### `task_file` for saved prompts

`search_agent_skills`, `search_target_files` and `select_mcp_tools` accept a
`task_file`: the path to a prompt or plan kept on disk.

```json
{ "task": "do phase 2", "root_dir": ".", "task_file": ".agent_plans/phase_2.md" }
```

Without it the judge receives only the *path*, "do phase 2 of
`.agent_plans/phase_2.md`", and has no statement of the task to reason from, so
it ranks everything at low confidence. `task` and the file are **combined**,
never swapped: "do phase 2 of X" is a real question whose subject only exists
in the file.

The file is confined exactly like `root_dir`, read head-only to 8 000
characters, and passed through a Markdown preview so front matter does not spend
question budget. An LLM-supplied path is an LLM-supplied `root_dir` with extra
steps, so it gets the same allowlist.

## How a decision is made

One request per tool call, however many questions ride along. The pipeline is
the same every time:

1. **Discover candidates** and turn each into short evidence, a front-matter
   `description` where present, else the head of the file. A `Choice` can only
   pick from the options it is given.
2. **Fit the state** to the token budget, truncating if needed.
3. **Ask.** `Noul` for a yes/no probability, `Choice` for a pick over named
   options. Every `Choice` is offered a **`none`** option, so "nothing here
   applies" is an answer the model can give rather than something you infer.
4. **Validate, fail closed.** The response is checked against the questions
   *before any policy number is read*. Probabilities must cover exactly the
   criteria, sum to 1, and the selected choice must be the argmax. Anything else
   raises `INVALID_RESPONSE`, and it is never read as `safe: true`.
5. **Map to an action.**

### Confidence to action

```
confidence >= JEV_MCP_AUTO_ACCEPT (0.8)  ->  "auto"
confidence >= JEV_MCP_REVIEW_AT    (0.5)  ->  "review"
otherwise                                ->  "escalate"
```

Confidence is normalised from the distribution as
`(top − 1/n) / (1 − 1/n)`, so 0 when uniform and 1.0 for a single option.

Three fail-closed rules:

- **Truncated context never yields `auto`.** Incomplete evidence downgrades to
  `review`. That covers context truncation, candidate truncation, and a tool
  list cut at `max_tools`.
- **Combining judgments never softens the strictest one.**
- **A malformed response is an error, not a low score.** No code path skips
  validation.

Thresholds must satisfy `0 <= review_at <= auto_accept <= 1`. An invalid value
fails at startup with `CONFIG_ERROR` rather than silently mis-routing.

## Configuration

Two files: `.env` for the server, `jevs_settings.json` for per-project
behaviour.

### `.env`, server environment

Resolved from the module's own directory, so one file serves every workspace.
An injected environment variable always wins; a *blank* injected value is
treated as absent so the file can supply it.

| Variable | Default | Meaning |
|---|---|---|
| `TYPESAFE_API_KEY` | *(required)* | Provider key. Missing plus `JEV_MCP_MOCK=0` gives `CONFIG_ERROR`. |
| `JEV_MCP_MODEL` | `jev-latest` | Model used for `system_one` calls. |
| `JEV_MCP_TIMEOUT_MS` | `30000` | Total per-tool-call budget in ms, retries included. Note 1. |
| `JEV_MCP_AUTO_ACCEPT` | `0.8` | Confidence at or above which a decision is `auto`. |
| `JEV_MCP_REVIEW_AT` | `0.5` | Confidence below which a decision `escalate`s. |
| `JEV_MCP_MOCK` | `0` | `1` runs an offline deterministic judge. Tests and demos only. |
| `JEV_MCP_ALLOWED_ROOTS` | *(empty)* | Extra directories an LLM-supplied `root_dir` / `task_file` may resolve inside. Note 2. |
| `JEV_MCP_BREAKER_THRESHOLD` | `3` | Consecutive provider failures before the breaker opens. |
| `JEV_MCP_BREAKER_COOLDOWN_S` | `30` | Seconds before one probe call is let through. |
| `JEV_MCP_AUTH_COOLDOWN_S` | `300` | Longer window after a 401/403. A bad key does not fix itself in 30 seconds. |
| `JEV_MCP_LOG_FILE` | `<repo>/logs/jev_engine.log` | Absolute log path. Always written cwd-independently. |
| `JEV_MCP_LOG_PREVIEW` | `0` | `1` also logs a 1 000-character result preview. Note 3. |

Note 1: it is a total budget, not a per-attempt timeout, and it is enforced in
mock mode too.

Note 2: `os.pathsep`-separated, `;` on Windows and `:` on POSIX. Empty means
only the process working directory and its ancestors below `$HOME`.

Note 3: off by default, because the result is serialized twice and a skill
result carries kilobytes.

`.env.example` documents all of these inline.

### `jevs_settings.json`, per project

```jsonc
{
  "enable_model_routing": false,   // master switch for select_model_tier
  "models": {
    "fast":     "",                 // "provider/model-id" used for FORCED switching
    "balanced": "",
    "frontier": ""
  },
  "scan_paths": [],                 // extra skill dirs, appended to the defaults
  "ignore_mcps": [],                // names/globs select_mcp_tools must never offer
  "judge_read_prompts": true,       // plugin only
  "inject_agent_instructions": true // plugin only
}
```

Discovered from four locations and **merged per key**, not first-file-wins:

1. `<project>/jevs_settings.json`
2. `<project>/.opencode/jevs_settings.json`
3. `<repo>/jevs_settings.json`, the server's own directory, so CWD does not matter
4. `~/.config/opencode/jevs_settings.json`

A project file that sets only `enable_model_routing` therefore does **not**
discard your user-level `models` map. That matters, because a default-valued
project `jevs_settings.json` is safe to commit and would otherwise shadow the
user config permanently.

`opencode.json` is deliberately **not** a settings source. Its schema rejects
unknown top-level keys, so a `jev_settings` block there would stop the MCP
server from loading at all.

This file contains only model IDs and paths, no secrets, so it is safe to
commit if you want to share a team default. Template:
[`config/jevs_settings.example.json`](config/jevs_settings.example.json).

Full reference:
[`config/README.md`](config/README.md#jevs_settings-reference-jevs_settingsjson).

## Safety and privacy

### What the server reads

Only Markdown, text and code files under the configured scan directories, plus
the file heads used as `Choice` evidence. It never executes anything, never
writes, and never makes a network request except to the TypeSafe API.

### `root_dir` is confined to an allowlist

`root_dir` and `task_file` are LLM-supplied, so they are treated as untrusted
input. Allowed:

- the process working directory and anything under it;
- any **ancestor** of the working directory that sits **below your home
  directory**. Hosts launch the server with `cwd` set below the project root,
  and a session legitimately asks about a parent of that;
- anything under `JEV_MCP_ALLOWED_ROOTS`.

Everything else is rejected with `INVALID_INPUT`. Containment uses
`Path.relative_to`, never `startswith`, so `<root>-evil` and a symlink pointing
outside the root are both refused. The OS's own directories are refused by name
as a second gate, even if you allowlist them: `C:\Windows`, `C:\Program Files`
and a drive root on Windows; `/etc`, `/proc`, `/sys`, `/dev`, `/var`, `/opt`,
`/srv` and `/root` on Linux and macOS.

**The ancestor walk stops at two boundaries, and both are load-bearing.** It
never reaches the filesystem root, because a base of `/` would make every
absolute path on the machine a member of the allowlist. And it never reaches
`$HOME` or above, because the usual checkout (`~/code/project`) puts your whole
home directory among the ancestors, which is exactly why running the server from
inside `$HOME` would otherwise let a prompt-injected `root_dir=$HOME` walk it.
The working directory itself is always allowed, even when it is `$HOME`: that is
the one directory your session already has.

This is an **allowlist**, not a denylist, on purpose. A denylist cannot
enumerate every sensitive path, and an LLM-supplied path should carry no more
privilege than the session's own working directory.

### What is logged

`<repo>/logs/jev_engine.log`, rotating at 2 MB with 3 backups, plus stderr. Each
tool call records its **key set** and coarse list sizes, not the serialized
result, which would put kilobytes of file content in your log. Set
`JEV_MCP_LOG_PREVIEW=1` to opt into a 1 000-character preview while debugging.

Credentials are **redacted in place**:
`curl -H 'Authorization: Bearer sk-…' https://x` keeps its command and URL and
loses only the token. Redaction applies to tool arguments, error messages and
previews.

The OpenCode plugin never writes prompt text at all, only `len=N` and a short
digest.

### This is advisory, not a permission system

`guardrail_command` tells your agent what the judge thinks. It **cannot stop a
tool call.** The host owns enforcement, and only OpenCode's own permission rules
or a wrapper can actually block a destructive command. If you need a hard
guarantee, do not rely on this server for it.

## Cost and limits

**One API request per tool call**, regardless of how many questions it asks.
`select_mcp_tools` puts four questions in one request and still costs one round
trip.

| Tool | Measured input tokens / call |
|---|---|
| `guardrail_command` | ~120 |
| `select_model_tier` | ~200 |
| `select_mcp_tools` | ~980 (roster-dependent) |
| `search_agent_skills` | 2 400 to 6 800 |
| `search_target_files` | up to ~12 900 |

The cost is deliberate. Each candidate path is given real text so the judge can
tell candidates apart: measured over the labelled fixture set,
`search_target_files` at a 40 000-character preview budget scores top-1 0.778 at
12 885 tokens, while paths only scores 0.444 at 1 410 tokens. The budget is a
dial, [`MAX_TOTAL_PREVIEW_CHARS`](docs/ARCHITECTURE.md#bounds). Lower it
deliberately if per-call cost matters more than the last few points of accuracy.

**Caps.** 64 000 total tokens, 250 `Choice` options, 120 file reads and 40 000
preview characters per call, 5 000 discovered files, 64 MCP servers, 6 000
characters per injected resource. Candidates beyond a cap are *reported*
(`candidates_considered` / `candidates_evaluated` / `candidates_truncated`,
`reason_codes`) and block `action: "auto"`, never silently dropped.

**The circuit breaker.** After `JEV_MCP_BREAKER_THRESHOLD` consecutive failures
the breaker opens and further calls fail immediately with a retryable `TIMEOUT`
that says when to come back, instead of paying three attempts plus backoff on
every call. After the cooldown exactly one probe is admitted. Auth failures use
a longer window, because a bad key does not fix itself in 30 seconds.

**Mock mode.** `JEV_MCP_MOCK=1` swaps the API for a deterministic offline judge
so you can develop, test and demo with no key and no network. It returns real
SDK answer objects, so the whole validation and policy pipeline still runs. Its
routing accuracy is far below live Jev. It is documentation and tests, not a
production decision engine.

## Troubleshooting

Run `scripts/doctor.py` first. It is read-only and resolves most of this table
on its own.

| Symptom | Cause |
|---|---|
| `CONFIG_ERROR: TYPESAFE_API_KEY ... is not configured` on every call | A harness `env` block set the key to `""`, shadowing `.env`. Remove `TYPESAFE_API_KEY` from the harness config. |
| Server absent from the harness list | Wrong config path, `<REPO_DIR>` unreplaced, or (OpenCode) an unknown top-level key invalidating the whole file. |
| VS Code cannot see it | The key is `servers`, not `mcpServers`. |
| Codex cannot see it | Table must be `mcp_servers`; TOML uses `=`, not `:`. |
| `INVALID_INPUT: root_dir is outside the allowed roots` | The host launched the server from an unexpected directory. Add the project root to `JEV_MCP_ALLOWED_ROOTS`. |
| `TIMEOUT` on every call, circuit open | The TypeSafe endpoint is unreachable or the key is bad. The envelope says how long to wait. |
| `AUTH_ERROR` | The key was rejected. Rotate it. |
| `no skill to inject: primary=none` in the plugin log | Working as intended. The judge answered "nothing here applies". Not an error. |
| Plugin edits seem to do nothing | You edited the installed copy instead of the example, or did not restart. See [`config/README.md`](config/README.md#4-optional--the-opencode-plugin). |

Reproduce a single call over the real transport with:

```bash
.venv/bin/python scripts/diag_mcp.py --tool select_mcp_tools \
    --task "commit the staged changes" --mcps @roster.json --mock
```

## Development

```bash
# Syntax check every module
.venv/bin/python -m py_compile jev_engine.py jev_mcp.py jev_validation.py \
    jev_errors.py policy.py limits.py config.py mock.py jev_logging.py \
    candidates.py scan_cache.py scripts/*.py

# Offline test suite - no API key needed
.venv/bin/python -m pytest tests -q

# Offline plugin tests - spawns scripts/stub_mcp.js, no API key, no OpenCode
node tests/test_plugin.mjs

# Performance gates. Exit 0 pass, 1 regression, 2 machine too loaded to judge.
.venv/bin/python scripts/bench_jev.py --assert

# Routing quality over the labelled fixture sets
.venv/bin/python scripts/eval_routing.py --mode mock
```

Everything above runs **offline**. You do not need a key to contribute.

| Document | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Module map, decision flow, error taxonomy, validation invariants, every bound, plugin internals, test map |
| [docs/perf-baseline.md](docs/perf-baseline.md) | Measurements and the reasoning behind each optimisation, as ratios |
| [docs/LICENSING.md](docs/LICENSING.md) | Why MIT, and what the other licences would have cost |
| [config/README.md](config/README.md) | Install guide for every harness, settings reference, plugin lifecycle |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Dev loop, conventions, PR expectations |
| [SECURITY.md](SECURITY.md) | What counts as a security bug, and how to report one |

## License

**MIT.** See [`LICENSE`](LICENSE).

Parts of this project were derived from other MIT-licensed projects. Those
notices, with full license texts, are collected in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md). The reasoning behind the
licence choice is in [docs/LICENSING.md](docs/LICENSING.md).

## Credits

- **[Jev / TypeSafe AI](https://pypi.org/project/typesafe-sdk/)**, the decision
  engine. Everything here wraps `TypeSafeClient.system_one`.
- **[burnigtm/jev-mcp](https://github.com/burnigtm/jev-mcp)** and
  **[jkudish/jev-mcp](https://github.com/jkudish/jev-mcp)**, MIT-licensed
  projects whose TypeScript implementations informed the validation, policy,
  limits, error and mock modules. See
  [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
- **[Model Context Protocol](https://modelcontextprotocol.io)**, the transport
  that makes any of this portable.