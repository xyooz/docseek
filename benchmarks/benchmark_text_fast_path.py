from __future__ import annotations

import argparse
import io
import statistics
import sys
import time
from dataclasses import dataclass
from typing import Iterator

from docseek.chunks import (
    DocumentChunk,
    TEXT_ENCODING_SAMPLE_BYTES,
    _decode_text_bytes,
    _iter_text_chunks,
    _iter_text_chunks_from_lines,
)


@dataclass(slots=True)
class _MemoryPath:
    payload: bytes

    def open(self, mode: str) -> io.BytesIO:
        if mode != "rb":
            raise AssertionError(f"unexpected mode: {mode}")
        return io.BytesIO(self.payload)


def _legacy_small_text_chunks(
    path: _MemoryPath,
    *,
    target_chars: int,
) -> Iterator[DocumentChunk]:
    """Reproduce the pre-fast-path small-text pipeline for same-run A/B timing."""
    with path.open("rb") as raw:
        probe = raw.read(TEXT_ENCODING_SAMPLE_BYTES + 1)
        has_more = len(probe) > TEXT_ENCODING_SAMPLE_BYTES
        if has_more:
            raise ValueError("benchmark legacy helper only covers small files")
        _encoding, decoded = _decode_text_bytes(probe)
        # This is the allocation/generator layer removed by the production fast
        # path: universal-newline StringIO -> line iterator -> buffer -> join.
        with io.StringIO(decoded, newline=None) as text:
            yield from _iter_text_chunks_from_lines(text, target_chars=target_chars)


def _materialize_current(path: _MemoryPath, target_chars: int) -> tuple[DocumentChunk, ...]:
    return tuple(_iter_text_chunks(path, target_chars=target_chars))  # type: ignore[arg-type]


def _materialize_legacy(path: _MemoryPath, target_chars: int) -> tuple[DocumentChunk, ...]:
    return tuple(_legacy_small_text_chunks(path, target_chars=target_chars))


def _run_many(function, path: _MemoryPath, *, target_chars: int, iterations: int) -> float:
    started = time.perf_counter()
    for _ in range(iterations):
        function(path, target_chars)
    return time.perf_counter() - started


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Same-run A/B benchmark for the small single-chunk text fast path"
    )
    parser.add_argument("--iterations", type=int, default=10_000)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--target-chars", type=int, default=12_000)
    args = parser.parse_args()
    if args.iterations < 1 or args.rounds < 1 or args.target_chars < 1:
        parser.error("iterations, rounds and target-chars must all be >= 1")

    cases = {
        "utf8-lf": _MemoryPath(
            "客户经理 信贷业务制度\n第二行办公资料\n".encode("utf-8")
        ),
        "utf8-crlf-blank": _MemoryPath(
            "第一行 客户经理\r\n\r\n第三行 信贷\r\n".encode("utf-8")
        ),
        "gb18030": _MemoryPath(
            "客户编号,姓名\r\n001,张三\r\n".encode("gb18030")
        ),
    }

    print("DocSeek small-text fast-path same-run A/B")
    print(
        f"iterations={args.iterations:,} rounds={args.rounds} "
        f"target_chars={args.target_chars:,}"
    )

    aggregate_legacy = 0.0
    aggregate_current = 0.0

    for name, path in cases.items():
        legacy_output = _materialize_legacy(path, args.target_chars)
        current_output = _materialize_current(path, args.target_chars)
        if current_output != legacy_output:
            raise AssertionError(
                f"{name}: current chunks differ from legacy contract: "
                f"legacy={legacy_output!r} current={current_output!r}"
            )
        if len(current_output) != 1:
            raise AssertionError(f"{name}: benchmark case must stay single-chunk")

        legacy_times: list[float] = []
        current_times: list[float] = []
        for round_no in range(args.rounds):
            # Alternate order so transient CPU scheduling does not consistently
            # favor one implementation.
            if round_no % 2 == 0:
                legacy_times.append(
                    _run_many(
                        _materialize_legacy,
                        path,
                        target_chars=args.target_chars,
                        iterations=args.iterations,
                    )
                )
                current_times.append(
                    _run_many(
                        _materialize_current,
                        path,
                        target_chars=args.target_chars,
                        iterations=args.iterations,
                    )
                )
            else:
                current_times.append(
                    _run_many(
                        _materialize_current,
                        path,
                        target_chars=args.target_chars,
                        iterations=args.iterations,
                    )
                )
                legacy_times.append(
                    _run_many(
                        _materialize_legacy,
                        path,
                        target_chars=args.target_chars,
                        iterations=args.iterations,
                    )
                )

        legacy_median = statistics.median(legacy_times)
        current_median = statistics.median(current_times)
        speedup = legacy_median / current_median if current_median > 0 else float("inf")
        saved = 1.0 - current_median / legacy_median if legacy_median > 0 else 0.0
        aggregate_legacy += legacy_median
        aggregate_current += current_median
        print(
            f"{name}: legacy={legacy_median:.4f}s current={current_median:.4f}s "
            f"speedup={speedup:.3f}x saved={saved:.1%} chunks_equal=yes"
        )

    aggregate_speedup = (
        aggregate_legacy / aggregate_current if aggregate_current > 0 else float("inf")
    )
    aggregate_saved = (
        1.0 - aggregate_current / aggregate_legacy if aggregate_legacy > 0 else 0.0
    )
    print(
        f"aggregate: legacy={aggregate_legacy:.4f}s current={aggregate_current:.4f}s "
        f"speedup={aggregate_speedup:.3f}x saved={aggregate_saved:.1%}"
    )


if __name__ == "__main__":
    main()
