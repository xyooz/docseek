# DocSeek First Successful Search 执行追踪

> 本文是当前 Beta 阶段的执行账本。路线原则见 `docs/NEXT_ITERATION.md`；这里专门记录“做了什么、没做什么、证据是什么、效果如何、下一步是什么”。

最后更新：2026-09-06

## 状态约定

- ✅ 已完成：代码完成、自动化验证通过，且不再有已知待实现项。
- 🟡 待实机验收：代码和 CI 已完成，但仍需要真实 Windows Beta 证明。
- 🔬 验证中：已有实现或实验，数据仍在收集，暂不下最终结论。
- ⏳ 未完成：尚有明确产品/工程工作未实现。
- 🚫 暂缓：第一轮真实 Beta 前明确冻结。

## 当前总览

| 项目 | 状态 | 当前判断 |
|---|---|---|
| P0-1 启动后自动校准 | 🟡 | 已实现并有 UI 生命周期回归；待真实验证关闭期间的新建/修改/重命名/删除均能在下次启动后自动收敛 |
| P0-2 目录先保存、扫描后执行 | 🟡 | 已实现并有回归；待真实验证首次扫描中断、强退、重启后自然恢复 |
| P0-3 索引数据位置可配置 | 🟡 | 后端、bootstrap、设置 UI、安全迁移和回滚均已实现；待真实验证 D/E 盘迁移、重启、升级后持续使用 |
| P0-4 首次使用路径简化 | 🟡 | 无索引目录时已突出“选择资料目录”和本地索引说明；待真实用户可理解性验证 |
| P0-5 可理解的索引状态 | ⏳ | discovery 进度、候选总数已经接入；还需完成/核对“已完成内容可搜索”、完成数/失败数和最终状态文案的一致体验 |
| Blocker：`database is locked` | 🟡 | 已修复多个确定性自锁机制并加回归；最新真实 Portable 测试暂未再次出现，继续观察，不宣称彻底消失 |
| 第一轮真实 Beta | ⏳ | 尚未形成 3～5 台机器 × 500～2,000 真实文件 × 每台 20 条查询的完整结果集 |

## 2026-09-06 可用性审查修复

- 修正索引迁移提交边界：位置配置写入成功后，新库保持权威；即使 Windows 暂时无法删除待迁移标记，也不会回删新库并留下错误指针。
- 索引任务改用窗口独立线程池；关闭窗口会先请求安全取消，任务退出后自动完成关闭。启动校准的全量扫描也显示“停止”按钮。
- 首次全量扫描在第 16、64 个新文档处提前提交，之后继续使用 512 文档批次兼顾吞吐。10k 小文本基准为首次扫描 3.528s、2,834.8 文件/秒。
- 首次欢迎页新增“先设置索引保存位置”，待迁移重启期间禁止误开始首次扫描。
- 删除仓库根目录误提交的 `NONEXISTENT` 临时文件。
- 本地验证：`compileall` 通过；全量 347 个测试通过，耗时 95.176s。

## P0 明细

### P0-1 启动后自动校准

**目标**

DocSeek 关闭期间发生的文件变化，在下次启动后无需用户手动“刷新索引”即可自动收敛。

**已做**

- 启动后读取 active roots；
- watcher 启动后安排后台 reconciliation；
- 校准期间已有内容仍可搜索；
- 已有回归覆盖启动 reconciliation。

**关键证据**

- `ee302677` — `test: cover startup reconciliation and root persistence`
- 自动测试：`test_preconfigured_active_roots_schedule_startup_reconciliation`

**未完成 / 待验收**

- [ ] 真实 Windows：关闭 DocSeek 后新增文件，重开后自动出现；
- [ ] 关闭期间修改正文，重开后旧内容消失、新内容可搜；
- [ ] 关闭期间重命名/删除，重开后旧路径不残留；
- [ ] 多目录同时配置时行为一致。

---

### P0-2 目录配置先于首次扫描持久化

**目标**

“用户选择这个目录”是持久配置；首次扫描只是可失败、可取消、可恢复的任务。

**已做**

