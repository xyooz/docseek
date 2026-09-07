from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import docseek.bootstrap as bootstrap


class SingleInstanceTests(unittest.TestCase):
    def test_second_instance_cannot_acquire_same_user_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            original_app_dir = bootstrap.APP_DIR
            original_lock_path = bootstrap.INSTANCE_LOCK_PATH
            try:
                bootstrap.APP_DIR = Path(temp_dir)
                bootstrap.INSTANCE_LOCK_PATH = Path(temp_dir) / "docseek.lock"

                first = bootstrap._acquire_instance_lock()
                self.assertIsNotNone(first)
                try:
                    second = bootstrap._acquire_instance_lock()
                    self.assertIsNone(second)
                finally:
                    assert first is not None
                    first.unlock()

                third = bootstrap._acquire_instance_lock()
                self.assertIsNotNone(third)
                if third is not None:
                    third.unlock()
            finally:
                bootstrap.APP_DIR = original_app_dir
                bootstrap.INSTANCE_LOCK_PATH = original_lock_path


if __name__ == "__main__":
    unittest.main()
