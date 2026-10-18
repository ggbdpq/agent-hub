// WP7：本地假 OpenAI SSE 上游（node:http，无 mock 库）。ChatOpenAI → langchain → translate 全链路
// 在无外网、无 API key 的条件下可测。帧格式与 spike3 验证过的一致：
// data: {"id","object":"chat.completion.chunk","created","model","choices":[...]}
// 工具调用分片：首片带 id/name/type，arguments 跨片拼接；finish_reason 'tool_calls'；
// 空 choices 帧容忍；usage 帧 choices:[]；结尾 data: [DONE]。
import { createServer, type Server } from "node:http";

export type FakeMode =
  | "reasoning" // 单轮：reasoning_content + content + 空 choices 帧 + usage
  | "agent-loop" // 完整工具循环：检索 → 目录 → 展示 → 作答（按请求 messages 决定下一步）
  | "http500" // 非 200：{"error":{"message":"boom sk-test"}}
  | "mid-stream-drop" // 发到一半 socket.destroy()
  | "long-preamble"; // 长开场白 + 工具调用（05 U1 扣留策略检查）

export interface FakeOpenAI {
  server: Server;
  /** ChatOpenAI 的 baseURL，形如 http://127.0.0.1:<port>/v1 */
  url: string;
  /** 每次收到的请求体（messages 为 OpenAI 格式），供断言循环推进 */
  requests: Array<{ messages: Array<Record<string, unknown>>; raw: unknown }>;
  close(): void;
}

const MODEL = "fake-gpt";
const ANSWER = "送礼场景我推荐礼盒款：香槟色系符合你浅色系的偏好，永生花款适合希望纪念意义更久的场合。";
const PRESENT_ARGS = {
  scene: "gift",
  title: "送礼首选：轻奢礼盒",
  reason: "送礼场景优先包装质感与寓意，避开你问卷里不喜欢的浓香型。",
  skuIds: ["fl-101", "fl-102"],
  evidenceIds: ["ev-survey-02"],
};

// 每次补全用唯一 id（真实 OpenAI/deepseek 行为）：翻译层按 msg.id 判定"是否新的模型调用"，
// 若所有补全共用一个 id，多轮工具循环会被误判为同一次调用（WP7 实测踩到：第 4 次正文被扣留吞掉）。
const chunk = (payload: unknown) =>
  `data: ${JSON.stringify({ id: `cmpl-${completionId}`, object: "chat.completion.chunk", created: 1, model: MODEL, ...(payload as object) })}\n\n`;
let completionId = 0;

/** 内容帧：首帧带 role（真实厂商行为；缺 role 时 ChatOpenAI 聚合为 generic 而非 AIMessage，WP7 实测） */
function contentFrames(text: string, slice = 6): unknown[] {
  const frames: unknown[] = [];
  for (let i = 0; i < text.length; i += slice) {
    frames.push({
      choices: [
        {
          index: 0,
          delta: i === 0 ? { role: "assistant", content: text.slice(0, slice) } : { content: text.slice(i, i + slice) },
          finish_reason: null,
        },
      ],
    });
  }
  return frames;
}

/** 工具调用分 3 片：首片带 id/name/type 与 arguments 首段，后两片只带 arguments 增量。
 * 真实厂商（OpenAI/deepseek）的 assistant 首个 delta 总是带 role，流直接以 tool_calls 开头时
 * ChatOpenAI 的 SSE 解析不产出增量（WP7 实测）——因此首片补 role。 */
function toolCallFrames(name: string, args: unknown): unknown[] {
  const raw = JSON.stringify(args);
  const third = Math.ceil(raw.length / 3);
  const pieces = [raw.slice(0, third), raw.slice(third, 2 * third), raw.slice(2 * third)];
  return [
    {
      choices: [
        {
          index: 0,
          delta: {
            role: "assistant",
            tool_calls: [{ index: 0, id: `call_${name}`, type: "function", function: { name, arguments: pieces[0] } }],
          },
          finish_reason: null,
        },
      ],
    },
    ...pieces.slice(1).map((p) => ({
      choices: [{ index: 0, delta: { tool_calls: [{ index: 0, function: { arguments: p } }] }, finish_reason: null }],
    })),
    { choices: [{ index: 0, delta: {}, finish_reason: "tool_calls" }] },
  ];
}

