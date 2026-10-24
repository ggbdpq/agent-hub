import { z } from "zod";

/**
 * 花语智能体契约层（单一事实源）
 *
 * 前后端共同的"接口词典"：web 用 zod 在边界处 parse，python 侧有 pydantic 镜像
 * （apps/api/app/schemas.py），两端以 fixtures/contract-example.json 同源对账。
 * AI 输出只允许白名单字段进 UI——契约即安全边界。
 */

/* ---------------------------------- 依据 ---------------------------------- */

/** 依据来源类型：AI 推荐结论必须能指认"从哪来" */
export const EvidenceSourceKind = z.enum(["purchase_history", "preference_survey", "care_record"]);
export type EvidenceSourceKind = z.infer<typeof EvidenceSourceKind>;

/** 依据：来源 + 可信度 + 引用原文。可信度 <80 前端渲染金色预警（信任阈值） */
export const Evidence = z.object({
  id: z.string(),
  source: z.string(),
  sourceKind: EvidenceSourceKind,
  /** 可信度 0-100，当前为服务端固定 fixture，前端只做分级渲染 */
  credibility: z.number().int().min(0).max(100),
  /** 引用原文（顾客对话/记录原文片段） */
  quote: z.string(),
  analyzedAt: z.string(),
});
export type Evidence = z.infer<typeof Evidence>;

export const EvidenceRef = z.object({
  evidenceId: z.string(),
  label: z.string(),
});
export type EvidenceRef = z.infer<typeof EvidenceRef>;

/* ------------------------------- A2UI 花束卡片 ------------------------------ */

/** 推荐场景：日常自用 / 送礼 / 探望慰问——场景决定卡片段式与文案 */
export const RecommendScene = z.enum(["daily", "gift", "comfort"]);
export type RecommendScene = z.infer<typeof RecommendScene>;

export const FlowerItem = z.object({
  id: z.string(),
  name: z.string(),
  /** 单位：分（避免浮点金额） */
  priceFen: z.number().int().nonnegative(),
  /** 商品类型标签：花束 / 盆栽 / 永生花 */
  typeLabel: z.string(),
  emoji: z.string(),
});
export type FlowerItem = z.infer<typeof FlowerItem>;

/** A2UI 卡片载荷：智能体结构化输出 → 前端按场景渲染，字段白名单 */
export const FlowerCardPayload = z.object({
  scene: RecommendScene,
  title: z.string(),
  reason: z.string(),
  items: z.array(FlowerItem).min(1),
  evidenceRefs: z.array(EvidenceRef),
});
export type FlowerCardPayload = z.infer<typeof FlowerCardPayload>;

/* ------------------------------ Agent 事件流 ------------------------------ */

export const ThoughtEvent = z.object({
  type: z.literal("thought"),
  /** 思考分段序号，从 1 开始 */
  seq: z.number().int().positive(),
  text: z.string(),
  /** 本段是否结束（前端据此决定是否追加下一段） */
  final: z.boolean(),
});
export const TokenEvent = z.object({ type: z.literal("token"), text: z.string() });
export const CardEvent = z.object({ type: z.literal("card"), payload: FlowerCardPayload });
export const EvidenceEvent = z.object({ type: z.literal("evidence"), evidence: Evidence });
export const DoneEvent = z.object({ type: z.literal("done"), replyId: z.string() });
export const ErrorEvent = z.object({
  type: z.literal("error"),
  code: z.enum(["protocol", "server", "aborted"]),
  message: z.string(),
});

/** SSE data 帧的完整事件联合 */
export const AgentStreamEvent = z.discriminatedUnion("type", [
  ThoughtEvent,
  TokenEvent,
  CardEvent,
  EvidenceEvent,
  DoneEvent,
  ErrorEvent,
]);
export type AgentStreamEvent = z.infer<typeof AgentStreamEvent>;

/* ------------------------------- 对话请求 -------------------------------- */

/** 对话请求：原来只在 schemas.py 里有，现在进入单一事实源 */
export const ChatRequest = z.object({
  message: z.string().trim().min(1).max(2000),
  /** 会话标识；不传 = 单轮（与旧行为一致）。前端生成，点"清空"时重置 */
  threadId: z.string().min(8).max(64).optional(),
});
export type ChatRequest = z.infer<typeof ChatRequest>;

/* --------------------------- 异步任务（养护方案） --------------------------- */

export const TaskStatus = z.enum([
  "queued",
  "running",
  "completed",
  "failed",
  "cancelled",
  "archived",
]);
export type TaskStatus = z.infer<typeof TaskStatus>;

/** 后端状态 → 页面态的映射在前端集中维护（TASK_STATUS_TO_PAGE_STATUS） */
export const TaskSnapshot = z.object({
  taskId: z.string(),
  /** 业务单号（有序可读，页面展示用）：YH+日期+每日序号；taskId 是内部主键不上页面 */
  taskNo: z.string(),
  kind: z.literal("care_plan"),
  status: TaskStatus,
  progress: z.number().int().min(0).max(100),
  /** pydantic 会把 None 序列化成 null：这里必须 nullish（optional 只收 undefined） */
  result: z
    .object({
      title: z.string(),
      sections: z.array(z.object({ heading: z.string(), body: z.string() })),
    })
    .nullish(),
  failReason: z.string().nullish(),
});
export type TaskSnapshot = z.infer<typeof TaskSnapshot>;

