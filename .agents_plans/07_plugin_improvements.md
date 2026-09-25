# Phase 7: Plugin Improvements

> **Priority:** 🟡 High
> **Estimated effort:** ~1 hour
> **Files to modify:** `config/jev-plugin.example.js`

---

## Task 7A: Remove Hardcoded Absolute Paths

### Problem

The plugin at `config/jev-plugin.example.js` has two hardcoded absolute paths specific to the original developer's machine:

**Line 114:**
```js
const defaultVenvPy = "D:\\mcp\\jev-typesafe-mcp\\.venv\\Scripts\\python.exe";
```

**Line 121:**
```js
const envFile = "D:\\mcp\\jev-typesafe-mcp\\.env";
```

Anyone who clones the repo to a different location or uses a different drive will hit `fs.existsSync()` returning `false`, falling through to the generic `"python"` (which may not have the SDK installed) and missing the API key.

### Where to look

- File: `config/jev-plugin.example.js`, lines 113–126 (inside `loadSettings()`)

### Exact fix

Derive paths from the plugin's own location using `import.meta.url` or from a known relative path. Since this is an ESM module, `import.meta.url` is available:

```js
import { fileURLToPath } from "node:url";

// At the top of the file, after existing imports:
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const REPO_ROOT = path.resolve(__dirname, "..");  // config/ → repo root
```

Then replace the hardcoded paths:

```js
// Line 113-116: Replace the hardcoded defaultVenvPy
if (!pythonPath || !fs.existsSync(pythonPath)) {
    const defaultVenvPy = path.join(REPO_ROOT, ".venv", "Scripts", "python.exe");
    // Also try Unix-style venv path
    const defaultVenvPyUnix = path.join(REPO_ROOT, ".venv", "bin", "python");
    if (fs.existsSync(defaultVenvPy)) {
        pythonPath = defaultVenvPy;
    } else if (fs.existsSync(defaultVenvPyUnix)) {
        pythonPath = defaultVenvPyUnix;
    } else {
        pythonPath = "python";
    }
}

// Lines 120-126: Replace the hardcoded envFile
let apiKey = process.env.TYPESAFE_API_KEY || "";
if (!apiKey) {
    const envFile = path.join(REPO_ROOT, ".env");
    if (fs.existsSync(envFile)) {
        const match = fs.readFileSync(envFile, "utf-8").match(/TYPESAFE_API_KEY\s*=\s*['"]?([^'"\s\n]+)['"]?/);
        if (match) apiKey = match[1].trim();
    }
}
```

### Bonus: Cross-platform venv detection

The current code only checks for `Scripts/python.exe` (Windows). Adding the Unix path `bin/python` makes the plugin work on macOS/Linux too.

---

## Task 7B: Fix `.env` Quote Stripping

### Problem

Line 123–124 of the plugin:
```js
const match = fs.readFileSync(envFile, "utf-8").match(/TYPESAFE_API_KEY\s*=\s*(.+)/);
if (match) apiKey = match[1].trim();
```

If the `.env` file has:
```
TYPESAFE_API_KEY="sk-abc123"
```

The regex captures `"sk-abc123"` (including quotes) → the API key sent to TypeSafe includes quotes → 401 auth failure.

### Exact fix

Update the regex to strip optional quotes:

```js
const match = fs.readFileSync(envFile, "utf-8")
    .match(/TYPESAFE_API_KEY\s*=\s*['"]?([^'"\s\n]+)['"]?/);
```

This regex:
- `['"]?` — optionally matches an opening quote
- `([^'"\s\n]+)` — captures the key value (no quotes, no whitespace, no newlines)
- `['"]?` — optionally matches a closing quote

Handles all common `.env` formats:
- `TYPESAFE_API_KEY=sk-abc123` ✅
- `TYPESAFE_API_KEY="sk-abc123"` ✅
- `TYPESAFE_API_KEY='sk-abc123'` ✅
- `TYPESAFE_API_KEY = sk-abc123` ✅

---

## Task 7C: Limit Injected Skill Content Size

### Problem

In the `injectIntoUserMessage` flow (lines 402–418), the entire content of the matched skill file is read and injected into the user's message:

```js
const content = fs.readFileSync(fullPath, "utf-8");
const injectedNotice = `\n\n[Active Capability / Skill: ${selectedRel}]\n${content}\n`;
```

A large skill file (e.g. 50KB) is fully injected, consuming the LLM's context window. The MCP server limits resource reads to `MAX_CONTENT_CHARS = 6000`, but the plugin has no such limit.

### Where to look

- File: `config/jev-plugin.example.js`, lines 404–411

### Exact fix

Add a content length limit matching the MCP server's constant:

```js
const MAX_INJECT_CHARS = 6000;  // Match jev_engine MAX_CONTENT_CHARS

// Lines 404-411:
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
    // ... rest unchanged
}
```

---

## Task 7D: Add Diagnostic Logging for Python Import Failures

### Problem

When the inline Python script fails to import `typesafe_sdk` (line 207–209), stderr goes to `pluginLog()` but the user sees nothing. The plugin silently does nothing — no skill injection, no model routing. Debugging requires finding the plugin log file.

### Where to look

- File: `config/jev-plugin.example.js`, lines 285–301 (the `proc.on("close")` handler)

### Exact fix

Add a `console.warn` when the child exits with code 2 (import error):

```js
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
```

---

## Task 7E: Document That Plugin Should Ideally Use MCP Protocol (Not Raw Python)

### Problem

The plugin spawns a full Python process per user message (`queryJev` function, lines 201–311). This:
- Adds 1–3s latency per message (Python startup + import + TLS handshake)
- Doesn't reuse the MCP server that's already running
- Duplicates logic that exists in the engine

A better architecture would call the MCP tools over stdio/SSE. However, this is a significant refactor (the plugin would need to be an MCP client), so for now, document it as a known limitation and add a TODO comment.

### Exact fix

Add a comment block at the top of the `queryJev` function:

```js
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
```

---

## Checklist

- [x] Add `import { fileURLToPath } from "node:url"` and derive `REPO_ROOT` from `__dirname`
- [x] Replace hardcoded `D:\\mcp\\jev-typesafe-mcp\\.venv\\...` with `path.join(REPO_ROOT, ...)`
- [x] Replace hardcoded `D:\\mcp\\jev-typesafe-mcp\\.env` with `path.join(REPO_ROOT, ".env")`
- [x] Add Unix venv path fallback (`bin/python`)
- [x] Fix `.env` regex to strip quotes: `/TYPESAFE_API_KEY\s*=\s*['"]?([^'"\s\n]+)['"]?/`
- [x] Add `MAX_INJECT_CHARS = 6000` and truncate injected skill content
- [x] Add `console.warn` for import errors (exit code 2)
- [x] Add TODO comment about MCP protocol refactor
- [x] Manual test: copy plugin to a fresh location, verify it resolves paths correctly
