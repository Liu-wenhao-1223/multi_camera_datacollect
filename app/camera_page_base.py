from __future__ import annotations

import threading
import multiprocessing as mp
import queue
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap, QResizeEvent
from PyQt6.QtWidgets import (
    QFileDialog,
    QFrame,
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton as QtPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    ComboBox,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    SubtitleLabel,
)

from .recording_settings import recording_output_dir, set_recording_output_dir
from .i18n import load_catalog, normalize_language
from .camera_process import run_orbbec_camera_process
from .multi_camera_sync_process import (
    DEFAULT_SYNC_CONFIG_PATH,
    reset_orbbec_devices_to_standalone,
    run_orbbec_multi_camera_sync_process,
)
from .ui_helpers import set_button_category
from .base import Page


MIN_DEPTH_MM = 20
MAX_DEPTH_MM = 5000
DEFAULT_CAMERA_RECORDING_NAME = "camera_glove_recording"


def _force_stop_process(process, terminate_timeout: float = 2.0) -> None:
    if process is None:
        return
    try:
        if not process.is_alive():
            return
    except Exception:
        return
    try:
        process.terminate()
        process.join(terminate_timeout)
    except Exception:
        pass
    try:
        if process.is_alive() and hasattr(process, "kill"):
            process.kill()
            process.join(terminate_timeout)
    except Exception:
        pass


def _shutdown_thread(worker: QThread | None, wait_ms: int = 5000, force_wait_ms: int = 6000) -> bool:
    if worker is None or not worker.isRunning():
        return True
    worker.requestInterruption()
    if worker.wait(wait_ms):
        return True
    force_stop = getattr(worker, "force_stop_process", None)
    if callable(force_stop):
        force_stop()
    if worker.wait(force_wait_ms):
        return True
    worker.terminate()
    return worker.wait(3000)


def _clear_layout_items(layout) -> None:
    while layout.count():
        layout.takeAt(0)


def create_camera_recording_session_dir(
    base_dir: Path,
    prefix: str = "camera_recording",
    add_timestamp: bool = True,
) -> Path:
    base_dir.mkdir(parents=True, exist_ok=True)
    session_name = str(prefix).strip() or "camera_recording"
    if add_timestamp:
        session_name = f"{session_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    for index in range(100):
        suffix = "" if index == 0 else f"_{index:02d}"
        session_dir = base_dir / f"{session_name}{suffix}"
        try:
            session_dir.mkdir()
            return session_dir
        except FileExistsError:
            continue
    raise RuntimeError(f"Failed to create camera recording folder under {base_dir}")


def sanitize_recording_session_name(name: str, default: str = DEFAULT_CAMERA_RECORDING_NAME) -> str:
    text = str(name or "").strip()
    if text.lower().endswith(".bag"):
        text = text[:-4]
    text = "_".join(text.split())
    invalid_chars = '<>:"/\\|?*\0'
    text = "".join("_" if char in invalid_chars or ord(char) < 32 else char for char in text)
    text = text.strip(" ._")
    return text if text and text not in {".", ".."} else default


def render_depth_3d(depth_mm, cv2, np):
    """Render depth using the relief-lighting pipeline from Orbbec quick_start.py."""

    depth_8bit = depth_to_8bit(depth_mm, np)

    grad_x = cv2.Scharr(depth_8bit, cv2.CV_32F, 1, 0)
    grad_y = cv2.Scharr(depth_8bit, cv2.CV_32F, 0, 1)
    magnitude = cv2.magnitude(grad_x, grad_y) + 1.0
    lighting = -0.707 * (grad_x + grad_y) / magnitude
    lighting = lighting * 0.15 + 0.85
    np.clip(lighting, 0.7, 1.0, out=lighting)

    depth_colored = cv2.applyColorMap(depth_8bit, cv2.COLORMAP_JET)
    return (depth_colored * lighting[..., np.newaxis]).astype(np.uint8)


def render_depth_2d(depth_mm, cv2, np):
    """Render a conventional fixed-range 2D depth colormap."""

    depth_colored = cv2.applyColorMap(depth_to_8bit(depth_mm, np), cv2.COLORMAP_JET)
    invalid = (depth_mm < MIN_DEPTH_MM) | (depth_mm > MAX_DEPTH_MM)
    depth_colored[invalid] = 0
    return depth_colored


def depth_to_8bit(depth_mm, np):
    depth_clipped = np.clip(depth_mm, MIN_DEPTH_MM, MAX_DEPTH_MM)
    depth_norm = (depth_clipped - MIN_DEPTH_MM) / (MAX_DEPTH_MM - MIN_DEPTH_MM + 1e-6)
    return (np.power(depth_norm, 0.8) * 255).astype(np.uint8)


