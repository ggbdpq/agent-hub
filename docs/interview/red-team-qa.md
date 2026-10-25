# 红蓝对抗：30 道面试官追问与标准答案（Z2）

> 定位：以面试官立场把这套系统往死里问。每题给标准答案与代码证据（文件:行号基于 `ah-Z@docs/deep-dive` 基线 `ebf93a3`；测试名为 `apps/api/tests/` 下真实用例）。答不了的如实标注**待确认**并写明缺什么——面试现场宁可说"没测过"，不可编造。
>
> 诚实素材索引（可主动交底，别等被抓住）：SQLite 单机定位（ADR-0006）；评测集合成数据（README:125）；E1 工具 P/R 口径错配修正中（docs/integration-requests/X.md:11-16）；SSE 终态竞态已修（c6eceed）；WAL 多进程并发写未压测（U3 / ADR-0006 后果）；编排层续跑只覆盖"中断挂起"形态（README:125）。

---

## A. 运行时一致性

### Q1. 两个进程同时 claim 同一个 run，真的只有一个赢吗？靠什么保证？

**答**：靠 `BEGIN IMMEDIATE` 的写锁语义。claim 的候选 SELECT 与抢占 UPDATE 在同一个写事务里（store.py:194-213），`BEGIN IMMEDIATE`（store.py:456）在事务开启瞬间就取写锁；第二个实例在 busy_timeout 内等待第一个 COMMIT，之后它的候选子查询在锁内重评——过期 running 已变成持有有效租约的 running，不再命中候选条件。锁等待若耗尽，还有模块级 BUSY 指数退避重试（store.py:110-152）。
**证据**：`tests/runtime/test_claim_lease.py::test_concurrent_claim_single_winner_across_instances`（两个独立 `SqliteRunStore` 实例、独立连接、线程并发认领，断言单胜者）；`tests/runtime/test_lock_retry.py::test_store_writes_retry_until_foreign_lock_released`。

### Q2. fencing 校验只看 token、不看租约过期——过期但没被接管的旧 worker 还能写事件，这不是漏洞吗？

**答**：是有意的精确语义，不是漏洞。校验条件是"token 是当前值且 status=running"（store.py:286、322）。租约刚过期但无人接管的窗口里，token 没变，旧 worker 马上要写的完成结果/收尾事件是合法的，按"过期即拒"会误拒合法完成、让任务白跑一遍。"被顶替"由 token 变化拒绝；"自己过期但没人管"由心跳失败→编排层主动停止承担（heartbeat.py:67 置 lost；worker.py:151-153 传导）。两条防线分工，ADR-0008 第 5 条记录了这个决策。
**证据**：`tests/runtime/test_cas_writes.py::test_expired_lease_without_takeover_can_finish`（放行合法完成）与 `test_stale_writer_rejected_after_takeover`（接管后一律拒）——两个测试合成完整语义。

### Q3. 取消和完成同时发生，最终状态听谁的？

**答**：听第一个 CAS 成功者的。取消是协作式：`request_cancel` 只置标记，不改状态不抢租约（store.py:344-360）；终态写入是 `finish` 的 CAS（`WHERE status='running' AND fencing_token=?`，store.py:319-331）。谁先过 CAS 谁定终态，另一个自然失败——不存在双方都成功的状态混合。标记写入与判定同事务（BEGIN IMMEDIATE），与跨实例 finish 竞争时不会把终态 run 污染上取消标记（store.py:347-349 docstring）。
**证据**：`tests/runtime/test_cooperative_cancel.py::test_cancel_and_finish_race_first_cas_wins`。

### Q4. 那取消标记永远没人消费会怎样？run 卡死吗？

**答**：不会卡死。标记的消费方是持有租约的 worker：编排层在步边界/工具前读 `ctx.cancelled()` 提前收尾（executor.py:89-91、inspector.py:112-114），worker 把"partial + cancelled()"映射成终态 cancelled（worker.py:178-179）。worker 若崩溃，租约过期 → 接管者认领后同样会看到标记。最坏情况（标记置了但 run 已终态）是良性的：终态幂等返回原记录，不置标记（store.py:355-359）。
**证据**：`tests/runtime/test_worker_resume.py::test_worker_maps_cancel_requested_partial_to_cancelled`；`test_cooperative_cancel.py::test_request_cancel_idempotent_on_terminal`。

