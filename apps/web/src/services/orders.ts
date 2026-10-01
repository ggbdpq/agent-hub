import { z } from "zod";
import { request } from "@/lib/api-client";
import { Order } from "@shared/index";

/** 订单/支付沙箱服务。真实支付仅差商户资质与官方 SDK 替换。 */

export function createOrder(
  body: { flowerName: string; amountFen: number; channel: "wechat" | "alipay" },
  signal?: AbortSignal,
) {
  return request({ url: "/orders", method: "POST", data: body, schema: Order, signal });
}

export function openCashier(orderId: string, signal?: AbortSignal) {
  return request({
    url: `/orders/${encodeURIComponent(orderId)}/cashier`,
    method: "POST",
    schema: Order,
    signal,
  });
}

/**
 * 模拟支付回调：沙箱收银台在"支付成功"时调用。
 * 真实场景这一步是微信/支付宝服务器 → 商户回调地址，带平台签名。
 */
export async function payNotify(
  orderId: string,
  channel: "wechat" | "alipay",
  sign: string,
  signal?: AbortSignal,
) {
  return request({
    url: `/orders/${encodeURIComponent(orderId)}/notify`,
    method: "POST",
    data: { orderId, channel, sign },
    schema: z.object({ duplicate: z.boolean(), orderId: z.string(), status: z.string() }),
    signal,
  });
}

export function closeOrder(orderId: string, signal?: AbortSignal) {
  return request({
    url: `/orders/${encodeURIComponent(orderId)}/close`,
    method: "POST",
    schema: Order,
    signal,
  });
}

export function listOrders(signal?: AbortSignal) {
  return request({ url: "/orders", schema: Order.array(), signal });
}

/** 沙箱专用：用订单号+渠道推导回调签名（模拟"平台侧生成签名"，本地演示用）。 */
export async function sandboxSign(orderId: string, channel: "wechat" | "alipay"): Promise<string> {
  const secret = "sandbox-secret-do-not-use-in-prod";
  const raw = `${orderId}${channel}${secret}`;
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(raw));
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("")
    .slice(0, 16);
}
