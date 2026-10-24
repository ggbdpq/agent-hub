# ADR-0006：巡检运行时采用 SQLite（WAL）实现租约 + fencing token + CAS

- 状态：已接受
- 日期：2026-10-24
- 相关：ADR-0001（技术栈）、ADR-0002（Python 依赖）、计划 §4.3 / §5.2

## 背景

巡检多智能体需要持久化运行时：任何时刻 `kill -9` 后任务被正确接管、过期执行者写入被拒、
副作用不重复发生。现有 `tasks.py` 只有进程内字典 + JSON 快照（WP6 修复后具备单进程续跑），
不满足多 worker 与崩溃一致性要求。

## 决定

1. 存储：**SQLite（WAL 模式 + busy_timeout）**。三张表：
   - `runs`：状态机 + 租约字段（`lease_owner` / `lease_expires_at` / `fencing_token` / `attempt` / `cancel_requested`）+ `idempotency_key UNIQUE`；
   - `run_events`：`(run_id, seq)` 主键，事务内 `MAX(seq)+1` 分配；
   - `tool_ledger`：写工具幂等账本（key 主键、`status` 含 `unknown`）。
2. 接口：`apps/api/app/contracts/runtime.py` 的 `RunStore` / `ToolLedger` Protocol（contracts-v1 冻结）；
   `fakes.InMemoryRunStore` 是语义参考实现，SQLite 版必须通过同一套契约测试
   （`tests/test_contracts_v1.py` 语义移植）。
3. 放弃 Postgres + Redis：单机演示场景，租约与 CAS 用一条 `UPDATE ... WHERE` 即可表达，
   原理与分布式版本一致；换 Postgres 只需替换 SQL 方言与锁语义（接口已隔离）。
4. 并发模型：单进程多协程 worker 为主；多进程仅用于崩溃注入测试（U3 风险控制）。

## 理由（vs 备选）

- 自研轻量 vs Celery/Temporal：JD 考的是机制本身（租约/fencing/CAS），黑盒框架讲不清。
- SQLite vs JSON 快照：快照无原子 CAS、无并发认领、无事件流。
- 时间语义：租约过期比较用数据库时间函数（对抗审查清单第 1 条），`ts` 统一 aware UTC。

## 后果

- 契约测试 + 故障注入测试（SIGKILL）是验收硬门槛。
- WAL 下并发写可能出现 `database is locked`：`busy_timeout` + 单写者假设缓解，压测验证（U3）。
