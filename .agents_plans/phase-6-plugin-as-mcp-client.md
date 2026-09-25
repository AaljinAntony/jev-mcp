# Phase 6 — Rewrite the Plugin as a stdio MCP Client

**Goal:** delete the plugin's duplicated Python and make drift structurally impossible,
while fixing the path traversal, the missing confidence gate, and the per-message
process spawn.

**Closes:** findings #14, #15, #16.

**Depends on:** Phase 2 (error envelopes), Phase 3 (the plugin now consumes a real
`action`/`confidence`).

**Risk:** medium-high. This is the only phase that changes a file outside the repo
(`~/.config/opencode/plugins/jev-plugin.js`) and the one with the least test coverage
today. Phase 1's drift guard is what keeps the installed copy honest afterwards.

---

## The state of things

### The drift is real and measured

```
installed: C:\Users\aalji\.config\opencode\plugins\jev-plugin.js   427 lines
           SHA256 BA49FEBCB8C01A274631A0D7244EFD499B7C07BE2F24C149DA6436F2D8C46BED
example:   D:\mcp\jev-typesafe-mcp\config\jev-plugin.example.js     510 lines
           SHA256 E1425B6B05D78325E4864DD3700AAC01B33AE2B47DFCB21A9516F5CA311B99B1
```

The installed file is missing everything the example added, **and** has problems the
example fixed:

| Concern | Example (good) | Installed (live) |
|---|---|---|
| `scanResourceFiles` recursion | `maxDepth = 6` (`:185-204`) | **unbounded** (`:148-166`) |
| `pluginLog` growth | rotates at 2 MB (`:52-62`) | **never rotates** (`:37-48`) |
| Oversized prompt | `MAX_PROMPT_CHARS` cap (`:42,433-436`) | **none** |
| SDK client | `timeout=3.0` + `RetryPolicy` (`:291-300`) | **bare** `TypeSafeClient(api_key=...)` (`:247`) — SDK default timeout is 30 s |
| Child kill timeout | `CHILD_KILL_MS = 12_000`, aligned to the Python retry budget (`:43,332-335`) | **4 000 ms** against a 30 s SDK timeout (`:276-279`) — every retry path is killed before it can report |
| Skill content | `MAX_INJECT_CHARS = 6_000` (`:41,486-489`) | **unbounded** (`:403-411`) |
| `splitModelId` on a bare id | returns `null` → skip the switch (`:387-394`) | returns `{providerID: id, modelID: id}` (`:316-323`) → **assigns a bogus model object and breaks opencode's routing** |
| venv / `.env` path | `REPO_ROOT` relative to the plugin (`:28-30`) | hardcoded `D:\mcp\jev-typesafe-mcp\...` (`:114,121`) |
| `.env` key regex | `/TYPESAFE_API_KEY\s*=\s*['"]?([^'"\s\n]+)['"]?/` handles quotes | `/TYPESAFE_API_KEY\s*=\s*(.+)/` → a quoted key becomes `"sk-abc"` **with the quotes**, i.e. a 401 |
| Confidence gate | none | **none** — injects whatever `choice` returns |
| Drift detection | — | **none** (Phase 1 adds it) |

The installed `.env` regex bug and the `splitModelId` bug are the two that break
things outright.

### And the plugin re-implements logic that already exists

The plugin's inline Python (`config/jev-plugin.example.js:246-312`) builds its own
`Choice` questions, calls `system_one` with no `RetryPolicy`/`timeout`, and reads
back only `get_val(answers.get("target"))`. It therefore has **none** of:

- `fit_state` token budgeting
- `validate_response` fail-closed checking
- `action_from_confidence` / `require_complete_context` / `worst_action`
- `candidates_truncated` awareness
- the `none` escape hatch semantics
- the settings cache
- the error taxonomy

The example's own TODO says this (`:236-243`):

> TODO: This spawns a new Python process per message, which is slow (~1-3s).
> A better approach would be to call the jev-engine MCP server's tools
> (search_agent_skills, select_model_tier) via the MCP protocol…

This phase does that.

---

## Architecture

