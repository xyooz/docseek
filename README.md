# DocSeek

DocSeek 是一个面向 Windows 办公环境的本地全文检索工具：像 Everything 一样快速定位文件，但重点解决“**记得正文内容，不记得文件名**”的问题。

当前版本：**0.2.1 Beta**（以 `src/docseek/__init__.py` 为准）。核心检索、增量索引、Windows 打包和发布流水线已经建立，当前重点是 **结构定位体验、真实办公机 Beta 验证与长期稳定性**。

> DocSeek 完全本地运行，不上传文档内容；但本地索引会保存从文档中提取的正文，因此索引数据库本身也应按可能包含敏感信息的数据文件对待。

## 当前能力

### 搜索与结果

- SQLite FTS5 本地全文倒排索引；
- 中文连续文本 overlapping-bigram 辅助索引；
- 英文、混合文本和带空格短语使用 unicode61；
- BM25 + 文件名精确/前缀/包含加权；
- 文件级精确分页，每批 100 个文件；
- 180ms debounce 输入即搜；
- `ext:` / `path:` / `after:` / `before:` / `size:` 元数据筛选；
- `page:` / `slide:` / `sheet:` 结构定位提示；
- Excel 工作表名、PPT/Word 标题等轻量结构排序信号；
- 相关性 / 最近修改 / 文件名排序；
- 搜索总数和耗时显示；
- 搜索历史、常用搜索收藏、筛选 Chips、F1 搜索帮助；
- `QTableView + QAbstractTableModel` 增量结果列表；
- 命中位置、上下文预览和关键词高亮；
- 按需查看 PDF 命中页、XLSX 命中行范围（保留行号、列位置及首行参考）；
- 预览在独立子进程中运行，15 秒超时，关闭即终止；检测到文件变化时提示重新搜索；
- 零结果页提供保留关键词移除元数据筛选、索引状态提示和设置入口；
- 同一文件全部命中按文档顺序分页浏览，每批 30 处；可继续打开各处的原文件位置；
- 双击打开文件、打开所在位置、复制路径。

### 索引与稳定性

- 多个索引目录；
- 单目录暂停 / 恢复，暂停时保留已有搜索结果；
- 排除目录和文件名通配规则；
- 可逐项选择参与索引的文件格式；新建索引默认启用常用 Office / WPS、PDF、文本和网页格式，XML 等长尾格式按需开启；
- watchdog 精确单文件增量更新；
- 目录结构变化时回退到根目录校准；
- watcher 单 Timer 防抖，避免高噪声环境制造大量线程；
- 修改才重建、删除自动清理；
- 单文件默认 200 MB 保护阈值，可调整；
- 跳过 `.git`、`node_modules`、Office `~$` 临时文件等高噪声内容；
- 索引问题持久记录：权限、损坏文件、超限文件、解析异常等；
- 问题文件支持精确重试 / 批量重试；
- 索引概况显示文件数、数据库占用、活动/暂停目录、问题数和最近完整校准时间；
- Schema Version + 显式迁移；
- Extractor Revision 渐进式重建，解析器升级只刷新受影响格式。

### 支持格式

优先使用结构保真度最高的专用解析器：

- 文本、网页与 XML：`.txt` `.md` `.log` `.csv` `.tsv` `.html` `.htm` `.xhtml` `.xml`
- Word：`.docx`
- Excel：`.xlsx`
- PowerPoint：`.pptx`
- PDF：`.pdf`

可选兼容后端：

- Calamine：`.xls` `.xlsb` `.ods`
- `iscc-tika` 原生兼容：`.wps` `.et` `.ett` `.etx` `.ettx` `.dps`、`.eml` `.msg` `.epub`、旧 Office、开放文档及其他长尾格式
- Windows 本机 WPS COM 兜底：部分旧 Office / WPS 格式

WPS 专有格式优先由本地 `iscc-tika` 原生库解析，不要求安装 WPS Office；Windows WPS COM 仅作为可选兜底。仓库已有真实 `.wps/.et/.dps` 回归样本，`.ett/.etx/.ettx` 已接入同一 Tika 路径，仍建议继续补充来自实际 WPS 版本的独立样本。

XML 使用本地流式解析器。标准 PPTX 保留结构化内置解析；如果检测到旧 OLE 容器，或内置解析器在输出任何内容前无法打开包，则自动尝试隔离的 Tika / WPS 兼容解析器。

## Windows 使用

DocSeek 已有两种经过 CI 实际启动验证的交付方式。

### Setup.exe

推荐普通用户使用：

```text
DocSeek-<version>-Setup-x64.exe
```

特点：

- 每用户安装；
- 默认安装到 `%LOCALAPPDATA%\Programs\DocSeek`；
- 正常安装不要求管理员权限 / UAC 提权；
- 默认创建开始菜单入口；
- 桌面快捷方式可选；
- 用户无需安装 Python；
- 卸载删除程序文件，但**默认保留** `%USERPROFILE%\.docseek` 中的索引和设置，避免误删用户状态。

