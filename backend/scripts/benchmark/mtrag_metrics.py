"""Recall@k and nDCG@k over BEIR-style runs and qrels.

Hand-rolled rather than pulled from `pytrec_eval`, which needs a C toolchain on
Windows, or from `mteb`, which drags in torch. Both are heavy dependencies for
two textbook formulas, and MTRAG publishes its baselines in exactly these two
metrics.

Graded relevance is preserved: MTRAG qrels carry scores above 1, and the
standard exponential gain is what its published table was computed with.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import log2

DEFAULT_CUTOFFS = (1, 3, 5, 10)


def recall_at_k(
    ranked: Sequence[str], relevant: Mapping[str, int], k: int
) -> float:
    """Share of relevant passages that appear in the top `k`."""
    if not relevant:
        return 0.0
    found = sum(1 for passage_id in ranked[:k] if passage_id in relevant)
    return found / len(relevant)


def _dcg(gains: Sequence[float]) -> float:
    # Rank positions are 1-based, so the first result divides by log2(2) == 1.
    return sum(gain / log2(position + 1) for position, gain in enumerate(gains, start=1))


def ndcg_at_k(ranked: Sequence[str], relevant: Mapping[str, int], k: int) -> float:
    """Normalized discounted cumulative gain with exponential gain."""
    if not relevant:
        return 0.0
    gains = [2 ** relevant.get(passage_id, 0) - 1 for passage_id in ranked[:k]]
    ideal = [2**score - 1 for score in sorted(relevant.values(), reverse=True)[:k]]
    best = _dcg(ideal)
    if best == 0:
        return 0.0
    return _dcg(gains) / best


def evaluate(
    runs: Mapping[str, Sequence[str]],
    qrels: Mapping[str, Mapping[str, int]],
    *,
    cutoffs: Sequence[int] = DEFAULT_CUTOFFS,
) -> dict[str, float]:
    """Average both metrics over every query that has qrels.

    Iteration is driven by `qrels`, not by `runs`, on purpose. A query the
    retriever returned nothing for scores zero instead of vanishing from the
    denominator, which is the difference between "we missed it" and "we never
    asked".
    """
    scores: dict[str, float] = {}
    if not qrels:
        return {"queries": 0}
    for k in cutoffs:
        recalls: list[float] = []
        ndcgs: list[float] = []
        for query_id, relevant in qrels.items():
            ranked = list(runs.get(query_id, ()))
            recalls.append(recall_at_k(ranked, relevant, k))
            ndcgs.append(ndcg_at_k(ranked, relevant, k))
        scores[f"recall@{k}"] = sum(recalls) / len(recalls)
        scores[f"ndcg@{k}"] = sum(ndcgs) / len(ndcgs)
    scores["queries"] = len(qrels)
    return scores
