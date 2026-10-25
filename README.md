# agent-hub · Agent 工程演示：巡检多智能体 + 持久化运行时 + MCP/Skill + 混合检索 + 评测

一个 monorepo 承载两个互补的演示：

- **[设备巡检多智能体](docs/inspection.md)**（后端 Agent 工程）：LangGraph Supervisor + 三类 Worker、SQLite 持久化运行时（租约/fencing token/CAS，`kill -9` 后自动接管续跑）、工具按读/写/高危分级并暴露为 MCP Server 与 Skill、BM25+向量+RRF 混合检索提供可回溯证据、离线评测质量门与消融实验。
- **[花语智能体](docs/flora.md)**（流式渲染与 A2UI 演示）：SSE 事件协议、Zod/Pydantic 契约镜像、A2UI 结构化卡片、证据溯源抽屉、异步任务恢复——前端与流式工程主线。

**无需真实 API Key**：两个演示都有确定性离线档（巡检 `AGENT_HUB_LLM=scripted`，花语 `ScriptedChatModel`），离线可跑、输出可断言、CI 全离线。

React 19 + TypeScript 7 + Vite 8 · langchain.js（花语智能体服务）· Python 3.12 + FastAPI + LangGraph（巡检多智能体）· SQLite（WAL）

## Quick Start

```bash
pnpm install      # JS workspace 依赖
pnpm api:setup    # python venv + pip install -e apps/api[dev]
pnpm demo         # 一键启动 FastAPI :8808 + TS 智能体 :8809 + Vite :8888
# 或容器化一键演示（巡检 LLM 默认离线确定性档，无需真实 Key）：
docker compose up --build   # 打开 http://localhost:8888
```

打开 <http://localhost:8888>：

1. **设备巡检 `/runs`**：发起巡检 → 时间线按节点推进 → critical 设备触发人工审批 → 批准/拒绝 → 报告（覆盖率、分级 findings、证据引用、工单）。运行中 `kill -9` API 进程再重启，任务自动接管续跑（E2E 覆盖）。
2. **对话页 `/`**：提问「想给妈妈送生日礼物」→ 思考分段流式 → 推荐卡片 → 点「依据」看溯源 → 点「下单」。
3. **任务页 `/tasks`**：创建养护方案任务 → 刷新按 `taskId` 恢复并自动追踪到终态。
4. **订单页 `/orders`**：沙箱收银台模拟支付，重复回调不重复入账。

前置：Node ≥ 24（pnpm 12）、Python ≥ 3.12。命令行崩溃恢复演示：`python -m app.runtime.demo_crash`。

## 验证

| 命令 | 覆盖 |
| --- | --- |
| `pnpm build` | `tsc -b` + `vite build`（类型与构建门槛） |
| `pnpm test:shared` | SSE 解码器 / 契约双镜像对账 / bones 断点（node:test） |
| `pnpm test:web` | 聊天 store + 受控 HTTP 流（node:test） |
| `pnpm test:agent` | 工具因果校验 / 事件翻译 / 剧本 / 记忆 / HTTP 层（node:test） |
| `pnpm test:api` | 契约不变量 / 运行时（租约·CAS·账本·kill -9 续跑）/ 编排恢复 / 检索 / 评测框架 / API 路由（pytest） |
| `pnpm test:e2e` | 九条端到端（Playwright，离线剧本驱动无需 Key）：花语四条 + 巡检三条 + **SIGKILL 进程重启两条**（运行中重启续跑 / 审批等待期重启恢复，写工具不重复执行） |
| `pnpm test:e2e:restart` | 单独跑进程重启套件 |
| `python -m evals.gate` | 离线评测质量门：检索 Recall@k / 成功率 / 越权次数低于阈值即退出码非 0（CI 红） |
| `python -m app.runtime.demo_crash` | 崩溃恢复演示：kill -9 → 回收 → 续跑 → 完整事件序列 |

消融与评测报告见 `apps/api/evals/reports/`（方法、样本量、方差、已知偏差齐全；离线数字仅验证流程，不代表模型效果）。机制深读与面试材料见 [docs/interview/](interview/)。

推送即跑 CI（Node 24 + Python 3.12，含质量门与九条 E2E）。

## Limitations

- **单机演示定位**：无鉴权、无多租户；巡检侧 SQLite（WAL）单机运行（Postgres 迁移路径见 ADR-0006），花语侧任务/记忆为进程内存储 + JSON 快照；支付为沙箱模拟。
- **巡检数据与口径**：`fixtures/inspection` 全部为合成数据（确定性生成器可复现）；离线评测数字仅验证工程流程，不代表模型效果；真实模型报告与消融结论以 `evals/reports/` 为准。
- **诚实声明的已知边界**（详见 [docs/inspection.md](docs/inspection.md)）：审批恢复重放 reporter 节点（事件按 id 幂等、写工具账本去重）；非中断点崩溃重跑靠 reducer 去重兜底；WAL 多进程高并发未压测。
- 生产化差距（DB 唯一键 + 分布式锁、平台公钥验签、服务端签发 threadId 等）在代码注释与 [docs/adr](docs/adr/) 中注明。

## License

[MIT](LICENSE)
