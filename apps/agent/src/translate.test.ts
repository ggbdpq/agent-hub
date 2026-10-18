// WP2 翻译层测试（04 §2）：映射表每一行、开场白扣留、错误码。
// 输入是手工构造的 [mode, chunk] 序列（async generator），不依赖工具与模型实现。
import assert from "node:assert/strict";
import { test } from "node:test";
import { AIMessageChunk, ToolMessage } from "@langchain/core/messages";
import { AgentStreamEvent, CardEvent, EvidenceEvent } from "@agent-hub/shared";
import { PREAMBLE, translate, toError } from "./translate.ts";
import { assertSequence } from "./sequence.ts";

type MsgChunk = [AIMessageChunk | ToolMessage, { langgraph_node?: string }];
type Chunk = ["messages", MsgChunk] | ["custom", unknown] | [string, unknown];

const collect = async (items: Chunk[], signal?: AbortSignal) => {
  async function* stream(): AsyncGenerator<Chunk> {
    yield* items;
  }
  const evs: AgentStreamEvent[] = [];
  for await (const e of translate(stream(), signal)) evs.push(AgentStreamEvent.parse(e));
  return evs;
};

const ai = (
  opts: {
    id?: string;
    content?: string;
    reasoning?: string;
    toolCall?: string;
    node?: string;
  } = {},
): MsgChunk => [
  new AIMessageChunk({
    id: opts.id ?? "run-1",
    content: opts.content ?? "",
    ...(opts.reasoning ? { additional_kwargs: { reasoning_content: opts.reasoning } } : {}),
    ...(opts.toolCall
      ? { tool_call_chunks: [{ type: "tool_call_chunk" as const, name: opts.toolCall, args: "", id: "call_1", index: 0 }] }
      : {}),
  }),
  { langgraph_node: opts.node ?? "model_request" },
];

const errToolMsg = (): MsgChunk => [
  new ToolMessage({ status: "error", content: "证据 ev-fake 不在本轮检索结果中", tool_call_id: "call_1" }),
  { langgraph_node: "tools" },
];

test("S1–S5：正常三场景流通过序列断言（公共函数冒烟）", async () => {
  const evs = await collect([
    ["messages", ai({ id: "r1", reasoning: "解析请求：送礼场景。" })],
    ["messages", ai({ id: "r1", toolCall: "search_evidence" })],
    ["custom", { type: "card", payload: giftCard() }],
    ["custom", { type: "evidence", evidence: giftCardEvidence() }],
    ["messages", ai({ id: "r2", content: "送礼场景我推荐礼盒款，心意到位。" })],
  ]);
  assertSequence(evs);
});

test("reasoning 两段增量 → 同一 seq 追加；新的模型调用 → 新 seq", async () => {
  const evs = await collect([
    ["messages", ai({ id: "r1", reasoning: "第一段。" })],
    ["messages", ai({ id: "r1", reasoning: "第二段。" })],
    ["messages", ai({ id: "r2", reasoning: "新调用。" })],
  ]);
  const thoughts = evs.filter(
    (e): e is Extract<AgentStreamEvent, { type: "thought" }> => e.type === "thought" && !e.final,
  );
  assert.deepEqual(
    thoughts.map((e) => e.seq),
    [1, 1, 2],
  );
  assert.deepEqual(
    thoughts.map((e) => e.text),
    ["第一段。", "第二段。", "新调用。"],
  );
  assertSequence(evs);
});

test("开场白 + tool_call_chunk → 开场白进 thought，不出 token", async () => {
  const evs = await collect([
    ["messages", ai({ id: "r1", content: "我先查一下。" })],
    ["messages", ai({ id: "r1", toolCall: "search_evidence" })],
  ]);
  const thoughts = evs.filter((e) => e.type === "thought") as Extract<AgentStreamEvent, { type: "thought" }>[];
  assert.ok(thoughts.some((t) => t.text.includes("我先查一下")), "扣留文本进思考区");
  assert.ok(thoughts.some((t) => t.text.includes("检索顾客画像")), "工具调用固定文案");
  assert.equal(evs.filter((e) => e.type === "token").length, 0, "不出 token");
  assertSequence(evs);
});

