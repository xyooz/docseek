from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures"
OFFICIAL_ROOT = FIXTURE_ROOT / "official"
WPS_ROOT = FIXTURE_ROOT / "wps"
DEFAULT_OUTPUT = REPO_ROOT / "manual-test-corpus" / "generated"
SENTINEL_NAME = ".docseek-manual-corpus"
SENTINEL_CONTENT = "docseek-manual-corpus-v1\n"

WPS_OLE_FIXTURES: dict[str, dict[str, Any]] = {
    "sample_writer.wps": {
        "payload": "sample_writer.wps.gz.b64",
        "size": 10_240,
        "sha256": "882b16b7a5e97a06e37b100457eb3141d9fb6f8ef751a88da52a99144ab17bd1",
    },
    "sample_sheet.et": {
        "payload_parts": "sample_sheet.et.gz.b64.part*",
        "size": 19_968,
        "sha256": "d9fb29635a52a02776a270d4f4407201ecf7c70f646f9f2c0a6969dbf071dcea",
    },
    "sample_slides.dps": {
        "payload_parts": "sample_slides.dps.gz.b64.part*",
        "size": 50_176,
        "sha256": "5ccaa90fd0b472f8c8e2474cc9ea89ce1b6da10d7571b8338ffa9940ce4fbd35",
    },
}

