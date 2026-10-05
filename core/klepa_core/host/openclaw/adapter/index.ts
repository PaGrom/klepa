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
export const HOOKS = ["before_dispatch", "before_prompt_build", "before_agent_run"] as const;
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
        await link.send("prompt_built", turnFacts(event, ctx));
      } catch (error) {
        warn(`prompt_built: ${why(error)}`);
      }
      return undefined; // stage 1 adds no instruction
    },
    async before_agent_run(event: HookFacts | undefined, ctx: HookFacts | undefined) {
      try {
        const answer = await link.send("turn_start", turnFacts(event, ctx));
        if (answer.outcome === "pass") return { outcome: "pass" };
        const message = typeof answer.message === "string" ? answer.message : UNREACHABLE;
        return { outcome: "block", reason: "klepa", message };
      } catch (error) {
        warn(`turn_start: ${why(error)}`);
        return { outcome: "block", reason: "klepa-unreachable", message: UNREACHABLE };
      }
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
