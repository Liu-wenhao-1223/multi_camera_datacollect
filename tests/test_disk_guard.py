from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.camera_process import SegmentedBagRecorder
from app.disk_guard import (
    DiskAutoStopReached,
    DiskUsageCheckFailed,
    DiskUsageLimitExceeded,
    ensure_recording_disk_available,
    get_recording_disk_usage,
)


class FakeRecordDevice:
    created_paths: list[str] = []

    def __init__(self, _device, path: str):
        self.created_paths.append(path)

    def pause(self) -> None:
        pass


def make_recorder() -> SegmentedBagRecorder:
    return SegmentedBagRecorder(
        record_device_cls=FakeRecordDevice,
        pipeline=object(),
        device=object(),
        camera_index=0,
        stream_mode="RGB-D raw",
        depth_mode="2d",
    )


class DiskGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeRecordDevice.created_paths.clear()

    def test_uses_nearest_existing_parent_for_new_recording_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "missing" / "session" / "camera.bag"
            with patch("app.disk_guard.shutil.disk_usage", return_value=(1000, 500, 500)) as usage:
                status = get_recording_disk_usage(target)
            usage.assert_called_once_with(Path(temp_dir).resolve())
            self.assertEqual(status.percent_used, 50.0)

    def test_below_limit_is_allowed(self) -> None:
        with patch("app.disk_guard.shutil.disk_usage", return_value=(1000, 899, 101)):
            status = ensure_recording_disk_available(Path.cwd())
        self.assertAlmostEqual(status.percent_used, 89.9)

    def test_exactly_ninety_percent_is_soft_stop_but_start_allowed(self) -> None:
        with patch("app.disk_guard.shutil.disk_usage", return_value=(1000, 900, 100)):
            status = ensure_recording_disk_available(Path.cwd())
        self.assertTrue(status.auto_stop_reached)
        self.assertFalse(status.start_block_reached)

    def test_just_below_hard_limit_is_allowed(self) -> None:
        with patch("app.disk_guard.shutil.disk_usage", return_value=(1000, 979, 21)):
            status = ensure_recording_disk_available(Path.cwd())
        self.assertAlmostEqual(status.percent_used, 97.9)

    def test_exactly_ninety_eight_percent_is_blocked(self) -> None:
        with patch("app.disk_guard.shutil.disk_usage", return_value=(1000, 980, 20)):
            with self.assertRaises(DiskUsageLimitExceeded) as raised:
                ensure_recording_disk_available(Path.cwd())
        self.assertIn("98.0%", str(raised.exception))

    def test_invalid_disk_capacity_fails_safe(self) -> None:
        with patch("app.disk_guard.shutil.disk_usage", return_value=(0, 0, 0)):
            with self.assertRaises(DiskUsageCheckFailed):
                ensure_recording_disk_available(Path.cwd())

    def test_active_recorder_fails_safe_when_disk_check_fails(self) -> None:
        recorder = make_recorder()
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "app.camera_process._write_segment_metadata"
        ), patch("app.camera_process._release_record_device"):
            bag_path = Path(temp_dir) / "camera.bag"
            with patch("app.disk_guard.shutil.disk_usage", return_value=(1000, 500, 500)):
                recorder.start(bag_path)
            recorder._last_disk_check_at = None
            with patch("app.disk_guard.shutil.disk_usage", side_effect=OSError("unavailable")):
                with self.assertRaises(DiskUsageCheckFailed):
                    recorder.rotate_if_due()
            recorder.close()

    def test_recorder_does_not_start_at_limit(self) -> None:
        recorder = make_recorder()
        with tempfile.TemporaryDirectory() as temp_dir:
            bag_path = Path(temp_dir) / "camera.bag"
            with patch("app.disk_guard.shutil.disk_usage", return_value=(1000, 980, 20)):
                with self.assertRaises(DiskUsageLimitExceeded):
                    recorder.start(bag_path)
        self.assertEqual(FakeRecordDevice.created_paths, [])

    def test_active_recorder_detects_limit_on_periodic_check(self) -> None:
        recorder = make_recorder()
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "app.camera_process._write_segment_metadata"
        ), patch("app.camera_process._release_record_device"):
            bag_path = Path(temp_dir) / "camera.bag"
            with patch("app.disk_guard.shutil.disk_usage", return_value=(1000, 899, 101)):
                recorder.start(bag_path)
            recorder._last_disk_check_at = None
            with patch("app.disk_guard.shutil.disk_usage", return_value=(1000, 901, 99)):
                with self.assertRaises(DiskAutoStopReached):
                    recorder.rotate_if_due()
            recorder.close()
        self.assertEqual(FakeRecordDevice.created_paths, [str(bag_path)])

    def test_recorder_started_in_buffer_continues_until_hard_limit(self) -> None:
        recorder = make_recorder()
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "app.camera_process._write_segment_metadata"
        ), patch("app.camera_process._release_record_device"):
            bag_path = Path(temp_dir) / "camera.bag"
            with patch("app.disk_guard.shutil.disk_usage", return_value=(1000, 920, 80)):
                recorder.start(bag_path)
            recorder._last_disk_check_at = None
            with patch("app.disk_guard.shutil.disk_usage", return_value=(1000, 950, 50)):
                self.assertIsNone(recorder.rotate_if_due())
            recorder._last_disk_check_at = None
            with patch("app.disk_guard.shutil.disk_usage", return_value=(1000, 980, 20)):
                with self.assertRaises(DiskUsageLimitExceeded):
                    recorder.rotate_if_due()
            recorder.close()

    def test_each_multi_camera_recorder_uses_the_same_guard(self) -> None:
        recorders = [make_recorder(), make_recorder()]
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "app.camera_process._write_segment_metadata"
        ), patch("app.camera_process._release_record_device"):
            with patch("app.disk_guard.shutil.disk_usage", return_value=(1000, 899, 101)):
                for index, recorder in enumerate(recorders):
                    recorder.start(Path(temp_dir) / f"camera_{index + 1}.bag")
            with patch("app.disk_guard.shutil.disk_usage", return_value=(1000, 901, 99)):
                for recorder in recorders:
                    recorder._last_disk_check_at = None
                    with self.assertRaises(DiskAutoStopReached):
                        recorder.rotate_if_due()
            for recorder in recorders:
                recorder.close()


if __name__ == "__main__":
    unittest.main()
