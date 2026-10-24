// 进程重启 E2E 的编排层（Y1/Y2 专用，所有权 scripts/e2e-restart/**）。
//
// 与 playwright.config.ts 的 webServer 模式不同：本编排由 spec 直接持有子进程——
// API（uvicorn :8818，独立 db）可被真实 SIGKILL 并以同一 db 重新拉起；
// web（Vite :8890）经程序化 createServer 启动，/api 代理指向 8818 而非共享栈的 8808，
// 因此可与既有 7 条 E2E（8888/8808/8809）同命令共存，互不干扰。
// 页面与 API 同源（都经 :8890），不涉及跨源 CORS。
import { spawn } from "node:child_process";
import { once } from "node:events";
import { rm } from "node:fs/promises";
import { DatabaseSync } from "node:sqlite";
import { dirname, resolve } from "node:path";
import process from "node:process";
import { fileURLToPath, pathToFileURL } from "node:url";

export const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
export const API_PORT = 8818;
export const WEB_PORT = 8890;
export const API_ORIGIN = `http://127.0.0.1:${API_PORT}`;
export const WEB_ORIGIN = `http://127.0.0.1:${WEB_PORT}`;

const venvPython = resolve(
  ROOT,
  "apps/api/.venv",
  ...(process.platform === "win32" ? ["Scripts/python.exe"] : ["bin/python"]),
);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 每条用例独立 db（runs+checkpoints 显式路径，启动前清理，含 WAL/SHM 伴生文件） */
export function dbPaths(tag) {
  return {
    runsDb: `/tmp/ah-e2e-restart-${tag}-runs.sqlite`,
    checkpoints: `/tmp/ah-e2e-restart-${tag}-ckpt.sqlite`,
  };
}

export async function cleanupDb(paths) {
  for (const base of [paths.runsDb, paths.checkpoints]) {
    await rm(base, { force: true });
    await rm(`${base}-wal`, { force: true });
    await rm(`${base}-shm`, { force: true });
  }
}

/** 可 SIGKILL/重启的 API 进程：restart 必须复用同一 db（runs+checkpoints 持久化路径） */
export class ApiProcess {
  constructor(paths) {
    this.runsDb = paths.runsDb;
    this.checkpoints = paths.checkpoints;
    this.child = null;
    this.output = "";
  }

  get running() {
    // SIGKILL 退出时 exitCode 为 null、signalCode 非空——两者都算已退出
    return (
      this.child !== null && this.child.exitCode === null && this.child.signalCode === null
    );
  }

  async start() {
    if (this.running) throw new Error("API 已在运行，先 kill9 再重启");
    const child = spawn(
      venvPython,
      ["-m", "uvicorn", "app.main:app", "--port", String(API_PORT)],
      {
        cwd: resolve(ROOT, "apps/api"),
        env: {
          ...process.env,
          AGENT_HUB_RUNS_DB: this.runsDb,
          AGENT_HUB_CHECKPOINTS: this.checkpoints,
          AGENT_HUB_LLM: "scripted", // 离线确定性剧本（E2E 不碰真实模型）
          OPENAI_API_KEY: "",
          OPENAI_PROVIDER: "",
          NO_PROXY: "localhost,127.0.0.1",
          no_proxy: "localhost,127.0.0.1",
        },
        stdio: ["ignore", "pipe", "pipe"],
        shell: false,
      },
    );
    this.child = child;
    const pipe = (chunk) => {
      this.output += chunk.toString();
      if (this.output.length > 20_000) this.output = this.output.slice(-10_000);
    };
    child.stdout.on("data", pipe);
    child.stderr.on("data", pipe);
    await waitApiHealthy(child, 30_000);
    return this;
  }

  /** 真实 SIGKILL：进程无任何清理机会（租约/心跳/审批 Future 戛然而止） */
  async kill9() {
    const child = this.child;
    if (child === null || child.exitCode !== null) return;
    child.kill("SIGKILL");
    await once(child, "exit");
  }

  async stop() {
    if (!this.running) return;
    this.child.kill("SIGTERM");
    await once(this.child, "exit");
  }
}

async function waitApiHealthy(child, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    if (child.exitCode !== null) {
      throw new Error(`API 进程启动即退出（code=${child.exitCode}）`);
    }
    try {
      const res = await fetch(`${API_ORIGIN}/api/health`);
      if (res.ok) return;
    } catch {
      /* 还没就绪 */
    }
    if (Date.now() > deadline) {
      throw new Error(`API 未在 ${timeoutMs}ms 内就绪（${API_ORIGIN}/api/health）`);
    }
    await sleep(150);
  }
}

/** 程序化 Vite（:8890）：复用 apps/web 的完整配置（插件/别名），仅覆盖端口与 /api 代理目标。
 * cacheDir 独立，避免与共享栈的 Vite（同 root）并发写 .vite 缓存。 */
export async function startWeb() {
  const viteEntry = resolve(ROOT, "apps/web/node_modules/vite/dist/node/index.js");
  const { createServer } = await import(pathToFileURL(viteEntry).href);
  const server = await createServer({
    configFile: resolve(ROOT, "apps/web/vite.config.ts"),
    root: resolve(ROOT, "apps/web"),
    cacheDir: resolve(ROOT, "apps/web/node_modules/.vite-e2e-restart"),
    server: {
      host: "127.0.0.1",
      port: WEB_PORT,
      strictPort: true,
      proxy: {
        "/api": { target: API_ORIGIN, changeOrigin: true },
      },
    },
    logLevel: "warn",
    clearScreen: false,
  });
  await server.listen();
  return server;
}

/** 终态后拉取完整事件序列（服务端排空后关流；直连 API，不经代理） */
export async function collectRunEvents(runId, { timeoutMs = 20_000 } = {}) {
  const res = await fetch(
    `${API_ORIGIN}/api/runs/${encodeURIComponent(runId)}/events`,
    { headers: { accept: "text/event-stream" }, signal: AbortSignal.timeout(timeoutMs) },
  );
  if (!res.ok) throw new Error(`events 请求失败：HTTP ${res.status}`);
  const text = await res.text();
  const events = [];
  for (const block of text.split("\n\n")) {
    const data = block
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data: "))
      .map((line) => line.slice(6))
      .join("");
    if (!data) continue;
    const parsed = JSON.parse(data);
    events.push({
      seq: parsed.seq,
      type: parsed.type,
      node: parsed.node,
      attempt: parsed.attempt,
      payload: parsed.payload ?? {},
    });
  }
  return events;
}

/** 账本层面证据：直接读 tool_ledger（WAL 库并发读安全） */
export function ledgerRows(runsDb) {
  let db;
  try {
    db = new DatabaseSync(runsDb, { readOnly: true });
  } catch {
    db = new DatabaseSync(runsDb); // -shm 尚未创建等场景退普通只读查询
  }
  try {
    return db.prepare("SELECT key, tool, status FROM tool_ledger ORDER BY key").all();
  } finally {
    db.close();
  }
}
