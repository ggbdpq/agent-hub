import { useLayoutEffect, useRef, useState } from "react";
import { boneStyle, getBones, pickBreakpoint } from "@shared/index";
import { cn } from "@/lib/utils";
import "@/features/boneyard/register-bones";

/**
 * Boneyard 骨架屏边界组件
 *
 * 三个签名特性：
 * 1. 真实内容保持挂载：content 层 `display: contents`（无 DOM 盒）——业务布局零侵入，
 *    骨架消失时无重排跳动；
 * 2. 骨架 overlay `pointer-events: none`：纯视觉占位，不拦截滚动与点击；
 * 3. URL 预览验收：`?boneyard=all` 强制全部边界进入 loading，`?boneyard=agent-reply`
 *    只命中指定边界——本地验收与 bones 采集用。
 *
 * 骨架形状（bones fixture）按断点捕获，运行时按容器宽度选最接近断点、
 * 百分比定位等比伸缩；业务接入只需 name + loading。
 */

/** URL 预览参数 */
function shouldForcePreview(name: string): boolean {
  if (typeof window === "undefined") return false;
  const value = new URLSearchParams(window.location.search).get("boneyard");
  if (!value) return false;
  if (value === "all") return true;
  return value
    .split(",")
    .map((item) => item.trim())
    .includes(name);
}

interface BoneyardProps {
  /** 全局唯一边界名（不含斜杠），对应注册的 bones fixture */
  name: string;
  /** 业务自己的接口等待态 */
  loading: boolean;
  /** 页面级灰度/回滚开关：false 时完全绕过骨架 */
  enabled?: boolean;
  children: React.ReactNode;
  className?: string;
}

export function Boneyard({ name, loading, enabled = true, children, className }: BoneyardProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);

  useLayoutEffect(() => {
    const el = rootRef.current;
    if (!el) return;
    setWidth(el.clientWidth);
    const observer = new ResizeObserver((entries) => {
      const next = entries[0]?.contentRect.width;
      if (next !== undefined) setWidth(next);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  if (!enabled) return <>{children}</>;

  const skeletonLoading = loading || shouldForcePreview(name);
  const fixture = getBones(name);

  return (
    <div ref={rootRef} className={cn("relative", className)} data-boneyard-root={name}>
      {/* 真实内容：无盒包裹，布局角色不变 */}
      <div data-boneyard-content className="contents">
        {children}
      </div>
      {/* 骨架 overlay：纯视觉，不拦截交互 */}
      {skeletonLoading && fixture && width > 0 && (
        <div
          data-boneyard-overlay
          aria-hidden
          className="pointer-events-none absolute inset-0 z-10 overflow-hidden rounded-[inherit] bg-background"
        >
          {pickBreakpoint(fixture, width).bones.map((bone, index) => (
            <div
              key={index}
              className="absolute animate-pulse bg-muted"
              style={boneStyle(bone, pickBreakpoint(fixture, width).height)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
