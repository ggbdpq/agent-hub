// WP4 HTTP 层测试（04 §2）：帧格式、422、证据接口、断连中止。
import assert from "node:assert/strict";
import { after, test } from "node:test";
import type { BaseChatModel } from "@langchain/core/language_models/chat_models";
import { ScriptedChatModel } from "./scripted-model.ts";
import { floraPolicy } from "./scripts.ts";
import { startServer } from "./server.ts";

async function postChat(port: number, body: unknown, signal?: AbortSignal): Promise<Response> {
  return fetch(`http://127.0.0.1:${port}/api/agent/chat`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: typeof body === "string" ? body : JSON.stringify(body),
    signal,
  });
}

test("SSE 每帧匹配 event/data 两行 + 空行，且 event 名 = data.type", async () => {
  const server = await startServer(0, { model: new ScriptedChatModel(floraPolicy) });
  after(() => server.close());
  const { port } = server.address() as { port: number };

  const res = await postChat(port, { message: "想给妈妈送生日礼物" });
  assert.equal(res.status, 200);
  assert.ok((res.headers.get("content-type") ?? "").startsWith("text/event-stream"));
  assert.equal(res.headers.get("cache-control"), "no-cache");
  assert.equal(res.headers.get("x-accel-buffering"), "no");

  const raw = await res.text();
  assert.ok(raw.endsWith("\n\n"));
  const frames = raw.match(/^event: (\w+)\ndata: ([^\n]+)\n\n/gm) ?? [];
  assert.ok(frames.length > 5, `应解析出多帧，raw=${raw.slice(0, 200)}`);
  for (const frame of frames) {
    assert.match(frame, /^event: (\w+)\ndata: [^\n]+\n\n$/);
    const event = frame.match(/^event: (\w+)/)![1];
    const data = JSON.parse(frame.match(/^event: \w+\ndata: (.+)\n\n$/)![1]);
    assert.equal(event, data.type);
  }
  const last = JSON.parse(frames.at(-1)!.match(/data: (.+)\n\n/)![1]);
  assert.equal(last.type, "done");
});

test("请求体不合法 → 422：空对象与超长 message", async () => {
  const server = await startServer(0, { model: new ScriptedChatModel(floraPolicy) });
  after(() => server.close());
  const { port } = server.address() as { port: number };

  for (const bad of [{}, { message: "" }, { message: "x".repeat(2001) }, { message: "ok", threadId: "short" }]) {
    const res = await postChat(port, bad);
    assert.equal(res.status, 422, `应拒绝 ${JSON.stringify(bad).slice(0, 40)}`);
    const detail = (await res.json()) as { detail: unknown };
    assert.ok(detail.detail, "422 应带 detail");
  }
});

test("GET /api/evidence/:id → 200 / 404（与 FastAPI 一致）", async () => {
  const server = await startServer(0, { model: new ScriptedChatModel(floraPolicy) });
  after(() => server.close());
  const { port } = server.address() as { port: number };

  const hit = await fetch(`http://127.0.0.1:${port}/api/evidence/ev-purchase-01`);
  assert.equal(hit.status, 200);
  const evidence = (await hit.json()) as { id: string; source: string };
  assert.equal(evidence.id, "ev-purchase-01");

  const miss = await fetch(`http://127.0.0.1:${port}/api/evidence/nope`);
  assert.equal(miss.status, 404);
  assert.deepEqual(await miss.json(), { detail: "evidence not found" });
});

test("客户端在第一个帧后断开 → 服务端模型调用停止（计数不再增长）", async () => {
  let calls = 0;
  const countingPolicy = (() => {
    const inner = floraPolicy;
    return ((messages: Parameters<typeof inner>[0]) => {
      calls += 1;
      return inner(messages);
    }) as typeof inner;
  })();
  const model: BaseChatModel = new ScriptedChatModel(countingPolicy, { delayMs: 40 });
  const server = await startServer(0, { model });
  after(() => server.close());
  const { port } = server.address() as { port: number };

  const ac = new AbortController();
  const res = await postChat(port, { message: "想给妈妈送生日礼物" }, ac.signal);
  const reader = res.body!.getReader();
  await reader.read(); // 读到第一个帧即断开
  const callsAtDisconnect = calls;
  await reader.cancel().catch(() => {});
  ac.abort();
  await new Promise((r) => setTimeout(r, 500)); // 若未中止，40ms×多次调用计数必然增长

  assert.ok(calls >= callsAtDisconnect);
  assert.equal(calls, callsAtDisconnect, "断开后模型调用应停止");
});