### Q5. 并发追加事件时 seq 会不会有洞或者重号？

**答**：不会。seq 分配是事务内 `SELECT COALESCE(MAX(seq),0)+1` 再 INSERT（store.py:475-480、288-293），与 append_event 的 fencing 校验同一事务；同 run 的写入被写锁串行化。跨实例并发也一样——第二个事务看到第一个已提交的最大 seq。
**证据**：`tests/runtime/test_event_log.py::test_event_seq_no_holes_across_instances`（两个实例交错追加，断言无洞无重复）。

### Q6. 租约比较用的进程时钟还是数据库时钟？机器时钟漂移怎么办？

**答**：数据库时钟。`_now_us` 未注入 clock 时取 SQLite `unixepoch('subsec')`（store.py:467-473），过期判定、事件时间戳全走它——多进程看到同一份数据库时钟，进程时钟漂移不影响正确性。测试注入可控时钟（冻结/推进）做确定性验证；kill -9 测试不注入时钟，验证真实跨进程时间语义。
**证据**：store.py:10-13 docstring（对抗审查清单第 1 条的落实）；`tests/runtime/test_kill9_reclaim.py::test_kill9_then_reclaimed_within_lease`。

### Q7. WAL 下 database is locked，你的重试会不会把半个事务重跑一遍？

**答**：不会。`BEGIN IMMEDIATE` 的 BUSY 发生在任何语句执行前——事务还没开，无部分写入；autocommit 下的单语句写失败即无副作用；WAL + IMMEDIATE 事务**内**不再出现写锁竞争。所以"整个方法重跑"是安全的（store.py:155-166 类装饰器 docstring 论证了这一点）。重试只针对 BUSY/LOCKED 错误码，业务异常、IntegrityError、no such table 都不重试（store.py:134-152）。
**证据**：`tests/runtime/test_lock_retry.py::test_retry_does_not_touch_business_or_non_busy_errors`、`test_retry_gives_up_after_max_attempts`。

### Q8. 多进程 worker 你压测过吗？能扛多少 QPS？

**答**：**没压测过**，如实说。这是登记在案的风险 U3（ADR-0006 后果、计划 §12）：并发模型以单进程多协程为主，多进程只用于崩溃注入测试（test_kill9_reclaim 是单受害者单接管者）。缺的是多进程并发写压测脚本与数据——多大吞吐下 BUSY 重试耗尽、p99 延迟多少，我没有数字。已有的是有限重试 + busy_timeout=5000ms 的机制与针对性测试，不是容量结论。
**证据**：ADR-0006"后果"；`tests/runtime/test_lock_retry.py`（机制测试，非容量测试）。**待确认**：多进程写吞吐与锁冲突率，需要专门压测脚本。

---

## B. LangGraph 细节

### Q9. Send 扇出为什么必须走 add_conditional_edges？节点直接返回 list[Send] 不行吗？

**答**：不行，langgraph 1.2 会抛 `InvalidUpdateError`。这是 U1 试验脚本实测的结论，和网上不少教程的记忆相反——所以先写探针再定 ADR，探针进 CI 防版本漂移。图里扇出是 `graph.add_conditional_edges("planner", lambda state: [Send("inspector", task) for task in state["tasks"]], ["inspector"])`。
**证据**：graph.py:251-256；ADR-0007 第 3 条；`tests/test_langgraph_probe.py::test_sync_send_interrupt_sqlite_checkpoint` / `test_async_send_interrupt_sqlite_checkpoint`。

### Q10. interrupt 恢复时 reporter 整个节点从头重跑，工单不会创建两次吗？

**答**：账本吸收。重跑的 reporter 会再次调 create_work_order，但 ledger_key 是 `run_id:reporter:wo:<device_id>`（graph.py:207），与第一次执行相同；`begin` 对已完成键返回 completed 原记录，执行器直接复用结果、发 `tool.deduplicated` 事件，handler 不再执行。派机同理（graph.py:232）。
**证据**：`tests/agents/test_approval.py::test_reporter_rerun_does_not_duplicate_work_orders`（断言 tool.called 1 次 + tool.deduplicated 1 次）；跨进程场景 `tests/agents/test_runner_resume.py::test_reclaimed_run_resumes_from_checkpoint_not_rerun` 同断言。

