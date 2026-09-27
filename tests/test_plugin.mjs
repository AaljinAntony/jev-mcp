/**
 * Behavioural tests for config/jev-plugin.example.js.
 *
 * These assert what the plugin *does*, not what its source happens to contain.
 * The MCP client is exercised against a real child process speaking real
 * newline-delimited JSON-RPC (scripts/stub_mcp.js), so framing, handshakes,
 * error unwrapping and respawn are covered end to end.
 *
 * Run: node tests\test_plugin.mjs
 */
import assert from "node:assert";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const REPO_ROOT = path.resolve(__dirname, "..");

const tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), "jev-plugin-test-"));
const STUB = path.join(REPO_ROOT, "scripts", "stub_mcp.js");

// Keep every pluginLog write in the sandbox, and make the rotation test cheap.
process.env.JEV_PLUGIN_LOG_DIR = path.join(tmpRoot, "logs");
const LOG_DIR = process.env.JEV_PLUGIN_LOG_DIR;

let failures = 0;

async function test(name, fn) {
  try {
    await fn();
    console.log(`  ok  ${name}`);
  } catch (err) {
    failures += 1;
    console.error(`FAIL  ${name}\n      ${err && err.message ? err.message : err}`);
    if (process.env.JEV_TEST_TRACE) console.error(err);
  }
}

function withTimeout(promise, ms, label) {
  let timer;
  const guard = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms);
  });
  return Promise.race([promise, guard]).finally(() => clearTimeout(timer));
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitFor(predicate, ms, label) {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    if (predicate()) return true;
    await sleep(10);
  }
  throw new Error(`timed out after ${ms}ms waiting for ${label}`);
}

function tmpDir(name) {
  const dir = path.join(tmpRoot, name);
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

const pluginPath = path.join(REPO_ROOT, "config", "jev-plugin.example.js");
const pluginSource = fs.readFileSync(pluginPath, "utf-8");
const pluginModule = await import(`file://${pluginPath.replace(/\\/g, "/")}`);
const P = pluginModule.JevPlugin;

console.log("--- 1. Module loading ---");
const pluginInstance = await P();
assert.strictEqual(typeof pluginModule.default, "function");
assert.ok(pluginInstance["chat.message"], "plugin exposes a chat.message hook");
for (const name of [
  "splitModelId",
  "isInside",
  "isConfident",
  "isInjectable",
  "scanResourceFiles",
  "pluginLog",
  "describeText",
  "resolveServerCommand",
  "loadSettings",
  "resolveSearchDirs",
  "extractUserPrompt",
  "applyTier",
  "injectSkill",
  "injectIntoUserMessage",
  "parseEnvelope",
  "runQueries",
  "JevMcpClient",
]) {
  assert.strictEqual(typeof P[name], "function", `plugin exports ${name}()`);
}
console.log("Plugin loaded and exports every tested helper.");

console.log("--- 2. splitModelId ---");
await test("splits provider/model, first slash only", () => {
  assert.deepStrictEqual(P.splitModelId("anthropic/claude-3-5-sonnet"), {
    providerID: "anthropic",
    modelID: "claude-3-5-sonnet",
  });
  assert.deepStrictEqual(P.splitModelId("openrouter/vendor/model-x"), {
    providerID: "openrouter",
    modelID: "vendor/model-x",
  });
});
await test("bare or malformed ids return null (never a bogus model object)", () => {
  for (const value of ["bare-model-id", "/leading", "trailing/", "", "/", null, undefined, 123, {}]) {
    assert.strictEqual(P.splitModelId(value), null, `expected null for ${JSON.stringify(value)}`);
  }
});

console.log("--- 3. isInside containment ---");
await test("accepts the root and its descendants", () => {
  const root = tmpDir("inside");
  const kid = path.join(root, "a", "b");
  fs.mkdirSync(kid, { recursive: true });
  assert.strictEqual(P.isInside(root, root), true);
  assert.strictEqual(P.isInside(root, path.join(root, "a")), true);
  assert.strictEqual(P.isInside(root, kid), true);
});
await test("refuses a sibling with a shared name prefix", () => {
  const parent = tmpDir("prefix");
  const root = path.join(parent, "app");
  const evil = path.join(parent, "app-evil");
  fs.mkdirSync(root, { recursive: true });
  fs.mkdirSync(evil, { recursive: true });
  assert.strictEqual(P.isInside(root, evil), false);
});
await test("refuses '..' traversal and an unrelated absolute path", () => {
  const root = tmpDir("traverse");
  assert.strictEqual(P.isInside(root, path.join(root, "..", "outside")), false);
  assert.strictEqual(P.isInside(root, path.join(root, "..", "..", "..")), false);
  assert.strictEqual(P.isInside(root, path.parse(root).root), false);
  assert.strictEqual(P.isInside(root, os.homedir()), false);
});
await test("refuses a symlink that points out of the workspace", () => {
  const outside = tmpDir("symlink-target");
  fs.writeFileSync(path.join(outside, "secret.md"), "secret");
  const root = tmpDir("symlink-root");
  const link = path.join(root, "escape");
  try {
    fs.symlinkSync(outside, link, "junction");
  } catch {
    console.log("      (skipped: symlink creation not permitted here)");
    return;
  }
  assert.strictEqual(P.isInside(root, link), false, "a junction out of the root must be refused");
  assert.strictEqual(P.isInside(root, path.join(link, "secret.md")), false);
});

console.log("--- 4. scanResourceFiles depth bound ---");
await test("stops at maxDepth instead of walking a 10-deep tree", () => {
  const deep = tmpDir("deep");
  let dir = deep;
  for (let i = 0; i < 10; i += 1) {
    dir = path.join(dir, `d${i}`);
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, `f${i}.md`), `# ${i}`);
  }
  const found = P.scanResourceFiles(deep);
  const deepest = Math.max(
    ...found.map((f) => path.relative(deep, f).split(path.sep).length)
  );
  assert.ok(found.length > 0, "found the shallow files");
  assert.ok(deepest <= 7, `walk stopped at depth ${deepest} (maxDepth 6 + the file itself)`);
  assert.ok(
    found.some((f) => f.includes(`${path.sep}f5.md`)),
    "a file inside the bound is found"
  );
  assert.ok(
    !found.some((f) => f.includes(`${path.sep}f9.md`)),
    "a file past the bound is not"
  );
});
await test("ignores dotfiles and non-markdown files", () => {
  const dir = tmpDir("filter");
  fs.writeFileSync(path.join(dir, "SKILL.md"), "# skill");
  fs.writeFileSync(path.join(dir, "notes.txt"), "nope");
  fs.mkdirSync(path.join(dir, ".hidden"), { recursive: true });
  fs.writeFileSync(path.join(dir, ".hidden", "x.md"), "nope");
  assert.deepStrictEqual(P.scanResourceFiles(dir).map((f) => path.basename(f)), ["SKILL.md"]);
});

