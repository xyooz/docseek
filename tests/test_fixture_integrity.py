from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools.wps_fixtures import WPS_ROOT, fixture_bytes, load_manifest


class FixtureIntegrityTests(unittest.TestCase):
    def test_trusted_wps_manifest_reconstructs_only_valid_ole_fixtures(self) -> None:
        manifest = load_manifest()
        self.assertEqual(
            set(manifest),
            {"sample_writer.wps", "sample_sheet.et", "sample_slides.dps"},
        )

        for name, spec in manifest.items():
            with self.subTest(name=name):
                data = fixture_bytes(name, manifest=manifest)
                self.assertEqual(len(data), int(spec["size"]))
                self.assertTrue(data.startswith(bytes.fromhex(str(spec["magic"]))))
                self.assertEqual(spec["backend"], "tika-native")
                self.assertTrue(spec["expected_text"])

    def test_unverified_native_binary_files_are_not_in_fixture_root(self) -> None:
        extensions = {".wps", ".wpt", ".et", ".ett", ".etx", ".ettx", ".dps", ".dpt"}
        direct_binary_fixtures = [
            path.name
            for path in WPS_ROOT.iterdir()
            if path.is_file() and path.suffix.lower() in extensions
        ]
        self.assertEqual(direct_binary_fixtures, [])


if __name__ == "__main__":
    unittest.main()
