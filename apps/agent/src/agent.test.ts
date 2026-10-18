// WP3 智能体装配测试（04 §2）：三场景文案与旧 agent.py 逐字一致、自我修正、限额、上游异常、中止。
import assert from "node:assert/strict";
import { test } from "node:test";
import { HumanMessage, type BaseMessage } from "@langchain/core/messages";
import { AgentStreamEvent } from "@agent-hub/shared";
import { buildAgent, runTurn } from "./agent.ts";
import { ScriptedChatModel, type Policy } from "./scripted-model.ts";
import { floraPolicy } from "./scripts.ts";
import { assertSequence } from "./sequence.ts";
import { EVIDENCE } from "./data.ts";

async function collect(agent: ReturnType<typeof buildAgent>, message: string, threadId: string, signal?: AbortSignal) {
  const evs: AgentStreamEvent[] = [];
  for await (const e of runTurn(agent, { message, threadId }, signal)) evs.push(AgentStreamEvent.parse(e));
  assertSequence(evs);
  return evs;
}

const textOf = (evs: AgentStreamEvent[]) =>
  evs
    .filter((e): e is Extract<AgentStreamEvent, { type: "token" }> => e.type === "token")
    .map((e) => e.text)
    .join("");

const cardOf = (evs: AgentStreamEvent[]) =>
  evs.find((e): e is Extract<AgentStreamEvent, { type: "card" }> => e.type === "card")?.payload;

const SCENES = [
  {
    message: "这周想买点花放客厅",
    title: "本周自用鲜花组合",
    skus: ["fl-001", "fl-002"],
    evidence: ["ev-purchase-01", "ev-care-03"],
    answer: "根据你的订单记录，玫瑰类是最稳的选择；上次反馈尤加利难养护，这次换成好打理的洋桔梗盆栽，花期更长。",
  },
  {
    message: "想给妈妈送生日礼物",
    title: "送礼首选：轻奢礼盒",
    skus: ["fl-101", "fl-102"],
    evidence: ["ev-survey-02"],
    answer: "送礼场景我推荐礼盒款：香槟色系符合你浅色系的偏好，永生花款适合希望纪念意义更久的场合。",
  },
  {
    message: "下周去医院探望朋友",
    title: "探望慰问：素雅安神",
    skus: ["fl-201", "fl-202"],
    evidence: ["ev-care-03"],
    answer: "探望慰问以素雅为宜：白绿桔梗无浓香，小雏菊好打理，收花人不需要费心照顾。",
  },
] as const;

test("三场景：卡片标题、商品 id、证据 id、正文全文与旧 agent.py 一致", async () => {
  const agent = buildAgent({ model: new ScriptedChatModel(floraPolicy) });
  for (const [i, scene] of SCENES.entries()) {
    const evs = await collect(agent, scene.message, `scene-${i}`);
    const card = cardOf(evs);
    assert.ok(card, `${scene.message} 应有卡片`);
    assert.equal(card.title, scene.title);
    assert.deepEqual(
      card.items.map((s) => s.id),
      [...scene.skus],
    );
    assert.deepEqual(
      card.evidenceRefs.map((r) => r.evidenceId),
      [...scene.evidence],
    );
    assert.equal(textOf(evs), scene.answer);
    assert.equal(evs.at(-1)?.type, "done");
  }
});

test("幻觉证据策略 → 「校验未通过」thought → 最终 done 且卡片证据合法", async () => {
  let tried = false;
  const hallucinate: Policy = (messages: BaseMessage[]) => {
    if (!tried) {
      tried = true;
      return {
        text: "我直接给你推荐吧。",
        toolCalls: [
          {
            name: "present_recommendation",
            args: { scene: "gift", title: "送礼首选：轻奢礼盒", reason: "凭感觉", skuIds: ["fl-101"], evidenceIds: ["ev-fake"] },
          },
        ],
      };
    }
    return floraPolicy(messages);
  };
  const evs = await collect(buildAgent({ model: new ScriptedChatModel(hallucinate) }), "想送礼", "h");
  assert.ok(evs.some((e) => e.type === "thought" && e.text.includes("校验未通过")));
  assert.equal(evs.at(-1)?.type, "done");
  const card = cardOf(evs);
  assert.ok(card);
  for (const ref of card.evidenceRefs) assert.ok(EVIDENCE[ref.evidenceId], "卡片证据必须真实存在");
  assert.ok(!textOf(evs).includes("我直接给你推荐吧"), "工具调用前的开场白进思考区，不进正文");
});

test("死循环策略 → error{server} 含「步数超出上限」，模型调用 ≤ 8 次", async () => {
  let calls = 0;
  const looping: Policy = () => {
    calls += 1;
    return { toolCalls: [{ name: "search_evidence", args: {} }] };
  };
  const evs = await collect(buildAgent({ model: new ScriptedChatModel(looping) }), "随便", "loop");
  const last = evs.at(-1);
  assert.equal(last?.type, "error");
  assert.ok(last && last.type === "error" && /步数超出上限/.test(last.message), "限额错误文案");
  assert.ok(calls <= 8, `模型调用次数 ${calls} 超过 8`);
});

test("上游抛 {status:500} → 只有一个 error 事件，原始报错不外泄", async () => {
  class Boom extends ScriptedChatModel {
    async *_streamResponseChunks(): AsyncGenerator<never> {
      throw Object.assign(new Error("upstream exploded sk-xxx"), { status: 500 });
    }
  }
  const evs = await collect(buildAgent({ model: new Boom(floraPolicy) }), "送礼", "boom");
  assert.deepEqual(
    evs.map((e) => e.type),
    ["error"],
  );
  assert.ok(!JSON.stringify(evs).includes("sk-xxx"), "上游原始报错不外泄");
  const err = evs[0] as Extract<AgentStreamEvent, { type: "error" }>;
  assert.equal(err.code, "server");
  assert.equal(err.message, "模型服务暂不可用（500）");
});

test("中止：第一个事件后 abort → 最后一个事件是 error{aborted}", async () => {
  const ac = new AbortController();
  const slow = buildAgent({ model: new ScriptedChatModel(floraPolicy, { delayMs: 30 }) });
  const aborted: AgentStreamEvent[] = [];
  for await (const e of runTurn(slow, { message: "送礼", threadId: "abort" }, ac.signal)) {
    aborted.push(AgentStreamEvent.parse(e));
    if (aborted.length === 1) ac.abort();
  }
  assertSequence(aborted);
  const last = aborted.at(-1);
  assert.equal(last?.type, "error");
  assert.ok(last && last.type === "error" && last.code === "aborted");
});
