<!-- 集成请求收件箱：子智能体 C 追加，主线程读并处理。只追加，不改历史记录。 -->

# 集成请求 — 子智能体 C（知识检索 + 评测）

格式：`- [日期] 文件 / 改动 / 原因 / 是否阻塞`。阻塞请求先在日报标红。

## 待处理

- [2026-10-24] ~~`apps/api/pyproject.toml` / 新增 `jieba` 可选依赖~~ → **已结案（2026-10-24，U4 闭环）**：主线程直接装入了 worktree venv（jieba 0.42.1，未进 pyproject），C 已按同口径补跑三轴对比——jieba 最优变体 @5=0.892，**低于** bigram 最优变体 0.925（-3.3pp），未满足">2pp 才申请依赖"条件。结论：**bigram 足够，U4 关闭，不申请把 jieba 写进 pyproject**（venv 中的 jieba 仅为环境残留，套件在无 jieba 环境会自动跳过 jieba 变体，不影响 CI）。详见 evals/reports/retrieval-20261024-153400.md 结论段。
- [2026-10-24] `apps/api/app/contracts/fakes.py`（只读反馈，非改动请求）/ 原因：`ScriptedAgentRunner` 不发 `tool.called`/`metric.usage` 事件，评测框架用它跑 agent 套件时工具调用 P/R、参数准确率、Token 指标恒为空/0（诚实口径，已标注）。C 已自建 `evals/scripted_tools.py`（实现同一 AgentRunner 契约、发完整事件流、支持故障注入）绕过，未改契约。/ **不阻塞**：若主线程后续愿意给 fakes 增加工具事件能力，C 的评测框架无需改动即可消费。
  - **【主线程 2026-10-24 处理】不采纳（不改 fakes）**：`ScriptedAgentRunner` 的职责是"报告级回放"替身（A9 续跑开发用），工具级事件流属于真实编排层（B2 执行器）行为；给替身加工具事件会模糊替身与真实实现的边界，且 C 的 `scripted_tools.py` 已以同一契约补位。结论：维持现状，`scripted_tools.py` 作为评测自检参考实现保留。contracts-v1 无需变更。


