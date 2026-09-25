# Phase 6: OpenCode Plugin Hardening, Child Process Safety & Security

> **Phase**: 06  
> **Target Files**:
> - [`config/jev-plugin.example.js`](file:///d:/mcp/jev-typesafe-mcp/config/jev-plugin.example.js)
> - [`tests/test_plugin.mjs`](file:///d:/mcp/jev-typesafe-mcp/tests/test_plugin.mjs)  
> **Parallel Execution Track**:
> - Belongs to **Track D (OpenCode Plugin Hardening)**.
> - **Completely independent** of all Python modules! Can run in full parallel with Tracks A, B, and C.

---

## 1. Problem Context & Rationale

### Bug E & Risk 1: Child Process Leak on Error & Missing Retries
In `queryJev()`:
1. When `proc.on("error")` fires (e.g. process cannot be spawned, permissions error), the event handler logs the error and calls `resolve(null)`, but does not explicitly ensure `proc.kill()` is invoked.
2. In the inline Python script executed by the child process:
   ```python
   client = TypeSafeClient(api_key=os.environ.get("TYPESAFE_API_KEY"))
   ```
   It does not configure `RetryPolicy` or a client timeout. A slow network request hangs until the outer 4s timer fires and kills the process with SIGTERM.
   **Fix**: Import `RetryPolicy` and configure `TypeSafeClient(..., timeout=3.0, retry=RetryPolicy(max_retries=2, backoff_initial=0.5, backoff_max=5.0))`.

### Plugin Issue 1: Unbounded Input Length
In `chat.message`, `promptText` is extracted and directly serialized to the child process's stdin. If a user pastes a 20MB file or giant log trace, serialization and IPC overhead can cause latency or OOM in the Node.js event loop.
**Fix**: Cap `promptText` at `100_000` characters with a truncation marker.

### Plugin Issue 2: Broken Bare Model IDs in `splitModelId`
```javascript
function splitModelId(modelId) {
  if (typeof modelId !== "string" || !modelId) return null;
  const slash = modelId.indexOf("/");
  if (slash > 0 && slash < modelId.length - 1) {
    return { providerID: modelId.slice(0, slash), modelID: modelId.slice(slash + 1) };
  }
  return { providerID: modelId, modelID: modelId };
}
```
If a user configures `"fast": "claude-3-5-haiku"` without a provider slash, `splitModelId` sets `providerID: "claude-3-5-haiku"`. OpenCode tries to look up an AI provider named `"claude-3-5-haiku"`, which fails and causes message routing crashes.
**Fix**: Return `null` when no `/` is present, and skip the forced model switch with a clear log warning.

### Plugin Issue 3: Unbounded Recursion in `scanResourceFiles`
`scanResourceFiles(dir)` has no depth guard. In environments with deeply nested directories, recursive symlinks, or unintended links into node_modules, this can cause call stack overflow.
**Fix**: Add an optional `depth` parameter with `maxDepth = 6`.

### Plugin Issue 4: Unbounded Log Growth in `pluginLog`
`fs.appendFileSync` appends to `~/.config/opencode/logs/jev-plugin.log` without rotation. Over months of developer usage, this file grows indefinitely.
**Fix**: Check file size before writing; if `> 2MB`, rotate to `jev-plugin.log.1`.

---

## 2. Step-by-Step Code Changes

### Step 2.1: `config/jev-plugin.example.js` — Log Rotation

**Target lines** (`config/jev-plugin.example.js` lines 44–55):
```javascript
// Dedicated plugin log with 2MB rotation. Best effort only.
function pluginLog(msg) {
  try {
    const dir = path.join(os.homedir(), ".config", "opencode", "logs");
    fs.mkdirSync(dir, { recursive: true });
    const logFile = path.join(dir, "jev-plugin.log");
    
    // Rotate if > 2MB
    if (fs.existsSync(logFile)) {
      const stats = fs.statSync(logFile);
      if (stats.size > 2 * 1024 * 1024) {
        const backupFile = path.join(dir, "jev-plugin.log.1");
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
```

---

### Step 2.2: `config/jev-plugin.example.js` — Depth-Bounded Directory Scan

**Target lines** (`config/jev-plugin.example.js` lines 171–189):
```javascript
/**
 * Recursively discover all Markdown skill, workflow, and memory files (max depth 6)
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
```

---

### Step 2.3: `config/jev-plugin.example.js` — Inline TypeSafeClient Retries & Child Cleanup

**Target lines** (`config/jev-plugin.example.js` lines 234–288):
In the inline `pythonScript` template:
```python
try:
    from typesafe_sdk import TypeSafeClient, Choice, RetryPolicy
except ImportError as e:
    sys.stderr.write(f"IMPORT_ERROR: {e}")
    sys.exit(2)
```
Update client initialization:
```python
    client = TypeSafeClient(
        api_key=os.environ.get("TYPESAFE_API_KEY"),
        timeout=3.0,
        retry=RetryPolicy(
            max_retries=2,
            backoff_initial=0.5,
            backoff_max=5.0,
            backoff_jitter=0.25,
        ),
    )
    res = client.system_one(state=f"User Task: {task}", questions=questions)
```

In `queryJev()` error handling (lines 314–318):
```javascript
    proc.on("error", (err) => {
      clearTimeout(timer);
      try {
        proc.kill();
      } catch {}
      pluginLog(`queryJev child error: ${err.message}`);
      resolve(null);
    });
```

---

### Step 2.4: `config/jev-plugin.example.js` — Bare Model ID & Prompt Length Validation

**Target lines** (`config/jev-plugin.example.js` lines 355–363):
```javascript
/**
 * Split a "providerID/modelID" string into its parts.
 * Returns null if modelId does not specify a valid "provider/model" format.
 */
function splitModelId(modelId) {
  if (typeof modelId !== "string" || !modelId) return null;
  const slash = modelId.indexOf("/");
  if (slash > 0 && slash < modelId.length - 1) {
    return { providerID: modelId.slice(0, slash), modelID: modelId.slice(slash + 1) };
  }
  return null;
}
```

In `chat.message` hook (lines 399–403):
```javascript
      let promptText = extractUserPrompt(input, output);
      if (!promptText) return;
      if (promptText.length > 100_000) {
        promptText = promptText.slice(0, 100_000) + "\n…[truncated]";
        pluginLog("User prompt exceeded 100k chars; truncated for Jev evaluation.");
      }
```

In model switch handling (lines 430–439):
```javascript
        const modelId = settings.models[result.tier];
        const parts = splitModelId(modelId);
        if (parts && output?.message?.model) {
          output.message.model = parts;
          pluginLog(`Forced model switch → ${result.tier}: ${parts.providerID}/${parts.modelID}`);
          console.log(
            `[jev-plugin] ⚡ Forced model switch → ${result.tier}: ${parts.providerID}/${parts.modelID} in ${elapsed}ms`
          );
        } else if (modelId && !parts) {
          pluginLog(`Skipping forced model switch: '${modelId}' is not in 'provider/model' format.`);
        }
```

---

### Step 2.5: `tests/test_plugin.mjs` — Add Unit Tests
Add tests verifying `splitModelId` and `scanResourceFiles` depth:
```javascript
import assert from "node:assert/strict";
import test from "node:test";

test("splitModelId parses provider/model correctly", () => {
  assert.deepStrictEqual(splitModelId("anthropic/claude-3-5-sonnet"), {
    providerID: "anthropic",
    modelID: "claude-3-5-sonnet",
  });
  assert.strictEqual(splitModelId("bare-model-id"), null);
  assert.strictEqual(splitModelId("/leading-slash"), null);
  assert.strictEqual(splitModelId("trailing-slash/"), null);
  assert.strictEqual(splitModelId(""), null);
});
```

---

## 3. Verification Commands

Run plugin test suite:
```bash
node tests/test_plugin.mjs
```

Expected output:
- All plugin assertions pass without errors.

---

## 4. Acceptance Criteria
- [ ] Child process is killed cleanly on error events.
- [ ] Inline Python client configures `RetryPolicy` and 3-second timeout.
- [ ] `promptText` is capped at 100,000 characters before child dispatch.
- [ ] `splitModelId` rejects bare model IDs without throwing.
- [ ] `scanResourceFiles` respects depth bound (max 6).
- [ ] `pluginLog` performs 2MB log rotation.
- [ ] `node tests/test_plugin.mjs` passes.
