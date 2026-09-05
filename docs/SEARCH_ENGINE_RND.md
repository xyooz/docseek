# DocSeek Search Engine R&D

本文档记录 DocSeek 检索内核的研发方向。目标不是为了“使用新技术”而重写，而是在 **Windows、本地离线、中文 Office 文档、低资源、可部署** 的约束下，用统一基准验证新的索引与查询方案。

## 1. 核心指标

任何新方案必须和当前 SQLite FTS5 Exact Search 使用同一份数据比较：

- 首屏 Top-100：cold / warm P50 / P95
- Exact Recall@100：必须 100%，除非功能明确标记为 approximate
- 首次索引：files/s、MiB/s
- 单文件增量更新延迟
- 未变化目录校准速度
- 数据库 / 索引放大率
- 进程常驻内存
- Windows 文件锁、崩溃恢复和迁移复杂度

日常 CI 使用 1k 文件 smoke；`.github/workflows/benchmark-scale.yml` 手动运行 10k / 50k。

## 2. Baseline：SQLite FTS5 Exact

当前生产基线继续保留：

- Chunk 级正文索引；文件级精确聚合
- 中文连续文本：overlapping bigram phrase
- 英文 / 混合文本：unicode61
- BM25 + 文件名精确 / 前缀 / 包含加权
- contentless FTS，正文只在 `chunks` 保存一份
- 批量事务索引、Watchdog 单文件精准增量
- GUI 搜索后台线程执行，generation 丢弃过期请求

所有实验内核必须证明比该基线更有价值才允许替换。

## 3. Track A：FTS5 深度优化

### A1. `rank` early-limit A/B

SQLite 官方说明，在 `ORDER BY ... LIMIT` 场景中，FTS5 隐藏 `rank` 列可能比直接调用 `bm25()` 更容易提前终止。

实验：

- 保持文件级最终排序完全一致；
- 尝试先以 `rank MATCH 'bm25(...)'` 获取候选，再进行文件级精确排序；
- 必须验证 Recall@100=100%，不能重复 Progressive Top-K 的错误。

### A2. 两阶段位置索引

当前中文 phrase 需要位置信息。实验性方案：

1. CJK FTS 使用更轻的 `detail=none/column` 做候选召回；
2. 从 `chunks.content` 对候选执行连续子串精确验证；
3. 只对最终候选进行排序与 snippet 生成。

目标：继续降低索引放大率和写入成本，同时保持中文连续查询精确性。

风险：候选过宽时会把成本从索引阶段转移到查询阶段，因此必须在 10k / 50k 上验证。

### A3. Segment maintenance

测试 FTS5 incremental merge / crisis merge / optimize 的策略，不在前台保存文件时触发不可预测的大合并。

目标：长期运行数月后，查询和增量写入性能不因 segment 数持续恶化。

## 4. Track B：文件级预过滤

Office 检索经常带有结构条件：类型、目录、日期、大小。

当前这些过滤条件通过 SQLite 元数据 JOIN 参与查询。可实验：

- 为 file_id 建立紧凑整数 ID；
- 对 `extension / root / date bucket / size bucket` 建立位图集合；
- 在进入正文排名前先做 bitmap intersection；
- 正文倒排结果只处理允许的 file_id。

候选实现可以先使用 Python integer/bitarray 验证，再考虑 Rust Roaring Bitmap。

适用场景：

`客户经理 ext:pdf path:制度 after:2026-01-01`

如果 50k 文件中最终只允许 2k 文件参与正文排名，收益可能明显。

## 5. Track C：Rank-safe Top-K

之前的 Chunk 级 Progressive Top-K 在大量同分文档下 Recall@100=0%，因此不能进入产品。

后续只研究 **rank-safe** 或能够明确给出近似模式的方案：

- Block-Max WAND / MaxScore
- block / superblock score upper bound
- 文件级而不是 Chunk 级的 upper bound
- Top-K heap threshold 动态提高后跳过不可能进入结果集的 block

关键要求：DocSeek 最终排序包含 BM25、文件名 boost、修改时间 tie-break，因此 upper bound 必须覆盖这些排序信号，不能只对 Chunk BM25 剪枝。

## 6. Track D：Tantivy 实验后端

不直接替换 SQLite。先设计可替换后端：

```text
Document Extractor
      ↓
Normalized DocumentChunk
      ↓
SearchBackend interface
      ├─ SQLiteFtsBackend   ← 当前稳定基线
      └─ TantivyBackend     ← 实验
```

Tantivy 重点验证：

- Block WAND / Top-K 广泛命中查询
- mmap 下 Windows 行为与文件更新 / 删除
- segment merge
- 中文 tokenizer / bigram tokenizer 可实现性
- Python-Rust 边界成本（PyO3/maturin）
- 打包体积、无管理员安装、内网部署复杂度

只有在 10k / 50k 下获得显著收益，且不明显恶化安装、安全和维护成本时才考虑切换。

## 7. Track E：中文办公检索专项

这是 DocSeek 最有机会形成差异化的方向。

### E1. Adaptive CJK indexing

不是所有 Chunk 都需要同一套 token：

- 连续中文较多：bigram
- 英文 / 数字较多：unicode61
- 表格 Chunk：保留字段/单元格边界信号
- 标题、Sheet 名、页标题：单独高权重 field

减少无用 token，同时提高结构信息权重。

### E2. Structured Office scoring

将 Office 结构加入排序，而不是只看纯文本 BM25：

```text
score = content_bm25
      + filename_boost
      + heading_boost
      + sheet_name_boost
      + exact_phrase_boost
      + small_recency_signal
```

例如搜索“客户经理”，Excel Sheet 名直接叫“客户经理名单”应明显优于正文随机出现一次。

### E3. Query intent routing

根据输入自动选择最便宜且正确的检索路径：

- `ext:pdf after:...` → metadata only
- 文件名高度明确 → filename first
- 2+ 连续中文 → CJK substring index
- 英文 phrase → positional unicode index
- 复杂多条件 → metadata prefilter + FTS

这比所有查询统一走一条 SQL 更有机会降低尾延迟。

## 8. Track F：可选语义检索（后期）

只有关键词内核稳定后再做。

候选方案：

- Chunk 级本地 embedding
- BM25 / sparse retrieval 负责高精度词项召回
- embedding 负责“记得意思、记不得原词”的补充召回
- Reciprocal Rank Fusion 或轻量 rerank

默认仍保持纯本地；语义索引必须可以关闭。

## 9. 决策门槛

任何实验进入默认产品前至少满足：

1. Windows CI 全绿；
2. Exact 模式 Recall@100=100%；
3. 10k 文件 P95 无明显回退；
4. 50k 文件至少一个关键指标有可重复的显著提升；
5. 索引体积 / 内存 / 打包复杂度没有不可接受增长；
6. 迁移方案明确，可以从旧 Schema 安全升级或重建。

## 10. 近期执行顺序

1. 运行 10k SQLite Exact baseline；
2. 运行 50k baseline；
3. 测 `rank` + LIMIT 是否能在保持精确排序的前提下降低候选成本；
4. 测元数据预过滤收益；
5. 测 CJK `detail=none/column + raw-content verify`；
6. 建立 `SearchBackend` 接口；
7. 实现 Tantivy 最小实验后端；
8. 同数据 A/B，决定是否值得进入产品主线。
