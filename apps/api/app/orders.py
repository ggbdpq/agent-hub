"""支付沙箱：订单状态机 + 模拟微信/支付宝异步回调。

真实支付只差两样：商户资质、把 mock 适配器换成官方 SDK。
这里保留真实支付的全部工程语义：
- 订单状态机：created → paying → paid / closed（超时关闭）
- 回调验签：sha256(orderId + channel + SECRET)（真实场景为平台公钥验签）
- 幂等：同一订单重复回调只入账一次，duplicate 标记返回
- 乱序/非法状态拒绝：closed 单不再回调成功
"""
import hashlib
import os
import uuid
from datetime import datetime, timezone

from app.schemas import Order, OrderStatus, PaymentChannel

# 演示密钥：仅本地沙箱使用；真实项目从环境变量/密钥服务读取
_PAY_SECRET = os.environ.get("AGENT_HUB_PAY_SECRET", "sandbox-secret-do-not-use-in-prod")

_ORDERS: dict[str, Order] = {}

# 业务单号：DD(订单)+日期+每日序号，每日从 001 递增
_SEQ_DATE = ""
_SEQ_N = 0


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


def _next_order_no() -> str:
    global _SEQ_DATE, _SEQ_N
    today = _today()
    if _SEQ_DATE != today:
        _SEQ_DATE, _SEQ_N = today, 0
    _SEQ_N += 1
    return f"DD{today}{_SEQ_N:03d}"


def _sign(order_id: str, channel: str) -> str:
    raw = f"{order_id}{channel}{_PAY_SECRET}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def expected_sign(order_id: str, channel: str) -> str:
    """暴露给测试与沙箱收银台使用：模拟平台侧生成签名。"""
    return _sign(order_id, channel)


def create_order(flower_name: str, amount_fen: int, channel: PaymentChannel) -> Order:
    order = Order(
        orderId=uuid.uuid4().hex[:12],
        orderNo=_next_order_no(),
        flowerName=flower_name,
        amountFen=amount_fen,
        channel=channel,
        status=OrderStatus.CREATED,
    )
    _ORDERS[order.orderId] = order
    return order


def get_order(order_id: str) -> Order | None:
    return _ORDERS.get(order_id)


def mark_paying(order_id: str) -> Order | None:
    order = _ORDERS.get(order_id)
    if order and order.status == OrderStatus.CREATED:
        order.status = OrderStatus.PAYING
    return order


def close_order(order_id: str) -> Order | None:
    """超时未支付关闭（演示闭环）。"""
    order = _ORDERS.get(order_id)
    if order and order.status in (OrderStatus.CREATED, OrderStatus.PAYING):
        order.status = OrderStatus.CLOSED
    return order


def handle_payment_notify(order_id: str, channel: PaymentChannel, sign: str) -> dict:
    """支付回调：渠道匹配 + 验签 + 状态机 + 幂等。返回 (order, result_dict)。"""
    order = _ORDERS.get(order_id)
    if order is None:
        return {"ok": False, "reason": "order_not_found"}

    if order.channel != channel:
        # 渠道与下单时不一致：签名体系不同渠道不可互认（微信签名打不进支付宝单）
        return {"ok": False, "reason": "channel_mismatch"}

    if sign != _sign(order_id, channel.value):
        return {"ok": False, "reason": "bad_sign"}

    if order.status == OrderStatus.PAID:
        # 幂等：重复回调不入账
        return {"ok": True, "duplicate": True, "order": order}

    if order.status != OrderStatus.PAYING:
        # 乱序/非法状态（created 未进收银台、closed 已关单）拒绝
        return {"ok": False, "reason": f"invalid_status_{order.status.value}"}

    order.status = OrderStatus.PAID
    order.paidAt = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {"ok": True, "duplicate": False, "order": order}


def list_orders() -> list[Order]:
    return list(_ORDERS.values())
