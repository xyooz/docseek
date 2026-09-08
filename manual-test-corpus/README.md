# DocSeek 人工验收测试语料

这个目录用于 **Windows 打包版 / portable 版本的人工验收**，与 `tests/fixtures/` 的自动化单元测试样例分开。

仓库只保留本说明和生成脚本；真正生成出来的语料位于 `manual-test-corpus/generated/`，已加入 `.gitignore`。

## 一键生成

在仓库根目录运行：

```bash
python manual-test-corpus/build.py --clean
```

默认会生成：

```text
manual-test-corpus/generated/
├── 01-normal/
├── 02-mixed/
├── 03-large/
├── 04-broken/
├── 05-wps/
├── TEST_PLAN.md
└── manifest.json
```

可调参数：

```bash
python manual-test-corpus/build.py --clean --large-mb 64 --xlsx-rows 100000
```

- `--large-mb`：大文本文件大小，默认 32 MiB。
- `--xlsx-rows`：大 XLSX 行数，默认 50,000 行。
- `--output`：指定输出目录。

## 五类语料

### 01-normal

复用 `tests/fixtures/official/` 中的真实 Office / PDF / ODT / RTF 样例，并附带一个可直接搜索的文本 sentinel。

### 02-mixed

TXT + XLS + XLSX + WPS/ET 混合目录，重点验证：

- SQLite writer transaction 与 parser lease 不发生自竞争；
- 不再出现 `database is locked`；
- 现代 Office 与兼容格式均能继续索引。

### 03-large

生成大文本和大 XLSX，重点验证：

- 索引进度；
- 点击“停止”的响应速度；
- Stop 后文件回到可重试状态；
- 再次刷新可以继续建立索引。

如果环境缺少 `openpyxl`，脚本会跳过大 XLSX，并在 `TEST_PLAN.md` 中说明。

### 04-broken

包含零字节、截断和伪造的 Office/PDF 文件，用于验证：

- 单个坏文件不会拖死整个任务；
- parser timeout / failure 可以被记录；
- 重启后不会反复卡在同一个坏文件；
- 其它健康文件仍能继续索引。

### 05-wps

从 `tests/fixtures/wps/` 复用或重建真实脱敏 WPS 样例，包括 OLE/CFB WPS、ET、DPS，以及仓库已有的 WPS Office 生成样例。

## 推荐人工验收流程

1. 启动打包后的 DocSeek，把整个 `manual-test-corpus/generated/` 加入索引。
2. 搜索 `DOCSEEK_NORMAL_SENTINEL`、`DOCSEEK_MIXED_SENTINEL`、`DOCSEEK_LARGE_TEXT_SENTINEL`。
3. 在大 XLSX 解析期间点击“停止”，确认任务能较快退出。
4. 再次刷新，确认刚才停止的 XLSX 会重新尝试，而不是永久跳过。
5. 索引期间打开“索引设置”，确认先停止当前任务，再自动打开设置。
6. 多次刷新 `02-mixed/`，确认没有 `database is locked`。
7. 强制关闭 DocSeek 后重新启动，确认不会陷入异常文件启动循环。
8. 检查 `04-broken/`：异常文件允许失败，但正常文件必须继续完成。
9. Windows 环境验证 `05-wps/`；实际解析能力取决于 Tika / WPS Local adapter 是否可用。
10. 查看 `manifest.json`，确认本轮语料文件数量、大小和 SHA-256 稳定。

这套语料主要用于 **打包后真实用户路径的 P0 验收**，不替代 pytest 自动化测试。
