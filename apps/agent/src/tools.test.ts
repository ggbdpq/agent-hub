// WP1 工具与数据测试（04 §2）：因果校验、价格只来自目录、custom 事件顺序、同轮一次展示。
// 纯函数部分直接构造 messages；集成部分用 createAgent + fakeModel（FIFO 响应）。
import assert from "node:assert/strict";
import { test } from "node:test";
import { createAgent, fakeModel } from "langchain";
import { AIMessage, HumanMessage, ToolMessage, type BaseMessage } from "@langchain/core/messages";
import { AgentStreamEvent, CardEvent, EvidenceEvent, FlowerCardPayload } from "@agent-hub/shared";
import { CATALOG, EVIDENCE } from "./data.ts";
import {
  presentCard,
  presentRecommendation,
  queryCatalog,
  queryEvidence,
  searchCatalog,
  searchEvidence,
} from "./tools.ts";

const human = (text: string) => new HumanMessage(text);

/** 模拟图内 ToolNode 的产物：带 name/status/artifact 的 ToolMessage */
function toolMsg(
  name: string,
  status: "success" | "error",
  artifact: unknown[] = [],
  content = "占位",
): ToolMessage {
  return new ToolMessage({ name, content, artifact, status, tool_call_id: `call_${name}` });
}

const GIFT_PRESENT = {
  scene: "gift" as "daily" | "gift" | "comfort",
  title: "送礼首选：轻奢礼盒",
  reason: "送礼场景优先包装质感与寓意。",
  skuIds: ["fl-101", "fl-102"],
  evidenceIds: ["ev-survey-02"],
};

/** 构造"本轮已检索"的消息序列（最后一条 HumanMessage 之后有两条成功检索） */
function searchedThisTurn(): BaseMessage[] {
  return [
    human("想给妈妈送生日礼物"),
    toolMsg("search_evidence", "success", [EVIDENCE["ev-survey-02"]]),
    toolMsg("search_catalog", "success", queryCatalog({ scene: "gift" })),
  ];
}

test("本轮未检索就展示 → 抛错，提示先调用 search_evidence", () => {
  assert.throws(
    () => presentCard([human("送礼")], { ...GIFT_PRESENT }, null),
    (e: Error) => e.message.includes("请先调用 search_evidence"),
  );
});

test("引用上一轮（上一条 HumanMessage 之前）检索到的证据 → 抛错", () => {
  // 上一轮检索过 ev-survey-02；本轮用户改口后未重新检索就引用
  const messages: BaseMessage[] = [
    human("想给妈妈送生日礼物"),
    toolMsg("search_evidence", "success", [EVIDENCE["ev-survey-02"]]),
    new AIMessage({ content: "", tool_calls: [] }),
    toolMsg("present_recommendation", "success"),
    human("换点别的"),
  ];
  assert.throws(
    () => presentCard(messages, { ...GIFT_PRESENT }, null),
    (e: Error) => e.message.includes("不在本轮检索结果中"),
  );
});

test("检索后展示 → writer 依次收到 1 个 card + n 个 evidence，且都过契约 parse", () => {
  const seen: unknown[] = [];
  const writer = (chunk: unknown) => seen.push(chunk);
  const result = presentCard(searchedThisTurn(), { ...GIFT_PRESENT }, writer);
  assert.match(result, /推荐卡片已展示/);
  const card = CardEvent.parse(seen[0]);
  assert.equal(card.type, "card");
  assert.equal(seen.length, 1 + card.payload.evidenceRefs.length);
  for (const ev of seen.slice(1)) assert.equal(EvidenceEvent.parse(ev).type, "evidence");
  assert.deepEqual(
    card.payload.evidenceRefs.map((r) => r.evidenceId),
    ["ev-survey-02"],
  );
  assert.equal(AgentStreamEvent.parse(seen[0]).type, "card");
});

