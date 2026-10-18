"""证据库：AI 推荐结论与依据分离——结论不自带证据，按 evidenceId 引用。

当前内容与可信度均为固定演示 fixture；前端抽屉按 id 拉取并做分级渲染，未实现可信度计算。
"""
from app.schemas import Evidence, EvidenceSourceKind

EVIDENCE_STORE: dict[str, Evidence] = {
    "ev-purchase-01": Evidence(
        id="ev-purchase-01",
        source="近 90 天订单记录",
        sourceKind=EvidenceSourceKind.PURCHASE_HISTORY,
        credibility=88,
        quote="顾客近 90 天购买 3 次鲜切花，均为玫瑰类，单价偏好 50-80 元档。",
        analyzedAt="2026-09-10 20:15",
    ),
    "ev-survey-02": Evidence(
        id="ev-survey-02",
        source="入会喜好问卷",
        sourceKind=EvidenceSourceKind.PREFERENCE_SURVEY,
        credibility=72,
        quote="问卷勾选'喜欢低香味、浅色系'，但对'香槟色'未表态（样本较旧，可信度降级）。",
        analyzedAt="2026-08-02 11:30",
    ),
    "ev-care-03": Evidence(
        id="ev-care-03",
        source="养护回访记录",
        sourceKind=EvidenceSourceKind.CARE_RECORD,
        credibility=91,
        quote="上次回访顾客反馈'尤加利叶养不活'，本次推荐应避免需高频打理的配叶。",
        analyzedAt="2026-09-12 09:05",
    ),
}


def get_evidence(evidence_id: str) -> Evidence | None:
    return EVIDENCE_STORE.get(evidence_id)
