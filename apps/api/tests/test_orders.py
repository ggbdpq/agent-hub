"""支付沙箱：验签、幂等、状态机拒绝路径。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.orders import expected_sign

# with 上下文持有 portal：与任务测试同理，事件循环跨请求存活
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _make_order(client, channel: str = "wechat") -> str:
    order = client.post(
        "/api/orders",
        json={"flowerName": "香槟玫瑰礼盒", "amountFen": 12800, "channel": channel},
    ).json()
    return order["orderId"]


def _notify(client, order_id: str, channel: str = "wechat", sign: str | None = None):
    return client.post(
        f"/api/orders/{order_id}/notify",
        json={"orderId": order_id, "channel": channel, "sign": sign or expected_sign(order_id, channel)},
    )


def test_notify_before_cashier_rejected(client):
    """created 未进收银台就回调：状态机拒绝（防跳过支付流程）。"""
    order_id = _make_order(client)
    response = _notify(client, order_id)
    assert response.status_code == 409
    assert "invalid_status" in response.json()["detail"]


def test_pay_flow_and_idempotency(client):
    """收银台 → 回调入账 → 重复回调幂等。"""
    order_id = _make_order(client)
    assert client.post(f"/api/orders/{order_id}/cashier").json()["status"] == "paying"

    first = _notify(client, order_id)
    assert first.status_code == 200
    assert first.json()["duplicate"] is False

    second = _notify(client, order_id)  # 重复回调（网络重发场景）
    assert second.status_code == 200
    assert second.json()["duplicate"] is True

    order = client.get("/api/orders").json()
    paid = [o for o in order if o["orderId"] == order_id][0]
    assert paid["status"] == "paid"
    assert paid["paidAt"] is not None


def test_bad_sign_rejected(client):
    order_id = _make_order(client, "alipay")
    client.post(f"/api/orders/{order_id}/cashier")
    response = _notify(client, order_id, channel="alipay", sign="0" * 16)
    assert response.status_code == 409
    assert response.json()["detail"] == "bad_sign"


def test_channel_mismatch_rejected(client):
    """渠道不匹配（下单 wechat，回调 alipay）：渠道绑定校验拒绝。"""
    order_id = _make_order(client)
    client.post(f"/api/orders/{order_id}/cashier")
    response = _notify(client, order_id, channel="alipay")
    assert response.status_code == 409


def test_close_after_paid_noop(client):
    order_id = _make_order(client)
    client.post(f"/api/orders/{order_id}/cashier")
    _notify(client, order_id)
    order = client.post(f"/api/orders/{order_id}/close").json()
    assert order["status"] == "paid"  # 已支付订单不可关单


def test_close_timeout_order(client):
    order_id = _make_order(client)
    client.post(f"/api/orders/{order_id}/cashier")
    order = client.post(f"/api/orders/{order_id}/close").json()
    assert order["status"] == "closed"
    # 关单后回调：拒绝
    assert _notify(client, order_id).status_code == 409


def test_notify_unknown_order(client):
    response = client.post(
        "/api/orders/nonexistent/notify",
        json={"orderId": "nonexistent", "channel": "wechat", "sign": "x"},
    )
    assert response.status_code == 409