console.log("--- 5. pluginLog rotation and prompt privacy ---");
await test("rotates at 2MB and keeps exactly one backup", () => {
  const dir = path.join(tmpRoot, "rotate");
  fs.mkdirSync(dir, { recursive: true });
  const prevDir = process.env.JEV_PLUGIN_LOG_DIR;
  process.env.JEV_PLUGIN_LOG_DIR = dir;
  try {
    const chunk = "x".repeat(64 * 1024);
    for (let i = 0; i < 40; i += 1) P.pluginLog(chunk);
    const logFile = path.join(dir, "jev-plugin.log");
    const backup = `${logFile}.1`;
    assert.ok(fs.existsSync(backup), "a .1 backup was created");
    assert.ok(fs.statSync(backup).size > 2 * 1024 * 1024, "the backup holds the old log");
    assert.ok(fs.statSync(logFile).size < 2 * 1024 * 1024, "the live log is under the cap");
    assert.strictEqual(
      fs.readdirSync(dir).filter((n) => n.startsWith("jev-plugin.log")).length,
      2,
      "only the live log and one backup"
    );
  } finally {
    process.env.JEV_PLUGIN_LOG_DIR = prevDir;
  }
});
await test("describeText identifies a prompt without reproducing it", () => {
  const secret = "my password is hunter2 and I live at 12 Elm Street";
  const described = P.describeText(secret);
  assert.ok(!described.includes("hunter2"), "no prompt content in the digest");
  assert.ok(!described.includes("Elm"), "no prompt content in the digest");
  assert.ok(described.includes(`len=${secret.length}`), "length is recorded");
  assert.ok(/sha256=[0-9a-f]{12}/.test(described), "a short digest is recorded");
});

console.log("--- 6. JevMcpClient over a real stdio child ---");
function newClient(env = {}, extra = {}) {
  const log = [];
  const client = new P.JevMcpClient(process.execPath, STUB, {
    args: [],
    env: { ...process.env, ...env },
    log: (m) => log.push(m),
    idleMs: 60_000,
    killGraceMs: 0,
    ...extra,
  });
  return { client, log };
}
const TIERS_BODY = { action: "auto", confidence: 0.87, recommended_tier: "balanced" };
const runQueries = P.runQueries;
const cwd = tmpDir("hook-cwd");

