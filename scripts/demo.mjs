// 一键演示：启动 FastAPI(:8808) + TS 智能体(:8809) + Vite(:8888)，
// 任一进程退出即拉停全部。与 scripts/api.mjs 同一跨平台策略：参数数组 spawn，不经过 shell。
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

// 相对脚本自身定位仓库根，不依赖调用方 cwd
const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const venvPython = resolve(
  ROOT,
  "apps/api/.venv",
  ...(process.platform === "win32" ? ["Scripts/python.exe"] : ["bin/python"]),
);
const viteEntry = resolve(ROOT, "apps/web/node_modules/vite/bin/vite.js");
const agentEntry = resolve(ROOT, "apps/agent/src/server.ts");

if (!existsSync(venvPython)) {
  console.error("[demo] 未找到 python venv，请先执行: pnpm api:setup");
  process.exit(1);
}
if (!existsSync(viteEntry)) {
  console.error("[demo] 未找到 web 依赖，请先执行: pnpm install");
  process.exit(1);
}
if (!existsSync(agentEntry)) {
  console.error("[demo] 未找到 apps/agent/src/server.ts，请先 pnpm install");
  process.exit(1);
}

const children = [];

function start(name, cmd, args, cwd) {
  const child = spawn(cmd, args, { cwd, stdio: ["ignore", "pipe", "pipe"], shell: false });
  let buffer = "";
  const pipe = (chunk) => {
    buffer += chunk.toString();
    const lines = buffer.split(/\r?\n/);
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      if (line.trim()) console.log(`[${name}] ${line}`);
    }
  };
  child.stdout.on("data", pipe);
  child.stderr.on("data", pipe);
  child.on("exit", (code) => {
    if (buffer.trim()) console.log(`[${name}] ${buffer}`);
    if (!shuttingDown) {
      console.log(`[demo] ${name} 已退出（code=${code ?? "signal"}），关闭全部进程`);
      shutdown(code ?? 1);
    }
  });
  children.push(child);
  return child;
}

let shuttingDown = false;
function shutdown(code) {
  if (shuttingDown) return;
  shuttingDown = true;
  for (const child of children) {
    if (child.exitCode === null && !child.killed) child.kill();
  }
  process.exit(code);
}
process.on("SIGINT", () => shutdown(130));
process.on("SIGTERM", () => shutdown(143));

console.log("[demo] 启动 FastAPI :8808 + TS 智能体 :8809 与 Vite :8888（Ctrl+C 退出）…");
// 先起智能体并等 /healthz 就绪，避免 Vite 先可访问而代理目标未监听的窗口
const agentChild = start("agent", process.execPath, [agentEntry], ROOT);
await waitHealthy("http://127.0.0.1:8809/healthz", "agent", agentChild);
start("api", venvPython, ["-m", "uvicorn", "app.main:app", "--port", "8808"], resolve(ROOT, "apps/api"));
start("web", process.execPath, [viteEntry], resolve(ROOT, "apps/web"));
console.log("[demo] 打开 http://localhost:8888");

async function waitHealthy(url, name, child) {
  for (let i = 0; i < 100; i++) {
    if (child.exitCode !== null) process.exit(1); // agent 已退出
    try {
      const res = await fetch(url);
      if (res.ok) return;
    } catch {
      /* 还没就绪 */
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  console.error(`[demo] ${name} 未在时限内就绪`);
  process.exit(1);
}
