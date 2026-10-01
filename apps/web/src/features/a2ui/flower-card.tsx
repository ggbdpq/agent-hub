import { CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Link2, ShoppingBag } from "lucide-react";
import type { CSSProperties } from "react";
import type { EvidenceRef, FlowerCardPayload, FlowerItem } from "@shared/index";

/**
 * A2UI 花束推荐卡
 *
 * 结构化输出契约 → 场景化多段式卡片 → 行动路径：
 * - 场景决定段式与文案（daily/gift/comfort）
 * - 价格格式化与类型标签在前端收敛（formatPrice / TYPE_LABEL 映射）
 * - 主题用 CSS 变量（--flora-*），换品牌只换变量表
 * - 每条推荐带依据入口（Link2）→ 依据抽屉
 */

const SCENE_TITLE: Record<FlowerCardPayload["scene"], string> = {
  daily: "日常自用",
  gift: "送礼推荐",
  comfort: "探望慰问",
};

const TYPE_LABEL: Record<string, string> = {
  花束: "鲜花花束",
  盆栽: "绿植盆栽",
  花盒: "礼盒装",
  永生花: "永生花",
};

/** 分 → 元：金额单位用分，展示层才格式化 */
export function formatPrice(fen: number): string {
  return `¥${(fen / 100).toFixed(2).replace(/\.00$/, "")}`;
}

function priceTier(fen: number): string {
  if (fen < 5000) return "入门档";
  if (fen < 15000) return "轻奢档";
  return "高端档";
}

interface Props {
  payload: FlowerCardPayload;
  onOpenEvidence: (evidenceId: string) => void;
  evidenceLabel?: (ref: EvidenceRef) => string;
  onOrder?: (item: FlowerItem) => void;
}

export function FlowerRecommendCard({ payload, onOpenEvidence, evidenceLabel, onOrder }: Props) {
  return (
    <div
      className="rounded-xl border bg-card"
      style={
        {
          "--flora-primary": "#7c5cbf",
          "--flora-accent": "#f3e8ff",
          "--flora-gold": "#b45309",
        } as CSSProperties
      }
    >
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle className="text-base">
            <span aria-hidden className="mr-1">
              💐
            </span>
            {payload.title}
          </CardTitle>
          <Badge variant="secondary" style={{ background: "var(--flora-accent)" }}>
            {SCENE_TITLE[payload.scene]}
          </Badge>
        </div>
        <p className="text-sm text-muted-foreground">{payload.reason}</p>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {payload.items.map((item) => (
          <div key={item.id} className="flex items-center gap-3 rounded-lg border p-3">
            <span className="text-2xl" aria-hidden>
              {item.emoji}
            </span>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="truncate text-sm font-medium">{item.name}</span>
                <Badge variant="outline" className="shrink-0 text-xs">
                  {TYPE_LABEL[item.typeLabel] ?? item.typeLabel}
                </Badge>
              </div>
              <div className="mt-1 flex items-baseline gap-2">
                <span className="text-sm font-semibold" style={{ color: "var(--flora-primary)" }}>
                  {formatPrice(item.priceFen)}
                </span>
                <span className="text-xs text-muted-foreground">{priceTier(item.priceFen)}</span>
              </div>
            </div>
            {onOrder && (
              <Button size="sm" variant="outline" className="gap-1" onClick={() => onOrder(item)}>
                <ShoppingBag className="size-3.5" />
                下单
              </Button>
            )}
          </div>
        ))}

        {/* 依据入口：每条推荐可溯源（证据抽屉） */}
        {payload.evidenceRefs.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 border-t pt-3">
            <span className="text-xs text-muted-foreground">推荐依据：</span>
            {payload.evidenceRefs.map((ref) => (
              <Button
                key={ref.evidenceId}
                size="sm"
                variant="ghost"
                className="h-7 gap-1 px-2 text-xs text-muted-foreground"
                onClick={() => onOpenEvidence(ref.evidenceId)}
              >
                <Link2 className="size-3" />
                {evidenceLabel?.(ref) ?? ref.label}
              </Button>
            ))}
          </div>
        )}
      </CardContent>
    </div>
  );
}
