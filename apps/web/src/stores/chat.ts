import { create } from "zustand";
import { AgentStreamEvent } from "@shared/index";
import type { Evidence, FlowerCardPayload } from "@shared/index";
import { streamAgentChat } from "@/services/agent";

/**
 * 对话 store（Zustand 分片）
 *
 * 一次回答 = 有序 segment 序列（thought[] → card → evidence[] → token[]），
 * 与 SSE 事件协议同构：流式事件到达即追加对应 segment，UI 只做投影。
 * 事件入 store 前过 AgentStreamEvent.parse——契约白名单，解析失败按协议错误处理。
 */

export type Segment =
  | { kind: "thought"; seq: number; text: string; final: boolean }
  | { kind: "card"; payload: FlowerCardPayload }
  | { kind: "evidence"; evidence: Evidence }
  | { kind: "token"; text: string };

export type StreamingPhase = "idle" | "thinking" | "answering" | "done" | "error";

export interface ChatMessage {
  id: string;
  role: "user" | "agent";
  /** agent 消息：流式段序列；user 消息：text */
  segments: Segment[];
  text?: string;
  error?: string;
}

interface ChatState {
  messages: ChatMessage[];
  phase: StreamingPhase;
  /** 依据抽屉是全局单例：active 为空展示逐层依据历史 */
  evidenceDrawer: { open: boolean; active: Evidence | null; history: Evidence[] };
  sendMessage: (text: string) => Promise<void>;
  abort: () => void;
  reset: () => void;
  openEvidence: (evidenceId: string) => void;
  closeEvidence: () => void;
}

let idCounter = 0;
const nextId = (prefix: string) => `${prefix}-${Date.now()}-${++idCounter}`;

export const useChatStore = create<ChatState>((set, get) => {
  // "停止生成"控制器：store 闭包内的普通变量（单例）。
  // 注意不能用 useRef——这里不是 React 组件，Hooks 规则不允许（否则 Invalid hook call）。
  let abortRef: AbortController | null = null;

  const patchAgentMessage = (agentId: string, patch: (m: ChatMessage) => ChatMessage) =>
    set((s) => ({
      messages: s.messages.map((m) => (m.id === agentId ? patch(m) : m)),
    }));

  const appendSegment = (agentId: string, segment: Segment) =>
    patchAgentMessage(agentId, (m) => ({ ...m, segments: [...m.segments, segment] }));

  const upsertThought = (agentId: string, seq: number, text: string, final: boolean) =>
    patchAgentMessage(agentId, (m) => {
      const idx = m.segments.findIndex((seg) => seg.kind === "thought" && seg.seq === seq);
      if (idx === -1) {
        return { ...m, segments: [...m.segments, { kind: "thought", seq, text, final }] };
      }
      return {
        ...m,
        segments: m.segments.map((seg, i) =>
          i === idx && seg.kind === "thought" ? { ...seg, text: seg.text + text, final } : seg,
        ),
      };
    });

  return {
    messages: [],
    phase: "idle",
    evidenceDrawer: { open: false, active: null, history: [] },

    sendMessage: async (text) => {
      if (get().phase === "thinking" || get().phase === "answering") return;

      const userMessage: ChatMessage = { id: nextId("u"), role: "user", segments: [], text };
      const agentId = nextId("a");
      const agentMessage: ChatMessage = { id: agentId, role: "agent", segments: [] };
      set((s) => ({ messages: [...s.messages, userMessage, agentMessage], phase: "thinking" }));

      abortRef?.abort();
      abortRef = new AbortController();

      try {
        for await (const event of streamAgentChat(text, abortRef.signal)) {
          const parsed = AgentStreamEvent.parse(event);
          switch (parsed.type) {
            case "thought":
              set({ phase: "thinking" });
              upsertThought(agentId, parsed.seq, parsed.text, parsed.final);
              break;
            case "card":
              set({ phase: "answering" });
              appendSegment(agentId, { kind: "card", payload: parsed.payload });
              break;
            case "evidence":
              appendSegment(agentId, { kind: "evidence", evidence: parsed.evidence });
              break;
            case "token": {
              set({ phase: "answering" });
              // 连续 token 合并进最后一个 token 段（打字机）
              const target = get().messages.find((m) => m.id === agentId);
              const last = target?.segments.at(-1);
              if (last?.kind === "token") {
                patchAgentMessage(agentId, (m) => ({
                  ...m,
                  segments: m.segments.map((seg, i) =>
                    i === m.segments.length - 1 && seg.kind === "token"
                      ? { ...seg, text: seg.text + parsed.text }
                      : seg,
                  ),
                }));
              } else {
                appendSegment(agentId, { kind: "token", text: parsed.text });
              }
              break;
            }
            case "done":
              set({ phase: "done" });
              break;
            case "error":
              patchAgentMessage(agentId, (m) => ({ ...m, error: parsed.message }));
              set({ phase: "error" });
              return;
          }
        }
        if (get().phase !== "done") set({ phase: "done" });
      } catch (error) {
        if (get().phase === "idle") return; // 用户主动 abort
        // 错误分级展示：契约违规不暴露原始 JSON 给用户，控制台保留细节
        const message =
          error instanceof Error && error.name === "ZodError"
            ? "智能体返回了契约之外的数据，已拦截展示"
            : error instanceof Error
              ? error.message
              : String(error);
        console.error("[agent] 流式事件异常:", error);
        patchAgentMessage(agentId, (m) => ({ ...m, error: message }));
        set({ phase: "error" });
      }
    },

    abort: () => {
      abortRef?.abort();
      set({ phase: "idle" });
    },

    reset: () => {
      abortRef?.abort();
      set({ messages: [], phase: "idle" });
    },

    openEvidence: (evidenceId) => {
      // 结论与依据分离：证据流到达时已缓存进 segment，抽屉按 id 装配
      const found = get()
        .messages.flatMap((m) => m.segments)
        .find(
          (seg): seg is Extract<Segment, { kind: "evidence" }> =>
            seg.kind === "evidence" && seg.evidence.id === evidenceId,
        );
      if (!found) return;
      set((s) => ({
        evidenceDrawer: {
          open: true,
          active: found.evidence,
          history: [
            ...s.evidenceDrawer.history.filter((e) => e.id !== found.evidence.id),
            found.evidence,
          ],
        },
      }));
    },

    closeEvidence: () => set((s) => ({ evidenceDrawer: { ...s.evidenceDrawer, open: false } })),
  };
});