### Portable ZIP

无需安装：

```text
DocSeek-<version>-Windows-x64.zip
```

解压后直接运行 `DocSeek.exe`。CI 会把最终 ZIP 解压到新的干净目录，再执行 frozen smoke，避免“构建目录能运行、交付 ZIP 缺文件”的问题。

> 当前 Beta 构建尚未完成商业代码签名。正式扩大分发范围前仍需评估代码签名、企业软件分发策略和终端安全策略。

## 首次使用

1. 启动 DocSeek；
2. 添加一个或多个允许检索的目录；
3. 等待首次索引完成；
4. 直接输入正文关键词搜索；
5. 如果需要缩小范围，再使用筛选或高级语法。

索引数据库默认位于：

```text
%USERPROFILE%\.docseek\docseek.db
```

## 搜索示例

普通搜索：

```text
客户经理
信贷业务
身份证有效期
```

组合筛选：

```text
信贷 ext:pdf
客户经理 path:制度
制度 after:2026-01-01
日报 before:2026-09-01
客户 size:>10MB
制度 ext:pdf after:2026-01-01 size:<=50MB
"customer manager" manual
```

只使用筛选条件也可以直接浏览文件：

```text
ext:pdf
after:2026-01-01
ext:xlsx size:>10MB
```

结构提示：

```text
信贷业务 page:12
年度总结 slide:4
客户经理 sheet:"客户 数据"
```

错误或未知筛选语法不会让搜索失败，而会按普通文本处理。

## 架构

```text
用户选择的一个或多个目录
            ↓
       Watchdog 监听
            ↓
精确文件事件 / 目录结构事件
     ↓                 ↓
update_paths       根目录校准
     └────────┬────────┘
              ↓
       DirectoryIndexer
              ↓
       ExtractionBroker
              ↓
  文本 / Word / Excel / PPT / PDF
  Calamine / Tika / WPS fallback
              ↓
         Structured DocIR
              ↓
         SQLite schema v11
   ├─ files / settings / index_issues
   ├─ chunks（原文副本）
   ├─ chunk_index（unicode61）
   └─ chunk_index_cjk2（CJK bigram）
              ↓
   ExactGroupedSearchEngine
   ├─ BM25
   ├─ 文件名加权
   ├─ 结构意图加权
   └─ 文件级精确分页
              ↓
      结果列表 / 预览 / 打开
```

### 原文存储

从 schema v7 起，新写入 Chunk 的原文副本使用版本化 zlib BLOB：

```text
原始文本 → UTF-8 → DSZ1 + zlib → chunks.content
```

FTS5 仍接收原始 Unicode 文本，因此压缩不会改变搜索语义。旧 TEXT 行继续兼容读取，升级不会为了压缩而一次性重写整库；文件以后自然重建时再迁移。

后续 schema 在此基础上继续完善索引结构和迁移契约；当前版本为 v11，以 `src/docseek/schema.py` 为准。v11 新增 `chunk_structure` 附加表，保存页码、工作表、行范围、标题等独立字段。升级时不重写已有正文；新写入保存结构，旧记录继续兼容位置标签。

### 位置预览与本轮改进

搜索命中 PDF 或 XLSX 后，点击右侧“查看原文件命中页 / 行”。PDF 展示对应页面；XLSX 展示命中块起始处最多 200 行、前 50 列，保留空行和列位置。第 1 行仅作为参考，不自动判断为表头。公式显示缓存值，单元格最多展示 1000 字符。

预览读取当前源文件，输出通过内存管道传递，不主动写入预览缓存；元数据变化或读取期间发生变化会拒绝展示。该检查不是完整内容哈希校验。暂不提供页内关键词框选、OCR 或 Office 外部程序精确跳转。

Word 解析现在保留段落与表格的实际顺序，并在标题边界分块。DOCX 提取版本提升为 3，后续校准时仅对过期格式渐进重建，无需手动删除数据库。

实施范围、验证和后续阶段见 [`docs/NEXT_ITERATION.md`](docs/NEXT_ITERATION.md)。

### 第二轮：结构存储、多处命中与更新保护

搜索后点击“查看此文件的全部命中位置”，可按文档顺序翻页浏览，每批最多 30 个匹配块（不是逐词计数）。查询在后台执行，支持取消与 5 秒 SQL 执行时限。索引文件的大小或修改时间已变化时，需要重新搜索后打开列表。

增量更新和全量扫描现在共用原子内容/状态提交路径，Office/PDF 解析先进入 spool 再获取写锁。提交前检查源文件身份、大小、纳秒修改时间及取消状态；解析中变化的文件保留待重试状态，删除或取消不写回本次内容。文件系统检查与数据库提交并非跨系统原子操作，最终一致性仍由 watcher 与周期校准保证；这不是完整的持久化任务 generation 系统。

