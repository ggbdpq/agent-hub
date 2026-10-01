import { registerBones } from "@shared/index";
import agentReply from "./agent-reply.bones.json";
import carePlanCard from "./care-plan-card.bones.json";

/**
 * bones 注册表（入口引入一次，业务组件只按 name 取）。
 * bones fixture 由"捕获断点 + 占位形状"组成，本文件手工维护。
 */
registerBones([agentReply, carePlanCard]);
