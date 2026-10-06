/**
 * Klepa's MCP server for OpenClaw (spec 4.4, 4.5): Core's tools over stdio, as OpenClaw's own MCP client runs it.
 *
 * It holds no key and decides nothing. It lists Core's tools and passes each call to Core over Core's Unix socket,
 * with the signature that the adapter's before_tool_call put into the call's arguments; Core checks it. Like the
 * adapter it speaks HTTP/1.1 on a raw socket: OpenClaw routes node:http through the egress proxy.
 */

import { connect } from "node:net";
import { createInterface } from "node:readline";

import { parseReply } from "./index.ts";

const SOCKET = process.env.KLEPA_SOCKET ?? "";
const PROTOCOL = "2025-06-18";
const TIMEOUT_MS = 30_000;
const MAX_ANSWER_BYTES = 1024 * 1024;
const UNREACHABLE = "Klepa Core did not answer.";

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** One request to Core over its socket: GET /v1/tools or POST /v1/tool. */
export function askCore(
  socket: string,
  method: "GET" | "POST",
  path: string,
  body: Buffer = Buffer.alloc(0),
): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const conn = connect(socket);
    const chunks: Buffer[] = [];
    let size = 0;
    const timer = setTimeout(() => conn.destroy(new Error("Core did not answer in time")), TIMEOUT_MS);
    conn.on("connect", () => {
      const head =
        `${method} ${path} HTTP/1.1\r\nHost: klepa-core\r\nContent-Type: application/json\r\n` +
        `Content-Length: ${body.length}\r\nConnection: close\r\n\r\n`;
      conn.write(Buffer.concat([Buffer.from(head, "latin1"), body]));
    });
    conn.on("data", (chunk: Buffer) => {
      size += chunk.length;
      if (size > MAX_ANSWER_BYTES) {
        conn.destroy(new Error("Core's answer is too large"));
        return;
      }
      chunks.push(chunk);
    });
    conn.on("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    conn.on("close", (hadError) => {
      clearTimeout(timer);
      if (hadError) return;
      try {
        const reply = parseReply(Buffer.concat(chunks));
        if (reply.status !== 200) throw new Error(`Core answered ${reply.status}`);
        resolve(JSON.parse(reply.body.toString("utf8")));
      } catch (error) {
        reject(error);
      }
    });
  });
}

function send(message: Json): void {
  process.stdout.write(`${JSON.stringify(message)}\n`);
}

export async function answer(message: Record<string, unknown>, socket: string = SOCKET): Promise<Json | undefined> {
  const id = message.id as Json | undefined;
  if (id === undefined) return undefined; // a notification
  const params = isRecord(message.params) ? message.params : {};
  try {
    switch (message.method) {
      case "initialize": {
        const asked = typeof params.protocolVersion === "string" ? params.protocolVersion : PROTOCOL;
        const result = {
          protocolVersion: asked,
          capabilities: { tools: { listChanged: false } },
          serverInfo: { name: "klepa", version: "1" },
        };
        return { jsonrpc: "2.0", id, result };
      }
      case "ping":
        return { jsonrpc: "2.0", id, result: {} };
      case "tools/list": {
        const listed = await askCore(socket, "GET", "/v1/tools");
        return { jsonrpc: "2.0", id, result: { tools: (isRecord(listed) ? listed.tools : []) as Json } };
      }
      case "tools/call": {
        const call = { name: params.name, arguments: isRecord(params.arguments) ? params.arguments : {} };
        const result = await askCore(socket, "POST", "/v1/tool", Buffer.from(JSON.stringify(call), "utf8"));
        return { jsonrpc: "2.0", id, result: result as Json };
      }
      default:
        return { jsonrpc: "2.0", id, error: { code: -32601, message: "method not found" } };
    }
  } catch {
    return { jsonrpc: "2.0", id, error: { code: -32603, message: UNREACHABLE } };
  }
}

if ((import.meta as { main?: boolean }).main) {
  const lines = createInterface({ input: process.stdin });
  lines.on("line", (line) => {
    let message: unknown;
    try {
      message = JSON.parse(line);
    } catch {
      return;
    }
    if (isRecord(message)) {
      void answer(message).then((reply) => {
        if (reply !== undefined) send(reply);
      });
    }
  });
}
