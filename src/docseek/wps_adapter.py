from __future__ import annotations

import contextlib
import importlib.util
import platform
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterator

from .document_types import DocumentFamily, document_family_for_extension


class WpsAdapterUnavailable(RuntimeError):
    pass


class WpsConversionError(RuntimeError):
    pass


@dataclass(slots=True, frozen=True)
class WpsComponent:
    family: DocumentFamily
    prog_id: str
    target_extension: str
    save_format: int


# WPS exposes Office-compatible automation object models. The format constants
# below are documented by WPS: wdFormatXMLDocument=12,
# xlOpenXMLWorkbook=51 and ppSaveAsOpenXMLPresentation=24.
_COMPONENTS = {
    DocumentFamily.WRITER: WpsComponent(
        DocumentFamily.WRITER, "kwps.Application", ".docx", 12
    ),
    DocumentFamily.SPREADSHEET: WpsComponent(
        DocumentFamily.SPREADSHEET, "ket.Application", ".xlsx", 51
    ),
    DocumentFamily.PRESENTATION: WpsComponent(
        DocumentFamily.PRESENTATION, "kwpp.Application", ".pptx", 24
    ),
}


def pywin32_available() -> bool:
    # Looking up a dotted module can raise ModuleNotFoundError when the parent
    # package is absent, so probe win32com first. Optional WPS support must never
    # break normal DocSeek startup on machines without pywin32.
    return (
        importlib.util.find_spec("win32com") is not None
        and importlib.util.find_spec("pythoncom") is not None
    )


def wps_component_registered(component: WpsComponent) -> bool:
    if platform.system() != "Windows":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, component.prog_id):
            return True
    except (ImportError, FileNotFoundError, OSError):
        return False


@lru_cache(maxsize=1)
def available_wps_families() -> frozenset[DocumentFamily]:
    if not pywin32_available():
        return frozenset()
    return frozenset(
        family
        for family, component in _COMPONENTS.items()
        if wps_component_registered(component)
    )


def can_convert_extension(extension: str) -> bool:
    family = document_family_for_extension(extension)
    return family in available_wps_families()


def _component_for(path: Path) -> WpsComponent:
    family = document_family_for_extension(path.suffix)
    component = _COMPONENTS.get(family)
    if component is None:
        raise WpsAdapterUnavailable(f"WPS 本地适配器不支持该格式：{path.suffix}")
    if family not in available_wps_families():
        raise WpsAdapterUnavailable(
            f"未检测到可用的 WPS {family.value} 自动化组件；"
            "请确认已安装 WPS Office，并安装 DocSeek 的 wps 可选依赖"
        )
    return component


def _open_and_save(app, component: WpsComponent, source: Path, target: Path) -> None:
    document = None
    try:
        try:
            app.Visible = False
        except Exception:
            pass
        try:
            app.DisplayAlerts = 0
        except Exception:
            pass

        if component.family == DocumentFamily.WRITER:
            document = app.Documents.Open(str(source), ReadOnly=True)
            document.SaveAs2(str(target), component.save_format)
        elif component.family == DocumentFamily.SPREADSHEET:
            document = app.Workbooks.Open(str(source), ReadOnly=True)
            document.SaveAs(str(target), component.save_format)
        elif component.family == DocumentFamily.PRESENTATION:
            try:
                document = app.Presentations.Open(str(source), WithWindow=False)
            except Exception:
                document = app.Presentations.Open(str(source))
            document.SaveAs(str(target), component.save_format)
        else:  # pragma: no cover - component registry prevents this path.
            raise WpsAdapterUnavailable(f"Unsupported WPS family: {component.family}")
    finally:
        if document is not None:
            try:
                document.Close(False)
            except Exception:
                pass


def convert_with_wps(source: Path, target: Path) -> None:
    """Convert one legacy/WPS document locally using the installed WPS client."""
    component = _component_for(source)
    try:
        import pythoncom
        from win32com.client import DispatchEx
    except ImportError as exc:
        raise WpsAdapterUnavailable("pywin32 is required for WPS local automation") from exc

    app = None
    pythoncom.CoInitialize()
    try:
        app = DispatchEx(component.prog_id)
        _open_and_save(app, component, source.resolve(), target.resolve())
        if not target.exists() or target.stat().st_size == 0:
            raise WpsConversionError(f"WPS 未生成有效的转换文件：{target}")
    except WpsAdapterUnavailable:
        raise
    except Exception as exc:
        raise WpsConversionError(f"WPS 本地转换失败：{source.name}：{exc}") from exc
    finally:
        if app is not None:
            try:
                app.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()


@contextlib.contextmanager
def converted_openxml(source: Path) -> Iterator[Path]:
    """Yield a temporary OOXML copy without modifying the user's source file."""
    component = _component_for(source)
    with tempfile.TemporaryDirectory(prefix="docseek-wps-") as directory:
        target = Path(directory) / f"{source.stem}{component.target_extension}"
        convert_with_wps(source, target)
        yield target