await test("handshakes, then returns the parsed tool body", async () => {
  const { client, log } = newClient({ STUB_BODY: JSON.stringify(TIERS_BODY) });
  try {
    const res = await withTimeout(client.callTool("select_model_tier", { task: "x" }), 5000, "callTool");
    assert.deepStrictEqual(res, TIERS_BODY);
    assert.ok(log.some((l) => l.startsWith("client started")), "logged one client start");
    assert.strictEqual(log.filter((l) => l.startsWith("client started")).length, 1);
  } finally {
    client.stop("test end");
  }
});
await test("reuses the child across calls (no second spawn)", async () => {
  const { client, log } = newClient({ STUB_BODY: JSON.stringify(TIERS_BODY) });
  try {
    await withTimeout(client.callTool("select_model_tier", { task: "a" }), 5000, "call 1");
    await withTimeout(client.callTool("select_model_tier", { task: "b" }), 5000, "call 2");
    assert.strictEqual(
      log.filter((l) => l.startsWith("client started")).length,
      1,
      "one child served both calls"
    );
  } finally {
    client.stop("test end");
  }
});
await test("reassembles a response split across two writes", async () => {
  const body = JSON.stringify({ action: "auto", confidence: 0.91, primary: { file: "a.md", content: "x" } });
  const full = JSON.stringify({
    jsonrpc: "2.0",
    id: 2,
    result: { content: [{ type: "text", text: body }] },
  });
  // Cut the message in half, mid-object, and send it in two separate writes.
  // No newline between the pieces: the client must hold the partial line in its
  // buffer until the rest arrives.
  const cut = Math.floor(full.length / 2);
  const { client, log } = newClient({
    STUB_CHUNKS: JSON.stringify({ "tools/call": [full.slice(0, cut), `${full.slice(cut)}\n`] }),
  });
  try {
    const res = await withTimeout(client.callTool("search_agent_skills", { task: "x" }), 5000, "split write");
    assert.strictEqual(res.action, "auto");
    assert.strictEqual(res.primary.file, "a.md");
    assert.ok(!log.some((l) => l.startsWith("unparseable line")), "the partial line was not parsed early");
  } finally {
    client.stop("test end");
  }
});
await test("ignores a non-JRPC line and still resolves the request", async () => {
  const body = JSON.stringify(TIERS_BODY);
  const good = JSON.stringify({ jsonrpc: "2.0", id: 2, result: { content: [{ type: "text", text: body }] } });
  const { client, log } = newClient({
    STUB_CHUNKS: JSON.stringify({ "tools/call": ["not json at all\n", `${good}\n`] }),
  });
  try {
    const res = await withTimeout(client.callTool("select_model_tier", { task: "x" }), 5000, "noisy line");
    assert.deepStrictEqual(res, TIERS_BODY);
    assert.ok(log.some((l) => l.startsWith("unparseable line")), "logged the bad line");
  } finally {
    client.stop("test end");
  }
});
await test("unwraps the isError envelope into a typed failure", async () => {
  const envelope = {
    error: { code: "TIMEOUT", message: "The tool request timed out.", retryable: true },
  };
  const { client } = newClient({
    STUB_IS_ERROR: "1",
    STUB_BODY: JSON.stringify(envelope),
  });
  try {
    await assert.rejects(
      () => withTimeout(client.callTool("search_agent_skills", { task: "x" }), 5000, "isError"),
      (err) => {
        assert.strictEqual(err.message, "The tool request timed out.");
        assert.strictEqual(err.code, "TIMEOUT");
        assert.strictEqual(err.retryable, true);
        assert.deepStrictEqual(err.envelope, envelope);
        return true;
      }
    );
  } finally {
    client.stop("test end");
  }
});
await test("parses the envelope out from under the 'Error executing tool' prefix", () => {
  const text = 'Error executing tool search_agent_skills: {"error": {"code": "INVALID_INPUT", "message": "root_dir points to a filesystem root.", "retryable": false}}';
  const env = P.parseEnvelope(text);
  assert.strictEqual(env.error.code, "INVALID_INPUT");
  assert.strictEqual(env.error.retryable, false);
  assert.strictEqual(P.parseEnvelope("just prose"), null);
  assert.strictEqual(P.parseEnvelope('{"ok": true}'), null);
  assert.strictEqual(P.parseEnvelope(undefined), null);
});
await test("fails loudly on a non-JSON body and on a missing body", async () => {
  const bad = newClient({ STUB_BODY: JSON.stringify("not json at all") });
  try {
    await assert.rejects(
      () => withTimeout(bad.client.callTool("select_model_tier", { task: "x" }), 5000, "non-JSON"),
      /returned non-JSON/
    );
  } finally {
    bad.client.stop("test end");
  }
  const empty = newClient({ STUB_NO_TEXT: "1" });
  try {
    await assert.rejects(
      () => withTimeout(empty.client.callTool("select_model_tier", { task: "x" }), 5000, "no text"),
      /returned no text content/
    );
  } finally {
    empty.client.stop("test end");
  }
});
await test("rejects and stays usable when the child dies mid-call", async () => {
  const { client } = newClient({ STUB_EXIT_ON_CALL: "1" });
  await assert.rejects(
    () => withTimeout(client.callTool("select_model_tier", { task: "x" }), 5000, "dead child"),
    /exited|client/i
  );
  client.stop("test end");
});
await test("respawns after the child is killed", async () => {
  const { client, log } = newClient({ STUB_BODY: JSON.stringify(TIERS_BODY) });
  try {
    await withTimeout(client.callTool("select_model_tier", { task: "a" }), 5000, "call 1");
    const firstPid = client.proc.pid;
    client.proc.kill();
    await waitFor(() => client.proc === null, 5000, "the child to be reaped");
    const res = await withTimeout(client.callTool("select_model_tier", { task: "b" }), 5000, "call 2");
    assert.deepStrictEqual(res, TIERS_BODY);
    assert.notStrictEqual(client.proc.pid, firstPid, "a new child is serving the session");
    assert.strictEqual(log.filter((l) => l.startsWith("client started")).length, 2);
  } finally {
    client.stop("test end");
  }
});
await test("a request timeout rejects instead of hanging", async () => {
  const { client } = newClient({ STUB_HANG: "1" });
  try {
    await assert.rejects(
      () => client.callTool("select_model_tier", { task: "x" }, 150),
      /timed out after 150ms/
    );
  } finally {
    client.stop("test end");
  }
});
await test("abort() fails the pending call immediately", async () => {
  const { client, log } = newClient({ STUB_HANG: "1" });
  const started = Date.now();
  const pending = client.callTool("select_model_tier", { task: "x" }, 60_000);
  await sleep(150);
  client.abort("aborted by user");
  await assert.rejects(() => pending, /aborted by user/);
  assert.ok(Date.now() - started < 5000, "rejected long before the 60s timeout");
  assert.ok(log.some((l) => l.startsWith("aborting")), "logged the abort");
  client.stop("test end");
});
await test("the circuit breaker opens after 3 consecutive failures", async () => {
  const { client, log } = newClient({
    STUB_IS_ERROR: "1",
    STUB_BODY: JSON.stringify({ error: { code: "API_ERROR", message: "boom", retryable: true } }),
  });
  try {
    for (let i = 0; i < 3; i += 1) {
      const res = await runQueries(client, { task: "x", cwd, wantTier: false });
      assert.strictEqual(res.tierRes, null, "a failing call yields no decisions");
      assert.strictEqual(res.skillsRes, null, "a failing call yields no decisions");
    }
    assert.strictEqual(client.breakerOpen(), true, "breaker is open after 3 failures");
    assert.ok(log.some((l) => l.startsWith("circuit breaker opened")), "logged the breaker");
    const callsSoFar = log.filter((l) => l.includes("failed: boom")).length;
    const res = await runQueries(client, { task: "x", cwd, wantTier: false });
    assert.strictEqual(res, null, "an open breaker short-circuits, distinctly from a decision");
    assert.strictEqual(
      log.filter((l) => l.includes("failed: boom")).length,
      callsSoFar,
      "no further call was made"
    );
    assert.ok(log.some((l) => l.includes("circuit breaker is open")), "logged the skip");
  } finally {
    client.stop("test end");
  }
});
await test("a success closes the breaker again", () => {
  const { client } = newClient({ STUB_BODY: JSON.stringify(TIERS_BODY) });
  try {
    for (let i = 0; i < 3; i += 1) client.recordFailure(new Error("boom"));
    assert.strictEqual(client.breakerOpen(), true);
    client.recordSuccess();
    assert.strictEqual(client.breakerOpen(), false);
    assert.strictEqual(client.consecutiveFailures, 0);
  } finally {
    client.stop("test end");
  }
});
await test("runQueries returns decisions and releases the in-flight slot", async () => {
  const skills = { action: "auto", confidence: 0.9, primary: { file: "a.md", content: "x" } };
  const { client } = newClient({ STUB_CHUNKS: null, STUB_BODY: JSON.stringify(skills) });
  try {
    const res = await withTimeout(runQueries(client, { task: "x", cwd, wantTier: false }), 5000, "runQueries");
    assert.strictEqual(res.skillsRes.primary.file, "a.md");
    assert.strictEqual(res.tierRes, null, "no tier call when routing is off");
    assert.strictEqual(client.inFlight, 0, "the in-flight slot is released");
  } finally {
    client.stop("test end");
  }
});
await test("two sessions are judged at once, not serialised behind one lock", async () => {
  // This is the parallel-opencode regression: a single `busy` boolean made the
  // second window log "already in flight" and never get a decision.
  const skills = { action: "auto", confidence: 0.9, primary: { file: "a.md", content: "x" } };
  const { client, log } = newClient({ STUB_BODY: JSON.stringify(skills), STUB_DELAY_MS: "250" });
  try {
    const results = await withTimeout(
      Promise.all([
        runQueries(client, { task: "one", cwd, wantTier: false, turn: P.beginTurn("sess-a") }),
        runQueries(client, { task: "two", cwd, wantTier: false, turn: P.beginTurn("sess-b") }),
      ]),
      5000,
      "two concurrent queries"
    );
    for (const res of results) {
      assert.ok(res, "neither query was skipped");
      assert.strictEqual(res.skillsRes.primary.file, "a.md");
    }
    assert.strictEqual(client.inFlight, 0, "both slots released");
    assert.ok(
      !log.some((l) => l.includes("already in flight")),
      "no contention was reported"
    );
  } finally {
    client.stop("test end");
  }
});
await test("concurrency past the cap is skipped and logged, never queued", async () => {
  const skills = { action: "auto", confidence: 0.9, primary: { file: "a.md", content: "x" } };
  const { client, log } = newClient({ STUB_BODY: JSON.stringify(skills), STUB_DELAY_MS: "400" });
  try {
    const cap = P.MAX_CONCURRENT_QUERIES;
    const started = [];
    for (let i = 0; i < cap; i += 1) {
      started.push(runQueries(client, { task: `t${i}`, cwd, wantTier: false, turn: P.beginTurn(`sess-${i}`) }));
    }
    await sleep(150);
    assert.strictEqual(client.inFlight, cap, "the cap is exactly what is running");
    const overflow = await withTimeout(
      runQueries(client, { task: "overflow", cwd, wantTier: false, turn: P.beginTurn("sess-x") }),
      5000,
      "overflow query"
    );
    assert.strictEqual(overflow, null, "the message past the cap is skipped");
    assert.ok(
      log.some((l) => l.includes(`${cap} jev queries already in flight`)),
      "the skip says how many are running"
    );
    await withTimeout(Promise.all(started), 5000, "the capped queries");
  } finally {
    client.stop("test end");
  }
});
await test("an aborted message does not destroy another session's work", async () => {
  // abort() used to reject every pending request and kill the child, which was
  // only survivable while a single message could be in flight.
  const skills = { action: "auto", confidence: 0.9, primary: { file: "a.md", content: "x" } };
  const { client, log } = newClient({ STUB_BODY: JSON.stringify(skills), STUB_DELAY_MS: "300" });
  try {
    const mine = P.beginTurn("sess-mine");
    const theirs = P.beginTurn("sess-theirs");
    const myQuery = runQueries(client, { task: "mine", cwd, wantTier: false, turn: mine });
    const theirQuery = runQueries(client, { task: "theirs", cwd, wantTier: false, turn: theirs });
    // Wait for the request to be genuinely pending: the first call pays the child
    // spawn and the initialize handshake, so a fixed sleep can land before the
    // tools/call is ever registered.
    const minePending = () => Array.from(client.pending.values()).some((e) => e.owner === mine);
    const deadline = Date.now() + 5000;
    while (!minePending() && Date.now() < deadline) await sleep(20);
    assert.ok(minePending(), "the aborted message has a request in flight");
    client.abort("aborted by user", mine);
    const aborted = await withTimeout(myQuery, 5000, "aborted query");
    assert.strictEqual(aborted.skillsRes, null, "the aborted message got no decision");
    const res = await withTimeout(theirQuery, 5000, "the other session's query");
    assert.ok(res, "the other session kept its request");
    assert.strictEqual(res.skillsRes.primary.file, "a.md");
    assert.ok(
      log.some((l) => l.includes("aborting")),
      "the abort was scoped, not global"
    );
  } finally {
    client.stop("test end");
  }
});
await test("a superseded message is dropped, and only within its own session", async () => {
  const first = P.beginTurn("sess-x");
  assert.strictEqual(P.isCurrentTurn(first), true);
  const second = P.beginTurn("sess-x");
  assert.strictEqual(P.isCurrentTurn(first), false, "the older message lost its session");
  assert.strictEqual(P.isCurrentTurn(second), true);
  // A different session, and a payload with no session id, are untouched.
  const other = P.beginTurn("sess-y");
  assert.strictEqual(P.isCurrentTurn(other), true);
  assert.strictEqual(P.isCurrentTurn(second), true, "another session cannot supersede this one");
  assert.strictEqual(P.isCurrentTurn(P.beginTurn(null)), true, "no session id cannot be superseded");
  assert.strictEqual(P.isCurrentTurn(undefined), true, "a missing turn is treated as current");
});

