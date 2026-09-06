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

即使报告本身按上述边界设计为脱敏，外发前仍应按照所在单位的数据处理规范复核。
