# DocSeek Benchmarks

这里的基准用于回答两个问题：

1. DocSeek 在 1k / 10k / 50k 文件规模下是否仍然足够快；
2. 后续索引结构或排序算法改动是否造成明显性能回退。

## 快速运行

开发期快速检查：

```bash
python benchmarks/benchmark_search.py --files 1000 --chunks 3 --iterations 20
```

建议的固定档位：

```bash
# 快速回归
python benchmarks/benchmark_search.py --files 1000 --chunks 3 --iterations 20

# 普通办公规模
python benchmarks/benchmark_search.py --files 10000 --chunks 3 --iterations 30

# 重点规模门槛
python benchmarks/benchmark_search.py --files 50000 --chunks 3 --iterations 30
```

如需保留数据库检查体积或 SQLite 结构：

```bash
python benchmarks/benchmark_search.py --files 10000 --db .bench/docseek-10k.db
```

## 当前输出

脚本会报告：

- 首次合成索引总耗时；
- files/s 索引吞吐；
- SQLite 数据库体积；
- 常见中英文查询 P50；
- 查询 P95；
- 最快 / 最慢单次查询；
- 首屏返回条数。

## 注意

当前脚本是**合成索引基准**，适合比较不同代码版本，但不能代替真实 Office 文件解析测试。

后续还需要增加真实语料基准：

- DOCX 段落和表格；
- 大 XLSX；
- 长 PDF；
- PPTX；
- 混合目录；
- 单文件保存后的增量更新耗时；
- Windows 文件系统监听到搜索可见的端到端延迟。

所有性能数字都应记录测试机器 CPU、内存、磁盘类型、Python 版本和文件规模，避免跨机器直接比较绝对值。

## Scanner-only Python/Rust 对比

`benchmark_scan_backends.py` 只测文件系统 discovery，不经过 Parser、ChunkBatchWriter
或 SQLite/FTS。它在独立 worker 进程中运行 Python 和 Rust backend，默认覆盖 sparse/dense
两种组成以及 10k/100k 文件规模，并输出控制台表格和 JSON 报告：

```bash
python benchmarks/benchmark_scan_backends.py \
  --files 10000 100000 \
  --scenario both \
  --backend both \
  --json-out benchmark-results.json
```

需要 benchmark 专用依赖：

```bash
python -m pip install -e ".[benchmarks]"
```

报告包含 discovery 总耗时、files/s、第一次有意义的 progress、第一次 candidate、
整个进程 RSS，以及单独的 sparse-100k 并发取消延迟。扫描结果会校验 Python/Rust
candidate 数量一致；性能数值不作为 CI hard gate。

现有的完整索引 benchmark 仍然保留 parser/writer/SQLite 分解；可以显式选择 backend
观察 discovery 对端到端结果的影响：

```bash
python benchmarks/benchmark_scan.py --files 10000 --backend python
python benchmarks/benchmark_scan.py --files 10000 --backend rust
python benchmarks/benchmark_scan.py --files 10000 --backend both
```

完整索引 benchmark 中的 `scanner_wait` 是各次 `ScanSession.next_batch()` 的累计耗时；
`discovery_complete_wall` 只是流式 discovery 完成时的交错 wall-clock 标记，不应解读为
纯 Scanner discovery 时间。`single_file_update` 与 backend 无关，因为 `update_paths()`
不经过 ScanBackend。

## Extraction / Writer 边界 profiling

`benchmark_scan.py` 仍然只修改 benchmark 进程内的计时探针，不改变生产索引实现。除
原有的完整索引和 unchanged rescan 外，`--workload` 可以选择不同的 extraction 负载：

```bash
# 大量小文本：当前 50k synthetic 场景
python benchmarks/benchmark_scan.py --workload tiny-text --files 50000 --backend both

# 中等文本：约 16 KiB / 文件
python benchmarks/benchmark_scan.py --workload medium-text --files 10000 --backend both

# 大文本：约 512 KiB / 文件
python benchmarks/benchmark_scan.py --workload large-text --files 1000 --backend both

# 复制仓库内真实 fixture，分别观察真实 parser / isolation lane
python benchmarks/benchmark_scan.py --workload docx --files 500 --backend both
python benchmarks/benchmark_scan.py --workload xlsx --files 200 --backend both
python benchmarks/benchmark_scan.py --workload pptx --files 500 --backend both
python benchmarks/benchmark_scan.py --workload pdf --files 500 --backend both
```

Windows 目标平台的正式矩阵可通过手动 Action `benchmark-index-hotspots` 运行。它只在
`windows-latest` 上执行，不设置性能 hard gate，完成后上传七个 workload 的原始文本报告；
默认 `source_ref` 为 `perf/index-hotspots`。

报告中的 `first_extraction` 会给出：

- `extraction_total`：按每次 chunk 拉取计时的 extraction 总耗时；
- `broker_dispatch`：适配器选择；
- `file_read`：benchmark 进程内 Python binary reader 的实际读取；
- `text_decode` / `text_decode_normalize`：文本 probe 解码以及流式文本解码/换行处理；
- `text_chunk_build` / `text_normalize_chunk_build`：行分块、DocumentChunk 构造以及小文件
  fast path 的剩余处理；
- `adapter_direct` 或 `adapter_isolated`：adapter 边界耗时。Office/PDF 在 DirectoryIndexer
  路径中通常位于隔离子进程，因此不会把子进程内部的 parser 时间伪装成 Python 文本阶段。

这些 extraction 子项彼此可能是嵌套诊断，不能和 `extraction_total` 或
`adapter_*` 相加。Writer 的 `writer_total` 使用 `replace_document + 外层 flush + exit`，
不会重复计算 `replace_document` 内部触发的 flush；`sql_total` 则是各次 SQL execute/commit
耗时的汇总，不包含重复的 `structure_total`。

scanner-only benchmark 的 Windows 报告通过手动触发的
`.github/workflows/benchmark-scan-backends.yml` 上传，不进入普通 push gate。
