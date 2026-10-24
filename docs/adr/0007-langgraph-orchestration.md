# ADR-0007：编排采用 LangGraph StateGraph（手写 Supervisor），检查点用 SQLite Saver

- 状态：已接受（U1 试验脚本已跑通后定稿）
- 日期：2026-10-24
- 相关：ADR-0002、ADR-0003（TS 侧 langchain.js）、计划 §4.3 / §12-U1

## 背景

巡检多智能体需要：Plan-and-Execute 规划 → 多设备并行扇出 → 汇总 → 报告与人工审批（可中断、
可续跑）。JD 点名 LangGraph。手写 Supervisor 才能讲清路由、超时与失败策略。

## 决定

1. `langgraph` StateGraph 手写 Supervisor；`langgraph-checkpoint-sqlite` 做检查点，
   `thread_id = run_id`；运行时（租约/续跑）与检查点（图状态）职责分离——A 不 import langgraph，
   B 不 import app.runtime。
2. 依赖一次加齐（`apps/api/pyproject.toml`，ADR-0002 的 venv + pip 路径不变）：

   | 包 | 实测版本（2026-10-24） | 用途 |
   |---|---|---|
   | `langgraph` | 1.2.17 | StateGraph / Send / interrupt |
   | `langgraph-checkpoint-sqlite` | 3.1.2 | SqliteSaver / AsyncSqliteSaver |
   | `langchain-core` | 1.7.0 | BaseChatModel / 假模型（FakeLLM 复用 GenericFakeChatModel） |
   | `mcp` | 2.3.0 | ToolSpec 注册表暴露为 MCP Server |
   | `numpy` | 2.5.4 | 向量余弦相似度 |
   | `rank-bm25` | 0.2.2 | BM25 检索（替代自写） |
   | `pytest-asyncio` | 1.4.0 | async 测试（`asyncio_mode=auto`） |

3. **U1 实测结论**（`apps/api/tests/test_langgraph_probe.py`，CI 跑 pytest 即回归）：
   - langgraph 1.2 中 `Send` 必须经 `add_conditional_edges` 发出；节点直接返回
     `list[Send]` 抛 `InvalidUpdateError`（与部分教程记忆不符，以实测为准）；
   - `interrupt()` 暂停后 `invoke` 结果带 `__interrupt__` 键；`Command(resume=...)` 的值
     即恢复后 `interrupt()` 的返回值；
   - 关闭再重开 `SqliteSaver.from_conn_string(同一 db)`（模拟进程重启）后，
     扇出结果保存在检查点中，恢复时已完成的 inspector 节点**不重复执行**；
   - 异步路径用 `AsyncSqliteSaver`（`langgraph.checkpoint.sqlite.aio`）。
4. 放弃 `langgraph-supervisor` 封装包与 CrewAI：黑盒封装讲不清策略；也放弃自研编排。

## 后果

- B 的编排契约只依赖 `AgentRunner` / `RunContext`；LLM 经 `ctx.llm` 工厂获取。
- 真实模型适配（OpenAI 兼容）在 `app/agents/models.py` 自建，不改 TS 侧 ADR-0003 的 `apps/agent`。
- 并行分支中的 interrupt 采用"单 interrupt 点"设计（审批只发生在 reporter），
  规避多中断点恢复语义的复杂性（YAGNI）。