test("30 字正文无工具调用 → 首个 token 前有 final thought，正文全文无损", async () => {
  const text = "根据你的订单记录，玫瑰类是最稳的选择；这次换成好打理的盆栽，花期更长。";
  assert.ok([...text].length > PREAMBLE, "用例前提：正文超过扣留长度");
  const evs = await collect([
    ["messages", ai({ id: "r1", reasoning: "结合偏好生成回答。" })],
    ["messages", ai({ id: "r1", content: text.slice(0, 10) })],
    ["messages", ai({ id: "r1", content: text.slice(10) })],
  ]);
  const firstTokenIndex = evs.findIndex((e) => e.type === "token");
  assert.ok(firstTokenIndex > 0);
  const finalBefore = evs.slice(0, firstTokenIndex).at(-1) as Extract<AgentStreamEvent, { type: "thought" }>;
  assert.equal(finalBefore.type, "thought");
  assert.equal(finalBefore.final, true);
  const joined = evs
    .filter((e) => e.type === "token")
    .map((e) => (e as { text: string }).text)
    .join("");
  assert.equal(joined, text);
  assertSequence(evs);
});

test("扣留边界：恰好 24 字不冲刷，第 25 字到达即全部冲出", async () => {
  const chars = "一二三四五六七八九十甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午".slice(0, PREAMBLE);
  assert.equal([...chars].length, PREAMBLE);
  // 24 字调用结束仍无工具调用 → 冲出为 token（等价旧单轮行为），全文无损
  const heldOnly = await collect([["messages", ai({ id: "r1", content: chars })]]);
  const heldText = heldOnly
    .filter((e) => e.type === "token")
    .map((e) => (e as { text: string }).text)
    .join("");
  assert.equal(heldText, chars);
  const flushed = await collect([
    ["messages", ai({ id: "r1", content: chars })],
    ["messages", ai({ id: "r1", content: "未" })],
  ]);
  const joined = flushed
    .filter((e) => e.type === "token")
    .map((e) => (e as { text: string }).text)
    .join("");
  assert.equal(joined, chars + "未");
  assertSequence(flushed);
});

test("已出现工具调用后同一调用的零碎文本丢弃；工具调用生成固定 thought 文案", async () => {
  const evs = await collect([
    ["messages", ai({ id: "r1", toolCall: "search_catalog" })],
    ["messages", ai({ id: "r1", content: "  " })],
  ]);
  const thoughts = evs.filter((e) => e.type === "thought") as Extract<AgentStreamEvent, { type: "thought" }>[];
  assert.equal(thoughts.filter((t) => t.text.includes("查询在售花材")).length, 1);
  assert.equal(evs.filter((e) => e.type === "token").length, 0);
  assertSequence(evs);
});

test("ToolMessage(status=error) → 「校验未通过」thought；success 不出站", async () => {
  const okToolMsg = (): MsgChunk => [
    new ToolMessage({ status: "success", content: "[ev-survey-02] 入会喜好问卷", tool_call_id: "call_1" }),
    { langgraph_node: "tools" },
  ];
  const evs = await collect([
    ["messages", okToolMsg()],
    ["messages", errToolMsg()],
  ]);
  const thoughts = evs.filter(
    (e): e is Extract<AgentStreamEvent, { type: "thought" }> => e.type === "thought" && !e.final,
  );
  assert.equal(thoughts.length, 1);
  assert.match(thoughts[0].text, /校验未通过/);
  assert.doesNotMatch(thoughts[0].text, /不在本轮检索结果中/, "错误原文只给模型，不出站");
  assertSequence(evs);
});

test("非 model_request 节点的 AIMessageChunk 不出站（摘要等内部调用）", async () => {
  const evs = await collect([
    ["messages", ai({ id: "r1", content: "内部摘要输出", node: "summarize" })],
  ]);
  assert.deepEqual(evs.map((e) => e.type), ["done"]);
  assertSequence(evs);
});

