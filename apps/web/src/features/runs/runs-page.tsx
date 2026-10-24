import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import type { RunEvent, RunRecord } from "@shared/index";
import type { InspectionReport } from "@shared/index";
import {
  createRun,
  getRun,
  resolveApproval,
  reportOf,
  saveResumeAnchor,
  loadResumeAnchor,
  clearResumeAnchor,
  streamRunEvents,
  type StreamHandle,
} from "@/services/runs";

/**
 * 巡检运行时间线页（I7）
 *
 * - 创建：站点 + 设备 ID 列表（逗号分隔）+ 巡检目标；
 * - 时间线：按节点分组，默认只显示阶段与结论，工具调用折叠；
 * - 审批：approval.required 出现即显示审批条，决定经 POST 下发（结果只消费一次）；
 * - 刷新恢复：localStorage 存 runId + lastSeq，重连带 Last-Event-ID 续传（ADR-0008）。
 */

/** 阶段级事件直接展示；工具级事件折叠（默认收起） */
const NODE_ORDER = ["supervisor", "planner", "inspector", "reporter"] as const;
const NODE_LABEL: Record<string, string> = {
  supervisor: "调度 Supervisor",
  planner: "规划 Planner",
  inspector: "巡检分析 Inspector",
  reporter: "报告与审批 Reporter",
  "-": "运行",
};
const TOOL_EVENTS = new Set(["tool.called", "tool.result", "tool.denied", "tool.deduplicated"]);

interface ApprovalRequest {
  approvalId: string;
  question: string;
  devices: string[];
  resolved: boolean;
}

function eventNode(event: RunEvent): string {
  return event.node ?? "-";
}

function summarizeEvent(event: RunEvent): string | null {
  // 阶段与结论级事件的一句话摘要；工具事件折叠区展示，metric 走专用徽标
  switch (event.type) {
    case "run.queued":
      return "任务已创建，等待认领";
    case "run.started":
      return "Worker 认领，开始执行";
    case "run.resumed":
      return `续跑（第 ${event.attempt} 次执行）`;
    case "run.completed":
      return "巡检完成";
    case "run.failed":
      return `执行失败：${String(event.payload.error ?? "未知错误")}`;
    case "run.cancelled":
      return "已取消";
    case "run.partial":
      return "部分完成（子任务降级，见覆盖率）";
    case "plan.created":
      return `规划完成：${Array.isArray(event.payload.steps) ? event.payload.steps.length : "?"} 台设备`;
    case "approval.required":
      return "等待人工审批";
    case "approval.resolved":
      return event.payload.approved ? "审批通过" : "审批拒绝";
    default:
      return null;
  }
}

function groupByNode(events: RunEvent[]): Map<string, RunEvent[]> {
  const groups = new Map<string, RunEvent[]>();
  for (const event of events) {
    const node = eventNode(event);
    const bucket = groups.get(node);
    if (bucket) bucket.push(event);
    else groups.set(node, [event]);
  }
  return groups;
}

