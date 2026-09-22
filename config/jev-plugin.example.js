/**
 * jev-plugin.js — OpenCode `chat.message` hook for Jev AI skill routing.
 *
 * Source example:   D:\mcp\jev-typesafe-mcp\config\jev-plugin.example.js
 * Real install:     C:\Users\<you>\.config\opencode\plugins\jev-plugin.js
 *
 * Reads the Python interpreter and TYPESAFE_API_KEY from the same locations as
 * the MCP server, scans the configured skill dirs for Markdown, asks Jev which
 * single file is most relevant, and injects its content into the conversation.
 *
 * NOTE: The inline Python below targets the LEGACY JevClient / Choice(options=...)
 * / client.decide(...) API, which does NOT exist in typesafe-sdk == 0.7.1.
 * The hook fails gracefully (never crashes OpenCode) but performs no routing
 * until migrated. Use the MCP tool `search_agent_skills` for the supported path.
 */
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";

/**
 * Locate and read configuration and venv details from opencode.json
 */
function loadConfig() {
  const candidateConfigPaths = [
    path.join(process.cwd(), "opencode.json"),
    path.join(process.cwd(), ".opencode", "opencode.json"),
    path.join(os.homedir(), ".config", "opencode", "opencode.json"),
  ];

  let config = null;
  for (const configPath of candidateConfigPaths) {
    if (fs.existsSync(configPath)) {
      try {
        const raw = fs.readFileSync(configPath, "utf-8");
        config = JSON.parse(raw);
        break;
      } catch (err) {
        console.warn(`[jev-plugin] Failed reading ${configPath}:`, err.message);
      }
    }
  }

  // Extract python path from MCP configuration if available
  const mcpCommand = config?.mcp?.["jev-engine"]?.command;
  let pythonPath = Array.isArray(mcpCommand) && mcpCommand[0] ? mcpCommand[0] : null;

  // Fallback to default venv location if not explicitly defined
  if (!pythonPath || !fs.existsSync(pythonPath)) {
    const defaultVenvPy = "D:\\mcp\\jev-typesafe-mcp\\.venv\\Scripts\\python.exe";
    pythonPath = fs.existsSync(defaultVenvPy) ? defaultVenvPy : "python";
  }

  // Fallback .env loader for TYPESAFE_API_KEY
  let apiKey = process.env.TYPESAFE_API_KEY;
  if (!apiKey) {
    const envFile = "D:\\mcp\\jev-typesafe-mcp\\.env";
    if (fs.existsSync(envFile)) {
      const envContent = fs.readFileSync(envFile, "utf-8");
      const match = envContent.match(/TYPESAFE_API_KEY\s*=\s*(.+)/);
      if (match) {
        apiKey = match[1].trim();
      }
    }
  }

  return {
    pythonPath,
    apiKey,
    enable_model_routing: Boolean(config?.jev_settings?.enable_model_routing),
    scan_paths: Array.isArray(config?.jev_settings?.scan_paths)
      ? config.jev_settings.scan_paths
      : [".agents/skills", ".agents/workflows", ".agents/memory", ".opencode/skills"],
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
 * Query Jev AI via the configured Python environment
 */
function queryJev(pythonPath, apiKey, task, candidates) {
  if (!candidates || candidates.length === 0) return null;

  const pythonScript = `
import sys, json, os

try:
    from typesafe_ai import JevClient, Choice
except ImportError:
    try:
        from typesafe_sdk import JevClient, Choice
    except ImportError as e:
        sys.stderr.write(f"IMPORT_ERROR: {e}")
        sys.exit(2)

try:
    payload = json.loads(sys.stdin.read())
    task = payload.get("task", "")
    candidates = payload.get("candidates", [])

    client = JevClient(api_key=os.getenv("TYPESAFE_API_KEY"))
    res = client.decide(
        state=f"User Task: {task}",
        decisions={"target": Choice(options=candidates[:250])}
    )
    print(json.dumps({"target": res.target.value}))
except Exception as e:
    sys.stderr.write(f"DECISION_ERROR: {e}")
    sys.exit(1)
`;

  const childEnv = { ...process.env };
  if (apiKey) childEnv.TYPESAFE_API_KEY = apiKey;

  try {
    const proc = spawnSync(pythonPath, ["-c", pythonScript], {
      input: JSON.stringify({ task, candidates }),
      encoding: "utf-8",
      timeout: 4000,
      env: childEnv,
    });

    if (proc.status === 0 && proc.stdout) {
      const parsed = JSON.parse(proc.stdout.trim());
      return parsed.target;
    } else {
      console.warn(`[jev-plugin] Query failed (status ${proc.status}):`, proc.stderr?.trim());
    }
  } catch (err) {
    console.warn(`[jev-plugin] Execution failed with binary "${pythonPath}":`, err.message);
  }
  return null;
}

export const JevPlugin = async () => ({
  "chat.message": async (input, output) => {
    try {
      const promptText = extractUserPrompt(input, output);
      if (!promptText) return;

      const cwd = process.cwd();
      const config = loadConfig();

      // Resolve scan paths relative to the current working workspace
      const searchDirs = config.scan_paths.map((p) => path.resolve(cwd, p));
      const allFiles = searchDirs.flatMap((d) => scanResourceFiles(d));

      if (allFiles.length === 0) return;

      const fileMap = new Map();
      for (const file of allFiles) {
        const rel = path.relative(cwd, file).replace(/\\/g, "/");
        fileMap.set(rel, file);
      }

      const candidates = Array.from(fileMap.keys());
      const startTime = Date.now();
      const selectedRel = queryJev(config.pythonPath, config.apiKey, promptText, candidates);
      const elapsed = Date.now() - startTime;

      if (selectedRel && fileMap.has(selectedRel)) {
        const fullPath = fileMap.get(selectedRel);
        const content = fs.readFileSync(fullPath, "utf-8");

        console.log(`[jev-plugin] ⚡ Selected ${selectedRel} in ${elapsed}ms`);

        const injectedNotice = `\n\n[Active Capability / Skill: ${selectedRel}]\n${content}\n`;

        if (Array.isArray(output?.parts)) {
          output.parts.push({ type: "text", text: injectedNotice });
        } else if (output?.message) {
          output.message.text = (output.message.text || "") + injectedNotice;
        }
      }
    } catch (err) {
      console.warn("[jev-plugin] Execution bypassed safely:", err.message);
    }
  },
});

export default JevPlugin;