### Q11. 为什么 interrupt 只设计一个点？审批要是出现在别的节点怎么办？

**答**：YAGNI 的显式取舍（ADR-0007 决定 4）。dangerous 工具只有 dispatch_drone，消费点只在 reporter；单 interrupt 点让"恢复时哪个节点挂起、resume 值去哪"的推理保持平凡——多中断点恢复的语义组合会爆炸。若未来出现第二个 dangerous 消费点，要么收敛到一个审批节点，要么为每个点设计独立 resume 路由并补 U1 式探针。dangerous 工具的审批凭证校验在执行器层强制（executor.py:102-104），interrupt 只是产生凭证的通道——点少不等于防线少。
**证据**：graph.py:224（唯一 interrupt）；executor.py:102-104；`tests/tools/test_executor.py::test_dangerous_tool_requires_approval_id`。

### Q12. recursion_limit 设了吗？审批来回几十次会不会把图撑爆？

**答**：没显式设置 recursion_limit，如实说——依赖 LangGraph 默认值（25）加图静态深度小（START→supervisor→planner→inspector 并行一层→reporter→END）。审批循环有应用层硬上限：runner 里 guard>8 抛 RuntimeError 中止（runner.py:98-99），异常由 worker 兜底落 failed 终态（worker.py:184-185），不会无限循环也不会永远 running。这个设计的漏洞我承认：如果未来图变深（比如加两级规划），默认 25 可能先于业务上限触发——届时应显式设置并把两处上限对齐。
**证据**：runner.py:95-105；worker.py:184-185。

### Q13. 检查点为什么用 langgraph-checkpoint-sqlite？换 Postgres/Redis 呢？

**答**：与运行时同库（SQLite WAL），单机演示定位下少一个组件、少一类故障；`thread_id = run_id` 让两个持久化域共享身份（ADR-0007 决定 1）。Saver 是 LangGraph 的标准接口，AsyncSqliteSaver 换成官方 Postgres Saver 是配置级替换，图代码不动。没选 Redis 是因为检查点需要的是持久化不是低延迟，内存检查点重启即丢（花语侧 MemorySaver 的教训写在 README:127）。
**证据**：runner.py:15（InMemorySaver 兜底）、main.py:174（生产装配注入 saver）；`tests/agents/test_checkpoints.py::test_crash_before_approval_resumes_without_rerunning_planner_inspector`。

### Q14. 图状态为什么用 TypedDict + dict，不用 pydantic 模型？

**答**：检查点序列化友好与 reducer 语义。并行 inspector 的输出靠 Annotated reducer 合并（graph.py:65-66 用 `_merge_by_device`），dict 全程避免了 pydantic 对象在检查点读写、deepcopy、合并上的坑；入参出参的校验没省——request 在 supervisor 用 pydantic 校验（graph.py:96-101），report 出图时 `InspectionReport.model_validate`（runner.py:120）。即"边界 pydantic、内核 dict"。
**证据**：graph.py:55-69 docstring 明示该取舍；`tests/agents/test_isolation.py::test_graph_state_has_no_message_history`（同时验证状态里不积攒消息历史，控制检查点体积）。

### Q15. A 禁止 import langgraph、B 禁止 import app.runtime——这种规定有什么用，不别扭吗？

**答**：是防腐层。运行时（租约/fencing/事件）与编排（图/检查点）各自演进：A 可以换存储实现而不动图，B 可以重构图而不碰租约；两者只经 `RunContext`（emit/cancelled/budget/ledger，contracts/agent.py）交互。这个约束的价值在 R1 修复时兑现过——审批恢复缺陷修在 B 的 runner 入口（runner.py:84-92），运行时零改动；反过来 fencing 语义细化（ADR-0008 第 5 条）修在 A 的 store，编排零改动。
**证据**：AGENTS.md 子智能体规则 5；ADR-0007 决定 1。

---

## C. 评测方法论

### Q16. 评测集是合成的？那你这些指标不就是自己考自己？

