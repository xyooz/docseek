from __future__ import annotations

import argparse
import tempfile
from dataclasses import dataclass
from pathlib import Path

from docseek.chunk_store import ChunkStore
from docseek.chunks import DocumentChunk
from docseek.exact_search import ExactGroupedSearchEngine
from docseek.retrieval_metrics import RetrievalMetrics, evaluate_ranking, mean_metrics
from docseek.search_db import SearchDatabase


@dataclass(frozen=True, slots=True)
class QualityCase:
    query: str
    relevance: dict[str, float]


def add_document(
    store: ChunkStore,
    *,
    filename: str,
    extension: str,
    chunks: list[tuple[str, str]],
    modified_time: float,
) -> None:
    path = str(Path("C:/docseek-quality") / filename)
    store.replace_document(
        path=path,
        filename=filename,
        extension=extension,
        modified_time=modified_time,
        size=sum(len(content.encode("utf-8")) for _, content in chunks),
        chunks=[
            DocumentChunk(index, location, content)
            for index, (location, content) in enumerate(chunks)
        ],
    )


def build_corpus(store: ChunkStore) -> list[QualityCase]:
    add_document(
        store,
        filename="客户经理管理办法.docx",
        extension=".docx",
        modified_time=10,
        chunks=[
            ("文档块 1-4 · 标题 客户经理管理", "客户经理管理要求与岗位职责"),
            ("文档块 5-8 · 标题 考核要求", "客户经理季度考核办法"),
        ],
    )
    add_document(
        store,
        filename="培训笔记.docx",
        extension=".docx",
        modified_time=20,
        chunks=[
            ("文档块 1-8", "客户经理管理 客户经理管理 客户经理管理 培训记录"),
        ],
    )
    add_document(
        store,
        filename="信贷业务操作手册.pdf",
        extension=".pdf",
        modified_time=30,
        chunks=[
            ("第 1 页", "信贷业务操作流程与客户材料要求"),
            ("第 12 页", "贷后管理与风险检查"),
        ],
    )
    add_document(
        store,
        filename="业务培训.pdf",
        extension=".pdf",
        modified_time=40,
        chunks=[("第 2 页", "培训中简要介绍信贷业务")],
    )
    add_document(
        store,
        filename="逾期客户清单.xlsx",
        extension=".xlsx",
        modified_time=50,
        chunks=[
            ("工作表 逾期客户 · 行 1-200", "工作表: 逾期客户\n客户号\t余额\t逾期天数"),
            ("工作表 正常客户 · 行 1-200", "工作表: 正常客户\n客户号\t余额\t状态"),
        ],
    )
    add_document(
        store,
        filename="客户统计.xlsx",
        extension=".xlsx",
        modified_time=60,
        chunks=[("工作表 汇总 · 行 1-200", "逾期客户 逾期客户 统计汇总")],
    )
    add_document(
        store,
        filename="customer-manager-handbook.txt",
        extension=".txt",
        modified_time=70,
        chunks=[("行 1-20", "customer manager handbook for branch operations")],
    )
    add_document(
        store,
        filename="english-notes.txt",
        extension=".txt",
        modified_time=80,
        chunks=[("行 1-20", "customer service notes written by a branch manager")],
    )

    return [
        QualityCase(
            "客户经理管理",
            {
                "客户经理管理办法.docx": 3.0,
                "培训笔记.docx": 1.0,
            },
        ),
        QualityCase(
            "信贷业务",
            {
                "信贷业务操作手册.pdf": 3.0,
                "业务培训.pdf": 1.0,
            },
        ),
        QualityCase(
            "逾期客户",
            {
                "逾期客户清单.xlsx": 3.0,
                "客户统计.xlsx": 1.0,
            },
        ),
        QualityCase(
            '"customer manager"',
            {"customer-manager-handbook.txt": 3.0},
        ),
    ]


def run_quality_evaluation(k: int) -> tuple[RetrievalMetrics, list[str]]:
    details: list[str] = []
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "quality.db"
        SearchDatabase(db_path)
        store = ChunkStore(db_path)
        engine = ExactGroupedSearchEngine(store)
        cases = build_corpus(store)

        all_metrics: list[RetrievalMetrics] = []
        for case in cases:
            page = engine.search_page(case.query, limit=max(k, 10))
            ranked = [item.filename for item in page.items]
            metrics = evaluate_ranking(ranked, case.relevance, k=k)
            all_metrics.append(metrics)
            details.append(
                f"query={case.query!r} recall@{k}={metrics.recall_at_k:.3f} "
                f"mrr={metrics.mrr:.3f} ndcg@{k}={metrics.ndcg_at_k:.3f} "
                f"top={ranked[:k]}"
            )

    return mean_metrics(all_metrics), details


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate DocSeek curated retrieval quality")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--assert-baseline", action="store_true")
    args = parser.parse_args()

    metrics, details = run_quality_evaluation(max(1, args.k))
    print("DocSeek curated retrieval quality")
    for line in details:
        print(f"- {line}")
    print(
        f"mean recall@{max(1, args.k)}={metrics.recall_at_k:.3f} "
        f"MRR={metrics.mrr:.3f} nDCG@{max(1, args.k)}={metrics.ndcg_at_k:.3f}"
    )

    if args.assert_baseline:
        if metrics.recall_at_k < 1.0 or metrics.mrr < 0.95 or metrics.ndcg_at_k < 0.90:
            raise SystemExit("retrieval quality baseline regressed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
