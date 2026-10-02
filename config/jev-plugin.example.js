/**
 * jev-plugin.js — OpenCode `chat.message` hook for Jev AI.
 *
 * Source example:   config/jev-plugin.example.js
 * Real install:     C:\Users\<you>\.config\opencode\plugins\jev-plugin.js
 *
 * Two jobs per user message:
 *   1. Model routing (when `enable_model_routing` is on): asks Jev for the task
 *      tier (fast/balanced/frontier), maps it via `models` to a model ID, and
 *      FORCES the switch by mutating `output.message.model`. opencode persists the
 *      user message AFTER the hook fires and routes the next reply from
 *      `lastUser.model`, so this is a real, forced switch — not a recommendation.
 *      When routing is off, the message model is left untouched (opencode.json /
 *      window-selected model wins).
 *   2. Skill routing: asks Jev which Markdown file under the workspace is most
 *      relevant, and injects its content into the conversation.
 *
 * Both decisions are made by the jev-engine MCP server over a **stdio JSON-RPC
 * connection**, not by an inline Python program. That matters for correctness as
 * much as latency: the server owns `fit_state` token budgeting, fail-closed
 * response validation, the policy thresholds, the escape hatch and the error
 * taxonomy. A plugin that asked the model directly got none of those, so a
 * malformed response read as a confident pick. One child is spawned lazily and
 * reused for the whole opencode session.
 *
 * No `@modelcontextprotocol/sdk` is available inside opencode's plugin sandbox,
 * so the client is hand-rolled: newline-delimited JSON-RPC 2.0 over the child's
 * stdin/stdout, which is exactly what `scripts/diag_mcp.py` speaks.
 *
 * Settings are read from `jevs_settings.json` (project > user) with a legacy
 * fallback to the `jev_settings` block in opencode.json. All errors are swallowed:
 * the hook never crashes OpenCode.
 */
import { spawn } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
/** Repo root of *this copy* — never a hardcoded drive letter, so the
 *  copy-into-`config/`-and-install workflow keeps working. */
const REPO_ROOT = path.resolve(__dirname, "..");

const DEFAULT_SCAN_PATHS = [
  ".agents/skills",
  ".agents/workflows",
  ".agents/memory",
  ".opencode/skills",
  "skills",
  ".agents",
];

const MAX_INJECT_CHARS = 6000; // Match limits.MAX_CONTENT_CHARS
const MAX_PROMPT_CHARS = 100_000; // Longer prompts are skipped, not sent

/**
 * Confidence floor for *both* effects (model switch and skill injection).
 *
 * Why 0.6 and not JEV_MCP_AUTO_ACCEPT (0.8)? A second, independent floor, so a
 * future server-side threshold change cannot silently start acting on weak
 * judgments. It can only ever *raise* the bar above what the server already
 * allows, which is why `isConfident` (auto only) and `isInjectable` (auto or
 * review) share it.
 */
const PLUGIN_MIN_CONFIDENCE = 0.6;

const INIT_TIMEOUT_MS = 10_000;
const TOOL_TIMEOUT_MS = 20_000;
const CLIENT_IDLE_MS = 5 * 60_000; // Don't hold a Python process for a whole session
const CLIENT_KILL_GRACE_MS = 2000; // Let the server flush its log on the way out
const MAX_BUFFER_BYTES = 8 * 1024 * 1024; // A server that never sends \n must not eat RAM
const CIRCUIT_FAILURES = 3; // Mirrors JEV_MCP_BREAKER_THRESHOLD
const CIRCUIT_COOLDOWN_MS = 60_000; // Mirrors JEV_MCP_BREAKER_COOLDOWN_S

/**
 * How many messages may be judged at once, process-wide.
 *
 * This used to be a single `busy` boolean, which made the plugin serialise every
 * session and every subagent in the opencode server behind one lock: with two
 * windows open, the second message logged `skip: a jev query is already in
 * flight` and never got a skill. The limit was never about the transport — the
 * stdio client multiplexes concurrent requests by JSON-RPC id over one child — it
 * was about not injecting a decision for a message that had already been
 * superseded. That concern is now handled per session (see `beginTurn`), so the
 * cap exists only to bound load on one Python child.
 */
const MAX_CONCURRENT_QUERIES = 3;

/**
 * How many session ids to remember for supersede detection.
 *
 * A `Map` keyed by session id would otherwise grow for the lifetime of the
 * server. 64 is far more than any realistic number of live sessions, and the
 * oldest entry is dropped once the cap is passed.
 */
const MAX_TRACKED_SESSIONS = 64;

const LOG_MAX_BYTES = 2 * 1024 * 1024;

/** Log directory. Overridable so the test suite can assert rotation. */
function logDir() {
  return process.env.JEV_PLUGIN_LOG_DIR || path.join(os.homedir(), ".config", "opencode", "logs");
}

// Dedicated plugin log (opencode may swallow console output). Best effort only.
function pluginLog(msg) {
  try {
    const dir = logDir();
    fs.mkdirSync(dir, { recursive: true });
    const logFile = path.join(dir, "jev-plugin.log");

    // Rotate once past 2MB so the log cannot grow without bound.
    if (fs.existsSync(logFile)) {
      let size = 0;
      try {
        size = fs.statSync(logFile).size;
      } catch {
        size = 0;
      }
      if (size > LOG_MAX_BYTES) {
        const backupFile = `${logFile}.1`;
        try {
          if (fs.existsSync(backupFile)) fs.unlinkSync(backupFile);
          fs.renameSync(logFile, backupFile);
        } catch {}
      }
    }

    fs.appendFileSync(logFile, `${new Date().toISOString()} ${msg}\n`);
  } catch (err) {
    console.warn(`[jev-plugin] pluginLog failed:`, err.message);
  }
}

/**
 * Log a user prompt without logging it.
 *
 * The first 80 characters of every prompt used to go to disk verbatim. That is a
 * privacy leak (it is also how the log grew without bound), so a prompt is now
 * only ever identified by its length and a short digest.
 */
function describeText(text) {
  const digest = crypto.createHash("sha256").update(text).digest("hex").slice(0, 12);
  return `len=${text.length} sha256=${digest}`;
}

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf-8"));
  } catch (err) {
    console.warn(`[jev-plugin] Failed reading ${file}:`, err.message);
    return null;
  }
}

