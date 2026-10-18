# CLAUDE.md — 项目编码约定

花语智能体（agent-hub）的个人项目约定。AI 会话与新人上手共享这一份上下文。

## 技术栈锚点（升级前先跑 `pnpm build` 全链路验证）

React 19.2 / TypeScript ~7.0 / Vite 8.2 / Tailwind CSS 4.3（CSS-first，无 config 文件）/ react-router 8（library mode，纯 SPA）/ zustand 5 / zod 4 / shadcn new-york(zinc) / FastAPI + pydantic v2 / pnpm 12.4.1。

## 铁律

1. **契约单一事实源**：接口 schema 只写在 `packages/shared/src/contracts.ts`（zod）；python 侧 `apps/api/app/schemas.py` 是镜像。改动必须三处同步：contracts.ts、schemas.py、`fixtures/contract-example.json`，并让 `pnpm test:shared` + `pnpm test:api` 双绿。
2. **AI 输出白名单**：任何来自 SSE 的数据进 UI 前必须过 `AgentStreamEvent.parse`（zod），禁止 `as any` 绕过。
3. **金额一律用分**（`priceFen`/`amountFen`，整数），展示层才 `formatPrice`。
4. **服务端事件帧格式**：`event: <type>\ndata: <JSON>\n\n`——data 必须是单行完整 JSON（前端帧解析器按行缓冲）。
5. **SSE 五事件**：thought / token / card / evidence / done，error 保留。新增事件类型先改契约再改两端。

## 分层约定

- web：`routes.tsx` 只做装配；页面在 `src/features/<域>/`；请求在 `src/services/`（fetch 封装 + zod schema）；跨页面状态才进 `src/stores/`（zustand 分片，一域一 store）。
- 页面不直接 fetch；service 不 import store；store 不操作 DOM。
- boneyard 边界：业务接入只给 `name` + `loading`；bones fixture 放 `src/features/boneyard/*.bones.json`，在 `register-bones.ts` 注册；容器/content/overlay 布局由组件统管，业务不要写 `[data-boneyard-*]` 样式。
- api：路由薄（装配与 HTTP 语义），业务在 `app/agent.py` / `app/tasks.py` / `app/orders.py`；所有响应模型过 pydantic。

## 验证门槛（每次改动后）

```bash
pnpm build && pnpm test:shared && pnpm test:web && pnpm test:api
```

涉及 SSE/任务/支付的改动，另跑真机冒烟（起 `pnpm api:serve` 后 curl，见 README 验收路径）。

## 已知坑

- `cd` 进仓库触发 fnm 钩子报错：node 命令用绝对路径或先 `eval "$(fnm env)"`。
- Windows 控制台 curl 直接 `-d` 中文会 GBK 乱码：body 先写 UTF-8 文件再 `--data-binary @file`。
- TestClient 裸用（不带 `with`）每个请求独立事件循环，`asyncio.create_task` 的后台任务会被冻结——测试用 `with TestClient(app) as client:`。
- **pnpm scripts 在 Windows 上走 cmd.exe**（与用户终端无关）：cmd 不认单引号、`&&` 链和正斜杠路径不可靠——涉及 Python venv 的命令一律走 `scripts/api.mjs`（参数数组 spawn，跨平台），不要往 package.json 里写内联 shell。
- **pydantic 的 None 序列化为 JSON null**，zod 的 `.optional()` 只收 undefined——可空字段一律 `.nullish()`（Order.paidAt、TaskSnapshot.result/failReason 已是）。
- **store 初始化器（zustand create）里禁止 React Hooks**：不是组件，Hooks dispatcher 为 null 会白屏；"单例可变引用"用闭包普通变量。
- LLM 流式输出的 ```card 围栏标记可能被 chunk 劈开：解析必须 hold-back 尾部字符（_StreamStateMachine 已实现），新解析逻辑照此办理。
