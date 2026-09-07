from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True, slots=True)
class RetrievalMetrics:
    recall_at_k: float
    mrr: float
    ndcg_at_k: float


def recall_at_k(
    ranked_ids: Sequence[str],
    relevant: Mapping[str, float] | set[str],
    *,
    k: int,
) -> float:
    relevant_ids = set(relevant)
    if not relevant_ids:
        return 1.0
    top = set(ranked_ids[: max(0, int(k))])
    return len(top & relevant_ids) / len(relevant_ids)


def reciprocal_rank(
    ranked_ids: Sequence[str],
    relevant: Mapping[str, float] | set[str],
) -> float:
    relevant_ids = set(relevant)
    for index, item_id in enumerate(ranked_ids, start=1):
        if item_id in relevant_ids:
            return 1.0 / index
    return 0.0


def ndcg_at_k(
    ranked_ids: Sequence[str],
    relevance: Mapping[str, float],
    *,
    k: int,
) -> float:
    limit = max(0, int(k))
    if limit == 0 or not relevance:
        return 1.0

    def dcg(gains: Sequence[float]) -> float:
        return sum(
            (2.0**gain - 1.0) / math.log2(position + 1)
            for position, gain in enumerate(gains, start=1)
            if gain > 0
        )

    actual = [float(relevance.get(item_id, 0.0)) for item_id in ranked_ids[:limit]]
    ideal = sorted((float(value) for value in relevance.values()), reverse=True)[:limit]
    ideal_score = dcg(ideal)
    if ideal_score == 0.0:
        return 1.0
    return dcg(actual) / ideal_score


def evaluate_ranking(
    ranked_ids: Sequence[str],
    relevance: Mapping[str, float],
    *,
    k: int = 10,
) -> RetrievalMetrics:
    return RetrievalMetrics(
        recall_at_k=recall_at_k(ranked_ids, relevance, k=k),
        mrr=reciprocal_rank(ranked_ids, relevance),
        ndcg_at_k=ndcg_at_k(ranked_ids, relevance, k=k),
    )


def mean_metrics(metrics: Sequence[RetrievalMetrics]) -> RetrievalMetrics:
    if not metrics:
        return RetrievalMetrics(1.0, 1.0, 1.0)
    count = len(metrics)
    return RetrievalMetrics(
        recall_at_k=sum(item.recall_at_k for item in metrics) / count,
        mrr=sum(item.mrr for item in metrics) / count,
        ndcg_at_k=sum(item.ndcg_at_k for item in metrics) / count,
    )