function realOrSelf(p) {
  try {
    return fs.realpathSync(p);
  } catch {
    return path.resolve(p);
  }
}

/**
 * True when `target` is `root` or lives inside it.
 *
 * Containment is a `path.relative` walk, never a `startsWith` on the string, so a
 * sibling like `<root>-evil` is refused. Both sides are resolved through
 * `realpath`, so a symlink pointing out of the workspace is refused too.
 */
function isInside(root, target) {
  const rel = path.relative(realOrSelf(root), realOrSelf(target));
  return rel === "" || (!rel.startsWith("..") && !path.isAbsolute(rel));
}

/**
 * Locate the `[python, server]` argv for jev-engine.
 *
 * `opencode.json`'s MCP entry is authoritative — a host that launches the server
 * that way is the same host the plugin must reuse. Only the server script must
 * exist on disk; the interpreter may legitimately be a bare `python` resolved
 * through PATH.
 *
 * Both config shapes are read. OpenCode 1.x keeps server names directly under
 * `mcp`; 2.x nests them under `mcp.servers`, states outright that it "does not
 * place server names directly under `mcp`" and that it has no `enabled` field,
 * and rejects a config that does not match the running version. Reading one shape
 * only would mean that on the other version this resolver finds nothing and
 * silently falls back to `REPO_ROOT` — which, for an installed plugin, is
 * `~/.config/opencode` and has no `.venv` there.
 */
function resolveServerCommand(configPaths) {
  for (const p of configPaths) {
    if (!p || !fs.existsSync(p)) continue;
    const mcp = readJson(p)?.mcp;
    const entry = mcp?.["jev-engine"] ?? mcp?.servers?.["jev-engine"];
    const cmd = entry?.command;
    if (entry && !Array.isArray(cmd)) {
      // A bare string command cannot be split into interpreter + script without
      // guessing, so the bundled server is used and the reason is logged.
      pluginLog(
        `${p}: jev-engine.command is ${cmd === undefined ? "absent" : typeof cmd}, not an array; ` +
          `using the bundled server`
      );
      continue;
    }
    if (!Array.isArray(cmd) || cmd.length < 2) continue;
    const [pythonPath, serverPath] = cmd;
    if (typeof pythonPath !== "string" || typeof serverPath !== "string") continue;
    if (!pythonPath || !serverPath) continue;
    if (fs.existsSync(serverPath)) return { pythonPath, serverPath };
    pluginLog(`opencode.json jev-engine.command points at a missing server: ${serverPath}`);
  }

  const fallbacks = [
    [path.join(REPO_ROOT, ".venv", "Scripts", "python.exe"), path.join(REPO_ROOT, "jev_mcp.py")],
    [path.join(REPO_ROOT, ".venv", "bin", "python"), path.join(REPO_ROOT, "jev_mcp.py")],
  ];
  for (const [pythonPath, serverPath] of fallbacks) {
    if (fs.existsSync(pythonPath) && fs.existsSync(serverPath)) return { pythonPath, serverPath };
  }
  return { pythonPath: "python", serverPath: path.join(REPO_ROOT, "jev_mcp.py") };
}

/**
 * Read Jev settings: `jevs_settings.json` (project > user), then the legacy
 * `jev_settings` block in opencode.json, plus the server command.
 *
 * The API key is deliberately NOT read here — the server loads it from its own
 * env or `.env`, and the opencode MCP `environment` block injects it. One fewer
 * copy of a credential path, and one fewer place for a quoting bug to hide.
 */
function loadSettings(cwd) {
  const base = cwd || process.cwd();
  const settingsPaths = [
    path.join(base, "jevs_settings.json"),
    path.join(base, ".opencode", "jevs_settings.json"),
    path.join(os.homedir(), ".config", "opencode", "jevs_settings.json"),
  ];
  const configPaths = [
    path.join(base, "opencode.json"),
    path.join(base, ".opencode", "opencode.json"),
    path.join(os.homedir(), ".config", "opencode", "opencode.json"),
  ];

  let jev = null;
  for (const p of settingsPaths) {
    if (!fs.existsSync(p)) continue;
    const raw = readJson(p);
    if (raw && typeof raw === "object" && !Array.isArray(raw)) {
      jev = raw;
      break;
    }
  }

  if (!jev) {
    for (const p of configPaths) {
      if (!fs.existsSync(p)) continue;
      const raw = readJson(p);
      if (raw?.jev_settings && typeof raw.jev_settings === "object") {
        jev = raw.jev_settings;
        break;
      }
    }
  }

  jev = jev || {};

  const configuredPaths = Array.isArray(jev.scan_paths)
    ? jev.scan_paths.filter((p) => typeof p === "string" && p.trim())
    : [];
  const scanPaths = Array.from(new Set([...DEFAULT_SCAN_PATHS, ...configuredPaths]));

  return {
    ...resolveServerCommand(configPaths),
    enable_model_routing: Boolean(jev.enable_model_routing),
    models:
      jev.models && typeof jev.models === "object" && !Array.isArray(jev.models)
        ? jev.models
        : {},
    scanPaths,
    // Both default ON: they are the two ways the engine reaches the agent when
    // the user has not said anything in the prompt itself. An explicit `false` in
    // jevs_settings.json turns either off; anything else (absent, null, a string)
    // leaves it on, because a typo must not silently disable the judge.
    judge_read_prompts: jev.judge_read_prompts !== false,
    inject_agent_instructions: jev.inject_agent_instructions !== false,
  };
}

/**
 * Environment for the child process.
 *
 * `JEV_PLUGIN_SCAN_ROOTS` is the plugin's opt-in for skill directories kept
 * outside the project (e.g. `~/.config/opencode/skills`); it is forwarded to the
 * server's own allowlist so both sides agree on what may be read.
 */
function childEnv() {
  const env = { ...process.env };
  const scanRoots = (process.env.JEV_PLUGIN_SCAN_ROOTS || "").trim();
  if (scanRoots) {
    const existing = (process.env.JEV_MCP_ALLOWED_ROOTS || "").trim();
    env.JEV_MCP_ALLOWED_ROOTS = existing ? `${existing}${path.delimiter}${scanRoots}` : scanRoots;
  }
  return env;
}

