# Retrieval quality gate

DocSeek 的检索优化不能只看查询延迟。正式搜索链路首先要求结果正确，其次才是更快。

## 当前自动指标

`benchmarks/evaluate_retrieval.py` 使用一组可重复的本地办公语料样例，对 `ExactGroupedSearchEngine` 计算：

- **Recall@K**：应该找到的相关文件有多少进入前 K；
- **MRR**：第一个相关结果是否足够靠前；
- **nDCG@K**：高相关文件是否排在低相关文件前面。

Windows CI 默认执行：

```text
python benchmarks/evaluate_retrieval.py --k 5 --assert-baseline
```

当前门槛：

- Recall@5 = 1.0；
- MRR >= 0.95；
- nDCG@5 >= 0.90。

这些门槛针对仓库内的小型 curated corpus，是**回归保护线**，不是对真实办公语料质量的最终宣称。

## 排序实验规则

后续增加或调整下列信号时，都应先扩充 relevance cases，再比较质量指标：

- 文件名精确 / 前缀 / 包含命中；
- 正文 BM25；
- 工作表名、章节标题、幻灯片标题；
- 同一文件多 Chunk 命中次数；
- 最近修改时间；
- 未来可能加入的分词、拼音、近似匹配或语义召回。

原则：

1. 不允许用更低 Recall 换取肉眼难以感知的几十毫秒收益；
2. 近似 Top-K 只有在 Recall 经真实数据证明足够高时才可进入主链路；
3. 所有自动 boost 都应保持小权重，避免覆盖明显更强的正文/文件名证据；
4. synthetic / curated corpus 只负责防回归，真实质量结论必须来自脱敏的实际办公检索案例。

## 下一步

逐步建立一个不含敏感正文的真实查询评测集，只保存：

```text
query
relevant_document_aliases
relevance_grade
optional_reason
```

文档使用匿名别名，不把真实文件名、路径和正文提交到仓库。这样可以持续测量 DocSeek 在真实办公检索任务上的 Recall@K、MRR 和 nDCG，而不泄露业务数据。