WPS_NATIVE_FILES = (
    "DocSeek-WPS-native.wps",
    "DocSeek-WPT-native.wpt",
    "DocSeek-ET-native.et",
    "DocSeek-ETT-native.ett",
    "DocSeek-DPS-native.dps",
    "DocSeek-DPT-native.dpt",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_fixture(source: Path, target: Path) -> None:
    if not source.exists():
        raise FileNotFoundError(f"missing fixture: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def reconstruct_wps_fixture(name: str, target: Path) -> None:
    spec = WPS_OLE_FIXTURES[name]
    if "payload" in spec:
        encoded = (WPS_ROOT / str(spec["payload"])).read_text(encoding="ascii")
    else:
        parts = sorted(WPS_ROOT.glob(str(spec["payload_parts"])))
        if not parts:
            raise FileNotFoundError(f"missing payload parts for {name}")
        encoded = "".join(part.read_text(encoding="ascii") for part in parts)

    data = gzip.decompress(base64.b64decode(encoded))
    if len(data) != int(spec["size"]):
        raise RuntimeError(f"{name}: expected {spec['size']} bytes, got {len(data)}")
    digest = hashlib.sha256(data).hexdigest()
    if digest != str(spec["sha256"]):
        raise RuntimeError(f"{name}: sha256 mismatch: {digest}")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def generate_large_text(target: Path, size_mb: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    line = (
        "DOCSEEK_LARGE_TEXT_SENTINEL "
        "客户经理 风险管理 跨境业务 全文检索 performance regression sample\n"
    ).encode("utf-8")
    remaining = max(1, size_mb) * 1024 * 1024
    with target.open("wb") as handle:
        while remaining > 0:
            chunk = line[:remaining]
            handle.write(chunk)
            remaining -= len(chunk)


def generate_large_xlsx(target: Path, rows: int) -> bool:
    try:
        from openpyxl import Workbook
    except ImportError:
        return False

    target.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("DocSeekLarge")
    sheet.append(["row", "keyword", "description"])
    for row_no in range(1, max(1, rows) + 1):
        sheet.append(
            [
                row_no,
                f"DOCSEEK_LARGE_XLSX_{row_no}",
                f"客户经理测试数据 第 {row_no} 行",
            ]
        )
    workbook.save(target)
    workbook.close()
    return True


def write_broken_files(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "zero-byte.xlsx").write_bytes(b"")
    (root / "truncated.xlsx").write_bytes(
        b"PK\x03\x04DOCSEEK_BROKEN_XLSX_SENTINEL"
    )
    (root / "fake.docx").write_text(
        "DOCSEEK_BROKEN_DOCX_SENTINEL this is not an OOXML package",
        encoding="utf-8",
    )
    (root / "fake.xls").write_bytes(
        bytes.fromhex("D0CF11E0A1B11AE1") + b"DOCSEEK_BROKEN_XLS_SENTINEL"
    )
    (root / "fake.pdf").write_bytes(
        b"%PDF-1.7\nDOCSEEK_BROKEN_PDF_SENTINEL\n%%EOF"
    )


def record_manifest(output: Path) -> None:
    items: list[dict[str, Any]] = []
    for path in sorted(output.rglob("*")):
        if not path.is_file() or path.name in {"manifest.json", SENTINEL_NAME}:
            continue
        items.append(
            {
                "path": path.relative_to(output).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    (output / "manifest.json").write_text(
        json.dumps(items, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_test_plan(output: Path, xlsx_generated: bool) -> None:
    xlsx_note = (
        "已生成 large-workbook.xlsx。"
        if xlsx_generated
        else "未安装 openpyxl，因此跳过 large-workbook.xlsx；安装项目依赖后重新生成即可。"
    )
    text = f"""# DocSeek manual acceptance corpus

本目录由 `manual-test-corpus/build.py` 自动生成，不应提交到 Git。

## 目录

- `01-normal/`：正常 Office/PDF/文本样例。
- `02-mixed/`：TXT + XLS/XLSX + WPS 混合目录，重点验证 SQLite writer / parser lease。
- `03-large/`：大文本与大 XLSX，用于停止、重试、进度与响应性测试。{xlsx_note}
- `04-broken/`：零字节、截断和伪造 Office/PDF 文件，用于异常恢复。
- `05-wps/`：真实/脱敏 WPS OLE 样例与 WPS Office 生成的扩展名样例。

## 建议验收

1. 将整个 `generated/` 加入 DocSeek 索引。
2. 搜索 `DOCSEEK_NORMAL_SENTINEL`、`DOCSEEK_MIXED_SENTINEL` 和 `DOCSEEK_LARGE_TEXT_SENTINEL`。
3. 在 `03-large/large-workbook.xlsx` 解析期间点击“停止”，确认任务尽快结束；再次刷新时应能重新处理。
4. 索引过程中打开“索引设置”，确认先停止索引再自动打开设置。
5. 强制关闭 DocSeek 后重新启动，确认不会被同一异常文件卡入启动循环。
6. 多次刷新 `02-mixed/`，确认没有 `database is locked`。
7. 检查 `04-broken/`：失败应被记录/隔离，但其它文件仍能继续索引和搜索。
8. Windows 上验证 `05-wps/`；具体可解析范围取决于 Tika/WPS 本地适配器是否可用。

生成参数会影响大文件大小；`manifest.json` 记录本次所有文件的 SHA-256。
"""
    (output / "TEST_PLAN.md").write_text(text, encoding="utf-8")


def _safe_to_clean(output: Path) -> bool:
    default_output = DEFAULT_OUTPUT.resolve()
    if output == default_output:
        return True
    marker = output / SENTINEL_NAME
    try:
        return marker.read_text(encoding="utf-8") == SENTINEL_CONTENT
    except OSError:
        return False


def build(output: Path, *, clean: bool, large_mb: int, xlsx_rows: int) -> None:
    output = output.resolve()
    if clean and output.exists():
        if not _safe_to_clean(output):
            raise RuntimeError(
                "refusing to clean an arbitrary directory; generate it once without "
                "--clean or use the default manual-test-corpus/generated path"
            )
        shutil.rmtree(output)

    output.mkdir(parents=True, exist_ok=True)
    (output / SENTINEL_NAME).write_text(SENTINEL_CONTENT, encoding="utf-8")

    for directory in (
        "01-normal",
        "02-mixed",
        "03-large",
        "04-broken",
        "05-wps",
    ):
        (output / directory).mkdir(parents=True, exist_ok=True)

    normal_files = (
        "testWORD.doc",
        "testWORD.docx",
        "testEXCEL.xls",
        "testEXCEL.xlsx",
        "testPPT.ppt",
        "testPPT.pptx",
        "testPDF.pdf",
        "testRTF.rtf",
        "testOpenOffice2.odt",
    )
    for name in normal_files:
        copy_fixture(OFFICIAL_ROOT / name, output / "01-normal" / name)
    (output / "01-normal" / "normal.txt").write_text(
        "DOCSEEK_NORMAL_SENTINEL 客户经理 本地全文检索\n",
        encoding="utf-8",
    )

    (output / "02-mixed" / "a.txt").write_text(
        "DOCSEEK_MIXED_SENTINEL 文本 A\n", encoding="utf-8"
    )
    (output / "02-mixed" / "b.txt").write_text(
        "DOCSEEK_MIXED_SENTINEL 文本 B\n", encoding="utf-8"
    )
    copy_fixture(OFFICIAL_ROOT / "testEXCEL.xls", output / "02-mixed" / "legacy.xls")
    copy_fixture(
        OFFICIAL_ROOT / "testEXCEL.xlsx",
        output / "02-mixed" / "modern.xlsx",
    )
    reconstruct_wps_fixture("sample_sheet.et", output / "02-mixed" / "sample_sheet.et")

    generate_large_text(output / "03-large" / "large-text.txt", large_mb)
    xlsx_generated = generate_large_xlsx(
        output / "03-large" / "large-workbook.xlsx",
        xlsx_rows,
    )

    write_broken_files(output / "04-broken")

    for name in WPS_OLE_FIXTURES:
        reconstruct_wps_fixture(name, output / "05-wps" / name)
    for name in WPS_NATIVE_FILES:
        source = WPS_ROOT / name
        if source.exists():
            copy_fixture(source, output / "05-wps" / name)

    write_test_plan(output, xlsx_generated)
    record_manifest(output)

    files = [path for path in output.rglob("*") if path.is_file()]
    total_bytes = sum(path.stat().st_size for path in files)
    print(f"Manual corpus ready: {output}")
    print(f"Files: {len(files)}, bytes: {total_bytes:,}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a deterministic manual acceptance corpus for DocSeek."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"output directory (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="remove a previously generated corpus before rebuilding it",
    )
    parser.add_argument(
        "--large-mb",
        type=int,
        default=32,
        help="size of generated large text file in MiB (default: 32)",
    )
    parser.add_argument(
        "--xlsx-rows",
        type=int,
        default=50_000,
        help="rows in generated large XLSX (default: 50000)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    build(
        args.output,
        clean=args.clean,
        large_mb=max(1, args.large_mb),
        xlsx_rows=max(1, args.xlsx_rows),
    )
