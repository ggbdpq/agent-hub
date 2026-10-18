// WP7 真实路径契约测试：ChatOpenAI → 本地假 OpenAI SSE → langchain → translate → AgentStreamEvent。
// 覆盖空 choices、reasoning_content、tool_calls 分片、HTTP 500、中途断流、长开场白（05 U1）。
// 全程无外网：OPENAI_BASE_URL 指向本地假上游。
import assert from "node:assert/strict";
import { after, test } from "node:test";
import { AgentStreamEvent } from "@agent-hub/shared";
import { buildAgent, runTurn } from "../src/agent.ts";
import { createChatModel } from "../src/llm.ts";
import { assertSequence } from "../src/sequence.ts";
import { startServer } from "../src/server.ts";
import { CATALOG } from "../src/data.ts";
import { startFakeOpenAI, type FakeMode } from "./fake-openai.ts";

function fakeModelUrl(mode: FakeMode) {
  return startFakeOpenAI(mode);
}

/** ChatOpenAI 指向假上游的模型实例（maxRetries 0 让 500 立刻抛出，缩短测试时长） */
function realModel(url: string) {
  return createChatModel({ OPENAI_API_KEY: "test", OPENAI_BASE_URL: url, OPENAI_PROVIDER: "openai", maxRetries: undefined } as never);
}

async function collect(agent: ReturnType<typeof buildAgent>, message: string, threadId: string) {
  const evs: AgentStreamEvent[] = [];
  for await (const e of runTurn(agent, { message, threadId })) evs.push(AgentStreamEvent.parse(e));
  return evs;
}

const textOf = (evs: AgentStreamEvent[]) =>
  evs.filter((e): e is Extract<AgentStreamEvent, { type: "token" }> => e.type === "token").map((e) => e.text).join("");

test("真实路径：reasoning → thought，content → token，空 choices 帧容忍，done 收口", async () => {
  const fake = await fakeModelUrl("reasoning");
  after(() => fake.close());
  const evs = await collect(buildAgent({ model: realModel(fake.url) }), "日常自用来点花", "rp-reasoning");
  assertSequence(evs);
  assert.ok(evs.some((e) => e.type === "thought" && e.text.includes("先分析送礼场景")), "reasoning_content 进 thought");
  assert.equal(textOf(evs), "推荐礼盒，心意到位。");
  assert.equal(evs.at(-1)?.type, "done");
});

test("真实路径：完整工具循环（tool_calls 分片）→ card + evidence + token，商品来自目录", async () => {
  const fake = await fakeModelUrl("agent-loop");
  after(() => fake.close());
  const evs = await collect(buildAgent({ model: realModel(fake.url) }), "想给妈妈送生日礼物", "rp-loop");
  assertSequence(evs);

  const card = evs.find((e): e is Extract<AgentStreamEvent, { type: "card" }> => e.type === "card");
  assert.ok(card, "应有卡片");
  for (const item of card.payload.items) {
    const source = CATALOG.find((s) => s.id === item.id);
    assert.ok(source, `商品 ${item.id} 必须来自 CATALOG`);
    assert.equal(item.priceFen, source.priceFen);
  }
  const evidenceIds = evs
    .filter((e): e is Extract<AgentStreamEvent, { type: "evidence" }> => e.type === "evidence")
    .map((e) => e.evidence.id);
  assert.deepEqual(evidenceIds, ["ev-survey-02"]);
  assert.equal(textOf(evs), "送礼场景我推荐礼盒款：香槟色系符合你浅色系的偏好，永生花款适合希望纪念意义更久的场合。");

  // 循环按 检索→目录→展示→作答 推进：模型被调用 4 次，且每次工具结果都回了给上游
  assert.equal(fake.requests.length, 4, `应推进 4 次模型调用，实际 ${fake.requests.length}`);
  const lastMessages = fake.requests.at(-1)!.messages;
  const toolContents = lastMessages.filter((m) => m.role === "tool").map((m) => String(m.content));
  assert.ok(toolContents.some((c) => c.includes("[ev-survey-02]")), "上游能看到检索结果");
  assert.ok(toolContents.some((c) => c.includes("[fl-101]")), "上游能看到目录结果");
  // 工具调用名固定 thought 文案不透传参数
  assert.ok(evs.some((e) => e.type === "thought" && e.text.includes("整理推荐方案")));
});