console.log("--- 7. Settings, command resolution and path confinement ---");
await test("resolveServerCommand prefers the opencode.json mcp command", () => {
  const dir = tmpDir("cmd");
  const python = path.join(dir, "python.exe");
  const server = path.join(dir, "jev_mcp.py");
  fs.writeFileSync(python, "");
  fs.writeFileSync(server, "");
  const cfg = path.join(dir, "opencode.json");
  fs.writeFileSync(cfg, JSON.stringify({ mcp: { "jev-engine": { command: [python, server] } } }));
  assert.deepStrictEqual(P.resolveServerCommand([cfg]), { pythonPath: python, serverPath: server });
});
await test("resolveServerCommand falls back when the config points at a missing server", () => {
  const dir = tmpDir("cmd-missing");
  const cfg = path.join(dir, "opencode.json");
  fs.writeFileSync(
    cfg,
    JSON.stringify({ mcp: { "jev-engine": { command: ["python", path.join(dir, "nope.py")] } } })
  );
  const resolved = P.resolveServerCommand([cfg]);
  assert.ok(!resolved.serverPath.includes("nope.py"), "did not trust the broken command");
  assert.ok(resolved.serverPath.endsWith("jev_mcp.py"), "fell back to the bundled server");
});
await test("loadSettings reads jevs_settings.json without touching .env", () => {
  const dir = tmpDir("settings");
  fs.writeFileSync(
    path.join(dir, "jevs_settings.json"),
    JSON.stringify({
      enable_model_routing: true,
      models: { fast: "anthropic/claude-haiku", balanced: "anthropic/claude-sonnet" },
      scan_paths: ["shared-skills"],
    })
  );
  fs.writeFileSync(path.join(dir, ".env"), 'TYPESAFE_API_KEY="sk-should-be-ignored"\n');
  const s = P.loadSettings(dir);
  assert.strictEqual(s.enable_model_routing, true);
  assert.strictEqual(s.models.balanced, "anthropic/claude-sonnet");
  assert.ok(s.scanPaths.includes("shared-skills"), "configured scan path merged in");
  assert.ok(s.scanPaths.includes(".agents/skills"), "defaults kept");
  assert.strictEqual(s.apiKey, undefined, "the plugin no longer reads an API key");
  assert.strictEqual(P.loadSettings(dir).scanPaths.length, new Set(s.scanPaths).size, "deduplicated");
});
await test("resolveSearchDirs drops scan paths that escape the workspace", () => {
  const dir = tmpDir("scan");
  const outside = tmpDir("scan-outside");
  fs.writeFileSync(path.join(outside, "victim.md"), "secret");
  const settings = { scanPaths: ["skills", path.relative(dir, outside).replace(/\\/g, "/")] };
  const kept = P.resolveSearchDirs(settings, dir);
  assert.deepStrictEqual(kept, [path.join(dir, "skills")]);
});
await test("resolveSearchDirs keeps everything with the explicit escape hatch", () => {
  const dir = tmpDir("scan-allow");
  const prev = process.env.JEV_PLUGIN_ALLOW_OUTSIDE;
  process.env.JEV_PLUGIN_ALLOW_OUTSIDE = "1";
  try {
    const kept = P.resolveSearchDirs({ scanPaths: ["..", "skills"] }, dir);
    assert.strictEqual(kept.length, 2);
  } finally {
    if (prev === undefined) delete process.env.JEV_PLUGIN_ALLOW_OUTSIDE;
    else process.env.JEV_PLUGIN_ALLOW_OUTSIDE = prev;
  }
});

