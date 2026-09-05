from __future__ import annotations

import unittest

from docseek.retrieval_metrics import (
    RetrievalMetrics,
    evaluate_ranking,
    mean_metrics,
    ndcg_at_k,
    reciprocal_rank,
    recall_at_k,
)


class RetrievalMetricTests(unittest.TestCase):
    def test_recall_at_k_uses_unique_relevant_documents(self) -> None:
        ranked = ["a", "b", "c", "d"]
        relevant = {"a": 3.0, "c": 1.0}
        self.assertEqual(recall_at_k(ranked, relevant, k=1), 0.5)
        self.assertEqual(recall_at_k(ranked, relevant, k=3), 1.0)

    def test_reciprocal_rank_uses_first_relevant_result(self) -> None:
        self.assertEqual(reciprocal_rank(["x", "a", "b"], {"a": 1.0}), 0.5)
        self.assertEqual(reciprocal_rank(["x", "y"], {"a": 1.0}), 0.0)

    def test_ndcg_rewards_higher_relevance_near_top(self) -> None:
        relevance = {"best": 3.0, "good": 2.0, "ok": 1.0}
        ideal = ndcg_at_k(["best", "good", "ok"], relevance, k=3)
        reversed_order = ndcg_at_k(["ok", "good", "best"], relevance, k=3)
        self.assertAlmostEqual(ideal, 1.0)
        self.assertLess(reversed_order, ideal)

    def test_evaluate_and_mean_metrics(self) -> None:
        first = evaluate_ranking(["a", "b"], {"a": 2.0, "b": 1.0}, k=2)
        second = evaluate_ranking(["x", "b", "a"], {"a": 2.0, "b": 1.0}, k=2)
        mean = mean_metrics([first, second])

        self.assertEqual(first, RetrievalMetrics(1.0, 1.0, 1.0))
        self.assertAlmostEqual(mean.recall_at_k, 0.75)
        self.assertAlmostEqual(mean.mrr, 0.75)
        self.assertGreater(mean.ndcg_at_k, 0.5)
        self.assertLess(mean.ndcg_at_k, 1.0)


if __name__ == "__main__":
    unittest.main()
