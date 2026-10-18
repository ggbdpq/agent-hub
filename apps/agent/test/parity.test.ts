// WP3 对等测试（04 §4）：FastAPI 离线剧本 vs TS 离线剧本，三场景对齐。
// 事件类型序列（连续 thought 折叠后）、card payload 深度相等、证据 id 列表相等、token 拼接全文相等。
// thought 文案允许不同：ts 版多了工具段，这是有意的变化。无 venv 时 skip（CI agent job 无 Python）。
import assert from "node:assert/strict";
import { spawn, type ChildProcess } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { test, type TestContext } from "node:test";
import { AgentStreamEvent } from "@agent-hub/shared";
import { buildAgent, runTurn } from "../src/agent.ts";
import { ScriptedChatModel } from "../src/scripted-model.ts";
import { floraPolicy } from "../src/scripts.ts";

const HERE = dirname(fileURLToPath(import.meta.url)); // apps/agent/test
const ROOT = resolve(HERE, "../../.."); // 仓库根（test → agent → apps → root）
const VENV_PY = resolve(ROOT, "apps/api/.venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
const PARITY_PORT = 8818; // 避开 demo 的 8808 与 agent 的 8809

const SCENES = ["这周想买点花放客厅", "想给妈妈送生日礼物", "下周去医院探望朋友"] as const;

/** SSE 文本 → 事件对象数组（py 侧帧格式与 ts 相同：event + data 两行 + 空行） */
function parseSse(raw: string): AgentStreamEvent[] {
  const evs: AgentStreamEvent[] = [];
  for (const block of raw.split("\n\n")) {
    const lines = block.split("\n").filter((l) => l.trim());
    if (!lines.length) continue;
    const event = lines.find((l) => l.startsWith("event:"))?.slice(6).trim();
    const data = lines.find((l) => l.startsWith("data:"))?.slice(5).trim();
    if (!event || !data) continue;
    evs.push(AgentStreamEvent.parse(JSON.parse(data)));
  }
  return evs;
}

/** 折叠连续同类型事件（thought 段数允许不同） */
function collapsedTypes(evs: AgentStreamEvent[]): string[] {
  const out: string[] = [];
  for (const e of evs) if (e.type !== out.at(-1)) out.push(e.type);
  return out;
}

async function collectPy(message: string): Promise<AgentStreamEvent[]> {
  const res = await fetch(`http://127.0.0.1:${PARITY_PORT}/api/agent/chat`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ message }),
  });
  assert.equal(res.status, 200);
  return parseSse(await res.text());
}

async function collectTs(agent: ReturnType<typeof buildAgent>, message: string): Promise<AgentStreamEvent[]> {
  const evs: AgentStreamEvent[] = [];
  for await (const e of runTurn(agent, { message, threadId: `parity-${SCENES.indexOf(message as never)}` })) {
    evs.push(AgentStreamEvent.parse(e));
  }
  return evs;
}

async function waitHealthy(): Promise<void> {
  for (let i = 0; i < 50; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${PARITY_PORT}/api/health`);
      if (res.ok) return;
    } catch {
      /* 还没起来 */
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error("FastAPI 未在时限内就绪");
}

test("对等：FastAPI 离线剧本与 ts 离线剧本三场景事件对齐", async (t: TestContext) => {
  if (!existsSync(VENV_PY)) return t.skip("apps/api/.venv 不存在，跳过对等测试");

  let py: ChildProcess | undefined;
  try {
    py = spawn(VENV_PY, ["-m", "uvicorn", "app.main:app", "--port", String(PARITY_PORT)], {
      cwd: resolve(ROOT, "apps/api"),
      stdio: "ignore",
      shell: false,
    });
    await waitHealthy();

    const tsAgent = buildAgent({ model: new ScriptedChatModel(floraPolicy) });
    for (const message of SCENES) {
      const [pyEvs, tsEvs] = await Promise.all([collectPy(message), collectTs(tsAgent, message)]);

      assert.deepEqual(collapsedTypes(tsEvs), collapsedTypes(pyEvs), `${message}：折叠后的事件类型序列`);

      const pyCard = pyEvs.find((e) => e.type === "card") as { payload: object } | undefined;
      const tsCard = tsEvs.find((e) => e.type === "card") as { payload: object } | undefined;
      assert.ok(pyCard && tsCard, `${message}：两侧都有卡片`);
      assert.deepEqual(tsCard.payload, pyCard.payload, `${message}：card payload 深度相等`);

      const ids = (evs: AgentStreamEvent[]) =>
        evs.filter((e): e is Extract<AgentStreamEvent, { type: "evidence" }> => e.type === "evidence").map((e) => e.evidence.id);
      assert.deepEqual(ids(tsEvs), ids(pyEvs), `${message}：证据 id 列表相等`);

      const text = (evs: AgentStreamEvent[]) =>
        evs.filter((e): e is Extract<AgentStreamEvent, { type: "token" }> => e.type === "token").map((e) => e.text).join("");
      assert.equal(text(tsEvs), text(pyEvs), `${message}：正文全文相等`);

      assert.equal(pyEvs.at(-1)?.type, "done");
      assert.equal(tsEvs.at(-1)?.type, "done");
    }
  } finally {
    py?.kill();
  }
});