console.log("--- 8. applyTier decision table ---");
const CONFIDENT = { action: "auto", confidence: 0.87, recommended_tier: "balanced" };
const settings = { models: { balanced: "anthropic/claude-sonnet-4", fast: "gpt-4o-mini" } };
const outputWith = () => ({ message: { model: { providerID: "openai", modelID: "gpt-5" } } });

await test("auto + confident + valid id + existing model object => switched", () => {
  const output = outputWith();
  assert.strictEqual(P.applyTier(CONFIDENT, settings, output), true);
  assert.deepStrictEqual(output.message.model, {
    providerID: "anthropic",
    modelID: "claude-sonnet-4",
  });
});
await test("action review or escalate => not switched", () => {
  for (const action of ["review", "escalate", "none", undefined]) {
    const output = outputWith();
    assert.strictEqual(P.applyTier({ ...CONFIDENT, action }, settings, output), false, action);
    assert.deepStrictEqual(output.message.model, { providerID: "openai", modelID: "gpt-5" });
  }
});
await test("confidence below the 0.6 floor => not switched", () => {
  for (const confidence of [0.59, 0.2, 0, null, undefined, "0.9"]) {
    const output = outputWith();
    assert.strictEqual(P.applyTier({ ...CONFIDENT, confidence }, settings, output), false, String(confidence));
  }
  assert.strictEqual(P.PLUGIN_MIN_CONFIDENCE, 0.6, "the floor is the documented 0.6");
});
await test("a bare model id => not switched (no bogus model object)", () => {
  const output = outputWith();
  assert.strictEqual(P.applyTier({ ...CONFIDENT, recommended_tier: "fast" }, settings, output), false);
  assert.deepStrictEqual(output.message.model, { providerID: "openai", modelID: "gpt-5" });
});
await test("no existing output.message.model => not switched", () => {
  assert.strictEqual(P.applyTier(CONFIDENT, settings, {}), false);
  assert.strictEqual(P.applyTier(CONFIDENT, settings, { message: {} }), false);
  assert.strictEqual(P.applyTier(CONFIDENT, settings, undefined), false);
});
await test("no recommended tier or no configured model => not switched", () => {
  const output = outputWith();
  assert.strictEqual(P.applyTier({ action: "auto", confidence: 0.9 }, settings, output), false);
  assert.strictEqual(P.applyTier({ ...CONFIDENT, recommended_tier: "frontier" }, settings, output), false);
  assert.deepStrictEqual(output.message.model, { providerID: "openai", modelID: "gpt-5" });
});

