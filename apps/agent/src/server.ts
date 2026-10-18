// HTTP 层（apps/agent）：node:http，无框架。端口 8809。
// 路由：POST /api/agent/chat（SSE）、GET /api/evidence/:id、GET /healthz。
// 帧格式与旧 FastAPI 完全一致：`event: <type>\ndata: <单行 JSON>\n\n`。
// 没有 OPENAI_API_KEY 时用离线剧本 ScriptedChatModel（与真实模型走同一张图，只换 model）。
import { createServer, type Server } from "node:http";
import { randomUUID } from "node:crypto";
import { resolve } from "node:path";
import type { BaseChatModel } from "@langchain/core/language_models/chat_models";
import { MemorySaver } from "@langchain/langgraph";
import { ChatRequest } from "@agent-hub/shared";
import type { FloraAgent } from "./agent.ts";
import { buildAgent, runTurn } from "./agent.ts";
import { EVIDENCE } from "./data.ts";
import { ScriptedChatModel } from "./scripted-model.ts";
import { floraPolicy } from "./scripts.ts";
import type { AgentStreamEvent } from "@agent-hub/shared";

export const frame = (e: AgentStreamEvent) => `event: ${e.type}\ndata: ${JSON.stringify(e)}\n\n`; // JSON.stringify 不产生换行 → 天然单行

async function pickModel(): Promise<BaseChatModel> {
  if (!process.env.OPENAI_API_KEY) return new ScriptedChatModel(floraPolicy, { delayMs: 40 });
  const { createChatModel } = await import("./llm.ts"); // 真实模型：provider 表 → ChatOpenAI
  return createChatModel();
}

/** AGENT_SUMMARY_MODEL 配置时用独立模型做长对话摘要（WP5）；只在真实模型模式下启用 */
async function pickSummaryModel(): Promise<BaseChatModel | undefined> {
  if (!process.env.OPENAI_API_KEY || !process.env.AGENT_SUMMARY_MODEL) return undefined;
  const { createChatModel } = await import("./llm.ts");
  return createChatModel({ ...process.env, OPENAI_MODEL: process.env.AGENT_SUMMARY_MODEL });
}

// .env 与 FastAPI 共用：先读 cwd，再兜底仓库根与 apps/api（Node 的 loadEnvFile 不覆盖已有变量）
function loadEnv(): void {
  const here = import.meta.dirname;
  for (const p of [resolve(process.cwd(), ".env"), resolve(here, "../../.env"), resolve(here, "../../apps/api/.env")]) {
    try {
      process.loadEnvFile(p);
    } catch {
      /* 文件不存在就跳过 */
    }
  }
}

export async function startServer(
  port = Number(process.env.AGENT_PORT ?? 8809),
  opts: { model?: BaseChatModel } = {},
): Promise<Server> {
  const model = opts.model ?? (await pickModel());
  // checkpointer 由 server 持有引用，超限的 thread 才能被逐出（MemorySaver 进程内存储，ADR-0005）
  const checkpointer = new MemorySaver();
  const summaryModel = await pickSummaryModel();
  const agent: FloraAgent = buildAgent({ model, summaryModel, checkpointer });
  // 最近使用的 thread（插入序 = LRU）：超过 AGENT_MAX_THREADS 时逐出最旧，防内存随 thread 数无限增长
  const activeThreads = new Map<string, true>();
  const maxThreads = Number(process.env.AGENT_MAX_THREADS ?? 1000);

  const rememberThread = (threadId: string) => {
    activeThreads.delete(threadId);
    activeThreads.set(threadId, true);
    while (activeThreads.size > maxThreads) {
      const oldest = activeThreads.keys().next().value as string;
      activeThreads.delete(oldest);
      void checkpointer.deleteThread(oldest).catch(() => {});
    }
  };

  const server = createServer(async (req, res) => {
    const url = new URL(req.url ?? "/", "http://x");
    if (req.method === "GET" && url.pathname === "/healthz") {
      res.end("ok");
      return;
    }
    const ev = url.pathname.match(/^\/api\/evidence\/([\w-]+)$/);
    if (req.method === "GET" && ev) {
      const hit = EVIDENCE[ev[1]];
      res.writeHead(hit ? 200 : 404, { "content-type": "application/json; charset=utf-8" });
      res.end(JSON.stringify(hit ?? { detail: "evidence not found" }));
      return;
    }
    if (req.method === "POST" && url.pathname === "/api/agent/chat") {
      let body: unknown;
      try {
        body = JSON.parse(await new Response(req as never).text());
      } catch {
        body = undefined;
      }
      const parsed = ChatRequest.safeParse(body);
      if (!parsed.success) {
        res.writeHead(422, { "content-type": "application/json" });
        res.end(JSON.stringify({ detail: parsed.error.issues }));
        return;
      }
      res.writeHead(200, {
        "content-type": "text/event-stream; charset=utf-8",
        "cache-control": "no-cache",
        "x-accel-buffering": "no",
      });
      const ac = new AbortController();
      res.on("close", () => {
        if (!res.writableFinished) ac.abort(); // 浏览器断开 → 中止模型调用（不再计费）
      });
      const threadId = parsed.data.threadId ?? randomUUID(); // 不带 threadId = 单轮（与旧行为一致）
      rememberThread(threadId);
      for await (const e of runTurn(agent, { message: parsed.data.message, threadId }, ac.signal)) {
        if (ac.signal.aborted) break;
        res.write(frame(e));
      }
      res.end();
      return;
    }
    res.writeHead(404).end();
  });
  await new Promise<void>((r) => server.listen(port, "127.0.0.1", r));
  return server;
}

if (import.meta.main) {
  loadEnv();
  const s = await startServer();
  console.log(`agent listening on http://127.0.0.1:${(s.address() as { port: number }).port}`);
}
