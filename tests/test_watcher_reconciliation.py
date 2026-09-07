from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from watchdog.events import FileMovedEvent

from docseek.watcher import WatchBatch, WatchManager, _DocSeekEventHandler


class WatcherReconciliationTests(unittest.TestCase):
    def test_office_style_move_queues_both_old_and_new_supported_paths(self) -> None:
        queued: list[Path] = []
        rescans: list[bool] = []
        handler = _DocSeekEventHandler(queued.append, lambda: rescans.append(True), [])

        handler.on_any_event(FileMovedEvent("old.docx", "renamed.docx"))

        self.assertEqual(queued, [Path("old.docx"), Path("renamed.docx")])
        self.assertEqual(rescans, [])

    def test_temp_to_supported_move_indexes_only_final_document(self) -> None:
        queued: list[Path] = []
        handler = _DocSeekEventHandler(queued.append, lambda: None, [])

        handler.on_any_event(FileMovedEvent("~$draft.tmp", "policy.docx"))

        self.assertEqual(queued, [Path("policy.docx")])

    def test_continuous_events_have_bounded_batch_delay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "guide.docx"
            manager = WatchManager(
                lambda _batch: None,
                debounce_seconds=1.2,
                max_batch_delay_seconds=10.0,
                reconcile_seconds=0,
                health_check_seconds=0,
            )

            timers: list[mock.MagicMock] = []

            def make_timer(delay, callback):
                timer = mock.MagicMock()
                timer.delay = delay
                timer.callback = callback
                timers.append(timer)
                return timer

            with mock.patch("docseek.watcher.threading.Timer", side_effect=make_timer), mock.patch(
                "docseek.watcher.time.monotonic", side_effect=[100.0, 111.0]
            ):
                manager._queue_path(target)
                manager._queue_path(target)

            self.assertEqual(len(timers), 2)
            self.assertAlmostEqual(timers[0].delay, 1.2)
            self.assertEqual(timers[1].delay, 0.0)
            timers[0].cancel.assert_called_once()

    def test_periodic_reconcile_emits_full_rescan_safety_net(self) -> None:
        batches: list[WatchBatch] = []
        manager = WatchManager(
            batches.append,
            debounce_seconds=0,
            max_batch_delay_seconds=0,
            reconcile_seconds=3600,
            health_check_seconds=0,
        )
        manager._observer = mock.MagicMock()
        manager._config = (("C:/docs",), ())

        timers: list[mock.MagicMock] = []

        def make_timer(delay, callback):
            timer = mock.MagicMock()
            timer.delay = delay
            timer.callback = callback
            timers.append(timer)
            return timer

        with mock.patch("docseek.watcher.threading.Timer", side_effect=make_timer):
            manager._periodic_reconcile()
            self.assertTrue(manager._full_rescan)
            self.assertGreaterEqual(len(timers), 2)
            debounce_timer = next(timer for timer in timers if timer.delay == 0)
            debounce_timer.callback()

        self.assertEqual(batches, [WatchBatch(full_rescan=True)])

    def test_health_check_restarts_dead_observer_and_reconciles(self) -> None:
        manager = WatchManager(
            lambda _batch: None,
            debounce_seconds=0,
            max_batch_delay_seconds=0,
            reconcile_seconds=0,
            health_check_seconds=60,
        )
        dead = mock.MagicMock()
        dead.is_alive.return_value = False
        manager._observer = dead
        manager._config = (("C:/docs",), ("C:/docs/private",))
        manager._last_health_check = 100.0

        with mock.patch.object(manager, "start") as start, mock.patch.object(
            manager, "_queue_rescan"
        ) as queue_rescan, mock.patch("docseek.watcher.time.monotonic", return_value=160.0):
            manager._health_check()

        start.assert_called_once_with(["C:/docs"], ["C:/docs/private"])
        queue_rescan.assert_called_once_with()

    def test_health_check_reconciles_after_long_resume_gap_without_restart(self) -> None:
        manager = WatchManager(
            lambda _batch: None,
            debounce_seconds=0,
            max_batch_delay_seconds=0,
            reconcile_seconds=0,
            health_check_seconds=60,
            resume_gap_seconds=120,
        )
        observer = mock.MagicMock()
        observer.is_alive.return_value = True
        manager._observer = observer
        manager._config = (("C:/docs",), ())
        manager._last_health_check = 100.0

        with mock.patch.object(manager, "_queue_rescan") as queue_rescan, mock.patch.object(
            manager, "_schedule_health_check_locked"
        ) as schedule_health, mock.patch(
            "docseek.watcher.time.monotonic", return_value=400.0
        ):
            manager._health_check()

        queue_rescan.assert_called_once_with()
        schedule_health.assert_called_once_with()
        self.assertEqual(manager._last_health_check, 400.0)

    def test_start_does_not_trust_dead_same_config_observer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = str(Path(temp_dir).resolve())
            manager = WatchManager(lambda _batch: None, reconcile_seconds=0, health_check_seconds=0)
            dead = mock.MagicMock()
            dead.is_alive.return_value = False
            manager._observer = dead
            manager._config = ((root,), ())

            replacement = mock.MagicMock()
            replacement.is_alive.return_value = True
            with mock.patch("docseek.watcher.Observer", return_value=replacement):
                manager.start([root])

            dead.stop.assert_called_once()
            dead.join.assert_called_once_with(timeout=2)
            replacement.schedule.assert_called_once()
            replacement.start.assert_called_once()
            self.assertIs(manager._observer, replacement)

    def test_stop_cancels_periodic_and_health_timers(self) -> None:
        manager = WatchManager(lambda _batch: None)
        observer = mock.MagicMock()
        reconcile_timer = mock.MagicMock()
        health_timer = mock.MagicMock()
        manager._observer = observer
        manager._config = (("C:/docs",), ())
        manager._reconcile_timer = reconcile_timer
        manager._health_timer = health_timer
        manager._last_health_check = 123.0

        manager.stop()

        observer.stop.assert_called_once()
        observer.join.assert_called_once_with(timeout=2)
        reconcile_timer.cancel.assert_called_once()
        health_timer.cancel.assert_called_once()
        self.assertIsNone(manager._reconcile_timer)
        self.assertIsNone(manager._health_timer)
        self.assertIsNone(manager._last_health_check)


if __name__ == "__main__":
    unittest.main()
