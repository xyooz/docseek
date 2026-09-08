# WPS native-format fixtures

These fixtures are small synthetic/non-sensitive WPS Office documents supplied for DocSeek compatibility testing. The GitHub connector used to add them can only write UTF-8 text, so the exact original binary bytes are stored as deterministic gzip + Base64 payloads and materialized by `test_wps_native_formats.py` before parsing.

The tests verify the reconstructed byte size and SHA-256 before they are used. All three samples begin with the OLE/CFB compound-document magic `D0 CF 11 E0 A1 B1 1A E1`; they are **not** ZIP/OOXML packages.

| Fixture | Original size | SHA-256 | Expected searchable text | Observed container evidence |
| --- | ---: | --- | --- | --- |
| `sample_writer.wps` | 10,240 B | `882b16b7a5e97a06e37b100457eb3141d9fb6f8ef751a88da52a99144ab17bd1` | `测试测试`, `郑xingyu` | OLE/CFB; Word-compatible `WordDocument`/table streams |
| `sample_sheet.et` | 19,968 B | `d9fb29635a52a02776a270d4f4407201ecf7c70f646f9f2c0a6969dbf071dcea` | `测试测试`, `郑xingyu` | OLE/CFB; Excel-compatible `Workbook`; `Sheet1` after conversion |
| `sample_slides.dps` | 50,176 B | `5ccaa90fd0b472f8c8e2474cc9ea89ce1b6da10d7571b8338ffa9940ce4fbd35` | `测试测试`, `郑xingyu` | OLE/CFB; PowerPoint-compatible document streams; text on slide 1 after conversion |

The DPS payload is split into numbered `.partNN` files only to keep repository API writes small; the test concatenates the parts before Base64 decoding.

These fixtures prove compatibility only for the represented WPS variants. Template formats (`.wpt/.ett/.dpt`) and other WPS generations remain separate validation targets until representative samples are available.

## WPS Office 生成的 OOXML 扩展名样例

下面这组脱敏文件由 WPS Office 在本地新建并保存，用于覆盖较新的 WPS 兼容变体。WPS 将 OOXML 包保存为对应的 WPS 扩展名；`file` 和 ZIP 目录检查均显示为 OOXML 容器，正好用于验证“扩展名 + 内容识别”两条路径。

| Fixture | Size | SHA-256 | Expected searchable text |
| --- | ---: | --- | --- |
| `DocSeek-WPS-native.wps` | 10,357 B | `8575ad8a3f86c402fa271cf72f823472b99c202ebb325480b0fb457e8ba9067f` | `DocSeek WPS native fixture`, `WPS writer regression sample` |
| `DocSeek-WPT-native.wpt` | 10,367 B | `6e6956c89f56c0a578361806972bd3d697db9de3ae15fa498542aea2c3b718e4` | same writer payload; template extension |
| `DocSeek-ET-native.et` | 9,460 B | `c64996b4e8e31edb4155ad61ebcf36d64da7d912f16b8a8fcbd3173a7f62f140` | `DocSeek ET native fixture`, `WPS table sample`, `123` |
| `DocSeek-ETT-native.ett` | 9,462 B | `290f5313474fadea252093adb60885fc13113a0700252a6dc7331f1298e22263` | same sheet payload; template extension |
| `DocSeek-DPS-native.dps` | 37,181 B | `0b2e817b2e22c91e1c81e262fdf803b816759bccde68b061df22a6c322a5f537` | `DocSeek DPS native fixture`, `WPS presentation regression sample` |
| `DocSeek-DPT-native.dpt` | 37,181 B | `2b032a165c4883c5b02009a86c754871c4568d05fde70a136f8cc45c42935518` | same presentation payload; template extension |

WPS 官方公开格式列表中列出了 `.wps/.wpt`、`.et/.ett`、`.dps/.dpt`，但没有列出 `.etx` 或 `.ettx`；因此这里没有把 `.et` 简单改名后冒充真实样例。若后续拿到真实脱敏文件，再单独补充。
