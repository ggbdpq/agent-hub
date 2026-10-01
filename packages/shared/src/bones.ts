/**
 * boneyard 骨架数据模型与断点选择（纯逻辑，供 web 边界组件消费）
 *
 * - bones fixture 按断点（视口宽度）组织，每根 bone 是相对边界盒的占位形状；
 * - 运行时按容器宽度选最接近的捕获断点，骨架随真实布局伸缩；
 * - 形状定位全部用百分比（x/w 相对宽度，y/h 相对捕获高度），overlay 任意容器高度等比适配。
 */

export interface Bone {
  /** 左偏移（容器宽度百分比） */
  x: number;
  /** 上偏移（捕获高度内的 px，渲染时换算为百分比） */
  y: number;
  /** 宽度（容器宽度百分比，或 "50%" 行内半宽标记——渲染等价于数字 50） */
  w: number | `${number}%`;
  /** 高度（捕获高度内的 px，渲染时换算为百分比） */
  h: number;
  /** 圆角半径 px（默认 8） */
  r?: number;
  /** 圆形（头像/图标位），忽略 r */
  circle?: boolean;
}

export interface BonesBreakpoint {
  /** 捕获时的视口宽度 key（如 "1280"） */
  viewportWidth: number;
  /** 捕获时边界盒高度（px），用于把 y/h 换算为百分比 */
  height: number;
  bones: Bone[];
}

export interface BonesFixture {
  name: string;
  breakpoints: Record<string, BonesBreakpoint>;
}

/**
 * 选最接近容器宽度的捕获断点（并列时取更小的——宁要略窄不要溢出）。
 * 纯函数：给定任意 fixture 与宽度，结果可断言（单测覆盖）。
 */
export function pickBreakpoint(fixture: BonesFixture, containerWidth: number): BonesBreakpoint {
  const entries = Object.values(fixture.breakpoints);
  if (entries.length === 0) throw new Error(`boneyard fixture 无断点：${fixture.name}`);
  const sorted = [...entries].sort((a, b) => a.viewportWidth - b.viewportWidth);
  let best = sorted[0];
  for (const bp of sorted) {
    if (
      Math.abs(bp.viewportWidth - containerWidth) < Math.abs(best.viewportWidth - containerWidth)
    ) {
      best = bp;
    }
  }
  return best;
}

/** bone 渲染样式换算（百分比定位，overlay 任意高度等比适配） */
export function boneStyle(
  bone: Bone,
  breakpointHeight: number,
): {
  left: string;
  top: string;
  width: string;
  height: string;
  borderRadius: string;
} {
  const width = typeof bone.w === "string" ? parseFloat(bone.w) : bone.w;
  return {
    left: `${bone.x}%`,
    top: `${(bone.y / breakpointHeight) * 100}%`,
    width: `${width}%`,
    height: `${(bone.h / breakpointHeight) * 100}%`,
    borderRadius: bone.circle ? "9999px" : `${bone.r ?? 8}px`,
  };
}

/* ------------------------------ 运行时注册表 ------------------------------ */

const registry = new Map<string, BonesFixture>();

/** 全局注册（入口处 import 一次 fixture，组件只按 name 取） */
export function registerBones(fixtures: BonesFixture[]): void {
  for (const fixture of fixtures) registry.set(fixture.name, fixture);
}

export function getBones(name: string): BonesFixture | undefined {
  return registry.get(name);
}
