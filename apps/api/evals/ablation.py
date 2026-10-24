"""消融配置矩阵（C9，P1）：一次展开并跑完检索侧消融，输出对比表（计划 §11-E5/E6）。

- 轴：channel（hybrid / bm25-only / vector-only）× token_mode（bigram / unigram），
  rerank 轴固定 False（P2 仅留接口，E6 结论：待真实 rerank API 再评估）；
- 真实模型侧消融（E1-E4/E7）依赖 B 的编排与 A 的运行时，集成后由同一框架入口扩展；
- 入口：``python -m evals.ablation``。
"""

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from app.contracts.knowledge import Chunk, Retriever
from app.knowledge.bm25 import Bm25Searcher
from app.knowledge.embedding import Embedder, HashingEmbedder
from app.knowledge.retriever import HybridRetriever
from app.knowledge.splitting import split_corpus
from app.knowledge.vector import VectorSearcher
from evals.datasets import corpus_root, load_retrieval_annotations
from evals.retrieval_eval import annotations_to_cases, run_retrieval_eval

DEFAULT_AXES: dict[str, list] = {
    "channel": ["hybrid", "bm25-only", "vector-only"],
    "token_mode": ["bigram", "unigram"],
    "rerank": [False],  # P2：默认关闭（接口已在 C4 留位）
}

DISCLAIMER = (
    "合成语料 + 人工标注的离线消融：量化确定性检索管道各组件的边际贡献，"
    "仅验证评测流程，不代表真实业务检索效果，不代表模型效果。"
)


def expand_matrix(axes: dict[str, list] | None = None) -> list[dict]:
    """笛卡尔积展开配置矩阵；未给出的轴回落默认值，产出带稳定命名的配置列表。"""
    merged = {**DEFAULT_AXES, **(axes or {})}
    for key, values in merged.items():
        if key not in DEFAULT_AXES:
            raise ValueError(f"未知消融轴：{key}（可用：{sorted(DEFAULT_AXES)}）")
        if not values:
            raise ValueError(f"消融轴 {key} 取值不能为空")
    configs: list[dict] = []
    for channel in merged["channel"]:
        for token_mode in merged["token_mode"]:
            for rerank in merged["rerank"]:
                name = f"ablation-{channel}-{token_mode}" + ("-rerank" if rerank else "")
                configs.append(
                    {"name": name, "channel": channel, "token_mode": token_mode, "rerank": rerank}
                )
    return configs


def build_retriever(config: dict, chunks: list[Chunk], embedder: Embedder | None = None) -> Retriever:
    if config.get("rerank"):
        raise NotImplementedError("重排器仅接口（P2）；真实 rerank API 接入后启用该轴")
    token_mode = config.get("token_mode", "bigram")
    if config.get("channel") == "hybrid":
        return HybridRetriever(chunks, embedder=embedder or HashingEmbedder(dim=512), token_mode=token_mode)
    if config.get("channel") == "bm25-only":
        return Bm25Searcher(chunks, token_mode=token_mode)
    if config.get("channel") == "vector-only":
        return _VectorOnly(chunks, embedder or HashingEmbedder(dim=512))
    raise ValueError(f"未知通道：{config.get('channel')}")


class _VectorOnly:
    """仅向量单路（E5 基线）：实现契约 Retriever 签名。"""

    def __init__(self, chunks: list[Chunk], embedder: Embedder, min_sim: float = 0.05) -> None:
        self._inner = VectorSearcher(chunks, embedder, min_sim=min_sim)

    async def search(self, query: str, k: int, filters: dict | None = None) -> list[Chunk]:
        return await self._inner.search(query, k, filters)


async def run_ablation(out_dir: Path | None = None, axes: dict[str, list] | None = None, k: int = 5) -> dict:
    out_dir = out_dir or Path(__file__).resolve().parent / "reports"
    configs = expand_matrix(axes)
    chunks = split_corpus(corpus_root())
    cases = annotations_to_cases(load_retrieval_annotations())
    baseline: dict | None = None
    results: list[dict] = []
    for config in configs:
        retriever = build_retriever(config, chunks)
        start = time.perf_counter()
        report = await run_retrieval_eval(retriever, cases, k=k)
        wall_ms = (time.perf_counter() - start) * 1000.0
        agg = report.aggregates
        if config["name"].endswith("bigram") and baseline is None:
            baseline = agg  # 基线 = 矩阵中第一个配置（默认 hybrid-bigram）
        results.append({"config": config, "aggregates": agg, "suite_wall_ms": round(wall_ms, 2)})
        print(
            f"[ablation] {config['name']}: recall@5={agg['recall_at_5']:.3f} mrr={agg['mrr']:.3f} "
            f"p95={agg['latency_ms_p95']:.2f}ms"
        )
    payload = {
        "suite": "ablation-retrieval",
        "generated_at": datetime.now(UTC).isoformat(),
        "disclaimer": DISCLAIMER,
        "baseline": results[0]["config"]["name"],
        "configs": results,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    (out_dir / f"ablation-retrieval-{stamp}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / f"ablation-retrieval-{stamp}.md").write_text(render_markdown(payload), encoding="utf-8")
    return payload


def render_markdown(payload: dict) -> str:
    base_name = payload["baseline"]
    base = next(c["aggregates"] for c in payload["configs"] if c["config"]["name"] == base_name)
    lines = [
        "# 检索消融对比表（C9 / 计划 §11-E5）",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 基线：{base_name}",
        "",
        f"> **{payload['disclaimer']}**",
        "",
        "| 配置 | Recall@1 | Recall@3 | Recall@5 | Δ@5 | MRR | P95 (ms) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in payload["configs"]:
        a = item["aggregates"]
        delta5 = a["recall_at_5"] - base["recall_at_5"]
        sign = "+" if delta5 >= 0 else ""
        lines.append(
            f"| {item['config']['name']} | {a['recall_at_1']:.3f} | {a['recall_at_3']:.3f} "
            f"| {a['recall_at_5']:.3f} | {sign}{delta5:.3f} | {a['mrr']:.3f} | {a['latency_ms_p95']:.2f} |"
        )
    best = max(payload["configs"], key=lambda c: c["aggregates"]["recall_at_5"])
    lines += [
        "",
        "## 结论摘录",
        "",
        f"- 按 Recall@5 最优：**{best['config']['name']}**（{best['aggregates']['recall_at_5']:.3f}）。",
        "- 若某组件（如向量通道）消融后指标不降反升，应在报告与 README 中如实写明，并说明其"
        "在何种条件下才可能有收益（E5 判定标准见计划 §11）。",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    asyncio.run(run_ablation())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
