// Tests of the adapter plugin, run by Node's own test runner: node --test tests/adapter/adapter.test.ts
import assert from "node:assert/strict";
import { createHash, createHmac } from "node:crypto";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import {
  CoreLink,
  HOOKS,
  TOOL_DOMAIN,
  canonical,
  failureKind,
  toolSignature,
  type Boot,
  type Post,
  type Reply,
  UNREACHABLE,
  heartbeatFields,
  hookHandlers,
  lookup,
  newBoot,
  parseReply,
  postUnix,
  primaryModel,
  processBoot,
  quietLogger,
  readConfig,
  register,
  remember,
  runtimeOf,
  sign,
  signedBy,
} from "../../core/klepa_core/host/openclaw/adapter/index.ts";

const KEY = Buffer.alloc(32, 7);

function keyFile(): { dir: string; path: string } {
  const dir = mkdtempSync(join(tmpdir(), "klepa-adapter-"));
  const path = join(dir, "adapter.key");
  writeFileSync(path, KEY, { mode: 0o600 });
  return { dir, path };
}

/** Core in a box: answers each message with `answer(message)`, signed and echoing (boot_id, seq). */
function fakeCore(answer: (message: Record<string, unknown>) => Record<string, unknown>, status = 200) {
  const seen: Record<string, unknown>[] = [];
  const post: Post = async (_socket, body, signature) => {
    assert.equal(signature, sign(KEY, body));
    const message = JSON.parse(body.toString("utf8")) as Record<string, unknown>;
    seen.push(message);
    const reply = Buffer.from(JSON.stringify({ ...answer(message), boot_id: message.boot_id, seq: message.seq }));
    return { status, body: reply, signature: sign(KEY, reply) };
  };
  return { post, seen };
}

function link(post: Post, boot: Boot = newBoot()): { link: CoreLink; cleanup: () => void } {
  const { dir, path } = keyFile();
  return {
    link: new CoreLink({ socket: "/unused", keyFile: path, policy: [] }, post, boot),
    cleanup: () => rmSync(dir, { recursive: true }),
  };
}

test("a signature is the HMAC of the exact bytes, compared in full", () => {
  const body = Buffer.from('{"a":1}');
  const signature = sign(KEY, body);
  assert.equal(signature, createHmac("sha256", KEY).update(body).digest("hex"));
  assert.ok(signedBy(KEY, body, signature));
  assert.ok(!signedBy(KEY, Buffer.from('{"a":2}'), signature));
  assert.ok(!signedBy(KEY, body, signature.slice(0, 10)));
  assert.ok(!signedBy(KEY, body, ""));
});

test("the config needs the socket, the key file and the policy keys", () => {
  assert.deepEqual(readConfig({ socket: "/s", keyFile: "/k", policy: ["a.b"] }), {
    socket: "/s",
    keyFile: "/k",
    policy: ["a.b"],
  });
  assert.throws(() => readConfig(undefined), /missing/);
  assert.throws(() => readConfig({ keyFile: "/k", policy: [] }), /socket/);
  assert.throws(() => readConfig({ socket: "/s", policy: [] }), /keyFile/);
  assert.throws(() => readConfig({ socket: "/s", keyFile: "/k", policy: [1] }), /policy/);
});

test("a reply is parsed from its raw bytes", () => {
  const raw = Buffer.from(
    'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nX-Klepa-Signature: abc\r\nContent-Length: 2\r\n\r\n{}',
  );
  assert.deepEqual(parseReply(raw), { status: 200, body: Buffer.from("{}"), signature: "abc" });
  assert.throws(() => parseReply(Buffer.from("HTTP/1.1 200 OK\r\n")), /no header/);
  assert.throws(() => parseReply(Buffer.from("garbage\r\n\r\n{}")), /status line/);
  assert.throws(() => parseReply(Buffer.from("HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\n{}")), /cut short/);
  assert.throws(() => parseReply(Buffer.from("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")), /chunked/);
});

