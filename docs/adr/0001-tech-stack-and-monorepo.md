# ADR-0001 技术选型与 monorepo 形态

- 状态：已采纳（2026-09-14）

## 背景

个人智能体演示项目：把智能体应用的四条工程主线（流式事件管线 / A2UI 结构化渲染 / 证据溯源 / 异步任务恢复）在最新主流栈上做最小可信实现，每个依赖的引入都要能说清理由。

## 决策

1. **版本锚定已验证组合**（React 19.2 + TS ~7.0 + Vite 8.2 + Tailwind 4.3 + zod 4.5 + react-router 8.3 + zustand 5.0），全链路验证过 `tsc -b && vite build` 再动工。不追最新小版本：演示项目的主风险是工具链组合未经验证，不是版本不够新。
2. **react-router 用 library mode**：智能体应用无 SEO/SSR 需求，framework mode 引入 node 运行时复杂度，与目标无关。
3. **monorepo 三成员**：apps/web、apps/api、packages/shared。shared 以 TS 源码直出（exports → src/index.ts），web 通过 tsconfig paths + vite alias 消费，不做预编译——少一套构建产物同步问题；代价是 shared 无法独立发 npm 包（当前无此需求）。
4. **请求层用原生 fetch 封装**：SSE 需要 ReadableStream + AbortController + POST JSON body，EventSource 三者皆不支持；普通请求与 SSE 共用同一套 fetch 基建。
5. **不引测试框架**：shared 用 node:test（TS 原生 type stripping），api 用 pytest，web 以 `tsc -b && vite build` 为门槛——符合"最少的可运行检查"原则。

## 后果

- 正面：全链路构建/测试 < 25s；每个依赖为什么在、为什么不在，都可讲述。
- 负面：shared 无独立类型检查入口（随 web 的 tsc -b 一起查）；将来要发包需补构建链。