console.log("--- 9. injectSkill decision table ---");
const injectCwd = tmpDir("inject-cwd");
const confidentSkills = (content) => ({
  action: "auto",
  confidence: 0.82,
  primary: { name: "demo", file: ".agents/skills/demo/SKILL.md", content },
  resources: [],
});
const textOutput = () => ({ parts: [{ id: "prt_1", type: "text", text: "do the thing" }] });

await test("auto + confident => injected", () => {
  const output = textOutput();
  assert.strictEqual(P.injectSkill(confidentSkills("SKILL BODY"), injectCwd, output), true);
  assert.ok(output.parts[0].text.includes("[Active Capability / Skill: .agents/skills/demo/SKILL.md]"));
  assert.ok(output.parts[0].text.includes("SKILL BODY"));
});
await test("review at or above the floor => injected (advisory skill hint)", () => {
  const output = textOutput();
  const res = { ...confidentSkills("REVIEW BODY"), action: "review", confidence: 0.64 };
  assert.strictEqual(P.injectSkill(res, injectCwd, output), true);
  assert.ok(output.parts[0].text.includes("[Active Capability / Skill: .agents/skills/demo/SKILL.md]"));
  assert.ok(output.parts[0].text.includes("REVIEW BODY"));
});
await test("escalate / weak confidence => not injected", () => {
  for (const res of [
    { ...confidentSkills("X"), action: "escalate" },
    { ...confidentSkills("X"), confidence: 0.4 },
    { ...confidentSkills("X"), confidence: null },
    { ...confidentSkills("X"), action: "review", confidence: 0.59 },
  ]) {
    const output = textOutput();
    assert.strictEqual(P.injectSkill(res, injectCwd, output), false, `${res.action}/${res.confidence}`);
    assert.strictEqual(output.parts[0].text, "do the thing", "message text untouched");
  }
});
await test("oversized content is truncated to MAX_INJECT_CHARS", () => {
  const output = textOutput();
  assert.strictEqual(P.injectSkill(confidentSkills("A".repeat(20_000)), injectCwd, output), true);
  const text = output.parts[0].text;
  assert.ok(text.includes("\n…[truncated]"), "marked as truncated");
  assert.ok(!text.includes("A".repeat(P.MAX_INJECT_CHARS + 1)), "nothing past the cap survived");
  assert.ok(text.includes("A".repeat(1000)), "the beginning of the skill survived");
});
await test("an out-of-workspace file is refused", () => {
  for (const bad of ["../../secret.md", path.join(injectCwd, "..", "escape.md"), "..\\..\\secret.md"]) {
    const output = textOutput();
    const res = {
      ...confidentSkills("SECRET"),
      primary: { name: "evil", file: bad, content: "SECRET" },
    };
    assert.strictEqual(P.injectSkill(res, injectCwd, output), false, bad);
    assert.strictEqual(output.parts[0].text, "do the thing");
  }
});
await test("no primary or no content => not injected", () => {
  const output = textOutput();
  assert.strictEqual(P.injectSkill({ action: "auto", confidence: 0.9, primary: null }, injectCwd, output), false);
  assert.strictEqual(
    P.injectSkill({ action: "auto", confidence: 0.9, primary: { file: "a.md" } }, injectCwd, output),
    false
  );
  assert.strictEqual(P.injectSkill(undefined, injectCwd, output), false);
  assert.strictEqual(output.parts[0].text, "do the thing");
});

