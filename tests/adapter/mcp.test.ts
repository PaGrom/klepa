// Tests of the MCP server of the adapter's package: node --test tests/adapter/mcp.test.ts
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { answer } from "../../core/klepa_core/host/openclaw/adapter/mcp.ts";

/** Core's tool endpoints on a Unix socket: GET /v1/tools and POST /v1/tool. */
async function fakeCore(): Promise<{ socket: string; calls: unknown[]; close: () => void }> {
  const dir = mkdtempSync(join(tmpdir(), "klepa-mcp-"));
  const socket = join(dir, "core.sock");
  const calls: unknown[] = [];
  const server = createServer((conn) => {
    let raw = "";
    conn.on("data", (chunk) => {
      raw += chunk.toString("utf8");
      const end = raw.indexOf("\r\n\r\n");
      if (end < 0) return;
      const length = Number(/content-length: (\d+)/i.exec(raw)?.[1] ?? 0);
      if (raw.length < end + 4 + length) return;
      const body = raw.slice(end + 4, end + 4 + length);
      let reply: unknown = { tools: [{ name: "search", description: "find", inputSchema: { type: "object" } }] };
      if (raw.startsWith("POST /v1/tool ")) {
        calls.push(JSON.parse(body));
        reply = { isError: false, content: [{ type: "text", text: "{}" }] };
      }
      const out = JSON.stringify(reply);
      conn.end(`HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ${Buffer.byteLength(out)}\r\n\r\n${out}`);
    });
  });
  await new Promise<void>((resolve) => server.listen(socket, resolve));
  return { socket, calls, close: () => (server.close(), rmSync(dir, { recursive: true })) };
}

test("the server introduces itself and answers what it does not know with an error", async () => {
  const hello = await answer({ jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-03-26" } });
  assert.deepEqual(hello, {
    jsonrpc: "2.0",
    id: 1,
    result: {
      protocolVersion: "2025-03-26",
      capabilities: { tools: { listChanged: false } },
      serverInfo: { name: "klepa", version: "1" },
    },
  });
  assert.deepEqual(await answer({ jsonrpc: "2.0", id: 2, method: "ping" }), { jsonrpc: "2.0", id: 2, result: {} });
  assert.equal((await answer({ jsonrpc: "2.0", id: 3, method: "resources/list" }) as { error: { code: number } }).error.code, -32601);
  assert.equal(await answer({ jsonrpc: "2.0", method: "notifications/initialized" }), undefined);
});

test("tools are Core's, and a call goes to Core with its arguments as they are", async () => {
  const core = await fakeCore();
  try {
    const listed = await answer({ jsonrpc: "2.0", id: 1, method: "tools/list" }, core.socket);
    assert.deepEqual(listed, {
      jsonrpc: "2.0",
      id: 1,
      result: { tools: [{ name: "search", description: "find", inputSchema: { type: "object" } }] },
    });
    const args = { query: "x", _klepa: { run_id: "r", tool_call_id: "c", sig: "s" } };
    const called = await answer({ jsonrpc: "2.0", id: 2, method: "tools/call", params: { name: "search", arguments: args } }, core.socket);
    assert.deepEqual(called, { jsonrpc: "2.0", id: 2, result: { isError: false, content: [{ type: "text", text: "{}" }] } });
    assert.deepEqual(core.calls, [{ name: "search", arguments: args }]);
  } finally {
    core.close();
  }
});

test("without Core a call is an error, never a made-up result", async () => {
  const lost = await answer({ jsonrpc: "2.0", id: 9, method: "tools/call", params: { name: "search" } }, "/nonexistent.sock");
  assert.deepEqual(lost, { jsonrpc: "2.0", id: 9, error: { code: -32603, message: "Klepa Core did not answer." } });
});