def frame_to_bgr_image(frame, cv2, np, ob_format):
    """Convert common Orbbec color frame formats into a BGR ndarray."""

    width = frame.get_width()
    height = frame.get_height()
    data = np.asanyarray(frame.get_data())
    color_format = frame.get_format()

    if color_format == ob_format.RGB:
        return cv2.cvtColor(np.resize(data, (height, width, 3)), cv2.COLOR_RGB2BGR)
    if color_format == ob_format.BGR:
        return np.resize(data, (height, width, 3)).copy()
    if color_format == ob_format.YUYV:
        return cv2.cvtColor(np.resize(data, (height, width, 2)), cv2.COLOR_YUV2BGR_YUYV)
    if color_format == ob_format.UYVY:
        return cv2.cvtColor(np.resize(data, (height, width, 2)), cv2.COLOR_YUV2BGR_UYVY)
    if color_format == ob_format.MJPG:
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    if color_format == ob_format.I420:
        return cv2.cvtColor(data.reshape((height * 3 // 2, width)), cv2.COLOR_YUV2BGR_I420)
    if color_format == ob_format.NV12:
        return cv2.cvtColor(data.reshape((height * 3 // 2, width)), cv2.COLOR_YUV2BGR_NV12)
    if color_format == ob_format.NV21:
        return cv2.cvtColor(data.reshape((height * 3 // 2, width)), cv2.COLOR_YUV2BGR_NV21)
    return None


def depth_frame_to_bgr_image(frame, cv2, np, depth_mode: str = "2d"):
    width = frame.get_width()
    height = frame.get_height()
    depth_data = np.frombuffer(frame.get_data(), dtype=np.uint16)
    if depth_data.size != width * height:
        return None
    depth_mm = depth_data.reshape((height, width)).astype(np.float32)
    depth_mm *= frame.get_depth_scale()
    if depth_mode == "3d":
        return render_depth_3d(depth_mm, cv2, np)
    return render_depth_2d(depth_mm, cv2, np)


class CameraStreamWorker(QThread):
    status_changed = pyqtSignal(str)
    device_info_changed = pyqtSignal(str, str, str)
    stream_error = pyqtSignal(str)
    stream_started = pyqtSignal()
    stream_stopped = pyqtSignal()
    recording_started = pyqtSignal(str)
    recording_stopped = pyqtSignal(str)
    recording_segment_saved = pyqtSignal(str)
    recording_error = pyqtSignal(str)

    def __init__(self, device_index: int = 0, parent: QWidget | None = None):
        super().__init__(parent)
        self.device_index = device_index
        self._depth_mode_lock = threading.Lock()
        self._depth_mode = "2d"
        self._latest_frame_lock = threading.Lock()
        self._latest_color_frame = None
        self._latest_depth_frame = None
        self._command_queue = None
        self._event_queue = None
        self._frame_queue = None
        self._process: mp.Process | None = None

    def request_start_recording(self, path: Path) -> None:
        self._send_command(("start_recording", str(path)))

    def request_stop_recording(self) -> None:
        self._send_command(("stop_recording", None))

    def set_point_cloud_recording_enabled(self, enabled: bool) -> None:
        self._send_command(("point_cloud_recording_enabled", bool(enabled)))

    def set_depth_mode(self, mode: str) -> None:
        normalized = "3d" if str(mode).lower() == "3d" else "2d"
        with self._depth_mode_lock:
            self._depth_mode = normalized
        self._send_command(("depth_mode", normalized))

    def _current_depth_mode(self) -> str:
        with self._depth_mode_lock:
            return self._depth_mode

    def take_latest_frames(self):
        """Return the newest available preview frames and discard older ones."""

        with self._latest_frame_lock:
            color_frame = self._latest_color_frame
            depth_frame = self._latest_depth_frame
            self._latest_color_frame = None
            self._latest_depth_frame = None
        return color_frame, depth_frame

    def requestInterruption(self) -> None:
        self._send_command(("stop", None))
        super().requestInterruption()

    def force_stop_process(self) -> None:
        _force_stop_process(self._process)

    def run(self) -> None:
        context = mp.get_context("spawn")
        self._event_queue = context.Queue(maxsize=64)
        self._frame_queue = context.Queue(maxsize=2)
        self._command_queue = context.Queue(maxsize=16)
        self._process = context.Process(
            target=run_orbbec_camera_process,
            args=(
                self.device_index,
                self._current_depth_mode(),
                self._event_queue,
                self._frame_queue,
                self._command_queue,
            ),
            daemon=True,
        )
        stopped_event_received = False
        try:
            import cv2
            import numpy as np

            self._process.start()
            while not self.isInterruptionRequested():
                stopped_event_received = self._drain_event_queue() or stopped_event_received
                self._drain_frame_queue(cv2, np)
                if self._process is not None and not self._process.is_alive():
                    break
                self.msleep(10)
        finally:
            self._stop_process()
            stopped_event_received = self._drain_event_queue() or stopped_event_received
            with self._latest_frame_lock:
                self._latest_color_frame = None
                self._latest_depth_frame = None
            if not stopped_event_received:
                self.stream_stopped.emit()

    def _send_command(self, command) -> None:
        command_queue = self._command_queue
        if command_queue is None:
            return
        important = bool(command and command[0] in {"stop", "stop_recording"})
        try:
            if important:
                try:
                    command_queue.put(command, timeout=0.5)
                    return
                except queue.Full:
                    try:
                        command_queue.get_nowait()
                    except queue.Empty:
                        pass
                    command_queue.put(command, timeout=0.5)
                    return
            command_queue.put_nowait(command)
        except queue.Full:
            pass

    def _stop_process(self) -> None:
        process = self._process
        if process is None:
            return
        self._send_command(("stop", None))
        process.join(8.0)
        if process.is_alive():
            self._send_command(("stop", None))
            process.join(4.0)
        if process.is_alive():
            self.stream_error.emit(
                "Camera process did not stop cleanly; forcing termination. "
                "If the next open reports UVC connection errors, power-cycle or replug the camera."
            )
            _force_stop_process(process)
        if process.exitcode not in (0, None) and not self.isInterruptionRequested():
            self.stream_error.emit(
                f"Camera process exited unexpectedly (exit code {process.exitcode})."
            )

    def _drain_event_queue(self) -> bool:
        event_queue = self._event_queue
        if event_queue is None:
            return False
        stopped = False
        while True:
            try:
                message = event_queue.get_nowait()
            except queue.Empty:
                return stopped
            event = message[0]
            payload = message[1:]
            if event == "status":
                self.status_changed.emit(str(payload[0]))
            elif event == "device_info":
                name = str(payload[0]) if len(payload) > 0 else ""
                serial = str(payload[1]) if len(payload) > 1 else ""
                connection = str(payload[2]) if len(payload) > 2 else ""
                self.device_info_changed.emit(name, serial, connection)
            elif event == "error":
                self.stream_error.emit(str(payload[0]))
            elif event == "started":
                self.stream_started.emit()
            elif event == "stopped":
                self.stream_stopped.emit()
                stopped = True
            elif event == "recording_started":
                self.recording_started.emit(str(payload[0]))
            elif event == "recording_stopped":
                self.recording_stopped.emit(str(payload[0]))
            elif event == "recording_segment_saved":
                self.recording_segment_saved.emit(str(payload[0]))
            elif event == "recording_error":
                self.recording_error.emit(str(payload[0]))

    def _drain_frame_queue(self, cv2, np) -> None:
        frame_queue = self._frame_queue
        if frame_queue is None:
            return
        latest = None
        while True:
            try:
                latest = frame_queue.get_nowait()
            except queue.Empty:
                break
        if latest is None:
            return
        color_bytes, depth_bytes = latest
        color_image = self._decode_preview_image(color_bytes, cv2, np)
        depth_image = self._decode_preview_image(depth_bytes, cv2, np)
        with self._latest_frame_lock:
            if color_image is not None:
                self._latest_color_frame = color_image
            if depth_image is not None:
                self._latest_depth_frame = depth_image

    @staticmethod
    def _decode_preview_image(image_bytes, cv2, np):
        if image_bytes is None:
            return None
        try:
            bgr_image = cv2.imdecode(
                np.frombuffer(image_bytes, dtype=np.uint8),
                cv2.IMREAD_COLOR,
            )
            if bgr_image is None:
                return None
            return cv2.cvtColor(bgr_image, cv2.COLOR_BGR2RGB)
        except Exception:
            return None


class MultiCameraSyncWorker(QThread):
    status_changed = pyqtSignal(int, str)
    device_info_changed = pyqtSignal(int, str, str, str)
    align_status_changed = pyqtSignal(int, bool, str)
    stream_error = pyqtSignal(int, str)
    stream_started = pyqtSignal()
    stream_stopped = pyqtSignal(int)
    recording_started = pyqtSignal(int, str)
    recording_stopped = pyqtSignal(int, str)
    recording_segment_saved = pyqtSignal(int, str)
    recording_error = pyqtSignal(int, str)

    def __init__(
        self,
        camera_count: int = 3,
        sync_config_path: Path | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.camera_count = int(max(1, min(3, camera_count)))
        self.active_camera_count = 0
        self.hardware_align_status: dict[int, tuple[bool, str]] = {}
        self.sync_config_path = Path(sync_config_path or DEFAULT_SYNC_CONFIG_PATH)
        self._depth_mode_lock = threading.Lock()
        self._depth_mode = "2d"
        self._latest_frame_lock = threading.Lock()
        self._latest_frames: dict[int, tuple[object, object]] = {}
        self._command_queue = None
        self._event_queue = None
        self._frame_queue = None
        self._process: mp.Process | None = None

    def request_start_recording(self, paths_by_index: dict[int, Path]) -> None:
        max_count = self.active_camera_count
        if max_count <= 0:
            return
        self._send_command(
            (
                "start_recording",
                {
                    int(index): str(path)
                    for index, path in paths_by_index.items()
                    if 0 <= int(index) < max_count
                },
            )
        )

    def request_stop_recording(self) -> None:
        self._send_command(("stop_recording", None))

    def set_point_cloud_recording_enabled(self, enabled: bool) -> None:
        self._send_command(("point_cloud_recording_enabled", bool(enabled)))

    def set_depth_mode(self, mode: str) -> None:
        normalized = "3d" if str(mode).lower() == "3d" else "2d"
        with self._depth_mode_lock:
            self._depth_mode = normalized
        self._send_command(("depth_mode", normalized))

    def take_latest_frames(self) -> dict[int, tuple[object, object]]:
        with self._latest_frame_lock:
            frames = dict(self._latest_frames)
            self._latest_frames.clear()
        return frames

    def requestInterruption(self) -> None:
        self._send_command(("stop", None))
        super().requestInterruption()

    def force_stop_process(self) -> None:
        _force_stop_process(self._process)

    def run(self) -> None:
        context = mp.get_context("spawn")
        self._event_queue = context.Queue(maxsize=128)
        self._frame_queue = context.Queue(maxsize=2)
        self._command_queue = context.Queue(maxsize=16)
        self._process = context.Process(
            target=run_orbbec_multi_camera_sync_process,
            args=(
                self.camera_count,
                self._current_depth_mode(),
                str(self.sync_config_path),
                self._event_queue,
                self._frame_queue,
                self._command_queue,
            ),
            daemon=True,
        )
        any_stopped = False
        try:
            import cv2
            import numpy as np

            self.active_camera_count = 0
            self.hardware_align_status.clear()
            self._process.start()
            while not self.isInterruptionRequested():
                any_stopped = self._drain_event_queue() or any_stopped
                self._drain_frame_queue(cv2, np)
                if self._process is not None and not self._process.is_alive():
                    break
                self.msleep(10)
        finally:
            self._stop_process()
            self._drain_event_queue()
            with self._latest_frame_lock:
                self._latest_frames.clear()
            if not any_stopped:
                stop_count = self.active_camera_count or self.camera_count
                for index in range(stop_count):
                    self.stream_stopped.emit(index)

    def _current_depth_mode(self) -> str:
        with self._depth_mode_lock:
            return self._depth_mode

    def _send_command(self, command) -> None:
        command_queue = self._command_queue
        if command_queue is None:
            return
        important = bool(command and command[0] in {"stop", "stop_recording"})
        try:
            if important:
                try:
                    command_queue.put(command, timeout=0.5)
                    return
                except queue.Full:
                    try:
                        command_queue.get_nowait()
                    except queue.Empty:
                        pass
                    command_queue.put(command, timeout=0.5)
                    return
            command_queue.put_nowait(command)
        except queue.Full:
            pass

    def _stop_process(self) -> None:
        process = self._process
        if process is None:
            return
        self._send_command(("stop", None))
        process.join(12.0)
        if process.is_alive():
            self._send_command(("stop", None))
            process.join(5.0)
        if process.is_alive():
            self.stream_error.emit(
                -1,
                "Multi-camera sync process did not stop cleanly; forcing termination. "
                "If the next open reports UVC connection errors, power-cycle or replug the cameras.",
            )
            _force_stop_process(process)
        if process.exitcode not in (0, None) and not self.isInterruptionRequested():
            self.stream_error.emit(-1, f"Multi-camera sync process exited unexpectedly ({process.exitcode}).")

    def _drain_event_queue(self) -> bool:
        event_queue = self._event_queue
        if event_queue is None:
            return False
        stopped = False
        while True:
            try:
                message = event_queue.get_nowait()
            except queue.Empty:
                return stopped
            event = message[0]
            payload = message[1:]
            if event == "status":
                self.status_changed.emit(int(payload[0]), str(payload[1]))
            elif event == "device_info":
                self.device_info_changed.emit(
                    int(payload[0]), str(payload[1]), str(payload[2]), str(payload[3])
                )
            elif event == "align_status":
                index = int(payload[0])
                enabled = bool(payload[1])
                status = str(payload[2]) if len(payload) > 2 else ""
                self.hardware_align_status[index] = (enabled, status)
                self.align_status_changed.emit(index, enabled, status)
            elif event == "error":
                self.stream_error.emit(int(payload[0]), str(payload[1]))
            elif event == "started":
                if payload:
                    self.active_camera_count = max(
                        0,
                        min(self.camera_count, int(payload[0])),
                    )
                else:
                    self.active_camera_count = self.camera_count
                self.stream_started.emit()
            elif event == "stopped":
                self.stream_stopped.emit(int(payload[0]))
                stopped = True
            elif event == "recording_started":
                self.recording_started.emit(int(payload[0]), str(payload[1]))
            elif event == "recording_stopped":
                self.recording_stopped.emit(int(payload[0]), str(payload[1]))
            elif event == "recording_segment_saved":
                self.recording_segment_saved.emit(int(payload[0]), str(payload[1]))
            elif event == "recording_error":
                self.recording_error.emit(int(payload[0]), str(payload[1]))

    def _drain_frame_queue(self, cv2, np) -> None:
        frame_queue = self._frame_queue
        if frame_queue is None:
            return
        latest_by_index = {}
        while True:
            try:
                index, color_bytes, depth_bytes = frame_queue.get_nowait()
            except queue.Empty:
                break
            latest_by_index[int(index)] = (color_bytes, depth_bytes)
        if not latest_by_index:
            return
        with self._latest_frame_lock:
            for index, (color_bytes, depth_bytes) in latest_by_index.items():
                color_image = CameraStreamWorker._decode_preview_image(color_bytes, cv2, np)
                depth_image = CameraStreamWorker._decode_preview_image(depth_bytes, cv2, np)
                self._latest_frames[index] = (color_image, depth_image)


class CameraStandaloneResetWorker(QThread):
    finished_status = pyqtSignal(str)
    reset_error = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._process = None

    def requestInterruption(self) -> None:
        self.force_stop_process()
        super().requestInterruption()

    def force_stop_process(self) -> None:
        _force_stop_process(self._process)

    def run(self) -> None:
        context = mp.get_context("spawn")
        process = context.Process(target=reset_orbbec_devices_to_standalone, daemon=True)
        self._process = process
        try:
            process.start()
            process.join(8.0)
            if process.is_alive():
                _force_stop_process(process, terminate_timeout=1.0)
                raise RuntimeError("Timed out while setting Orbbec cameras to STANDALONE.")
            if process.exitcode not in (0, None):
                if self.isInterruptionRequested():
                    return
                raise RuntimeError(f"Standalone reset process exited with code {process.exitcode}.")
            self.finished_status.emit("所有 Orbbec 相机已切回 STANDALONE。")
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.reset_error.emit(str(exc))
        finally:
            self._process = None


class BagMp4Converter(QThread):
    conversion_started = pyqtSignal(str)
    conversion_finished = pyqtSignal(str, int)
    conversion_error = pyqtSignal(str)

    def __init__(
        self,
        bag_path: Path,
        mp4_path: Path,
        stream_type: str = "rgb",
        depth_mode: str = "2d",
        default_fps: float = 30.0,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.bag_path = Path(bag_path)
        self.mp4_path = Path(mp4_path)
        self.stream_type = "depth" if str(stream_type).lower() == "depth" else "rgb"
        self.depth_mode = "3d" if str(depth_mode).lower() == "3d" else "2d"
        self.default_fps = default_fps
        self._playback_status_lock = threading.Lock()
        self._playback_status = None

    def run(self) -> None:
        writer = None
        pipeline = None
        target_size: tuple[int, int] | None = None
        frame_count = 0
        empty_polls = 0
        try:
            import cv2
            import numpy as np
            from pyorbbecsdk import Config, OBFormat, OBPlaybackStatus, OBSensorType
            from pyorbbecsdk import Pipeline, PlaybackDevice

            self.conversion_started.emit(str(self.mp4_path))
            playback = PlaybackDevice(str(self.bag_path))
            playback.set_playback_status_change_callback(self._on_playback_status_changed)

            pipeline = Pipeline(playback)
            config = Config()
            if self.stream_type == "depth":
                config.enable_stream(OBSensorType.DEPTH_SENSOR)
            else:
                config.enable_stream(OBSensorType.COLOR_SENSOR)
            pipeline.start(config)

            while not self.isInterruptionRequested():
                frames = pipeline.wait_for_frames(100)
                if frames is None:
                    empty_polls += 1
                    if self._is_playback_stopped(OBPlaybackStatus):
                        break
                    if empty_polls >= 50:
                        if frame_count > 0:
                            break
                        raise RuntimeError(
                            f"Timed out waiting for {self._stream_label()} frames from bag recording"
                        )
                    continue
                empty_polls = 0

                frame = frames.get_depth_frame() if self.stream_type == "depth" else frames.get_color_frame()
                if frame is None:
                    empty_polls += 1
                    if self._is_playback_stopped(OBPlaybackStatus):
                        break
                    if empty_polls >= 50:
                        if frame_count > 0:
                            break
                        raise RuntimeError(f"No {self._stream_label()} frames found in bag recording")
                    continue
                empty_polls = 0

                if self.stream_type == "depth":
                    bgr_image = depth_frame_to_bgr_image(frame, cv2, np, self.depth_mode)
                else:
                    bgr_image = frame_to_bgr_image(frame, cv2, np, OBFormat)
                if bgr_image is None:
                    raise RuntimeError(
                        f"Unsupported {self._stream_label()} format while converting: {frame.get_format()}"
                    )

                if writer is None:
                    fps = self._frame_fps(frame) or self.default_fps
                    height, width = bgr_image.shape[:2]
                    width -= width % 2
                    height -= height % 2
                    if width <= 0 or height <= 0:
                        raise RuntimeError(f"Invalid {self._stream_label()} frame size for MP4 conversion")
                    bgr_image = bgr_image[:height, :width]
                    writer = cv2.VideoWriter(
                        str(self.mp4_path),
                        cv2.VideoWriter_fourcc(*"mp4v"),
                        fps,
                        (width, height),
                    )
                    if not writer.isOpened():
                        raise RuntimeError(f"Failed to open MP4 writer: {self.mp4_path}")
                    target_size = (width, height)
                else:
                    if target_size is not None:
                        width, height = target_size
                        if bgr_image.shape[1] != width or bgr_image.shape[0] != height:
                            bgr_image = cv2.resize(bgr_image, (width, height))

                writer.write(bgr_image)
                frame_count += 1

            if frame_count == 0:
                raise RuntimeError(f"No {self._stream_label()} frames found in bag recording")
            self.conversion_finished.emit(str(self.mp4_path), frame_count)
        except Exception as exc:
            self.conversion_error.emit(str(exc))
        finally:
            if writer is not None:
                writer.release()
            if pipeline is not None:
                try:
                    pipeline.stop()
                except Exception:
                    pass

    def _on_playback_status_changed(self, status) -> None:
        with self._playback_status_lock:
            self._playback_status = status

    def _is_playback_stopped(self, playback_status_type) -> bool:
        with self._playback_status_lock:
            return self._playback_status == playback_status_type.STOPPED

    def _stream_label(self) -> str:
        return "depth" if self.stream_type == "depth" else "RGB"

    @staticmethod
    def _frame_fps(frame) -> float | None:
        try:
            profile = frame.get_stream_profile()
            if not profile.is_video_stream_profile():
                return None
            fps = float(profile.as_video_stream_profile().get_fps())
            return fps if fps > 0 else None
        except Exception:
            return None


class CameraPanel(QWidget):
    def __init__(
        self,
        title: str,
        device_index: int,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("cameraPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.title = title
        self.device_index = device_index
        self.worker: CameraStreamWorker | None = None
        self._converters: list[BagMp4Converter] = []
        self._last_pixmaps: dict[QLabel, QPixmap] = {}
        self._preview_cards: list[CardWidget] = []
        self.recording_base_dir = recording_output_dir()
        self._recording = False
        self._recording_pending = False
        self._recording_started_at: float | None = None
        self._recording_elapsed_ms = 0
        self._recording_segment_paths: list[Path] = []
        self._convert_recording_to_mp4 = False
        self._record_point_cloud_enabled = False
        self._playback_color_capture = None
        self._playback_depth_capture = None
        self._playback_started_at: float | None = None
        self._playback_duration_ms = 0
        self._playback_elapsed_ms = 0
        self._manual_stop_requested = False
        self._last_start_error: str | None = None
        self._remaining_start_retries = 0
        self._max_start_retries = 2
        self._start_retry_delay_ms = 1100
        self._depth_mode = "2d"
        self._compact_layout_enabled = False
        self.view_role = "first_person"
        self.recording_timer = QTimer(self)
        self.recording_timer.setInterval(200)
        self.recording_timer.timeout.connect(self._update_recording_time)
        self.preview_timer = QTimer(self)
        self.preview_timer.setInterval(33)
        self.preview_timer.timeout.connect(self._refresh_latest_frame)
        self.playback_timer = QTimer(self)
        self.playback_timer.setInterval(33)
        self.playback_timer.timeout.connect(self._refresh_playback_frame)
        self.start_retry_timer = QTimer(self)
        self.start_retry_timer.setSingleShot(True)
        self.start_retry_timer.timeout.connect(self._retry_start_after_failure)

        self.panel_layout = QVBoxLayout(self)
        self.panel_layout.setContentsMargins(16, 16, 16, 16)
        self.panel_layout.setSpacing(12)

        self.title_row = QHBoxLayout()
        self.title_row.setSpacing(8)
        self.title_label = SubtitleLabel(title, self)
        self.title_row.addWidget(self.title_label)
        self.serial_label = CaptionLabel("SN: --", self)
        self.serial_label.setMinimumWidth(150)
        self.title_row.addWidget(self.serial_label)
        self.title_row.addStretch(1)
        self.panel_layout.addLayout(self.title_row)

        self.previews_layout = QVBoxLayout()
        self.previews_layout.setSpacing(16)
        self.color_preview = self._build_preview_card("Color View", self.previews_layout)
        self.depth_preview = self._build_preview_card("Depth View", self.previews_layout)
        self.panel_layout.addLayout(self.previews_layout, 1)
        self.control_card = self._build_control_card()
        self.panel_layout.addWidget(self.control_card, 0)
        self.set_compact_layout(False)

    def _build_control_card(self) -> CardWidget:
        card = CardWidget(self)
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.control_grid = QGridLayout(card)
        self.control_grid.setContentsMargins(16, 14, 16, 16)
        self.control_grid.setHorizontalSpacing(10)
        self.control_grid.setVerticalSpacing(10)

        self.start_button = PrimaryPushButton("Start Camera", card)
        self.start_button.clicked.connect(self.start)
        self.stop_button = PushButton("Stop Camera", card)
        self.stop_button.clicked.connect(self.stop)
        self.stop_button.setEnabled(False)
        self.record_button = PushButton("Start Recording", card)
        self.record_button.clicked.connect(self._toggle_recording)
        self.record_button.setEnabled(False)
        self.recording_time_label = CaptionLabel("Recording: 00:00:00", card)
        self.recording_time_label.setMinimumWidth(145)
        self.depth_mode_button = PushButton("Depth Mode: 2D", card)
        self.depth_mode_button.clicked.connect(self._toggle_depth_mode)
        self.view_role_combo = ComboBox(card)
        self.view_role_combo.setProperty("i18nTranslateItems", True)
        self.view_role_combo.addItem("First-Person View", userData="first_person")
        self.view_role_combo.addItem("Third-Person View", userData="third_person")
        self.view_role_combo.currentIndexChanged.connect(self._set_view_role)
        self.view_role_combo.setMinimumWidth(120)
        for widget in (
            self.start_button,
            self.stop_button,
            self.record_button,
            self.depth_mode_button,
        ):
            widget.setMinimumWidth(0)
        self.status_label = CaptionLabel(f"{self.title} idle", card)
        self.status_label.setWordWrap(True)
        self._arrange_control_widgets()
        return card

    def _arrange_control_widgets(self) -> None:
        _clear_layout_items(self.control_grid)
        compact = self._compact_layout_enabled
        self.control_grid.setContentsMargins(
            10 if compact else 16,
            8 if compact else 14,
            10 if compact else 16,
            10 if compact else 16,
        )
        self.control_grid.setHorizontalSpacing(6 if compact else 10)
        self.control_grid.setVerticalSpacing(6 if compact else 10)
        self.recording_time_label.setMinimumWidth(108 if compact else 145)
        self.view_role_combo.setMinimumWidth(116 if compact else 150)
        for column in range(8):
            self.control_grid.setColumnStretch(column, 0)

        if compact:
            self.control_grid.addWidget(self.start_button, 0, 0)
            self.control_grid.addWidget(self.stop_button, 0, 1)
            self.control_grid.addWidget(self.record_button, 1, 0)
            self.control_grid.addWidget(self.recording_time_label, 1, 1)
            self.control_grid.addWidget(self.depth_mode_button, 2, 0)
            self.control_grid.addWidget(self.view_role_combo, 2, 1)
            self.control_grid.addWidget(self.status_label, 3, 0, 1, 2)
            self.control_grid.setColumnStretch(0, 1)
            self.control_grid.setColumnStretch(1, 1)
            return

        self.control_grid.addWidget(self.start_button, 0, 0)
        self.control_grid.addWidget(self.stop_button, 0, 1)
        self.control_grid.addWidget(self.record_button, 0, 2)
        self.control_grid.addWidget(self.recording_time_label, 0, 3)
        self.control_grid.addWidget(self.depth_mode_button, 0, 4)
        self.control_grid.addWidget(self.view_role_combo, 0, 5)
        self.control_grid.addWidget(self.status_label, 1, 0, 1, 7)
        self.control_grid.setColumnStretch(6, 1)

    def set_compact_layout(self, enabled: bool) -> None:
        enabled = bool(enabled)
        self._compact_layout_enabled = enabled
        self.panel_layout.setContentsMargins(
            8 if enabled else 16,
            8 if enabled else 16,
            8 if enabled else 16,
            8 if enabled else 16,
        )
        self.panel_layout.setSpacing(6 if enabled else 12)
        self.title_row.setSpacing(6 if enabled else 8)
        self.previews_layout.setSpacing(8 if enabled else 16)
        self.title_label.setText(f"Camera {self.device_index + 1}" if enabled else self.title)
        self.serial_label.setMinimumWidth(88 if enabled else 150)
        self.control_card.setMinimumWidth(0)
        self.control_card.setMaximumWidth(16777215)
        self.color_preview.setMinimumSize(180 if enabled else 320, 132 if enabled else 240)
        self.depth_preview.setMinimumSize(180 if enabled else 320, 132 if enabled else 240)
        for preview in (self.color_preview, self.depth_preview):
            preview.setMaximumWidth(16777215)
            preview.setMaximumHeight(180 if enabled else 16777215)
        for card in self._preview_cards:
            card.setMinimumWidth(0 if enabled else 320)
            card.setMinimumHeight(0 if enabled else 260)
            card.setMaximumWidth(16777215)
            card.setMaximumHeight(238 if enabled else 16777215)
        for widget in (
            self.start_button,
            self.stop_button,
            self.record_button,
            self.depth_mode_button,
        ):
            widget.setMaximumWidth(120 if enabled else 16777215)
        self._arrange_control_widgets()
        for preview in list(self._last_pixmaps):
            self._update_preview(preview)

    def _build_preview_card(self, title: str, row: QVBoxLayout) -> QLabel:
        card = CardWidget(self)
        self._preview_cards.append(card)
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(10)
        layout.addWidget(StrongBodyLabel(title, card))

        preview = QLabel("Waiting for camera stream", card)
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview.setMinimumSize(320, 240)
        preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._set_preview_placeholder(preview)
        layout.addWidget(preview, 1)
        row.addWidget(card, 1)
        return preview

    @staticmethod
    def _set_preview_placeholder(preview: QLabel) -> None:
        CameraPanel._set_preview_message(preview, "Waiting for camera stream")

    @staticmethod
    def _set_preview_message(preview: QLabel, message: str) -> None:
        preview.clear()
        preview.setText(message)
        preview.setStyleSheet(
            "QLabel { background: #111111; color: #A0A0A0; border-radius: 6px; }"
        )

    def start(self) -> None:
        self.start_with_retry()

    def start_with_retry(self, retries: int | None = None) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        self.start_retry_timer.stop()
        self._remaining_start_retries = self._max_start_retries if retries is None else max(0, int(retries))
        self._manual_stop_requested = False
        self._last_start_error = None
        self._start_worker()

    def _start_worker(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return

        self.stop_playback()
        self._last_pixmaps.clear()
        self._set_preview_placeholder(self.color_preview)
        self._set_preview_placeholder(self.depth_preview)
        self.worker = CameraStreamWorker(self.device_index, self)
        self.worker.set_depth_mode(self._depth_mode)
        self.worker.status_changed.connect(self.status_label.setText)
        self.worker.device_info_changed.connect(self._on_device_info_changed)
        self.worker.stream_error.connect(self._on_error)
        self.worker.stream_started.connect(self._on_stream_started)
        self.worker.stream_stopped.connect(self._on_stream_stopped)
        self.worker.recording_started.connect(self._on_recording_started)
        self.worker.recording_stopped.connect(self._on_recording_stopped)
        self.worker.recording_segment_saved.connect(self._on_recording_segment_saved)
        self.worker.recording_error.connect(self._on_recording_error)
        self.worker.finished.connect(self._on_stopped)
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        retry_text = (
            f" (retry left: {self._remaining_start_retries})"
            if self._remaining_start_retries
            else ""
        )
        self.status_label.setText(f"Starting {self.title}{retry_text}...")
        self.worker.start()

    def is_running(self) -> bool:
        return self.worker is not None and self.worker.isRunning()

    def is_recording_or_pending(self) -> bool:
        return self._recording or self._recording_pending

    def is_playing_back(self) -> bool:
        return self._playback_started_at is not None

    def stop(self) -> None:
        self._manual_stop_requested = True
        self.start_retry_timer.stop()
        self._remaining_start_retries = 0
        if self.worker is None or not self.worker.isRunning():
            return
        if self._recording or self._recording_pending:
            self.worker.request_stop_recording()
        self.status_label.setText(f"Stopping {self.title}...")
        self.stop_button.setEnabled(False)
        self.record_button.setEnabled(False)
        self.worker.requestInterruption()

    def shutdown(self) -> bool:
        self.preview_timer.stop()
        self.start_retry_timer.stop()
        self._manual_stop_requested = True
        self._remaining_start_retries = 0
        self.stop_playback()
        if self.worker is None or not self.worker.isRunning():
            self._stop_converters()
            return True
        self.worker.request_stop_recording()
        stopped = _shutdown_thread(self.worker, wait_ms=5000, force_wait_ms=7000)
        self._stop_converters()
        return stopped

    def set_recording_output_dir(self, path: Path) -> None:
        self.recording_base_dir = Path(path).expanduser().resolve()

    def set_recording_mp4_conversion_enabled(self, enabled: bool) -> None:
        self._convert_recording_to_mp4 = bool(enabled)

    def set_point_cloud_recording_enabled(self, enabled: bool) -> None:
        self._record_point_cloud_enabled = bool(enabled)
        if self.worker is not None and self.worker.isRunning():
            self.worker.set_point_cloud_recording_enabled(self._record_point_cloud_enabled)

    def set_sync_mode_controls_enabled(self, enabled: bool) -> None:
        self.start_button.setEnabled(enabled)
        self.stop_button.setEnabled(enabled and self.worker is not None and self.worker.isRunning())
        self.record_button.setEnabled(False)

    def set_sync_stream_started(self) -> None:
        self._manual_stop_requested = False
        self._last_start_error = None
        self._remaining_start_retries = 0
        self.start_retry_timer.stop()
        self.preview_timer.stop()
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.record_button.setEnabled(True)

    def set_sync_stream_stopped(self) -> None:
        self.preview_timer.stop()
        self._last_pixmaps.clear()
        self._set_preview_placeholder(self.color_preview)
        self._set_preview_placeholder(self.depth_preview)
        self._recording = False
        self._recording_pending = False
        self.record_button.setText("Start Recording")
        self.record_button.setEnabled(False)
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)

    def set_sync_status(self, message: str) -> None:
        self.status_label.setText(message)

    def set_sync_error(self, message: str) -> None:
        self.status_label.setText(f"Camera error: {message}")

    def set_device_info(self, name: str, serial: str, connection: str = "") -> None:
        serial_text = serial or "--"
        suffix = f" · {connection}" if connection else ""
        self.serial_label.setText(f"SN: {serial_text}{suffix}")

    def apply_sync_preview_frame(self, color_image, depth_image) -> None:
        if color_image is not None:
            self._set_preview_frame(self.color_preview, color_image)
        if depth_image is not None:
            self._set_preview_frame(self.depth_preview, depth_image)

    def on_sync_recording_started(self, path: str) -> None:
        self._recording = True
        self._recording_pending = False
        self._recording_segment_paths = []
        self._recording_started_at = time.monotonic()
        self._recording_elapsed_ms = 0
        self.record_button.setText("Stop Recording")
        self.record_button.setEnabled(True)
        self.recording_timer.start()
        self._update_recording_time()
        self.status_label.setText(f"Recording RGB-D: {Path(path).parent}")

    def on_sync_recording_stopped(self, path: str) -> None:
        self._on_recording_stopped(path)

    def on_sync_recording_segment_saved(self, path: str) -> None:
        self._on_recording_segment_saved(path)

    def _toggle_depth_mode(self) -> None:
        self._depth_mode = "3d" if self._depth_mode == "2d" else "2d"
        self.depth_mode_button.setText(f"Depth Mode: {self._depth_mode.upper()}")
        if self.worker is not None:
            self.worker.set_depth_mode(self._depth_mode)

    def _set_view_role(self, index: int) -> None:
        role = self.view_role_combo.itemData(index)
        if isinstance(role, str):
            self.view_role = role

    def recording_metadata(self) -> dict[str, int | str]:
        """Return the stable tag for a future all-camera recording manifest."""

        return {
            "camera_index": self.device_index,
            "view_role": self.view_role,
        }

    def _toggle_recording(self) -> None:
        if self._recording:
            self.stop_recording()
            return

        try:
            session_dir = create_camera_recording_session_dir(
                self.recording_base_dir,
                prefix=f"camera_{self.device_index + 1}_recording",
            )
        except OSError as exc:
            self._on_recording_error(str(exc))
            return

        self.start_recording_to_bag(session_dir / "rgb_depth.bag")

    def start_recording_to_bag(self, bag_path: Path) -> bool:
        if self.worker is None or not self.worker.isRunning() or self._recording_pending or self._recording:
            return False
        self.stop_playback()
        self._recording_pending = True
        self.record_button.setEnabled(False)
        self.recording_time_label.setText("Recording: starting...")
        self.worker.set_point_cloud_recording_enabled(self._record_point_cloud_enabled)
        self.worker.request_start_recording(bag_path)
        return True

    def stop_recording(self) -> None:
        if self.worker is None or not self.worker.isRunning() or not self.is_recording_or_pending():
            return
        self._recording_pending = True
        self.record_button.setEnabled(False)
        self.worker.request_stop_recording()

    def _on_stream_started(self) -> None:
        self._manual_stop_requested = False
        self._last_start_error = None
        self._remaining_start_retries = 0
        self.start_retry_timer.stop()
        self.record_button.setEnabled(True)
        self.preview_timer.start()

    def _on_stream_stopped(self) -> None:
        self.record_button.setEnabled(False)
        self.preview_timer.stop()
        if self.is_playing_back():
            return
        self._last_pixmaps.clear()
        self._set_preview_placeholder(self.color_preview)
        self._set_preview_placeholder(self.depth_preview)

    def start_playback(
        self,
        color_path: Path | None,
        depth_path: Path | None,
        record_dir: Path,
    ) -> bool:
        if self.is_recording_or_pending():
            self.status_label.setText("Stop recording before loading playback.")
            return False
        if color_path is None and depth_path is None:
            self.status_label.setText(f"No playback video found in {record_dir}")
            return False

        self.stop_playback()
        if self.worker is not None and self.worker.isRunning():
            self.stop()
        self.preview_timer.stop()
        self._last_pixmaps.clear()

        try:
            import cv2
        except ImportError as exc:
            self.status_label.setText(f"OpenCV unavailable for playback: {exc}")
            return False

        color_capture = self._open_video_capture(cv2, color_path)
        depth_capture = self._open_video_capture(cv2, depth_path)
        if color_path is not None and color_capture is None:
            self.status_label.setText(f"Failed to open RGB playback: {color_path}")
        if depth_path is not None and depth_capture is None:
            self.status_label.setText(f"Failed to open depth playback: {depth_path}")
        if color_capture is None and depth_capture is None:
            return False

        self._playback_color_capture = color_capture
        self._playback_depth_capture = depth_capture
        self._playback_duration_ms = max(
            self._video_duration_ms(color_capture),
            self._video_duration_ms(depth_capture),
        )
        playback_fps = max(
            self._video_fps(color_capture),
            self._video_fps(depth_capture),
            1.0,
        )
        self.playback_timer.setInterval(max(10, int(1000 / playback_fps)))
        self._playback_elapsed_ms = 0
        self._playback_started_at = time.perf_counter()
        if color_capture is None:
            self._set_preview_message(self.color_preview, "No RGB playback video")
        if depth_capture is None:
            self._set_preview_message(self.depth_preview, "No depth playback video")
        self.status_label.setText(
            f"Playback: {self._format_elapsed(0)} / "
            f"{self._format_elapsed(self._playback_duration_ms)} - {record_dir.name}"
        )
        self.playback_timer.start()
        self._refresh_playback_frame()
        return True

    def stop_playback(self) -> None:
        self.playback_timer.stop()
        for capture in (self._playback_color_capture, self._playback_depth_capture):
            if capture is not None:
                capture.release()
        self._playback_color_capture = None
        self._playback_depth_capture = None
        self._playback_started_at = None

    @staticmethod
    def _open_video_capture(cv2, path: Path | None):
        if path is None:
            return None
        capture = cv2.VideoCapture(str(path))
        if capture.isOpened():
            return capture
        capture.release()
        return None

    @staticmethod
    def _video_fps(capture) -> float:
        if capture is None:
            return 0.0
        fps = float(capture.get(5) or 0.0)
        return fps if fps > 0 else 0.0

    @classmethod
    def _video_duration_ms(cls, capture) -> int:
        if capture is None:
            return 0
        fps = cls._video_fps(capture)
        frame_count = float(capture.get(7) or 0.0)
        if fps <= 0 or frame_count <= 0:
            return 0
        return int(frame_count / fps * 1000)

    def _refresh_playback_frame(self) -> None:
        if self._playback_started_at is None:
            return
        self._playback_elapsed_ms = int(
            (time.perf_counter() - self._playback_started_at) * 1000
        )
        color_ok = self._read_playback_frame(self._playback_color_capture, self.color_preview)
        depth_ok = self._read_playback_frame(self._playback_depth_capture, self.depth_preview)
        if self._playback_duration_ms:
            self._playback_elapsed_ms = min(self._playback_elapsed_ms, self._playback_duration_ms)
        self.status_label.setText(
            f"Playback: {self._format_elapsed(self._playback_elapsed_ms)} / "
            f"{self._format_elapsed(self._playback_duration_ms)}"
        )
        if not color_ok and not depth_ok:
            elapsed = self._format_elapsed(self._playback_elapsed_ms)
            duration = self._format_elapsed(self._playback_duration_ms)
            self.stop_playback()
            self.status_label.setText(f"播放结束: {elapsed} / {duration}")

    def _read_playback_frame(self, capture, preview: QLabel) -> bool:
        if capture is None:
            return False
        ok, bgr_frame = capture.read()
        if not ok:
            return False
        try:
            import cv2
        except ImportError:
            return False
        rgb_frame = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2RGB)
        self._set_preview_frame(preview, rgb_frame)
        return True

    def _on_recording_started(self, path: str) -> None:
        self._recording = True
        self._recording_pending = False
        self._recording_segment_paths = []
        self._recording_started_at = time.monotonic()
        self._recording_elapsed_ms = 0
        self.record_button.setText("Stop Recording")
        self.record_button.setEnabled(True)
        self.recording_timer.start()
        self._update_recording_time()
        self.status_label.setText(f"Recording RGB-D: {Path(path).parent}")

    def _on_recording_segment_saved(self, path: str) -> None:
        if not path:
            return
        segment_path = Path(path)
        self._recording_segment_paths.append(segment_path)
        self.status_label.setText(f"Saved segment: {segment_path.name}")

    def _on_recording_stopped(self, path: str) -> None:
        self._capture_recording_elapsed()
        self._recording = False
        self._recording_pending = False
        self._recording_started_at = None
        self.recording_timer.stop()
        self.record_button.setText("Start Recording")
        self.record_button.setEnabled(self.worker is not None and self.worker.isRunning())
        self.recording_time_label.setText(
            f"Recorded: {self._format_elapsed(self._recording_elapsed_ms)}"
        )
        if path:
            final_path = Path(path)
            if final_path not in self._recording_segment_paths:
                self._recording_segment_paths.append(final_path)
            self.status_label.setText(f"RGB-D recording saved: {Path(path).parent}")
            if self._convert_recording_to_mp4:
                for bag_path in list(self._recording_segment_paths):
                    self._start_mp4_conversions(bag_path)
        self._recording_segment_paths = []

    def _on_recording_error(self, message: str) -> None:
        self._recording = False
        self._recording_pending = False
        self._recording_segment_paths = []
        self._recording_started_at = None
        self.recording_timer.stop()
        self.record_button.setText("Start Recording")
        self.record_button.setEnabled(self.worker is not None and self.worker.isRunning())
        self.recording_time_label.setText("Recording: error")
        self.status_label.setText(f"Recording error: {message}")

    def _on_device_info_changed(self, name: str, serial: str, connection: str) -> None:
        self.set_device_info(name, serial, connection)

    def _start_mp4_conversions(self, bag_path: Path) -> None:
        mp4_path = bag_path.with_suffix(".mp4")
        depth_mp4_path = self._depth_mp4_path_for_bag(bag_path)
        self._start_mp4_converter(bag_path, mp4_path, stream_type="rgb")
        self._start_mp4_converter(
            bag_path,
            depth_mp4_path,
            stream_type="depth",
            depth_mode=self._depth_mode,
        )

    def _start_mp4_converter(
        self,
        bag_path: Path,
        mp4_path: Path,
        stream_type: str,
        depth_mode: str = "2d",
    ) -> None:
        converter = BagMp4Converter(
            bag_path,
            mp4_path,
            stream_type=stream_type,
            depth_mode=depth_mode,
            parent=self,
        )
        self._converters.append(converter)
        converter.conversion_started.connect(self._on_conversion_started)
        converter.conversion_finished.connect(self._on_conversion_finished)
        converter.conversion_error.connect(self._on_conversion_error)
        converter.finished.connect(lambda worker=converter: self._remove_converter(worker))
        converter.start()

    @staticmethod
    def _depth_mp4_path_for_bag(bag_path: Path) -> Path:
        stem = bag_path.stem
        if stem == "rgb_depth":
            return bag_path.with_name("depth.mp4")
        if stem.endswith("_rgb_depth"):
            return bag_path.with_name(f"{stem.removesuffix('_rgb_depth')}_depth.mp4")
        return bag_path.with_name(f"{stem}_depth.mp4")

    def _on_conversion_started(self, path: str) -> None:
        self.status_label.setText(f"Converting MP4: {Path(path)}")

    def _on_conversion_finished(self, path: str, frame_count: int) -> None:
        self.status_label.setText(
            f"MP4 saved: {Path(path)} ({frame_count} frames)"
        )

    def _on_conversion_error(self, message: str) -> None:
        self.status_label.setText(f"MP4 conversion error: {message}")

    def _remove_converter(self, converter: BagMp4Converter) -> None:
        if converter in self._converters:
            self._converters.remove(converter)
        converter.deleteLater()

    def _stop_converters(self) -> None:
        for converter in list(self._converters):
            if converter.isRunning():
                _shutdown_thread(converter, wait_ms=1000, force_wait_ms=1000)

    def _capture_recording_elapsed(self) -> None:
        if self._recording_started_at is not None:
            self._recording_elapsed_ms = int(
                (time.monotonic() - self._recording_started_at) * 1000
            )

    def _update_recording_time(self) -> None:
        self._capture_recording_elapsed()
        self.recording_time_label.setText(
            f"Recording: {self._format_elapsed(self._recording_elapsed_ms)}"
        )

    @staticmethod
    def _format_elapsed(elapsed_ms: int) -> str:
        total_seconds = max(0, elapsed_ms // 1000)
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _refresh_latest_frame(self) -> None:
        if self.worker is None:
            return
        color_image, depth_image = self.worker.take_latest_frames()
        if color_image is not None:
            self._set_preview_frame(self.color_preview, color_image)
        if depth_image is not None:
            self._set_preview_frame(self.depth_preview, depth_image)

    def _set_preview_frame(self, preview: QLabel, rgb_image) -> None:
        height, width, channels = rgb_image.shape
        image = QImage(
            rgb_image.data,
            width,
            height,
            channels * width,
            QImage.Format.Format_RGB888,
        ).copy()
        self._last_pixmaps[preview] = QPixmap.fromImage(image)
        preview.setStyleSheet(
            "QLabel { background: transparent; color: #A0A0A0; border-radius: 6px; }"
        )
        self._update_preview(preview)

    def _on_error(self, message: str) -> None:
        self._last_start_error = message
        self.status_label.setText(f"Camera error: {message}")

    def _on_stopped(self) -> None:
        self.preview_timer.stop()
        self.worker = None
        if self._recording or self._recording_pending:
            self._on_recording_stopped("")
        if self.is_playing_back():
            self.start_button.setEnabled(True)
            self.stop_button.setEnabled(False)
            return
        if self._should_retry_start():
            self._schedule_start_retry()
            return
        if not self.status_label.text().startswith("Camera error:"):
            self.status_label.setText(f"{self.title} stopped")
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)

    def _should_retry_start(self) -> bool:
        return (
            not self._manual_stop_requested
            and self._last_start_error is not None
            and self._remaining_start_retries > 0
        )

    def _schedule_start_retry(self) -> None:
        self.status_label.setText(
            f"Camera error: {self._last_start_error}; retrying in "
            f"{self._start_retry_delay_ms / 1000:.1f}s "
            f"({self._remaining_start_retries} left)"
        )
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.record_button.setEnabled(False)
        self.start_retry_timer.start(self._start_retry_delay_ms)

    def _retry_start_after_failure(self) -> None:
        if self._manual_stop_requested or self._remaining_start_retries <= 0:
            self.start_button.setEnabled(True)
            self.stop_button.setEnabled(False)
            return
        self._remaining_start_retries -= 1
        self._last_start_error = None
        self._start_worker()

    def _update_preview(self, preview: QLabel) -> None:
        pixmap = self._last_pixmaps.get(preview)
        if pixmap is None:
            return
        preview.setPixmap(
            pixmap.scaled(
                preview.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        for preview in self._last_pixmaps:
            self._update_preview(preview)


class CameraPageBase(Page):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        route_key: str = "camera",
        compact_layout: bool = False,
        show_sync_mode_controls: bool = False,
        default_multi_camera_sync: bool = False,
    ):
        super().__init__(route_key, parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.show_sync_mode_controls = bool(show_sync_mode_controls)
        self.camera_panels = [
            CameraPanel("Orbbec Camera 1", 0, self),
            CameraPanel("Orbbec Camera 2", 1, self),
            CameraPanel("Orbbec Camera 3", 2, self),
        ]
        self.current_language = "Chinese"
        self._last_all_recording_dir: Path | None = None
        self._all_recording_started_at: float | None = None
        self._all_recording_elapsed_ms = 0
        self._loaded_record_dir: Path | None = None
        self._connect_pending_panels: list[CameraPanel] = []
        self._connect_sequence_active = False
        self._connect_sequence_delay_ms = 900
        self._compact_layout_enabled = False
        self._multi_camera_sync_enabled = bool(default_multi_camera_sync)
        self._convert_recordings_to_mp4 = False
        self._record_point_clouds_enabled = False
        self._sync_hardware_align_status: dict[int, tuple[bool, str]] = {}
        self._multi_camera_sync_worker: MultiCameraSyncWorker | None = None
        self._standalone_reset_worker: CameraStandaloneResetWorker | None = None
        self.trigger_sync_button: QtPushButton | None = None
        self.standalone_mode_button: QtPushButton | None = None
        self.convert_mp4_checkbox: QCheckBox | None = None
        self.point_cloud_checkbox: QCheckBox | None = None
        self.stop_cameras_button: PushButton | None = None
        self.output_dir_label: CaptionLabel | None = None
        self.choose_output_dir_button: PushButton | None = None
        self.connect_sequence_timer = QTimer(self)
        self.connect_sequence_timer.setSingleShot(True)
        self.connect_sequence_timer.timeout.connect(self._start_next_pending_camera)
        self.sync_preview_timer = QTimer(self)
        self.sync_preview_timer.setInterval(33)
        self.sync_preview_timer.timeout.connect(self._refresh_sync_frames)
        self.global_state_timer = QTimer(self)
        self.global_state_timer.setInterval(500)
        self.global_state_timer.timeout.connect(self._refresh_global_controls)
        self.all_recording_timer = QTimer(self)
        self.all_recording_timer.setInterval(200)
        self.all_recording_timer.timeout.connect(self._update_all_recording_time)
        self.recording_auto_stop_timer = QTimer(self)
        self.recording_auto_stop_timer.setSingleShot(True)
        self.recording_auto_stop_timer.timeout.connect(self._auto_stop_all_recording)

        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(28, 24, 28, 24)
        self.main_layout.setSpacing(16)

        self.header_card = self._build_header_card()
        self.main_layout.addWidget(self.header_card, 0)
        self.global_control_card = self._build_global_control_card()
        self.main_layout.addWidget(self.global_control_card, 0)

        self.scroll_area = QScrollArea(self)
        self.scroll_area.setObjectName("cameraScrollArea")
        self.scroll_area.viewport().setObjectName("cameraScrollViewport")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self.scroll_content = QWidget(self.scroll_area)
        self.scroll_content.setObjectName("cameraScrollContent")
        self.scroll_content.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.scroll_content_layout = QGridLayout(self.scroll_content)
        self.scroll_content_layout.setContentsMargins(0, 0, 0, 0)
        self.scroll_content_layout.setSpacing(18)
        self._arrange_camera_panels()

        self.scroll_area.setWidget(self.scroll_content)
        self.main_layout.addWidget(self.scroll_area, 1)
        if compact_layout:
            self.set_compact_layout(True)
        self.global_state_timer.start()
        self._apply_sync_mode_to_panels()
        self._set_mode_buttons(self._multi_camera_sync_enabled)
        self._refresh_global_controls()

    def _build_header_card(self) -> CardWidget:
        card = CardWidget(self)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(6)

        self.title_label = SubtitleLabel(self._tr("camera.title"), card)
        self.subtitle_label = BodyLabel(self._tr("camera.subtitle"), card)
        self.subtitle_label.setWordWrap(True)
        layout.addWidget(self.title_label)
        layout.addWidget(self.subtitle_label)
        return card

    def _build_global_control_card(self) -> CardWidget:
        card = CardWidget(self)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 16)
        layout.setSpacing(10)

        top_row = QHBoxLayout()
        top_row.setSpacing(14)
        layout.addLayout(top_row)

        self.global_controls_grid = QGridLayout()
        self.global_controls_grid.setHorizontalSpacing(10)
        self.global_controls_grid.setVerticalSpacing(8)
        top_row.addLayout(self.global_controls_grid, 1)

        self.recording_settings_widget = QWidget(card)
        self.recording_settings_widget.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Fixed,
        )
        self.recording_settings_grid = QGridLayout(self.recording_settings_widget)
        self.recording_settings_grid.setContentsMargins(0, 0, 0, 0)
        self.recording_settings_grid.setHorizontalSpacing(10)
        self.recording_settings_grid.setVerticalSpacing(6)
        top_row.addWidget(
            self.recording_settings_widget,
            0,
            Qt.AlignmentFlag.AlignVCenter,
        )

        self.all_recording_time_label = QLabel("ALL REC\n00:00:00", card)
        self.all_recording_time_label.setObjectName("allRecordingTimeLabel")
        self.all_recording_time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.all_recording_time_label.setMinimumWidth(220)
        self.all_recording_time_label.setMinimumHeight(72)
        self.all_recording_time_label.setStyleSheet(self._all_recording_timer_style())
        top_row.addWidget(self.all_recording_time_label, 0, Qt.AlignmentFlag.AlignRight)
        self._set_all_recording_timer_active(False)

        self.connect_all_button = PrimaryPushButton(self._tr("camera.connect_all"), card)
        self.connect_all_button.clicked.connect(self._connect_all_cameras)
        self.stop_cameras_button = PushButton("停止相机", card)
        self.stop_cameras_button.clicked.connect(self.stop_all_cameras)

        self.record_name_label = CaptionLabel("名称", card)
        self.record_name_edit = QLineEdit(card)
        self.record_name_edit.setText(DEFAULT_CAMERA_RECORDING_NAME)
        self.record_name_edit.setPlaceholderText(DEFAULT_CAMERA_RECORDING_NAME)
        self.record_name_edit.setClearButtonEnabled(True)
        self.record_name_edit.setFixedWidth(180)
        self.record_timestamp_checkbox = QCheckBox("加日期时间", card)
        self.record_timestamp_checkbox.setChecked(True)
        self.record_auto_stop_checkbox = QCheckBox("定时停止", card)
        self.record_auto_stop_checkbox.setChecked(False)
        self.record_auto_stop_checkbox.setMinimumHeight(34)
        self.record_auto_stop_checkbox.setStyleSheet(
            "QCheckBox { spacing: 8px; padding: 2px 0; }"
            "QCheckBox::indicator { width: 26px; height: 26px; }"
        )
        self.record_auto_stop_checkbox.stateChanged.connect(self._on_auto_stop_recording_changed)
        self.record_duration_label = CaptionLabel("时长", card)
        self.record_duration_spinbox = QSpinBox(card)
        self.record_duration_spinbox.setRange(1, 24 * 60 * 60)
        self.record_duration_spinbox.setSingleStep(10)
        self.record_duration_spinbox.setValue(120)
        self.record_duration_spinbox.setSuffix(" s")
        self.record_duration_spinbox.setMinimumWidth(84)
        self.record_duration_spinbox.setEnabled(False)
        self.record_duration_controls = QWidget(card)
        self.record_duration_controls_layout = QHBoxLayout(self.record_duration_controls)
        self.record_duration_controls_layout.setContentsMargins(0, 0, 0, 0)
        self.record_duration_controls_layout.setSpacing(8)
        self.record_duration_controls_layout.addWidget(self.record_auto_stop_checkbox, 0)
        self.record_duration_controls_layout.addWidget(self.record_duration_spinbox, 0)
        self.record_duration_controls_layout.addStretch(1)

        if self.show_sync_mode_controls:
            self.trigger_sync_button = QtPushButton("触发同步模式", card)
            self.trigger_sync_button.setCheckable(True)
            self.trigger_sync_button.setMinimumWidth(130)
            self.trigger_sync_button.setStyleSheet(self._mode_button_style())
            self.trigger_sync_button.clicked.connect(self._enable_trigger_sync_mode)

            self.standalone_mode_button = QtPushButton("非同步模式", card)
            self.standalone_mode_button.setCheckable(True)
            self.standalone_mode_button.setMinimumWidth(130)
            self.standalone_mode_button.setStyleSheet(self._mode_button_style())
            self.standalone_mode_button.clicked.connect(self._enable_standalone_mode)

        self.record_all_button = PushButton(self._tr("camera.record_all_start"), card)
        self.record_all_button.setStyleSheet(self._record_all_button_style())
        self.record_all_button.clicked.connect(self._toggle_all_recording)
        self.convert_mp4_checkbox = QCheckBox("录制后转存 RGB/Depth MP4", card)
        self.convert_mp4_checkbox.setChecked(False)
        self.convert_mp4_checkbox.stateChanged.connect(self._on_convert_mp4_changed)
        self.point_cloud_checkbox = QCheckBox("录制点云PLY", card)
        self.point_cloud_checkbox.setChecked(False)
        self.point_cloud_checkbox.stateChanged.connect(self._on_point_cloud_recording_changed)
        self.load_record_button = PushButton("加载Record", card)
        self.load_record_button.clicked.connect(self._choose_recording_folder)
        self.choose_output_dir_button = PushButton("数据保存目录", card)
        self.choose_output_dir_button.clicked.connect(self._choose_recording_output_dir)
        self.stop_playback_button = PushButton("停止播放", card)
        self.stop_playback_button.clicked.connect(self.stop_all_playback)
        self._arrange_global_control_widgets()

        status_row = QHBoxLayout()
        status_row.setSpacing(12)
        layout.addLayout(status_row)
        self.global_status_label = CaptionLabel(self._tr("camera.all_idle"), card)
        self.global_status_label.setWordWrap(True)
        status_row.addWidget(self.global_status_label, 1)
        self.output_dir_label = CaptionLabel("", card)
        self.output_dir_label.setWordWrap(False)
        self.output_dir_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        status_row.addWidget(self.output_dir_label, 1)
        self._refresh_recording_output_label()
        return card

    def set_language(self, language: str) -> None:
        self.current_language = normalize_language(language)
        self.title_label.setText(self._tr("camera.title"))
        self.subtitle_label.setText(self._tr("camera.subtitle"))
        self._refresh_global_controls()
        if hasattr(self, "global_status_label") and self.global_status_label.text() in (
            load_catalog("Chinese")["camera.all_idle"],
            load_catalog("English")["camera.all_idle"],
        ):
            self.global_status_label.setText(self._tr("camera.all_idle"))

    def set_compact_layout(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self._compact_layout_enabled == enabled:
            return
        self._compact_layout_enabled = enabled
        self.header_card.setVisible(not enabled)
        self.main_layout.setContentsMargins(
            10 if enabled else 28,
            8 if enabled else 24,
            10 if enabled else 28,
            8 if enabled else 24,
        )
        self.main_layout.setSpacing(8 if enabled else 16)
        self.scroll_content_layout.setSpacing(8 if enabled else 18)
        self._arrange_global_control_widgets()
        self._arrange_camera_panels()
        self._refresh_global_controls()

    def is_compact_layout(self) -> bool:
        return self._compact_layout_enabled

    def _arrange_global_control_widgets(self) -> None:
        _clear_layout_items(self.global_controls_grid)
        _clear_layout_items(self.recording_settings_grid)
        compact = self._compact_layout_enabled
        self.global_controls_grid.setHorizontalSpacing(8 if compact else 10)
        self.global_controls_grid.setVerticalSpacing(6 if compact else 8)
        self.recording_settings_grid.setHorizontalSpacing(8 if compact else 10)
        self.recording_settings_grid.setVerticalSpacing(4 if compact else 6)
        self.all_recording_time_label.setMinimumWidth(180 if compact else 220)
        self.all_recording_time_label.setMinimumHeight(62 if compact else 72)
        self.all_recording_time_label.setMaximumWidth(210 if compact else 280)
        if self.trigger_sync_button is not None:
            self.trigger_sync_button.setMinimumWidth(112 if compact else 130)
        if self.standalone_mode_button is not None:
            self.standalone_mode_button.setMinimumWidth(104 if compact else 130)
        self.record_name_label.setText("名" if compact else "名称")
        self.record_duration_label.setText("时" if compact else "时长")
        self.record_timestamp_checkbox.setText("日期" if compact else "加日期时间")
        self.record_auto_stop_checkbox.setText("定时" if compact else "定时停止")
        self.record_duration_controls_layout.setSpacing(6 if compact else 8)
        self.record_name_edit.setFixedWidth(180)
        self.record_duration_spinbox.setFixedWidth(66 if compact else 84)

        first_row = [
            self.connect_all_button,
            self.stop_cameras_button,
            self.record_all_button,
        ]
        if self.trigger_sync_button is not None:
            first_row.append(self.trigger_sync_button)
        if self.standalone_mode_button is not None:
            first_row.append(self.standalone_mode_button)

        second_row = [
            self.convert_mp4_checkbox,
            self.point_cloud_checkbox,
            self.load_record_button,
            self.choose_output_dir_button,
            self.stop_playback_button,
        ]
        rows = [first_row, second_row] if compact else [first_row + second_row]
        for row_index, widgets in enumerate(rows):
            for column, widget in enumerate(widget for widget in widgets if widget is not None):
                self.global_controls_grid.addWidget(widget, row_index, column)
        for column in range(12):
            self.global_controls_grid.setColumnStretch(column, 0)
        self.global_controls_grid.setColumnStretch(max(len(rows[0]), 1), 1)

        self.recording_settings_grid.addWidget(self.record_name_label, 0, 0)
        self.recording_settings_grid.addWidget(self.record_name_edit, 0, 1)
        self.recording_settings_grid.addWidget(
            self.record_timestamp_checkbox,
            0,
            2,
            alignment=Qt.AlignmentFlag.AlignLeft,
        )
        self.recording_settings_grid.addWidget(self.record_duration_label, 1, 0)
        self.recording_settings_grid.addWidget(
            self.record_duration_controls,
            1,
            1,
            1,
            2,
        )
        for column in range(8):
            self.recording_settings_grid.setColumnStretch(column, 0)

    def _arrange_camera_panels(self) -> None:
        _clear_layout_items(self.scroll_content_layout)
        compact = self._compact_layout_enabled
        for index in range(8):
            self.scroll_content_layout.setColumnStretch(index, 0)
            self.scroll_content_layout.setRowStretch(index, 0)
        for panel in self.camera_panels:
            panel.set_compact_layout(compact)
        if compact:
            for index, panel in enumerate(self.camera_panels):
                self.scroll_content_layout.addWidget(panel, 0, index)
                self.scroll_content_layout.setColumnStretch(index, 1)
            self.scroll_content_layout.setRowStretch(1, 1)
            return

        for index, panel in enumerate(self.camera_panels):
            self.scroll_content_layout.addWidget(panel, index, 0)
            self.scroll_content_layout.setRowStretch(index, 0)
        self.scroll_content_layout.setColumnStretch(0, 1)
        self.scroll_content_layout.setRowStretch(len(self.camera_panels), 1)

    def _on_convert_mp4_changed(self, state: int) -> None:
        self.set_recording_mp4_conversion_enabled(state != 0)

    def _on_point_cloud_recording_changed(self, state: int) -> None:
        self.set_point_cloud_recording_enabled(state != 0)

    def _on_auto_stop_recording_changed(self, state: int) -> None:
        self.record_duration_spinbox.setEnabled(state != 0)

    def set_recording_mp4_conversion_enabled(self, enabled: bool) -> None:
        self._convert_recordings_to_mp4 = bool(enabled)
        if self.convert_mp4_checkbox is not None and self.convert_mp4_checkbox.isChecked() != self._convert_recordings_to_mp4:
            self.convert_mp4_checkbox.blockSignals(True)
            self.convert_mp4_checkbox.setChecked(self._convert_recordings_to_mp4)
            self.convert_mp4_checkbox.blockSignals(False)
        for panel in self.camera_panels:
            panel.set_recording_mp4_conversion_enabled(self._convert_recordings_to_mp4)

    def set_point_cloud_recording_enabled(self, enabled: bool) -> None:
        self._record_point_clouds_enabled = bool(enabled)
        if (
            self.point_cloud_checkbox is not None
            and self.point_cloud_checkbox.isChecked() != self._record_point_clouds_enabled
        ):
            self.point_cloud_checkbox.blockSignals(True)
            self.point_cloud_checkbox.setChecked(self._record_point_clouds_enabled)
            self.point_cloud_checkbox.blockSignals(False)
        for panel in self.camera_panels:
            panel.set_point_cloud_recording_enabled(self._record_point_clouds_enabled)
        if self._multi_camera_sync_worker is not None and self._multi_camera_sync_worker.isRunning():
            self._multi_camera_sync_worker.set_point_cloud_recording_enabled(
                self._record_point_clouds_enabled
            )

    def _tr(self, key: str, **kwargs: object) -> str:
        text = load_catalog(self.current_language)[key]
        return text.format(**kwargs) if kwargs else text

    def _connect_all_cameras(self) -> None:
        if self._multi_camera_sync_enabled:
            self._connect_sync_cameras()
            return
        if self._connect_sequence_active:
            return
        self._connect_pending_panels = [
            panel for panel in self.camera_panels if not panel.is_running()
        ]
        if not self._connect_pending_panels:
            self.global_status_label.setText(self._tr("camera.all_connected"))
            self._refresh_global_controls()
            return

        self._connect_sequence_active = True
        self.global_status_label.setText(
            f"错峰连接相机中，共 {len(self._connect_pending_panels)} 路..."
        )
        self._refresh_global_controls()
        self._start_next_pending_camera()

    def _start_next_pending_camera(self) -> None:
        while self._connect_pending_panels:
            panel = self._connect_pending_panels.pop(0)
            if panel.is_running():
                continue
            panel.start()
            remaining = len(self._connect_pending_panels)
            self.global_status_label.setText(
                f"正在连接 {panel.title}，剩余 {remaining} 路..."
            )
            self._refresh_global_controls()
            if remaining:
                self.connect_sequence_timer.start(self._connect_sequence_delay_ms)
            else:
                self._connect_sequence_active = False
                self.global_status_label.setText(self._tr("camera.all_starting"))
                self._refresh_global_controls()
            return

        self._connect_sequence_active = False
        self.global_status_label.setText(self._tr("camera.all_connected"))
        self._refresh_global_controls()

    def _connect_sync_cameras(self) -> None:
        if self._multi_camera_sync_worker is not None and self._multi_camera_sync_worker.isRunning():
            return
        for panel in self.camera_panels:
            if panel.is_running():
                self.global_status_label.setText("请先停止单相机模式，再启动同步模式。")
                return

        worker = MultiCameraSyncWorker(
            camera_count=min(3, len(self.camera_panels)),
            sync_config_path=DEFAULT_SYNC_CONFIG_PATH,
            parent=self,
        )
        worker.set_depth_mode(self.camera_panels[0]._depth_mode if self.camera_panels else "2d")
        worker.status_changed.connect(self._on_sync_status)
        worker.device_info_changed.connect(self._on_sync_device_info)
        worker.align_status_changed.connect(self._on_sync_align_status)
        worker.stream_error.connect(self._on_sync_error)
        worker.stream_started.connect(self._on_sync_started)
        worker.stream_stopped.connect(self._on_sync_stopped)
        worker.recording_started.connect(self._on_sync_recording_started)
        worker.recording_stopped.connect(self._on_sync_recording_stopped)
        worker.recording_segment_saved.connect(self._on_sync_recording_segment_saved)
        worker.recording_error.connect(self._on_sync_recording_error)
        worker.finished.connect(lambda worker=worker: self._on_sync_worker_finished(worker))
        self._multi_camera_sync_worker = worker
        self._sync_hardware_align_status.clear()
        for panel in self.camera_panels:
            panel.set_sync_status("Starting sync camera stream...")
            panel.set_sync_mode_controls_enabled(False)
        self.global_status_label.setText("正在启动 Orbbec 同步相机...")
        self._refresh_global_controls()
        worker.start()

    def _refresh_sync_frames(self) -> None:
        worker = self._multi_camera_sync_worker
        if worker is None:
            return
        for index, (color_image, depth_image) in worker.take_latest_frames().items():
            if 0 <= index < len(self.camera_panels):
                self.camera_panels[index].apply_sync_preview_frame(color_image, depth_image)

    def _on_sync_status(self, index: int, message: str) -> None:
        if 0 <= index < len(self.camera_panels):
            self.camera_panels[index].set_sync_status(message)
        else:
            self.global_status_label.setText(message)

    def _on_sync_device_info(self, index: int, name: str, serial: str, connection: str) -> None:
        if 0 <= index < len(self.camera_panels):
            self.camera_panels[index].set_device_info(name, serial, connection)

    def _on_sync_align_status(self, index: int, enabled: bool, status: str) -> None:
        self._sync_hardware_align_status[int(index)] = (bool(enabled), str(status))

    def _on_sync_error(self, index: int, message: str) -> None:
        if 0 <= index < len(self.camera_panels):
            self.camera_panels[index].set_sync_error(message)
        else:
            self.global_status_label.setText(f"同步相机错误: {message}")

    def _on_sync_started(self) -> None:
        active_count = self._sync_active_camera_count()
        for index, panel in enumerate(self.camera_panels):
            if index < active_count:
                panel.set_sync_stream_started()
            else:
                panel.set_sync_stream_stopped()
                panel.set_sync_mode_controls_enabled(False)
                panel.set_sync_status(f"Camera {index + 1}: 未检测到，不参与本次同步。")
        self.sync_preview_timer.start()
        self.global_status_label.setText(
            f"{active_count} 路 Orbbec 同步相机已连接。"
            f"{self._sync_hardware_align_summary(active_count)}"
        )
        self._refresh_global_controls()

    def _on_sync_stopped(self, index: int) -> None:
        if 0 <= index < len(self.camera_panels):
            self.camera_panels[index].set_sync_stream_stopped()

    def _on_sync_worker_finished(self, worker: MultiCameraSyncWorker) -> None:
        self.sync_preview_timer.stop()
        if self._multi_camera_sync_worker is worker:
            self._multi_camera_sync_worker = None
        for panel in self.camera_panels:
            panel.set_sync_stream_stopped()
            panel.set_sync_mode_controls_enabled(not self._multi_camera_sync_enabled)
        self.global_status_label.setText("同步相机已停止。")
        worker.deleteLater()
        self._refresh_global_controls()

    def _sync_hardware_align_summary(self, active_count: int) -> str:
        if active_count <= 0:
            return "硬件Align: 未连接。"
        statuses = {}
        worker = self._multi_camera_sync_worker
        if worker is not None:
            statuses.update(worker.hardware_align_status)
        statuses.update(self._sync_hardware_align_status)
        success_count = sum(
            1
            for index in range(active_count)
            if statuses.get(index, (False, ""))[0]
        )
        if success_count == active_count:
            return f"硬件Align: {success_count}/{active_count} 成功。"
        fallback_count = active_count - success_count
        return (
            f"硬件Align: {success_count}/{active_count} 成功，"
            f"{fallback_count} 路使用原始Depth。"
        )

    def _on_sync_recording_started(self, index: int, path: str) -> None:
        if 0 <= index < len(self.camera_panels):
            self.camera_panels[index].on_sync_recording_started(path)

    def _on_sync_recording_stopped(self, index: int, path: str) -> None:
        if 0 <= index < len(self.camera_panels):
            self.camera_panels[index].on_sync_recording_stopped(path)

    def _on_sync_recording_segment_saved(self, index: int, path: str) -> None:
        if 0 <= index < len(self.camera_panels):
            self.camera_panels[index].on_sync_recording_segment_saved(path)

    def _on_sync_recording_error(self, index: int, message: str) -> None:
        if 0 <= index < len(self.camera_panels):
            self.camera_panels[index]._on_recording_error(message)

    def _toggle_all_recording(self) -> None:
        if any(panel.is_recording_or_pending() for panel in self.camera_panels):
            self._stop_all_recording()
            return
        self._start_all_recording()

    def connect_all_cameras(self) -> None:
        self._connect_all_cameras()

    def stop_all_cameras(self) -> None:
        self.connect_sequence_timer.stop()
        self._connect_pending_panels.clear()
        self._connect_sequence_active = False

        stopped_any = False
        if self._multi_camera_sync_worker is not None and self._multi_camera_sync_worker.isRunning():
            self._multi_camera_sync_worker.requestInterruption()
            self.sync_preview_timer.stop()
            for panel in self.camera_panels:
                panel.set_sync_status("Stopping sync camera stream...")
                panel.record_button.setEnabled(False)
            self.global_status_label.setText("正在停止同步相机...")
            stopped_any = True

        for panel in self.camera_panels:
            if panel.is_running():
                panel.stop()
                stopped_any = True

        if not stopped_any:
            self.global_status_label.setText("没有正在运行的相机。")
        self._refresh_global_controls()

    def _enable_trigger_sync_mode(self) -> None:
        self.set_multi_camera_sync_enabled(True)

    def _enable_standalone_mode(self) -> None:
        changed = self.set_multi_camera_sync_enabled(False)
        if changed or not self._multi_camera_sync_enabled:
            self.reset_cameras_to_standalone()

    def set_multi_camera_sync_enabled(self, enabled: bool) -> bool:
        enabled = bool(enabled)
        if self._multi_camera_sync_enabled == enabled:
            self._apply_sync_mode_to_panels()
            self._set_mode_buttons(enabled)
            return False
        if self.is_any_camera_running():
            self.global_status_label.setText("请先停止相机，再切换同步模式。")
            self._set_mode_buttons(self._multi_camera_sync_enabled)
            return False
        self._multi_camera_sync_enabled = enabled
        self._apply_sync_mode_to_panels()
        self._set_mode_buttons(enabled)
        self.global_status_label.setText("多相机同步模式" if enabled else "非同步模式")
        self._refresh_global_controls()
        return True

    def is_multi_camera_sync_enabled(self) -> bool:
        return self._multi_camera_sync_enabled

    def _apply_sync_mode_to_panels(self) -> None:
        controls_enabled = not self._multi_camera_sync_enabled
        for panel in self.camera_panels:
            panel.set_sync_mode_controls_enabled(controls_enabled)

    def _set_mode_buttons(self, trigger_sync: bool) -> None:
        if self.trigger_sync_button is None or self.standalone_mode_button is None:
            return
        self.trigger_sync_button.setChecked(trigger_sync)
        self.standalone_mode_button.setChecked(not trigger_sync)
        set_button_category(self.trigger_sync_button, "success" if trigger_sync else "primary")
        set_button_category(self.standalone_mode_button, "success" if not trigger_sync else "primary")

    @staticmethod
    def _mode_button_style() -> str:
        return """
        QPushButton {
            background: #FFFFFF;
            border: 1px solid #D8DEE8;
            color: #344054;
            border-radius: 6px;
            min-height: 30px;
            min-width: 78px;
            padding: 5px 12px;
            font-weight: 500;
        }
        QPushButton:hover {
            background: #F8FAFC;
            border-color: #B9C4D4;
        }
        QPushButton:pressed {
            background: #EEF2F7;
        }
        QPushButton[category="primary"] {
            background: #3B82F6;
            border-color: #3B82F6;
            color: #FFFFFF;
        }
        QPushButton[category="primary"]:hover {
            background: #60A5FA;
            border-color: #60A5FA;
        }
        QPushButton[category="primary"]:pressed {
            background: #2563EB;
            border-color: #2563EB;
        }
        QPushButton[category="success"] {
            background: #ECFDF3;
            border-color: #A6F4C5;
            color: #067647;
        }
        QPushButton[category="success"]:hover {
            background: #ECFDF3;
            border-color: #A6F4C5;
        }
        QPushButton[category="success"]:pressed {
            background: #D1FADF;
            border-color: #A6F4C5;
        }
        QPushButton:checked {
            font-weight: 600;
        }
        QPushButton:disabled {
            background: #F3F4F6;
            border-color: #E5E7EB;
            color: #9CA3AF;
        }
        """

    @staticmethod
    def _record_all_button_style() -> str:
        return """
        QPushButton[recordingState="idle"] {
            background: #2563EB;
            border: 1px solid #2563EB;
            color: #FFFFFF;
            border-radius: 6px;
            min-height: 30px;
            padding: 5px 14px;
            font-weight: 600;
        }
        QPushButton[recordingState="idle"]:hover {
            background: #1D4ED8;
            border-color: #1D4ED8;
            color: #FFFFFF;
        }
        QPushButton[recordingState="idle"]:pressed {
            background: #1E40AF;
            border-color: #1E40AF;
            color: #FFFFFF;
        }
        QPushButton[recordingState="recording"] {
            background: #DC2626;
            border: 1px solid #DC2626;
            color: #FFFFFF;
            border-radius: 6px;
            min-height: 30px;
            padding: 5px 14px;
            font-weight: 700;
        }
        QPushButton[recordingState="recording"]:hover {
            background: #B91C1C;
            border-color: #B91C1C;
            color: #FFFFFF;
        }
        QPushButton[recordingState="recording"]:pressed {
            background: #991B1B;
            border-color: #991B1B;
            color: #FFFFFF;
        }
        QPushButton:disabled {
            background: #E5E7EB;
            border-color: #D1D5DB;
            color: #9CA3AF;
        }
        """

    @staticmethod
    def _all_recording_timer_style() -> str:
        return """
        QLabel#allRecordingTimeLabel {
            background: #F8FAFC;
            border: 2px solid #CBD5E1;
            border-radius: 8px;
            color: #475569;
            font-size: 26px;
            font-weight: 800;
            letter-spacing: 0px;
            padding: 4px 14px;
        }
        QLabel#allRecordingTimeLabel[recordingState="recording"] {
            background: #FEF2F2;
            border-color: #DC2626;
            color: #B91C1C;
        }
        QLabel#allRecordingTimeLabel[recordingState="recorded"] {
            background: #F0FDF4;
            border-color: #16A34A;
            color: #15803D;
        }
        """

    def _set_record_all_button_recording(self, recording: bool) -> None:
        state = "recording" if recording else "idle"
        if self.record_all_button.property("recordingState") == state:
            return
        self.record_all_button.setProperty("recordingState", state)
        self.record_all_button.style().unpolish(self.record_all_button)
        self.record_all_button.style().polish(self.record_all_button)
        self.record_all_button.update()

    def _set_all_recording_timer_active(self, active: bool, *, recorded: bool = False) -> None:
        state = "recording" if active else ("recorded" if recorded else "idle")
        if self.all_recording_time_label.property("recordingState") == state:
            return
        self.all_recording_time_label.setProperty("recordingState", state)
        self.all_recording_time_label.style().unpolish(self.all_recording_time_label)
        self.all_recording_time_label.style().polish(self.all_recording_time_label)
        self.all_recording_time_label.update()

    def _start_all_recording_timer(self) -> None:
        self._all_recording_started_at = time.monotonic()
        self._all_recording_elapsed_ms = 0
        self._set_all_recording_timer_active(True)
        self._update_all_recording_time()
        self.all_recording_timer.start()
        self._start_recording_auto_stop_timer()

    def _finish_all_recording_timer(self) -> None:
        if self._all_recording_started_at is not None:
            self._capture_all_recording_elapsed()
        self._all_recording_started_at = None
        self.all_recording_timer.stop()
        self.recording_auto_stop_timer.stop()
        self.all_recording_time_label.setText(
            f"RECORDED\n{self._format_all_elapsed(self._all_recording_elapsed_ms)}"
        )
        self._set_all_recording_timer_active(False, recorded=self._all_recording_elapsed_ms > 0)

    def _recording_session_prefix(self) -> str:
        return sanitize_recording_session_name(
            self.record_name_edit.text(),
            default=DEFAULT_CAMERA_RECORDING_NAME,
        )

    def _recording_session_add_timestamp(self) -> bool:
        return self.record_timestamp_checkbox.isChecked()

    def _recording_auto_stop_seconds(self) -> int:
        if not self.record_auto_stop_checkbox.isChecked():
            return 0
        return int(self.record_duration_spinbox.value())

    def _start_recording_auto_stop_timer(self) -> None:
        self.recording_auto_stop_timer.stop()
        duration_seconds = self._recording_auto_stop_seconds()
        if duration_seconds > 0:
            self.recording_auto_stop_timer.start(duration_seconds * 1000)

    def _auto_stop_all_recording(self) -> None:
        if not self.is_any_recording_or_pending():
            return
        self.global_status_label.setText("录制时长已到，正在自动停止录制...")
        self._stop_all_recording()

    def _capture_all_recording_elapsed(self) -> None:
        if self._all_recording_started_at is not None:
            self._all_recording_elapsed_ms = int(
                (time.monotonic() - self._all_recording_started_at) * 1000
            )

    def _update_all_recording_time(self) -> None:
        self._capture_all_recording_elapsed()
        label = "ALL REC" if self._all_recording_started_at is not None else "RECORDED"
        self.all_recording_time_label.setText(
            f"{label}\n{self._format_all_elapsed(self._all_recording_elapsed_ms)}"
        )

    @staticmethod
    def _format_all_elapsed(elapsed_ms: int) -> str:
        total_seconds = max(0, elapsed_ms // 1000)
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def reset_cameras_to_standalone(self) -> None:
        if self.is_any_camera_running():
            self.global_status_label.setText("请先停止相机，再切换 STANDALONE。")
            return
        if self._standalone_reset_worker is not None and self._standalone_reset_worker.isRunning():
            return
        self.global_status_label.setText("正在将 Orbbec 相机切回 STANDALONE...")
        worker = CameraStandaloneResetWorker(self)
        self._standalone_reset_worker = worker
        worker.finished_status.connect(self.global_status_label.setText)
        worker.reset_error.connect(lambda message: self.global_status_label.setText(f"STANDALONE 设置失败: {message}"))
        worker.finished.connect(lambda: self._on_standalone_reset_finished(worker))
        worker.start()

    def _on_standalone_reset_finished(self, worker: CameraStandaloneResetWorker) -> None:
        if self._standalone_reset_worker is worker:
            self._standalone_reset_worker = None
        worker.deleteLater()
        self._refresh_global_controls()

    def start_all_recording_to_dir(self, session_dir: Path) -> int:
        if self._multi_camera_sync_worker is not None and self._multi_camera_sync_worker.isRunning():
            return self._start_sync_recording_to_dir(session_dir)
        running_panels = [panel for panel in self.camera_panels if panel.is_running()]
        if not running_panels:
            self.global_status_label.setText(self._tr("camera.all_record_need_camera"))
            self._refresh_global_controls()
            return 0

        try:
            session_dir = Path(session_dir).expanduser().resolve()
            session_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.global_status_label.setText(self._tr("camera.all_record_error", message=exc))
            self._refresh_global_controls()
            return 0

        started = 0
        for panel in running_panels:
            bag_path = session_dir / f"camera_{panel.device_index + 1}_rgb_depth.bag"
            if panel.start_recording_to_bag(bag_path):
                started += 1

        if started:
            self._last_all_recording_dir = session_dir
            self._start_all_recording_timer()
            self.global_status_label.setText(self._tr("camera.all_recording", path=session_dir))
        else:
            self.global_status_label.setText(self._tr("camera.all_record_none_ready"))
        self._refresh_global_controls()
        return started

    def _start_sync_recording_to_dir(self, session_dir: Path) -> int:
        worker = self._multi_camera_sync_worker
        if worker is None or not worker.isRunning():
            self.global_status_label.setText(self._tr("camera.all_record_need_camera"))
            self._refresh_global_controls()
            return 0
        try:
            session_dir = Path(session_dir).expanduser().resolve()
            session_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.global_status_label.setText(self._tr("camera.all_record_error", message=exc))
            self._refresh_global_controls()
            return 0

        paths = {
            index: session_dir / f"camera_{index + 1}_rgb_depth.bag"
            for index in range(self._sync_active_camera_count())
        }
        if not paths:
            self.global_status_label.setText(self._tr("camera.all_record_need_camera"))
            self._refresh_global_controls()
            return 0
        for index, panel in enumerate(self.camera_panels):
            if index in paths:
                panel._recording_pending = True
                panel.record_button.setEnabled(False)
                panel.recording_time_label.setText("Recording: starting...")
            else:
                panel._recording_pending = False
                panel.record_button.setEnabled(False)
        worker.set_point_cloud_recording_enabled(self._record_point_clouds_enabled)
        worker.request_start_recording(paths)
        self._last_all_recording_dir = session_dir
        self._start_all_recording_timer()
        self.global_status_label.setText(self._tr("camera.all_recording", path=session_dir))
        self._refresh_global_controls()
        return len(paths)

    def _sync_active_camera_count(self) -> int:
        worker = self._multi_camera_sync_worker
        if worker is None:
            return 0
        active_count = worker.active_camera_count
        return max(0, min(active_count, len(self.camera_panels)))

    def stop_all_recording(self) -> None:
        self._stop_all_recording()

    def is_any_camera_running(self) -> bool:
        return (
            any(panel.is_running() for panel in self.camera_panels)
            or (
                self._multi_camera_sync_worker is not None
                and self._multi_camera_sync_worker.isRunning()
            )
        )

    def is_any_recording_or_pending(self) -> bool:
        return any(panel.is_recording_or_pending() for panel in self.camera_panels)

    def _start_all_recording(self) -> None:
        try:
            session_dir = create_camera_recording_session_dir(
                self.camera_panels[0].recording_base_dir,
                prefix=self._recording_session_prefix(),
                add_timestamp=self._recording_session_add_timestamp(),
            )
        except OSError as exc:
            self.global_status_label.setText(self._tr("camera.all_record_error", message=exc))
            return

        self.start_all_recording_to_dir(session_dir)

    def _stop_all_recording(self) -> None:
        self.recording_auto_stop_timer.stop()
        if self._multi_camera_sync_worker is not None and self._multi_camera_sync_worker.isRunning():
            self._multi_camera_sync_worker.request_stop_recording()
            if self._last_all_recording_dir is not None:
                self.global_status_label.setText(
                    self._tr("camera.all_stopping_path", path=self._last_all_recording_dir)
                )
            else:
                self.global_status_label.setText(self._tr("camera.all_stopping"))
            self._refresh_global_controls()
            return
        stopped = 0
        for panel in self.camera_panels:
            if panel.is_recording_or_pending():
                panel.stop_recording()
                stopped += 1
        if stopped:
            if self._last_all_recording_dir is not None:
                self.global_status_label.setText(
                    self._tr("camera.all_stopping_path", path=self._last_all_recording_dir)
                )
            else:
                self.global_status_label.setText(self._tr("camera.all_stopping"))
        else:
            self.global_status_label.setText(self._tr("camera.all_no_recording"))
            self._finish_all_recording_timer()
        self._refresh_global_controls()

    def _refresh_global_controls(self) -> None:
        standalone_running = any(panel.is_running() for panel in self.camera_panels)
        sync_running = self._multi_camera_sync_worker is not None and self._multi_camera_sync_worker.isRunning()
        sync_ready = sync_running and self._sync_active_camera_count() > 0
        any_running = standalone_running or sync_running
        all_running = all(panel.is_running() for panel in self.camera_panels)
        if sync_running:
            all_running = True
        any_recording = any(panel.is_recording_or_pending() for panel in self.camera_panels)
        if self._all_recording_started_at is not None and not any_recording:
            self._finish_all_recording_timer()
        for panel in self.camera_panels:
            panel.set_recording_mp4_conversion_enabled(self._convert_recordings_to_mp4)
        self.connect_all_button.setEnabled(not all_running and not self._connect_sequence_active)
        self.record_all_button.setEnabled(any_running and not self._connect_sequence_active)
        self.connect_all_button.setText(
            "连接中..." if self._connect_sequence_active else self._tr("camera.connect_all")
        )
        if self.stop_cameras_button is not None:
            self.stop_cameras_button.setEnabled(any_running or self._connect_sequence_active)
        mode_controls_enabled = not any_running and not any_recording and not self._connect_sequence_active
        if self.trigger_sync_button is not None and self.standalone_mode_button is not None:
            self.trigger_sync_button.setEnabled(mode_controls_enabled)
            self.standalone_mode_button.setEnabled(mode_controls_enabled)
            self._set_mode_buttons(self._multi_camera_sync_enabled)
        self.record_all_button.setText(
            self._tr("camera.record_all_stop") if any_recording else self._tr("camera.record_all_start")
        )
        record_ready = standalone_running or sync_ready
        self.record_all_button.setEnabled(record_ready and not self._connect_sequence_active)
        self._set_record_all_button_recording(any_recording)
        self.load_record_button.setEnabled(not any_recording)
        if self.convert_mp4_checkbox is not None:
            self.convert_mp4_checkbox.setEnabled(not any_recording)
        if self.point_cloud_checkbox is not None:
            self.point_cloud_checkbox.setEnabled(not any_recording)
        self.record_name_edit.setEnabled(not any_recording)
        self.record_timestamp_checkbox.setEnabled(not any_recording)
        self.record_auto_stop_checkbox.setEnabled(not any_recording)
        self.record_duration_spinbox.setEnabled(
            not any_recording and self.record_auto_stop_checkbox.isChecked()
        )
        self.stop_playback_button.setEnabled(any(panel.is_playing_back() for panel in self.camera_panels))

    def shutdown(self) -> bool:
        self.connect_sequence_timer.stop()
        self.sync_preview_timer.stop()
        self.recording_auto_stop_timer.stop()
        self._connect_pending_panels.clear()
        self._connect_sequence_active = False
        stopped = True
        if self._multi_camera_sync_worker is not None and self._multi_camera_sync_worker.isRunning():
            stopped = _shutdown_thread(
                self._multi_camera_sync_worker,
                wait_ms=5000,
                force_wait_ms=8000,
            ) and stopped
        if self._standalone_reset_worker is not None and self._standalone_reset_worker.isRunning():
            stopped = _shutdown_thread(
                self._standalone_reset_worker,
                wait_ms=2000,
                force_wait_ms=3000,
            ) and stopped
        self.global_state_timer.stop()
        self.all_recording_timer.stop()
        for panel in self.camera_panels:
            stopped = panel.shutdown() and stopped
        return stopped

    def set_recording_output_dir(self, path: Path) -> None:
        for panel in self.camera_panels:
            panel.set_recording_output_dir(path)
        self._refresh_recording_output_label()

    def _refresh_recording_output_label(self) -> None:
        if self.output_dir_label is None or not self.camera_panels:
            return
        self.output_dir_label.setText(f"数据保存目录: {self.camera_panels[0].recording_base_dir}")

    def _choose_recording_output_dir(self) -> None:
        start_dir = self.camera_panels[0].recording_base_dir
        selected = QFileDialog.getExistingDirectory(
            self,
            "选择数据保存目录",
            str(start_dir),
        )
        if not selected:
            return
        try:
            output_dir = set_recording_output_dir(Path(selected))
        except (OSError, ValueError) as exc:
            self.global_status_label.setText(f"保存目录设置失败: {exc}")
            return
        self.set_recording_output_dir(output_dir)
        self.global_status_label.setText(f"数据保存目录已更新: {output_dir}")
        self._refresh_global_controls()

    def stop_all_playback(self) -> None:
        for panel in self.camera_panels:
            panel.stop_playback()
        self.global_status_label.setText("Record播放已停止。")
        self._refresh_global_controls()

    def _choose_recording_folder(self) -> None:
        start_dir = self._loaded_record_dir or self.camera_panels[0].recording_base_dir
        selected = QFileDialog.getExistingDirectory(
            self,
            "选择Record文件夹",
            str(start_dir),
        )
        if selected:
            self.load_recording_folder(Path(selected))

    def load_recording_folder(self, record_dir: Path) -> int:
        record_dir = Path(record_dir).expanduser().resolve()
        if not record_dir.is_dir():
            self.global_status_label.setText(f"Record文件夹不存在: {record_dir}")
            return 0
        if self.is_any_recording_or_pending():
            self.global_status_label.setText("请先停止相机采集，再加载Record。")
            return 0

        self.stop_all_playback()
        loaded = 0
        missing: list[str] = []
        for panel in self.camera_panels:
            color_path, depth_path = self._find_playback_videos(record_dir, panel.device_index + 1)
            if panel.start_playback(color_path, depth_path, record_dir):
                loaded += 1
            else:
                missing.append(f"Camera {panel.device_index + 1}")

        self._loaded_record_dir = record_dir
        if loaded:
            detail = f"，未找到: {', '.join(missing)}" if missing else ""
            self.global_status_label.setText(
                f"正在播放Record: {record_dir.name}（{loaded}路相机）{detail}"
            )
        else:
            self.global_status_label.setText(f"未找到可播放的rgb/depth视频: {record_dir}")
        self._refresh_global_controls()
        return loaded

    @staticmethod
    def _first_existing_path(candidates: list[Path]) -> Path | None:
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    @classmethod
    def _find_playback_videos(cls, record_dir: Path, camera_number: int) -> tuple[Path | None, Path | None]:
        camera_dirs = [
            record_dir / f"camera_{camera_number}",
            record_dir / f"camera{camera_number}",
            record_dir / f"camera-{camera_number}",
            record_dir / f"orbbec_{camera_number}",
        ]
        rgb_candidates: list[Path] = []
        depth_candidates: list[Path] = []
        for camera_dir in camera_dirs:
            rgb_candidates.extend(
                [
                    camera_dir / "rgb.mp4",
                    camera_dir / "color.mp4",
                    camera_dir / "rgb_depth.mp4",
                ]
            )
            depth_candidates.extend(
                [
                    camera_dir / "depth.mp4",
                    camera_dir / "depth_color.mp4",
                    camera_dir / "rgb_depth_depth.mp4",
                ]
            )

        rgb_candidates.extend(
            [
                record_dir / f"camera_{camera_number}_rgb.mp4",
                record_dir / f"camera_{camera_number}_color.mp4",
                record_dir / f"camera_{camera_number}_rgb_depth.mp4",
                record_dir / f"camera{camera_number}_rgb.mp4",
                record_dir / f"camera{camera_number}_color.mp4",
            ]
        )
        depth_candidates.extend(
            [
                record_dir / f"camera_{camera_number}_depth.mp4",
                record_dir / f"camera_{camera_number}_depth_color.mp4",
                record_dir / f"camera_{camera_number}_rgb_depth_depth.mp4",
                record_dir / f"camera{camera_number}_depth.mp4",
            ]
        )

        if camera_number == 1:
            rgb_candidates.extend(
                [
                    record_dir / "rgb.mp4",
                    record_dir / "color.mp4",
                    record_dir / "rgb_depth.mp4",
                ]
            )
            depth_candidates.extend(
                [
                    record_dir / "depth.mp4",
                    record_dir / "depth_color.mp4",
                    record_dir / "rgb_depth_depth.mp4",
                ]
            )

        return (
            cls._first_existing_path(rgb_candidates),
            cls._first_existing_path(depth_candidates),
        )