test("a message carries its type, boot and sequence, whatever the fields say", async () => {
  const core = fakeCore(() => ({ ok: true }));
  const { link: one, cleanup } = link(core.post);
  try {
    await one.send("heartbeat", { type: "turn_start", boot_id: "x", seq: 99, registrations: [] });
    await one.send("dispatch", { sender_id: "5" });
    assert.equal(core.seen[0]?.type, "heartbeat");
    assert.equal(core.seen[0]?.boot_id, one.bootId);
    assert.deepEqual(
      core.seen.map((message) => message.seq),
      [1, 2],
    );
    assert.match(one.bootId, /^[A-Za-z0-9_-]{8,64}$/);
  } finally {
    cleanup();
  }
});

test("registrations in one process share one boot and one sequence", async () => {
  assert.equal(processBoot(), processBoot());
  const core = fakeCore(() => ({ ok: true }));
  const boot = newBoot();
  const first = link(core.post, boot);
  const second = link(core.post, boot);
  try {
    await first.link.send("heartbeat", {});
    await second.link.send("turn_start", {});
    await first.link.send("dispatch", {});
    assert.deepEqual(
      core.seen.map((message) => [message.boot_id, message.seq]),
      [
        [boot.id, 1],
        [boot.id, 2],
        [boot.id, 3],
      ],
    );
  } finally {
    first.cleanup();
    second.cleanup();
  }
});

test("an answer that is not Core's is refused", async () => {
  const unsigned: Post = async () => ({ status: 200, body: Buffer.from("{}"), signature: "" });
  const forged: Post = async (_socket, body) => {
    const message = JSON.parse(body.toString("utf8")) as Record<string, unknown>;
    const reply = Buffer.from(JSON.stringify({ boot_id: message.boot_id, seq: message.seq }));
    return { status: 200, body: reply, signature: sign(Buffer.alloc(32, 8), reply) };
  };
  const other: Post = async () => {
    const reply = Buffer.from(JSON.stringify({ boot_id: "someone-else", seq: 1 }));
    return { status: 200, body: reply, signature: sign(KEY, reply) };
  };
  for (const [post, error] of [
    [unsigned, /not signed/],
    [forged, /not signed/],
    [other, /another message/],
    [fakeCore(() => ({ ok: false }), 409).post, /refused/],
  ] as [Post, RegExp][]) {
    const { link: one, cleanup } = link(post);
    try {
      await assert.rejects(one.send("heartbeat", {}), error);
    } finally {
      cleanup();
    }
  }
});

test("before_dispatch claims a message only when Core answers for it", async () => {
  const warnings: string[] = [];
  const answers: Record<string, unknown>[] = [{ handled: true, text: "stage 1" }, { handled: true }, { handled: false }];
  const core = fakeCore(() => answers.shift() ?? {});
  const { link: one, cleanup } = link(core.post);
  try {
    const hooks = hookHandlers(one, (reason) => warnings.push(reason));
    const event = { senderId: "222222", messageId: "7", sessionKey: "agent:main:telegram:direct:222222" };
    assert.deepEqual(await hooks.before_dispatch(event, {}), { handled: true, text: "stage 1" });
    assert.deepEqual(await hooks.before_dispatch(event, {}), { handled: true });
    assert.equal(await hooks.before_dispatch(event, {}), undefined);
    assert.deepEqual(
      { sender: core.seen[0]?.sender_id, message: core.seen[0]?.message_id, type: core.seen[0]?.type },
      { sender: "222222", message: "7", type: "dispatch" },
    );
    assert.deepEqual(warnings, []);
  } finally {
    cleanup();
  }
});

test("without Core, before_dispatch claims nothing and before_agent_run blocks", async () => {
  const warnings: string[] = [];
  const down: Post = async () => {
    throw new Error("connect ENOENT");
  };
  const { link: one, cleanup } = link(down);
  try {
    const hooks = hookHandlers(one, (reason) => warnings.push(reason));
    assert.equal(await hooks.before_dispatch({ senderId: "1" }, {}), undefined);
    assert.equal(await hooks.before_prompt_build({}, { runId: "r" }), undefined);
    assert.deepEqual(await hooks.before_agent_run({}, { runId: "r" }), {
      outcome: "block",
      reason: "klepa-unreachable",
      message: UNREACHABLE,
    });
    assert.equal(warnings.length, 3);
    assert.ok(warnings.every((warning) => !warning.includes("222222")));
  } finally {
    cleanup();
  }
});