/**
 * Recursively discover all Markdown skill, workflow, and memory files.
 * Depth-bounded so a symlink loop or a huge tree cannot stall the hook.
 */
function scanResourceFiles(dir, depth = 0, maxDepth = 6) {
  const results = [];
  if (depth > maxDepth || !fs.existsSync(dir)) return results;

  try {
    const entries = fs.readdirSync(dir, { withFileTypes: true });
    for (const entry of entries) {
      if (entry.name.startsWith(".")) continue;
      const fullPath = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        results.push(...scanResourceFiles(fullPath, depth + 1, maxDepth));
      } else if (entry.isFile() && entry.name.endsWith(".md")) {
        results.push(fullPath);
      }
    }
  } catch (err) {
    console.warn(`[jev-plugin] Directory scan error (${dir}):`, err.message);
  }
  return results;
}

/**
 * Confine the configured scan paths to the workspace.
 *
 * `scan_paths` comes from `jevs_settings.json`, which the README documents as
 * safe to commit and share. Resolved against the CWD with no containment check, an
 * untrusted repository could name `../../../../Users/victim` and get arbitrary
 * `.md` files read and injected. Everything outside the workspace is dropped.
 */
function resolveSearchDirs(settings, cwd) {
  const allowOutside = process.env.JEV_PLUGIN_ALLOW_OUTSIDE === "1";
  const dirs = settings.scanPaths.map((p) => path.resolve(cwd, p));
  const kept = dirs.filter((d) => isInside(cwd, d) || allowOutside);
  if (kept.length !== dirs.length) {
    pluginLog(`dropped ${dirs.length - kept.length} scan path(s) outside cwd`);
  }
  return Array.from(new Set(kept));
}

/**
 * Extract prompt text across OpenCode hook payload variations
 */
function extractUserPrompt(input, output) {
  if (Array.isArray(output?.parts)) {
    const textPart = output.parts.find((p) => p?.type === "text" && typeof p?.text === "string");
    if (textPart) return textPart.text.trim();
  }

  if (typeof output?.message?.text === "string") return output.message.text.trim();
  if (typeof output?.message?.content === "string") return output.message.content.trim();

  const messages = output?.messages || input?.messages;
  if (Array.isArray(messages) && messages.length > 0) {
    const lastMsg = messages[messages.length - 1];
    if (typeof lastMsg?.content === "string") return lastMsg.content.trim();
    if (Array.isArray(lastMsg?.parts)) {
      const textPart = lastMsg.parts.find((p) => p?.type === "text" && typeof p?.text === "string");
      if (textPart) return textPart.text.trim();
    }
  }

  return "";
}

/**
 * Split a "providerID/modelID" string into its parts.
 * Returns null for anything not strictly in that format, so callers can
 * skip the model switch instead of forcing a bogus model object.
 */
function splitModelId(modelId) {
  if (typeof modelId !== "string" || !modelId) return null;
  const slash = modelId.indexOf("/");
  if (slash > 0 && slash < modelId.length - 1) {
    return { providerID: modelId.slice(0, slash), modelID: modelId.slice(slash + 1) };
  }
  return null;
}

/**
 * Append the skill notice to the USER's existing text part instead of pushing a
 * new part.
 *
 * opencode 2.x validates every message part against PartV2 at save time
 * (id / sessionID / messageID required). Pushing a bare `{type:"text", text}`
 * crashes `SessionPrompt.createUserMessage` ("invalid user part before save" ->
 * EventV2.InvalidDurableEvent -> popup + task auto-stop). Editing the existing
 * text part's `text` field keeps the object opencode already owns, so saving
 * still succeeds. If we cannot mutate safely, we skip injection (never crash).
 */
function injectIntoUserMessage(output, notice) {
  if (Array.isArray(output?.parts)) {
    const textPart = output.parts.find(
      (p) => p && p.type === "text" && typeof p.text === "string"
    );
    if (textPart) {
      const suffix = textPart.text.endsWith("\n") ? "" : "\n";
      textPart.text = textPart.text + suffix + notice;
      return true;
    }
    pluginLog("inject: no text part found; skipped (safe)");
    return false;
  }
  if (output?.message && typeof output.message.text === "string") {
    output.message.text = output.message.text + notice;
    return true;
  }
  pluginLog("inject: no supported message shape; skipped (safe)");
  return false;
}

/** True only for a decision the server is willing to act on without review. */
function isConfident(res) {
  if (!res || res.action !== "auto") return false;
  const confidence = typeof res.confidence === "number" ? res.confidence : 0;
  return confidence >= PLUGIN_MIN_CONFIDENCE;
}

/**
 * True when a decision is worth injecting as a skill hint.
 *
 * Deliberately wider than `isConfident`: a `review` decision (0.5 <= confidence
 * < JEV_MCP_AUTO_ACCEPT) is a real, ranked pick that the server merely wants a
 * human to glance at, and a skill note is advisory text in the user's own
 * message — not a model switch and not a command. Requiring `auto` here made the
 * 0.6 floor unreachable, so the effective bar was 0.8 and an `review`-grade
 * match was dropped without a trace even though the plugin had already decided
 * it was worth 0.6. `escalate` (below JEV_MCP_REVIEW_AT, a forced pick among
 * options that do not fit) is still refused, and the model switch keeps the
 * strict `isConfident` gate.
 */
function isInjectable(res) {
  if (!res || (res.action !== "auto" && res.action !== "review")) return false;
  const confidence = typeof res.confidence === "number" ? res.confidence : 0;
  return confidence >= PLUGIN_MIN_CONFIDENCE;
}

/**
 * Pull the `{code, message, retryable}` envelope out of an MCP tool error.
 *
 * The MCP runtime reports a tool failure as text of the form
 * `Error executing tool <name>: {"error": {...}}`, so the JSON has to be cut out
 * from under the prefix rather than parsed from the whole string.
 */
