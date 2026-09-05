# Watcher / Indexer Soak Testing

这个工具用于验证 DocSeek 在真实文件系统事件下长期运行时，**磁盘文件状态、watcher 事件和 SQLite 索引是否持续一致**。

它不会修改你的真实文档。每次运行都会创建一个独立的 `docseek-soak-*` 子目录，只在里面生成测试 TXT 文件和测试索引数据库。

## 快速验证

在项目环境中运行：

```bash
python benchmarks/watcher_soak.py --cycles 120
```

测试会循环执行：

- 新增文件；
- 修改文件正文；
- 重命名文件；
- 删除文件；
- 创建应被文件名规则排除的文件；
- 创建目录并移动文件，触发目录级校准。

每个循环等待 watcher 收敛后都会立即核对：

1. 当前磁盘上应该被索引的文件集合与数据库 `files` 完全一致；
2. 每个文件索引正文包含当前最新 marker，而不是旧版本；
3. 已删除 / 重命名旧路径不存在残留索引；
4. `ignore_*` 测试文件不会进入索引；
5. 没有遗留索引问题记录。

因此后续某次全量扫描不会替前面漏掉的 watcher 事件“掩盖问题”。

## 在真实 Windows 办公机上跑

默认使用系统临时目录。如果希望验证某个具体磁盘、同步盘或测试共享盘，可以指定父目录：

```powershell
python benchmarks/watcher_soak.py --cycles 600 --workspace D:\DocSeekTest
```

程序只会在 `D:\DocSeekTest` 下创建类似：

```text
D:\DocSeekTest\docseek-soak-xxxxxxxx\
```

不会扫描或修改 `D:\DocSeekTest` 中原有文件。

### 约 1 小时观察

```powershell
python benchmarks/watcher_soak.py --cycles 3000 --interval 1
```

`--interval` 用于拉长真实运行时间，便于观察休眠 / 唤醒、同步盘抖动、杀毒软件扫描等环境因素。

可以把测试放在目标磁盘或专门的测试共享目录中运行。

## 输出指标

成功后会输出 JSON，包括：

- `cycles`：变更循环数；
- `batches`：watcher 实际产生的批次数；
- `precise_batches` / `precise_paths`：精确增量更新情况；
- `full_rescans`：目录结构变化触发的完整校准次数；
- `max_settle_seconds`：一次文件变化到索引重新一致的最长时间；
- `python_peak_memory_mib`：Python 分配内存峰值，仅用于趋势观察；
- `status: pass`：所有循环均保持一致。

## 失败诊断

失败时测试目录默认**不会删除**，终端会输出：

```text
diagnostic_session=...
```

可以保留该目录检查最后的磁盘状态和 `docseek.db`。

成功运行默认会自动删除测试目录；使用 `--keep` 可以保留。

## 与 CI 的区别

普通 Windows CI 只运行一个很短的真实 watcher smoke，用于确认：

```text
真实文件修改
    ↓
watchdog.Observer
    ↓
WatchBatch
    ↓
DirectoryIndexer.update_paths()
    ↓
新正文进入索引 / 旧正文消失
```

长时间 soak 不放进每次提交的 CI，避免反馈时间过长和环境噪声造成误报。它更适合发布前、重大 watcher / indexer 修改后，以及真实办公机阶段性验证时运行。
