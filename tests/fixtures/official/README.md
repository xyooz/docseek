# 官方办公格式回归样例

这些文件来自 Apache Tika 的公开测试文档目录：

https://svn.apache.org/repos/asf/tika/branches/0.7/tika-parsers/src/test/resources/test-documents/

它们只用于 DocSeek 的解析、格式识别和索引回归测试，不代表真实业务文档。保留原始文件名，便于与 Tika 的测试预期对照。

| 文件 | 覆盖格式 |
| --- | --- |
| testWORD.doc / testWORD.docx | Word 97-2003 / OOXML |
| testEXCEL.xls / testEXCEL.xlsx | Excel 97-2003 / OOXML |
| testPPT.ppt / testPPT.pptx | PowerPoint 97-2003 / OOXML |
| testPDF.pdf | PDF |
| testOpenOffice2.odt | OpenDocument Text |
| testRTF.rtf | RTF |
| testXML.xml | XML |

Apache Tika 的 WPS/ET/DPS 样例不在这个公共目录中；对应的 WPS 样例继续放在 tests/fixtures/wps，其中包含 .wps、.et 和 .dps 回归文件。真实 .etx、.ettx、.ett 文件仍建议在 Windows/WPS 环境补充一组脱敏样例。