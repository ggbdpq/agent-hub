# 深读：巡检多智能体的五个核心机制（Z1，面试深挖版）

> 口径说明：本文所有「文件:行号」基于 worktree `ah-Z` 分支 `docs/deep-dive`、基线提交 `ebf93a3` 的实际代码（`apps/api/` 下）；所有测试名为 `pytest` 真实存在的用例，位于 `apps/api/tests/`。每节末尾附 3 分钟口述稿（先结论后展开，含一个反例/边界），面试现场可直接讲。

---

## ① 认领 SQL 与 fencing token + CAS：过期执行者的写入为什么一律被拒

### 一句话结论

认领在 `BEGIN IMMEDIATE` 写事务内完成"选候选 + 抢占 + 发事件"，并把 `fencing_token` 单调 +1；此后一切写入（事件、终态）都带 `WHERE status='running' AND fencing_token=?` 的 CAS 条件——旧执行者被接管后 token 必然失效，写入必然不命中，物理上写不进来。

### 代码地图（apps/api/app/runtime/store.py）

| 位置 | 内容 |
|---|---|
| store.py:194 | `claim` 进入 `with self._lock, self._tx() as conn`——线程锁 + `BEGIN IMMEDIATE` 写事务（store.py:453-465，进入即取写锁） |
| store.py:197-202 | 候选 SELECT：`status='queued' OR (status='running' AND lease_expires_at <= now)`，按 `created_at, rowid` FIFO |
| store.py:203 | `can_transition` 状态机守卫（state_machine.py:25-38，终态无出边），与 WHERE 条件双保险 |
| store.py:206-207 | `new_token = fencing_token + 1`、`new_attempt = attempt + 1`——接管即单调递增 |
| store.py:208-213 | 抢占 UPDATE：置 running / 新 token / 新租约，`version+1` |
| store.py:214-221 | 同事务写 `run.started` 或 `run.resumed` 事件 |
| store.py:281-287 | `append_event` 事务内读 `status, fencing_token`：`status != 'running'` 或 token 不匹配 → `raise LeaseLost` |
| store.py:288-293 | `seq = MAX(seq)+1`（store.py:475-480）与事件插入同一事务——无空洞无重复 |
| store.py:319-331 | `finish` 的 CAS：`UPDATE ... WHERE run_id=? AND status='running' AND fencing_token=?`，`rowcount != 1` 即被拒（store.py:332-333 返回 False） |
| store.py:337-341 | 终态事件与状态变更**同事务**落盘——崩溃不会出现"状态已终态但没有终态事件" |
| store.py:467-473 | "现在"取 `unixepoch('subsec')`（数据库时钟，多进程一致）；测试注入可控 clock |

### 机制拆解（推理链）

1. **并发认领为何单一胜者**：两个 worker 同时 `claim`，都要先过 `BEGIN IMMEDIATE`（store.py:456）。SQLite 写锁串行化：后到者在 busy_timeout 内等前者的 COMMIT，之后它的候选子查询在锁内重评，看到的是前者已抢占后的库状态——过期 running 已变成有效租约的 running，不再命中候选条件。即使锁等待耗尽穿透到 Python 层，还有 `_retry_on_locked` 指数退避（store.py:110-152）与进程外唯一性的另一层防线：`fakes` 参考实现同语义。
2. **旧 token 为什么必然失效**：接管只能经 `claim`，`claim` 必然 `token+1`（store.py:206）。此后旧执行者带旧 token 的 `append_event` 在 store.py:286 被 `LeaseLost` 拒绝，`finish` 的 UPDATE 影响行数为 0 返回 False。没有任何"跳过 claim 获得执行权"的路径。
3. **fencing 的精确语义（ADR-0008 第 5 条，store.py:4-7 docstring）**：CAS 校验的是"token 是**当前值**且 status=running"，**不校验租约是否过期**。这是有意设计：租约刚好过期但还没人接管时，旧 worker 的合法完成写入（比如马上要 finish）不误拒；"被顶替"由 token 变化拒绝，"自己过期"由心跳失败 + 编排层主动停止承担（见机制②）。两条防线分工，不重叠也不留缝。
4. **终态吸收**：`TERMINAL_STATUSES` 无出边（state_machine.py:23-33），`finish` 的 WHERE 要求 `status='running'`，终态后任何写入（含同 token 的）一律被拒且不可逆。

