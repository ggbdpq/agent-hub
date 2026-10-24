// 重启 E2E 独立命令入口（集成请求 Y3：package.json 建议加
//   "test:e2e:restart": "node scripts/e2e-restart/run.mjs"）。
// 职责：设置重启编排所需环境（离线剧本、回环不走代理、用例门闩）后拉起 playwright，
// 只跑 e2e/inspection-restart.spec.ts。跨平台：参数数组 spawn，不经 shell。
import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const cli = resolve(root, "node_modules/@playwright/test/cli.js");

const result = spawnSync(
  process.execPath,
  [cli, "test", "e2e/inspection-restart.spec.ts", ...process.argv.slice(2)],
  {
    cwd: root,
    env: {
      ...process.env,
      AGENT_HUB_E2E_RESTART: "1", // 打开 spec 门闩（runner 审批恢复补丁合入后可移除）
      AGENT_HUB_LLM: "scripted",
      OPENAI_API_KEY: "",
      OPENAI_PROVIDER: "",
      NO_PROXY: "localhost,127.0.0.1",
      no_proxy: "localhost,127.0.0.1",
    },
    stdio: "inherit",
    shell: false,
  },
);
if (result.error) {
  console.error(`启动失败: ${result.error.message}`);
  process.exit(1);
}
process.exit(result.status ?? 1);