- 用户选目录后先写入 index root；
- watcher 立即切到新目录；
- 再启动首次扫描；
- 下次启动可依靠 P0-1 自动继续收敛。
- 关闭窗口时会先安全取消活动索引，已提交批次保留，下次启动继续收敛。

**关键证据**

- `ee302677` — startup reconciliation/root persistence 回归
- 自动测试：`test_adding_root_persists_before_first_scan_and_restarts_watcher`

**未完成 / 待验收**

- [ ] 首次扫描进行中直接关闭程序，再打开后能够自然继续；
- [ ] 首次扫描遇到坏文件不会丢失 root 配置；
- [ ] 断电/强退等异常退出后无“半配置”状态。

---

### P0-3 索引数据位置可配置

**目标**

真正占空间的 SQLite 索引可以放到 D/E 等本地磁盘，而控制配置保持小而稳定。

**已做**

- 索引数据目录与控制配置目录分离；
- 首次欢迎页可在选择资料目录之前安排非系统盘位置；
- 设置中显示当前索引位置并允许选择新目录；
- 当前进程只登记迁移请求，不运行中硬搬 WAL/SHM；
- 下次启动在 SQLite 打开前执行 SQLite backup；
- `quick_check` / Schema 校验；
- 原子切换配置；
- 失败时旧数据库继续作为权威数据；
- Portable/Beta snapshot 路径跟随配置。

**关键证据**

- `fb86847a` — `feat: expose index storage location in settings`
- 自动测试包括：
  - `test_product_startup_applies_staged_storage_move_before_app_open`
  - `test_apply_move_copies_valid_index_switches_config_and_removes_old_db`
  - `test_config_switch_failure_keeps_old_index_authoritative`
  - `test_change_location_stages_restart_migration`

**未完成 / 待验收**

- [ ] Portable：C 盘 → E 盘真实迁移；
- [ ] Setup：C 盘 → E 盘真实迁移；
- [ ] 迁移后重启搜索、watcher、备份/恢复均正常；
- [ ] 升级新版本后仍自动使用原位置；
- [ ] 大数据库迁移时的用户反馈和失败提示可理解。

---

### P0-4 首次使用路径

**目标**

第一次打开只需要理解：选择目录 → 等待/边索引边搜索 → 找到并打开文件。

**已做**

- 无索引 root 时使用 first-run empty state；
- 主动作突出“选择资料目录”；
- 明确本地建立索引、不上传文档；
- 已配置用户直接进入完整搜索工作区；
- 高级筛选、历史、收藏等不作为首次使用主路径。

**自动测试**

- `test_empty_profile_focuses_on_choose_directory`
- `test_existing_profile_skips_first_run_panel`
- `test_selecting_first_root_switches_to_search_workspace_immediately`

**未完成 / 待验收**

- [ ] 找 3～5 名未参与开发的测试者，观察是否无需解释就能开始；
- [ ] 记录首次成功搜索耗时（Time to First Successful Search）；
- [ ] 记录用户是否误以为必须等全部索引完成。

---

### P0-5 可理解的索引状态

**目标**

普通用户能知道“现在在做什么、已经能不能搜、还有没有失败”，无需理解 SQLite/FTS/WAL。

**已做**

- discovery 阶段提供正式回调；
- 候选总数在内容索引开始前确定；
- 桌面现有 progress channel 能看到 discovery 和最终 candidate total；
- 大目录不再只显示没有变化的“准备建立索引”。

**关键证据**

- `fb057665` — `feat: expose index discovery progress`
- `d3a46e7c` — discovery progress contract 回归
- `9d6ed4b3` — desktop progress channel 回归

**仍需完成**

- [ ] 统一产品文案：发现阶段 / 索引阶段 / 完成阶段；
- [ ] 明确显示 `已完成 X / 共 Y`；
- [ ] 明确显示“已完成的内容现在可以搜索”；
- [ ] 完成后显示索引文件总数；
- [ ] 有失败时显示失败数量，并提供“查看问题”；
- [ ] 避免把内部阶段名、WAL、Chunk、Schema 暴露给普通用户。

## Blocker / 稳定性追踪

### `database is locked`

**历史现象**

