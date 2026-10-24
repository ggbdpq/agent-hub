<!-- 集成请求收件箱：子智能体 C 追加，主线程读并处理。只追加，不改历史记录。 -->

# 集成请求 — 子智能体 C（知识检索 + 评测）

格式：`- [日期] 文件 / 改动 / 原因 / 是否阻塞`。阻塞请求先在日报标红。

## 待处理

- [2026-10-24] `apps/api/pyproject.toml` / 新增 `jieba` 可选依赖（建议 extras：`dev` 或 `eval`）/ 原因：计划 §7.3-C2 与 U4 要求中文分词在 jieba 与字符 bigram 间按 Recall@5 对比后定论；jieba 未安装且 C 无权改 pyproject，本轮对比只能在 bigram vs unigram 之间完成（结论已写入 evals/reports/retrieval-*.md 的「分词方案结论（U4）」：混合+unigram 最优 0.942，bm25-only-bigram 0.925 次之，向量单路 0.858 最弱；HashingEmbedder 无真实语义，向量通道对融合是负贡献，接真实 embedding API 后需复核）。/ **不阻塞**：bigram 起步可满足 P0 验收，jieba 对比留待依赖到位后补跑。
- [2026-10-24] `apps/api/app/contracts/fakes.py`（只读反馈，非改动请求）/ 原因：`ScriptedAgentRunner` 不发 `tool.called`/`metric.usage` 事件，评测框架用它跑 agent 套件时工具调用 P/R、参数准确率、Token 指标恒为空/0（诚实口径，已标注）。C 已自建 `evals/scripted_tools.py`（实现同一 AgentRunner 契约、发完整事件流、支持故障注入）绕过，未改契约。/ **不阻塞**：若主线程后续愿意给 fakes 增加工具事件能力，C 的评测框架无需改动即可消费。

