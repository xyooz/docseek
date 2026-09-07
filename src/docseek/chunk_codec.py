from __future__ import annotations

import zlib


_CODEC_MAGIC = b"DSZ1"


def encode_chunk_content(content: str) -> bytes:
    """Encode extracted chunk text for compact local storage.

    The FTS indexes still receive the original Unicode text. Only the raw copy
    kept for snippets, updates and contentless-FTS deletes is compressed.
    ``DSZ1`` makes the on-disk representation self-identifying so a future
    codec can coexist with this one.
    """
    return _CODEC_MAGIC + zlib.compress(content.encode("utf-8"), level=1)


def decode_chunk_content(value: object) -> str:
    """Read both legacy TEXT chunks and v7 compressed BLOB chunks."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value

    if isinstance(value, memoryview):
        raw = value.tobytes()
    elif isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    else:
        return str(value)

    if raw.startswith(_CODEC_MAGIC):
        try:
            return zlib.decompress(raw[len(_CODEC_MAGIC) :]).decode("utf-8")
        except (zlib.error, UnicodeDecodeError) as exc:
            raise ValueError("DocSeek 压缩内容块损坏，无法解压。") from exc

    # Defensive compatibility for an old/unversioned BLOB. DocSeek never
    # intentionally wrote one, but decoding UTF-8 is safer than presenting a
    # Python bytes repr or silently dropping the content.
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("DocSeek 内容块格式无法识别。") from exc


def is_compressed_chunk_content(value: object) -> bool:
    if isinstance(value, memoryview):
        raw = value.tobytes()
    elif isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    else:
        return False
    return raw.startswith(_CODEC_MAGIC)