真实 Portable 在建立索引时，UI 左下角曾出现 `database is locked`。

**已经修复的机制问题**

1. 运行期重复 `PRAGMA journal_mode=WAL` / Schema 初始化；
2. Office/PDF 慢解析发生在 SQLite writer transaction 内；
3. 多层目录惰性 discovery 与 `IndexIssueStore` 第二 writer 自锁；
4. discovery issue 写入改为发现完成后的批量处理；
5. SQLite 锁回归覆盖活跃 reader/writer、嵌套目录扫描等场景。

**当前证据**

- 用户最新真实 Portable 测试：暂未再次出现；
- 自动测试持续通过：
  - `test_nested_full_scan_does_not_self_lock_during_lazy_discovery`
  - `test_runtime_reopens_do_not_reinitialize_schema_with_active_reader_and_writer`
  - `test_metadata_reads_succeed_while_chunk_writer_holds_wal_transaction`

**状态**：🟡 继续观察。

**退出条件**

- [ ] 同一真实大目录连续重建/刷新多轮无锁错误；
- [ ] 索引时连续搜索、历史/设置写入无锁错误；
- [ ] watcher 增量更新 + 手动刷新并发无锁错误；
- [ ] 睡眠/唤醒后继续运行无锁错误；
- [ ] 24h soak 无锁错误。

## 性能优化账本

> CI runner 有明显磁盘抖动。跨 run 的总 wall-clock 只作参考；优先相信同一 run A/B、阶段占比和机制级计时。

| 方向 | 改动 / 实验 | 实测证据 | 决策 |
|---|---|---|---|
| 大目录 discovery | discovery issue 批量化、无排除时跳过 resolve、`os.scandir()`/DirEntry 复用 | 10k discovery 从早期约 0.5～0.8s 降到约 0.3～0.4s 量级 | ✅ 保留 |
| unchanged stale cleanup | 已匹配路径不再重复 `resolve()` | `missing_cleanup` 4.226s → 0.014s | ✅ 保留 |
| discovery path reuse | 普通文件复用 discovery 已规范化父目录；symlink 保持完整 resolve | `normalize_post` 0.867s → 0.000s；10k unchanged 曾达到约 0.9s | ✅ 保留 |
| XLSX 解析 | Calamine 高优先级快路径 + openpyxl fallback；语义等价门禁 | 同一 4 万行表约 7.9～8.7× parser 加速，chunk/工作表/行范围/正文等价 | ✅ 保留 |
| 小文本 full-scan batch | 128 → 512，保留 8M 文本上限 | sweep：128 约 13.747s、512 约 5.584s；production 10k 曾约 8.482s → 7.563s | ✅ 保留 512，不上 1024 |
| 纯文本读取 | 单句柄编码探测；小文件复用 probe decode；去掉冗余 source stat | extraction 10k 曾降到约 1.1s；UTF-8/GB18030/大文件流式均有回归 | ✅ 保留 |
| 纯文本 structure sidecar | `行 x-y` 直接写 `text-range`，异常 locator 回退 DocIR | `structure_total` 0.379s → 0.106s；`structure_cpu` 0.271s → 0.034s | ✅ 保留 |
| 新文件 writer fast-path | 新文件跳过不存在的旧 chunks 删除 | 语义正确；跨 runner wall-clock 波动大，净收益不够可靠 | 🟡 保留简单实现，不继续围绕它微调 |
| SAVEPOINT 语句复用 | 保留每文档故障隔离，仅减少固定语句构造/调用开销 | 有回归保护；总体收益继续结合 profile 观察 | 🟡 保留，禁止取消每文档隔离 |
| 小单 chunk 文本 fast-path | `901d4181`；同 run A/B benchmark 已加入并在 #456 通过 | 数字继续作为后续性能基线记录 | 🔬 验证中 |
| Compact CJK | 压缩候选索引 | 建库约 1.34×、体积约 23.7%，但高频中文查询可退化到 50～60ms | 🚫 当前不采用 |
| Native trigram hybrid | 用空间换中文查询速度 | 建库仅约 1.07×，DB 膨胀到约 1.48×，查询更快 | 🚫 当前痛点不匹配，不采用 |
| USN/MFT | 尚未进入生产方案 | 普通 discovery/reconcile 已有明显优化，尚无证据必须上 | 🚫 第一轮 Beta 前冻结 |