console.log("--- 10. injectIntoUserMessage PartV2 mutation semantics ---");
await test("mutates the existing part object; never pushes a new one", () => {
  const original = { id: "prt_1", type: "text", text: "hello" };
  const output = { parts: [original, { id: "prt_2", type: "text", text: "second" }] };
  assert.strictEqual(P.injectIntoUserMessage(output, "\nNOTICE"), true);
  assert.strictEqual(output.parts.length, 2, "no part was pushed");
  assert.strictEqual(output.parts[0], original, "the same object was mutated");
  assert.strictEqual(output.parts[0].text, "hello\n\nNOTICE", "the notice is appended on its own line");
  assert.strictEqual(output.parts[1].text, "second", "only the first text part changed");
});
await test("does not double the newline when the text already ends with one", () => {
  const output = { parts: [{ type: "text", text: "hello\n" }] };
  P.injectIntoUserMessage(output, "NOTICE");
  assert.strictEqual(output.parts[0].text, "hello\nNOTICE");
});
await test("falls back to message.text, and otherwise refuses safely", () => {
  const msg = { message: { text: "hello" } };
  assert.strictEqual(P.injectIntoUserMessage(msg, "N"), true);
  assert.strictEqual(msg.message.text, "helloN");
  assert.strictEqual(P.injectIntoUserMessage({ parts: [{ type: "tool", text: "x" }] }, "N"), false);
  assert.strictEqual(P.injectIntoUserMessage({}, "N"), false);
  assert.strictEqual(P.injectIntoUserMessage(undefined, "N"), false);
});

console.log("--- 11. extractUserPrompt ---");
await test("finds the prompt across the payload shapes opencode uses", () => {
  assert.strictEqual(P.extractUserPrompt({}, { parts: [{ type: "text", text: " a " }] }), "a");
  assert.strictEqual(P.extractUserPrompt({}, { message: { text: "b" } }), "b");
  assert.strictEqual(P.extractUserPrompt({}, { message: { content: "c" } }), "c");
  assert.strictEqual(P.extractUserPrompt({}, { messages: [{ content: "d" }] }), "d");
  assert.strictEqual(P.extractUserPrompt({ messages: [{ parts: [{ type: "text", text: "e" }] }] }, {}), "e");
  assert.strictEqual(P.extractUserPrompt({}, {}), "");
  assert.strictEqual(P.extractUserPrompt({}, { parts: [{ type: "tool" }] }), "");
});

console.log("--- 12. The chat.message hook ---");
const hook = pluginInstance["chat.message"];
await test("an empty payload is a no-op, not a throw", async () => {
  await hook({}, {});
  await hook(undefined, undefined);
  await hook({ messages: [] }, { parts: [] });
});
await test("a project with no skills and routing off returns before spawning", async () => {
  const dir = tmpDir("bare-project");
  fs.writeFileSync(
    path.join(dir, "jevs_settings.json"),
    JSON.stringify({ enable_model_routing: false, models: {}, scan_paths: [] })
  );
  const logDirBefore = fs.existsSync(LOG_DIR) ? fs.readdirSync(LOG_DIR) : [];
  const prevCwd = process.cwd();
  process.chdir(dir);
  try {
    await hook({}, { parts: [{ type: "text", text: "hello there" }] });
  } finally {
    process.chdir(prevCwd);
  }
  const logDirAfter = fs.existsSync(LOG_DIR) ? fs.readdirSync(LOG_DIR) : [];
  assert.deepStrictEqual(logDirAfter, logDirBefore, "nothing was logged, so nothing was spawned");
});
await test("a server that exits at startup fails fast, and the hook survives it", async () => {
  const dir = tmpDir("dead-server");
  const deadServer = path.join(dir, "dead.js");
  fs.writeFileSync(deadServer, "process.exit(1);\n");

  // A project with skills and routing on, pointed at a server that never starts.
  const skillDir = path.join(dir, ".agents", "skills", "demo");
  fs.mkdirSync(skillDir, { recursive: true });
  fs.writeFileSync(path.join(skillDir, "SKILL.md"), "# demo");
  fs.writeFileSync(
    path.join(dir, "jevs_settings.json"),
    JSON.stringify({ enable_model_routing: true, models: { fast: "anthropic/claude-haiku" }, scan_paths: [] })
  );
  fs.writeFileSync(
    path.join(dir, "opencode.json"),
    JSON.stringify({ mcp: { "jev-engine": { command: [process.execPath, deadServer] } } })
  );

  const logFile = path.join(LOG_DIR, "jev-plugin.log");
  const before = fs.existsSync(logFile) ? fs.readFileSync(logFile, "utf-8") : "";
  const output = {
    parts: [{ type: "text", text: "hello" }],
    message: { model: { providerID: "a", modelID: "b" } },
  };
  const prevCwd = process.cwd();
  process.chdir(dir);
  try {
    // Must not throw: this is the "opencode does not error" acceptance case.
    await withTimeout(hook({}, output), 20_000, "hook with a dead server");
  } finally {
    process.chdir(prevCwd);
    P.getClient({ pythonPath: "none", serverPath: "none" }).stop("test end");
  }

  assert.strictEqual(output.parts[0].text, "hello", "nothing was injected");
  assert.deepStrictEqual(output.message.model, { providerID: "a", modelID: "b" }, "no model switch");
  const after = fs.existsSync(logFile) ? fs.readFileSync(logFile, "utf-8") : "";
  const added = after.slice(before.length);
  assert.ok(/client started/.test(added), "the child was attempted");
  assert.ok(/exited|handshake|failed/.test(added), `the failure was logged:\n${added}`);
});

