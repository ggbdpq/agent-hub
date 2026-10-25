"""真实模型消融共享机件（X2R/X3R）：计量代理 / 轮次落盘与断点续跑 / 汇总。

- ``MeteredLLM`` + ``RealLLMMeter``：包一层 ainvoke 代理，按系统提示路由角色计数；
  Token 优先取上游 usage_metadata（计费口径），缺失时按内容估算并单独标记
  （estimated_calls）。**不记录、不打印任何消息内容与 API key。**
- 轮次文件（``round<N>-<config>.json``）：每完成一轮立即落盘，进程中断后重跑
  同一命令可跳过已完成轮次（断点续跑，不重复烧预算）。
- ``round_anomaly``：单轮失败率异常检查（真实模型解析失败→大量 crashed 等情况
  如实记录并停止后续轮次，不烧预算掩埋问题）。
- 汇总复用 e1_ablation 的语义（均值+范围；时延对全部轮×用例样本取分位数）。

真实模型 env：OPENAI_BASE_URL / OPENAI_MODEL / OPENAI_API_KEY（key 只从环境读，
不在日志与报告中回显）；调用前须 unset http_proxy https_proxy all_proxy。
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from evals.compliant_llm import _ROLE_MARKERS, _text

MAX_CRASH_RATE = 0.2  # 单轮 crashed 占比超过 → 停止后续轮次
MIN_SUCCESS = 0.5  # 单轮成功率低于 → 停止后续轮次


def role_of(messages) -> str:
    system = _text(messages[0]) if messages else ""
    for role, marker in _ROLE_MARKERS:
        if marker in system:
            return role
    return "unknown"


class RealLLMMeter:
    """真实模型调用计量：次数（按角色）+ Token（上游 usage 优先，估算兜底）。"""

    def __init__(self) -> None:
        self.calls = 0
        self.calls_by_role: dict[str, int] = {}
        self.input_tokens = 0
        self.output_tokens = 0
        self.input_tokens_est = 0
        self.output_tokens_est = 0
        self.estimated_calls = 0

    def record(self, messages, reply) -> None:
        from app.tools.budget import estimate_tokens

        self.calls += 1
        role = role_of(messages)
        self.calls_by_role[role] = self.calls_by_role.get(role, 0) + 1
        usage = getattr(reply, "usage_metadata", None) or {}
        if usage.get("total_tokens"):
            self.input_tokens += int(usage.get("input_tokens", 0))
            self.output_tokens += int(usage.get("output_tokens", 0))
        else:  # 上游未回 usage：按内容估算（单独标记，不计入"计费口径"）
            self.input_tokens_est += sum(estimate_tokens(_text(m)) for m in messages)
            content = reply.content if isinstance(reply.content, str) else json.dumps(reply.content, ensure_ascii=False)
            self.output_tokens_est += estimate_tokens(content)
            self.estimated_calls += 1

    def snapshot(self) -> dict:
        return {
            "calls": self.calls,
            "calls_by_role": dict(self.calls_by_role),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "input_tokens_est": self.input_tokens_est,
            "output_tokens_est": self.output_tokens_est,
            "estimated_calls": self.estimated_calls,
        }


class MeteredLLM:
    """ainvoke 代理：转发真实模型，记录 usage；其余属性透传。"""

    def __init__(self, inner, meter: RealLLMMeter) -> None:
        self._inner = inner
        self._meter = meter

    async def ainvoke(self, messages, *args, **kwargs):
        reply = await self._inner.ainvoke(messages, *args, **kwargs)
        self._meter.record(messages, reply)
        return reply

    def __getattr__(self, name):
        return getattr(self._inner, name)


def metered_env_factory() -> tuple:
    """(llm_factory, meter)：env_chat_model_factory 包计量代理（真实模型）。"""
    from app.agents.models import env_chat_model_factory

    meter = RealLLMMeter()
    inner = env_chat_model_factory()

    def build() -> MeteredLLM:
        return MeteredLLM(inner(), meter)

    return build, meter


def round_anomaly(round_result: dict, *, max_crash_rate: float = MAX_CRASH_RATE, min_success: float = MIN_SUCCESS) -> str | None:
    """单轮异常检查：返回异常原因（None=正常）。crash 率与成功率双阈值。"""
    agg = round_result.get("aggregates", {})
    cases = agg.get("cases") or 1
    crashed = agg.get("crashed_total", 0)
    success = agg.get("success_rate", 1.0)
    if crashed / cases > max_crash_rate:
        return f"crash 率 {crashed}/{cases} 超过 {max_crash_rate:.0%}（疑似真实模型解析/上游故障）"
    if success < min_success:
        return f"成功率 {success:.0%} 低于 {min_success:.0%}（疑似系统性失败）"
    return None


def usd_from_usage(input_tokens: int, output_tokens: int, prices: dict) -> float:
    """Token → USD（价格结构同 e1_ablation.DEEPSEEK_PRICES 的档位项）。"""
    return input_tokens / 1e6 * prices["input_usd_per_m"] + output_tokens / 1e6 * prices["output_usd_per_m"]


def dump_round(rounds_dir: Path, round_result: dict) -> Path:
    rounds_dir.mkdir(parents=True, exist_ok=True)
    path = rounds_dir / f"round{round_result['round']}-{round_result['config']}.json"
    path.write_text(json.dumps(round_result, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def collect_rounds(rounds_dir: Path) -> dict[str, list[dict]]:
    """读取轮次文件 → {config: [round 结果按轮次升序]}。"""
    by_config: dict[str, list[dict]] = {}
    for path in sorted(rounds_dir.glob("round*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        by_config.setdefault(data["config"], []).append(data)
    for rounds in by_config.values():
        rounds.sort(key=lambda r: r["round"])
    return by_config


def summarize_config(rounds: list[dict]) -> dict:
    """跨轮汇总（语义同 e1_ablation.summarize_runs）+ 轮次元信息与 LLM 计量合计。"""
    from evals.e1_ablation import summarize_runs

    summary = summarize_runs(rounds, [r.get("latencies") or [] for r in rounds])
    summary["runs"] = len(rounds)
    summary["round_indices"] = [r["round"] for r in rounds]
    summary["llm"] = {
        "calls": sum(r["llm_meter"]["calls"] for r in rounds),
        "calls_by_round": [r["llm_meter"]["calls"] for r in rounds],
        "calls_by_role": _merge_role_counts(rounds),
        "input_tokens": sum(r["llm_meter"]["input_tokens"] for r in rounds),
        "output_tokens": sum(r["llm_meter"]["output_tokens"] for r in rounds),
        "input_tokens_est": sum(r["llm_meter"].get("input_tokens_est", 0) for r in rounds),
        "output_tokens_est": sum(r["llm_meter"].get("output_tokens_est", 0) for r in rounds),
        "estimated_calls": sum(r["llm_meter"].get("estimated_calls", 0) for r in rounds),
    }
    summary["per_round"] = [
        {
            "round": r["round"],
            "success_rate": r["aggregates"].get("success_rate"),
            "crashed_total": r["aggregates"].get("crashed_total"),
            "wall_s": r.get("wall_s"),
            "llm_calls": r["llm_meter"]["calls"],
            "anomaly": r.get("anomaly"),
        }
        for r in rounds
    ]
    return summary


def _merge_role_counts(rounds: list[dict]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for r in rounds:
        for role, n in r["llm_meter"].get("calls_by_role", {}).items():
            merged[role] = merged.get(role, 0) + n
    return merged


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()