### 测试作证

- `tests/runtime/test_claim_lease.py::test_concurrent_claim_single_winner_across_instances`（两实例并发认领，仅一者胜出）
- `tests/runtime/test_cas_writes.py::test_stale_writer_rejected_after_takeover`（w1 暂停→w2 接管→w1 的 append_event 抛 LeaseLost、finish 返回 False，最终结果是 w2 的）
- `tests/runtime/test_cas_writes.py::test_expired_lease_without_takeover_can_finish`（过期未被接管：合法 finish 不误拒——fencing 精确语义）
- `tests/runtime/test_cas_writes.py::test_terminal_absorbs_all_writes`（终态后认领/写入全拒）
- `tests/runtime/test_kill9_reclaim.py::test_kill9_then_reclaimed_within_lease`（真实子进程 SIGKILL + 真实数据库时钟，接管后 attempt=2/token=2 并完成）

### 3 分钟口述稿

> 我们要保证的不变量是：任何时刻一个 run 只有一个有效执行者，过期执行者的写入一律无效。做法是 SQLite 里一条租约记录加一个单调递增的 fencing token。
>
> 认领是一个写事务：`BEGIN IMMEDIATE` 里 SELECT 出候选——要么 queued，要么 running 但租约已过期——然后原子地把状态置 running、token+1、attempt+1、写新租约。因为 BEGIN IMMEDIATE 进入即拿写锁，两个 worker 并发认领时后到者的子查询是在前者提交之后重评的，所以天然单一胜者，不需要应用层锁。
>
> token 是所有权凭据。接管必然经 claim，claim 必然 token+1，所以旧执行者被顶替后，它带的旧 token 在 append_event 的校验里对不上，抛 LeaseLost；finish 是一条 `UPDATE ... WHERE status='running' AND fencing_token=?`，影响行数为 0 就返回 False——迟到写入物理上写不进结果字段。终态事件和状态变更放同一事务，崩溃不会出现"状态是终态但没有终态事件"的半吊子。
>
> 这里有个容易被追问的边界：fencing 校验的是"token 是当前值"，**不是**"租约没过期"。反例是租约刚过期但还没人接管的窗口——这时 token 没变，旧 worker 马上要写完成结果，如果我们按"租约过期就拒"，会把一次合法完成误拒掉，任务反而要多跑一遍。所以我们把两类问题拆开：被顶替由 token 拒绝，自己过期由心跳失败、编排层主动停下来承担（下一个问题展开）。另外时钟统一用 SQLite 的 `unixepoch`，避免两台机器时钟漂移把租约算错；kill -9 子进程故障注入测试用真实时钟验证了这条链路。

---

## ② 心跳失败后，编排层怎么停下来

### 一句话结论

心跳是一个 lease/3 间隔的后台协程，续租是一条带 token 和租约时间条件的 CAS；一旦失败就置 `lost` 事件，`RunContext.cancelled()` 读到它，编排层在工具调用前、ReAct 每步前、节点入口停下；就算编排层不听话硬写终态，CAS 也会在被接管后拒绝它。

### 代码地图

