import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { listOrders, openCashier, payNotify, sandboxSign } from "@/services/orders";
import type { Order } from "@shared/index";
import { toast } from "sonner";

/**
 * 订单与支付沙箱页
 *
 * 流程：下单（created）→ 收银台（paying）→ 支付成功 → 模拟平台异步回调（验签+幂等）→ paid。
 * 支付回调签名由沙箱推导（真实场景为微信/支付宝服务器带平台签名回调商户）。
 */

const STATUS_META: Record<
  Order["status"],
  { label: string; tone: "secondary" | "default" | "outline" | "destructive" }
> = {
  created: { label: "待支付", tone: "secondary" },
  paying: { label: "支付中", tone: "default" },
  paid: { label: "已支付", tone: "outline" },
  closed: { label: "已关闭", tone: "destructive" },
};

const CHANNEL_LABEL = { wechat: "微信支付", alipay: "支付宝" } as const;

interface CashierState {
  order: Order;
  paying: boolean;
  duplicate: boolean | null;
}

export function OrdersPage() {
  const [orders, setOrders] = useState<Order[]>([]);
  const [cashier, setCashier] = useState<CashierState | null>(null);

  const refresh = useCallback(async () => {
    try {
      setOrders(await listOrders());
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const openPayDialog = async (order: Order) => {
    try {
      const paying = await openCashier(order.orderId);
      setOrders((list) => list.map((o) => (o.orderId === paying.orderId ? paying : o)));
      setCashier({ order: paying, paying: false, duplicate: null });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error));
    }
  };

  const confirmPay = async () => {
    if (!cashier) return;
    setCashier({ ...cashier, paying: true });
    try {
      // 模拟"支付平台"侧：生成签名 → 异步回调商户（本沙箱内直调 notify 接口）
      const sign = await sandboxSign(cashier.order.orderId, cashier.order.channel);
      const result = await payNotify(cashier.order.orderId, cashier.order.channel, sign);
      setCashier({ ...cashier, duplicate: result.duplicate, paying: false });
      toast[result.duplicate ? "info" : "success"](
        result.duplicate ? "重复回调：幂等处理，未重复入账" : "支付成功",
      );
      await refresh();
    } catch (error) {
      setCashier({ ...cashier, paying: false });
      toast.error(error instanceof Error ? error.message : String(error));
    }
  };

  return (
    <div className="mx-auto w-full max-w-2xl space-y-4 overflow-y-auto p-5">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">订单支付（沙箱）</CardTitle>
          <p className="text-sm text-muted-foreground">
            从对话里的推荐卡片下单；本页完成"收银台 → 异步回调 → 幂等入账"全流程模拟。
          </p>
        </CardHeader>
        <CardContent className="space-y-3">
          {orders.length === 0 && (
            <p className="text-sm text-muted-foreground">
              还没有订单——去「花语顾问」拿一份推荐，点卡片上的「下单」。
            </p>
          )}
          {orders.map((order) => (
            <div key={order.orderId} className="flex items-center gap-3 rounded-lg border p-3">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="truncate text-sm font-medium">{order.flowerName}</span>
                  <Badge variant={STATUS_META[order.status].tone}>
                    {STATUS_META[order.status].label}
                  </Badge>
                </div>
                <div className="mt-0.5 flex items-center gap-2 text-xs text-muted-foreground">
                  <span>{CHANNEL_LABEL[order.channel]}</span>
                  <span>·</span>
                  <span className="font-mono">单号 {order.orderNo}</span>
                  {order.paidAt && (
                    <>
                      <span>·</span>
                      <span>{order.paidAt}</span>
                    </>
                  )}
                </div>
              </div>
              <span className="text-sm font-semibold">¥{(order.amountFen / 100).toFixed(2)}</span>
              {order.status === "created" && (
                <Button size="sm" onClick={() => void openPayDialog(order)}>
                  去支付
                </Button>
              )}
            </div>
          ))}
        </CardContent>
      </Card>

      {/* 模拟收银台 */}
      <Dialog open={cashier !== null} onOpenChange={(open) => !open && setCashier(null)}>
        <DialogContent>
          {cashier && (
            <>
              <DialogHeader>
                <DialogTitle>{CHANNEL_LABEL[cashier.order.channel]} · 沙箱收银台</DialogTitle>
                <DialogDescription>
                  真实场景这里是微信/支付宝收银台；支付完成后由平台服务器异步回调商户。
                </DialogDescription>
              </DialogHeader>
              <div className="space-y-4">
                <div className="rounded-lg border p-4 text-center">
                  <p className="text-xs text-muted-foreground">支付金额</p>
                  <p className="text-2xl font-semibold">
                    ¥{(cashier.order.amountFen / 100).toFixed(2)}
                  </p>
                  <p className="mt-1 text-xs text-muted-foreground">{cashier.order.flowerName}</p>
                </div>
                {cashier.duplicate === null ? (
                  <Button
                    className="w-full"
                    disabled={cashier.paying}
                    onClick={() => void confirmPay()}
                  >
                    {cashier.paying ? "支付中…" : "模拟支付成功"}
                  </Button>
                ) : (
                  <div className="space-y-2 text-center text-sm">
                    <p className="font-medium text-emerald-600">
                      {cashier.duplicate
                        ? "该订单已支付（本次为重复回调，已幂等处理）"
                        : "支付成功"}
                    </p>
                    <Button variant="outline" className="w-full" onClick={() => setCashier(null)}>
                      完成
                    </Button>
                  </div>
                )}
              </div>
            </>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