function parseEnvelope(text) {
  if (typeof text !== "string") return null;
  let json = null;
  if (text.trimStart().startsWith("{")) {
    json = text;
  } else {
    const idx = text.indexOf(": {");
    if (idx >= 0) json = text.slice(idx + 2);
  }
  if (!json) return null;
  try {
    const parsed = JSON.parse(json);
    return parsed && typeof parsed === "object" && parsed.error ? parsed : null;
  } catch {
    return null;
  }
}

/**
 * A minimal stdio JSON-RPC client for the jev-engine MCP server.
 *
 * Newline-delimited JSON-RPC 2.0 over a child process's stdin/stdout — the same
 * wire format `scripts/diag_mcp.py` speaks. One instance is reused for a whole
 * opencode session, so the per-message cost is one HTTPS round trip on a warm
 * connection instead of an interpreter start, an SDK import and a new HTTPS
 * round trip.
 */
class JevMcpClient {
  constructor(pythonPath, serverPath, options = {}) {
    this.pythonPath = pythonPath;
    this.serverPath = serverPath;
    this.args = Array.isArray(options.args) ? options.args : [];
    this.env = options.env || process.env;
    this.log = typeof options.log === "function" ? options.log : pluginLog;
    this.idleMs = Number.isFinite(options.idleMs) ? options.idleMs : CLIENT_IDLE_MS;
    this.killGraceMs = Number.isFinite(options.killGraceMs) ? options.killGraceMs : CLIENT_KILL_GRACE_MS;
    this.maxBufferBytes = Number.isFinite(options.maxBufferBytes) ? options.maxBufferBytes : MAX_BUFFER_BYTES;

    this.proc = null;
    this.buffer = "";
    this.nextId = 1;
    this.pending = new Map();
    this.ready = null;
    this.idleTimer = null;
    /** Messages currently being judged. Bounded by MAX_CONCURRENT_QUERIES. */
    this.inFlight = 0;
    this.consecutiveFailures = 0;
    this.breakerOpenUntil = 0;
  }

  /** Spawn lazily, once, and reuse. Idempotent. */
  start() {
    if (this.proc && this.ready) return this.ready;

    this.ready = new Promise((resolve, reject) => {
      let proc;
      try {
        proc = spawn(this.pythonPath, [...this.args, this.serverPath], {
          env: this.env,
          stdio: ["pipe", "pipe", "pipe"],
        });
      } catch (err) {
        this.ready = null;
        reject(new Error(`spawn failed: ${err.message}`));
        return;
      }
      this.proc = proc;
      this.log(`client started pid=${proc.pid} server=${this.serverPath}`);

      proc.stdout.on("data", (chunk) => this.onData(chunk));
      // stderr MUST be drained: a full pipe buffer would block the server's
      // logging mid-call and deadlock the request.
      proc.stderr.on("data", (d) => {
        const text = String(d).trim();
        if (text) this.log(`server stderr: ${text.slice(-300)}`);
      });
      proc.on("error", (err) => this.failAll(err));
      proc.on("exit", (code, signal) => {
        this.log(`server exited code=${code} signal=${signal}`);
        if (this.proc === proc) this.proc = null;
        this.failAll(new Error(`jev-engine exited (${code ?? signal})`));
        this.ready = null;
        this.clearIdle();
      });

      // stdout is JSON-RPC only; the server logs to stderr and to its own file.
      this.send(
        {
          jsonrpc: "2.0",
          id: this.nextId++,
          method: "initialize",
          params: {
            protocolVersion: "2025-06-18",
            capabilities: {},
            clientInfo: { name: "jev-plugin", version: "1" },
          },
        },
        INIT_TIMEOUT_MS
      )
        .then((res) => {
          this.notify("notifications/initialized");
          resolve(res);
        })
        .catch((err) => {
          this.ready = null;
          this.stop(`handshake failed: ${err.message}`);
          reject(err);
        });
    });

    return this.ready;
  }

  /** Fire-and-forget notification. */
  notify(method, params) {
    if (!this.proc) return;
    try {
      this.proc.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", method, params })}\n`);
    } catch (err) {
      this.log(`notify ${method} failed: ${err.message}`);
    }
  }

  send(message, timeoutMs, owner = null) {
    return new Promise((resolve, reject) => {
      const timer =
        Number.isFinite(timeoutMs) && timeoutMs > 0
          ? setTimeout(() => {
              this.pending.delete(message.id);
              reject(new Error(`${message.method} timed out after ${timeoutMs}ms`));
            }, timeoutMs)
          : null;
      this.pending.set(message.id, { resolve, reject, timer, owner });
      try {
        this.proc.stdin.write(`${JSON.stringify(message)}\n`);
      } catch (err) {
        this.pending.delete(message.id);
        if (timer) clearTimeout(timer);
        reject(new Error(`stdin write failed: ${err.message}`));
      }
    });
  }

  async request(method, params, timeoutMs = TOOL_TIMEOUT_MS, owner = null) {
    await this.start();
    return this.send({ jsonrpc: "2.0", id: this.nextId++, method, params }, timeoutMs, owner);
  }

  /** Frame on newlines and dispatch by request id. */
  onData(chunk) {
    this.buffer += chunk.toString("utf-8");
    let idx;
    while ((idx = this.buffer.indexOf("\n")) >= 0) {
      const line = this.buffer.slice(0, idx).trim();
      this.buffer = this.buffer.slice(idx + 1);
      if (!line) continue;
      let msg;
      try {
        msg = JSON.parse(line);
      } catch {
        this.log(`unparseable line: ${line.slice(0, 200)}`);
        continue;
      }
      if (msg.id !== undefined && msg.method) {
        // A server-initiated request (ping, roots/list, ...). Answer rather than
        // leave the server blocked on a response it will wait for.
        this.reply(msg.id, { error: { code: -32601, message: `method not found: ${msg.method}` } });
        continue;
      }
      const entry = msg.id !== undefined ? this.pending.get(msg.id) : undefined;
      if (!entry) continue;
      this.pending.delete(msg.id);
      if (entry.timer) clearTimeout(entry.timer);
      if (msg.error) entry.reject(new Error(msg.error?.message || "rpc error"));
      else entry.resolve(msg.result);
    }

    if (this.buffer.length > this.maxBufferBytes) {
      this.log(`response buffer exceeded ${this.maxBufferBytes} bytes; killing child`);
      this.stop("buffer overflow");
    }
  }