test("卡片商品价格等于 CATALOG 中的值（模型无法传价格）", () => {
  const seen: unknown[] = [];
  presentCard(searchedThisTurn(), { ...GIFT_PRESENT }, (c) => seen.push(c));
  const card = FlowerCardPayload.parse((seen[0] as { payload: unknown }).payload);
  for (const item of card.items) {
    const source = CATALOG.find((s) => s.id === item.id);
    assert.ok(source);
    assert.equal(item.priceFen, source.priceFen);
    assert.equal(item.name, source.name);
  }
});

test("search_catalog 按场景过滤，返回的商品不含 scenes 字段", () => {
  const gift = queryCatalog({ scene: "gift" });
  assert.deepEqual(
    gift.map((s) => s.id),
    ["fl-101", "fl-102"],
  );
  for (const item of gift) assert.equal("scenes" in item, false);
  // 两个检索工具的 content 都按 [id] 名称（可信度/价格）格式给模型看
});

test("status=error 的检索结果不算'检索过'", () => {
  const messages: BaseMessage[] = [
    human("送礼"),
    toolMsg("search_evidence", "error"),
    toolMsg("search_catalog", "error"),
  ];
  assert.throws(
    () => presentCard(messages, { ...GIFT_PRESENT }, null),
    (e: Error) => e.message.includes("请先调用 search_evidence"),
  );
});

test("同一轮第二次 present_recommendation 报错（每轮只展示一次）", () => {
  const messages: BaseMessage[] = [
    ...searchedThisTurn(),
    new AIMessage({ content: "", tool_calls: [] }),
    toolMsg("present_recommendation", "success"),
  ];
  assert.throws(
    () => presentCard(messages, { ...GIFT_PRESENT }, null),
    (e: Error) => e.message.includes("每轮只能展示一次推荐卡片"),
  );
});

test("检索工具直接调用：content 为 [id] 来源（可信度 n）格式", async () => {
  // 直接 .invoke（无 toolCallId）运行时返回 content 本身；类型上仍是 [content, artifact] 元组，故经 unknown 转换
  const evidenceContent = (await searchEvidence.invoke({})) as unknown as string;
  assert.match(evidenceContent, /\[ev-purchase-01\] 近 90 天订单记录（可信度 88）：/);
  const kinds = (await searchEvidence.invoke({ kinds: ["care_record"] })) as unknown as string;
  assert.doesNotMatch(kinds, /ev-purchase-01/);
  const catalogContent = (await searchCatalog.invoke({ scene: "gift" })) as unknown as string;
  assert.match(catalogContent, /\[fl-101\] 香槟乌梅礼盒 花盒 ¥128/);
  assert.doesNotMatch(catalogContent, /fl-001/);
});

test("集成：createAgent + fakeModel 先违规调用 → error ToolMessage 回灌 → 自我修正成功", async () => {
  const model = fakeModel()
    .respondWithTools([
      { name: "present_recommendation", args: { ...GIFT_PRESENT, evidenceIds: ["ev-fake"] } },
    ])
    .respondWithTools([{ name: "search_evidence", args: {} }])
    .respondWithTools([{ name: "search_catalog", args: { scene: "gift" } }])
    .respondWithTools([{ name: "present_recommendation", args: { ...GIFT_PRESENT } }])
    .respond(new AIMessage("已为您展示送礼推荐。"));

  const agent = createAgent({ model, tools: [searchEvidence, searchCatalog, presentRecommendation] });
  const result = await agent.invoke(
    { messages: [human("想给妈妈送生日礼物")] },
    { configurable: { thread_id: "wp1-it" } },
  );
  const messages = result.messages as BaseMessage[];
  const toolMessages = messages.filter((m): m is ToolMessage => ToolMessage.isInstance(m));
  assert.ok(toolMessages.some((m) => m.status === "error"), "中间应出现 error ToolMessage");
  const present = toolMessages.filter((m) => m.name === "present_recommendation");
  assert.equal(present.at(-1)?.status, "success");
  assert.equal(
    queryEvidence(undefined).length > 0,
    true,
    "证据库迁移自 evidence.py，非空",
  );
  const final = messages.at(-1) as AIMessage;
  assert.equal(final.content, "已为您展示送礼推荐。");
  assert.equal(model.callCount, 5);
});
