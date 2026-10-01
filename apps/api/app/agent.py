"""mock LLM 花语顾问：确定性剧本驱动的 SSE 生成器。

真实 LLM 接入位：把 script_stream() 换成对 LLM 的流式调用，
事件协议（thought/token/card/evidence/done）不变——前端零改动。
"""
import asyncio
import uuid
from collections.abc import AsyncIterator

from app.evidence import get_evidence
from app.schemas import (
    CardEvent,
    DoneEvent,
    EvidenceEvent,
    RecommendScene,
    ThoughtEvent,
    TokenEvent,
)
from app.sse import sse_frame

# 推荐剧本：按场景给出确定性推荐（离线可跑、输出可断言）
SCENE_SCRIPTS: dict[RecommendScene, dict] = {
    "daily": {
        "title": "本周自用鲜花组合",
        "reason": "结合你的购买节奏与养护习惯，选了低打理、花期长的组合。",
        "items": [
            {"id": "fl-001", "name": "香槟色玫瑰 9 支", "priceFen": 6900, "typeLabel": "花束", "emoji": "🌹"},
            {"id": "fl-002", "name": "洋桔梗小盆栽", "priceFen": 3500, "typeLabel": "盆栽", "emoji": "🪴"},
        ],
        "evidenceRefs": [
            {"evidenceId": "ev-purchase-01", "label": "近 90 天订单记录"},
            {"evidenceId": "ev-care-03", "label": "养护回访记录"},
        ],
        "answer": "根据你的订单记录，玫瑰类是最稳的选择；上次反馈尤加利难养护，这次换成好打理的洋桔梗盆栽，花期更长。",
    },
    "gift": {
        "title": "送礼首选：轻奢礼盒",
        "reason": "送礼场景优先包装质感与寓意，避开你问卷里不喜欢的浓香型。",
        "items": [
            {"id": "fl-101", "name": "香槟乌梅礼盒", "priceFen": 12800, "typeLabel": "花盒", "emoji": "🎁"},
            {"id": "fl-102", "name": "永生花圆舞曲", "priceFen": 19800, "typeLabel": "永生花", "emoji": "✨"},
        ],
        "evidenceRefs": [
            {"evidenceId": "ev-survey-02", "label": "入会喜好问卷"},
        ],
        "answer": "送礼场景我推荐礼盒款：香槟色系符合你浅色系的偏好，永生花款适合希望纪念意义更久的场合。",
    },
    "comfort": {
        "title": "探望慰问：素雅安神",
        "reason": "探望场景避免浓香与艳色，以素雅、好养护为主。",
        "items": [
            {"id": "fl-201", "name": "白绿桔梗花束", "priceFen": 8800, "typeLabel": "花束", "emoji": "🤍"},
            {"id": "fl-202", "name": "小雏菊盆栽", "priceFen": 4200, "typeLabel": "盆栽", "emoji": "🌼"},
        ],
        "evidenceRefs": [
            {"evidenceId": "ev-care-03", "label": "养护回访记录"},
        ],
        "answer": "探望慰问以素雅为宜：白绿桔梗无浓香，小雏菊好打理，收花人不需要费心照顾。",
    },
}

THOUGHTS_BY_SCENE = {
    "daily": [
        "解析请求：顾客在问日常自用的花。",
        "检索画像：近 90 天订单以玫瑰类为主，价格带 50-80 元。",
        "交叉校验：上次养护反馈排除难打理配叶，生成组合方案。",
    ],
    "gift": [
        "解析请求：这是送礼场景。",
        "检索画像：问卷显示偏好浅色、低香型。",
        "权衡：礼盒的包装质感与寓意优先，给出两档价位。",
    ],
    "comfort": [
        "解析请求：探望慰问场景。",
        "约束：避免浓香、艳色、难养护品种。",
        "按约束筛选：白绿系 + 好养护盆栽。",
    ],
}


def detect_scene(message: str) -> RecommendScene:
    """关键词路由（真实场景应交给 LLM 意图识别）。"""
    if any(k in message for k in ("送", "礼", "生日", "表白", "纪念")):
        return RecommendScene.GIFT
    if any(k in message for k in ("探望", "慰问", "看病", "安慰")):
        return RecommendScene.COMFORT
    return RecommendScene.DAILY


def _frame(event: str, payload) -> str:
    return sse_frame(event, payload)


async def script_stream(message: str) -> AsyncIterator[str]:
    """确定性剧本流：思考分段 → 卡片 → 依据 → 正文 → done。"""
    scene = detect_scene(message)
    script = SCENE_SCRIPTS[scene]
    reply_id = uuid.uuid4().hex[:12]

    # 思考分段（前端逐段追加，完成后自动折叠）
    thoughts = THOUGHTS_BY_SCENE[scene.value]
    for i, text in enumerate(thoughts, start=1):
        yield _frame("thought", ThoughtEvent(type="thought", seq=i, text=text, final=False))
        await asyncio.sleep(0.25)
    yield _frame("thought", ThoughtEvent(type="thought", seq=len(thoughts), text="", final=True))

    # A2UI 结构化卡片（白名单契约字段，前端 zod parse 后渲染）
    payload = {**script, "scene": scene.value}
    yield _frame("card", CardEvent(type="card", payload=payload))

    # 依据：按卡片里的引用逐一推送（结论与依据分离）
    for ref in script["evidenceRefs"]:
        evidence = get_evidence(ref["evidenceId"])
        if evidence is not None:
            yield _frame("evidence", EvidenceEvent(type="evidence", evidence=evidence))
            await asyncio.sleep(0.15)

    # 正文增量（打字机）：按 6 字一切
    answer = script["answer"]
    for i in range(0, len(answer), 6):
        yield _frame("token", TokenEvent(type="token", text=answer[i : i + 6]))
        await asyncio.sleep(0.08)

    yield _frame("done", DoneEvent(type="done", replyId=reply_id))