  reply(id, payload) {
    if (!this.proc) return;
    try {
      this.proc.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id, ...payload })}\n`);
    } catch (err) {
      this.log(`reply failed: ${err.message}`);
    }
  }

  /** Unwrap the MCP result shape and return the parsed tool body. */
  async callTool(name, args, timeoutMs = TOOL_TIMEOUT_MS, owner = null) {
    const result = await this.request("tools/call", { name, arguments: args }, timeoutMs, owner);
    const text = (result?.content || []).find((c) => c && c.type === "text")?.text;

    if (result?.isError) {
      const envelope = parseEnvelope(text);
      const err = new Error(
        envelope?.error?.message || `${name} failed: ${String(text || "no detail").slice(0, 200)}`
      );
      err.envelope = envelope || null;
      err.code = envelope?.error?.code || null;
      err.retryable = Boolean(envelope?.error?.retryable);
      throw err;
    }

    // `structuredContent` first when the server offers it; otherwise the text
    // body, which is the JSON document itself.
    if (result?.structuredContent && typeof result.structuredContent === "object") {
      return result.structuredContent;
    }
    if (typeof text !== "string" || !text.trim()) throw new Error(`${name} returned no text content`);
    try {
      return JSON.parse(text);
    } catch {
      throw new Error(`${name} returned non-JSON: ${text.slice(0, 200)}`);
    }
  }

  failAll(err) {
    const entries = Array.from(this.pending.values());
    this.pending.clear();
    for (const entry of entries) {
      if (entry.timer) clearTimeout(entry.timer);
      entry.reject(err);
    }
  }

  /**
   * Reject everything pending, then shut the child down. Without rejecting first,
   * a killed child leaves promises that never settle and the hook hangs.
   */
  stop(reason = "stopped") {
    this.clearIdle();
    const proc = this.proc;
    this.proc = null;
    this.ready = null;
    this.buffer = "";
    // `inFlight` is deliberately NOT reset here. Every query releases in its
    // own `finally`, so the counter drains on its own; zeroing it here would
    // under-count the queries that are still unwinding and let the cap be
    // exceeded for the rest of their lifetime.
    this.failAll(new Error(`jev-engine client ${reason}`));
    if (!proc) return;
    this.log(`client stopped (${reason})`);
    try {
      proc.stdin.end();
    } catch {}
    if (this.killGraceMs <= 0) {
      try {
        proc.kill();
      } catch {}
      return;
    }
    // Close stdin first so the server can run its shutdown logging, but do not
    // wait forever for it.
    const killTimer = setTimeout(() => {
      try {
        proc.kill();
      } catch {}
    }, this.killGraceMs);
    if (typeof killTimer.unref === "function") killTimer.unref();
  }

  clearIdle() {
    if (this.idleTimer) {
      clearTimeout(this.idleTimer);
      this.idleTimer = null;
    }
  }

  /** Arm the idle shutdown; cancelled by the next call. */
  armIdle() {
    this.clearIdle();
    if (!(this.idleMs > 0)) return;
    this.idleTimer = setTimeout(() => this.stop("idle"), this.idleMs);
    if (typeof this.idleTimer.unref === "function") this.idleTimer.unref();
  }

  breakerOpen() {
    return Date.now() < this.breakerOpenUntil;
  }

  recordSuccess() {
    this.consecutiveFailures = 0;
    this.breakerOpenUntil = 0;
  }

  recordFailure(err) {
    this.consecutiveFailures += 1;
    if (this.consecutiveFailures >= CIRCUIT_FAILURES) {
      this.breakerOpenUntil = Date.now() + CIRCUIT_COOLDOWN_MS;
      this.log(
        `circuit breaker opened for ${CIRCUIT_COOLDOWN_MS}ms after ` +
          `${this.consecutiveFailures} consecutive failures (last: ${err?.message || "unknown"})`
      );
      this.stop("circuit breaker open");
    }
  }

  /**
   * The user aborted one message: fail only that message's requests.
   *
   * This used to reject every pending request and kill the child, which was safe
   * only because a single message could be in flight. With several sessions
   * sharing one client, an abort in one window would have destroyed the others'
   * work, so the entries are filtered by owner and the child is stopped only
   * when no other query is left running.
   */
  abort(reason = "aborted", owner = null) {
    const mine = Array.from(this.pending.entries()).filter(([, entry]) => entry.owner === owner);
    if (!mine.length) return;
    this.log(`aborting ${mine.length} in-flight request(s): ${reason}`);
    for (const [id, entry] of mine) {
      this.pending.delete(id);
      if (entry.timer) clearTimeout(entry.timer);
      entry.reject(new Error(`jev-engine request ${reason}`));
    }
    if (this.inFlight <= 1) this.stop(reason);
  }

  /** Called by the hook once it is done with the client for this message. */
  release() {
    this.inFlight = Math.max(0, this.inFlight - 1);
    // Only the last caller may arm the idle shutdown, or a message that finished
    // while others were still running would schedule a stop under their feet.
    if (this.inFlight === 0) this.armIdle();
  }
}

/** The session-wide client. Rebuilt only if the configured command changes. */
let activeClient = null;
let activeClientKey = null;

function getClient(settings) {
  const key = `${settings.pythonPath} ${settings.serverPath}`;
  if (activeClient && activeClientKey !== key) {
    activeClient.stop("server command changed");
    activeClient = null;
    activeClientKey = null;
  }
  if (!activeClient) {
    activeClient = new JevMcpClient(settings.pythonPath, settings.serverPath, { env: childEnv() });
    activeClientKey = key;
  }
  return activeClient;
}

/**
 * Per-session supersede tracking.
 *
 * The reason the old code refused to run two queries at once: a decision for a
 * message the user has already moved past is worse than no decision, because it
 * injects a skill that answers a question nobody is asking any more. That is a
 * *per-session* property, so it is tracked per session — several windows can now
 * be judged at once, and only the stale one is dropped.
 */
let turnSeq = 0;
const latestTurnBySession = new Map();

function beginTurn(sessionID) {
  turnSeq += 1;
  const turn = { sessionID: sessionID || null, token: turnSeq };
  if (turn.sessionID) {
    latestTurnBySession.set(turn.sessionID, turn.token);
    while (latestTurnBySession.size > MAX_TRACKED_SESSIONS) {
      const oldest = latestTurnBySession.keys().next().value;
      latestTurnBySession.delete(oldest);
    }
  }
  return turn;
}

/**
 * True while `turn` is still the newest message for its session.
 *
 * A payload with no session id cannot be superseded, so it is always current:
 * dropping its result would be a silent no-op with no way to explain it.
 */
function isCurrentTurn(turn) {
  if (!turn?.sessionID) return true;
  return latestTurnBySession.get(turn.sessionID) === turn.token;
}

/**
 * Run the two tool calls for one message.
 *
 * - Up to MAX_CONCURRENT_QUERIES messages are judged at once, so parallel
 *   sessions and subagents no longer queue behind one another. Past the cap the
 *   message is skipped and logged, never queued: a decision that arrives after
 *   the user moved on is not worth the latency.
 * - The two calls are independent, so they run concurrently and a failure in one
 *   does not discard the other.
 * - The circuit breaker mirrors the server's own, so an outage is not paid twice
 *   per message.
 * - Requests carry the caller's `owner`, so an abort rejects only its own.
 *
 * Returns `null` when nothing was run, so the caller can tell a skip from a
 * decision it chose to ignore.
 */
async function runQueries(client, { task, cwd, wantTier, signal, turn, taskFile }) {
  if (client.inFlight >= MAX_CONCURRENT_QUERIES) {
    client.log(`skip: ${MAX_CONCURRENT_QUERIES} jev queries already in flight`);
    return null;
  }
  if (client.breakerOpen()) {
    client.log("skip: circuit breaker is open");
    return null;
  }
  if (signal?.aborted) {
    client.log("skip: message was already aborted");
    return null;
  }

  client.inFlight += 1;
  const onAbort = () => client.abort("aborted by user", turn);
  if (typeof signal?.addEventListener === "function") {
    signal.addEventListener("abort", onAbort, { once: true });
  }

  const safeCall = async (name, args) => {
    try {
      const res = await client.callTool(name, args, TOOL_TIMEOUT_MS, turn);
      client.recordSuccess();
      return res;
    } catch (err) {
      client.recordFailure(err);
      client.log(`${name} failed: ${err.message}`);
      return null;
    }
  };

  const skillArgs = { task, root_dir: cwd };
  if (taskFile) skillArgs.task_file = taskFile;

  try {
    const [tierRes, skillsRes] = await Promise.all([
      wantTier ? safeCall("select_model_tier", { task }) : null,
      safeCall("search_agent_skills", skillArgs),
    ]);
    if (tierRes) client.log(`select_model_tier action=${tierRes.action} confidence=${tierRes.confidence}`);
    if (skillsRes) {
      client.log(
        `search_agent_skills action=${skillsRes.action} confidence=${skillsRes.confidence} ` +
          `primary=${skillsRes.primary?.file}`
      );
    }
    return { tierRes, skillsRes };
  } finally {
    if (typeof signal?.removeEventListener === "function") signal.removeEventListener("abort", onAbort);
    client.release();
  }
}

/**
 * Force the model switch, if the server's decision is worth acting on.
 * Returns true when `output.message.model` was replaced.
 */
function applyTier(tierRes, settings, output) {
  const tier = tierRes?.recommended_tier;
  if (!tier) return false;

  const modelId = settings.models?.[tier];
  if (!modelId) {
    pluginLog(`tier ${tier} has no configured model id; skipping switch`);
    return false;
  }
  if (!isConfident(tierRes)) {
    pluginLog(`tier ${tier} not applied: action=${tierRes.action} confidence=${tierRes.confidence}`);
    return false;
  }
  const parts = splitModelId(modelId);
  if (!parts) {
    pluginLog(`skipping switch: '${modelId}' is not in 'provider/model' format`);
    return false;
  }
  if (!output?.message?.model) {
    pluginLog("no output.message.model to switch; skipping");
    return false;
  }
  output.message.model = parts;
  pluginLog(`forced model switch -> ${tier}: ${parts.providerID}/${parts.modelID}`);
  return true;
}

/**
 * Inject the selected skill into the user's message, if the decision is worth
 * acting on. Returns true when the message text was changed.
 */
function injectSkill(skillsRes, cwd, output) {
  const primary = skillsRes?.primary;
  // `primary` is an identity view — which file won — and the text lives on the
  // full record in `resources[0]`. The two were byte-identical, so every message
  // serialized the same up-to-6,000-character blob twice.
  const record = Array.isArray(skillsRes?.resources) ? skillsRes.resources[0] : null;
  if (!primary?.file || !record || typeof record.content !== "string") {
    // A confident `auto` with no primary is a real outcome: the model picked
    // `none`, so there is nothing to inject. Say so, or it reads as a silent
    // no-op in the log.
    pluginLog(
      `no skill to inject: action=${skillsRes?.action ?? "none"} ` +
        `confidence=${skillsRes?.confidence ?? "n/a"} primary=${primary?.file ?? "none"}`
    );
    return false;
  }

  if (!isInjectable(skillsRes)) {
    pluginLog(
      `skill ${primary.file} not injected: action=${skillsRes.action} confidence=${skillsRes.confidence}`
    );
    return false;
  }

  // Defence in depth: the server already refuses to read outside its roots, but
  // `primary.file` is a path this hook is about to act on, so re-check it here.
  const fullPath = path.resolve(cwd, primary.file);
  if (!isInside(cwd, fullPath)) {
    pluginLog(`refusing to inject out-of-workspace file: ${primary.file}`);
    return false;
  }

  let content = record.content;
  if (content.length > MAX_INJECT_CHARS) {
    content = content.slice(0, MAX_INJECT_CHARS) + "\n…[truncated]";
    pluginLog(`truncated skill ${primary.file} to ${MAX_INJECT_CHARS} chars`);
  }

  const injected = injectIntoUserMessage(
    output,
    `\n\n[Active Capability / Skill: ${primary.file}]\n${content}\n`
  );
  pluginLog(`inject ${injected ? "ok" : "skipped"}: ${primary.file} (${content.length} chars)`);
  return injected;
}

/**
 * Files whose content is treated as a saved prompt when the agent reads them.
 *
 * Deliberately a short list rather than "any text file": every entry costs one
 * judged round trip, and re-judging a source file the agent is reading for its
 * own sake is noise.
 */
const PROMPT_FILE_EXTENSIONS = new Set([".md", ".markdown", ".txt", ".prompt", ".plan"]);

/** Cap on the judge note appended to a tool result. */
const MAX_TOOL_NOTICE_CHARS = 600;

/**
 * Append `notice` to a tool result, in place.
 *
 * opencode validates every message part against PartV2 when it saves the
 * message, and a *pushed* bare `{type:"text", text}` crashes the session
 * ("invalid user part before save" -> the task auto-stops) — the same trap
 * `injectIntoUserMessage` documents. So nothing is ever pushed here either: the
 * last existing text part is *replaced* by a copy of itself with a longer
 * `text`, which keeps every id, messageID, sessionID and synthetic flag the
 * host put there. A result with no text part, or a plain string, is refused.
 */
function appendToToolResult(result, notice) {
  const suffix = `\n\n${truncateText(notice, MAX_TOOL_NOTICE_CHARS)}`;
  if (Array.isArray(result)) {
    for (let i = result.length - 1; i >= 0; i -= 1) {
      const part = result[i];
      if (part && part.type === "text" && typeof part.text === "string") {
        result[i] = { ...part, text: part.text + suffix };
        return true;
      }
    }
    return false;
  }
  if (result && typeof result === "object" && typeof result.output === "string") {
    result.output += suffix;
    return true;
  }
  return false;
}

function truncateText(value, maxChars) {
  const text = String(value ?? "");
  return text.length <= maxChars ? text : `${text.slice(0, maxChars)}…`;
}

/** The one-line-per-skill ranking appended after a prompt file is read. */
function renderDecision(res, file) {
  const ranked = Array.isArray(res?.ranked) ? res.ranked.slice(0, 3) : [];
  // Paths come from the engine, so they are workspace-relative and normally
  // short, but nothing guarantees a short one. Each line is capped here so the
  // note is bounded at the source; `appendToToolResult` trims the whole thing
  // again as a final guard.
  const clip = (value) => truncateText(value, 120);
  const lines = ranked.map((r, i) => `${i + 1}. ${clip(r.file)} (${Number(r.probability ?? 0).toFixed(2)})`);
  const body = lines.length ? lines.join("\n") : "no skill stood out";
  return (
    `[Jev] judged ${clip(file)}: action=${res.action} confidence=${res.confidence}\n` +
    `${body}\n` +
    `Read the top skill with your read tool if it applies.`
  );
}

/**
 * Per-(session, file, revision) memo of what has already been judged.
 *
 * An agent re-reads a file for many reasons — a grep hit, a failed edit, a
 * second opinion — and each one would otherwise cost a round trip to the API.
 * Keyed by size and mtime so an edited file is judged again, and bounded so a
 * long session cannot grow it without limit.
 */
const judgedFiles = new Map();
const MAX_JUDGED_FILES = 64;

function fileStamp(file, stat) {
  return `${file}|${stat.size}:${Math.round(stat.mtimeMs)}`;
}

function alreadyJudged(key) {
  const seen = judgedFiles.get(key);
  const fresh = seen !== undefined && Date.now() - seen < 10 * 60_000;
  if (fresh) return true;
  judgedFiles.set(key, Date.now());
  while (judgedFiles.size > MAX_JUDGED_FILES) {
    judgedFiles.delete(judgedFiles.keys().next().value);
  }
  return false;
}

/**
 * The rules block appended to the system prompt every turn.
 *
 * The plugin can only *judge and advise*; it cannot make the agent act. So the
 * decision points are stated where the agent actually reads them — its system
 * prompt — instead of in a project file that may not exist. Kept short on
 * purpose: it is prepended to every request in every workspace, so every line
 * is paid for on every turn.
 */
const AGENT_DECISION_RULES = [
  "# Jev decision points",
  "",
  "The `jev-engine` MCP server turns prose into typed judgments. Use it at the",
  "points below rather than guessing:",
  "",
  "- Before running a shell command that deletes, moves, force-writes, resets or",
  "  pushes: `guardrail_command` with the exact command string. Treat",
  "  `safe: false` as a stop, not a warning.",
  "- Before editing: `search_target_files` to find which files the task really",
  "  touches, so you inspect the right ones.",
  "- At the start of a task, and after reading a saved prompt or plan:",
  "  `search_agent_skills` to pick the skill or workflow that applies. When the",
  "  prompt lives in a file, pass that path as `task_file` instead of pasting it,",
  "  e.g. `task_file: \"docs/plan.md\"`.",
  "- When several MCP servers are connected and the task may need one:",
  "  `select_mcp_tools` with the roster you are connected to, to pick the server",
  "  and the tools to call. It never picks its own server, and `exists:",
  "  \"absent\"` or `\"ambiguous\"` means it is not choosing for you.",
  "- Every call returns `action` (`auto` / `review` / `escalate`) and a",
  "  `confidence`. Below 0.5 the model is guessing: say so instead of acting on",
  "  it. An error envelope means no judgment was made, not that nothing matched.",
].join("\n");

