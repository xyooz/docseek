from __future__ import annotations

import argparse
import sys
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
    expected_top1: str


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
    # Filename and ordinary body relevance.
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

    # PDF body relevance.
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

    # Spreadsheet structure relevance.
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

    # Quoted English phrase semantics.
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

    # A semantic title should beat a long document that repeats the same words.
    # This protects structure-aware ranking from being swamped by document length.
    add_document(
        store,
        filename="内部控制手册.pptx",
        extension=".pptx",
        modified_time=90,
        chunks=[
            ("幻灯片 1 · 标题 操作风险审计", "操作风险审计 要求与检查方法"),
            ("幻灯片 2 · 标题 整改闭环", "整改闭环与复核要求"),
        ],
    )
    add_document(
        store,
        filename="年度汇报.pdf",
        extension=".pdf",
        modified_time=100,
        chunks=[
            (f"第 {page} 页", "操作风险审计 培训记录")
            for page in range(1, 21)
        ],
    )

    # Stable evidence across several chunks should beat an otherwise equal
    # accidental one-off body hit, but the bonus must remain small and capped.
    add_document(
        store,
        filename="贷后检查记录.docx",
        extension=".docx",
        modified_time=110,
        chunks=[
            ("文档块 1-2", "贷后风险排查"),
            ("文档块 3-4", "贷后风险排查"),
            ("文档块 5-6", "贷后风险排查"),
        ],
    )
    add_document(
        store,
        filename="会议速记.txt",
        extension=".txt",
        modified_time=120,
        chunks=[("行 1-20", "贷后风险排查")],
    )

    # A strong filename match must remain dominant over many body hits.
    add_document(
        store,
        filename="数据治理.txt",
        extension=".txt",
        modified_time=130,
        chunks=[("行 1-20", "数据治理")],
    )
    add_document(
        store,
        filename="项目周报.docx",
        extension=".docx",
        modified_time=140,
        chunks=[
            (f"文档块 {index + 1}", "数据治理")
            for index in range(8)
        ],
    )

    # Mixed English / number / Chinese office query.
    add_document(
        store,
        filename="客户核验清单.xlsx",
        extension=".xlsx",
        modified_time=150,
        chunks=[
            ("工作表 KYC 2026 · 行 1-200", "KYC 2026 客户核验 待复核"),
        ],
    )
    add_document(
        store,
        filename="历史培训材料.docx",
        extension=".docx",
        modified_time=160,
        chunks=[("文档块 1-3", "KYC 2025 客户核验 培训")],
    )

    return [
        QualityCase(
            "客户经理管理",
            {
                "客户经理管理办法.docx": 3.0,
                "培训笔记.docx": 1.0,
            },
            "客户经理管理办法.docx",
        ),
        QualityCase(
            "信贷业务",
            {
                "信贷业务操作手册.pdf": 3.0,
                "业务培训.pdf": 1.0,
            },
            "信贷业务操作手册.pdf",
        ),
        QualityCase(
            "逾期客户",
            {
                "逾期客户清单.xlsx": 3.0,
                "客户统计.xlsx": 1.0,
            },
            "逾期客户清单.xlsx",
        ),
        QualityCase(
            '"customer manager"',
            {"customer-manager-handbook.txt": 3.0},
            "customer-manager-handbook.txt",
        ),
        QualityCase(
            "操作风险审计",
            {
                "内部控制手册.pptx": 3.0,
                "年度汇报.pdf": 1.0,
            },
            "内部控制手册.pptx",
        ),
        QualityCase(
            "贷后风险排查",
            {
                "贷后检查记录.docx": 3.0,
                "会议速记.txt": 1.0,
            },
            "贷后检查记录.docx",
        ),
        QualityCase(
            "数据治理",
            {
                "数据治理.txt": 3.0,
                "项目周报.docx": 1.0,
            },
            "数据治理.txt",
        ),
        QualityCase(
            "KYC 2026 客户核验",
            {"客户核验清单.xlsx": 3.0},
            "客户核验清单.xlsx",
        ),
    ]


def run_quality_evaluation(
    k: int,
) -> tuple[RetrievalMetrics, list[str], list[str]]:
    details: list[str] = []
    failures: list[str] = []
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
            actual_top1 = ranked[0] if ranked else "<none>"
            top1_ok = actual_top1 == case.expected_top1
            details.append(
                f"query={case.query!r} recall@{k}={metrics.recall_at_k:.3f} "
                f"mrr={metrics.mrr:.3f} ndcg@{k}={metrics.ndcg_at_k:.3f} "
                f"top1={actual_top1!r} expected_top1={case.expected_top1!r} "
                f"top={ranked[:k]}"
            )
            if not top1_ok:
                failures.append(
                    f"query={case.query!r}: expected top1 {case.expected_top1!r}, "
                    f"got {actual_top1!r}"
                )
            if metrics.recall_at_k < 1.0 or metrics.ndcg_at_k < 0.90:
                failures.append(
                    f"query={case.query!r}: recall@{k}={metrics.recall_at_k:.3f}, "
                    f"ndcg@{k}={metrics.ndcg_at_k:.3f}"
                )

    return mean_metrics(all_metrics), details, failures


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

    parser = argparse.ArgumentParser(description="Evaluate DocSeek curated retrieval quality")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--assert-baseline", action="store_true")
    args = parser.parse_args()

    k = max(1, args.k)
    metrics, details, failures = run_quality_evaluation(k)
    print("DocSeek curated retrieval quality")
    for line in details:
        print(f"- {line}")
    print(
        f"mean recall@{k}={metrics.recall_at_k:.3f} "
        f"MRR={metrics.mrr:.3f} nDCG@{k}={metrics.ndcg_at_k:.3f}"
    )

    if args.assert_baseline:
        if metrics.recall_at_k < 1.0 or metrics.mrr < 0.95 or metrics.ndcg_at_k < 0.90:
            failures.append(
                "aggregate retrieval quality baseline regressed: "
                f"recall@{k}={metrics.recall_at_k:.3f}, "
                f"MRR={metrics.mrr:.3f}, nDCG@{k}={metrics.ndcg_at_k:.3f}"
            )
        if failures:
            raise SystemExit("retrieval quality baseline regressed\n- " + "\n- ".join(failures))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