```
chat.message hook
      │
      ├─ extractUserPrompt(input, output)          [unchanged]
      ├─ loadSettings()                            [simplified: no Python/env probing]
      ├─ getClient()  ── lazily spawns ONCE ──►  python jev_mcp.py
      │      (one long-lived child, newline-delimited JSON-RPC over stdio)
      │      ├─ initialize + notifications/initialized
      │      └─ reused across every message: pooled connection, warm client cache
      │
      ├─ tools/call search_agent_skills {task, root_dir}
      ├─ tools/call select_model_tier  {task}          (only when routing is on)
      │
      ├─ model switch  ← only when action == "auto" and confidence >= threshold
      └─ skill inject  ← only when action == "auto" and confidence >= threshold
```

Per-message cost today: one `python -c` interpreter start + SDK import + one HTTPS
round trip. After: one HTTPS round trip on a warm pooled connection. Roughly
200–400 ms of interpreter and import overhead removed per message.

**There is no `@modelcontextprotocol/sdk` in opencode's `node_modules`** (verified),
so the client is hand-rolled: `child_process.spawn` plus newline-delimited JSON-RPC.
`scripts/diag_mcp.py` already speaks this exact protocol in 30 lines of Python, so
the wire format is proven.

---

## Task 6.1 — The MCP client

New section in `config/jev-plugin.example.js`.

```js
/**
 * A minimal stdio JSON-RPC client for the jev-engine MCP server.
 *
 * No @modelcontextprotocol/sdk is available inside opencode's plugin sandbox,
 * so this is hand-rolled: newline-delimited JSON-RPC 2.0 over a child process's
 * stdin/stdout, which is exactly what scripts/diag_mcp.py speaks.
 */
class JevMcpClient {
  constructor(pythonPath, serverPath, env, log) {
    this.pythonPath = pythonPath;
    this.serverPath = serverPath;
    this.env = env;
    this.log = log;
    this.proc = null;
    this.buffer = "";
    this.nextId = 1;
    this.pending = new Map();
    this.ready = null;
  }

  start() { /* idempotent: returns this.ready */ }
  request(method, params, timeoutMs) { /* returns a promise */ }
  notify(method, params) { /* fire-and-forget */ }
  callTool(name, args, timeoutMs) { /* returns the parsed text body */ }
  stop() { /* kill + reject all pending */ }
}
```

### Requirements

**`start()`** — spawn lazily, once, and reuse:

```js
  start() {
    if (this.ready) return this.ready;
    this.ready = new Promise((resolve, reject) => {
      let proc;
      try {
        proc = spawn(this.pythonPath, [this.serverPath], {
          env: this.env,
          stdio: ["pipe", "pipe", "pipe"],
        });
      } catch (err) {
        reject(new Error(`spawn failed: ${err.message}`));
        return;
      }
      this.proc = proc;

      proc.stdout.on("data", (chunk) => this.#onData(chunk));
      proc.stderr.on("data", (d) => {
        const text = String(d).trim();
        if (text) this.log(`server stderr: ${text.slice(-300)}`);
      });
      proc.on("error", (err) => this.#failAll(err));
      proc.on("exit", (code, signal) => {
        this.log(`server exited code=${code} signal=${signal}`);
        this.proc = null;
        this.#failAll(new Error(`jev-engine exited (${code ?? signal})`));
        this.ready = null; // next message respawns
      });

      // The server logs its own start to stdout? No -- stdout is JSON-RPC only.
      this.#raw({
        jsonrpc: "2.0",
        id: this.nextId++,
        method: "initialize",
        params: {
          protocolVersion: "2025-06-18",
          capabilities: {},
          clientInfo: { name: "jev-plugin", version: "1" },
        },
      }, 10_000)
        .then((res) => {
          this.notify("notifications/initialized");
          resolve(res);
        })
        .catch(reject);
    });
    return this.ready;
  }
```

The `#` private fields are fine in the opencode plugin runtime (ESM, Node 18+); the
example already uses `import.meta.url`, so the module system is modern.

**`#onData`** — frame on newlines, dispatch by `id`:

```js
  #onData(chunk) {
    this.buffer += chunk.toString("utf-8");
    let idx;
    while ((idx = this.buffer.indexOf("\n")) >= 0) {
      const line = this.buffer.slice(0, idx).trim();
      this.buffer = this.buffer.slice(idx + 1);
      if (!line) continue;
      let msg;
      try {
        msg = JSON.parse(line);
      } catch (err) {
        this.log(`unparseable line: ${line.slice(0, 200)}`);
        continue;
      }
      if (msg.id !== undefined && this.pending.has(msg.id)) {
        const { resolve, reject, timer } = this.pending.get(msg.id);
        clearTimeout(timer);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(msg.error.message || "rpc error"));
        else resolve(msg.result);
      }
    }
  }
```

