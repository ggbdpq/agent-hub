import { z } from "zod";

/**
 * 请求层（fetch 封装）
 *
 * 职责链：统一前缀 → JSON 序列化 → 错误分级（HTTP / 业务）→ Zod 契约校验。
 * 契约破坏在边界处报错，而不是渲染时 NPE——AI 辅助开发改坏接口时最先在这里炸。
 */

const BASE_URL = "/api";

export class ApiError extends Error {
  constructor(
    public readonly code: "HTTP" | "BUSINESS" | "CONTRACT",
    message: string,
    public readonly detail?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

interface RequestOptions<T> {
  url: string;
  method?: "GET" | "POST";
  /** GET 查询参数 */
  params?: Record<string, string>;
  /** 请求体 */
  data?: unknown;
  /** Zod Schema：响应运行时校验 */
  schema?: z.ZodType<T>;
  signal?: AbortSignal;
}

export async function request<T>(options: RequestOptions<T>): Promise<T> {
  const { url, method = "GET", params, data, schema, signal } = options;

  let uri = `${BASE_URL}${url}`;
  if (method === "GET" && params) {
    const qs = new URLSearchParams(params).toString();
    if (qs) uri += `?${qs}`;
  }

  let res: Response;
  try {
    res = await fetch(uri, {
      method,
      headers: data !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: data !== undefined ? JSON.stringify(data) : undefined,
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError("HTTP", `网络请求失败：${String(error)}`, error);
  }

  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new ApiError("HTTP", `HTTP ${res.status} ${res.statusText}`, detail);
  }

  const payload: unknown = await res.json().catch(() => {
    throw new ApiError("CONTRACT", "响应不是合法 JSON");
  });

  if (!schema) return payload as T;

  const parsed = schema.safeParse(payload);
  if (!parsed.success) {
    throw new ApiError("CONTRACT", "响应数据不符合契约 Schema", parsed.error);
  }
  return parsed.data;
}
