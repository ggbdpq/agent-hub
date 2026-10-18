# CONTEXT.md — 领域术语表

AI 会话与新人消除歧义的词典。术语按"本仓库的精确含义"定义，与日常口语用法不同时以本表为准。

| 术语                         | 本仓库的精确含义                                                                                                                                                                       |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 花语智能体                   | 面向花卉电商顾客的推荐对话智能体；服务端为 `apps/agent`（langchain.js createAgent）。未配置 Key 时用确定性离线剧本 `ScriptedChatModel`，与真实模型共享同一张图，只差 model 一个参数            |
| A2UI                         | Agent-to-UI：智能体只输出结构化契约数据（FlowerCardPayload），UI 由前端组件按白名单渲染；禁止 LLM 生成 UI 代码                                                                         |
| 依据（Evidence）             | 支撑推荐结论的溯源记录：来源 + 可信度 + 引用原文 + 分析时间。结论与依据分离，卡片按 evidenceId 引用                                                                                    |
| 可信度                       | 当前为服务端固定 fixture 的 0-100 整数，不是实时计算或经算法评测的结果；前端只做分级渲染，**<80 金色预警**是演示的信任阈值                                                             |
| 思考段（thought）            | 智能体推理过程的分段文本；`final=true` 表示该回答的思考结束（前端据此折叠）                                                                                                            |
| token 段                     | 正文增量文本；连续 token 在前端合并为同一段（打字机效果）                                                                                                                              |
| segment                      | 一次回答内有ford有序的内容单元：thought / card / evidence / token；store 中按事件到达顺序排列                                                                                          |
| 帧（SSE frame）              | `event:` + `data:` + 空行；data 为单行完整 JSON。帧解析器按空行派发                                                                                                                    |
| boneyard                     | 骨架屏方案：真实内容保持挂载（`display: contents` 无盒包裹），骨架为 overlay（`pointer-events: none`）；bones fixture 按断点捕获，URL `?boneyard=` 预览                                |
| bones                        | 骨架占位形状清单（相对边界盒的百分比定位矩形/圆形），存于 `*.bones.json`                                                                                                               |
| 边界（boundary）             | 一个 Boneyard 组件实例对应的页面区域，`name` 全局唯一且不含斜杠                                                                                                                        |
| 任务态机                     | 养护方案任务的 queued → running(progress) → completed/failed/cancelled → archived 状态机；进度 0-100 整数                                                                              |
| 恢复锚点                     | taskId：刷新/重进页面以远端详情恢复并续追（localStorage 只存 id）；后端重启会把 queued/running 任务从快照进度续跑                                                                        |
| 支付沙箱                     | 模拟微信/支付宝的收银与异步回调；验签为 SHA-256(orderId+channel+SECRET) 前 16 位；真实接入仅差商户资质与官方 SDK                                                                       |
| 幂等回调                     | 同一订单重复支付回调只入账一次，响应 `duplicate: true`；渠道与订单绑定，跨渠道回调拒绝                                                                                                 |
| 业务单号（taskNo / orderNo） | 有序可读的展示编号：前缀（YH=养护任务 / DD=订单）+ 日期 + 每日三位序号，每日从 001 递增，重启从快照续号。**内部主键（taskId / orderId，随机 hex）不上页面**，仅用于 API 路由与恢复锚点 |
| 契约双镜像                   | contracts.ts（zod）是单一事实源；schemas.py（pydantic）只镜像 Python 仍在用的任务/订单/场景；`fixtures/contract-example.json` 是共同对账样例（事件段只由 TS 校验）                          |
| 场景（scene）                | 推荐场景枚举：daily 日常自用 / gift 送礼 / comfort 探望慰问；决定卡片段式与文案                                                                                                        |
| threadId                     | 会话标识（8-64 字符），前端首次发送时生成、清空时重置；服务端按它存取多轮记忆，不传 = 单轮                                                                                              |
| 本轮（this turn）            | 最后一条 HumanMessage 之后的范围；证据与商品因果校验以"本轮"为界，记忆不放宽                                                                                                            |
| 事件翻译层（translate）      | `apps/agent/src/translate.ts`：createAgent 的 messages+custom 流 → 五事件协议，出站 zod 校验与 error/done 收口都在这里                                                                   |