test("custom card 先补 final thought 再原样下发；首个 card 前扣留文本作 token 冲出", async () => {
  const longText = "这是一段超过二十四字扣留限制的正文开场白，用于验证扣留在 custom 卡片前被冲刷。";
  const evs = await collect([
    ["messages", ai({ id: "r1", content: longText })],
    ["custom", { type: "card", payload: giftCard() }],
    ["custom", { type: "evidence", evidence: giftCardEvidence() }],
  ]);
  assert.equal((evs.find((e) => e.type === "card") as { type: string }).type, "card");
  const firstToken = evs.findIndex((e) => e.type === "token");
  const cardIndex = evs.findIndex((e) => e.type === "card");
  assert.ok(firstToken < cardIndex, "扣留正文先于卡片冲出");
  assertSequence(evs);
});

test("custom 事件不合契约 → 最后一个事件是 error{protocol}，且没有 done", async () => {
  const evs = await collect([["custom", { type: "card", payload: { scene: "gift" } }]]);
  assert.equal(evs.at(-1)?.type, "error");
  assert.equal((evs.at(-1) as { code?: string }).code, "protocol");
  assert.equal(evs.filter((e) => e.type === "done").length, 0);
});

test("抛 {status:500} → error{server}，message 不含原始报错文本", async () => {
  async function* boom(): AsyncGenerator<Chunk> {
    yield ["messages", ai({ id: "r1", reasoning: "thinking" })];
    throw Object.assign(new Error("upstream exploded sk-test-key http://secret"), { status: 500 });
  }
  const evs: AgentStreamEvent[] = [];
  for await (const e of translate(boom())) evs.push(AgentStreamEvent.parse(e));
  const err = evs.at(-1) as { type: string; code: string; message: string };
  assert.equal(err.type, "error");
  assert.equal(err.code, "server");
  assert.equal(err.message, "模型服务暂不可用（500）");
  assert.ok(!JSON.stringify(evs).includes("sk-test-key"), "原始报错不外泄");
  assertSequence(evs);
});

test("流中途 AbortError / signal 已中止 → error{aborted}，无 done", async () => {
  async function* abortedMidway(): AsyncGenerator<Chunk> {
    yield ["messages", ai({ id: "r1", reasoning: "x" })];
    const e = new Error("The operation was aborted");
    e.name = "AbortError";
    throw e;
  }
  const evs: AgentStreamEvent[] = [];
  for await (const e of translate(abortedMidway())) evs.push(AgentStreamEvent.parse(e));
  assert.equal(evs.at(-1)?.type, "error");
  assert.equal((evs.at(-1) as { code?: string }).code, "aborted");
  assertSequence(evs);

  const ac = new AbortController();
  ac.abort();
  const abortErr = toError(new Error("x"), ac.signal);
  assert.equal(abortErr.type === "error" && abortErr.code, "aborted");
});

test("流正常结束补 final thought 与 done{replyId=12 位 hex}；replyId 不再是模型名", async () => {
  const evs = await collect([["messages", ai({ id: "r1", reasoning: "想一下" })]]);
  const last = evs.at(-1) as { type: string; replyId?: string };
  assert.equal(last.type, "done");
  assert.match(last.replyId ?? "", /^[0-9a-f]{12}$/);
  const finalThought = evs.at(-2) as Extract<AgentStreamEvent, { type: "thought" }>;
  assert.equal(finalThought.type, "thought");
  assert.equal(finalThought.final, true);
  assertSequence(evs);
});

function giftCard() {
  return CardEvent.parse({
    type: "card",
    payload: {
      scene: "gift",
      title: "送礼首选：轻奢礼盒",
      reason: "浅色低香，符合问卷偏好。",
      items: [{ id: "fl-101", name: "香槟乌梅礼盒", priceFen: 12800, typeLabel: "花盒", emoji: "🎁" }],
      evidenceRefs: [{ evidenceId: "ev-survey-02", label: "入会喜好问卷" }],
    },
  }).payload;
}

function giftCardEvidence() {
  return EvidenceEvent.parse({
    type: "evidence",
    evidence: {
      id: "ev-survey-02",
      source: "入会喜好问卷",
      sourceKind: "preference_survey",
      credibility: 72,
      quote: "问卷勾选'喜欢低香味、浅色系'。",
      analyzedAt: "2026-08-02 11:30",
    },
  }).evidence;
}
