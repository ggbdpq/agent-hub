// 三个工具：检索证据、查询在售商品、展示推荐卡片（A2UI 即工具）。
// 证据因果：present_recommendation 只接受"本轮 search_evidence 真的返回过"的证据 id，
// 只接受"本轮 search_catalog 真的返回过"的 skuId；不满足就抛错 → ToolNode 回灌错误 → 模型自我修正。
// 检索与展示的核心逻辑抽成导出纯函数（queryEvidence / queryCatalog / presentCard），工具回调只做装配。
import { tool, type ToolRuntime } from "langchain";
import { HumanMessage, ToolMessage, type BaseMessage } from "@langchain/core/messages";
import { z } from "zod";
import { CardEvent, EvidenceEvent, FlowerCardPayload } from "@agent-hub/shared";
import { CATALOG, EVIDENCE, type EvidenceRecord, type FlowerItemRecord, type Scene } from "./data.ts";

/** 本轮（最后一条用户消息之后）某个工具返回的 artifact 里出现过的 id 集合 */
function idsSeenThisTurn(messages: BaseMessage[], toolName: string): Set<string> {
  const start = messages.findLastIndex((m) => HumanMessage.isInstance(m));
  const ids = new Set<string>();
  for (const m of messages.slice(start + 1)) {
    if (ToolMessage.isInstance(m) && m.name === toolName && m.status !== "error") {
      for (const item of (m.artifact as { id: string }[] | undefined) ?? []) ids.add(item.id);
    }
  }
  return ids;
}

/** 本轮某个工具是否已成功执行过（不看 artifact，用于"每轮只展示一次"） */
function toolSucceededThisTurn(messages: BaseMessage[], toolName: string): boolean {
  const start = messages.findLastIndex((m) => HumanMessage.isInstance(m));
  return messages
    .slice(start + 1)
    .some((m) => ToolMessage.isInstance(m) && m.name === toolName && m.status !== "error");
}

export function queryEvidence(kinds?: ("purchase_history" | "preference_survey" | "care_record")[]): EvidenceRecord[] {
  return Object.values(EVIDENCE).filter((e) => !kinds?.length || kinds.includes(e.sourceKind));
}

export function queryCatalog(opts: { scene?: Scene; maxPriceFen?: number } = {}): FlowerItemRecord[] {
  return CATALOG.filter(
    (s) => (!opts.scene || s.scenes.includes(opts.scene)) && (!opts.maxPriceFen || s.priceFen <= opts.maxPriceFen),
  ).map(({ scenes: _scenes, ...item }) => item);
}

export function presentCard(
  messages: BaseMessage[],
  input: { scene: Scene; title: string; reason: string; skuIds: string[]; evidenceIds: string[] },
  writer: ((chunk: unknown) => void) | null,
): string {
  // 05 U10：一轮只展示一张卡片
  if (toolSucceededThisTurn(messages, "present_recommendation")) {
    throw new Error("每轮只能展示一次推荐卡片，请直接用文字说明理由");
  }
  const evidenceSeen = idsSeenThisTurn(messages, "search_evidence");
  const skuSeen = idsSeenThisTurn(messages, "search_catalog");
  const badEvidence = input.evidenceIds.filter((id) => !evidenceSeen.has(id));
  const badSku = input.skuIds.filter((id) => !skuSeen.has(id));
  if (badEvidence.length || badSku.length) {
    // 抛错 = ToolMessage(status:error) 回灌给模型，让它先检索再推荐
    throw new Error(
      [
        badEvidence.length && `证据 ${badEvidence.join("、")} 不在本轮检索结果中，请先调用 search_evidence`,
        badSku.length && `商品 ${badSku.join("、")} 不在本轮查询结果中，请先调用 search_catalog`,
      ]
        .filter(Boolean)
        .join("；"),
    );
  }
  const payload = FlowerCardPayload.parse({
    scene: input.scene,
    title: input.title,
    reason: input.reason,
    // 商品字段（名称、价格、类型、emoji）只从目录取：模型无法报价（ADR-0004）
    items: input.skuIds.map((id) => {
      const hit = CATALOG.find((s) => s.id === id);
      if (!hit) throw new Error(`商品 ${id} 不在目录中`);
      const { scenes: _scenes, ...item } = hit;
      return item;
    }),
    evidenceRefs: input.evidenceIds.map((id) => {
      const hit = EVIDENCE[id];
      if (!hit) throw new Error(`证据 ${id} 不在证据库中`);
      return { evidenceId: id, label: hit.source };
    }),
  });
  // custom 流：卡片与它引用的证据按 card → evidence[] 顺序直达翻译层（与旧协议顺序一致）
  writer?.(CardEvent.parse({ type: "card", payload }));
  for (const ref of payload.evidenceRefs) {
    writer?.(EvidenceEvent.parse({ type: "evidence", evidence: EVIDENCE[ref.evidenceId] }));
  }
  return `推荐卡片已展示（${payload.items.map((i) => i.name).join("、")}）。接下来用一两句话说明推荐理由，不要重复卡片内容。`;
}

export const searchEvidence = tool(
  async ({ kinds }) => {
    const hits = queryEvidence(kinds);
    // content 给模型看（带 id 才能引用）；artifact 给服务端校验与 UI 用，不进模型上下文
    const content =
      hits.map((e) => `[${e.id}] ${e.source}（可信度 ${e.credibility}）：${e.quote}`).join("\n") ||
      "没有相关证据";
    return [content, hits];
  },
  {
    name: "search_evidence",
    description:
      "检索当前顾客的画像证据（订单记录、喜好问卷、养护回访）。推荐前必须先调用，推荐卡片只能引用这里返回的证据 id。",
    schema: z.object({
      kinds: z.array(z.enum(["purchase_history", "preference_survey", "care_record"])).optional(),
    }),
    responseFormat: "content_and_artifact",
  },
);

export const searchCatalog = tool(
  async ({ scene, maxPriceFen }) => {
    const hits = queryCatalog({ scene: scene as Scene | undefined, maxPriceFen });
    const content =
      hits.map((s) => `[${s.id}] ${s.name} ${s.typeLabel} ¥${(s.priceFen / 100).toFixed(0)}`).join("\n") ||
      "没有符合条件的在售商品";
    return [content, hits];
  },
  {
    name: "search_catalog",
    description: "查询在售花材商品。推荐卡片里的商品只能从这里返回的 skuId 中选择。",
    schema: z.object({
      scene: z.enum(["daily", "gift", "comfort"]).optional(),
      maxPriceFen: z.number().int().positive().optional(),
    }),
    responseFormat: "content_and_artifact",
  },
);

export const presentRecommendation = tool(
  // runtime 必须显式标注 ToolRuntime：不标注时 TS 会选第一组重载（第二参数是 ToolRunnableConfig），
  // state/writer 属性不存在，typecheck 报 TS2339（05 U4 实测记录）
  async (input, runtime: ToolRuntime) => {
    const messages = (runtime.state as { messages: BaseMessage[] } | undefined)?.messages ?? [];
    return presentCard(messages, input, runtime.writer);
  },
  {
    name: "present_recommendation",
    description: "把推荐以卡片形式展示给顾客。每次回答最多调用一次。",
    schema: z.object({
      scene: z.enum(["daily", "gift", "comfort"]),
      title: z.string().min(1).max(30),
      reason: z.string().min(1).max(80),
      skuIds: z.array(z.string()).min(1).max(3),
      evidenceIds: z.array(z.string()).min(1),
    }),
  },
);

export const TOOLS = [searchEvidence, searchCatalog, presentRecommendation];