Buffer growth must be bounded — a pathological server could stream without newlines:

```js
      if (this.buffer.length > 8 * 1024 * 1024) {
        this.log("response buffer exceeded 8MB; killing child");
        this.stop();
      }
```

**`callTool`** — unwrap the MCP result shape and return the parsed body:

```js
  async callTool(name, args, timeoutMs = 20_000) {
    await this.start();
    const result = await this.request(
      "tools/call",
      { name, arguments: args },
      timeoutMs
    );
    if (result?.isError) {
      // Phase 2 embeds the JSON envelope in the error text.
      let envelope = null;
      const text = (result.content || []).find((c) => c.type === "text")?.text;
      try { envelope = text ? JSON.parse(text) : null; } catch { /* not JSON */ }
      const e = new Error(envelope?.error?.message || `${name} failed`);
      e.envelope = envelope;
      e.retryable = Boolean(envelope?.error?.retryable);
      throw e;
    }
    const text = (result?.content || []).find((c) => c.type === "text")?.text;
    if (!text) throw new Error(`${name} returned no text content`);
    try {
      return JSON.parse(text);
    } catch (err) {
      throw new Error(`${name} returned non-JSON: ${text.slice(0, 200)}`);
    }
  }
```

Also read `result.structuredContent` first when present — Phase 2's Pydantic output
models make it available, and parsing typed JSON beats parsing text.

**`stop()`** — reject everything pending, then kill. Without this, a killed child
leaves promises that never settle and the hook hangs.

**Process lifecycle:**

- **Idle timeout** — `setTimeout(() => client.stop(), IDLE_MS).unref()` after each
  completed call, cleared on the next call. Default 5 min. Without it an opencode
  session holds a Python process forever.
- **No concurrent pile-up** — one in-flight call per session. If a call is already
  running when `chat.message` fires, **skip** and log, rather than spawning a second:

  ```js
  if (this.busy) {
    this.log("skip: a jev query is already in flight for this session");
    return null;
  }
  ```

  Skipping is right: a stale skill injection for a superseded message is worse than
  none.
- **Circuit breaker** — after 3 consecutive failures, stop calling for 60 s
  (`JEV_PLUGIN_CIRCUIT_*`). Mirrors the Python breaker from Phase 4 so an outage is
  not paid twice per message.
- **`AbortSignal`** — if `input` carries one, abort the pending request on cancel and
  kill the child if the server is mid-call. The user aborting a message must not wait
  out the timeout.