function NodeSection({ node, events }: { node: string; events: RunEvent[] }) {
  const [toolsOpen, setToolsOpen] = useState(false);
  const toolEvents = events.filter((e) => TOOL_EVENTS.has(e.type));
  const phaseEvents = events.filter((e) => !TOOL_EVENTS.has(e.type) && e.type !== "metric.usage");
  const summaries = phaseEvents
    .map(summarizeEvent)
    .filter((s): s is string => s !== null);

  return (
    <section className="rounded-lg border p-3">
      <div className="mb-2 flex items-center gap-2">
        <Badge variant="outline">{NODE_LABEL[node] ?? node}</Badge>
        <span className="text-xs text-muted-foreground">{events.length} 个事件</span>
      </div>
      <ul className="space-y-1 text-sm">
        {summaries.map((line, i) => (
          <li key={`${node}-s-${i}`} className="text-foreground/90">
            · {line}
          </li>
        ))}
      </ul>
      {toolEvents.length > 0 ? (
        <div className="mt-2">
          <button
            type="button"
            className="text-xs text-muted-foreground underline underline-offset-2"
            onClick={() => setToolsOpen((open) => !open)}
          >
            {toolsOpen ? "收起工具调用" : `展开工具调用（${toolEvents.length}）`}
          </button>
          {toolsOpen ? (
            <ul className="mt-1 space-y-0.5 font-mono text-xs text-muted-foreground">
              {toolEvents.map((e) => (
                <li key={`tool-${e.seq}`}>
                  #{e.seq} {e.type} {JSON.stringify(e.payload).slice(0, 120)}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

function Timeline({ events }: { events: RunEvent[] }) {
  const groups = useMemo(() => groupByNode(events), [events]);
  const ordered = [...groups.entries()].sort(
    (a, b) => NODE_ORDER.indexOf(a[0] as (typeof NODE_ORDER)[number]) -
      NODE_ORDER.indexOf(b[0] as (typeof NODE_ORDER)[number]),
  );
  return (
    <div className="space-y-3">
      {ordered.map(([node, nodeEvents]) => (
        <NodeSection key={node} node={node} events={nodeEvents} />
      ))}
    </div>
  );
}

function ReportView({ report }: { report: InspectionReport }) {
  const levelColor: Record<string, string> = {
    normal: "bg-emerald-100 text-emerald-800",
    warning: "bg-amber-100 text-amber-800",
    critical: "bg-red-100 text-red-800",
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          巡检报告
          <Badge variant="outline">{report.status}</Badge>
          <span className="text-xs font-normal text-muted-foreground">
            覆盖率 {Math.round(report.coverage * 100)}%
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        {report.findings.map((f) => (
          <div key={f.deviceId} className="flex items-start gap-2">
            <span className={`rounded px-1.5 py-0.5 text-xs ${levelColor[f.level] ?? ""}`}>{f.level}</span>
            <div>
              <div className="font-medium">{f.deviceId}</div>
              <div className="text-muted-foreground">{f.summary}</div>
              {f.evidenceChunkIds.length > 0 ? (
                <div className="font-mono text-xs text-muted-foreground">
                  证据：{f.evidenceChunkIds.join("，")}
                </div>
              ) : null}
            </div>
          </div>
        ))}
        {report.workOrders.length > 0 ? (
          <div className="border-t pt-2">
            {report.workOrders.map((o) => (
              <div key={o.deviceId} className="text-sm">
                工单：{o.title}（{o.deviceId}，{o.status}）—— {o.action}
              </div>
            ))}
          </div>
        ) : null}
        {report.notes ? <div className="text-xs text-muted-foreground">{report.notes}</div> : null}
      </CardContent>
    </Card>
  );
}

export function RunsPage() {
  const [site, setSite] = useState("SITE-A");
  const [deviceIds, setDeviceIds] = useState("INV-001, INV-005, INV-002");
  const [goal, setGoal] = useState("例行巡检：遥测比对与异常判读");
  const [record, setRecord] = useState<RunRecord | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [pendingApproval, setPendingApproval] = useState<ApprovalRequest | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const streamRef = useRef<StreamHandle | null>(null);
  const lastSeqRef = useRef(0);

  const terminal = record !== null && ["completed", "failed", "cancelled", "partial"].includes(record.status);

  const ingest = useCallback((event: RunEvent) => {
    lastSeqRef.current = Math.max(lastSeqRef.current, event.seq);
    setEvents((prev) => [...prev, event]);
    if (event.type === "approval.required") {
      const payload = event.payload as { approvalId?: string; question?: string; devices?: string[] };
      setPendingApproval({
        approvalId: String(payload.approvalId ?? ""),
        question: String(payload.question ?? "是否批准？"),
        devices: Array.isArray(payload.devices) ? payload.devices.map(String) : [],
        resolved: false,
      });
    }
    if (event.type === "approval.resolved") {
      setPendingApproval((current) => (current ? { ...current, resolved: true } : current));
    }
  }, []);

  /** 订阅事件流；终态后拉详情取报告并清理恢复锚点 */
  const follow = useCallback((runId: string, lastSeq: number) => {
    streamRef.current?.abort();
    const handle = streamRunEvents(runId, {
      lastSeq,
      onEvent: (event) => {
        ingest(event);
        saveResumeAnchor({ runId, lastSeq: lastSeqRef.current });
      },
    });
    streamRef.current = handle;
    void handle.promise
      .then(async () => {
        const fresh = await getRun(runId);
        setRecord(fresh);
        if (["completed", "failed", "cancelled", "partial"].includes(fresh.status)) {
          clearResumeAnchor();
        } else {
          // 服务端提前断流（罕见）：以最后 seq 重连续传
          follow(runId, lastSeqRef.current);
        }
      })
      .catch((err: unknown) => {
        if ((err as Error).name !== "AbortError") {
          setError(String((err as Error).message ?? err));
        }
      });
  }, [ingest]);

  /** 刷新恢复：localStorage 有锚点则拉详情 + Last-Event-ID 续传 */
  useEffect(() => {
    const anchor = loadResumeAnchor();
    if (!anchor) return;
    let cancelled = false;
    lastSeqRef.current = anchor.lastSeq;
    void getRun(anchor.runId)
      .then((fresh) => {
        if (cancelled) return;
        setRecord(fresh);
        if (["completed", "failed", "cancelled", "partial"].includes(fresh.status)) {
          clearResumeAnchor();
        } else {
          // 刷新后内存丢失：从 0 重放全量事件重建时间线与未决审批条（事件幂等、
          // 渲染幂等）；Last-Event-ID 续传用于页内断线重连（lastSeqRef 仍在）
          follow(anchor.runId, 0);
        }
      })
      .catch(() => clearResumeAnchor());
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => () => streamRef.current?.abort(), []);

  const start = useCallback(async () => {
    setError(null);
    setStarting(true);
    setEvents([]);
    setPendingApproval(null);
    lastSeqRef.current = 0;
    try {
      const ids = deviceIds
        .split(/[,，\s]+/)
        .map((s) => s.trim())
        .filter(Boolean);
      const created = await createRun({ site: site.trim(), deviceIds: ids, goal: goal.trim() || null });
      setRecord(created);
      saveResumeAnchor({ runId: created.runId, lastSeq: 0 });
      follow(created.runId, 0);
    } catch (err) {
      setError(String((err as Error).message ?? err));
    } finally {
      setStarting(false);
    }
  }, [site, deviceIds, goal, follow]);

  const decide = useCallback(
    async (approved: boolean) => {
      if (!record || !pendingApproval) return;
      setPendingApproval({ ...pendingApproval, resolved: true });
      try {
        await resolveApproval(record.runId, pendingApproval.approvalId, approved);
      } catch (err) {
        setError(String((err as Error).message ?? err));
      }
    },
    [record, pendingApproval],
  );

  const report = record ? reportOf(record) : null;

  return (
    <div className="mx-auto w-full max-w-3xl space-y-4 p-6">
      <h1 className="text-xl font-semibold">巡检多智能体</h1>

      <Card>
        <CardContent className="flex flex-wrap items-end gap-3 pt-6">
          <label className="flex flex-col gap-1 text-sm">
            站点
            <Input value={site} onChange={(e) => setSite(e.target.value)} className="w-36" />
          </label>
          <label className="flex flex-1 flex-col gap-1 text-sm">
            设备 ID（逗号分隔）
            <Input value={deviceIds} onChange={(e) => setDeviceIds(e.target.value)} />
          </label>
          <label className="flex flex-1 flex-col gap-1 text-sm">
            巡检目标
            <Input value={goal} onChange={(e) => setGoal(e.target.value)} />
          </label>
          <Button onClick={() => void start()} disabled={starting}>
            {starting ? "创建中…" : "发起巡检"}
          </Button>
        </CardContent>
      </Card>

      {error ? (
        <div className="rounded border border-red-300 bg-red-50 p-3 text-sm text-red-800">{error}</div>
      ) : null}

      {pendingApproval && !pendingApproval.resolved ? (
        <div className="rounded border border-amber-300 bg-amber-50 p-3">
          <div className="mb-2 text-sm font-medium">{pendingApproval.question}</div>
          <div className="mb-3 text-xs text-muted-foreground">
            待复核设备：{pendingApproval.devices.join("、") || "-"}
          </div>
          <div className="flex gap-2">
            <Button size="sm" onClick={() => void decide(true)}>
              批准派无人机
            </Button>
            <Button size="sm" variant="outline" onClick={() => void decide(false)}>
              拒绝
            </Button>
          </div>
        </div>
      ) : null}

      {record ? (
        <div className="space-y-3">
          <div className="flex items-center gap-2 text-sm">
            <Badge variant="outline">{record.status}</Badge>
            <span className="font-mono text-xs text-muted-foreground">
              run {record.runId.slice(0, 8)} · attempt {record.attempt}
            </span>
          </div>
          {events.length > 0 ? <Timeline events={events} /> : null}
          {terminal && report ? <ReportView report={report} /> : null}
        </div>
      ) : null}
    </div>
  );
}