| 位置 | 内容 |
|---|---|
| heartbeat.py:59-68 | `_beat_forever`：sleep(interval) → `to_thread(store.heartbeat(...))` → 返回 False 则 `lost.set()` 并退出 |
| heartbeat.py:39 | 间隔默认 `lease_s / 3`（租约 15s → 心跳 5s；worker.py:36） |
| store.py:238-243 | 续租 CAS：`WHERE run_id=? AND status='running' AND fencing_token=? AND lease_expires_at > now`——**过期后自己也续不回来**，即使无人接管 |
| worker.py:146-147 | `_execute` 启动 HeartbeatLoop，`lost` 事件注入 |
| worker.py:151-153 | `def cancelled(): return lost.is_set() or cancel.requested`——心跳失联与外部取消共用一个传导通道 |
| worker.py:163-170 | `cancelled` 与 emit/ledger/budget 一起组装进 `RunContext`（B 只经 ctx 使用运行时，不 import app.runtime） |
| inspector.py:112-114 | ReAct 循环每步开头检查 `ctx.cancelled()` |
| executor.py:89-91 | 每次工具调用前检查取消（第 4 步，权限/预算之后） |
| graph.py:273 | reporter 起草工单前检查 `ctx.cancelled()`；planner.py:83-85 每次尝试前同理 |
| worker.py:178-179 | 终态映射：runner 返回 partial 且 `cancelled()` → 终态 `cancelled`（因中断而未跑完），自然降级才是 partial |
| worker.py:191-211 | finish CAS 被拒（被顶替）→ `run.finish_rejected` 警告日志，写入作废，任务由接管者收尾——"任务最终结束"不依赖本 worker |
| reclaimer.py:38-52 | 回收器只检测上报（同一 `(run_id, attempt)` 只报一次），不改状态；真正接管由 claim 惰性完成 |

### 机制拆解

1. **心跳失败的三种含义**：store.py:238-243 的 WHERE 覆盖三种失联——被顶替（token 变了）、已终态（status 非 running）、租约已过期（`lease_expires_at > now` 不成立）。特别注意第三条：**一旦过期，哪怕无人接管，自己也续不回来**——否则会出现"过期瞬间 claim 被别人拿走、旧 worker 又把租约续回去"的窗口。这一条把"过期后自我复活"彻底关死。
2. **停下的方式是协作式不是强杀**：lost 置位后不抛异常、不 cancel task，编排层在既有检查点（步边界、工具前、节点入口）读 `ctx.cancelled()` 自然收尾，能产出 partial 报告（含 notes "run 被请求取消，提前结束"，graph.py:341-343）。这与外部取消走完全相同的传导路径——`request_cancel` 只置标记不改状态（store.py:344-360），终态由持有租约的 worker 写。
3. **不听话也有兜底**：编排层若忽略 cancelled 硬跑完并 finish，被接管后 finish 的 CAS 返回 False（机制①），写入作废；metrics 里记 `lease_lost`（worker.py:194-199）。正确性最终由存储层 CAS 保证，编排层配合只是为了少做无用功、尽快让位。

### 测试作证

- `tests/runtime/test_heartbeat_reclaimer.py::test_heartbeat_loop_signals_lost_on_expiry`（租约过期后下一次心跳失败 → lost 置位）
- `tests/runtime/test_heartbeat_reclaimer.py::test_heartbeat_loop_interval_is_lease_over_three`（间隔 = 租约/3）
- `tests/runtime/test_claim_lease.py::test_heartbeat_fails_after_expiry`（store 层：过期后续租返回 False）
- `tests/agents/test_inspector.py::test_cancelled_subtask_fails_fast`（inspector 步边界快速失败）
- `tests/tools/test_executor.py::test_cancelled_context_denies`（工具调用前被拒）
- `tests/runtime/test_worker_resume.py::test_worker_maps_cancel_requested_partial_to_cancelled`（cancelled 通道贯通到终态映射）

### 3 分钟口述稿

> fencing 拒绝的是"被顶替之后"的写入，但还有一种情况：worker 自己租约过期了、还没被接管，它还在傻跑。这就要靠心跳主动认输。
>
> 心跳是每个 run 一个后台协程，间隔是租约的三分之一，默认租约 15 秒、心跳 5 秒。每次续租是一条 CAS：状态得是 running、token 得是当前值、**租约还得没过期**。最后这个条件很关键——租约一旦过期，就算没人接管，自己也续不回来了，否则会出现"过期瞬间被别人认领、旧 worker 又续回去"的竞争窗口。
>
> 续租失败就置一个 lost 事件。worker 把它和外部取消标记合并成一个函数 `cancelled()`，塞进 RunContext 传给编排层。编排层在三个粒度上检查：ReAct 每步开头、每次工具调用前（执行器第 4 道闸）、节点入口和起草工单前。这样停下来是协作式的，还能顺手产出一份带"提前结束"说明的 partial 报告，终态映射成 cancelled。
>
> 追问会有两个。第一，如果编排层不检查会怎样？答案是不影响正确性：它硬跑完去写终态，被接管后 CAS 拒绝，写入作废，任务由接管者收尾——存储层兜底，编排层配合只是为了省算力和尽快让位。第二，如果事件循环被同步计算卡住呢？心跳协程本身也跑不动，租约会过期、任务被接管，我们损失的是进度不是正确性；缓解是所有 SQLite 调用都走 to_thread、心跳间隔留了三倍余量。这是我在对抗审查清单里自己列出来的已知边界，诚实说没做事件循环卡死的专项压测。

