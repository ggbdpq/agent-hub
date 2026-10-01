import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Badge } from "@/components/ui/badge";
import { toPageStatus, createTask, confirmTask, getTask, cancelTask } from "@/services/tasks";
import { Boneyard } from "@/features/boneyard/boneyard";
import type { TaskSnapshot } from "@shared/index";
import { CheckCircle2, Loader2, RefreshCw, XCircle } from "lucide-react";
import { toast } from "sonner";

/**
 * 养护方案任务页（异步 AI 任务状态机演示）
 *
 * 轮询 3s 一档（POLL_INTERVAL），taskId 存 localStorage：
 * 刷新/重进按远端详情恢复（远端是事实源，本地只存 UI 态）。
 */

const POLL_INTERVAL_MS = 3_000;
const TASK_ID_KEY = "agent-hub:care-plan-task-id";

const PAGE_STATUS_META = {
  waiting: { label: "排队中", tone: "secondary" as const },
  processing: { label: "生成中", tone: "default" as const },
  success: { label: "已完成", tone: "outline" as const },
  error: { label: "失败", tone: "destructive" as const },
  cancelled: { label: "已取消", tone: "outline" as const },
};

export function TasksPage() {
  const [task, setTask] = useState<TaskSnapshot | null>(null);
  const [restoring, setRestoring] = useState(false);
  const [polling, setPolling] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const stopPolling = useCallback(() => {
    setPolling(false);
    if (timerRef.current) clearTimeout(timerRef.current);
  }, []);

  const startPolling = useCallback(
    (taskId: string) => {
      setPolling(true);
      const tick = async () => {
        try {
          const snapshot = await getTask(taskId);
          setTask(snapshot);
          if (["completed", "failed", "cancelled", "archived"].includes(snapshot.status)) {
            stopPolling();
            return;
          }
          timerRef.current = setTimeout(tick, POLL_INTERVAL_MS);
        } catch (error) {
          toast.error(error instanceof Error ? error.message : String(error));
          stopPolling();
        }
      };
      void tick();
    },
    [stopPolling],
  );

  // 进页恢复：localStorage 有 taskId 就以远端为准拉详情（恢复期展示 boneyard 骨架）
  useEffect(() => {
    const saved = localStorage.getItem(TASK_ID_KEY);
    if (!saved) return;
    setRestoring(true);
    getTask(saved)
      .then(setTask)
      .catch(() => localStorage.removeItem(TASK_ID_KEY))
      .finally(() => setRestoring(false));
    return () => stopPolling();
  }, [stopPolling]);

  const updateTask = (snapshot: TaskSnapshot) => {
    setTask(snapshot);
    localStorage.setItem(TASK_ID_KEY, snapshot.taskId);
  };

  const handleCreate = async () => {
    const snapshot = await createTask();
    updateTask(snapshot);
    startPolling(snapshot.taskId);
  };

  const handleCancel = async () => {
    if (!task) return;
    try {
      updateTask(await cancelTask(task.taskId));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error));
      return;
    }
    stopPolling();
  };

  const handleConfirm = async () => {
    if (!task) return;
    updateTask(await confirmTask(task.taskId));
    toast.success("已保存到我的养护方案");
  };

  const pageStatus = task ? toPageStatus(task.status) : null;
  const meta = pageStatus ? PAGE_STATUS_META[pageStatus] : null;
  const isTerminal = task
    ? ["completed", "failed", "cancelled", "archived"].includes(task.status)
    : false;

  return (
    <div className="mx-auto w-full max-w-2xl space-y-4 overflow-y-auto p-5">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">养护方案生成</CardTitle>
          <p className="text-sm text-muted-foreground">
            异步 AI 长任务演示：创建后服务端逐步推进进度，前端 3
            秒轮询；任务可取消、完成后确认归档。
          </p>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center gap-2">
            <Button onClick={() => void handleCreate()} disabled={polling}>
              {polling ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <RefreshCw className="size-4" />
              )}
              创建生成任务
            </Button>
            {task && !isTerminal && (
              <Button variant="outline" onClick={() => void handleCancel()}>
                取消任务
              </Button>
            )}
            {task?.status === "completed" && (
              <Button onClick={() => void handleConfirm()}>确认保存</Button>
            )}
          </div>

          {restoring && (
            <Boneyard name="care-plan-card" loading>
              <div className="rounded-lg border p-4" style={{ minHeight: 240 }}>
                <p className="text-sm text-muted-foreground">正在恢复任务状态…</p>
              </div>
            </Boneyard>
          )}

          {task && (
            <div className="space-y-3 rounded-lg border p-4">
              <div className="flex items-center justify-between">
                <span className="font-mono text-xs text-muted-foreground">单号 {task.taskNo}</span>
                {meta && <Badge variant={meta.tone}>{meta.label}</Badge>}
              </div>
              {["queued", "running"].includes(task.status) && <Progress value={task.progress} />}
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                {task.status === "running" && <Loader2 className="size-3.5 animate-spin" />}
                {task.status === "completed" && (
                  <CheckCircle2 className="size-4 text-emerald-600" />
                )}
                {task.status === "failed" && <XCircle className="size-4 text-destructive" />}
                进度 {task.progress}%
              </div>
              {task.failReason && (
                <p className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
                  {task.failReason}
                </p>
              )}
              {task.result && (
                <div className="space-y-3 border-t pt-3">
                  <p className="text-sm font-medium">{task.result.title}</p>
                  {task.result.sections.map((section) => (
                    <div key={section.heading}>
                      <p className="text-sm font-medium">{section.heading}</p>
                      <p className="text-sm text-muted-foreground">{section.body}</p>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