test("the turn hooks report the run and pass on Core's decision", async () => {
  const answers: Record<string, unknown>[] = [
    { ok: true, instruction: "I", tools_allow: [] },
    { outcome: "block", message: "not now" },
    { outcome: "pass" },
    { outcome: "something else" },
  ];
  const core = fakeCore(() => answers.shift() ?? {});
  const { link: one, cleanup } = link(core.post);
  try {
    const hooks = hookHandlers(one, () => {});
    const ctx = { runId: "run-1", chatId: "222222", senderId: "222222", sessionKey: "agent:main:telegram:direct:222222" };
    assert.deepEqual(await hooks.before_prompt_build({}, ctx), { appendSystemContext: "I", toolsAllow: [] });
    assert.deepEqual(await hooks.before_agent_run({}, ctx), { outcome: "block", reason: "klepa", message: "not now" });
    assert.deepEqual(await hooks.before_agent_run({}, ctx), { outcome: "pass" });
    assert.deepEqual(await hooks.before_agent_run({}, ctx), { outcome: "block", reason: "klepa", message: UNREACHABLE });
    assert.deepEqual(
      [core.seen[0]?.type, core.seen[0]?.run_id, core.seen[0]?.chat_id, core.seen[0]?.session_key],
      ["prompt_built", "run-1", "222222", "agent:main:telegram:direct:222222"],
    );
    assert.equal(core.seen[1]?.type, "turn_start");
  } finally {
    cleanup();
  }
});

test("a run whose prompt lacks Core's instruction never reaches the model, whatever Core says next", async () => {
  // Core answered prompt_built after the plugin gave up waiting, or without an instruction: Core may have marked the
  // run, but its prompt has no instruction, so the plugin blocks it without asking.
  const warnings: string[] = [];
  const core = fakeCore((message) => (message.type === "turn_start" ? { outcome: "pass" } : { ok: true }));
  const { link: one, cleanup } = link(core.post);
  try {
    const hooks = hookHandlers(one, (reason) => warnings.push(reason));
    const ctx = { runId: "run-x", chatId: "1", senderId: "1", sessionKey: "agent:main:telegram:direct:1" };
    assert.equal(await hooks.before_prompt_build({}, ctx), undefined);
    assert.deepEqual(await hooks.before_agent_run({}, ctx), {
      outcome: "block",
      reason: "klepa-unreachable",
      message: UNREACHABLE,
    });
    assert.deepEqual(
      core.seen.map((message) => message.type),
      ["prompt_built"],
    );
    assert.deepEqual(warnings, ["turn_start: no instruction from Core"]);
  } finally {
    cleanup();
  }
});

test("the runs the plugin remembers are bounded, and the oldest goes first", () => {
  const runs = new Set<string>();
  for (const id of ["a", "b", "c"]) remember(runs, id, 2);
  assert.deepEqual([...runs], ["b", "c"]);
  remember(runs, "b", 2); // remembered again: now the newest
  remember(runs, "d", 2);
  assert.deepEqual([...runs], ["b", "d"]);
});

test("ids from the hook context win over the event's, and numbers become strings", async () => {
  const core = fakeCore(() => ({ ok: true }));
  const { link: one, cleanup } = link(core.post);
  try {
    await hookHandlers(one, () => {}).before_prompt_build({ senderId: 1, runId: "event" }, { senderId: 222222 });
    assert.deepEqual([core.seen[0]?.sender_id, core.seen[0]?.run_id], ["222222", "event"]);
  } finally {
    cleanup();
  }
});

