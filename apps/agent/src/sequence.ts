// 事件序列约束（02 §2 的 S1–S5，04 §3）：所有层（翻译单测、agent、server、真实路径）复用。
import assert from "node:assert/strict";
import { AgentStreamEvent } from "@agent-hub/shared";

export function assertSequence(evs: AgentStreamEvent[]): void {
  const last = evs.at(-1);
  assert.ok(last && (last.type === "done" || last.type === "error"), "S1: 最后一个事件必须是 done 或 error");
  assert.equal(
    evs.filter((e) => e.type === "done" || e.type === "error").length,
    1,
    "S1: done / error 合计恰好出现一次",
  );
  let maxSeq = 0;
  let closed = false;
  for (const e of evs) {
    if (e.type === "thought") {
      assert.ok(!closed, "S2: thought(final:true) 之后不再出现 thought");
      assert.ok(e.seq >= maxSeq, "S2: thought.seq 单调不减");
      maxSeq = e.seq;
      if (e.final) closed = true;
      continue;
    }
    if ((e.type === "token" || e.type === "card") && maxSeq > 0) {
      assert.ok(closed, "S3: 出现过 thought 时，首个 card/token 之前必有 final:true");
    }
  }
  const card = evs.find((e) => e.type === "card");
  if (card && card.type === "card") {
    const firstCardIndex = evs.indexOf(card);
    const evIds = evs
      .slice(firstCardIndex)
      .filter((e): e is Extract<AgentStreamEvent, { type: "evidence" }> => e.type === "evidence")
      .map((e) => e.evidence.id);
    for (const ref of card.payload.evidenceRefs) {
      assert.ok(evIds.includes(ref.evidenceId), `S4: 卡片引用的证据 ${ref.evidenceId} 必须随后下发`);
    }
  }
}
