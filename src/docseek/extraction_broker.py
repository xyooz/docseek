from __future__ import annotations

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
from .document_types import DocumentFamily, document_family_for_extension


@dataclass(slots=True, frozen=True)
class ExtractionDecision:
    extension: str
    family: DocumentFamily
    adapter_name: str


class ContentExtractionBroker:
    """Choose a mature local extractor and expose one streaming DocIR contract.

    The broker does not own file-format parsing. It routes a document to the
    best available adapter for the current machine, then lifts the adapter's
    location-aware chunk stream into DocIR. This keeps format compatibility
    replaceable while the indexing/retrieval layer stays stable.
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

    def iter_blocks(
        self,
        path: Path,
        *,
        on_progress: ChunkProgressCallback | None = None,
    ) -> Iterator[DocumentBlock]:
        decision = self.decision_for(path)
        adapter = self.registry.adapter_for(path)
        for chunk in adapter.iter_chunks(path, on_progress=on_progress):
            yield block_from_chunk(path, decision.family, chunk)

    def iter_chunks(
        self,
        path: Path,
        *,
        on_progress: ChunkProgressCallback | None = None,
    ) -> Iterator[DocumentChunk]:
        """Compatibility stream for the existing v7 search/index schema."""
        for block in self.iter_blocks(path, on_progress=on_progress):
            yield block.as_chunk()


DEFAULT_EXTRACTION_BROKER = ContentExtractionBroker()
