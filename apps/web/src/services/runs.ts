import { z } from "zod";
import { request, ApiError } from "@/lib/api-client";
import { decodeUtf8Stream, createSseParser } from "@shared/index";
import type { RawSseFrame } from "@shared/index";
import {
  RunEvent,
  RunRecord,
  InspectionReport,
  type RunEvent as RunEventType,
} from "@shared/index";

/**
 * 巡检多智能体服务（/api/runs）
 *
 * 与花语服务同构：传输层只做"帧 → JSON"，契约白名单（RunEvent.parse）在边界处
 * 强制——事件必须过 zod 才进渲染。SSE 支持 Last-Event-ID 断点续传（ADR-0008）。
 */

/** 恢复锚点（CLAUDE.md 任务页同款约定）：localStorage 只存 id 与已收 seq，远端是事实源 */
const RESUME_KEY = "agent-hub-run";

export interface RunResumeAnchor {
  runId: string;
  lastSeq: number;
}

export function saveResumeAnchor(anchor: RunResumeAnchor): void {
  localStorage.setItem(RESUME_KEY, JSON.stringify(anchor));
}

export function loadResumeAnchor(): RunResumeAnchor | null {
  try {
    const raw = localStorage.getItem(RESUME_KEY);
    if (!raw) return null;
    const parsed = z.object({ runId: z.string(), lastSeq: z.number().int().nonnegative() }).parse(JSON.parse(raw));
    return parsed;
  } catch {
    localStorage.removeItem(RESUME_KEY);
    return null;
  }
}

export function clearResumeAnchor(): void {
  localStorage.removeItem(RESUME_KEY);
}

export type CreateRunBody = {
  site: string;
  deviceIds: string[];
  window?: string | null;
  goal?: string | null;
};

/** 创建巡检 run；Idempotency-Key 供重试去重（服务端幂等返回同一 run） */
export async function createRun(body: CreateRunBody): Promise<z.infer<typeof RunRecord>> {
  const idempotencyKey = crypto.randomUUID();
  let res: Response;
  try {
    res = await fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(body),
    });
  } catch (error) {
    throw new ApiError("HTTP", `网络请求失败：${String(error)}`, error);
  }
  const payload: unknown = await res.json().catch(() => {
    throw new ApiError("CONTRACT", "响应不是合法 JSON");
  });
  if (!res.ok) {
    throw new ApiError("HTTP", `HTTP ${res.status} ${res.statusText}`, payload);
  }
  const parsed = RunRecord.safeParse(payload);
  if (!parsed.success) {
    throw new ApiError("CONTRACT", "run 响应不符合契约", parsed.error);
  }
  return parsed.data;
}

export async function getRun(runId: string): Promise<z.infer<typeof RunRecord>> {
  return request({ url: `/runs/${encodeURIComponent(runId)}`, schema: RunRecord });
}

export function reportOf(record: z.infer<typeof RunRecord>): z.infer<typeof InspectionReport> | null {
  if (!record.result) return null;
  const parsed = InspectionReport.safeParse(record.result);
  return parsed.success ? parsed.data : null;
}

export async function resolveApproval(
  runId: string,
  approvalId: string,
  approved: boolean,
  note?: string,
): Promise<void> {
  await request({
    url: `/runs/${encodeURIComponent(runId)}/approvals/${encodeURIComponent(approvalId)}`,
    method: "POST",
    data: { approved, note },
    schema: z.object({ ok: z.boolean() }),
  });
}

export interface StreamHandle {
  promise: Promise<void>;
  abort: () => void;
}

/**
 * 订阅 run 事件流（SSE + Last-Event-ID 续传）。
 *
 * 服务端在终态事件后关闭流；断线重连时调用方以已收最大 seq 作为 lastSeq 重入。
 */
export function streamRunEvents(
  runId: string,
  handlers: {
    lastSeq?: number;
    onEvent: (event: RunEventType) => void;
  },
): StreamHandle {
  const controller = new AbortController();
  const promise = (async () => {
    const headers: Record<string, string> = {};
    if (handlers.lastSeq && handlers.lastSeq > 0) {
      headers["Last-Event-ID"] = String(handlers.lastSeq);
    }
    const res = await fetch(`/api/runs/${encodeURIComponent(runId)}/events`, {
      headers,
      signal: controller.signal,
    });
    if (!res.ok || !res.body) {
      throw new ApiError("HTTP", `事件流请求失败：HTTP ${res.status}`);
    }
    const decoder = decodeUtf8Stream();
    const parser = createSseParser();
    const reader = res.body.getReader();
    const handleFrame = (frame: RawSseFrame): void => {
      let parsedEvent: unknown;
      try {
        parsedEvent = JSON.parse(frame.data);
      } catch {
        throw new ApiError("CONTRACT", `SSE 数据帧不是合法 JSON（event=${frame.event}）`);
      }
      const event = RunEvent.parse(parsedEvent); // 白名单：契约外数据不进 UI
      handlers.onEvent(event);
    };
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        const text = decoder.push(value);
        for (const frame of parser.push(text)) handleFrame(frame);
      }
      for (const frame of parser.push(decoder.flush())) handleFrame(frame);
      for (const frame of parser.flush()) handleFrame(frame);
    } finally {
      reader.releaseLock();
    }
  })();
  return { promise, abort: () => controller.abort() };
}