---

## ③ 续跑时，账本键为什么可复现

### 一句话结论

写工具的账本键由**图结构常量 + run 级不变量 + 检查点状态**构成（`run_id:reporter:wo:<device_id>`），刻意不含 attempt、token、时间戳这类续跑会变的量——所以不管进程重启多少次、LLM 重新起草出什么标题，同一台设备的工单永远落在同一个键上，begin 幂等，副作用只执行一次。

### 代码地图

| 位置 | 内容 |
|---|---|
| store.py:81-93 | `tool_ledger` 表，`key TEXT PRIMARY KEY`；注释明确键格式 `run_id:node:step:tool_call_id`（编排层生成） |
| graph.py:207 | 工单键：`ledger_key=f"{run_id}:reporter:wo:{order.device_id}"` |
| graph.py:232 | 派机键：`ledger_key=f"{run_id}:reporter:drone:{f.device_id}"` |
| graph.py:195 | run_id 取自 `config["configurable"]["thread_id"]`——与检查点 thread 同源 |
| store.py:546-559 | `begin` 同键返回**原记录**（含崩溃遗留的 in_progress 与 completed 的结果） |
| executor.py:117-134 | begin 后三分支：completed → `deduplicated` 直接复用结果不执行 handler；unknown 或"本实例已 begin 未收尾" → 拒绝（状态未知禁止自动重试）；failed / 新键 in_progress → 执行 |
| executor.py:197-200 | 副作用工具超时/异常 → `fail(retryable=True)` → 账本记 **unknown**（可能已执行），store.py:571-580 |
| budget.py:44-47 | `args_hash`：入参规范化 JSON 的 sha256 前 16 位（账本附加指纹，非键的一部分） |

### 机制拆解（键的每个成分为什么不变）

1. **run_id**：续跑经 claim（store.py:208-213）只改 attempt/token/lease，run_id 终身不变——检查点 thread_id 也是它（graph.py:195、runner.py:81），两个持久化域用同一身份。
2. **node + 语义段**：`reporter` / `wo` / `drone` 是图结构常量；`<device_id>` 来自 findings——**findings 是已提交到检查点的状态**（InspectionState，graph.py:55-69），不是续跑后 LLM 现编的。审批中断恢复时 reporter 节点会从头重跑（LangGraph 节点级重放语义），重新起草工单，但只要 findings 里还是 INV-005，键就还是 `...:wo:INV-005`，与第一次执行相同。
3. **刻意排除的量**：不含 attempt（否则续跑 attempt+1 键就变，等于没有幂等）、不含 fencing_token（同理）、不含 LLM 输出内容（标题变了键不变，靠 args_hash 单独留指纹可审计）。
4. **与"对抗审查清单"追问的对答**：续跑后 LLM 给出不同计划怎么办？计划只决定 inspector 查哪些设备；写工具只发生在 reporter，以检查点里的 findings 寻址——键的锚是"已提交到检查点的状态"，不是模型当轮输出。

### 测试作证

