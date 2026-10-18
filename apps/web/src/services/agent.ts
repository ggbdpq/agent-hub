import { decodeUtf8Stream, createSseParser } from "@shared/index";
import type { RawSseFrame } from "@shared/index";

/**
 * 对话服务：fetch + ReadableStream 的 SSE 客户端
 *
 * 链路：POST JSON → 响应字节流 → decodeUtf8Stream（跨 chunk 多字节安全）
 *      → createSseParser（跨 chunk 帧边界安全）→ JSON.parse(data) → yield 事件对象
 *
 * 注意：这里只负责"帧 → JSON 对象"的传输层解析；契约白名单校验
 * （AgentStreamEvent.parse）在 store 层做——传输错误与契约错误分级。
 */
export async function* streamAgentChat(
  message: string,
  signal?: AbortSignal,
  threadId?: string,
): AsyncGenerator<Record<string, unknown>> {
  const res = await fetch("/api/agent/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(threadId ? { message, threadId } : { message }),
    signal,
  });
  if (!res.ok || !res.body) {
    throw new Error(`对话请求失败：HTTP ${res.status}`);
  }

  const decoder = decodeUtf8Stream();
  const parser = createSseParser();
  const reader = res.body.getReader();

  const yieldFrame = (frame: RawSseFrame) => {
    try {
      return JSON.parse(frame.data) as Record<string, unknown>;
    } catch {
      // 传输层容错：单帧 JSON 损坏时明确报协议错误，而不是让 store 收到 undefined
      throw new Error(`SSE 数据帧不是合法 JSON（event=${frame.event}）`);
    }
  };

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      const text = decoder.push(value);
      for (const frame of parser.push(text)) {
        yield yieldFrame(frame);
      }
    }
    // 流结束：冲出解码器半截多字节与解析器残帧
    for (const frame of parser.push(decoder.flush())) {
      yield yieldFrame(frame);
    }
    for (const frame of parser.flush()) {
      yield yieldFrame(frame);
    }
  } finally {
    reader.releaseLock();
  }
}
