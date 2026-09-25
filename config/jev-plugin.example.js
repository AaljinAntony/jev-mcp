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
 *   2. Skill routing: scans the configured skill dirs for Markdown, asks Jev which
 *      file is most relevant, and injects its content into the conversation.
 *
 * Settings are read from `jevs_settings.json` (project > user) with a legacy
 * fallback to the `jev_settings` block in opencode.json. All errors are swallowed:
 * the hook never crashes OpenCode.
 */
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const REPO_ROOT = path.resolve(__dirname, "..");

const DEFAULT_SCAN_PATHS = [
  ".agents/skills",
  ".agents/workflows",
  ".agents/memory",
  ".opencode/skills",
  "skills",
  ".agents",
];

const MAX_INJECT_CHARS = 6000; // Match jev_engine MAX_CONTENT_CHARS

// Dedicated plugin log (opencode may swallow console output). Best effort only.
function pluginLog(msg) {
  try {
    const dir = path.join(os.homedir(), ".config", "opencode", "logs");
    fs.mkdirSync(dir, { recursive: true });
    fs.appendFileSync(
      path.join(dir, "jev-plugin.log"),
      `${new Date().toISOString()} ${msg}\n`
    );
  } catch (err) {
    console.warn(`[jev-plugin] pluginLog failed:`, err.message);
  }
}

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf-8"));
  } catch (err) {
    console.warn(`[jev-plugin] Failed reading ${file}:`, err.message);
    return null;
  }
}

/**
 * Locate and read Jev settings: jevs_settings.json first, then legacy
 * opencode.json `jev_settings`, plus the Python interpreter and API key.
 */
