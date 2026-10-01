// Python venv 启动器：Windows 下 pnpm scripts 走 cmd.exe，
// 不认识单引号、对正斜杠路径/&& 链也不可靠——所有 Python 调用集中在这里，
// 用参数数组 spawn，彻底消灭 shell 引号问题。跨平台（win32: Scripts/python，posix: bin/python）。
import { spawnSync } from "node:child_process";
import { resolve } from "node:path";
import process from "node:process";

const [, , command] = process.argv;

// 绝对路径：serve/test 的 cwd 在 apps/api，相对路径会失效
const venvPython = resolve(
  "apps/api/.venv",
  ...(process.platform === "win32" ? ["Scripts/python.exe"] : ["bin/python"]),
);

function run(cmd, args, cwd) {
  const result = spawnSync(cmd, args, { stdio: "inherit", cwd, shell: false });
  if (result.error) {
    console.error(`启动失败: ${result.error.message}`);
    process.exit(1);
  }
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}

switch (command) {
  case "setup":
    // venv 已存在时 python -m venv 幂等（只补 ensurepip），不会破坏已装依赖
    run("python", ["-m", "venv", "apps/api/.venv"]);
    run(venvPython, ["-m", "pip", "install", "-e", "apps/api[dev]"]);
    break;
  case "serve":
    run(venvPython, ["-m", "uvicorn", "app.main:app", "--port", "8808"], "apps/api");
    break;
  case "test":
    run(venvPython, ["-m", "pytest", "-q", "tests"], "apps/api");
    break;
  default:
    console.error("用法: node scripts/api.mjs <setup|serve|test>");
    process.exit(1);
}