/* ----------------------------- 订单与支付沙箱 ----------------------------- */

export const PaymentChannel = z.enum(["wechat", "alipay"]);
export type PaymentChannel = z.infer<typeof PaymentChannel>;

export const OrderStatus = z.enum(["created", "paying", "paid", "closed"]);
export type OrderStatus = z.infer<typeof OrderStatus>;

export const Order = z.object({
  orderId: z.string(),
  /** 业务单号（有序可读，页面展示用）：DD+日期+每日序号 */
  orderNo: z.string(),
  flowerName: z.string(),
  /** 单位：分 */
  amountFen: z.number().int().positive(),
  channel: PaymentChannel,
  status: OrderStatus,
  /** pydantic None → JSON null：nullish 而非 optional（下单未支付时为 null） */
  paidAt: z.string().nullish(),
});
export type Order = z.infer<typeof Order>;

/** 支付回调（模拟微信/支付宝异步通知）：验签 + 幂等 */
export const PaymentNotify = z.object({
  orderId: z.string(),
  channel: PaymentChannel,
  /** 模拟签名：sha256(orderId + channel + SECRET) 的前 16 位 */
  sign: z.string(),
});
export type PaymentNotify = z.infer<typeof PaymentNotify>;

/* ------------------------- 巡检多智能体（/api/runs） ------------------------ */
/**
 * 与 apps/api/app/contracts/events.py（pydantic）对账；fixtures/contract-example.json
 * 的 runEvent* 样例两端同源解析。事件由 SSE 携带：event: <type> + data: <RunEvent JSON>
 * （单行完整 JSON，CLAUDE.md 铁律 4）；id: seq 供 Last-Event-ID 断点续传。
 */
export const RunEventType = z.enum([
  "run.queued",
  "run.started",
  "run.resumed",
  "run.completed",
  "run.failed",
  "run.cancelled",
  "run.partial",
  "node.started",
  "node.completed",
  "node.failed",
  "tool.called",
  "tool.result",
  "tool.denied",
  "tool.deduplicated",
  "plan.created",
  "evidence.attached",
  "approval.required",
  "approval.resolved",
  "metric.usage",
]);
export type RunEventType = z.infer<typeof RunEventType>;

export const RunEvent = z.object({
  runId: z.string(),
  /** 每个 run 内严格递增、无空洞（RunStore 事务内分配），对应 SSE 的 id 头 */
  seq: z.number().int().nonnegative(),
  type: RunEventType,
  /** supervisor / planner / inspector / reporter */
  node: z.string().nullish(),
  /** 第几次执行（续跑后递增） */
  attempt: z.number().int().nonnegative(),
  payload: z.record(z.string(), z.unknown()),
  ts: z.string(),
});
export type RunEvent = z.infer<typeof RunEvent>;

export const RunStatus = z.enum(["queued", "running", "completed", "failed", "cancelled", "partial"]);
export type RunStatus = z.infer<typeof RunStatus>;

export const RunRecord = z.object({
  runId: z.string(),
  kind: z.string(),
  status: RunStatus,
  input: z.record(z.string(), z.unknown()),
  result: z.record(z.string(), z.unknown()).nullish(),
  error: z.string().nullish(),
  attempt: z.number().int().nonnegative(),
  fencingToken: z.number().int().nonnegative(),
  leaseOwner: z.string().nullish(),
  leaseExpiresAt: z.string().nullish(),
  cancelRequested: z.boolean(),
  idempotencyKey: z.string().nullish(),
  createdAt: z.string(),
  updatedAt: z.string(),
});
export type RunRecord = z.infer<typeof RunRecord>;

/** 巡检请求（POST /api/runs body） */
export const InspectionRequest = z.object({
  site: z.string(),
  deviceIds: z.array(z.string()).min(1),
  window: z.string().nullish(),
  goal: z.string().nullish(),
});
export type InspectionRequest = z.infer<typeof InspectionRequest>;

/** 巡检发现：结论必须引用真实存在的证据 chunk_id（B4 剔除未观测引用） */
export const Finding = z.object({
  deviceId: z.string(),
  level: z.enum(["normal", "warning", "critical"]),
  summary: z.string(),
  evidenceChunkIds: z.array(z.string()),
  metrics: z.record(z.string(), z.unknown()),
});
export type Finding = z.infer<typeof Finding>;

export const WorkOrder = z.object({
  deviceId: z.string(),
  title: z.string(),
  action: z.string(),
  status: z.enum(["draft", "approved", "rejected", "executed"]).default("draft"),
  approvalId: z.string().nullish(),
});
export type WorkOrder = z.infer<typeof WorkOrder>;

/** 巡检报告（run 终态后的 result 字段） */
export const InspectionReport = z.object({
  findings: z.array(Finding),
  workOrders: z.array(WorkOrder),
  status: z.enum(["completed", "partial", "failed"]),
  /** 0-1：已完成子任务 / 计划子任务 */
  coverage: z.number(),
  notes: z.string().nullish(),
});
export type InspectionReport = z.infer<typeof InspectionReport>;