- `tests/agents/test_approval.py::test_reporter_rerun_does_not_duplicate_work_orders`（interrupt 恢复重跑 reporter：`tool.called` 1 次 + `tool.deduplicated` 1 次，handler 只执行一次）
- `tests/agents/test_runner_resume.py::test_reclaimed_run_resumes_from_checkpoint_not_rerun`（跨进程重启恢复，工单仍只执行一次：created==1 且 dedup==1）
- `tests/runtime/test_tool_ledger.py::test_write_tool_not_executed_twice_after_crash`（begin 后 kill -9 → 新进程同键 begin 得原记录 → 状态未知不盲目重跑 → 人工确认 complete → 永远复用）
- `tests/tools/test_executor.py::test_timeout_write_tool_marks_ledger_unknown`（副作用工具超时 → unknown，禁止自动重试）
- `tests/runtime/test_tool_ledger.py::test_fail_retryable_marks_unknown`

### 3 分钟口述稿

> 不变量是：写工具不因重试或续跑执行两次。靠的是幂等账本，而幂等的前提是**键可复现**——同一个逻辑动作，两次执行必须算出同一个键。
>
> 我们的键是 `run_id:reporter:wo:<device_id>`。逐项看它为什么跨 attempt、跨进程不变：run_id 终身不变，续跑只加 attempt 和 token；reporter 和 wo 是图结构常量；device_id 来自 findings，而 findings 是已经提交到检查点的状态，不是续跑后 LLM 新编的。反过来，attempt、token、时间戳、LLM 起草的标题，这些会变的量一个都不进键——这是设计时刻意排除的。标题虽然不进键，但入参会算一个 sha256 指纹存在账本行里，两次执行参数是否一致可以事后审计。
>
> 有了可复现键，账本语义是：begin 对已存在的键返回原记录。第一次执行完是 completed，恢复重跑时 begin 拿到 completed，直接把结果复用给调用方，handler 不再执行，事件流上记一条 deduplicated。如果第一次是崩在半中间，账本是 in_progress 或 unknown——unknown 意味着副作用可能已经发生，我们禁止自动重试，留给人工确认后再落结果。
>
> 反例就用"键里含 attempt 会怎样"：续跑后 attempt 从 1 变 2，键变了，账本当成新调用，工单就重复创建了——这正是很多"重试加个随机后缀"式幂等键的通病。还有一个实际踩过的点：审批中断恢复时 reporter 是整节点重跑的，起草工单的代码会再走一遍，第一次测试时就靠账本把这次重复吸收掉了，`tool.called` 1 次、`deduplicated` 1 次，断言写死在回归里。

> **版本行为变化（2026-10-25，第 6 轮 P2）**：上文"reporter 对 warning/critical 起草工单"不再是无条件语义——`request.goal` 为「只诊断」（带"只出结论/不开工单/禁止创建工单"类显式信号，且无肯定式"生成工单/处置/派工"动作词，判定见 `graph.py` 的 `diagnosis_only_goal`）时 reporter 跳过起草，notes 记"按 goal 只诊断，未起草工单"；无显式信号或处置类 goal 行为完全不变。口径与评测侧一致（只诊断 → `create_work_order` 属 forbidden，见 docs/integration-requests/Q.md），幂等账本键机制本身不受影响。

---

## ④ 审批等待期重启，如何从检查点恢复（含整图重跑缺陷的修复背景）

### 一句话结论

runner 每次进入先 `aget_state` 查这个 thread 是否停在 pending interrupt：是 → 走 approver 决策后 `Command(resume)` 恢复，绝不带输入重启；否 → 才正常带输入 ainvoke。修复前 runner 无条件带输入 invoke，图会从 START 整图重跑、findings 翻倍、coverage 到 2.0 还标 completed——这条缺陷由真实回归测试钉死。

### 代码地图

