import assert from "node:assert/strict";
import { test } from "node:test";
import { boneStyle, pickBreakpoint, registerBones, getBones } from "./bones.ts";
import type { BonesFixture } from "./bones.ts";

const fixture: BonesFixture = {
  name: "demo",
  breakpoints: {
    "640": { viewportWidth: 640, height: 200, bones: [] },
    "1280": { viewportWidth: 1280, height: 400, bones: [] },
    "1920": { viewportWidth: 1920, height: 500, bones: [] },
  },
};

test("pickBreakpoint：选最接近容器宽度的捕获断点", () => {
  assert.equal(pickBreakpoint(fixture, 600).viewportWidth, 640);
  assert.equal(pickBreakpoint(fixture, 700).viewportWidth, 640); // 与 640 差 60 < 与 1280 差 580
  assert.equal(pickBreakpoint(fixture, 1100).viewportWidth, 1280);
  assert.equal(pickBreakpoint(fixture, 5000).viewportWidth, 1920);
  assert.equal(pickBreakpoint(fixture, 1).viewportWidth, 640);
});

test("pickBreakpoint：并列距离取更小断点（宁窄勿溢出）", () => {
  // 960 与 640/1280 等距 → 取 640
  assert.equal(pickBreakpoint(fixture, 960).viewportWidth, 640);
});

test("pickBreakpoint：空断点显式报错", () => {
  assert.throws(() => pickBreakpoint({ name: "empty", breakpoints: {} }, 100));
});

test("boneStyle：px 按捕获高度换算为百分比，宽度百分比直出", () => {
  const style = boneStyle({ x: 6.7, y: 45, w: "50%", h: 20 }, 200);
  assert.equal(style.left, "6.7%");
  assert.equal(style.top, "22.5%"); // 45/200
  assert.equal(style.width, "50%");
  assert.equal(style.height, "10%"); // 20/200
  assert.equal(style.borderRadius, "8px");
});

test("boneStyle：circle 忽略圆角半径", () => {
  const style = boneStyle({ x: 0, y: 0, w: 10, h: 40, r: 8, circle: true }, 400);
  assert.equal(style.borderRadius, "9999px");
});

test("registry：注册后按 name 取用", () => {
  registerBones([fixture]);
  assert.equal(getBones("demo"), fixture);
  assert.equal(getBones("missing"), undefined);
});
