# AGENTS.md — Agent 工作约定

在本仓库工作的 AI Agent（Claude Code / Cursor / Codex 等）除遵守 `CLAUDE.md` 外，另遵守：

## 工作边界

1. 只在既有分层内改动：契约（shared/contracts.ts ↔ api/schemas.py）的变更必须同步双镜像与 fixture，禁止只改一端。
2. 新功能先写明动机、验收标准与取舍，再动手。
3. 架构级取舍（依赖引入、目录调整、协议变更）写 ADR 到 `docs/adr/`，四位序号递增。
4. 禁止引入新依赖，除非：标准库/现有依赖/平台能力确实覆盖不了，且在 ADR 中记录对比。
5. 领域术语以 `CONTEXT.md` 为准；新术语先入表再使用。

## 完成定义（DoD）

- `pnpm build && pnpm test:shared && pnpm test:web && pnpm test:api` 全绿。
- 新增分支/解析/状态逻辑必须有单测（node:test 或 pytest），不引测试框架。
- 改动涉及用户故事（README 三条验收路径）时，真机冒烟走一遍并在 PR/提交说明里贴结果。

## 禁改区

- `fixtures/contract-example.json` 的既有字段（只能追加新示例）。
- `packages/shared/src/sse-decoder.ts` 的对外签名（帧协议兼容性承诺）。
- `.venv/`、`dist/`、`node_modules/`。