**Logging** — keep `pluginLog` with the example's 2 MB rotation. Drop the prompt
content from the log (`:382` logs the first 80 chars of every user prompt; that is a
privacy leak and was one of the reasons the installed file's log grew unbounded).
Log the length and a hash instead.

---

## Task 6.2 — Fix `loadSettings`

`settings.pythonPath` still comes from `opencode.json`'s `mcp["jev-engine"].command`
(`:99-116`), but now points at the interpreter while `serverPath` is the second array
element. Simplify the probe:

```js
function resolveServerCommand(configPaths) {
  /** The [python, server] argv for jev-engine, from opencode.json or the venv default. */
  const REPO_ROOT = path.resolve(__dirname, "..");       // keep: no hardcoded drive
  const fallbacks = [
    [path.join(REPO_ROOT, ".venv", "Scripts", "python.exe"), path.join(REPO_ROOT, "jev_mcp.py")],
    [path.join(REPO_ROOT, ".venv", "bin", "python"), path.join(REPO_ROOT, "jev_mcp.py")],
    ["python", path.join(REPO_ROOT, "jev_mcp.py")],
  ];
  for (const p of configPaths) {
    if (!fs.existsSync(p)) continue;
    const cmd = readJson(p)?.mcp?.["jev-engine"]?.command;
    if (Array.isArray(cmd) && cmd.length >= 2 && cmd[0] && fs.existsSync(cmd[0]) && fs.existsSync(cmd[1])) {
      return { pythonPath: cmd[0], serverPath: cmd[1] };
    }
  }
  for (const [pythonPath, serverPath] of fallbacks) {
    if (fs.existsSync(pythonPath) && fs.existsSync(serverPath)) return { pythonPath, serverPath };
  }
  return { pythonPath: "python", serverPath: path.join(REPO_ROOT, "jev_mcp.py") };
}
```

Note the example **already** fixed the hardcoded `D:\` paths (`:30,134-137,155-157`) —
the installed file did not. Keeping `REPO_ROOT` means the copy-into-`config/`
workflow in `tests/test_plugin.mjs:60-93` still works.

**The API key is no longer needed here.** The MCP server reads it itself
(`config.py:96`), from its own env or `.env`. Keep passing
`TYPESAFE_API_KEY` through in the child env when the plugin has one
(`process.env` already carries it in the normal opencode case), but **delete the
`.env` regex parsing entirely** — that code existed only to feed the inline Python,
and it is where the quote-handling bug lives. One fewer copy of a credential path.

The MCP `environment` block in `config/opencode.example.json:11-18` already injects
the key into the server's env, so removing the plugin's `.env` read loses nothing.

---

## Task 6.3 — Fix the path traversal (finding #15)

The current code has no containment check, and the Python server does
(`jev_engine.py:540-541`):

```python
for sdir in settings.scanPaths:                 # resolved against cwd, no containment
  ...
  const rel = path.relative(cwd, file).replace(/\\/g, "/");   // happily yields "../.."
  fileMap.set(rel, file);
  ...
  if (selectedRel && fileMap.has(selectedRel)) {
    let content = fs.readFileSync(fileMap.get(selectedRel), "utf-8");   // reads outside the workspace
```

`scan_paths` comes from `jevs_settings.json`, which the README explicitly says is
**safe to commit and share** (`README.md:52`, `config/README.md:156`). So a hostile
repository setting `"scan_paths": ["../../../../Users/victim"]` causes the plugin to
read arbitrary `.md` files and inject them into the conversation.

Add the guard:

```js
function isInside(root, target) {
  const rel = path.relative(root, target);
  return rel === "" || (!rel.startsWith("..") && !path.isAbsolute(rel));
}
```

and apply it in **both** places — at scan time and again at read time (defence in
depth, in case `fileMap` is ever built from a different source):

```js
  const searchDirs = settings.scanPaths
    .map((p) => path.resolve(cwd, p))
    .filter((d) => isInside(cwd, d) || process.env.JEV_PLUGIN_ALLOW_OUTSIDE === "1");
  if (searchDirs.length !== settings.scanPaths.length) {
    pluginLog(`dropped ${settings.scanPaths.length - searchDirs.length} scan path(s) outside cwd`);
  }
  ...
  // at injection time
  if (selectedRel && fileMap.has(selectedRel)) {
    const fullPath = fileMap.get(selectedRel);
    if (!isInside(cwd, fullPath)) {
      pluginLog(`refusing to inject out-of-workspace file: ${selectedRel}`);
      return;
    }
```

`JEV_PLUGIN_ALLOW_OUTSIDE=1` is the documented escape hatch for a user who
deliberately keeps skills outside the project (e.g. `~/.config/opencode/skills`).
Better: reuse the Python side's Phase 4 mechanism and pass that root as
`JEV_MCP_ALLOWED_ROOTS` in the child env instead. Do both — the JS guard is
defence in depth, the env var is the supported path.

**Note the plugin never passes `root_dir`**, so the Phase 4 `root_dir` allowlist does
not affect the default flow. It becomes relevant only if the plugin later forwards a
configured root. Add `JEV_MCP_ALLOWED_ROOTS` to the child env from
`process.env.JEV_PLUGIN_SCAN_ROOTS` so external skill dirs are an explicit opt-in.

---

## Task 6.4 — Confidence-gate both effects

Today the plugin switches the model and injects a skill based purely on
`result?.tier` / `result?.target` being truthy. Phase 2 gives both real `action` and
`confidence` values, so use them.

```js
const PLUGIN_MIN_CONFIDENCE = 0.6;   // align with JEV_MCP_AUTO_ACCEPT's intent
const isConfident = (res) =>
  Boolean(res) && res.action === "auto" && (res.confidence ?? 0) >= PLUGIN_MIN_CONFIDENCE;
```

> Why 0.6 and not `JEV_MCP_AUTO_ACCEPT` (0.8)? The server already downgraded to
> `review` at <0.8, so by the time we see `action === "auto"` the bar is met. The
> extra 0.6 is a second, independent floor so a future server-side threshold change
> cannot silently start injecting on weak judgments. Keep it as a named constant with
> that comment.

### Model switch

```js
      if (wantTier && tierRes?.recommended_tier) {
        const confident = isConfident(tierRes);
        const modelId = settings.models[tierRes.recommended_tier];
        const parts = modelId ? splitModelId(modelId) : null;   // null-safe, as in the example
        if (!confident) {
          pluginLog(`tier ${tierRes.recommended_tier} not applied: action=${tierRes.action} confidence=${tierRes.confidence}`);
        } else if (!parts) {
          pluginLog(`skipping switch: '${modelId}' is not in 'provider/model' format`);
        } else if (output?.message?.model) {
          output.message.model = parts;
          pluginLog(`forced model switch -> ${tierRes.recommended_tier}: ${parts.providerID}/${parts.modelID}`);
        } else {
          pluginLog("no output.message.model to switch; skipping");
        }
      }
```

Two behavioural fixes land here: `splitModelId` returns `null` for a bare id (the
installed version returns a bogus `{providerID: id, modelID: id}`), and the switch
requires `output.message.model` to already exist.

### Skill injection

```js
      const primary = skillsRes?.primary;
      if (primary?.file && primary.content) {
        if (!isConfident(skillsRes)) {
          pluginLog(`skill ${primary.file} not injected: action=${skillsRes.action} confidence=${skillsRes.confidence}`);
        } else {
          let content = String(primary.content);
          if (content.length > MAX_INJECT_CHARS) {
            content = content.slice(0, MAX_INJECT_CHARS) + "\n…[truncated]";
          }
          injectIntoUserMessage(
            output,
            `\n\n[Active Capability / Skill: ${primary.file}]\n${content}\n`
          );
        }
      }
```

This is a meaningful reliability improvement: today a low-confidence judgment still
injects a possibly-irrelevant skill into the model's context, where it is
indistinguishable from something the user asked for.

It also uses `primary.content` — Phase 5 removes the duplicated top-level
`file`/`content` keys, so the plugin must read `primary.file` / `primary.content`.
That is why this phase comes after 3 and 5.

**Keep `injectIntoUserMessage` unchanged** (`:407-426` in the example). Editing the
existing text part's `text` field rather than pushing a new part is the fix for the
opencode `PartV2` save crash described in its docstring — a regression there means the
"Unexpected error occurred" popup and task auto-stop. Do not "simplify" it.

---

## Task 6.5 — Rewrite the hook

```js
  "chat.message": async (input, output) => {
    try {
      const promptText = extractUserPrompt(input, output);
      if (!promptText || promptText.length > MAX_PROMPT_CHARS) return;

      const cwd = process.cwd();
      const settings = loadSettings(cwd);
      const configuredModels = Object.values(settings.models).filter(
        (id) => typeof id === "string" && id.trim()
      );
      const wantTier = settings.enable_model_routing && configuredModels.length > 0;

      // Cheap local pre-check: no skills on disk and no routing means no call.
      const searchDirs = settings.scanPaths
        .map((p) => path.resolve(cwd, p))
        .filter((d) => isInside(cwd, d));
      if (!wantTier && searchDirs.every((d) => scanResourceFiles(d).length === 0)) {
        return;
      }

      const client = getClient(settings);
      const { tierRes, skillsRes } = await runQueries(client, {
        task: promptText,
        cwd,
        wantTier,
        signal: input?.signal ?? output?.signal,
      });

      applyTier(tierRes, settings, output);
      injectSkill(skillsRes, cwd, output);
    } catch (err) {
      pluginLog(`hook errored (bypassed safely): ${err.message}`);
      console.warn("[jev-plugin] Execution bypassed safely:", err.message);
    } finally {
      releaseClient();
    }
  },
```

Split `applyTier` / `injectSkill` out of the hook body so they are unit-testable
without a live server — `tests/test_plugin.mjs` can call them with fixture envelopes
and assert the model switch / injection decision. That replaces the current
substring assertions (`:129-136`) with real behavioural tests.

**Delete** from the plugin: `queryJev` and its entire inline Python string
(`:246-380`), the `.env` regex, and `scanResourceFiles`'s use for candidate
construction (keep the function for the cheap pre-check and for `fileMap`, but the
server now does the real scan — the plugin only needs to know *whether* any candidate
exists and to map the returned relative path to a file).

Actually, the plugin can **drop its own scan entirely** for the candidate list: ask
the server, and read the file only if the server names one. The pre-check becomes a
cheap `fs.existsSync` over the scan dirs:

```js
const hasSkills = searchDirs.some((d) => fs.existsSync(d) && scanResourceFiles(d).length > 0);
```

Keep `scanResourceFiles` (depth-bounded, as in the example) for that and nothing
else.

---

## Task 6.6 — Tests

`tests/test_plugin.mjs` currently asserts substrings (`:129-136`) and tests the
example only. Rewrite:

| Test | Replaces |
|---|---|
| `splitModelId` table incl. bare ids → `null` | keep (`:101-127`) |
| `isInside` table: sibling prefix, `..`, absolute, symlink | new |
| `scanResourceFiles` depth bound: a 10-deep tree stops | new (guards `:185-204`) |
| `pluginLog` rotation: write 2 MB, assert `.1` exists | new |
| `JevMcpClient` against a **stub** JSON-RPC server: initialize, framed responses, `isError` unwrapping, non-JSON, mid-stream newline split | new |
| `JevMcpClient` reconnect: kill the child, assert the next call respawns | new |
| `applyTier` with fixture envelopes: `auto`/high → switch; `review` → no switch; bare model id → no switch; missing `output.message.model` → no switch | replaces `:129-136` |
| `injectSkill` with fixture envelopes: confident → injects; `review`/`escalate` → not; oversized content → truncated | new |
| `injectIntoUserMessage` mutation shape | new — assert the existing part object is mutated, not replaced (the PartV2 fix) |
| drift guard | Phase 1 |
| `.env` regex tests (`:22-40`) | **delete** — the plugin no longer reads `.env` |

For the client tests, write a tiny stub server in the test file:

```js
async function startStubServer(handler) {
  const proc = spawn(process.execPath, ["-e", `
    let buf = "";
    process.stdin.on("data", (c) => {
      buf += c;
      let i;
      while ((i = buf.indexOf("\\n")) >= 0) {
        const line = buf.slice(0, i); buf = buf.slice(i + 1);
        if (!line.trim()) continue;
        const msg = JSON.parse(line);
        if (msg.id === undefined) continue;
        process.stdout.write(JSON.stringify(${handler.toString()}(msg)) + "\\n");
      }
    });
  `], { stdio: ["pipe", "pipe", "pipe"] });
  ...
}
```

Feed it a deliberately chunked write (split one JSON object across two `write`
calls) to prove the framing works.

---

## Deploying

```powershell
cd D:\mcp\jev-typesafe-mcp
# 1. Tests green against the example
node tests\test_plugin.mjs
# 2. Install
Copy-Item config\jev-plugin.example.js `
          "$env:USERPROFILE\.config\opencode\plugins\jev-plugin.js" -Force
# 3. Drift guard proves they match
node tests\test_plugin.mjs
# 4. Restart opencode, then:
Get-Content "$env:USERPROFILE\.config\opencode\logs\jev-plugin.log" -Tail 40
```

Then exercise a real turn and check the log shows `client started`,
`search_agent_skills action=auto confidence=0.8x`, `injected <path>`. Send a second
message and confirm there is **no** second `client started` line — that is the
connection-reuse proof.

## Out of scope

- **Long-lived daemon / socket transport.** stdio is right for opencode's plugin
  lifecycle; an HTTP transport would need a separate always-on process.
- **A second `guardrail_command` integration in the plugin.** The MCP tool is
  available to the model already; auto-invoking it from the hook would be
  surprising behaviour.
- **`JEV_PLUGIN_ALLOW_OUTSIDE`.** Prefer `JEV_PLUGIN_SCAN_ROOTS` →
  `JEV_MCP_ALLOWED_ROOTS`; keep the JS escape hatch undocumented and delete it in a
  later phase if unused.

---

## Files touched

| File | Change |
|---|---|
| `config/jev-plugin.example.js` | **rewrite**: `JevMcpClient`, `isInside`, `resolveServerCommand`, `applyTier`, `injectSkill`, slim `loadSettings`, confidence gate, delete `queryJev` + inline Python + `.env` regex, drop prompt content from logs |
| `~/.config/opencode/plugins/jev-plugin.js` | overwritten copy (not committed) |
| `tests/test_plugin.mjs` | **rewrite** sections 2, 5, 7; add 9–14 |
| `config/README.md` | new step for the new lifecycle; remove the `spawnSync` note; document `JEV_PLUGIN_SCAN_ROOTS` |
| `README.md` | §"The OpenCode Plugin" — `spawnSync` → stdio MCP client; note the confidence gate |
| `docs/perf-baseline.md` | per-message plugin latency, before vs after |

## Verification

```powershell
cd D:\mcp\jev-typesafe-mcp
node tests\test_plugin.mjs
& .\.venv\Scripts\python.exe -m pytest tests -q
# manual, after restarting opencode
Get-Content "$env:USERPROFILE\.config\opencode\logs\jev-plugin.log" -Tail 40
& .\.venv\Scripts\python.exe scripts\diag_mcp.py --tool search_agent_skills --task "x" --root_dir . --mock
```

Manual acceptance, in order:

1. A normal message → one `client started`, a confident `search_agent_skills`, a
   skill injected.
2. A second message → **no** second `client started`.
3. A message in a project with no skills and routing off → hook returns early, no
   spawn at all.
4. Set `TYPESAFE_API_KEY=""` → the child exits 1, the hook logs and continues, and
   **opencode does not error**.
5. Add `"scan_paths": ["../../.."]` to a scratch `jevs_settings.json` → the log shows
   `dropped N scan path(s) outside cwd` and nothing is injected from outside.
6. Send a message, then abort it → the pending call aborts; no injection afterwards.
7. Kill the Python child from a shell mid-session → the next message respawns it.

## Definition of done

- [ ] No inline Python left in the plugin; no `.env` regex
- [ ] One lazily-spawned, reused MCP child per session; idle timeout
- [ ] `scan_paths` confined to the cwd (or explicitly allowed roots)
- [ ] Out-of-workspace files refused at both scan and read time
- [ ] Model switch requires `action == "auto"`, confidence ≥ 0.6, valid `provider/model`, and an existing `output.message.model`
- [ ] Skill injection requires the same confidence bar
- [ ] `injectIntoUserMessage` mutation semantics unchanged (PartV2 regression guard)
- [ ] Concurrency guard, circuit breaker and abort support present
- [ ] Plugin log rotates at 2 MB and contains no user prompt text
- [ ] `tests/test_plugin.mjs` is behavioural, not substring greps
- [ ] All 7 manual acceptance checks pass
- [ ] `pytest tests -q` green

## Commit

```
refactor(plugin): drive jev-engine over MCP instead of inline Python

The installed plugin had drifted to 427 lines against the example's 510 and was
missing the recursion depth bound, log rotation, prompt cap, RetryPolicy and
timeout, skill-content truncation and the null-safe splitModelId. It also had
two outright bugs the example had fixed: a .env regex that captured a quoted
key including its quotes (a guaranteed 401), and a splitModelId that returned
{providerID: id, modelID: id} for a bare model id, which would assign a bogus
model object and break opencode's routing.

Its inline Python reimplemented logic jev_engine.py already owns, and had none
of fit_state, validate_response, the policy thresholds, the escape hatch, the
settings cache or the error taxonomy -- so a malformed response read as a
confident pick, and a low-confidence judgment still injected a skill into the
model's context where it was indistinguishable from something the user asked
for. Replace it with a hand-rolled newline-delimited JSON-RPC stdio client
(the protocol scripts/diag_mcp.py already speaks; no MCP SDK is available in
opencode's plugin sandbox). One lazily-spawned child is reused across messages,
which also removes a per-message interpreter start.

Both effects are now gated on action == "auto" and confidence >= 0.6, and the
model switch additionally requires a valid provider/model id and an existing
output.message.model.

Fix a path traversal: scan_paths is resolved against cwd with no containment
check and path.relative happily yields "../..", so a jevs_settings.json from an
untrusted repo (documented as safe to share) could get arbitrary .md files read
and injected. The Python side already guards this via relative_to; the plugin
now does too, at both scan and read time.

Add an idle timeout, a single-in-flight guard, a circuit breaker and abort
support, and rewrite tests/test_plugin.mjs from substring greps to behavioural
tests against a stub JSON-RPC server.
```