## 为什么生产搜索不用 Progressive Top-K

仓库保留多种搜索实验。Progressive Top-K 在部分高频查询上更快，但密集结果场景曾出现明显 Recall@100 损失，因此没有进入用户搜索主链路。

DocSeek 当前坚持：**先保证结果语义正确，再优化几十毫秒的延迟。**

生产交互搜索继续使用 `ExactGroupedSearchEngine`。

## 性能与质量门槛

Windows CI 固定包含：

- compileall；
- 桌面模块 smoke import；
- 1k 文件快速搜索 benchmark；
- persistent search benchmark；
- Recall@K / MRR / nDCG curated 回归集；
- directory scan smoke；
- 完整 unittest。

另外保留 10k / 50k / 更大规模压力档以及真实 XLSX 等 one-shot benchmark。CI 合成负载已经达到既定 10k / 50k 查询延迟目标，但这些结果**不能替代真实办公机测试，也不能解释成正式 SLA**。

## 脱敏诊断

Beta 阶段出现问题时，可以在：

```text
索引设置 → 索引概况 → 导出脱敏诊断…
```

导出的 JSON 只包含聚合信息，例如：

- DocSeek / schema 版本；
- Windows、Python、SQLite 环境；
- 已索引文件数量和数据库占用；
- 活动 / 暂停目录数量；
- 索引问题按错误代码的数量；
- 最近完整校准时间；
- 排除规则数量、文件大小上限等非内容配置。

明确不包含：

- 索引目录路径；
- 文件路径或文件名；
- 文档正文；
- 索引问题详情；
- 搜索历史或收藏查询。

测试会专门向数据库写入 `SECRET_*` 路径、文件名和问题详情，再断言它们不能出现在诊断 JSON 中。

## 安全边界

“完全离线”不等于“索引没有敏感性”。`docseek.db` 保存提取后的正文 Chunk，因此需要按本地敏感数据文件处理。

当前原则：

- 只索引用户明确选择且允许检索的目录；
- 支持排除目录和排除文件规则；
- 数据库存放在当前用户目录；
- 不以管理员权限绕过原文件 ACL；
- 删除 / 卸载动作优先保守，避免误删用户数据。

正式内网推广前仍需进一步评估：Windows ACL、索引加密或安全索引模式、备份/清理策略、代码签名和企业软件分发。

## Release 流程

仓库已经建立 guarded Windows release workflow：

1. `docseek.__version__` 是版本唯一来源；
2. 发布 tag 必须严格等于 `v<version>`；
3. 构建 Portable ZIP 和 Setup.exe；
4. 安装 Setup → frozen smoke → 卸载 → 验证用户数据保留；
5. 解压最终 Portable ZIP → frozen smoke；
6. 生成 `SHA256SUMS.txt`；
7. PR / 手动 dry-run 只上传临时候选 artifact；
8. 只有真正的 `v*` tag push 才创建或更新 GitHub Release。

当前仍处于 Beta 阶段，因此仓库可以具备正式发布能力，但不意味着已经达到 GA / 大范围推广标准。

## Beta 验证

真实 Windows 办公机验证方法见：

- [`docs/BETA_TESTING.md`](docs/BETA_TESTING.md)
- [`ROADMAP.md`](ROADMAP.md)

重点验证：

- 真实 10k～50k Office 文件；
- 首次索引与大 Excel/PDF 长尾；
- 典型真实查询 Top5；
- 增删改移后的 watcher 最终一致性；
- 睡眠 / 唤醒；
- 4～8 小时及 1～3 天连续运行；
- 网络盘 / 同步盘（如实际需要）；
- 异常、锁定、损坏和旧格式文件；
- CPU / 内存 / WAL 长时间状态。

## 开发运行

建议 Python 3.11 或 3.12：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[calamine,tika]"
docseek
```

安装打包依赖后可构建 Windows 交付物：

```powershell
pip install -e ".[calamine,tika,wps,package]"
.\packaging\windows\build_portable.ps1
.\packaging\windows\build_installer.ps1
```

## 技术栈

- Python 3.11+
- PySide6
- SQLite FTS5
- watchdog
- PyMuPDF
- python-docx
- python-pptx
- openpyxl
- python-calamine（可选）
- iscc-tika（可选）
- pywin32 / WPS COM（可选 fallback）
- PyInstaller + Inno Setup（Windows 发布）

## 项目定位

DocSeek 当前优先解决：

> 文件很多、文件名记不住，但记得正文中的关键词，希望像 Everything 一样快速找到 Word、Excel、PowerPoint、PDF 等办公资料。

下一阶段的成功标准不是“功能更多”，而是：**真实办公人员愿意每天开着用，而且在长时间运行、文件持续变化和实际敏感数据边界下仍然稳定、可解释、可维护。**