## 当前性能基线解释

性能数字不要只看某一次 GitHub runner 的总时间。例如相邻 run 中 SQLite `commit_sql` 曾从约 0.968s 抖到 5.526s，同时单文件 update 也异常变慢，说明 runner 磁盘 I/O 抖动显著。

因此后续性能改动必须满足至少一条：

1. 同一 run A/B 明显更快；
2. profiler 中目标阶段稳定下降；
3. 真实 Windows 同一目录前后对比有改善；
4. 不以增加锁持有时间、内存峰值或牺牲检索语义换取跑分。

## 第一轮真实 Beta 验收表

| 验收项 | 自动化状态 | 真实 Beta 状态 |
|---|---|---|
| Win10/Win11 无 Python 启动 | CI/打包链路有保护 | ⏳ 待多机 |
| 普通用户权限 Setup | 有 installer smoke | ⏳ 待真实办公机 |
| Portable 正常启动 | 已多轮人工运行 | 🟡 已开始，需继续 |
| 选择 500～2,000 真实文档目录 | 有 synthetic/fixture | 🟡 已有稍大真实目录测试，未形成正式记录 |
| 已完成内容可立即搜索 | 核心检索测试覆盖 | ⏳ 待用户路径验收 |
| 20 条真实查询 Top5 | curated benchmark 不是用户数据 | ⏳ 未开始正式记录 |
| 双击打开目标文件 | UI 有能力 | ⏳ 待真实验收 |
| 新建/修改/重命名/删除自动更新 | watcher 自动测试覆盖 | ⏳ 待真实目录验收 |
| 关闭期间变化下次启动收敛 | 自动回归覆盖 | ⏳ 待实机 |
| 首次扫描中断后恢复 | 自动回归覆盖配置语义 | ⏳ 待实机 |
| 坏文件不阻塞整体任务 | 自动测试覆盖 | ⏳ 待真实文件混合集 |
| 索引迁移到非 C 盘 | 后端/UI/回滚测试覆盖 | ⏳ 待实机 |
| 连续运行 24h | soak harness 已有 | ⏳ 未完成正式结果 |
| 无 `database is locked` | 针对性回归持续通过 | 🟡 当前实机暂未再现，继续观察 |

## 接下来执行顺序

### A. 先收完 P0-5

1. 统一 discovery / indexing / complete / partial-failure 四类状态；
2. 显示候选总数、已完成数、失败数；
3. 明确“已完成内容现在可以搜索”；
4. 做 UI 回归，不增加第二套扫描逻辑。

### B. 打新的 Portable RC 做完整路径回归

按同一真实稍大目录依次验证：

```text
首次启动
→ 选择目录
→ discovery 反馈
→ 边索引边搜索
→ 找到并打开
→ 修改/重命名/删除
→ 关闭 DocSeek 后继续修改文件
→ 重开自动校准
→ 中断一次全量扫描再重开
→ 索引迁移到 E/D 盘
→ 连续运行并观察 database is locked
```

### C. 建立第一份正式 Beta 结果

先从 1 台机器跑完整模板，再扩到 3～5 台；不要一开始追求 50k 文件。

### D. 性能优化只做剩余真实阻塞点

- 优先同 run A/B；
- 优先普通文件路径和格式专用成熟解析器；
- 不取消 SAVEPOINT / source-change guard；
- 不因 GitHub runner 某一次 commit 抖动调整 SQLite 策略；
- 第一轮 Beta 前继续冻结 OCR、语义搜索、USN/MFT、搜索引擎替换。

## 更新规则

以后每次重要迭代至少更新以下一项：

- P0 状态；
- 新的真实 Beta 证据；
- 性能前后数据；
- 被否决的实验及原因；
- 下一步动作。

只有“代码完成 + CI 通过 + 对应真实 Beta 场景通过”才能把产品类 P0 从 🟡 改成 ✅。