test("configuration values are read by dotted keys", () => {
  const config = {
    gateway: { reload: { mode: "off" } },
    agents: {
      defaults: {
        model: { primary: "anthropic/claude-sonnet-5" },
        models: { "anthropic/claude-sonnet-5": { agentRuntime: { id: "openclaw" } } },
      },
    },
  };
  assert.equal(lookup(config, "gateway.reload.mode"), "off");
  assert.equal(lookup(config, "gateway.reload.missing"), undefined);
  assert.equal(lookup(config, "toString"), undefined);
  assert.equal(primaryModel(config), "anthropic/claude-sonnet-5");
  assert.equal(primaryModel({ agents: { defaults: { model: "anthropic/x" } } }), "anthropic/x");
  assert.equal(runtimeOf(config), "openclaw");
  assert.equal(runtimeOf({}), undefined);
});

test("a heartbeat carries the hooks, the plugin's hash, the policy, the model and the runtime", () => {
  const api = {
    config: {
      tools: { profile: "minimal" },
      agents: { defaults: { model: { primary: "anthropic/m" }, models: { "anthropic/m": { agentRuntime: { id: "openclaw" } } } } },
    },
    logger: { warn() {} },
    runtime: { version: "2026.9.4" },
    on() {},
    registerService() {},
  };
  const fields = heartbeatFields(api, { socket: "/s", keyFile: "/k", policy: ["tools.profile", "tools.missing"] }, ["before_dispatch"], "f".repeat(64));
  assert.deepEqual(fields, {
    registrations: ["before_dispatch"],
    plugin_sha256: "f".repeat(64),
    policy: { "tools.profile": "minimal" },
    model: "anthropic/m",
    runtime: "openclaw",
    version: "2026.9.4",
  });
});

test("warnings repeat at most once a minute for each reason", () => {
  const lines: string[] = [];
  let now = 0;
  const warn = quietLogger({ warn: (line) => lines.push(line) }, () => now);
  warn("a");
  warn("a");
  warn("b");
  now = 60_000;
  warn("a");
  assert.deepEqual(lines, ["klepa-adapter: a", "klepa-adapter: b", "klepa-adapter: a"]);
});

test("register arms the hooks and a heartbeat service that beats at once", async () => {
  const { dir, path } = keyFile();
  const hooks: string[] = [];
  const services: { id: string; start(): void; stop(): void }[] = [];
  const core = fakeCore(() => ({ ok: true, problems: [] }));
  const source = join(dir, "index.ts");
  writeFileSync(source, "// the plugin\n");
  const api = {
    config: {},
    pluginConfig: { socket: "/s", keyFile: path, policy: [] },
    logger: { warn() {} },
    on(name: string) {
      hooks.push(name);
    },
    registerService(service: { id: string; start(): void; stop(): void }) {
      services.push(service);
    },
  };
  try {
    register(api, core.post, source);
    assert.deepEqual(hooks, [...HOOKS]);
    assert.equal(services.length, 1);
    services[0]?.start();
    await new Promise((resolve) => setImmediate(resolve));
    services[0]?.stop();
    const beat = core.seen[0];
    assert.equal(beat?.type, "heartbeat");
    assert.deepEqual(beat?.registrations, [...HOOKS]);
    assert.equal(beat?.plugin_sha256, createHash("sha256").update("// the plugin\n").digest("hex"));
  } finally {
    rmSync(dir, { recursive: true });
  }
});

test("a hook OpenClaw refuses to register is left out of the heartbeat", async () => {
  const { dir, path } = keyFile();
  const source = join(dir, "index.ts");
  writeFileSync(source, "x");
  const services: { start(): void; stop(): void }[] = [];
  const core = fakeCore(() => ({ ok: true }));
  const api = {
    config: {},
    pluginConfig: { socket: "/s", keyFile: path, policy: [] },
    logger: { warn() {} },
    on(name: string) {
      if (name === "before_agent_run") throw new Error("refused");
    },
    registerService(service: { start(): void; stop(): void }) {
      services.push(service);
    },
  };
  try {
    register(api, core.post, source);
    services[0]?.start();
    await new Promise((resolve) => setImmediate(resolve));
    services[0]?.stop();
    assert.deepEqual(core.seen[0]?.registrations, [
      "before_dispatch",
      "before_prompt_build",
      "before_tool_call",
      "reply_payload_sending",
    ]);
  } finally {
    rmSync(dir, { recursive: true });
  }
});

