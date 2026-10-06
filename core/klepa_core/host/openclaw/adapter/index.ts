/**
 * Klepa's adapter plugin for OpenClaw 2026.9.4 (spec 4.4–4.6): the gateway's link to Klepa Core.
 *
 * Every message to Core is one JSON object POSTed to /v1/message on Core's Unix socket, signed with HMAC-SHA256
 * over its exact bytes. Core's answer is signed the same way and echoes (boot_id, seq). A boot is one gateway
 * process, however many times OpenClaw registers the plugin in it.
 *
 * Stage 1:
 * - before_dispatch asks Core first. Core answers people itself and lets only its live probe through to a turn;
 * - before_prompt_build tells Core that a run built its prompt;
 * - before_agent_run asks Core whether the turn may reach the model; in stage 1 Core blocks every turn;
 * - a background service sends the heartbeat every 5 s: the hooks this plugin registered, the SHA-256 of this
 *   file, the configuration values Core checks, the model and the runtime.
 *
 * Without Core nothing goes through: before_dispatch then claims nothing, and before_agent_run, which OpenClaw
 * treats as a gate that fails closed, blocks the turn. The plugin never logs what people wrote.
 */

import { createHash, createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import { readFileSync } from "node:fs";
import { connect } from "node:net";
import { fileURLToPath } from "node:url";

export const PLUGIN_ID = "klepa-adapter";
export const HOOKS = [
  "before_dispatch",
  "before_prompt_build",
  "before_agent_run",
  "before_tool_call",
  "reply_payload_sending",
] as const;
export const TOOL_PREFIX = "klepa__"; // OpenClaw names an MCP tool <server>__<tool>
export const TOOL_DOMAIN = "klepa-tool-call-v1";
export const HEARTBEAT_MS = 5_000;
export const REQUEST_TIMEOUT_MS = 5_000;
export const MAX_ANSWER_BYTES = 64 * 1024;
export const SIGNATURE_HEADER = "x-klepa-signature";
const WARN_EVERY_MS = 60_000;
// OpenClaw shows a blocked turn as "Your message could not be sent: <message> (blocked by klepa-adapter)".
export const UNREACHABLE = "Klepa Core did not answer. Please try again in a few minutes.";

type HookName = (typeof HOOKS)[number];
type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
type Answer = Record<string, unknown>;

export interface AdapterConfig {
  socket: string; // Core's Unix socket
  keyFile: string; // the 0600 key both sides sign with
  policy: string[]; // dotted configuration keys whose values every heartbeat reports
}

export interface Reply {
  status: number;
  body: Buffer;
  signature: string;
}

export type Post = (socket: string, body: Buffer, signature: string) => Promise<Reply>;

interface Logger {
  warn(message: string): void;
}

interface Service {
  id: string;
  start(): void;
  stop(): void;
}

export interface PluginApi {
  config?: unknown;
  pluginConfig?: unknown;
  logger: Logger;
  runtime?: { version?: string };
  on(hook: string, handler: (event: never, ctx: never) => unknown): void;
  registerService(service: Service): void;
}

// The fields this plugin reads from a hook's event and context. OpenClaw gives ids as strings; some may be absent.
export interface HookFacts {
  senderId?: unknown;
  chatId?: unknown;
  messageId?: unknown;
  sessionKey?: unknown;
  runId?: unknown;
  toolCallId?: unknown;
  toolName?: unknown;
  params?: unknown;
  systemPrompt?: unknown;
  payload?: unknown;
}

export function sign(key: Buffer, body: Buffer): string {
  return createHmac("sha256", key).update(body).digest("hex");
}

export function signedBy(key: Buffer, body: Buffer, signature: string): boolean {
  const expected = Buffer.from(sign(key, body));
  const given = Buffer.from(signature);
  return given.length === expected.length && timingSafeEqual(given, expected);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** An id or key as Core expects it: a string, or nothing. */
function text(value: unknown): string | undefined {
  if (typeof value === "string" && value.length > 0) return value;
  if (typeof value === "number" && Number.isSafeInteger(value)) return String(value);
  return undefined;
}

export function readConfig(raw: unknown): AdapterConfig {
  if (!isRecord(raw)) throw new Error("klepa-adapter: the plugin config is missing");
  const { socket, keyFile, policy } = raw;
  if (typeof socket !== "string" || socket === "") throw new Error("klepa-adapter: config.socket is missing");
  if (typeof keyFile !== "string" || keyFile === "") throw new Error("klepa-adapter: config.keyFile is missing");
  if (!Array.isArray(policy) || !policy.every((key) => typeof key === "string" && key !== "")) {
    throw new Error("klepa-adapter: config.policy must list configuration keys");
  }
  return { socket, keyFile, policy: [...policy] };
}

/** Core's answer from the raw bytes of one HTTP/1.1 response that ends when Core closes the connection. */
export function parseReply(raw: Buffer): Reply {
  const end = raw.indexOf("\r\n\r\n");
  if (end < 0) throw new Error("Core's answer has no header");
  const lines = raw.subarray(0, end).toString("latin1").split("\r\n");
  const status = /^HTTP\/1\.[01] (\d{3})( |$)/.exec(lines[0] ?? "");
  if (status === null) throw new Error("Core's answer has no status line");
  const headers = new Map<string, string>();
  for (const line of lines.slice(1)) {
    const colon = line.indexOf(":");
    if (colon > 0) headers.set(line.slice(0, colon).trim().toLowerCase(), line.slice(colon + 1).trim());
  }
  if (headers.has("transfer-encoding")) throw new Error("Core's answer is chunked");
  const length = Number(headers.get("content-length"));
  let body = raw.subarray(end + 4);
  if (Number.isSafeInteger(length) && length >= 0) {
    if (body.length < length) throw new Error("Core's answer was cut short");
    body = body.subarray(0, length);
  }
  return { status: Number(status[1]), body: Buffer.from(body), signature: headers.get(SIGNATURE_HEADER) ?? "" };
}

/**
 * One POST over Core's Unix socket, on a raw socket of node:net. OpenClaw routes every node:http and fetch
 * request of its process through the egress proxy, even one that names a socket path; raw sockets are outside
 * that routing, and Core's socket must never be reached through a proxy.
 */
export function postUnix(
  socket: string,
  body: Buffer,
  signature: string,
  timeoutMs: number = REQUEST_TIMEOUT_MS,
): Promise<Reply> {
  return new Promise((resolve, reject) => {
    const conn = connect(socket);
    const chunks: Buffer[] = [];
    let size = 0;
    const timer = setTimeout(() => conn.destroy(new Error("Core did not answer in time")), timeoutMs);
    conn.on("connect", () => {
      const head =
        "POST /v1/message HTTP/1.1\r\nHost: klepa-core\r\nContent-Type: application/json\r\n" +
        `Content-Length: ${body.length}\r\n${SIGNATURE_HEADER}: ${signature}\r\nConnection: close\r\n\r\n`;
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
      if (hadError) return; // the error handler has rejected already
      try {
        resolve(parseReply(Buffer.concat(chunks)));
      } catch (error) {
        reject(error);
      }
    });
  });
}

/**
 * JSON as Core writes it canonically: object keys sorted, no spaces. For what Core's tools take, ASCII keys,
 * strings, integers, booleans and lists, this is RFC 8785, and Core's Python writes the same bytes.
 */
export function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (isRecord(value)) {
    const keys = Object.keys(value).sort();
    return `{${keys.map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
  }
  const json = JSON.stringify(value);
  if (json === undefined) throw new Error("a value JSON cannot hold");
  return json;
}

/** The signature of one call of one of Core's tools (spec 4.5): only these parameters, for this call of this turn. */
export function toolSignature(
  key: Buffer,
  runId: string,
  toolCallId: string,
  tool: string,
  params: Record<string, unknown>,
): string {
  const signed = [TOOL_DOMAIN, runId, toolCallId, tool, canonical(params)].join("\n");
  return createHmac("sha256", key).update(signed, "utf8").digest("hex");
}

/** One boot of the gateway: its id, and the number of the last message sent. */
export interface Boot {
  id: string;
  seq: number;
}

const BOOT = Symbol.for("klepa-adapter.boot");

export function newBoot(): Boot {
  return { id: randomBytes(12).toString("base64url"), seq: 0 };
}

/**
 * The boot of this process. OpenClaw registers the plugin more than once in a process: a "full" registration,
 * whose before_dispatch and service run, and a "discovery" one, whose turn hooks run. Every message of the process
 * must carry one boot and one sequence, so the boot lives on the process, and a new process is a new boot.
 */
export function processBoot(): Boot {
  const store = globalThis as unknown as Record<symbol, Boot | undefined>;
  store[BOOT] ??= newBoot();
  return store[BOOT];
}

export class CoreLink {
  private key: Buffer | undefined;
  private readonly config: AdapterConfig;
  private readonly post: Post;
  private readonly boot: Boot;

  constructor(config: AdapterConfig, post: Post = postUnix, boot: Boot = processBoot()) {
    this.config = config;
    this.post = post;
    this.boot = boot;
  }

  get bootId(): string {
    return this.boot.id;
  }

  private signingKey(): Buffer {
    if (this.key === undefined) {
      const key = readFileSync(this.config.keyFile); // read when first needed: Core makes it at its own start
      if (key.length < 32) throw new Error("the adapter key is shorter than 32 bytes");
      this.key = key;
    }
    return this.key;
  }

  /** The signature Core checks on a call of one of its tools. */
  signTool(runId: string, toolCallId: string, tool: string, params: Record<string, unknown>): string {
    return toolSignature(this.signingKey(), runId, toolCallId, tool, params);
  }

  /** Send one message and return Core's answer, or throw: unsigned, for another message, or refused. */
  async send(type: string, fields: Record<string, Json | undefined>): Promise<Answer> {
    const key = this.signingKey();
    this.boot.seq += 1;
    const seq = this.boot.seq;
    const body = Buffer.from(JSON.stringify({ ...fields, type, boot_id: this.bootId, seq }), "utf8");
    const reply = await this.post(this.config.socket, body, sign(key, body));
    if (!signedBy(key, reply.body, reply.signature)) throw new Error("Core's answer is not signed with the key");
    const answer: unknown = JSON.parse(reply.body.toString("utf8"));
    if (!isRecord(answer) || answer.boot_id !== this.bootId || answer.seq !== seq) {
      throw new Error("Core's answer belongs to another message");
    }
    if (reply.status !== 200) throw new Error(`Core refused the message (${reply.status})`);
    return answer;
  }
}

/** Warn at most once a minute per reason: a Core outage must not flood the gateway's log. */
export function quietLogger(logger: Logger, now: () => number = Date.now): (reason: string) => void {
  const last = new Map<string, number>();
  return (reason) => {
    const at = now();
    if (at - (last.get(reason) ?? -Infinity) >= WARN_EVERY_MS) {
      last.set(reason, at);
      logger.warn(`klepa-adapter: ${reason}`);
    }
  };
}

function why(error: unknown): string {
  return error instanceof Error ? error.message : "unknown error";
}

function turnFacts(event: HookFacts | undefined, ctx: HookFacts | undefined): Record<string, Json | undefined> {
  return {
    run_id: text(ctx?.runId ?? event?.runId),
    chat_id: text(ctx?.chatId ?? event?.chatId),
    sender_id: text(ctx?.senderId ?? event?.senderId),
    session_key: text(ctx?.sessionKey ?? event?.sessionKey),
  };
}

const FAILURE = Symbol.for("klepa-adapter.failure");

/** Core's text for a turn that failed, as Core last sent it: the hooks of both registrations see one. */
function failureStore(): Record<symbol, string | undefined> {
  return globalThis as unknown as Record<symbol, string | undefined>;
}

const PASSED = Symbol.for("klepa-adapter.passed");
const INSTRUCTED = Symbol.for("klepa-adapter.instructed");
const KEEP_RUNS = 1000;

function runSet(name: symbol): Set<string> {
  const store = globalThis as unknown as Record<symbol, Set<string> | undefined>;
  store[name] ??= new Set();
  return store[name];
}

/** The runs this process let through to the model: only their errors are the model's. */
function passedRuns(): Set<string> {
  return runSet(PASSED);
}

/** The runs whose prompt carries Core's instruction: only they may reach the model (spec 10). */
function instructedRuns(): Set<string> {
  return runSet(INSTRUCTED);
}

/** Remember a run, forgetting the oldest beyond `keep`: a busy gateway never forgets the runs it is still on. */
export function remember(runs: Set<string>, runId: string, keep = KEEP_RUNS): void {
  runs.delete(runId);
  runs.add(runId);
  for (const oldest of runs) {
    if (runs.size <= keep) break;
    runs.delete(oldest);
  }
}

// OpenClaw wraps a blocked turn's text: "Your message could not be sent: <text> (blocked by <plugin>)" (spike
// report, point 15). The person reads Core's text alone.
const BLOCK_WRAPPER = /^Your message could not be sent: ([\s\S]*?)(?: \(blocked by [\w.-]+\))?$/;

/** What kind of failure ended a turn, without its text: OpenClaw's error names the provider and the cause. */
export function failureKind(error: unknown): string {
  const text = typeof error === "string" ? error : "";
  if (/HTTP 401|HTTP 403|authentication/i.test(text)) return "auth";
  if (/HTTP 429|rate limit/i.test(text)) return "rate_limit";
  if (/timed? ?out/i.test(text)) return "timeout";
  if (/HTTP 5\d\d/.test(text)) return "provider";
  return "other";
}

const TOOLS_ONLY = "Only Klepa's own tools may run.";
const UNSIGNED = "Klepa could not sign this call.";

export function hookHandlers(link: CoreLink, warn: (reason: string) => void) {
  return {
    async before_dispatch(event: HookFacts | undefined, ctx: HookFacts | undefined) {
      try {
        const answer = await link.send("dispatch", {
          sender_id: text(event?.senderId ?? ctx?.senderId),
          message_id: text(event?.messageId ?? ctx?.messageId),
          session_key: text(event?.sessionKey ?? ctx?.sessionKey),
        });
        if (answer.handled !== true) return undefined;
        return typeof answer.text === "string" ? { handled: true, text: answer.text } : { handled: true };
      } catch (error) {
        warn(`dispatch: ${why(error)}`);
        return undefined; // claims nothing; before_agent_run blocks the turn
      }
    },
    async before_prompt_build(event: HookFacts | undefined, ctx: HookFacts | undefined) {
      try {
        const facts = turnFacts(event, ctx);
        const answer = await link.send("prompt_built", facts);
        if (typeof answer.instruction !== "string") return undefined;
        if (typeof answer.failure === "string") failureStore()[FAILURE] = answer.failure;
        const tools = Array.isArray(answer.tools_allow) ? answer.tools_allow.filter((t) => typeof t === "string") : [];
        if (typeof facts.run_id === "string") remember(instructedRuns(), facts.run_id);
        return { appendSystemContext: answer.instruction, toolsAllow: tools };
      } catch (error) {
        warn(`prompt_built: ${why(error)}`);
        return undefined; // without Core's instruction before_agent_run blocks the turn
      }
    },
    async before_agent_run(event: HookFacts | undefined, ctx: HookFacts | undefined) {
      try {
        const facts = turnFacts(event, ctx);
        // Core may have answered prompt_built after this plugin gave up waiting: then the prompt lacks Core's
        // instruction, and the run never reaches the model, whatever Core says of it.
        if (typeof facts.run_id !== "string" || !instructedRuns().has(facts.run_id)) {
          warn("turn_start: no instruction from Core");
          return { outcome: "block", reason: "klepa-unreachable", message: UNREACHABLE };
        }
        const answer = await link.send("turn_start", facts);
        if (answer.outcome === "pass") {
          remember(passedRuns(), facts.run_id);
          return { outcome: "pass" };
        }
        const message = typeof answer.message === "string" ? answer.message : UNREACHABLE;
        return { outcome: "block", reason: "klepa", message };
      } catch (error) {
        warn(`turn_start: ${why(error)}`);
        return { outcome: "block", reason: "klepa-unreachable", message: UNREACHABLE };
      }
    },
    // Each call of Core's tools carries its own signature (spec 4.5); a _klepa field the model wrote is replaced.
    before_tool_call(event: HookFacts | undefined, ctx: HookFacts | undefined) {
      const name = text(event?.toolName ?? ctx?.toolName) ?? "";
      if (!name.startsWith(TOOL_PREFIX)) return { block: true, blockReason: TOOLS_ONLY };
      const runId = text(ctx?.runId ?? event?.runId);
      const toolCallId = text(ctx?.toolCallId ?? event?.toolCallId);
      if (runId === undefined || toolCallId === undefined || toolCallId.startsWith("http-")) {
        return { block: true, blockReason: UNSIGNED }; // a direct call through /tools/invoke has no turn
      }
      const params: Record<string, unknown> = isRecord(event?.params) ? { ...event.params } : {};
      delete params._klepa;
      try {
        const sig = link.signTool(runId, toolCallId, name.slice(TOOL_PREFIX.length), params);
        return { params: { ...params, _klepa: { run_id: runId, tool_call_id: toolCallId, sig } } };
      } catch (error) {
        warn(`tool call: ${why(error)}`);
        return { block: true, blockReason: UNSIGNED };
      }
    },
    // A second layer behind the gatekeeper, which takes no files: a MEDIA line would make OpenClaw drop the whole
    // reply when the download fails (spike report, points 11 and 18), so the reply goes as text.
    reply_payload_sending(event: HookFacts | undefined, ctx: HookFacts | undefined) {
      const payload = event?.payload;
      if (!isRecord(payload)) return undefined;
      const runId = text(event?.runId ?? ctx?.runId);
      const failure = failureStore()[FAILURE];
      // OpenClaw sends its English error whatever errorPolicy says; for a turn that reached the model the person
      // reads Core's words instead. A turn this plugin blocked keeps Core's own text, without OpenClaw's wrapper.
      const passed = runId !== undefined && passedRuns().has(runId);
      if (passed) {
        // How the model answered, for Core: the gate's model probe passes on an answer, a person's failed turn alerts
        // the owner. OpenClaw's agent_end says success for a turn whose error it surfaced as the reply.
        const error = payload.isError === true;
        link.send("turn_reply", { run_id: runId, error, error_kind: error ? failureKind(payload.text) : undefined }).catch(
          (problem: unknown) => warn(`turn_reply: ${why(problem)}`),
        );
      }
      const failed = payload.isError === true && failure !== undefined && passed;
      const blocked = typeof payload.text === "string" ? BLOCK_WRAPPER.exec(payload.text) : null;
      const media = "mediaUrl" in payload || "mediaUrls" in payload;
      if (!failed && blocked === null && !media) return undefined;
      const { mediaUrl: _url, mediaUrls: _urls, ...rest } = payload;
      if (failed) return { payload: { ...rest, text: failure } };
      return { payload: blocked === null ? rest : { ...rest, text: blocked[1] } };
    },

  };
}

/** The value at a dotted key of the gateway's configuration, or undefined when any part is missing. */
export function lookup(config: unknown, key: string): Json | undefined {
  let value: unknown = config;
  for (const part of key.split(".")) {
    if (!isRecord(value) || !Object.hasOwn(value, part)) return undefined;
    value = value[part];
  }
  return value as Json;
}

export function primaryModel(config: unknown): string | undefined {
  const model = lookup(config, "agents.defaults.model");
  if (typeof model === "string") return model;
  return isRecord(model) && typeof model.primary === "string" ? model.primary : undefined;
}

export function runtimeOf(config: unknown): string | undefined {
  const model = primaryModel(config);
  const models = lookup(config, "agents.defaults.models");
  const entry = model !== undefined && isRecord(models) ? models[model] : undefined;
  const id = isRecord(entry) && isRecord(entry.agentRuntime) ? entry.agentRuntime.id : undefined;
  return typeof id === "string" ? id : undefined;
}

export function fileSha256(path: string): string {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

export function heartbeatFields(
  api: PluginApi,
  config: AdapterConfig,
  registrations: string[],
  sha256: string,
): Record<string, Json | undefined> {
  const policy: Record<string, Json> = {};
  for (const key of config.policy) {
    const value = lookup(api.config, key);
    if (value !== undefined) policy[key] = value;
  }
  return {
    registrations,
    plugin_sha256: sha256,
    policy,
    model: primaryModel(api.config),
    runtime: runtimeOf(api.config),
    version: text(api.runtime?.version),
  };
}

export function register(api: PluginApi, post: Post = postUnix, source: string = fileURLToPath(import.meta.url)) {
  const config = readConfig(api.pluginConfig);
  const link = new CoreLink(config, post);
  const warn = quietLogger(api.logger);
  const handlers = hookHandlers(link, warn);
  const registrations: string[] = [];
  for (const name of HOOKS) {
    try {
      api.on(name, handlers[name as HookName] as (event: never, ctx: never) => unknown);
      registrations.push(name);
    } catch (error) {
      warn(`${name} was not registered: ${why(error)}`);
    }
  }
  const sha256 = fileSha256(source);
  let timer: ReturnType<typeof setInterval> | undefined;
  const beat = () => {
    link.send("heartbeat", heartbeatFields(api, config, registrations, sha256)).then(
      (answer) => {
        const problems = Array.isArray(answer.problems) ? answer.problems.filter((p) => typeof p === "string") : [];
        if (problems.length > 0) warn(`Core rejects the heartbeat: ${problems.slice(0, 5).join(", ")}`);
      },
      (error: unknown) => warn(`heartbeat: ${why(error)}`),
    );
  };
  api.registerService({
    id: "klepa-heartbeat",
    start() {
      beat();
      timer = setInterval(beat, HEARTBEAT_MS);
      timer.unref();
    },
    stop() {
      clearInterval(timer);
      timer = undefined;
    },
  });
  return link;
}

export default {
  id: PLUGIN_ID,
  name: "Klepa adapter",
  description: "Links the gateway to Klepa Core: the turn gate, the heartbeat and the stage-1 replies.",
  register(api: PluginApi) {
    register(api);
  },
};
