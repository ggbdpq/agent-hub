/**
 * SSE 帧解析器 + UTF-8 流式解码器
 *
 * 为什么不用 EventSource：只能 GET、不能带 JSON body、不能 AbortController、
 * 无法自定义事件类型分发——所以用 fetch ReadableStream + 这两个纯函数自己解帧。
 *
 * 两个核心难点：
 * 1. 多字节字符被 chunk 从中间劈开：decodeUtf8Stream 用 TextDecoder 的
 *    streaming 模式，半截多字节序列自动留到下一个 chunk；
 * 2. SSE 帧被 chunk 从中间劈开：createSseParser 维护行缓冲，遇到空行才派发，
 *    最后一行无换行时留到 flush()。
 */

export interface RawSseFrame {
  event: string;
  data: string;
}

/** 字节流 → 文本流：TextDecoder streaming 模式处理跨 chunk 的多字节字符 */
export function decodeUtf8Stream(): { push: (bytes: Uint8Array) => string; flush: () => string } {
  const decoder = new TextDecoder("utf-8");
  return {
    push: (bytes) => decoder.decode(bytes, { stream: true }),
    flush: () => decoder.decode(),
  };
}

export interface SseParser {
  /** 喂入一段（可能不完整的）文本，返回其中已完整的帧 */
  push: (text: string) => RawSseFrame[];
  /** 流结束：把缓冲里未以换行结尾的残帧冲出来 */
  flush: () => RawSseFrame[];
}

/**
 * SSE 帧解析器（纯函数，无 IO，便于单测）
 *
 * 帧格式（与服务端约定一致）：
 *   event: <类型>\n
 *   data: <JSON>\n
 *   \n            ← 空行派发
 * 兼容 CRLF；忽略注释行（: 开头）；多行 data 以 \n 连接。
 */
export function createSseParser(): SseParser {
  let lineBuffer = "";
  let eventName = "message";
  let dataLines: string[] = [];

  const dispatch = (): RawSseFrame | null => {
    if (dataLines.length === 0) return null;
    const frame: RawSseFrame = { event: eventName, data: dataLines.join("\n") };
    eventName = "message";
    dataLines = [];
    return frame;
  };

  const consumeLine = (line: string, frames: RawSseFrame[]) => {
    if (line === "") {
      const frame = dispatch();
      if (frame) frames.push(frame);
      return;
    }
    if (line.startsWith(":")) return; // SSE 注释/心跳
    if (line.startsWith("event:")) {
      eventName = line.slice(6).trim();
      return;
    }
    if (line.startsWith("data:")) {
      // 规范允许 "data: xxx"（冒号后一个空格）
      dataLines.push(line.slice(5).startsWith(" ") ? line.slice(6) : line.slice(5));
    }
  };

  return {
    push(text) {
      const frames: RawSseFrame[] = [];
      lineBuffer += text;
      let idx: number;
      // while 循环消化所有完整行；最后一个不以换行结尾的片段留在 lineBuffer
      while ((idx = lineBuffer.search(/\r?\n/)) !== -1) {
        const line = lineBuffer.slice(0, idx);
        lineBuffer = lineBuffer.slice(idx).replace(/^\r?\n/, "");
        consumeLine(line, frames);
      }
      return frames;
    },
    flush() {
      const frames: RawSseFrame[] = [];
      if (lineBuffer !== "") {
        consumeLine(lineBuffer, frames);
        lineBuffer = "";
      }
      const frame = dispatch();
      if (frame) frames.push(frame);
      return frames;
    },
  };
}
