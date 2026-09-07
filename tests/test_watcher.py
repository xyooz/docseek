from __future__ import annotations

import unittest
from pathlib import Path

from watchdog.events import DirCreatedEvent, FileModifiedEvent, FileMovedEvent

from docseek.watcher import _DocSeekEventHandler


class WatcherEventTests(unittest.TestCase):
    def test_supported_file_change_queues_only_that_path(self) -> None:
        paths: list[Path] = []
        rescans: list[bool] = []
        handler = _DocSeekEventHandler(paths.append, lambda: rescans.append(True), [])

        handler.on_any_event(FileModifiedEvent(r"C:\docs\credit.pdf"))
        handler.on_any_event(FileModifiedEvent(r"C:\docs\image.png"))

        self.assertEqual(paths, [Path(r"C:\docs\credit.pdf")])
        self.assertEqual(rescans, [])

    def test_move_into_excluded_directory_removes_old_supported_path_only(self) -> None:
        paths: list[Path] = []
        excluded = [Path(r"C:\docs\private").resolve()]
        handler = _DocSeekEventHandler(paths.append, lambda: None, excluded)

        handler.on_any_event(
            FileMovedEvent(
                r"C:\docs\credit.pdf",
                r"C:\docs\private\credit.pdf",
            )
        )

        self.assertEqual(paths, [Path(r"C:\docs\credit.pdf")])

    def test_move_out_of_excluded_directory_indexes_new_path_only(self) -> None:
        paths: list[Path] = []
        excluded = [Path(r"C:\docs\private").resolve()]
        handler = _DocSeekEventHandler(paths.append, lambda: None, excluded)

        handler.on_any_event(
            FileMovedEvent(
                r"C:\docs\private\credit.pdf",
                r"C:\docs\credit.pdf",
            )
        )

        self.assertEqual(paths, [Path(r"C:\docs\credit.pdf")])

    def test_directory_structure_change_requests_full_rescan(self) -> None:
        paths: list[Path] = []
        rescans: list[bool] = []
        handler = _DocSeekEventHandler(paths.append, lambda: rescans.append(True), [])

        handler.on_any_event(DirCreatedEvent(r"C:\docs\new-folder"))

        self.assertEqual(paths, [])
        self.assertEqual(rescans, [True])


if __name__ == "__main__":
    unittest.main()
