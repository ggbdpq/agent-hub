import assert from "node:assert/strict";
import { test } from "node:test";
import { createSseParser, decodeUtf8Stream } from "./sse-decoder.ts";

test("跨 chunk 的多字节字符：中文被字节边界劈开也能完整解码", () => {
  const decoder = decodeUtf8Stream();
  // "花语" 的 UTF-8：花=E8 8A B1，语=E8 AF AD —— 故意从 8A 后劈开
  const bytes = new Uint8Array([0xe8, 0x8a, 0xb1, 0xe8, 0xaf, 0xad]);
  const first = decoder.push(bytes.slice(0, 2));
  const second = decoder.push(bytes.slice(2));
  const last = decoder.flush();
  assert.equal(first + second + last, "花语");
});

test("SSE 帧被 chunk 从中间劈开：行缓冲直到空行才派发", () => {
  const parser = createSseParser();
  assert.deepEqual(parser.push("event: token\nda"), []); // data 行未完整
  assert.deepEqual(parser.push('ta: {"type":"token","text":"玫瑰"}\n'), []); // 行完整但未见空行
  assert.deepEqual(parser.push("\n"), [{ event: "token", data: '{"type":"token","text":"玫瑰"}' }]);
});

test("一个 chunk 含多个完整帧：全部解析且顺序正确", () => {
  const parser = createSseParser();
  const frames = parser.push(
    'event: thought\ndata: {"seq":1}\n\nevent: token\ndata: {"text":"hi"}\n\n',
  );
  assert.deepEqual(frames, [
    { event: "thought", data: '{"seq":1}' },
    { event: "token", data: '{"text":"hi"}' },
  ]);
});

test("CRLF 兼容", () => {
  const parser = createSseParser();
  assert.deepEqual(parser.push('event: done\r\ndata: {"replyId":"r1"}\r\n\r\n'), [
    { event: "done", data: '{"replyId":"r1"}' },
  ]);
});

test("注释行与 data: 前空格规范", () => {
  const parser = createSseParser();
  const frames = parser.push(': heartbeat\n\nevent: token\ndata: {"text":"a b"}\n\n');
  assert.deepEqual(frames, [{ event: "token", data: '{"text":"a b"}' }]);
});

test("flush 冲出未以换行结尾的残帧", () => {
  const parser = createSseParser();
  assert.deepEqual(parser.push('event: done\ndata: {"replyId":"r2"}'), []);
  assert.deepEqual(parser.flush(), [{ event: "done", data: '{"replyId":"r2"}' }]);
  assert.deepEqual(parser.flush(), []); // 派发后缓冲清空
});
