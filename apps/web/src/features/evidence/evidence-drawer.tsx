import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Badge } from "@/components/ui/badge";
import { LinkIcon } from "lucide-react";
import type { Evidence } from "@shared/index";

/**
 * 证据抽屉
 *
 * AI 结论可溯源：来源标签 + 可信度分级 + 引用原文 + 时间。
 * 可信度 <80 金色预警是产品信任阈值——前端只做分级渲染，不做计算。
 */

const SOURCE_KIND_LABEL: Record<Evidence["sourceKind"], string> = {
  purchase_history: "订单记录",
  preference_survey: "喜好问卷",
  care_record: "养护回访",
};

function CredibilityBadge({ credibility }: { credibility: number }) {
  const gold = credibility < 80; // 信任阈值：低于 80 金色预警
  return (
    <Badge
      variant="outline"
      className={
        gold ? "border-amber-500/60 text-amber-600" : "border-emerald-500/60 text-emerald-600"
      }
      title={gold ? "可信度低于 80，仅供参考" : "可信度较高"}
    >
      可信度 {credibility}
    </Badge>
  );
}

function EvidenceItem({ evidence }: { evidence: Evidence }) {
  return (
    <div className="flex flex-col gap-2 rounded-lg border p-3">
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 text-sm font-medium">
          <LinkIcon className="size-3.5 text-muted-foreground" />
          {evidence.source}
        </span>
        <CredibilityBadge credibility={evidence.credibility} />
      </div>
      {/* 引用原文：AI 为什么这么说 */}
      <blockquote className="rounded-md bg-muted/60 px-3 py-2 text-sm leading-relaxed text-foreground/90">
        {evidence.quote}
      </blockquote>
      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>{SOURCE_KIND_LABEL[evidence.sourceKind]}</span>
        <span>分析时间 {evidence.analyzedAt}</span>
      </div>
    </div>
  );
}

interface Props {
  open: boolean;
  active: Evidence | null;
  history: Evidence[];
  onOpenChange: (open: boolean) => void;
}

export function EvidenceDrawer({ open, active, history, onOpenChange }: Props) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent>
        <SheetHeader>
          <SheetTitle>推荐依据</SheetTitle>
          <SheetDescription>每条 AI 结论都来自以下记录，点引用可逐层查看。</SheetDescription>
        </SheetHeader>
        <div className="flex flex-col gap-3 overflow-y-auto px-5 pb-5">
          {active && <EvidenceItem evidence={active} />}
          {history
            .filter((e) => e.id !== active?.id)
            .map((e) => (
              <EvidenceItem key={e.id} evidence={e} />
            ))}
          {!active && history.length === 0 && (
            <p className="text-sm text-muted-foreground">暂无依据记录——先在对话里获取一次推荐。</p>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
