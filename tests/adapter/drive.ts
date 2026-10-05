// Drives the real adapter plugin against a running Core for tests/test_adapter_plugin.py, the way OpenClaw would:
// register, start the heartbeat service, then call the hooks of a member's message and of the live probe.
// Usage: node drive.ts <socket> <key file> <member id> <probe peer>. Prints one JSON line per step.
import plugin from "../../core/klepa_core/host/openclaw/adapter/index.ts";

const [socket, keyFile, member, probe] = process.argv.slice(2);
type Handler = (event: object, ctx: object) => Promise<unknown>;
const hooks = new Map<string, Handler>();
const services: { start(): void; stop(): void }[] = [];
const api = {
  config: {
    tools: { profile: "minimal" },
    agents: {
      defaults: {
        model: { primary: "anthropic/claude-sonnet-5" },
        models: { "anthropic/claude-sonnet-5": { agentRuntime: { id: "openclaw" } } },
      },
    },
  },
  pluginConfig: { socket, keyFile, policy: ["tools.profile"] },
  logger: { warn: (line: string) => console.error(line) },
  runtime: { version: "2026.9.4" },
  on: (name: string, handler: (event: never, ctx: never) => unknown) => {
    hooks.set(name, handler as unknown as Handler);
  },
  registerService: (service: { start(): void; stop(): void }) => services.push(service),
};

const say = (step: string, result: unknown) => console.log(JSON.stringify({ step, result: result ?? null }));
const hook = (name: string) => {
  const handler = hooks.get(name);
  if (handler === undefined) throw new Error(`${name} is not registered`);
  return handler;
};

plugin.register(api);
say("registered", [...hooks.keys()]);
services[0]?.start();
await new Promise((resolve) => setTimeout(resolve, 300));
const session = (peer: string | undefined) => `agent:main:telegram:direct:${peer}`;
say("member_dispatch", await hook("before_dispatch")({ senderId: member, messageId: "10", sessionKey: session(member) }, {}));
say("probe_dispatch", await hook("before_dispatch")({ senderId: probe, messageId: "1", sessionKey: session(probe) }, {}));
const ctx = { runId: "run-probe", chatId: probe, senderId: probe, sessionKey: session(probe) };
say("probe_prompt_built", await hook("before_prompt_build")({}, ctx));
say("probe_turn", await hook("before_agent_run")({}, ctx));
services[0]?.stop();
