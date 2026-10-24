---
name: procedure-lookup
description: 规程检索——从知识库检索相关条款作为结论证据，只引用真实 chunk_id
allowed_tools: search_procedures
output_schema: app.contracts.knowledge.Chunk
---

## 检索守则
- 用异常参数与设备类型构造查询词；
- evidence_chunk_ids 只能引用 search_procedures 返回过的 chunk_id；
- 检索无结果时留空，不编造条款。
