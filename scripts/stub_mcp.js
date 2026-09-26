/**
 * Stub jev-engine MCP server for tests/test_plugin.mjs.
 *
 * Speaks the same wire format as `jev_mcp.py` — newline-delimited JSON-RPC 2.0
 * over stdin/stdout — so the plugin's hand-rolled client is exercised against a
 * real child process rather than a mock. Behaviour is driven entirely by env:
 *
 *   STUB_BODY            JSON of the tool body. A JSON *string* is passed
 *                        through verbatim, to test non-JSON responses.
 *   STUB_IS_ERROR=1      answer with isError: true
 *   STUB_NO_TEXT=1       answer with an empty content array
 *   STUB_CHUNKS          {method: [piece, ...]} written raw, with a short delay
 *                        between pieces (the framing test)
 *   STUB_HANG=1          never answer tools/call
 *   STUB_EXIT_ON_CALL=1  exit(1) instead of answering tools/call
 *
 * Run: node scripts\stub_mcp.js  (normally spawned by the test, not by hand)
 */

let buf = "";

function chunksFor(method) {
  if (!process.env.STUB_CHUNKS) return null;
  try {
    const map = JSON.parse(process.env.STUB_CHUNKS);
    return Array.isArray(map[method]) ? map[method] : null;
  } catch {
    return null;
  }
}

function answer(msg) {
  if (msg.method === "initialize") {
    return {
      jsonrpc: "2.0",
      id: msg.id,
      result: {
        protocolVersion: "2025-06-18",
        capabilities: {},
        serverInfo: { name: "jev-engine-stub", version: "0" },
      },
    };
  }

  if (process.env.STUB_EXIT_ON_CALL === "1") process.exit(1);
  if (process.env.STUB_HANG === "1") return null;

  let body = {};
  if (process.env.STUB_BODY) {
    try {
      body = JSON.parse(process.env.STUB_BODY);
    } catch {
      body = {};
    }
  }

  const result = process.env.STUB_NO_TEXT === "1" ? { content: [] } : {
    content: [
      { type: "text", text: typeof body === "string" ? body : JSON.stringify(body) },
    ],
  };
  if (process.env.STUB_IS_ERROR === "1") result.isError = true;
  return { jsonrpc: "2.0", id: msg.id, result };
}

process.stdin.on("data", (chunk) => {
  buf += chunk.toString();
  let idx;
  while ((idx = buf.indexOf("\n")) >= 0) {
    const line = buf.slice(0, idx);
    buf = buf.slice(idx + 1);
    if (!line.trim()) continue;
    let msg;
    try {
      msg = JSON.parse(line);
    } catch {
      continue;
    }
    if (msg.id === undefined) continue; // a notification: nothing to answer

    const pieces = chunksFor(msg.method);
    if (pieces) {
      // Written one at a time with a gap, so the reader really does see the
      // message in pieces rather than coalesced into one chunk.
      pieces.reduce((when, piece) => {
        setTimeout(() => process.stdout.write(piece), when);
        return when + 20;
      }, 0);
      continue;
    }

    const response = answer(msg);
    if (response) process.stdout.write(`${JSON.stringify(response)}\n`);
  }
});

process.stdin.on("end", () => process.exit(0));