**答**：合成是前提，但期望不是拍的——期望从遥测数据推导而不是从生成器注释抄。评测集 60 条、难度三层各 20、含 10 条已知类型的故障注入（独立测试锁结构）；expected_anomalies 由数据派生：生成器注入异常后，期望集直接对 fixtures 遥测跑确定性判读得出，生成器注释里的"这是异常设备"不进入期望（防泄露）。所有报告页眉强制声明"合成语料、仅验证流程、不代表模型效果"，README:125 同口径。面试话术：数字回答"流程与不变量是否成立"，不回答"业务效果好不好"。
**证据**：`tests/evals/test_agent_cases.py::test_expected_anomalies_derived_from_telemetry_not_from_comments`、`test_dataset_has_60_cases_unique_cid`、`test_exactly_10_fault_injection_cases_of_known_types`；evals/derive_expectations.py。

### Q17. FakeLLM 剧本跑出来的"成功率 100%"能写进简历吗？

**答**：不能当模型效果写，我们也没这么写。FakeLLM/剧本回放的用途是锁定工程流程与不变量（崩溃续跑、审批、超时降级），它验证的是"编排与运行时在既定模型行为下正确"，成功率恒 100% 恰恰说明它是确定性回放。模型效果只认真实模型报告（evals/reports/ 下 e1 系列，deepseek-chat），且当前 E1 报告自带偏差声明：12 条子集、每配置单次、无方差。
**证据**：e1-real-20261024-222238.md"已知偏差"段（单次运行、样本小、不外推）；README:125。

### Q18. E1 说 multi 的工具 P/R 比 single 低，是不是你的多智能体更差？

**答**：先交底——这个数字当前口径有错配，正在修正（X 的集成请求在案）。逐用例核实结论：multi 的 inspector 拿到的设备简报已内联遥测摘要（B6 设计），它不调 get_telemetry 是**设计行为**而非漏调，但期望集把 get_telemetry 记为"必调"，12/12 判漏；dispatch_drone 期望按"批准"写，而 E1 默认 approver 拒绝——口径错配。修正方案：期望区分"必调"与"信息获取方式不限"（以结论/证据判定），dispatch_drone 默认拒绝口径下期望"不调用"；重跑报告双口径并列（process-strict 与 goal-oriented），量化口径影响、不隐藏差异。
**证据**：docs/integration-requests/X.md:11-16（主线程下行修正方案）；`tests/evals/test_agent_eval.py::test_grade_case_metrics_hand_computed`（指标手工可验）。**待确认**：双口径重跑后的数字，等 X 的修正版 e1 报告。

### Q19. 12 条样本、单次运行，你敢下结论吗？

**答**：不敢，报告里也没下。e1 报告"已知偏差"明写：单次、12 条、差异在噪声范围不可外推；计划 §11 要求真实模型每配置 ≥3 次取均值与范围，未执行完毕前结论一律按"趋势参考"处理。面试里这题反而能加分：知道样本量-方差-结论强度的关系，比硬给数字可信。
**证据**：e1-real-20261024-222238.md（"运行次数：每配置 1 次（无方差数据，样本量小）"）；`tests/evals/test_report.py::test_std_population_semantics`（方差语义有测试）。

### Q20. 质量门怎么防止"指标难看但 CI 绿"？

**答**：gate 是独立退出码程序：阈值表检查 + 越权调用直接失败 + 主函数非零退出（CI 红）。指标本身 Recall@k/MRR/P-R 全部有手工算例的单元测试钉住口径，避免指标实现自欺。
**证据**：`tests/evals/test_gate.py::test_gate_main_exit_codes`、`test_agent_forbidden_calls_fail`；`tests/evals/test_retrieval_eval.py::test_recall_at_k_hand_computed`、`test_mrr_hand_computed`。

### Q21. 评测集会不会被"调参后又用来报告"，数据泄露？

**答**：这是登记在对抗审查清单里的问题，当前缓解分两层。结构上：期望从遥测派生而非人工标注（Q16），"调参"空间主要是检索管道与提示词——检索侧有独立标注集与消融报告（retrieval/ablation 系列），agent 侧的提示词迭代没有严格开发/测试集切分。**待确认**：agent 评测集的开发集/测试集切分未做，严格答案是"泄露风险未完全消除，现状如实承认"；要补的是 prompt 迭代历史与评测集的隔离记录。
**证据**：计划 §10 检索与评测节（数据泄露项）；`tests/evals/test_retrieval_eval.py::test_run_retrieval_eval_over_corpus`（标注集独立存在）。

---

## D. 检索工程

