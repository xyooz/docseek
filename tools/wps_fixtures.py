from __future__ import annotations

import base64
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
WPS_ROOT = REPO_ROOT / "tests" / "fixtures" / "wps"
MANIFEST_PATH = WPS_ROOT / "manifest.json"


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, dict[str, Any]]:
    """Load the trusted WPS fixture manifest from the repository."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not value:
        raise ValueError(f"invalid WPS fixture manifest: {path}")
    return value


def _encoded_payload(name: str, spec: dict[str, Any], root: Path) -> str:
    if "payload" in spec:
        payload = root / str(spec["payload"])
        return payload.read_text(encoding="ascii")

    parts = sorted(root.glob(str(spec["payload_parts"])))
    if not parts:
        raise FileNotFoundError(f"missing payload parts for {name}")
    return "".join(part.read_text(encoding="ascii") for part in parts)


def fixture_bytes(
    name: str,
    *,
    manifest: dict[str, dict[str, Any]] | None = None,
    root: Path = WPS_ROOT,
) -> bytes:
    """Reconstruct and validate one trusted OLE/CFB WPS fixture.

    Size, SHA-256, and container magic are checked before the bytes are
    returned, so callers cannot accidentally use a truncated text transport
    artifact as a binary office fixture.
    """
    specs = load_manifest() if manifest is None else manifest
    try:
        spec = specs[name]
    except KeyError as exc:
        raise KeyError(f"fixture is not in the trusted manifest: {name}") from exc

    encoded = _encoded_payload(name, spec, root)
    data = gzip.decompress(base64.b64decode(encoded, validate=True))
    expected_size = int(spec["size"])
    if len(data) != expected_size:
        raise ValueError(f"{name}: expected {expected_size} bytes, got {len(data)}")

    digest = hashlib.sha256(data).hexdigest()
    expected_digest = str(spec["sha256"]).lower()
    if digest != expected_digest:
        raise ValueError(f"{name}: expected SHA-256 {expected_digest}, got {digest}")

    magic = bytes.fromhex(str(spec["magic"]))
    if not data.startswith(magic):
        raise ValueError(f"{name}: missing expected container magic {magic.hex()}")
    return data


def materialize_fixture(
    name: str,
    target: Path,
    *,
    manifest: dict[str, dict[str, Any]] | None = None,
    root: Path = WPS_ROOT,
) -> Path:
    """Validate a fixture and write it to an exact target path."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(fixture_bytes(name, manifest=manifest, root=root))
    return target
