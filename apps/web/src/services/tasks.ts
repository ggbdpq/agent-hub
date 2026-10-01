import { z } from "zod";
import { request, ApiError } from "@/lib/api-client";
import { TaskSnapshot, TaskStatus } from "@shared/index";

/**
 * 养护方案任务服务
 *
 * 分级超时常量：普通请求 15s / 任务轮询 3s（长耗时场景预留 10min 档）
 */
export const REQUEST_TIMEOUT_MS = {
  normal: 15_000,
  poll: 3_000, /* wave: 600_000（10min 档留位） */
} as const;

/** 后端状态 → 页面态集中映射；未知状态显式报错不静默 */
export const TASK_STATUS_TO_PAGE_STATUS = {
  queued: "waiting",
  running: "processing",
  completed: "success",
  failed: "error",
  cancelled: "cancelled",
  archived: "success",
} as const satisfies Record<z.infer<typeof TaskStatus>, string>;

export type TaskPageStatus =
  (typeof TASK_STATUS_TO_PAGE_STATUS)[keyof typeof TASK_STATUS_TO_PAGE_STATUS];

export function toPageStatus(status: z.infer<typeof TaskStatus>): TaskPageStatus {
  const page = TASK_STATUS_TO_PAGE_STATUS[status];
  if (!page) throw new ApiError("CONTRACT", `未知任务状态：${status}`);
  return page;
}

const taskSnapshotSchema = TaskSnapshot;

export function createTask(signal?: AbortSignal) {
  return request({ url: "/tasks", method: "POST", schema: taskSnapshotSchema, signal });
}

export function getTask(taskId: string, signal?: AbortSignal) {
  return request({
    url: `/tasks/${encodeURIComponent(taskId)}`,
    schema: taskSnapshotSchema,
    signal,
  });
}

export function cancelTask(taskId: string, signal?: AbortSignal) {
  return request({
    url: `/tasks/${encodeURIComponent(taskId)}/cancel`,
    method: "POST",
    schema: taskSnapshotSchema,
    signal,
  });
}

export function confirmTask(taskId: string, signal?: AbortSignal) {
  return request({
    url: `/tasks/${encodeURIComponent(taskId)}/confirm`,
    method: "POST",
    schema: taskSnapshotSchema,
    signal,
  });
}
