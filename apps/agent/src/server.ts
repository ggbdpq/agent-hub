// TS 智能体服务（apps/agent）：node:http，无框架。端口 8809。
// WP0 空壳：只提供 /healthz，其余 404。聊天与证据路由由 WP4 接管。
import { createServer, type Server } from "node:http";

export function startServer(port = Number(process.env.AGENT_PORT ?? 8809)): Promise<Server> {
  const server = createServer((req, res) => {
    const url = new URL(req.url ?? "/", "http://x");
    if (req.method === "GET" && url.pathname === "/healthz") {
      res.end("ok");
      return;
    }
    res.writeHead(404).end();
  });
  return new Promise((resolve) => {
    server.listen(port, "127.0.0.1", () => resolve(server));
  });
}

if (import.meta.main) {
  // 与 FastAPI 共用仓库根 .env（OPENAI_API_KEY 等）；没有 .env 就走离线剧本
  try {
    process.loadEnvFile();
  } catch {
    /* 没有 .env，忽略 */
  }
  const s = await startServer();
  console.log(`agent listening on http://127.0.0.1:${(s.address() as { port: number }).port}`);
}