| 位置 | 内容 |
|---|---|
| runner.py:81 | `config = {"configurable": {"thread_id": run_id}}`——检查点身份 = run 身份 |
| runner.py:84-92 | **R1 修复主体**：`pending = await _pending_interrupt_payload(...)`；非 None → `self._approver(pending)` 决策 → `await graph.ainvoke(Command(resume=decision), config)`；None → `await graph.ainvoke({"request": ...}, config)` |
| runner.py:123-133 | `_pending_interrupt_payload`：`state.next` 非空且某 task 带 interrupts → 返回 payload（供 approver 决策） |
| runner.py:95-105 | 进程内 interrupt 循环：本轮新中断逐次 `Command(resume)`；`guard > 8` 防异常死循环（runner.py:98-99） |
| runner.py:88-89 | 重启后审批由**新进程的 approver** 一次性消费（注释明确） |
| graph.py:224 | 唯一 interrupt 点在 reporter（单中断点设计，ADR-0007 决定 4） |
| graph.py:35-47 | **R1 加固**：findings/failures 的 reducer 换成 `_merge_by_device`——按 device_id 去重、后写覆盖；即使回归到"带输入重启"，coverage 也永不 >1 |
| graph.py:207 / 232 | 恢复重跑 reporter 时工单/派机靠账本键去重（见机制③） |
| tests/test_langgraph_probe.py:85 | `test_async_send_interrupt_sqlite_checkpoint`：U1 探针——关连接重开后扇出结果在检查点里、已完成节点不重跑、`Command(resume=...)` 的值即 `interrupt()` 返回值 |

### 机制拆解

1. **职责分层**：运行时（租约/fencing/事件）在 A 的 store，图状态检查点在 B 的 LangGraph SqliteSaver，两者以 run_id 关联、互不 import（ADR-0007 决定 1）。重启后 worker 经 claim 拿到新 attempt，再调 `runner.run(run_id, ...)`；runner 对"这个 run 是新开还是续跑"无感知差异——**由检查点状态决定入口**。
2. **缺陷根因（修复前）**：LangGraph 对"thread 有未完成检查点"的再次 invoke，如果带输入，会按新输入重新驱动图——planner/inspector 全部重跑；findings 当时用追加式 reducer 合并，旧值 + 重跑值 = 翻倍；coverage = 覆盖设备数/总数 >1 仍报 completed。表现为：审批等待期进程重启，报告里同一条异常出现两次。
3. **修复两层**：入口层正确恢复（aget_state → Command(resume)，runner.py:84-92）治根；reducer 层 `_merge_by_device` 按 device_id 去重覆盖（graph.py:35-47）治"未来任何回归"——两层独立生效，coverage 数学上不可能超过 1。
4. **审批只消费一次**：`interrupt()` 恢复后返回的就是 `Command(resume=...)` 携带的值（U1 探针实测），resume 一次即离开中断点；重启场景里由新进程的 approver 一次性消费（runner.py:88-89），不存在旧进程缓存的决定。

### 测试作证

- `tests/agents/test_runner_resume.py::test_reclaimed_run_resumes_from_checkpoint_not_rerun`（生产路径全链路：进程1 停在审批中断 → 进程2 新 saver 连接重认领恢复；断言 planner==1、inspector==3、findings==3、coverage==1.0、工单 created==1+dedup==1——这些断言在缺陷版本下全红）
- `tests/agents/test_checkpoints.py::test_crash_before_approval_resumes_without_rerunning_planner_inspector`（图级同场景；`state.next == ("reporter",)` 验证卡点）
- `tests/agents/test_approval.py::test_resume_approved_dispatches_with_approval_id` / `test_resume_denied_no_drone_dispatch`（审批两分支与 dangerous 凭证传导）
- `tests/agents/test_runner.py::test_default_approver_denies_and_completes`（安全缺省：无审批通道自动拒绝）

### 3 分钟口述稿

