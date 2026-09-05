# Structure-aware ranking

This note defines the first schema-free retrieval use of DocIR location metadata.

## Goal

Keep ordinary BM25 + filename ranking unchanged, while allowing an explicit structure hint to prefer the matching chunk inside a document.

Initial hints:

- `page:12` — PDF page 12
- `slide:4` — presentation slide 4
- `sheet:客户` — spreadsheet sheet named `客户`

A hint is additive metadata, not indexed text. When normal content terms are also present, the hint is removed from the FTS query and contributes a small negative score (better rank) to a matching chunk. The database schema remains unchanged because current `chunks.location` labels already carry page/slide/sheet information.

## Compatibility rules

1. Queries without a structure hint must preserve current exact ranking.
2. `DocumentChunk` remains the persisted compatibility contract.
3. No schema migration is introduced for this phase.
4. Structure-only queries are not activated in the first step; they retain legacy behavior until a dedicated browse-by-structure path exists.
5. The initial implementation belongs in the exact grouped retrieval layer, not in individual extractors.

## Validation

Add regression tests covering:

- ordinary query parity against `ChunkStore.search_page()`;
- `page:N` choosing the requested PDF page when multiple chunks match;
- `slide:N` choosing the requested slide;
- `sheet:name` choosing the requested sheet row block;
- metadata filters and deep pagination remaining unchanged.
