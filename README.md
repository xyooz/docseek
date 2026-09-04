# DocSeek

DocSeek 是一个面向 Windows 办公环境的本地全文检索工具。目标不是做一个“能搜正文的 Demo”，而是把 **Everything 的即时、简洁、低资源体验** 与 **Recoll/DocFetcher 一类全文检索工具的内容索引、相关性排序和预览能力**结合起来，并针对中文 Office 文档和内网办公本进行优化。

## 核心原则

- **完全本地**：不上传文件内容，不依赖云服务。
- **搜索与解析解耦**：文件内容提前建立索引，搜索时不遍历磁盘。
- **增量更新**：未修改文件不重复解析，降低 CPU、内存与磁盘压力。
- **界面始终可响应**：耗时索引在后台线程执行，可主动停止。
- **普通用户优先**：默认直接输入关键词即可，不要求学习复杂检索语法。
- **可解释结果**：显示文件名、路径、大小、修改时间和命中上下文，并按相关性排序。

## 当前能力

- [x] PySide6 Windows 桌面界面
- [x] 支持多个索引目录
- [x] TXT / Markdown / Log / CSV / DOCX / XLSX / PPTX / PDF 内容解析
- [x] SQLite + FTS5 全文倒排索引
- [x] BM25 相关性排序（文件名权重更高）
- [x] 中文 trigram 子串索引增强
- [x] 常规 unicode61 索引作为兼容与回退路径
- [x] 180ms debounce 输入即搜
- [x] 后台索引，避免阻塞 UI
- [x] 增量索引：修改才重建，删除自动清理
- [x] watchdog 文件系统实时监听 + 事件防抖
- [x] 超大文件默认保护（200 MB 上限）
- [x] 自动跳过 .git / node_modules / 临时 Office 文件等高噪声内容
- [x] 文件类型筛选
- [x] 搜索结果元数据：类型、大小、修改时间、完整路径
- [x] 命中上下文预览和关键词高亮
- [x] 双击打开文件
- [x] 打开文件所在位置
- [x] 复制完整路径
- [x] 索引状态、手动刷新与停止索引

## 架构

```text
一个或多个工作目录
        ↓
Watchdog 文件变化监听
        ↓（事件合并 / debounce）
DirectoryIndexer
    ├─ 文件扫描
    ├─ mtime + size 增量判断
    ├─ 临时/噪声目录过滤
    ├─ 大文件保护
    └─ 删除检测
        ↓
Extractors
    ├─ 文本
    ├─ Word
    ├─ Excel（read_only 流式读取）
    ├─ PowerPoint
    └─ PDF（逐页提取并保留页码标记）
        ↓
SQLite
    ├─ files：文件元数据
    ├─ settings：索引配置
    ├─ FTS5 unicode61：常规全文索引
    └─ FTS5 trigram：中文连续文本 / 子串增强
        ↓
BM25 Search
        ↓
类型过滤 + 结果列表 + 命中预览 + Windows 文件操作
```

## 中文检索策略

SQLite FTS5 默认 `unicode61` 对中文连续文本并不理想。DocSeek 目前采用双索引策略：

1. `unicode61` 保留常规全文索引、兼容性与 BM25 检索能力；
2. SQLite 支持时，同时维护 `trigram` 索引；
3. 对包含中文且长度足够的查询优先走 trigram，从而支持中文连续子串匹配；
4. 环境中的 SQLite 不支持 trigram 时自动回退，不影响基本使用。

后续仍会针对 1～2 个汉字的短查询、中文分词、查询纠错和索引体积做基准测试，而不是直接堆入较重的 NLP 依赖。

## 性能策略

DocSeek 不会在用户输入关键词后逐个打开文件。文件读取发生在索引阶段，查询只访问本地 FTS5 索引。

目前采用：

- SQLite WAL，降低索引写入与搜索读取互相阻塞的概率；
- 32 MB SQLite page cache；
- 文件 `modified_time + size` 判断是否变化；
- 文件变化事件先合并，再触发增量扫描；
- Excel 使用 `read_only=True`；
- PDF 逐页提取正文；
- 单文件默认 200 MB 保护阈值；
- 扫描时剪枝 `.git`、`node_modules` 等无关目录；
- 跳过 `~$` Office 临时文件；
- 搜索输入 180ms 防抖；
- 单次最多渲染 150 条结果，避免 GUI 一次塞入数万项。

## 下一阶段

### P1：把长期使用体验做完整

- [ ] 索引目录管理界面（查看、删除、暂停单个目录）
- [ ] 用户自定义排除目录 / 排除文件规则
- [ ] 文件大小与修改时间筛选
- [ ] 索引失败记录与可视化
- [ ] 大文件更细粒度的切块索引
- [ ] 搜索结果分页 / 虚拟化列表
- [ ] 搜索历史与常用筛选保存

### P2：中文检索继续专项优化

- [ ] 1～2 汉字短查询优化
- [ ] trigram 与本地中文分词方案基准测试
- [ ] 精确短语 / 前缀 / 模糊搜索统一语法
- [ ] 中文错别字与近似搜索
- [ ] 搜索建议

所有方案都以 **离线、低资源、可打包部署** 为约束。

### P3：可选智能检索

后续可加入本地 Hybrid Search：

```text
FTS5 / BM25 关键词召回
          +
本地 Embedding 语义召回
          ↓
       重排序
```

语义检索只作为可选增强，传统全文检索始终保留，以保证内网环境中的速度、稳定性和可解释性。

## 开发运行

建议 Python 3.11 或 3.12：

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e .
docseek
```

索引数据库默认存放在：

```text
%USERPROFILE%\.docseek\docseek.db
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

## 项目定位

DocSeek 现阶段优先解决：

> 文件很多、文件名记不住，但记得正文中的关键词，希望像 Everything 一样快速找到 Word、Excel、PowerPoint、PDF 等办公资料。

在这个目标稳定实现之后，再逐步增加更高级的检索能力。
