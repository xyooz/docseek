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
