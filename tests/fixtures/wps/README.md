# WPS native-format fixtures

These fixtures are small synthetic/non-sensitive WPS Office documents supplied for DocSeek compatibility testing. The GitHub connector used to add them can only write UTF-8 text, so the exact original binary bytes are stored as deterministic gzip + Base64 payloads and materialized by the shared `tools/wps_fixtures.py` helper before parsing or manual-corpus generation.

`manifest.json` is the source of truth for the trusted corpus. Every payload is rejected unless its reconstructed byte size, SHA-256, and OLE/CFB compound-document magic (`D0 CF 11 E0 A1 B1 1A E1`) match the manifest. The samples are **not** ZIP/OOXML packages.

| Fixture | Original size | SHA-256 | Expected searchable text | Observed container evidence |
| --- | ---: | --- | --- | --- |
| `sample_writer.wps` | 10,240 B | `882b16b7a5e97a06e37b100457eb3141d9fb6f8ef751a88da52a99144ab17bd1` | `测试测试`, `郑xingyu` | OLE/CFB; Word-compatible `WordDocument`/table streams |
| `sample_sheet.et` | 19,968 B | `d9fb29635a52a02776a270d4f4407201ecf7c70f646f9f2c0a6969dbf071dcea` | `测试测试`, `郑xingyu` | OLE/CFB; Excel-compatible `Workbook`; `Sheet1` after conversion |
| `sample_slides.dps` | 50,176 B | `5ccaa90fd0b472f8c8e2474cc9ea89ce1b6da10d7571b8338ffa9940ce4fbd35` | `测试测试`, `郑xingyu` | OLE/CFB; PowerPoint-compatible document streams; text on slide 1 after conversion |

The DPS payload is split into numbered `.partNN` files only to keep repository API writes small; the test concatenates the parts before Base64 decoding.

These fixtures prove compatibility only for the represented WPS variants. Template formats (`.wpt/.ett/.dpt`) and other WPS generations remain separate validation targets until representative samples are available.

## 未纳入正式测试范围的格式

仓库已移除一组只有约 190 B、与记录尺寸不一致的 `DocSeek-*-native.*` 文件，避免把损坏的文本传输产物误判为解析器回归。`.wpt`、`.ett`、`.dpt`、`.etx` 和 `.ettx` 目前没有可信的独立二进制样例，只有拿到真实脱敏文件并加入 `manifest.json` 后才会进入正式 fixture 集合。

WPS 官方公开格式列表中列出了 `.wps/.wpt`、`.et/.ett`、`.dps/.dpt`，但没有列出 `.etx` 或 `.ettx`；当前扩展名路由测试会用可信 `sample_sheet.et` 验证这些别名不会被路由拦截，但这不等同于它们已有独立格式样例。