const reasoningFrame = (text: string) => ({
  choices: [{ index: 0, delta: { role: "assistant", reasoning_content: text }, finish_reason: null }],
});

/** 最后一条 tool 消息的工具名（OpenAI 格式 messages） */
export function lastToolName(messages: Array<Record<string, unknown>>): string | undefined {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m?.role === "tool") return (m.name as string | undefined) ?? ((m.content as string | undefined)?.startsWith("[") ? "search_evidence" : undefined);
  }
  return undefined;
}

/** 按"本轮已完成的工具"推进（与离线剧本同构的循环） */
export function agentLoopFrames(messages: Array<Record<string, unknown>>): unknown[] {
  const done = lastToolName(messages);
  if (!done) return [reasoningFrame("解析请求：这是送礼场景。"), ...toolCallFrames("search_evidence", {})];
  if (done === "search_evidence") return toolCallFrames("search_catalog", { scene: "gift" });
  if (done === "search_catalog") return toolCallFrames("present_recommendation", PRESENT_ARGS);
  return [
    reasoningFrame("卡片已展示，给顾客说明理由。"),
    ...contentFrames(ANSWER),
    { choices: [{ index: 0, delta: {}, finish_reason: "stop" }] },
    { choices: [], usage: { prompt_tokens: 120, completion_tokens: 40, total_tokens: 160 } },
  ];
}

export function startFakeOpenAI(mode: FakeMode): Promise<FakeOpenAI> {
  const requests: FakeOpenAI["requests"] = [];
  const server = createServer((req, res) => {
    if (req.method !== "POST" || !req.url?.endsWith("/chat/completions")) {
      res.writeHead(404).end();
      return;
    }
    void (async () => {
      let body: { messages?: Array<Record<string, unknown>> } = {};
      const chunks: Buffer[] = [];
      for await (const c of req) chunks.push(c as Buffer);
      try {
        body = JSON.parse(Buffer.concat(chunks).toString("utf-8"));
      } catch {
        body = {};
      }
      const messages = body.messages ?? [];
      requests.push({ messages, raw: body });
      completionId += 1; // 每次补全一个新 id（同一次补全内的所有帧共享）

      if (mode === "http500") {
        res.writeHead(500, { "content-type": "application/json" });
        res.end(JSON.stringify({ error: { message: "boom sk-test", type: "server_error" } }));
        return;
      }

      res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" });
      const write = (frame: unknown) => res.write(chunk(frame));

      if (mode === "reasoning") {
        // 首帧空 choices（usage 帧）在流首也容忍（spike3 验证）
        write({ choices: [] });
        write(reasoningFrame("先分析送礼场景"));
        for (const f of contentFrames("推荐礼盒，心意到位。")) write(f);
        write({ choices: [{ index: 0, delta: {}, finish_reason: "stop" }] });
        write({ choices: [], usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 } });
        res.end("data: [DONE]\n\n");
        return;
      }

      if (mode === "long-preamble") {
        // 首轮：30 字开场白 + 同一补全内的工具调用，检验扣留策略（01 §4.3 / 05 U1）；
        // 后续轮按 agent-loop 推进，否则循环永不结束会被限额打断
        if (!lastToolName(messages)) {
          const preamble = "好的，我先看一下顾客的画像和在售商品，再给您整理一份合适的送礼推荐方案。";
          for (const f of contentFrames(preamble, 5)) write(f);
          for (const f of toolCallFrames("search_evidence", {})) write(f);
        } else {
          for (const f of agentLoopFrames(messages)) write(f);
        }
        res.end("data: [DONE]\n\n");
        return;
      }

      if (mode === "mid-stream-drop") {
        write(reasoningFrame("开始分析"));
        write({ choices: [{ index: 0, delta: { content: "我查一下" }, finish_reason: null }] });
        // 不发 [DONE]、不发 finish_reason：直接断开连接
        res.socket?.destroy();
        return;
      }

      // agent-loop
      for (const f of agentLoopFrames(messages)) write(f);
      res.end("data: [DONE]\n\n");
    })();
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address() as { port: number };
      resolve({
        server,
        url: `http://127.0.0.1:${port}/v1`,
        requests,
        close: () => server.close(),
      });
    });
  });
}