### Q22. 中文 BM25 分词怎么做的？为什么不用 jieba？

**答**：默认字符 bigram（CJK 连续段两两切分，ASCII 段整词），零依赖、确定性。jieba 做了对照实验：在本标注集上 jieba 最优变体 Recall@5=0.892，低于 bigram 最优 0.908 约 1.6pp（按 C 的离线报告口径，hybrid-unigram 0.942 最优）——本集查询措辞与正文高度字面对齐，字符级匹配占优；jieba 的优势场景（口语化改写）本集没有。结论：不引依赖，报告同时写明"真实业务查询需复评"。
**证据**：app/knowledge/tokenize.py:1-9（三种模式）；evals/reports/retrieval-20261024-155402.md（分词对比与限定语）；U4 风险闭环。

### Q23. BM25 分数和余弦相似度怎么融合？k=60 哪来的？

**答**：RRF 倒数排名融合：`score = Σ 1/(k + rank)`，只依赖名次不依赖分数量纲——BM25 无界分数与 [0,1] 余弦不可直接加权，RRF 是标准做法。k=60 是信息检索文献的常用默认值（TREC 融合经验值），作用是压制头部名次的权重陡度。同分按 chunk_id 字典序稳定排序，保证评测可复现。
**证据**：app/knowledge/fuse.py:1-31（公式与 docstring）；retriever.py:32-42（双路各召回 20，融合后取前 k）。

### Q24. 你的 embedding 是什么模型？min_sim=0.05 怎么定的？

**答**：离线用自研 HashingEmbedder：token 级特征哈希（MD5 派生桶与符号）+ L2 归一化，512 维——无模型、无随机源、逐位可复现，这是 CI 与离线评测的唯一嵌入实现；真实评测可切 OpenAI 兼容端点（环境变量注入，仓库不持密钥）。min_sim=0.05 的依据：合成语料约 170 chunk、单查询 token < 60，哈希桶碰撞导致的无关文本相似度实测 < 0.05，低于此判为不相关——有专门测试。诚实说：这是"哈希嵌入的噪声底"，不是语义阈值；换真实 embedding 后该值要重标。
**证据**：app/knowledge/embedding.py:31-36（docstring 给出实验依据与测试指引）；`tests/knowledge/test_embedding.py`。

### Q25. 检索不相关时返回什么？会不会硬凑 top-k？

**答**：返回空列表，不编造。BM25 零分候选、向量低于 min_sim 的候选不进融合池，两路全空直接 `return []`（retriever.py:36-37）。下游也有对齐防线：inspector 结论里的 evidence_chunk_ids 只保留真实观测到的 chunk，模型引用了没看过的会被剔除并记 error（inspector.py:258-261）——"结论必须引用真实证据"是双向的：检索不造结果，结论不造引用。
**证据**：retriever.py:32-42；`tests/agents/test_inspector.py::test_bogus_chunk_id_stripped`。

---

## E. 工程取舍

### Q26. 为什么自研运行时，不用 Celery/Temporal？

**答**：演示项目的卖点是"讲得清机制"：租约、fencing、CAS、幂等账本用 SQLite 一条 `UPDATE ... WHERE` 就能完整表达，黑盒框架反而讲不出 WHERE 条件为什么那样写。Celery 给任务队列不给 fencing 语义，Temporal 给的是整套编程模型但把面试题变成"你会不会用 Temporal"。ADR-0006 记录了对比；接口（RunStore/ToolLedger Protocol）隔离实现，将来换 Postgres 替换方言即可。
**证据**：ADR-0006"理由"；contracts/fakes.py 参考实现 + 同一套契约测试（`tests/test_contracts_v1.py`）。

### Q27. SQLite 撑得住吗？什么时候必须换 Postgres？

**答**：单机演示定位，撑不撑得住取决于多进程写负载——这题接 Q8，如实说没压测。必须换的信号：多 worker 多机部署、写吞吐让 BUSY 重试频繁耗尽、需要行级锁与在线 DDL。架构上换了只动 store.py 一个文件（SQL 方言 + 锁语义 + 时间函数），契约测试同一套（InMemoryRunStore/SqliteRunStore/未来 PostgresRunStore 过同一语义测试）。WAL + busy_timeout + 单写者假设是当前的边界声明。
**证据**：ADR-0006 决定 3/后果；store.py:15-16 docstring（并发模型声明）。

