# AGENTS.md — Agent 工作约定

在本仓库工作的 AI Agent（Claude Code / Cursor / Codex 等）除遵守 `CLAUDE.md` 外，另遵守：

## 巡检多智能体升级项目（contracts-v1 起）

> 权威计划：`interview/docs/agent-hub/agent-hub升级报告与三智能体并行开发计划-v1.md`（仓库外）。
> 三个子智能体在独立 worktree 并行开发：A=`feat/runtime`（ah-A）、B=`feat/agents`（ah-B）、
> C=`feat/knowledge-eval`（ah-C），基线标签 `contracts-v1`。合并、推送、契约变更仅由主线程执行。

### 文件所有权矩阵（硬规则）

| 路径 | A 运行时 | B 编排 | C 知识评测 | 主线程 |
|---|:-:|:-:|:-:|:-:|
| `apps/api/app/runtime/**`、`tests/runtime/**` | **写** | 读 | 读 | 审 |
| `apps/api/app/tasks.py`、`tests/test_task_snapshots.py`、`tests/test_tasks.py` | **写** | — | — | 审 |
| `apps/api/app/agents/**`、`app/tools/**`、`app/skills/**`、`apps/mcp_inspection/**`、`tests/agents/**`、`tests/tools/**` | 读 | **写** | 读 | 审 |
| `apps/api/app/knowledge/**`、`apps/api/evals/**`、`tests/knowledge/**`、`tests/evals/**`、`fixtures/inspection/corpus/**`（追加） | 读 | 读 | **写** | 审 |
| `apps/api/app/contracts/**`、`apps/api/pyproject.toml`、`apps/api/tests/conftest.py`、本文件、`docs/adr/**` | 读 | 读 | 读 | **写** |
| `app/main.py`、`app/schemas.py`、`packages/shared/**`、`apps/web/**`、`e2e/**`、`.github/**`、`README.md`、Docker 文件 | — | — | — | **写（第 4 阶段）** |
| `docs/integration-requests/A|B|C.md` | 追加自己的 | 追加自己的 | 追加自己的 | 读并处理 |

需要改不属于自己的文件时：**停止修改**，在自己的集成请求文件追加
（文件 / 改动 / 原因 / 是否阻塞），用 `app/contracts/fakes.py` 的替身绕过，继续开发。

### 子智能体规则

1. 先写失败测试再实现；一个任务一个提交，格式 `feat(<模块>): <任务ID> <摘要>`；不推送、不合并。
2. 不编造数据与指标；离线评测报告必须标注"仅验证流程，不代表模型效果"。
3. 运行时不变量必须有自动化测试：单一执行者、过期写入被拒、写工具不重复执行、终态不可逆、崩溃后任务最终结束。
4. 收尾输出日报：完成任务 ID、测试命令与结果、未解决问题、集成请求、最不自信的 3 个点。
5. A 禁止 import langgraph；B 只经 `RunContext` 使用运行时（禁止 import `app.runtime`）、真实模型适配在 `app/agents/models.py` 自建（不改 `llm.py`——已删除，见 ADR-0003）；C 评测框架只依赖 `AgentRunner`。

## 工作边界

1. 只在既有分层内改动：契约（shared/contracts.ts ↔ api/schemas.py）的变更必须同步双镜像与 fixture，禁止只改一端。巡检新契约以 `app/contracts/**` 为准（第 0 阶段冻结；变更走集成请求，升 contracts-v2）。
2. 新功能先写明动机、验收标准与取舍，再动手。
3. 架构级取舍（依赖引入、目录调整、协议变更）写 ADR 到 `docs/adr/`，四位序号递增。
4. 禁止引入新依赖，除非：标准库/现有依赖/平台能力确实覆盖不了，且在 ADR 中记录对比。
5. 领域术语以 `CONTEXT.md` 为准；新术语先入表再使用。

## 完成定义（DoD）

- `pnpm build && pnpm test:shared && pnpm test:web && pnpm test:api` 全绿。
- 新增分支/解析/状态逻辑必须有单测（node:test 或 pytest），不引测试框架。
- 改动涉及用户故事（README 三条验收路径）时，真机冒烟走一遍并在 PR/提交说明里贴结果。
- 巡检运行时改动：`pytest tests/runtime tests/test_contracts_v1.py -q` 必须全绿（不变量测试）。

## 禁改区

- `fixtures/contract-example.json` 的既有字段（只能追加新示例）。
- `packages/shared/src/sse-decoder.ts` 的对外签名（帧协议兼容性承诺）。
- `.venv/`、`dist/`、`node_modules/`。
- `fixtures/inspection/**` 由 `generate.py` 再生成（C 只可追加 `corpus/`，不手改生成文件）。

