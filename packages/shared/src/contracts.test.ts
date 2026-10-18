import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { AgentStreamEvent, ChatRequest, FlowerCardPayload, Order, TaskSnapshot } from "./contracts.ts";

/**
 * 契约双镜像对账：fixtures/contract-example.json 同时被
 * apps/api/tests/test_contracts.py（pydantic）解析——两端都过，契约才没漂移。
 */

const FIXTURE = new URL("../../../fixtures/contract-example.json", import.meta.url);

const example = JSON.parse(readFileSync(FIXTURE, "utf-8")) as Record<string, unknown>;

test("契约示例：Agent 五类事件全部通过 zod 判别联合", () => {
  for (const key of ["thought", "token", "card", "evidence", "done"]) {
    const parsed = AgentStreamEvent.parse(example[key]);
    assert.equal(["thought", "token", "card", "evidence", "done"].includes(parsed.type), true);
  }
});

test("契约示例：花束卡片场景与金额单位（分）", () => {
  const card = FlowerCardPayload.parse((example["card"] as { payload: unknown }).payload);
  assert.equal(card.scene, "gift");
  assert.equal(card.items[0].priceFen, 12800);
});

test("契约示例：任务快照终态", () => {
  const task = TaskSnapshot.parse(example["taskCompleted"]);
  assert.equal(task.progress, 100);
});

test("契约示例：已支付订单", () => {
  const order = Order.parse(example["orderPaid"]);
  assert.equal(order.status, "paid");
});

test("契约外字段被拒绝（AI 输出白名单）", () => {
  assert.throws(() =>
    AgentStreamEvent.parse({ type: "hack", payload: "<script>alert(1)</script>" }),
  );
});

test("契约示例：对话请求（ChatRequest 双端对账，threadId 可选）", () => {
  const req = ChatRequest.parse(example["chatRequest"]);
  assert.equal(req.message, "想给妈妈送生日礼物");
  assert.equal(req.threadId, "t-0123456789");
  // 不带 threadId = 单轮（与旧行为一致）
  assert.equal(ChatRequest.parse({ message: "日常自用来点花" }).threadId, undefined);
  // threadId 长度边界：过短拒绝
  assert.throws(() => ChatRequest.parse({ message: "hi", threadId: "short" }));
});
