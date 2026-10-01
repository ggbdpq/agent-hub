import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { FlowerRecommendCard } from "@/features/a2ui/flower-card";
import { EvidenceDrawer } from "@/features/evidence/evidence-drawer";
import { Boneyard } from "@/features/boneyard/boneyard";
import { useChatStore } from "@/stores/chat";
import { createOrder } from "@/services/orders";
import { toast } from "sonner";
import { ChevronDown, ChevronRight, Send, Square } from "lucide-react";
import { cn } from "@/lib/utils";
import type { Segment } from "@/stores/chat";

/**
 * 花语顾问对话页
 *
 * UI 投影：messages/segments 由 store 管理，页面只做渲染。
 * 思考段 = 可折叠 ThoughtPanel（完成后自动折叠：过程可见建立信任，折叠保住效率）。
 */

const QUICK_ASKS = ["日常自用来点花", "想给妈妈送生日礼物", "去医院探望长辈买什么花"];

function ThoughtPanel({ seq, text, final }: { seq: number; text: string; final: boolean }) {
  const [collapsed, setCollapsed] = useState(false);
  const wasFinal = useRef(final);
  // 思考完成后自动折叠（深度思考渲染：过程可见建立信任，折叠保住效率）
  useEffect(() => {
    if (final && !wasFinal.current) setCollapsed(true);
    wasFinal.current = final;
  }, [final]);

  return (
    <div className="rounded-lg border border-dashed bg-muted/30">
      <button
        type="button"
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs text-muted-foreground"
        onClick={() => setCollapsed((c) => !c)}
      >
        {collapsed ? <ChevronRight className="size-3.5" /> : <ChevronDown className="size-3.5" />}
        思考过程 · 第 {seq} 段{final ? "" : "（思考中…）"}
      </button>
      {!collapsed && (
        <p className="px-4 pb-3 text-sm leading-relaxed text-muted-foreground">{text}</p>
      )}
    </div>
  );
}

function SegmentView({
  segment,
  onOpenEvidence,
  onOrder,
}: {
  segment: Segment;
  onOpenEvidence: (id: string) => void;
  onOrder: (item: { name: string; priceFen: number }) => void;
}) {
  switch (segment.kind) {
    case "thought":
      return <ThoughtPanel seq={segment.seq} text={segment.text} final={segment.final} />;
    case "card":
      return (
        <FlowerRecommendCard
          payload={segment.payload}
          onOpenEvidence={onOpenEvidence}
          onOrder={onOrder}
        />
      );
    case "token":
      return <p className="text-sm leading-relaxed">{segment.text}</p>;
    case "evidence":
      // 证据以抽屉呈现；消息流里不重复展示正文（卡片上的"依据"入口负责打开）
      return null;
  }
}

export function ChatPage() {
  const {
    messages,
    phase,
    evidenceDrawer,
    sendMessage,
    abort,
    reset,
    openEvidence,
    closeEvidence,
  } = useChatStore();
  const [input, setInput] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);
  const busy = phase === "thinking" || phase === "answering";

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const submit = (text: string) => {
    const trimmed = text.trim();
    if (!trimmed || busy) return;
    setInput("");
    void sendMessage(trimmed);
  };

  const handleOrder = (item: { name: string; priceFen: number }) => {
    void createOrder({ flowerName: item.name, amountFen: item.priceFen, channel: "wechat" })
      .then((order) =>
        toast.success(`已创建订单 ${order.orderNo}`, {
          description: "到「订单支付」页完成模拟支付",
        }),
      )
      .catch((error: Error) => toast.error(error.message));
  };

  return (
    <div className="mx-auto flex h-full w-full max-w-3xl flex-col">
      {/* 消息流 */}
      <div className="min-h-0 flex-1 space-y-5 overflow-y-auto p-5">
        {messages.length === 0 && (
          <div className="flex h-full flex-col items-center justify-center gap-4 text-center">
            <p className="text-lg font-medium">Hi，我是花语顾问 🌸</p>
            <p className="max-w-sm text-sm text-muted-foreground">
              说说你的用花场景，我会结合你的购买记录给出推荐——每条推荐都能点开看依据。
            </p>
            <div className="flex flex-wrap justify-center gap-2">
              {QUICK_ASKS.map((q) => (
                <Button key={q} variant="outline" size="sm" onClick={() => submit(q)}>
                  {q}
                </Button>
              ))}
            </div>
          </div>
        )}

        {messages.map((message) =>
          message.role === "user" ? (
            <div key={message.id} className="flex justify-end">
              <div className="max-w-[80%] rounded-2xl rounded-br-sm bg-primary px-4 py-2 text-sm text-primary-foreground">
                {message.text}
              </div>
            </div>
          ) : (
            <div key={message.id} className="flex justify-start">
              <div className="w-full max-w-[92%]">
                {/* boneyard 边界：等待首个流式段时展示骨架，真实内容保持挂载（无 DOM 盒） */}
                <Boneyard
                  name="agent-reply"
                  loading={message.segments.length === 0 && !message.error && phase !== "idle"}
                >
                  <div className="space-y-3">
                    {message.segments.map((segment, i) => (
                      <SegmentView
                        key={i}
                        segment={segment}
                        onOpenEvidence={openEvidence}
                        onOrder={handleOrder}
                      />
                    ))}
                  </div>
                </Boneyard>
                {message.error && (
                  <p className="mt-2 rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                    {message.error}
                  </p>
                )}
              </div>
            </div>
          ),
        )}
        <div ref={bottomRef} />
      </div>

      {/* 输入区 */}
      <div className="shrink-0 border-t p-4">
        <div className="mx-auto flex max-w-3xl items-center gap-2">
          <Input
            value={input}
            placeholder={busy ? "花语顾问思考中…" : "说说你的用花场景，例如「送闺蜜生日花束」"}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit(input)}
            disabled={busy}
          />
          {busy ? (
            <Button variant="destructive" size="icon" onClick={abort} title="停止生成">
              <Square className="size-4" />
            </Button>
          ) : (
            <Button size="icon" onClick={() => submit(input)} disabled={!input.trim()} title="发送">
              <Send className={cn("size-4")} />
            </Button>
          )}
          {messages.length > 0 && (
            <Button variant="ghost" size="sm" onClick={reset}>
              清空
            </Button>
          )}
        </div>
      </div>

      <EvidenceDrawer
        open={evidenceDrawer.open}
        active={evidenceDrawer.active}
        history={evidenceDrawer.history}
        onOpenChange={(open) => (open ? undefined : closeEvidence())}
      />
    </div>
  );
}