function loadSettings() {
  const cwd = process.cwd();
  const settingsPaths = [
    path.join(cwd, "jevs_settings.json"),
    path.join(cwd, ".opencode", "jevs_settings.json"),
    path.join(os.homedir(), ".config", "opencode", "jevs_settings.json"),
  ];
  const configPaths = [
    path.join(cwd, "opencode.json"),
    path.join(cwd, ".opencode", "opencode.json"),
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

  // Python interpreter: mcp command -> default venv
  let pythonPath = null;
  for (const p of configPaths) {
    if (!fs.existsSync(p)) continue;
    const cmd = readJson(p)?.mcp?.["jev-engine"]?.command;
    if (Array.isArray(cmd) && cmd[0] && fs.existsSync(cmd[0])) {
      pythonPath = cmd[0];
      break;
    }
    if (typeof cmd === "string" && fs.existsSync(cmd)) {
      pythonPath = cmd;
      break;
    }
  }
  if (!pythonPath || !fs.existsSync(pythonPath)) {
    const defaultVenvPy = path.join(REPO_ROOT, ".venv", "Scripts", "python.exe");
    // Also try Unix-style venv path
    const defaultVenvPyUnix = path.join(REPO_ROOT, ".venv", "bin", "python");
    const cwdVenvPy = path.join(cwd, ".venv", "Scripts", "python.exe");
    const cwdVenvPyUnix = path.join(cwd, ".venv", "bin", "python");
    if (fs.existsSync(defaultVenvPy)) {
      pythonPath = defaultVenvPy;
    } else if (fs.existsSync(defaultVenvPyUnix)) {
      pythonPath = defaultVenvPyUnix;
    } else if (fs.existsSync(cwdVenvPy)) {
      pythonPath = cwdVenvPy;
    } else if (fs.existsSync(cwdVenvPyUnix)) {
      pythonPath = cwdVenvPyUnix;
    } else {
      pythonPath = "python";
    }
  }

  // API key: process env -> repo .env
  let apiKey = process.env.TYPESAFE_API_KEY || "";
  if (!apiKey) {
    const envFile = path.join(REPO_ROOT, ".env");
    const cwdEnvFile = path.join(cwd, ".env");
    const targetEnv = fs.existsSync(envFile) ? envFile : fs.existsSync(cwdEnvFile) ? cwdEnvFile : null;
    if (targetEnv) {
      const match = fs.readFileSync(targetEnv, "utf-8").match(/TYPESAFE_API_KEY\s*=\s*['"]?([^'"\s\n]+)['"]?/);
      if (match) apiKey = match[1].trim();
    }
  }

  const configuredPaths = Array.isArray(jev.scan_paths)
    ? jev.scan_paths.filter((p) => typeof p === "string")
    : [];
  const scanPaths = Array.from(new Set([...DEFAULT_SCAN_PATHS, ...configuredPaths]));

  return {
    pythonPath,
    apiKey,
    enable_model_routing: Boolean(jev.enable_model_routing),
    models:
      jev.models && typeof jev.models === "object" && !Array.isArray(jev.models)
        ? jev.models
        : {},
    scanPaths,
  };
}

/**
 * Recursively discover all Markdown skill, workflow, and memory files
 */
function scanResourceFiles(dir) {
  const results = [];
  if (!fs.existsSync(dir)) return results;

  try {
    const entries = fs.readdirSync(dir, { withFileTypes: true });
    for (const entry of entries) {
      const fullPath = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        results.push(...scanResourceFiles(fullPath));
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
 * Query Jev via the configured Python environment using the current
 * TypeSafeClient.system_one(...) API. Returns { tier?, target? }.
 *
 * TODO: This spawns a new Python process per message, which is slow (~1-3s).
 * A better approach would be to call the jev-engine MCP server's tools
 * (search_agent_skills, select_model_tier) via the MCP protocol, reusing
 * the already-running server's client, connection pool, and retry logic.
 * This requires the plugin to act as an MCP client over stdio.
 *
 * Async: spawns a child and awaits its exit so the hook NEVER blocks the
 * opencode process (a blocking spawnSync here is what trips opencode's task
 * loop into "Unexpected error occurred" + auto-stop). Capped at 4s.
 */
function queryJev(pythonPath, apiKey, task, candidates, wantTier) {
  const pythonScript = `
import sys, json, os

try:
    from typesafe_sdk import TypeSafeClient, Choice
except ImportError as e:
    sys.stderr.write(f"IMPORT_ERROR: {e}")
    sys.exit(2)

def get_val(ans):
    if ans is None:
        return None
    source = ans if isinstance(ans, dict) else ans
    for attr in ("choice", "value", "key", "selected"):
        v = source.get(attr) if isinstance(source, dict) else getattr(source, attr, None)
        if v is not None:
            return v
    return ans

try:
    payload = json.loads(sys.stdin.read())
    task = payload.get("task", "")
    candidates = payload.get("candidates", [])
    want_tier = payload.get("want_tier", False)

    questions = {}
    if want_tier:
        questions["tier"] = Choice(
            criteria={
                "fast": "Typos, simple lookups, docstrings, boilerplate",
                "balanced": "Standard bugs, test cases, isolated feature changes",
                "frontier": "Multi-file refactors, architecture design, complex algorithmic logic",
            },
            instructions="Select the appropriate model capability tier for this task.",
        )
    if candidates:
        questions["target"] = Choice(
            criteria={c: f"Agent resource: {c}" for c in candidates[:250]},
            instructions="Select the primary matching agent skill, workflow, or memory document.",
        )

    if not questions:
        print(json.dumps({}))
        sys.exit(0)

    client = TypeSafeClient(api_key=os.environ.get("TYPESAFE_API_KEY"))
    res = client.system_one(state=f"User Task: {task}", questions=questions)
    answers = getattr(res, "answers", {})
    out = {}
    if want_tier:
        out["tier"] = get_val(answers.get("tier"))
    if candidates:
        out["target"] = get_val(answers.get("target"))
    print(json.dumps({k: v for k, v in out.items() if v}))
except Exception as e:
    sys.stderr.write(f"QUERY_ERROR: {e}")
    sys.exit(1)
`;
  const childEnv = { ...process.env };
  if (apiKey) childEnv.TYPESAFE_API_KEY = apiKey;

  return new Promise((resolve) => {
    let proc;
    try {
      proc = spawn(pythonPath, ["-c", pythonScript], {
        env: childEnv,
        stdio: ["pipe", "pipe", "pipe"],
      });
    } catch (err) {
      pluginLog(`queryJev spawn threw: ${err.message}`);
      resolve(null);
      return;
    }

    const timer = setTimeout(() => {
      pluginLog("queryJev timed out (4s), killing child");
      proc.kill();
    }, 4000);

    let stdout = "";
    let stderr = "";
    proc.stdout.on("data", (d) => (stdout += d));
    proc.stderr.on("data", (d) => (stderr += d));
    proc.on("error", (err) => {
      clearTimeout(timer);
      pluginLog(`queryJev child error: ${err.message}`);
      resolve(null);
    });
    proc.on("close", (code) => {
      clearTimeout(timer);
      if (code === 0 && stdout) {
        try {
          resolve(JSON.parse(stdout.trim()));
          return;
        } catch (err) {
          pluginLog(`queryJev JSON parse failed: ${err.message}`);
        }
      }
      const stderrTrimmed = String(stderr).trim().slice(0, 300);
      pluginLog(`queryJev failed (status ${code}): ${stderrTrimmed}`);

      // Surface import errors so the user knows the SDK is missing
      if (code === 2 && stderrTrimmed.includes("IMPORT_ERROR")) {
        console.warn(
          `[jev-plugin] ⚠️  TypeSafe SDK not found in Python environment. ` +
          `Skills and model routing are disabled. Check your venv path.`
        );
      }

      resolve(null);
    });

    try {
      proc.stdin.write(JSON.stringify({ task, candidates, want_tier: Boolean(wantTier) }));
      proc.stdin.end();
    } catch (err) {
      pluginLog(`queryJev stdin write failed: ${err.message}`);
    }
  });
}

/**
 * Split a "providerID/modelID" string into its parts (modelID may be absent).
 */
function splitModelId(modelId) {
  if (typeof modelId !== "string" || !modelId) return null;
  const slash = modelId.indexOf("/");
  if (slash > 0 && slash < modelId.length - 1) {
    return { providerID: modelId.slice(0, slash), modelID: modelId.slice(slash + 1) };
  }
  return { providerID: modelId, modelID: modelId };
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

export const JevPlugin = async () => ({
  "chat.message": async (input, output) => {
    try {
      const promptText = extractUserPrompt(input, output);
      if (!promptText) return;

      const cwd = process.cwd();
      const settings = loadSettings();

      const configuredModels = Object.values(settings.models).filter((id) => typeof id === "string" && id.trim());
      const wantTier = settings.enable_model_routing && configuredModels.length > 0;

      // Resolve scan paths relative to the current working workspace
      const searchDirs = settings.scanPaths.map((p) => path.resolve(cwd, p));
      const allFiles = searchDirs.flatMap((d) => scanResourceFiles(d));

      const fileMap = new Map();
      for (const file of allFiles) {
        const rel = path.relative(cwd, file).replace(/\\/g, "/");
        fileMap.set(rel, file);
      }
      const candidates = Array.from(fileMap.keys());

      if (!wantTier && candidates.length === 0) return;

      pluginLog(`chat.message fired: prompt="${promptText.slice(0, 80)}" wantTier=${wantTier} candidates=${candidates.length} cwd=${cwd}`);

      const startTime = Date.now();
      const result = await queryJev(settings.pythonPath, settings.apiKey, promptText, candidates, wantTier);
      const elapsed = Date.now() - startTime;
      pluginLog(`queryJev returned in ${elapsed}ms tier=${result?.tier} target=${result?.target}`);

      // 1. Forced model switch (only when routing is enabled)
      if (wantTier && result?.tier) {
        const modelId = settings.models[result.tier];
        if (modelId && output?.message?.model) {
          const parts = splitModelId(modelId);
          output.message.model = parts;
          pluginLog(`Forced model switch → ${result.tier}: ${parts.providerID}/${parts.modelID}`);
          console.log(
            `[jev-plugin] ⚡ Forced model switch → ${result.tier}: ${parts.providerID}/${parts.modelID} in ${elapsed}ms`
          );
        }
      }

      // 2. Skill injection — schema-safe (see injectIntoUserMessage)
      const selectedRel = result?.target;
      if (selectedRel && fileMap.has(selectedRel)) {
        const fullPath = fileMap.get(selectedRel);
        let content = fs.readFileSync(fullPath, "utf-8");

        // Truncate to prevent bloating the LLM context
        if (content.length > MAX_INJECT_CHARS) {
          content = content.slice(0, MAX_INJECT_CHARS) + "\n…[truncated]";
          pluginLog(`Truncated skill ${selectedRel} from ${fs.statSync(fullPath).size} to ${MAX_INJECT_CHARS} chars`);
        }

        console.log(`[jev-plugin] ⚡ Selected ${selectedRel} in ${elapsed}ms`);
        pluginLog(`Injecting skill ${selectedRel} (${content.length} chars)`);

        const injectedNotice = `\n\n[Active Capability / Skill: ${selectedRel}]\n${content}\n`;

        const injected = injectIntoUserMessage(output, injectedNotice);
        pluginLog(
          `inject ${injected ? "ok" : "skipped"}; parts=${JSON.stringify(
            (output?.parts || []).map((p) => p && p.type)
          )}`
        );
      }
    } catch (err) {
      pluginLog(`hook errored (bypassed safely): ${err.message}`);
      console.warn("[jev-plugin] Execution bypassed safely:", err.message);
    }
  },
});

export default JevPlugin;