# DocSeek 搜索性能基线

本文记录已经在 Windows GitHub Actions 上验证过的搜索性能实验，用于指导后续优化。数字会受到共享 runner 波动影响，因此更看重同一轮 A/B 的相对趋势，而不是跨 run 的绝对毫秒值。

## 当前基线

基础测试环境：

- Python 3.11 / Windows Server 2025
- SQLite FTS5
- 每文件 3 个 Chunk
- 每 Chunk 约 4 KiB 正文
- 首屏 100 个文件结果
- 中文连续词使用 CJK bigram phrase 索引

### 1,000 文件压力基线

逻辑正文约 11.72 MiB，数据库约 17.71 MiB，索引放大约 1.51x。

代表性广泛查询 `客户经理` 命中全部 1,000 个文件时：

- 每次新建 SQLite 连接：约 43 ms
- 常驻只读搜索会话：约 21 ms

常驻查询连接在小到中等索引上是明确收益，因此已进入交互式搜索链路。

### 10,000 文件极端全命中压力基线

逻辑正文约 117.19 MiB，数据库约 175.82 MiB，索引放大约 1.50x。

代表性广泛查询 `客户经理` 命中全部 10,000 个文件时，近期同轮 A/B：

- 新连接 + window Exact：约 324 ms
- 常驻连接 + window Exact：约 308 ms
- 常驻连接 + grouped Exact（late metadata）：约 275 ms

代表性稀有查询 `专项稀有词` 命中约 100 个文件时：

- 常驻 window Exact：约 4.6 ms
- 常驻 grouped Exact：约 4.4 ms

这说明 10k 场景的主要瓶颈不是建立连接，而是广泛命中时对全部 Chunk 做精确的文件级折叠、排名和排序。

### 10,000 文件混合办公 workload

为避免只使用“所有文件都命中”的极端合成数据判断架构，新增了更接近办公目录的混合 workload：

- PDF / DOCX / XLSX / PPTX 混合；
- 多个业务目录；
- 文件修改时间和大小独立分布；
- 关键词只出现在部分文件，通常只位于一个 Chunk；
- 覆盖普通关键词、多关键词、英文短语、文件名命中，以及 ext/path/date/size 过滤；
- 每个 workload 都会校验实际 `total_count` 与生成数据的理论命中数。

Windows GitHub Actions 上 10,000 文件、30,000 Chunk 的生产路径（常驻 SQLite 连接 + window Exact）实测：

| 场景 | 命中文件 | 选择率 | Warm P50 |
| --- | ---: | ---: | ---: |
| 普通常见词 `业务流程` | 2,500 | 25.00% | 21.51 ms |
| 中等词 `身份证有效期` | 834 | 8.34% | 10.98 ms |
| 稀有词 `跨境专项复核` | 50 | 0.50% | 3.60 ms |
| 仅文件名 `专项检查` | 100 | 1.00% | 5.05 ms |
| 多关键词 `客户经理 信贷政策` | 500 | 5.00% | 7.59 ms |
| 英文短语 `"customer manager"` | 200 | 2.00% | 4.92 ms |
| 关键词 + `ext:pdf` | 684 | 6.84% | 12.81 ms |
| 关键词 + `path:信贷管理` | 500 | 5.00% | 13.75 ms |
| 关键词 + `after:2026-01-01` | 307 | 3.07% | 8.35 ms |
| 关键词 + `size:>1MB` | 1,295 | 12.95% | 17.25 ms |
| 纯过滤 `ext:xlsx path:风险合规` | 548 | 5.48% | 2.30 ms |
| 多条件组合查询 | 12 | 0.12% | 2.73 ms |

这一轮逻辑正文约 117.19 MiB，SQLite 数据库约 179.72 MiB，索引放大约 1.53x；12 类查询的理论命中数全部与实际结果一致。

### 当前架构判断

对约 10,000 份办公文档的更真实 workload，生产 Exact 搜索绝大多数 warm P50 落在约 2～22 ms。当前 SQLite FTS5 后端已经满足交互式本地检索的性能目标，因此暂不引入 Tantivy 等新检索后端。

约 300 ms 的慢查询主要出现在“一个关键词几乎命中全部 10,000 个文件”的压力极端情况。该边界继续作为规模压力指标保留，但不应单独驱动后端重构。

下一阶段优先关注：

1. 实际 Office / PDF 文件解析和首次索引耗时；
2. 50k 文件混合 workload 的规模曲线；
3. 文件名索引与正文 Chunk 索引解耦，减少文件名在每个 Chunk FTS 行中的重复；
4. 真实 Windows 办公电脑上的端到端输入响应与内存占用。

只有在真实 workload 下持续无法满足交互延迟目标时，再评估 Tantivy 等专用倒排后端。

## 已验证的优化

### 常驻只读搜索会话：保留

搜索线程复用 SQLite 连接并开启 `query_only`。在 1k 广泛查询中 warm 延迟通常约减半，且搜索语义完全不变。

### Schema v5 `file_id`：保留

Chunk 增加整数 `file_id`，替代热路径上的长 Windows 路径关联。独立布局实验中数据库体积约减少 10%，简化查询约快 1.5x；完整 Exact SQL 中整数关联与路径关联延迟接近，因此不把 1.5x 视为生产搜索收益。

### Grouped Exact：继续实验，不作为默认

Grouped minima 可以避免 `ROW_NUMBER() OVER(PARTITION BY ...)` 的全量窗口排序。在 10k 极端广泛查询中有约 1.1x～1.4x 的收益记录，但 1k 上收益不稳定，部分 run 会略慢。

当前生产默认仍使用 window Exact；grouped engine 保留为严格 Exact 的实验实现。

## 已证伪或暂不采用

### Metadata-first FTS probe：淘汰

先按扩展名/日期/大小筛文件，再逐 rowid probe FTS，虽然结果精确，但在 1k 实验中慢约 10～50 倍。SQLite FTS5 更适合让 `MATCH` 驱动查询。

### 固定候选数 Progressive Top-K：不作为默认

在广泛、强并列的合成工作负载中，固定 8x Chunk 候选无法保证文件级 Recall@100，曾出现 0% 的严格排序召回。除非以后能给出安全停止条件，否则不能替代 Exact 搜索。

### 只靠 PRAGMA 调参：优先级低

10k 极端广泛查询中常驻连接仅比新连接快约 5%～10%，说明规模瓶颈已经转移到精确聚合/排序，而不是连接初始化或 page cache。

## 运行大规模 benchmark

大规模 benchmark 已从普通 push CI 移除。GitHub Actions 中的 `benchmarks` workflow 支持两种模式：

- `office`：混合办公 workload，默认模式，用于判断真实使用体验；
- `saturation`：极端广泛命中压力 workload，用于观察 Exact 搜索的规模上限。

运行时可设置文件数、每文件 Chunk 数、Chunk 大小和查询迭代次数。
