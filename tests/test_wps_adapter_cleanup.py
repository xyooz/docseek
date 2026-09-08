from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docseek.wps_adapter import _cleanup_conversion_directory


class WpsConversionCleanupTests(unittest.TestCase):
    def test_cleanup_retries_a_transient_windows_sharing_violation(self) -> None:
        directory = Path(tempfile.mkdtemp(prefix="docseek-wps-test-"))
        with (
            patch(
                "docseek.wps_adapter.shutil.rmtree",
                side_effect=[PermissionError("still locked"), None],
            ) as remove,
            patch("docseek.wps_adapter.time.sleep") as sleep,
        ):
            _cleanup_conversion_directory(directory, timeout=1.0)

        self.assertEqual(remove.call_count, 2)
        sleep.assert_called_once_with(0.1)

    def test_cleanup_does_not_fail_extraction_when_lock_outlives_timeout(self) -> None:
        directory = Path(tempfile.mkdtemp(prefix="docseek-wps-test-"))
        with patch(
            "docseek.wps_adapter.shutil.rmtree",
            side_effect=[PermissionError("still locked"), None],
        ) as remove:
            _cleanup_conversion_directory(directory, timeout=0)

        self.assertEqual(remove.call_count, 2)
        self.assertEqual(remove.call_args_list[-1].kwargs, {"ignore_errors": True})


if __name__ == "__main__":
    unittest.main()
