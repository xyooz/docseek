from __future__ import annotations

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

    @staticmethod
    def _should_isolate(path: Path) -> bool:
        return (
            path.suffix.lower() not in DIRECT_SUPPORTED_EXTENSIONS
            and os.environ.get("DOCSEEK_LEGACY_WORKER") != "1"
        )

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
    ) -> Iterator[DocumentChunk]:
        """Return chunks through the direct lane or isolated compatibility lane."""
        path = Path(path)
        if self._should_isolate(path):
            from .legacy_isolation import iter_legacy_chunks_isolated

            yield from iter_legacy_chunks_isolated(path)
            return

        adapter = self.registry.adapter_for(path)
        yield from adapter.iter_chunks(
            path,
            target_chars=target_chars,
            spreadsheet_rows_per_chunk=spreadsheet_rows_per_chunk,
            on_progress=on_progress,
        )


DEFAULT_EXTRACTION_BROKER = ContentExtractionBroker()
