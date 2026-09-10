from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .chunks import ChunkProgressCallback, DocumentChunk
from .doc_ir import DocumentBlock, block_from_chunk
from .document_adapters import (
    DEFAULT_ADAPTER_REGISTRY,
    AdapterUnavailable,
    DocumentAdapter,
    DocumentAdapterRegistry,
)
from .document_types import DIRECT_SUPPORTED_EXTENSIONS, DocumentFamily, document_family_for_extension


logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class ExtractionDecision:
    extension: str
    family: DocumentFamily
    adapter_name: str


class ContentExtractionBroker:
    """Choose a mature local extractor and expose one streaming DocIR contract.

    Modern/direct formats stay in-process for low-overhead streaming. Legacy and
    compatibility formats are isolated in a killable subprocess when invoked by
    the desktop/indexer process, so one malformed XLS/DOC/PPT cannot hang the
    whole indexing pipeline. The worker process sets ``DOCSEEK_LEGACY_WORKER``
    and therefore executes the chosen adapter directly without recursion.
    """

    def __init__(self, registry: DocumentAdapterRegistry | None = None) -> None:
        self.registry = registry or DEFAULT_ADAPTER_REGISTRY

    @property
    def known_extensions(self) -> frozenset[str]:
        return self.registry.known_extensions

    def is_known_path(self, path: Path) -> bool:
        return self.registry.is_known_path(path)

    def decision_for(self, path: Path) -> ExtractionDecision:
        extension = path.suffix.lower()
        family = document_family_for_extension(extension)
        if family is None:
            raise AdapterUnavailable(f"DocSeek 尚未注册该文件格式：{extension}")
        adapter = self.registry.adapter_for(path)
        return ExtractionDecision(extension, family, adapter.name)

    def adapter_for(self, path: Path) -> DocumentAdapter:
        return self.registry.adapter_for(path)

    def _should_isolate(self, path: Path, *, force: bool = False) -> bool:
        if os.environ.get("DOCSEEK_LEGACY_WORKER") == "1":
            return False
        extension = path.suffix.lower()
        if extension not in DIRECT_SUPPORTED_EXTENSIONS:
            return True
        adapter = self.registry.adapter_for(path)
        # Office/PDF readers can spend an unbounded amount of time inside a
        # native or C-extension call. Keep these formats in the same killable
        # process lane as legacy compatibility formats so cancellation and
        # timeouts do not depend on cooperative checks inside the parser.
        if extension in {".docx", ".xlsx", ".pptx", ".pdf"}:
            return adapter.name in {"tika-native", "wps-local"} or (
                force
                and adapter.name in {"direct", "calamine-xlsx-fast"}
            )
        # A direct extension can still select a compatibility adapter after
        # container sniffing, notably an OLE presentation named *.pptx.
        return adapter.name in {"tika-native", "wps-local"}

    def iter_blocks(
        self,
        path: Path,
        *,
        target_chars: int = 12_000,
        spreadsheet_rows_per_chunk: int = 200,
        on_progress: ChunkProgressCallback | None = None,
    ) -> Iterator[DocumentBlock]:
        decision = self.decision_for(path)
        for chunk in self.iter_chunks(
            path,
            target_chars=target_chars,
            spreadsheet_rows_per_chunk=spreadsheet_rows_per_chunk,
            on_progress=on_progress,
        ):
            yield block_from_chunk(path, decision.family, chunk)

    def iter_chunks(
        self,
        path: Path,
        *,
        target_chars: int = 12_000,
        spreadsheet_rows_per_chunk: int = 200,
        on_progress: ChunkProgressCallback | None = None,
        cancelled=None,
        persistent_worker=None,
    ) -> Iterator[DocumentChunk]:
        """Return chunks through the direct lane or isolated compatibility lane."""
        path = Path(path)
        if self._should_isolate(path, force=cancelled is not None):
            from .legacy_isolation import iter_legacy_chunks_isolated

            if persistent_worker is not None:
                # Startup failure is the only automatic fallback boundary for
                # the persistent session. Once a worker is ready, parser
                # failures stay inside the adapter cascade and are not retried
                # through a second extraction path for the same request.
                from .persistent_extraction import PersistentWorkerError

                unavailable = bool(
                    getattr(persistent_worker, "unavailable_for_job", False)
                    or getattr(persistent_worker, "_unavailable_for_job", False)
                )
                if unavailable:
                    persistent_worker = None
                else:
                    try:
                        persistent_worker.start()
                    except PersistentWorkerError as exc:
                        disable_for_job = getattr(
                            persistent_worker, "disable_for_job", None
                        )
                        if callable(disable_for_job):
                            disable_for_job()
                        else:
                            # Keep simple test/embedder workers job-scoped too
                            # when they do not implement the controller API.
                            try:
                                setattr(persistent_worker, "unavailable_for_job", True)
                            except (AttributeError, TypeError):
                                pass
                        logger.warning(
                            "persistent extraction worker unavailable; "
                            "falling back to one-shot isolation for this job: %s",
                            exc,
                        )
                        persistent_worker = None

            isolation_kwargs = {}
            if cancelled is not None:
                isolation_kwargs["cancelled"] = cancelled
            if on_progress is not None:
                isolation_kwargs["on_progress"] = on_progress
            if persistent_worker is not None:
                isolation_kwargs["persistent_worker"] = persistent_worker
            yield from iter_legacy_chunks_isolated(path, **isolation_kwargs)
            return

        adapter = self.registry.adapter_for(path)
        yielded = False
        try:
            for chunk in adapter.iter_chunks(
                path,
                target_chars=target_chars,
                spreadsheet_rows_per_chunk=spreadsheet_rows_per_chunk,
                on_progress=on_progress,
            ):
                yielded = True
                yield chunk
        except Exception:
            # Real-world PPTX files sometimes use a valid ZIP container but a
            # package variant python-pptx cannot open. If opening failed before
            # producing any content, give the isolated Tika/WPS cascade a
            # chance. Never switch after yielding, which would duplicate slides.
            if (
                yielded
                or path.suffix.lower() != ".pptx"
                or adapter.name != "direct"
                or os.environ.get("DOCSEEK_LEGACY_WORKER") == "1"
            ):
                raise
            from .legacy_isolation import iter_legacy_chunks_isolated

            yield from iter_legacy_chunks_isolated(path)


DEFAULT_EXTRACTION_BROKER = ContentExtractionBroker()
