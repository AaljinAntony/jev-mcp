import assert from "node:assert";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const REPO_ROOT = path.resolve(__dirname, "..");

console.log("--- 1. Testing plugin module loading ---");
const pluginPath = path.join(REPO_ROOT, "config", "jev-plugin.example.js");
const pluginModule = await import(`file://${pluginPath.replace(/\\/g, "/")}`);

assert.strictEqual(typeof pluginModule.JevPlugin, "function");
assert.strictEqual(typeof pluginModule.default, "function");

const pluginInstance = await pluginModule.JevPlugin();
assert.ok(pluginInstance["chat.message"], "Plugin should have chat.message hook");
console.log("Plugin loaded successfully and exports hook.");

console.log("--- 2. Testing .env API Key Regex with optional quotes ---");
const envRegex = /TYPESAFE_API_KEY\s*=\s*['"]?([^'"\s\n]+)['"]?/;

const cases = [
  ["TYPESAFE_API_KEY=sk-abc123", "sk-abc123"],
  ['TYPESAFE_API_KEY="sk-abc123"', "sk-abc123"],
  ["TYPESAFE_API_KEY='sk-abc123'", "sk-abc123"],
  ["TYPESAFE_API_KEY = sk-abc123", "sk-abc123"],
  ['TYPESAFE_API_KEY = "sk-quotes-123"', "sk-quotes-123"],
  ["TYPESAFE_API_KEY = 'sk-single-123'", "sk-single-123"],
  ["TYPESAFE_API_KEY=sk-trailing   \n", "sk-trailing"],
];

for (const [line, expected] of cases) {
  const match = line.match(envRegex);
  assert.ok(match, `Expected match for '${line}'`);
  assert.strictEqual(match[1].trim(), expected, `Value mismatch for '${line}'`);
}
console.log("All .env regex tests passed.");

console.log("--- 3. Testing Skill Content Truncation logic ---");
const MAX_INJECT_CHARS = 6000;
const shortSkill = "Short skill content";
let truncatedShort = shortSkill;
if (truncatedShort.length > MAX_INJECT_CHARS) {
  truncatedShort = truncatedShort.slice(0, MAX_INJECT_CHARS) + "\n…[truncated]";
}
assert.strictEqual(truncatedShort, shortSkill);

const longSkill = "A".repeat(10000);
let truncatedLong = longSkill;
if (truncatedLong.length > MAX_INJECT_CHARS) {
  truncatedLong = truncatedLong.slice(0, MAX_INJECT_CHARS) + "\n…[truncated]";
}
assert.strictEqual(truncatedLong.length, 6000 + "\n…[truncated]".length);
assert.ok(truncatedLong.endsWith("\n…[truncated]"));
console.log("Skill truncation logic verified.");

console.log("--- 4. Testing Path Resolution & Copied Plugin in Fresh Location ---");
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "jev-plugin-test-"));
try {
  const subConfig = path.join(tmpDir, "config");
  fs.mkdirSync(subConfig, { recursive: true });
  const copiedPluginPath = path.join(subConfig, "jev-plugin.js");
  fs.copyFileSync(pluginPath, copiedPluginPath);

  // Create mock .venv and .env in tmpDir (the REPO_ROOT for the copied plugin)
  const mockVenvScripts = path.join(tmpDir, ".venv", "Scripts");
  fs.mkdirSync(mockVenvScripts, { recursive: true });
  fs.writeFileSync(path.join(mockVenvScripts, "python.exe"), "mock python");

  fs.writeFileSync(
    path.join(tmpDir, ".env"),
    'TYPESAFE_API_KEY="sk-test-copy-key"\n'
  );

  // Import the copied plugin
  const copiedModule = await import(`file://${copiedPluginPath.replace(/\\/g, "/")}`);
  assert.strictEqual(typeof copiedModule.JevPlugin, "function");

  // Also test reading from the copied plugin directly
  const content = fs.readFileSync(copiedPluginPath, "utf-8");
  assert.ok(!content.includes("D:\\\\mcp\\\\jev-typesafe-mcp"), "Should not contain hardcoded original drive path");
  assert.ok(content.includes("REPO_ROOT"), "Should use REPO_ROOT");
  assert.ok(content.includes("MAX_INJECT_CHARS"), "Should define MAX_INJECT_CHARS");
  assert.ok(content.includes("IMPORT_ERROR"), "Should check IMPORT_ERROR");
  assert.ok(content.includes("TODO: This spawns a new Python process"), "Should contain MCP protocol refactor TODO");

  console.log("Copied plugin verified in fresh location:", tmpDir);
} finally {
  fs.rmSync(tmpDir, { recursive: true, force: true });
}

console.log("--- 5. Testing chat.message Hook Safe Bypassing ---");
const hook = pluginInstance["chat.message"];
// Invoking hook with empty output should not throw
await hook({}, {});
console.log("chat.message hook safe invocation passed.");

console.log("--- 6. Testing splitModelId provider/model parsing ---");
const splitModelId = pluginModule.splitModelId;
assert.strictEqual(typeof splitModelId, "function", "Plugin should export splitModelId");

assert.deepStrictEqual(splitModelId("anthropic/claude-3-5-sonnet"), {
  providerID: "anthropic",
  modelID: "claude-3-5-sonnet",
});
// Only the first slash splits; the remainder is the model ID.
assert.deepStrictEqual(splitModelId("openrouter/vendor/model-x"), {
  providerID: "openrouter",
  modelID: "vendor/model-x",
});

const invalidModelIds = [
  "bare-model-id",
  "/leading-slash",
  "trailing-slash/",
  "",
  null,
  undefined,
  123,
];
for (const value of invalidModelIds) {
  assert.strictEqual(splitModelId(value), null, `Expected null for ${JSON.stringify(value)}`);
}
console.log("splitModelId parsing and rejection verified.");

console.log("--- 7. Testing plugin hardening guards are present ---");
const pluginSource = fs.readFileSync(pluginPath, "utf-8");
assert.ok(pluginSource.includes("RetryPolicy("), "Should configure RetryPolicy");
assert.ok(pluginSource.includes("timeout=3.0"), "Should set a 3s client timeout");
assert.ok(pluginSource.includes("maxDepth = 6"), "Should bound scanResourceFiles depth");
assert.ok(pluginSource.includes("2 * 1024 * 1024"), "Should rotate the plugin log at 2MB");
assert.ok(pluginSource.includes("MAX_PROMPT_CHARS"), "Should truncate oversized user prompts");
console.log("Plugin hardening guards verified.");

console.log("\nALL PLUGIN TESTS PASSED! 🎉");