export const JevPlugin = async () => ({
  "experimental.chat.system.transform": async (input, output) => {
    try {
      if (!Array.isArray(output?.system)) return;
      const settings = loadSettings(process.cwd());
      if (!settings.inject_agent_instructions) return;
      // Pushed, never spliced in: the host keeps the first element as the
      // provider's own instruction block, so the block belongs at the end.
      output.system.push(AGENT_DECISION_RULES);
    } catch (err) {
      pluginLog(`system.transform bypassed safely: ${err.message}`);
    }
  },
  "tool.execute.after": async (input, result) => {
    if (input?.tool !== "read") return;
    const file = input?.args?.filePath;
    if (typeof file !== "string" || !file) return;
    const cwd = process.cwd();
    if (!PROMPT_FILE_EXTENSIONS.has(path.extname(file).toLowerCase())) return;
    if (!isInside(cwd, file)) return;
    let stat;
    try {
      stat = fs.statSync(file);
    } catch {
      return;
    }
    if (alreadyJudged(fileStamp(file, stat))) return;

    try {
      const settings = loadSettings(cwd);
      if (!settings.judge_read_prompts) return;
      const client = getClient(settings);
      if (client.inFlight >= MAX_CONCURRENT_QUERIES || client.breakerOpen()) {
        pluginLog(`read judge skipped for ${file}: client is busy or the breaker is open`);
        return;
      }
      client.inFlight += 1;
      let res;
      try {
        res = await client.callTool(
          "search_agent_skills",
          { task: `Which skill or workflow applies to the document I just read (${path.basename(file)})?`, root_dir: cwd, task_file: file },
          TOOL_TIMEOUT_MS,
          { sessionID: input?.sessionID ?? null, token: 0 }
        );
        client.recordSuccess();
      } catch (err) {
        client.recordFailure(err);
        pluginLog(`read judge failed for ${file}: ${err.message}`);
        return;
      } finally {
        client.release();
      }

      if (!isInjectable(res) || !res?.primary?.file) {
        pluginLog(`read judge: nothing to add for ${file} (action=${res?.action} confidence=${res?.confidence})`);
        return;
      }
      const appended = appendToToolResult(result, renderDecision(res, path.basename(file)));      pluginLog(`read judge: ${appended ? "appended" : "no text part to append to"} for ${file}`);
      if (appended) {
        console.log(`[jev-plugin] ⚡ Judged ${path.basename(file)} → ${res.primary.file}`);
      }
    } catch (err) {
      pluginLog(`read judge bypassed safely: ${err.message}`);
    }
  },
  "chat.message": async (input, output) => {
    try {
      const promptText = extractUserPrompt(input, output);
      if (!promptText) return;
      if (promptText.length > MAX_PROMPT_CHARS) {
        pluginLog(`prompt over ${MAX_PROMPT_CHARS} chars; skipping (${describeText(promptText)})`);
        return;
      }

      const cwd = process.cwd();
      const settings = loadSettings(cwd);

      const configuredModels = Object.values(settings.models).filter(
        (id) => typeof id === "string" && id.trim()
      );
      const wantTier = settings.enable_model_routing && configuredModels.length > 0;

      // Cheap local pre-check: no skills on disk and no routing means no call.
      const searchDirs = resolveSearchDirs(settings, cwd);
      const hasSkills = searchDirs.some((d) => fs.existsSync(d) && scanResourceFiles(d).length > 0);
      if (!wantTier && !hasSkills) return;

      pluginLog(
        `chat.message: prompt ${describeText(promptText)} wantTier=${wantTier} ` +
          `skillDirs=${searchDirs.length} cwd=${cwd} session=${input?.sessionID ?? "none"}`
      );

      const turn = beginTurn(input?.sessionID);

      const client = getClient(settings);
      const started = Date.now();
      const results = await runQueries(client, {
        task: promptText,
        cwd,
        wantTier,
        turn,
        signal: input?.signal ?? output?.signal,
      });
      const elapsed = Date.now() - started;
      if (!results) {
        pluginLog(`chat.message skipped after ${elapsed}ms`);
        return;
      }
      if (!isCurrentTurn(turn)) {
        // A newer message arrived in this same session while we were waiting. Its
        // own hook is judging the question the user is actually asking, so this
        // decision is dropped rather than injected into a message they moved on
        // from. Other sessions are unaffected.
        pluginLog(`chat.message result dropped: superseded by a newer message (${elapsed}ms)`);
        return;
      }
      const { tierRes, skillsRes } = results;

      if (tierRes && applyTier(tierRes, settings, output)) {
        console.log(
          `[jev-plugin] ⚡ Forced model switch → ${tierRes.recommended_tier} in ${elapsed}ms`
        );
      }
      if (skillsRes && injectSkill(skillsRes, cwd, output)) {
        console.log(`[jev-plugin] ⚡ Injected ${skillsRes.primary.file} in ${elapsed}ms`);
      }
      pluginLog(`chat.message done in ${elapsed}ms`);
    } catch (err) {
      pluginLog(`hook errored (bypassed safely): ${err.message}`);
      console.warn("[jev-plugin] Execution bypassed safely:", err.message);
    }
  },
});

