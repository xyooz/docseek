# DocSeek Beta 快照采集

Beta 验证时可以使用内置辅助脚本自动记录不涉敏的运行快照，减少手工抄写版本、Schema、索引规模和 SQLite 占用。

## 使用方法

先让 DocSeek 至少完成一次索引，然后在源码开发环境中运行：

```powershell
python benchmarks/collect_beta_snapshot.py
```

默认读取：

```text
%USERPROFILE%\.docseek\docseek.db
```

默认输出到：

```text
%USERPROFILE%\.docseek\beta-reports\
```

每次生成两个同时间戳文件：

```text
beta-snapshot-YYYYMMDD-HHMMSS.json
beta-snapshot-YYYYMMDD-HHMMSS.md
```

需要测试其他数据库副本时可以显式指定：

```powershell
python benchmarks/collect_beta_snapshot.py --db D:\beta\docseek.db --output-dir D:\beta\reports
```

如果数据库不存在，脚本会直接报错退出，不会为了生成报告而创建新的空索引库。

## 前后快照对比

在 4 小时、8 小时或跨睡眠的 Soak 测试中，建议在开始和结束时各采集一次快照：

```powershell
python benchmarks/collect_beta_snapshot.py
# 正常使用一段时间
python benchmarks/collect_beta_snapshot.py
```

然后直接比较两份 JSON：

```powershell
python benchmarks/compare_beta_snapshots.py `
  .\beta-snapshot-20260906-090000.json `
  .\beta-snapshot-20260906-170000.json
```

默认在后一份快照所在目录生成：

```text
beta-comparison-YYYYMMDD-HHMMSS.json
beta-comparison-YYYYMMDD-HHMMSS.md
```

也可以指定输出目录：

```powershell
python benchmarks/compare_beta_snapshots.py before.json after.json --output-dir D:\beta\compare
```

对比报告会自动计算：

- 两次快照的时间间隔；
- 已索引文件数变化；
- 持久化索引问题总数变化；
- 各错误代码数量变化；
- `docseek.db`、WAL、SHM 及 SQLite 总占用变化；
- 最近完整校准时间是否变化；
- DocSeek 版本和 Schema 是否发生变化；
- 活动 / 暂停索引根数量变化。

对比器只对确定性较强的情况给出“需要关注”提示，例如：

- 结束快照 Schema 与程序期望值不一致；
- 持久化索引问题数量增加；
- 已索引文件数减少，但测试期间并未主动删除 / 移动 / 排除文件；
- 快照时间顺序异常。

**WAL 或数据库变大不会被自动判定为故障。** SQLite 文件增长需要结合测试期间是否发生新增索引、重建、checkpoint 等行为判断。对比报告会把变化量客观列出来，供测试者结合场景分析。

## 自动记录内容

快照会自动记录：

- DocSeek 版本；
- 当前 / 期望 Schema 版本；
- Windows / OS、架构、Python、SQLite 版本；
- 逻辑 CPU 数；
- 物理内存总量（可读取时）；
- 已索引文件数量；
- 索引目录总数 / 活动数 / 暂停数；
- 索引问题总数和按错误代码汇总；
- 最近一次完整校准时间；
- `docseek.db`、WAL、SHM 分项及总占用。

Markdown 文件还保留人工补录栏，用于填写：

- A Smoke / B Scale / C Soak 阶段；
- 真实源文件数；
- 首次索引耗时；
- 第二次无变化校准耗时；
- CPU / 内存峰值；
- 真实查询 Top1 / Top5 / Top10；
- 查询改写数；
- Blocker / Major / Minor 数量和备注。

完整 Beta 流程仍以 [`BETA_TESTING.md`](BETA_TESTING.md) 和 [`BETA_RESULT_TEMPLATE.md`](BETA_RESULT_TEMPLATE.md) 为准。

## 隐私边界

Beta 快照复用 DocSeek 的脱敏诊断数据，并额外只增加机器容量和 SQLite 文件大小等聚合信息。报告设计上不包含：

- 数据库路径；
- 索引根目录路径；
- 文件路径或文件名；
- 文档正文或标题；
- 索引问题详情；
- 搜索历史或收藏查询。

快照对比器同样不会把两份输入快照的本地文件路径写进 JSON/Markdown 对比报告。

即使报告本身按上述边界设计为脱敏，外发前仍应按照所在单位的数据处理规范复核。