test("真实路径：HTTP 500 → error{server}，message 不含上游原始报错（sk-test）", async () => {
  const fake = await fakeModelUrl("http500");
  after(() => fake.close());
  const evs = await collect(buildAgent({ model: realModel(fake.url) }), "送礼", "rp-500");
  assert.deepEqual(evs.map((e) => e.type), ["error"]);
  const err = evs[0] as Extract<AgentStreamEvent, { type: "error" }>;
  assert.equal(err.code, "server");
  assert.equal(err.message, "模型服务暂不可用（500）");
  assert.ok(!JSON.stringify(evs).includes("sk-test"), "上游报错文本不外泄");
});

test("真实路径：上游中途断流 → 单个 error，没有 done", async () => {
  const fake = await fakeModelUrl("mid-stream-drop");
  after(() => fake.close());
  const evs = await collect(buildAgent({ model: realModel(fake.url) }), "送礼", "rp-drop");
  assert.equal(evs.at(-1)?.type, "error");
  assert.equal(evs.filter((e) => e.type === "done").length, 0, "断流不产生 done");
  assert.equal(evs.filter((e) => e.type === "error").length, 1, "只收口一个 error");
});

test("真实路径：长开场白 + 工具调用（05 U1）——现行策略与已证伪的回退方案", async () => {
  const fake = await fakeModelUrl("long-preamble");
  after(() => fake.close());
  const evs = await collect(buildAgent({ model: realModel(fake.url) }), "想给妈妈送生日礼物", "rp-preamble");
  assertSequence(evs);
  const tokens = textOf(evs);
  const preamble = "好的，我先看一下顾客的画像和在售商品，再给您整理一份合适的送礼推荐方案。";
  // 现行策略（02 §2 映射表）：超过 24 字且尚未出现工具调用 → 开场白溢出为 token。
  // 05 U1 的假设"24 字够用"在此被证伪：长开场白后的工具调用提示不再进思考区——因为 S3 要求
  // 首个 token 前必须已 final thought，事后补 thought 会违反序列约束。
  assert.ok(tokens.includes(preamble), "超限开场白按现行策略整体溢出为 token");
  assert.equal(evs.at(-1)?.type, "done");
  assert.ok(evs.some((e) => e.type === "card"), "工具循环照常完成");
  // 文档回退"扣到本次调用结束"经实测会让单次回答完全没有 token 流式（不只是首字延迟），
  // 与 05 U1 描述不符，故不采用；结论记录在最终报告。
});

test("SSE 端到端：startServer + 真 ChatOpenAI × 假上游，帧格式与收口", async () => {
  const fake = await fakeModelUrl("agent-loop");
  after(() => fake.close());
  const server = await startServer(0, { model: realModel(fake.url) });
  after(() => server.close());
  const { port } = server.address() as { port: number };

  const res = await fetch(`http://127.0.0.1:${port}/api/agent/chat`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ message: "想给妈妈送生日礼物", threadId: "rp-sse-0001" }),
  });
  assert.equal(res.status, 200);
  const raw = await res.text();
  const frames = raw.match(/^event: (\w+)\ndata: ([^\n]+)\n\n/gm) ?? [];
  assert.ok(frames.length > 5);
  const evs = frames.map((f) => AgentStreamEvent.parse(JSON.parse(f.match(/data: (.+)\n\n/)![1])));
  assertSequence(evs);
  assert.equal(evs.at(-1)?.type, "done");
  assert.ok(evs.some((e) => e.type === "card"), "端到端有卡片");
});
