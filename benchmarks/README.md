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

## SQLite / FTS writer baseline

M9-A 只增加 benchmark 进程内的 writer/SQLite 计时探针，不改变生产数据库逻辑、SQLite
PRAGMA、batch size 或 transaction 粒度。它在完整索引和 unchanged rescan 中额外报告：

- `transactions` / `commits` / `rollbacks`、`rows_per_transaction` 和 `chunks_per_transaction`；
- `files_insert` / `files_update`、chunk insert/delete、structure、extraction state；
- normal FTS 与 CJK FTS 的 insert/delete 时间及 row 数；
- `execute` / `executemany` / `executescript` 时间和调用次数；
- `flush`、`replace_document`、`commit`、WAL checkpoint 及 WAL sidecar 大小。

这些指标存在嵌套关系：`writer_total` 包含 `replace_document`、外层 flush 和 writer exit；
`sql_total` 是分类后的 SQL execute/commit 时间汇总，不再把 `structure_total` 重复相加；
`execute` 是所有分类 SQL 的外层总计，不能与分类项相加。Windows 正式矩阵由手动触发的
`.github/workflows/benchmark-sqlite-writer.yml` 上传，覆盖：

```bash
tiny-text 50,000
medium-text 10,000
large-text 1,000
```

该 workflow 还传入 `--profile-replacement`，在 fresh index 和 unchanged rescan 后修改一个
文件并再扫一次，以便观察 replacement path 的 chunk/FTS delete 与 insert；这部分单独标为
`replacement_*`，不与 fresh-index 计时相加。

## SQLite transaction / commit A/B

`benchmark_transaction_sweep.py` 是 M9-B 的 benchmark-only transaction 容量 sweep。它在
索引进程内临时扩大 `DirectoryIndexer` 的 discovery/write batch 边界，从而观察不同事务容量
对 SQLite commit 的影响；生产默认值、`ChunkBatchWriter`、SQLite PRAGMA、journal mode、
`synchronous` 和 tokenizer 都不会被修改。

默认 Windows 矩阵为：

```bash
python benchmarks/benchmark_transaction_sweep.py \
  --workload tiny-text --workload medium-text --workload large-text \
  --backend both \
  --capacities 128 256 512 1024 files \
  --cancel-files 2000 \
  --json-out benchmark-transaction-sweep.json
```

其中 `files` 是单次扫描的 upper-bound transaction 参考值。每组都会验证 indexed file 数、
chunk/FTS/state 内容 digest、搜索结果、数据库 `integrity_check` 以及重新打开数据库后的
一致性；同时报告 commit latency 的平均值、P50、P95、最大值、RSS 峰值和取消延迟。
JSON 中保留每一次 commit 的 latency 样本，终端输出使用聚合值。不同指标存在嵌套关系，
`writer_total`、`sql_total` 和 `commit_total` 不能简单相加。

## SQLite production transaction tuning

M9-B.1 将完整索引的 production flush boundary 固定为 512 个文件，同时保留 scanner
内部最多 128 个 candidate 的 pull batch，以及 8,000,000 字符的 payload 上限。也就是说，
索引层可以在一次 writer transaction 中积累更多小文本文件，但不会扩大 Rust/Python scanner
的核心 batch 上限，也不会改变 SQLite PRAGMA、FTS 或 CJK tokenizer。M9-B 的 transaction
sweep 仍可通过临时覆盖 `FULL_SCAN_BATCH_SIZE` 重跑各容量，用于验证生产默认值。

## SQLite paired transaction confirmation

`benchmark_transaction_paired.py` 是 M9-B.2 的同 runner paired A/B benchmark。它保持生产
默认 `FULL_SCAN_BATCH_SIZE=512`，在 benchmark 进程内按 `128, 512, 128, 512`（默认两轮）
临时覆盖完整索引边界；每次运行都会建立独立数据库，并验证文件数、chunk/FTS/state
内容、搜索结果、`integrity_check`、数据库重开以及取消后的数据库一致性。脚本同时记录
RSS、取消延迟、事务/commit 数和 commit latency，并按 workload/backend 输出 128 与 512
的 median、delta 和 speedup。它不会修改 SQLite PRAGMA、journal mode、synchronous、FTS
或 tokenizer。

本地 smoke：

```bash
python benchmarks/benchmark_transaction_paired.py \
  --workload tiny-text --workload medium-text --workload large-text \
  --backend both --repeats 2 \
  --cancel-files 200 \
  --json-out benchmark-transaction-paired.json \
  --report-out benchmark-transaction-paired.txt
```

Windows 正式运行由手动触发的 `.github/workflows/benchmark-sqlite-transaction-paired.yml`
上传。Actions 页面中的 workflow 分支应选 `main`，实际测试代码通过 `source_ref` 选择
实验分支；默认 workload 是 tiny-text 50,000、medium-text 10,000 和 large-text 1,000。