test("postUnix speaks HTTP over a Unix socket and returns Core's signed reply", async () => {
  const dir = mkdtempSync(join(tmpdir(), "klepa-sock-"));
  const socket = join(dir, "core.sock");
  let request = "";
  const server = createServer((conn) => {
    conn.on("data", (chunk) => {
      request += chunk.toString("latin1");
      if (request.includes("\r\n\r\n") && request.endsWith("{}")) {
        const body = '{"ok":true}';
        conn.end(`HTTP/1.1 200 OK\r\nContent-Length: ${body.length}\r\nX-Klepa-Signature: s1\r\n\r\n${body}`);
      }
    });
  });
  await new Promise<void>((resolve) => server.listen(socket, resolve));
  try {
    const reply: Reply = await postUnix(socket, Buffer.from("{}"), "sig");
    assert.equal(reply.status, 200);
    assert.equal(reply.body.toString(), '{"ok":true}');
    assert.equal(reply.signature, "s1");
    assert.match(request, /^POST \/v1\/message HTTP\/1\.1\r\n/);
    assert.match(request, /\r\nx-klepa-signature: sig\r\n/);
    assert.match(request, /\r\nContent-Length: 2\r\n/);
  } finally {
    server.close();
    rmSync(dir, { recursive: true });
  }
});

test("postUnix gives up on a Core that does not answer, or answers too much", async () => {
  const dir = mkdtempSync(join(tmpdir(), "klepa-sock-"));
  const silent = join(dir, "silent.sock");
  const flood = join(dir, "flood.sock");
  const quiet = createServer(() => {});
  const loud = createServer((conn) => {
    conn.write("HTTP/1.1 200 OK\r\n\r\n");
    conn.end(Buffer.alloc(70 * 1024, 65));
  });
  await new Promise<void>((resolve) => quiet.listen(silent, resolve));
  await new Promise<void>((resolve) => loud.listen(flood, resolve));
  try {
    await assert.rejects(postUnix(silent, Buffer.from("{}"), "sig", 200), /did not answer/);
    await assert.rejects(postUnix(flood, Buffer.from("{}"), "sig"), /too large/);
    await assert.rejects(postUnix(join(dir, "missing.sock"), Buffer.from("{}"), "sig"), /ENOENT/);
  } finally {
    quiet.close();
    loud.close();
    rmSync(dir, { recursive: true });
  }
});


test("canonical JSON sorts keys at every level and writes no spaces", () => {
  assert.equal(
    canonical({ b: 1, a: [true, null, "x"], c: { z: "é", y: 'q"\\' } }),
    '{"a":[true,null,"x"],"b":1,"c":{"y":"q\\"\\\\","z":"é"}}',
  );
  assert.throws(() => canonical({ a: undefined }));
});

test("a tool call is signed for exactly its run, call, tool and parameters", () => {
  const base = toolSignature(KEY, "run-1", "call-1", "search", { query: "x" });
  const expected = createHmac("sha256", KEY).update(`${TOOL_DOMAIN}\nrun-1\ncall-1\nsearch\n{"query":"x"}`);
  assert.equal(base, expected.digest("hex"));
  for (const other of [
    toolSignature(KEY, "run-2", "call-1", "search", { query: "x" }),
    toolSignature(KEY, "run-1", "call-2", "search", { query: "x" }),
    toolSignature(KEY, "run-1", "call-1", "get", { query: "x" }),
    toolSignature(KEY, "run-1", "call-1", "search", { query: "y" }),
  ]) {
    assert.notEqual(other, base);
  }
});

