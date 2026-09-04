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
