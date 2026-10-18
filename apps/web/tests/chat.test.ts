import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";
import type { TestContext } from "node:test";
import { setImmediate } from "node:timers/promises";

const aliases: Record<string, string> = {
  "@shared/index": new URL("../../../packages/shared/src/index.ts", import.meta.url).href,
  "@/services/agent": new URL("../src/services/agent.ts", import.meta.url).href,
};
registerHooks({
  resolve: (specifier, context, nextResolve) =>
    nextResolve(aliases[specifier] ?? specifier, context),
});
const { useChatStore } = await import("../src/stores/chat.ts");

function controlledHttp(t: TestContext) {
  useChatStore.getState().reset();
  const streams: ReadableStreamDefaultController<Uint8Array>[] = [];
  t.mock.method(globalThis, "fetch", async () => {
    const body = new ReadableStream<Uint8Array>({
      start: (controller) => streams.push(controller),
    });
    return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
  });
  t.mock.method(console, "error", () => {});
  t.after(async () => {
    useChatStore.getState().reset();
    for (const stream of streams) {
      try {
        stream.close();
      } catch {
        /* 已关闭或报错的流无需再次收尾。 */
      }
    }
    await setImmediate();
  });
  return streams;
}

function emit(stream: ReadableStreamDefaultController<Uint8Array>, event: Record<string, unknown>) {
  stream.enqueue(
    new TextEncoder().encode(`event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`),
  );
}

test("停止后立即重发：旧请求的延迟异常不影响新回答", async (t) => {
  const streams = controlledHttp(t);
  const old = useChatStore.getState().sendMessage("旧问题");
  await setImmediate();
  useChatStore.getState().abort();
  const current = useChatStore.getState().sendMessage("新问题");
  await setImmediate();
  streams[0].error(new DOMException("旧请求已停止", "AbortError"));
  await old;
  assert.equal(useChatStore.getState().phase, "thinking");
  assert.ok(useChatStore.getState().messages.every((message) => message.error === undefined));

  emit(streams[1], { type: "token", text: "新回复" });
  emit(streams[1], { type: "done", replyId: "current" });
  streams[1].close();
  await current;
  assert.equal(useChatStore.getState().phase, "done");
  assert.deepEqual(useChatStore.getState().messages.at(-1)?.segments, [
    { kind: "token", text: "新回复" },
  ]);
});

test("停止后立即重发：旧流的延迟事件不改消息或新流状态", async (t) => {
  const streams = controlledHttp(t);
  const old = useChatStore.getState().sendMessage("旧问题");
  await setImmediate();
  useChatStore.getState().abort();
  const current = useChatStore.getState().sendMessage("新问题");
  await setImmediate();
  emit(streams[1], { type: "token", text: "新回复" });
  await setImmediate();
  emit(streams[0], { type: "token", text: "已停止的旧回复" });
  emit(streams[0], { type: "done", replyId: "old" });
  streams[0].close();
  await old;
  assert.equal(useChatStore.getState().phase, "answering");
  assert.deepEqual(useChatStore.getState().messages[1].segments, []);
  assert.deepEqual(useChatStore.getState().messages.at(-1)?.segments, [
    { kind: "token", text: "新回复" },
  ]);
  emit(streams[1], { type: "done", replyId: "current" });
  streams[1].close();
  await current;
  assert.equal(useChatStore.getState().phase, "done");
});

test("停止后立即重发：旧流的空 EOF 不结束新回答", async (t) => {
  const streams = controlledHttp(t);
  const old = useChatStore.getState().sendMessage("旧问题");
  await setImmediate();
  useChatStore.getState().abort();
  const current = useChatStore.getState().sendMessage("新问题");
  await setImmediate();
  streams[0].close();
  await old;
  assert.equal(useChatStore.getState().phase, "thinking");
  emit(streams[1], { type: "done", replyId: "current" });
  streams[1].close();
  await current;
  assert.equal(useChatStore.getState().phase, "done");
});

test("当前回答未收到 done 就 EOF：保留部分回复并明确显示截断", async (t) => {
  const streams = controlledHttp(t);
  const current = useChatStore.getState().sendMessage("问题");
  await setImmediate();
  emit(streams[0], { type: "token", text: "部分回复" });
  streams[0].close();
  await current;
  const state = useChatStore.getState();
  assert.equal(state.phase, "error");
  assert.equal(state.messages.at(-1)?.error, "响应流提前结束，未收到完成事件");
  assert.deepEqual(state.messages.at(-1)?.segments, [{ kind: "token", text: "部分回复" }]);
});