test("before_tool_call signs Core's tools, replaces the model's _klepa and blocks everything else", () => {
  const core = fakeCore(() => ({}));
  const { link: one, cleanup } = link(core.post);
  try {
    const hooks = hookHandlers(one, () => {});
    const ctx = { runId: "run-1", toolCallId: "call-1" };
    const forged = { toolName: "klepa__search", params: { query: "x", _klepa: { sig: "forged" } } };
    const sig = toolSignature(KEY, "run-1", "call-1", "search", { query: "x" });
    assert.deepEqual(hooks.before_tool_call(forged, ctx), {
      params: { query: "x", _klepa: { run_id: "run-1", tool_call_id: "call-1", sig } },
    });
    assert.equal(hooks.before_tool_call({ toolName: "exec", params: {} }, ctx).block, true);
    assert.equal(hooks.before_tool_call({ toolName: "klepa__get", params: {} }, { toolCallId: "c2" }).block, true);
    const direct = { runId: "r", toolCallId: "http-1" }; // /tools/invoke: no turn behind it
    assert.equal(hooks.before_tool_call({ toolName: "klepa__get", params: {} }, direct).block, true);
    assert.equal(core.seen.length, 0); // signing asks Core nothing
  } finally {
    cleanup();
  }
});

test("before_prompt_build hands over Core's instruction and only its tool names", async () => {
  const core = fakeCore(() => ({ ok: true, instruction: "INSTRUCTION", tools_allow: ["klepa__search", 5] }));
  const { link: one, cleanup } = link(core.post);
  try {
    const hooks = hookHandlers(one, () => {});
    const ctx = { runId: "run-1", chatId: "1", senderId: "1", sessionKey: "agent:main:telegram:direct:1" };
    assert.deepEqual(await hooks.before_prompt_build({}, ctx), {
      appendSystemContext: "INSTRUCTION",
      toolsAllow: ["klepa__search"],
    });
  } finally {
    cleanup();
  }
});

test("a reply leaves as text: no media, Core's words for a failed turn, Core's text alone for a blocked one", async () => {
  const answers: Record<string, unknown>[] = [{ ok: true, instruction: "I", tools_allow: [], failure: "Sorry." }];
  const core = fakeCore((message) => (message.type === "turn_start" ? { outcome: "pass" } : (answers.shift() ?? { ok: true })));
  const { link: one, cleanup } = link(core.post);
  try {
    const hooks = hookHandlers(one, () => {});
    const ctx = { runId: "run-9", chatId: "1", senderId: "1", sessionKey: "agent:main:telegram:direct:1" };
    await hooks.before_prompt_build({}, ctx);
    assert.deepEqual(await hooks.before_agent_run({}, ctx), { outcome: "pass" });
    const media = { text: "hi", mediaUrl: "https://a.invalid/x.png", mediaUrls: ["https://a.invalid/y.png"] };
    assert.deepEqual(hooks.reply_payload_sending({ payload: media, runId: "run-9" }, {}), { payload: { text: "hi" } });
    const failed = { text: "⚠️ a/b request failed (authentication failed, HTTP 401).", isError: true };
    assert.deepEqual(hooks.reply_payload_sending({ payload: failed, runId: "run-9" }, {}), {
      payload: { text: "Sorry.", isError: true },
    });
    const blocked = { text: "Your message could not be sent: on hold (blocked by klepa-adapter)", isError: true };
    assert.deepEqual(hooks.reply_payload_sending({ payload: blocked, runId: "other" }, {}), {
      payload: { text: "on hold", isError: true },
    });
    assert.equal(hooks.reply_payload_sending({ payload: { text: "fine" }, runId: "run-9" }, {}), undefined);
    await new Promise((resolve) => setImmediate(resolve));
    const reports = core.seen.filter((message) => message.type === "turn_reply");
    assert.deepEqual(
      reports.map((message) => [message.run_id, message.error, message.error_kind]),
      [
        ["run-9", false, undefined],
        ["run-9", true, "auth"],
        ["run-9", false, undefined],
      ],
    );
  } finally {
    cleanup();
  }
});

test("a failure is named by its kind, never by its text", () => {
  assert.equal(failureKind("⚠️ a/b request failed (authentication failed, HTTP 401). Re-authenticate"), "auth");
  assert.equal(failureKind("HTTP 429: rate limit reached"), "rate_limit");
  assert.equal(failureKind("the request timed out"), "timeout");
  assert.equal(failureKind("HTTP 503 from the provider"), "provider");
  assert.equal(failureKind(undefined), "other");
});