> 场景是：巡检跑到人工审批这一步，interrupt 把图挂起，等运维点确认。这时候服务重启了——新进程重认领这个 run，怎么保证不把整个巡检重跑一遍？
>
> 我的答案是让入口有状态感知。runner 每次进来先 aget_state 看这个 thread 的检查点：如果 `state.next` 非空、任务上挂着 interrupt，说明它停在审批点，就走 approver 决策、用 `Command(resume=决定)` 恢复——绝不把原始输入再喂一遍；只有检查点没有挂起中断，才按新输入正常启动。检查点的 thread_id 就是 run_id，和运行时的租约记录用同一个身份，两个持久化域各管各的：store 管"谁有权执行、事件怎么流"，Saver 管"图跑到哪了"。
>
> 这里面有个真实踩过的缺陷。最早 runner 是无条件带输入 invoke 的，LangGraph 在 thread 有未完成检查点时会按新输入重新驱动整图：planner 重新规划、三台设备的 inspector 全部重跑，findings 用的又是追加式合并，重启一次就翻倍，coverage 算出 2.0 还标 completed。修复分两层：入口层改为检测 pending interrupt 后 Command(resume)，治根；状态层把 findings/failures 的 reducer 改成按 device_id 去重、后写覆盖，这样就算未来哪条路径又退回带输入重启，覆盖率数学上也超不过 1。修复同时写了跨进程重启的回归测试，把 planner 只跑一次、inspector 只跑三次、findings 三条、coverage 1.0 全部断言死——缺陷版本下这些断言全红。
>
> 边界也要说清楚：恢复时 reporter 是整节点从头重跑的，重新起草的工单靠幂等账本去重（上一个问题讲的键可复现在这里兜底）；审批决定由新进程的 approver 一次性消费，resume 的值就是 interrupt 的返回值，这个语义是 U1 试验脚本在 CI 里钉住的。以及诚实说，我们只覆盖了"中断挂起"这一种续跑形态——图执行到一半没有 interrupt 的崩溃，图状态可以续但工具账本语义不同，这个形态没做（README 口径里写明了）。

---

## ⑤ Supervisor 超时如何降级为 partial（coverage 分母 = 请求的 device_ids）

### 一句话结论

每个设备子任务被节点级 `asyncio.wait_for(timeout=task_timeout_s)` 罩住，超时/异常都转成一条带原因的 failures 记录进状态，不抛出图；reporter 汇总时只要有 failures 就 partial，coverage 用**请求的 device_ids 做分母**、有 findings 的设备做分子，缩水计划不再虚抬覆盖率。

### 代码地图

| 位置 | 内容 |
|---|---|
| graph.py:142-154 | inspector 节点：`await asyncio.wait_for(run_inspection(...), timeout=task_timeout_s)` |
| graph.py:155-163 | `except TimeoutError` → 发 `node.failed`（含 device 与 `timeout after ...s`）→ `return {"failures": [{device_id, reason}]}` |
| graph.py:164-172 | `except Exception` 同样转 failures——子任务异常不拖垮整体 |
| graph.py:65-66 | findings / failures 用 `_merge_by_device` reducer 合并并行输出（graph.py:35-47） |
| inspector.py:67 | `run_inspection` 自身"永不抛异常"，失败转 failure_reason——超时兜底只有节点层一处 |
| graph.py:312-361 | `_assemble_report` 汇总 |
| graph.py:323-330 | **R2 修复**：`total = len(requested) if requested else len(tasks)`，分母优先请求的 device_ids；`covered = 有 findings 的设备集`，`coverage = len(covered)/total` |
| graph.py:331 | `status = "completed" if not failures else "partial"` |
| graph.py:333-340 | notes 逐台记失败原因；**计划缩水检查**：请求了但没进计划的设备，记入 notes 并强制 partial——缩水不静默 |
| graph.py:341-343 | `ctx.cancelled()` → partial + notes 提前结束 |
| executor.py:146-151 | 工具级第二层超时（`spec.timeout_s`）：与节点级职责不同——节点级保整体时延上界，工具级驱动账本 unknown 语义 |

### 机制拆解

1. **两层超时，职责分离**：节点级 `wait_for` 包住整台设备的 ReAct 循环，保证 supervisor 的时延上界 = 慢的那台 + 汇总；工具级超时在执行器里，决定这次调用算失败还是状态未知（副作用工具超时 → 账本 unknown，禁止自动重试）。只做工具级不做节点级，模型在循环里空转照样拖死整体；只做节点级不做工具级，副作用工具的状态未知语义无从落地。
2. **降级而不是失败**：超时设备进 failures，其余设备照常出 findings，报告 partial、coverage 如实反映（2/3 就是 2/3），notes 里能指名道姓说哪台为什么没查成。运维拿到的是"查了什么、没查到什么、为什么"，而不是一个整体超时异常。
3. **R2 修复背景（分母口径）**：修复前 coverage 分母实际取计划出来的任务数——LLM 缩水规划（请求 3 台只计划 2 台）时，2 台全成功就 coverage=1.0，虚高且掩盖了计划缺陷。修复后分母一律是请求的 device_ids，且缩水设备在 notes 里点名并强制 partial（graph.py:336-340）。single_react 单智能体基线同口径，消融对比不失真。