### Q28. kill -9 之后到底多少秒任务会被接管？这个数字怎么推？

**答**：上界 = 租约时长 + claim 轮询间隔。默认租约 15s（worker.py:36），worker 空转轮询 0.2s（WorkerConfig.poll_interval_s），所以接管延迟 ≤ 15.2s 左右——前提是有 worker 在池里活着。kill -9 测试里用 2s 短租约加速，断言 5s 内回收器扫描到过期、新 worker 接管 attempt=2 并完成。注意：回收器（Reclaimer）只检测上报不接管，接管是 claim 的惰性行为——这也是可讲的取舍：少一个主动迁移路径，少一类状态机竞态。
**证据**：`tests/runtime/test_kill9_reclaim.py::test_kill9_then_reclaimed_within_lease`（2s 租约实测）；reclaimer.py:1-8 docstring（职责边界）；worker.py:36-38。

---

## F. 压迫题（"你不知道的"）

### Q29. 说说你自己修过的最隐蔽的 bug。

**答**：SSE 生成器终态竞态（c6eceed）。现象：仓库迁移后一个 E2E 测试 3/3 稳定红——SSE 流停在审批事件重放处断流，而数据库里 run 实际已 completed。根因：finish 把终态事件与终态状态放同一事务落库，但消费端 gen 的循环里"读事件批次"与"读 run 状态"是两次独立读——批次读发生在状态可见前、状态读发现终态直接 return，最后一批事件（approval.resolved、run.completed）永远推不出去。修法：检测到终态后补排空一次再退出；同事务保证补读必含终态事件。教训：**"同事务写入"保证的是存储侧一致，消费侧的两次读之间照样有窗口**——一致性是读出来的，不是写出来的。
**证据**：app/main.py:340-346（补排空修补与注释）；`tests/test_runs_api.py::test_approval_flow_with_last_event_id_resume`；提交 c6eceed（迁移前该时序只是概率性，迁移"拨动了时序"把它变成确定性——也说明这类竞态测试一次绿不代表没有）。

### Q30. 这个系统最脆的地方在哪？给我三个你验证不了的。

**答**（主动交底清单，按脆度排序）：
1. **WAL 多进程并发写容量**（U3）：机制有（BUSY 退避重试 + busy_timeout），容量无数据——多大并发下重试耗尽、锁等待对 p99 的影响，没有压测脚本与数字。**待确认：需要多进程压测。**
2. **编排层续跑只覆盖"中断挂起"形态**（README:125 明示）：审批等待期重启→检查点恢复有测试钉死；但"图执行到一半无 interrupt 的崩溃后重入"——LangGraph 检查点可恢复部分 super-step，但 inspector 半途的工具账本状态与图恢复的组合语义没有专项测试。当前真实断言覆盖：store 层进程重启（kill -9）、编排层中断挂起恢复、E2E 刷新与断线——三层各管一段，中间形态是缝。
3. **事件循环阻塞下的心跳失联**（计划 §10 清单遗留）：心跳协程与业务共享事件循环，同步 CPU 密集会卡心跳导致租约过期被接管（正确性由 fencing 保住，损失的是进度）。缓解只有"SQLite 调用全走 to_thread + 心跳间隔租约/3"，没有事件循环卡死的故障注入测试。**待确认：需要 loop 阻塞注入测试。**
另两个可主动说的：Token 计量部分依赖估算公式（budget.py:20-30，FakeLLM 无 usage_metadata 时按字符估算，非计费口径）；审批中心是进程内 Future（main.py:53-76），重启后未消费的决定丢弃、由检查点恢复路径重新等待——语义正确但"等待中的前端"要靠自己重连。
**证据**：README:125（分层覆盖口径）；ADR-0006 后果；worker.py:184-185（failed 兜底）；main.py:53-76。

---

## 使用建议

- 每节答案先背"一句话版本"（加粗首句），展开细节按追问给。
- 所有"待确认"共 5 处（Q8/Q18/Q21/Q30×2）——面试里主动交底其中 1-2 个比全藏着好；被问到再如实补其余。
- 数字类引用（检索 Recall、E1 对比）一律带"离线合成语料口径，报告在 evals/reports/"后缀，防止被当成生产指标追问。
