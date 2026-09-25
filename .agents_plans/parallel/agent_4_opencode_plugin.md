# Parallel Worker 4: OpenCode Plugin Security & Robustness

> **Target Agent**: Agent Chat 4 (runs concurrently with Agents 1, 2, 3)  
> **Exclusive File Ownership**:
> - [`config/jev-plugin.example.js`](file:///d:/mcp/jev-typesafe-mcp/config/jev-plugin.example.js)
> - [`tests/test_plugin.mjs`](file:///d:/mcp/jev-typesafe-mcp/tests/test_plugin.mjs)  
> ⚠️ **CRITICAL LOCK RULE**: Do NOT touch any Python (`*.py`) file.

---

## 1. Tasks Overview
1. Clean up child process leaks in `proc.on("error")` by explicitly ensuring `proc.kill()` is invoked.
2. Add `RetryPolicy` and 3-second timeout to the inline Python script inside `queryJev()`.
3. Guard against excessively large user prompts (truncate prompts > 100k chars).
4. Guard against bare model IDs in `splitModelId` (return `null` if not in `provider/model` format, and skip model switch gracefully).
5. Add recursion depth limit (max 6) to `scanResourceFiles`.
6. Add 2MB log rotation in `pluginLog`.

---

## 2. Exact Changes

### 2.1 `config/jev-plugin.example.js`

#### Log Rotation:
In `pluginLog` (lines 44–55):
```javascript
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

#### Depth-Bounded Scan:
In `scanResourceFiles` (lines 171–189):
```javascript
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

#### Inline Script Retries & Child Cleanup:
In `queryJev` inline Python script:
```python
try:
    from typesafe_sdk import TypeSafeClient, Choice, RetryPolicy
except ImportError as e:
    sys.stderr.write(f"IMPORT_ERROR: {e}")
    sys.exit(2)
...
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
In `proc.on("error")`:
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

#### Model ID & Prompt Validation:
In `splitModelId`:
```javascript
function splitModelId(modelId) {
  if (typeof modelId !== "string" || !modelId) return null;
  const slash = modelId.indexOf("/");
  if (slash > 0 && slash < modelId.length - 1) {
    return { providerID: modelId.slice(0, slash), modelID: modelId.slice(slash + 1) };
  }
  return null;
}
```
In `chat.message`:
```javascript
      let promptText = extractUserPrompt(input, output);
      if (!promptText) return;
      if (promptText.length > 100_000) {
        promptText = promptText.slice(0, 100_000) + "\n…[truncated]";
        pluginLog("User prompt exceeded 100k chars; truncated for Jev evaluation.");
      }
```
In model switch block:
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

### 2.2 `tests/test_plugin.mjs`
Add tests for `splitModelId`:
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

## 3. Verification Command
```bash
node tests/test_plugin.mjs
```