console.log("--- 13. Privacy and gating invariants ---");
await test("a prompt is logged as a length plus a digest, never as text", () => {
  const secret = "my private prompt about the acquisition";
  const described = P.describeText(secret);
  assert.ok(!described.includes("private"), `the prompt leaked into the log: ${described}`);
  assert.ok(/^len=\d+ sha256=[0-9a-f]{12}$/.test(described), `unexpected shape: ${described}`);
  // A digest, not a truncation: two prompts of the same length differ.
  const other = "my private prompt about the acquisitionX";
  assert.notStrictEqual(P.describeText(other), described);
  // A 200kB prompt still yields a short, bounded description.
  const huge = "x".repeat(200_000);
  assert.ok(P.describeText(huge).length < 64, "the description is not bounded");
});
await test("isConfident is the action AND the confidence floor, not either alone", () => {
  assert.strictEqual(P.isConfident({ action: "auto", confidence: 0.9 }), true);
  assert.strictEqual(P.isConfident({ action: "review", confidence: 0.99 }), false);
  assert.strictEqual(P.isConfident({ action: "escalate", confidence: 0.99 }), false);
  assert.strictEqual(P.isConfident({ action: "auto", confidence: 0.59 }), false);
  assert.strictEqual(P.isConfident({ action: "auto", confidence: 0.6 }), true);
  assert.strictEqual(P.isConfident({ action: "auto" }), false, "a missing confidence is not confidence");
  assert.strictEqual(P.isConfident({ action: "auto", confidence: "0.9" }), false, "a string is not a number");
  assert.strictEqual(P.isConfident(null), false);
  assert.strictEqual(P.isConfident(undefined), false);
});
await test("isInjectable admits review, isConfident does not (skill hint vs model switch)", () => {
  assert.strictEqual(P.isInjectable({ action: "auto", confidence: 0.9 }), true);
  assert.strictEqual(P.isInjectable({ action: "review", confidence: 0.99 }), true);
  assert.strictEqual(P.isInjectable({ action: "review", confidence: 0.6 }), true);
  assert.strictEqual(P.isInjectable({ action: "review", confidence: 0.59 }), false);
  assert.strictEqual(P.isInjectable({ action: "escalate", confidence: 0.99 }), false, "a forced pick is never a hint");
  assert.strictEqual(P.isInjectable({ action: "auto", confidence: "0.9" }), false, "a string is not a number");
  assert.strictEqual(P.isInjectable(null), false);
  assert.strictEqual(P.isInjectable(undefined), false);
  // The model switch keeps the strict gate: only `auto` moves a model.
  assert.strictEqual(P.isConfident({ action: "review", confidence: 0.99 }), false);
});
await test("the log records the digest, and never an interpolated prompt", () => {
  // Behavioural, not a substring check: put a marker in a prompt the plugin
  // logs, then prove the marker is absent from the log file afterwards.
  const marker = "ZZPROMPTZZ-unique-marker";
  const logFile = path.join(LOG_DIR, "jev-plugin.log");
  fs.mkdirSync(LOG_DIR, { recursive: true });
  fs.writeFileSync(logFile, "");
  P.pluginLog(`prompt ${P.describeText(marker)}`);
  const contents = fs.readFileSync(logFile, "utf-8");
  assert.ok(!contents.includes(marker), "the raw prompt reached the log");
  assert.ok(contents.includes("sha256="), "no digest was logged instead");
});
await test("the plugin carries no inline Python and no direct SDK use", () => {
  // The one invariant that is architectural rather than behavioural: nothing in
  // this file may reach the TypeSafe SDK except through the MCP child, because
  // an inline Python path would silently bypass every bound the server
  // enforces. No behavioural test can fail if such dead code is re-added, so it
  // is asserted against the source — the file the drift check below proves is
  // byte-identical to the installed plugin.
  for (const forbidden of [
    "typesafe_sdk",
    "TypeSafeClient",
    "pythonScript",
    "spawnSync",
    "readFileSync(targetEnv",
    "TYPESAFE_API_KEY\\s*=",
  ]) {
    assert.ok(!pluginSource.includes(forbidden), `source must not contain ${forbidden}`);
  }
  // One spawn site, and it is the MCP child.
  const spawns = pluginSource.match(/\bspawn\(/g) || [];
  assert.strictEqual(spawns.length, 1, `expected exactly one spawn() call, found ${spawns.length}`);
});

console.log("--- 14. Installed plugin drift ---");
const homePlugin = path.join(os.homedir(), ".config", "opencode", "plugins", "jev-plugin.js");
if (!fs.existsSync(homePlugin)) {
  console.warn(
    `SKIP: no installed plugin at ${homePlugin}. ` +
      `Copy config\\jev-plugin.example.js there per config\\README.md step 5.`
  );
} else {
  const installed = fs.readFileSync(homePlugin, "utf-8");
  if (installed !== pluginSource) {
    failures += 1;
    console.error(
      `\nFAIL: installed plugin has drifted from config/jev-plugin.example.js\n` +
        `  installed: ${homePlugin} (${installed.split("\n").length} lines)\n` +
        `  example:   ${pluginPath} (${pluginSource.split("\n").length} lines)\n` +
        `  Fix: Copy-Item config\\jev-plugin.example.js ` +
        `"$env:USERPROFILE\\.config\\opencode\\plugins\\jev-plugin.js" -Force\n`
    );
  } else {
    console.log("Installed plugin matches the committed example.");
  }
}

fs.rmSync(tmpRoot, { recursive: true, force: true });

if (failures > 0) {
  console.error(`\n${failures} PLUGIN TEST(S) FAILED.`);
  process.exit(1);
}
console.log("\nALL PLUGIN TESTS PASSED");
