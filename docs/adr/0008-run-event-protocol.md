# ADR-0008：运行事件协议（RunEvent）与 SSE 续传语义

- 状态：已接受
- 日期：2026-10-24
- 相关：ADR-0006、计划 §5.2

## 背景

巡检多智能体的三类消费方（前端时间线、评测复盘、调试）读同一份事件流；
崩溃续跑要求事件可按序号断点续传；CLAUDE.md 铁律 4/5 已规定 SSE 帧格式与
花语五事件——巡检走**新事件族**，不复用五事件协议（绞杀者模式）。

## 决定

1. `apps/api/app/contracts/events.py`：`NewEvent`（调用方提交：type/node/payload）→
   `RunEvent`（持久化后：run_id/seq/attempt/ts 由 RunStore 分配）。
   19 种 `EventType` 覆盖 run 生命周期、节点、工具、计划、证据、审批、用量（见契约源码）。
2. `seq` 在每个 run 内严格递增、无空洞，锁/事务内分配；`events_after(run_id, after_seq)`
   支撑 SSE `Last-Event-ID` 续传。
3. `attempt`：续跑后递增（由 claim 分配）；事件按其归属执行记录 attempt，复盘可区分两次执行。
4. 时间统一 aware UTC；租约过期比较用数据库时间函数（SQLite 版）。
5. fencing 语义精确化：写入校验"token 是当前值且 status=running"。租约过期但未被
   重新认领时 token 仍是当前值，合法完成写入不误拒；被重新认领后旧执行者一律
   `LeaseLost` / `False`。（对计划 §5.2"token 过期则拒绝"的细化，理由：避免误拒
   "租约刚好到期但无人接管"的合法写入；过期防护由心跳失败 + 编排层主动停止承担。）
6. `RunContext` 用 dataclass 而非 pydantic（对计划 §5.2 的偏差）：承载 emit/cancelled
   等 callable，pydantic 校验无价值。
7. `metric.usage` 事件承载 tokens / cost / latency；评测框架消费，不在运行时聚合。

## 后果

- 第 4 阶段 I6 才把 `RunEvent` 镜像进 `contracts.ts` / `schemas.py`（契约双镜像铁律届时同步）。
- 事件 payload 不定 Schema（dict）：评测与前端只依赖 type 分发；代价是 payload 内部字段
  无契约保护，由消费方容错——demo 项目可接受（YAGNI）。