JevPlugin.splitModelId = splitModelId;
JevPlugin.isInside = isInside;
JevPlugin.isConfident = isConfident;
JevPlugin.isInjectable = isInjectable;
JevPlugin.scanResourceFiles = scanResourceFiles;
JevPlugin.pluginLog = pluginLog;
JevPlugin.describeText = describeText;
JevPlugin.resolveServerCommand = resolveServerCommand;
JevPlugin.loadSettings = loadSettings;
JevPlugin.resolveSearchDirs = resolveSearchDirs;
JevPlugin.extractUserPrompt = extractUserPrompt;
JevPlugin.applyTier = applyTier;
JevPlugin.injectSkill = injectSkill;
JevPlugin.injectIntoUserMessage = injectIntoUserMessage;
JevPlugin.parseEnvelope = parseEnvelope;
JevPlugin.runQueries = runQueries;
JevPlugin.beginTurn = beginTurn;
JevPlugin.isCurrentTurn = isCurrentTurn;
JevPlugin.appendToToolResult = appendToToolResult;
JevPlugin.renderDecision = renderDecision;
JevPlugin.fileStamp = fileStamp;
JevPlugin.alreadyJudged = alreadyJudged;
JevPlugin.AGENT_DECISION_RULES = AGENT_DECISION_RULES;
JevPlugin.PROMPT_FILE_EXTENSIONS = PROMPT_FILE_EXTENSIONS;
JevPlugin.MAX_CONCURRENT_QUERIES = MAX_CONCURRENT_QUERIES;
JevPlugin.getClient = getClient;
JevPlugin.JevMcpClient = JevMcpClient;
JevPlugin.MAX_INJECT_CHARS = MAX_INJECT_CHARS;
JevPlugin.PLUGIN_MIN_CONFIDENCE = PLUGIN_MIN_CONFIDENCE;

export default JevPlugin;
