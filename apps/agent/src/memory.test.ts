// WP5 多轮记忆测试（04 §2 WP5）：同 thread 记忆、跨 thread 隔离、不带 threadId 等价旧行为、摘要压缩。
import assert from "node:assert/strict";
import { after, test } from "node:test";
import { fakeModel } from "langchain";
import { AIMessage, HumanMessage, type BaseMessage } from "@langchain/core/messages";
import { MemorySaver } from "@langchain/langgraph";
import { AgentStreamEvent } from "@agent-hub/shared";
import { buildAgent, runTurn } from "./agent.ts";
import { ScriptedChatModel, type Policy } from "./scripted-model.ts";
import { floraPolicy } from "./scripts.ts";
import { assertSequence } from "./sequence.ts";
import { startServer } from "./server.ts";

/** 记录每次模型调用收到的用户消息文本序列 */
function recordingPolicy(): { policy: Policy; calls: string[][] } {
  const calls: string[][] = [];
  const inner = floraPolicy;
  const policy: Policy = (messages: BaseMessage[]) => {
    calls.push(
      messages
        .filter((m) => HumanMessage.isInstance(m))
        .map((m) => String(m.content)),
    );
    return inner(messages);
  };
  return { policy, calls };
}

async function collect(agent: ReturnType<typeof buildAgent>, message: string, threadId: string) {
  const evs: AgentStreamEvent[] = [];
  for await (const e of runTurn(agent, { message, threadId })) evs.push(AgentStreamEvent.parse(e));
  assertSequence(evs);
  return evs;
}

test("同 threadId 两轮：第二轮模型收到的消息含第一轮的 HumanMessage", async () => {
  const { policy, calls } = recordingPolicy();
  const agent = buildAgent({ model: new ScriptedChatModel(policy) });
  await collect(agent, "想给妈妈送生日礼物", "mem-a");
  await collect(agent, "预算再低一点呢", "mem-a");
  assert.ok(calls.length >= 8, `两次对话应有多次模型调用，实际 ${calls.length}`);
  // 第二轮的某次调用能看到第一轮用户消息（checkpointer 回放）
  assert.ok(
    calls.slice(4).some((call) => call.includes("想给妈妈送生日礼物")),
    "第二轮应看到第一轮消息",
  );
  assert.ok(calls.at(-1)!.includes("预算再低一点呢"));
});

test("不同 threadId 互不可见", async () => {
  const { policy, calls } = recordingPolicy();
  const agent = buildAgent({ model: new ScriptedChatModel(policy) });
  await collect(agent, "想给妈妈送生日礼物", "mem-b1");
  await collect(agent, "日常自用来点花", "mem-b2");
  assert.ok(
    calls.every((call) => !call.includes("想给妈妈送生日礼物") || call.length === 1),
    "第二会话不应看到第一会话消息",
  );
});

test("HTTP：不带 threadId 两次请求互不可见；带相同 threadId 可见（透传验证）", async () => {
  const { policy, calls } = recordingPolicy();
  const server = await startServer(0, { model: new ScriptedChatModel(policy) });
  after(() => server.close());
  const { port } = server.address() as { port: number };
  const chat = (body: Record<string, unknown>) =>
    fetch(`http://127.0.0.1:${port}/api/agent/chat`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => r.text());

  await chat({ message: "想给妈妈送生日礼物" });
  const callsAfterFirst = calls.length;
  await chat({ message: "预算再低一点呢" });
  // 不带 threadId：第二次请求的所有调用都看不到第一条消息
  assert.ok(
    calls.slice(callsAfterFirst).every((call) => !call.includes("想给妈妈送生日礼物")),
    "不带 threadId 应等价单轮",
  );

  await chat({ message: "换一下场景", threadId: "t-wp5visible" });
  const callsAfterSecond = calls.length;
  await chat({ message: "预算再低一点呢", threadId: "t-wp5visible" });
  assert.ok(
    calls.slice(callsAfterSecond).some((call) => call.includes("换一下场景")),
    "相同 threadId 第二轮应看到第一轮消息",
  );
});

test("摘要：配置 summaryModel 且历史超阈值 → 主模型收到条数下降且含摘要内容", async () => {
  const summaryModel = fakeModel().respond(() => new AIMessage("这是对话摘要。"));
  // 触发阈值 6000 tokens 按 charsPerToken=4 估算 ≈ 24000 字符历史：每轮 ~2900 字符 × 10 轮
  const longTurn = (n: number) => `第${n}轮背景：${"顾客偏好浅色系低香型鲜花，预算区间五十到八十元。".repeat(120)}`;

  const { policy, calls } = recordingPolicy();
  const agent = buildAgent({
    model: new ScriptedChatModel(policy),
    summaryModel,
  });

  // 先积累超过 trigger(6000 tokens) 的历史
  for (let i = 1; i <= 10; i++) {
    await collect(agent, longTurn(i), "mem-sum");
  }
  // 触发摘要的下一轮：主模型收到的用户消息条数应明显低于未摘要时的历史
  await collect(agent, longTurn(11), "mem-sum");
  assert.ok(summaryModel.callCount >= 1, "摘要模型应被调用");
  const counts = calls.map((c) => c.length);
  const drops = counts.some((n, i) => i > 0 && n < counts[i - 1] - 1);
  assert.ok(drops, `摘要触发后模型收到的历史应骤减：${counts.join(",")}`);
  assert.ok(counts.at(-1)! <= 12, `摘要后历史保持有界：${counts.at(-1)}`);
  assert.ok(!calls.at(-1)!.some((t) => t.includes("第1轮背景")), "被摘要的早期消息不再进模型上下文");
});

test("摘要模型调用不产生出站事件（fakeModel 非流式，U5 离线侧验证）", async () => {
  const summaryModel = fakeModel().respond(() => new AIMessage("这是对话摘要。"));
  const agent = buildAgent({
    model: new ScriptedChatModel(floraPolicy),
    summaryModel,
  });
  const evs = await collect(agent, "想给妈妈送生日礼物", "mem-u5");
  assert.ok(!JSON.stringify(evs).includes("这是对话摘要"), "摘要文本不出站");
  assert.equal(evs.at(-1)?.type, "done");
});