### 测试作证

- `tests/agents/test_graph.py::test_subtask_timeout_degrades_to_partial_with_coverage`（慢 handler 注入 + task_timeout_s=0.2 → partial、coverage≈2/3、notes 含 INV-005 timeout、`node.failed` 事件可观测）
- `tests/agents/test_graph.py::test_shrunken_plan_coverage_uses_requested_denominator`（缩水计划：coverage≈2/3 不虚抬、status=partial、notes 点名 INV-003 "未列入计划"）
- `tests/agents/test_graph.py::test_full_flow_completed_with_coverage_one`（无失败：completed + coverage 1.0）
- `tests/agents/test_graph.py::test_cancelled_run_yields_partial_report`（取消：partial + coverage 0.0，不抛异常）
- `tests/tools/test_registry.py::test_spec_overrides_support_fault_injection`（故障注入机制本身可测）

### 3 分钟口述稿

> 多设备并行扇出之后最怕一台设备把整个 run 拖死。做法是把"整体成功"和"单台成功"解耦：每台设备的子任务被节点级的 `asyncio.wait_for` 罩住，超时或者抛异常都不往外传，而是转成一条 failures 记录——设备号加原因——写进图状态。所有设备汇合后 reporter 一看 failures 非空，报告就是 partial，notes 里逐台说明哪台超时了为什么。
>
> 覆盖率的口径这里有个真实的坑。最初分母用的是 planner 规划出来的任务数，结果 LLM 偷懒缩水——请求三台只规划两台——两台全查完覆盖率就是 1.0，看起来完美，实际上第三台根本没人管。修复是把分母统一改成请求的 device_ids，跟单智能体基线同一口径；而且凡是请求了但没进计划的设备，直接在 notes 里点名并强制 partial，缩水不允许静默。修复配了专门回归：缩水计划下 coverage 必须是 2/3、状态必须是 partial。
>
> 还有个容易忽略的层次问题：超时其实有两层，节点级和工具级，职责不一样。节点级保的是 supervisor 的整体时延上界；工具级超时在执行器里，它决定的是"这次写工具的副作用到底发生没有"——副作用工具超时，账本记 unknown、禁止自动重试，这和第二个问题讲的幂等链路接上。反例是如果只做工具级超时：每台设备的 ReAct 循环有步数上限没错，但模型在步数内反复调慢工具，节点照样没有时延上界——所以节点级那层 wait_for 不能省。边界是全部超时：coverage 0 + partial，报告仍然产出、事件流完整，这是"降级"和"失败"的区别。

---

## 附：五机制的公共地基（被追问时用）

- **存储**：SQLite WAL + busy_timeout + BEGIN IMMEDIATE（store.py:434-439、453-465），单写者假设；BUSY 有限重试（store.py:110-152，`tests/runtime/test_lock_retry.py`）。单机演示定位，接口（`RunStore`/`ToolLedger` Protocol）已隔离，换 Postgres 替换 SQL 方言与锁语义（ADR-0006）。
- **状态机**：`queued→running→{completed,failed,cancelled,partial}`，终态无出边（state_machine.py:25-33），`running→running` 只发生在过期重认领。
- **事件流**：`(run_id, seq)` 主键、事务内 MAX+1 分配、无空洞；`events_after` 支撑 SSE `Last-Event-ID` 断点续传（main.py:313-349，`tests/test_runs_api.py::test_approval_flow_with_last_event_id_resume`）。
- **验证矩阵**：计划 §9 的 11 条不变量每条有自动化测试；`kill -9` 故障注入（tests/runtime/test_kill9_reclaim.py）与跨进程审批恢复（tests/agents/test_runner_resume.py）是两条最重的证据链。
