import assert from "node:assert/strict";
import { after, test } from "node:test";
import { startServer } from "./server.ts";

// WP0 空壳验收：/healthz 可用、未知路径 404。端口传 0 由系统分配，避免测试间冲突。

test("GET /healthz 返回 ok，未知路径 404", async () => {
  const server = await startServer(0);
  after(() => server.close());
  const { port } = server.address() as { port: number };

  const ok = await fetch(`http://127.0.0.1:${port}/healthz`);
  assert.equal(ok.status, 200);
  assert.equal(await ok.text(), "ok");

  const missing = await fetch(`http://127.0.0.1:${port}/nope`);
  assert.equal(missing.status, 404);
});
