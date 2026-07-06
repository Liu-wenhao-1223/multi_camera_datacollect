from __future__ import annotations

import csv
import gc
import json
import queue
import time
from datetime import datetime
from pathlib import Path


MIN_DEPTH_MM = 20
MAX_DEPTH_MM = 5000
RECORDING_SEGMENT_SECONDS = 120.0
RECORDING_SEGMENT_ROTATE_GRACE_SECONDS = 1.0
ENABLE_HARDWARE_D2C_ALIGN = False


class SegmentedBagRecorder:
    def __init__(
        self,
        *,
        record_device_cls,
        pipeline,
        device,
        camera_index: int,
        stream_mode: str,
        depth_mode: str,
        hardware_align_enabled: bool = False,
        hardware_align_status: str = "",
        point_cloud_enabled: bool = False,
        status_callback=None,
    ):
        self._record_device_cls = record_device_cls
        self._pipeline = pipeline
        self._device = device
        self._camera_index = int(camera_index)
        self._stream_mode = str(stream_mode)
        self._depth_mode = str(depth_mode)
        self._hardware_align_enabled = bool(hardware_align_enabled)
        self._hardware_align_status = str(hardware_align_status)
        self._point_cloud_enabled = bool(point_cloud_enabled)
        self._status_callback = status_callback
        self._recorder = None
        self._point_cloud_recorder = None
        self._base_path: Path | None = None
        self._active_path: Path | None = None
        self._segment_index = 1
        self._has_rotated = False
        self._segment_started_at: float | None = None

    @property
    def active_path(self) -> Path | None:
        return self._active_path

    @property
    def is_active(self) -> bool:
        return self._recorder is not None

    def start(self, base_path: Path) -> Path:
        self._base_path = Path(base_path)
        self._segment_index = 1
        self._has_rotated = False
        self._start_segment(self._base_path)
        return self._base_path

    def close(self) -> Path | None:
        closed_path = self._close_active_segment()
        self._reset()
        return closed_path

    def record_point_cloud(self, frames, color_frame, depth_frame) -> None:
        if self._point_cloud_recorder is None:
            return
        try:
            self._point_cloud_recorder.record(frames, color_frame, depth_frame)
        except Exception as exc:
            self._point_cloud_recorder.close()
            self._point_cloud_recorder = None
            self._emit_status(f"point cloud recording stopped ({exc})")

    def rotate_if_due(self) -> Path | None:
        if self._recorder is None or self._base_path is None or self._active_path is None:
            return None
        if self._segment_started_at is None:
            return None
        elapsed = time.monotonic() - self._segment_started_at
        rotate_after = RECORDING_SEGMENT_SECONDS + RECORDING_SEGMENT_ROTATE_GRACE_SECONDS
        if elapsed < rotate_after:
            return None

        closed_path = self._close_active_segment()
        if closed_path is None:
            return None

        if not self._has_rotated:
            target_path = recording_segment_path(self._base_path, 1)
            closed_path = _move_first_segment_recording(self._base_path, target_path)
            _write_segment_metadata(
                target_path,
                self._base_path,
                self._segment_index,
                self._pipeline,
                self._device,
                self._camera_index,
                self._stream_mode,
                self._depth_mode,
                self._hardware_align_enabled,
                self._hardware_align_status,
            )
            self._has_rotated = True

        self._segment_index += 1
        next_path = recording_segment_path(self._base_path, self._segment_index)
        try:
            self._start_segment(next_path)
        except Exception:
            self._reset()
            raise
        return closed_path

    def _start_segment(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._recorder = self._record_device_cls(self._device, str(path))
        self._active_path = path
        self._segment_started_at = time.monotonic()
        if self._point_cloud_enabled:
            try:
                self._point_cloud_recorder = PointCloudRecorder(path)
                self._emit_status(
                    "point cloud recording enabled "
                    f"({self._point_cloud_recorder.output_dir})"
                )
            except Exception as exc:
                self._point_cloud_recorder = None
                self._emit_status(f"point cloud recording disabled ({exc})")
        _write_segment_metadata(
            path,
            self._base_path or path,
            self._segment_index,
            self._pipeline,
            self._device,
            self._camera_index,
            self._stream_mode,
            self._depth_mode,
            self._hardware_align_enabled,
            self._hardware_align_status,
        )

    def _close_active_segment(self) -> Path | None:
        path = self._active_path
        if self._point_cloud_recorder is not None:
            self._point_cloud_recorder.close()
            self._point_cloud_recorder = None
        record_device = self._recorder
        self._recorder = None
        if record_device is not None:
            _release_record_device(record_device)
            record_device = None
        gc.collect()
        return path

    def _reset(self) -> None:
        self._recorder = None
        self._point_cloud_recorder = None
        self._base_path = None
        self._active_path = None
        self._segment_index = 1
        self._has_rotated = False
        self._segment_started_at = None

    def _emit_status(self, message: str) -> None:
        if self._status_callback is not None:
            self._status_callback(message)


def run_orbbec_camera_process(
    device_index: int,
    initial_depth_mode: str,
    event_queue,
    frame_queue,
    command_queue,
) -> None:
    """Run pyorbbecsdk in an isolated process.

    The GUI process must not import or initialize pyorbbecsdk here. Keeping the
    native SDK in this child process prevents C++ aborts from killing the GUI.
    """

    import cv2
    import numpy as np
    from pyorbbecsdk import Config, OBFormat, OBSensorType, Pipeline

    try:
        from pyorbbecsdk import OBAlignMode
    except Exception:
        OBAlignMode = None

    depth_mode = "3d" if str(initial_depth_mode).lower() == "3d" else "2d"
    pipeline = None
    device = None
    recording: SegmentedBagRecorder | None = None
    point_cloud_recording_enabled = False
    running = True
    stream_started = False
    stream_mode = "RGB-D raw"
    hardware_align_enabled = False
    hardware_align_status = "not started"

    def emit(event: str, *payload) -> None:
        try:
            event_queue.put((event, *payload), timeout=0.2)
        except Exception:
            pass

    def finish_recording() -> None:
        nonlocal recording
        if recording is None:
            return
        current_recording = recording
        recording = None
        try:
            path = current_recording.close()
            gc.collect()
            if path:
                emit("recording_stopped", str(path))
        except Exception as exc:
            emit("recording_error", str(exc))

    def handle_command(command) -> None:
        nonlocal depth_mode, device, recording
        nonlocal point_cloud_recording_enabled, running
        action = command[0] if command else None
        payload = command[1] if len(command) > 1 else None
        if action == "stop":
            finish_recording()
            running = False
            return
        if action == "depth_mode":
            depth_mode = "3d" if str(payload).lower() == "3d" else "2d"
            return
        if action == "point_cloud_recording_enabled":
            point_cloud_recording_enabled = bool(payload)
            return
        if action == "stop_recording":
            finish_recording()
            return
        if action != "start_recording" or recording is not None or not payload:
            return
        try:
            from pyorbbecsdk import RecordDevice

            if device is None and pipeline is not None:
                device = pipeline.get_device()
            recording = SegmentedBagRecorder(
                record_device_cls=RecordDevice,
                pipeline=pipeline,
                device=device,
                camera_index=device_index,
                stream_mode=stream_mode,
                depth_mode=depth_mode,
                hardware_align_enabled=hardware_align_enabled,
                hardware_align_status=hardware_align_status,
                point_cloud_enabled=point_cloud_recording_enabled,
                status_callback=lambda message: emit(
                    "status",
                    f"Camera {device_index + 1}: {message}",
                ),
            )
            active_path = recording.start(Path(str(payload)))
            emit("recording_started", str(active_path))
        except Exception as exc:
            failed_recording = recording
            recording = None
            if failed_recording is not None:
                try:
                    failed_recording.close()
                except Exception:
                    pass
            gc.collect()
            emit("recording_error", str(exc))

    def rotate_recording_if_needed() -> None:
        nonlocal recording
        if recording is None:
            return
        try:
            closed_path = recording.rotate_if_due()
            if closed_path is not None:
                active_path = recording.active_path
                emit("recording_segment_saved", str(closed_path))
                if active_path is not None:
                    emit(
                        "status",
                        f"Camera {device_index + 1}: saved {closed_path.name}; "
                        f"recording {active_path.name}",
                    )
        except Exception as exc:
            failed_recording = recording
            recording = None
            if failed_recording is not None:
                try:
                    failed_recording.close()
                except Exception:
                    pass
            gc.collect()
            emit("recording_error", f"Recording segment rotation failed: {exc}")

    def drain_commands() -> None:
        while True:
            try:
                handle_command(command_queue.get_nowait())
            except queue.Empty:
                return

    try:
        if device_index == 0:
            emit("status", "Camera 1: starting RGB-D stream")
            # Match Orbbec quick_start.py: skip an explicit device query and let
            # the SDK open the default camera with its model-specific defaults.
            pipeline = Pipeline()
        else:
            from pyorbbecsdk import Context

            context = Context()
            devices = context.query_devices()
            device_count = devices.get_count()
            if device_count == 0:
                emit("error", "No Orbbec camera detected. Check the USB connection.")
                return
            if device_index >= device_count:
                emit(
                    "error",
                    f"Camera {device_index + 1} not found. Detected {device_count} Orbbec camera(s).",
                )
                return

            device = devices.get_device_by_index(device_index)
            pipeline = Pipeline(device)

        (
            stream_mode,
            hardware_align_enabled,
            hardware_align_status,
        ) = _start_rgbd_pipeline_with_hw_align_fallback(
            pipeline,
            Config=Config,
            OBSensorType=OBSensorType,
            OBFormat=OBFormat,
            OBAlignMode=OBAlignMode,
            enable_imu=True,
            status_callback=lambda message: emit(
                "status",
                f"Camera {device_index + 1}: {message}",
            ),
        )
        stream_started = True
        if device is None:
            device = pipeline.get_device()
        device_info = device.get_device_info()
        device_name = device_info.get_name()
        try:
            serial_number = device_info.get_serial_number()
        except Exception:
            serial_number = ""
        try:
            connection_type = device_info.get_connection_type()
        except Exception:
            connection_type = ""
        emit("device_info", device_name, serial_number, connection_type)
        emit(
            "status",
            f"{device_name} · Camera {device_index + 1} connected; "
            f"{stream_mode} stream running; {_hardware_align_text(hardware_align_enabled, hardware_align_status)}",
        )
        emit("started")

        while running:
            drain_commands()
            rotate_recording_if_needed()
            frames = pipeline.wait_for_frames(100)
            if frames is None:
                continue

            color_frame = frames.get_color_frame()
            depth_frame = frames.get_depth_frame()
            color_bytes = None
            depth_bytes = None

            if color_frame is not None:
                color_image = _process_color_frame(color_frame, cv2, np, OBFormat)
                if color_image is not None:
                    color_bytes = _encode_jpeg(color_image, cv2)
                else:
                    emit("error", f"Unsupported color format: {color_frame.get_format()}")
                    running = False

            if depth_frame is not None:
                depth_image = _process_depth_frame(depth_frame, depth_mode, cv2, np)
                if depth_image is not None:
                    depth_bytes = _encode_jpeg(depth_image, cv2)

            if recording is not None:
                recording.record_point_cloud(frames, color_frame, depth_frame)

            if color_bytes is not None or depth_bytes is not None:
                _put_latest_frame(frame_queue, (color_bytes, depth_bytes))
    except Exception as exc:
        emit("error", str(exc))
    finally:
        finish_recording()
        if pipeline is not None:
            try:
                pipeline.stop()
            except Exception:
                pass
        pipeline = None
        device = None
        gc.collect()
        time.sleep(0.2)
        if stream_started:
            emit("stopped")


def _process_color_frame(frame, cv2, np, ob_format):
    return frame_to_bgr_image(frame, cv2, np, ob_format)


def _release_record_device(record_device) -> None:
    if record_device is None:
        return
    try:
        record_device.pause()
    except Exception:
        pass
    record_device = None
    gc.collect()
    time.sleep(0.25)


def _start_rgbd_pipeline_with_hw_align_fallback(
    pipeline,
    *,
    Config,
    OBSensorType,
    OBFormat,
    OBAlignMode,
    enable_imu: bool = True,
    status_callback=None,
) -> tuple[str, bool, str]:
    """Start RGB-D streams with raw depth by default.

    Hardware D2C profile probing is disabled by default because unsupported
    devices can fail or stall while querying D2C profiles.
    """

    attempts: list[tuple[object, str, bool, str]] = []
    seen: set[tuple[bool, bool]] = set()
    hardware_align_reason = ""

    def add_attempt(config, *, hardware_align: bool, imu_enabled: bool, align_status: str) -> None:
        key = (hardware_align, imu_enabled)
        if key in seen:
            return
        seen.add(key)
        attempts.append(
            (
                config,
                _rgbd_stream_mode_label(hardware_align, imu_enabled),
                hardware_align,
                align_status,
            )
        )

    if ENABLE_HARDWARE_D2C_ALIGN:
        for with_imu in ([True, False] if enable_imu else [False]):
            config, imu_enabled, reason = _build_hw_aligned_rgbd_config(
                pipeline,
                Config=Config,
                OBSensorType=OBSensorType,
                OBFormat=OBFormat,
                OBAlignMode=OBAlignMode,
                enable_imu=with_imu,
            )
            if config is not None:
                add_attempt(
                    config,
                    hardware_align=True,
                    imu_enabled=imu_enabled,
                    align_status="hardware D2C enabled",
                )
            elif reason and not hardware_align_reason:
                hardware_align_reason = reason
    else:
        hardware_align_reason = "hardware D2C profile disabled"

    for with_imu in ([True, False] if enable_imu else [False]):
        config, imu_enabled, reason = _build_raw_rgbd_config(
            Config=Config,
            OBSensorType=OBSensorType,
            enable_imu=with_imu,
        )
        if reason and status_callback:
            status_callback(f"IMU stream not enabled ({reason})")
        add_attempt(
            config,
            hardware_align=False,
            imu_enabled=imu_enabled,
            align_status=hardware_align_reason or "hardware D2C not enabled",
        )

    last_error = None
    for config, stream_mode, hardware_align, align_status in attempts:
        try:
            pipeline.start(config)
            if not hardware_align and hardware_align_reason:
                align_status = hardware_align_reason
            return stream_mode, hardware_align, align_status
        except Exception as exc:
            last_error = exc
            if hardware_align:
                hardware_align_reason = f"{stream_mode} start failed: {exc}"
            if status_callback:
                status_callback(f"{stream_mode} start failed ({exc})")

    if last_error is not None:
        raise last_error
    raise RuntimeError("No RGB-D stream config available.")


def _build_hw_aligned_rgbd_config(
    pipeline,
    *,
    Config,
    OBSensorType,
    OBFormat,
    OBAlignMode,
    enable_imu: bool,
) -> tuple[object | None, bool, str]:
    if OBAlignMode is None or not hasattr(OBAlignMode, "HW_MODE"):
        return None, False, "pyorbbecsdk does not expose OBAlignMode.HW_MODE"
    if not hasattr(pipeline, "get_d2c_depth_profile_list"):
        return None, False, "pyorbbecsdk does not expose hardware D2C profiles"
    try:
        color_profiles = pipeline.get_stream_profile_list(OBSensorType.COLOR_SENSOR)
    except Exception as exc:
        return None, False, f"query color profiles failed: {exc}"

    last_error = ""
    try:
        profile_count = len(color_profiles)
    except Exception as exc:
        return None, False, f"query color profile count failed: {exc}"

    for profile_index in range(profile_count):
        try:
            color_profile = color_profiles[profile_index]
        except Exception as exc:
            last_error = f"read color profile failed: {exc}"
            continue
        try:
            if color_profile.get_format() != OBFormat.RGB:
                continue
        except Exception as exc:
            last_error = f"read color profile format failed: {exc}"
            continue
        try:
            hw_depth_profiles = pipeline.get_d2c_depth_profile_list(
                color_profile,
                OBAlignMode.HW_MODE,
            )
        except Exception as exc:
            last_error = f"query hardware D2C depth profile failed: {exc}"
            continue
        try:
            if len(hw_depth_profiles) == 0:
                continue
        except Exception as exc:
            last_error = f"query hardware D2C profile count failed: {exc}"
            continue

        config = Config()
        try:
            config.enable_stream(hw_depth_profiles[0])
            config.enable_stream(color_profile)
            imu_enabled, imu_error = _try_enable_imu_streams(config, OBSensorType, enable_imu)
            config.set_align_mode(OBAlignMode.HW_MODE)
            if imu_error:
                last_error = imu_error
            return config, imu_enabled, ""
        except Exception as exc:
            last_error = f"enable hardware D2C streams failed: {exc}"

    return None, False, last_error or "no RGB hardware D2C depth profile"


def _build_raw_rgbd_config(
    *,
    Config,
    OBSensorType,
    enable_imu: bool,
) -> tuple[object, bool, str]:
    config = Config()
    config.enable_stream(OBSensorType.COLOR_SENSOR)
    config.enable_stream(OBSensorType.DEPTH_SENSOR)
    imu_enabled, imu_error = _try_enable_imu_streams(config, OBSensorType, enable_imu)
    return config, imu_enabled, imu_error


def _try_enable_imu_streams(config, OBSensorType, enable_imu: bool) -> tuple[bool, str]:
    if not enable_imu:
        return False, ""
    try:
        config.enable_stream(OBSensorType.ACCEL_SENSOR)
        config.enable_stream(OBSensorType.GYRO_SENSOR)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _rgbd_stream_mode_label(hardware_align: bool, imu_enabled: bool) -> str:
    label = "RGB-D HW Align" if hardware_align else "RGB-D raw"
    if imu_enabled:
        label += " + IMU"
    return label


def _hardware_align_text(enabled: bool, status: str) -> str:
    if enabled:
        return "硬件Align成功"
    suffix = f": {status}" if status else ""
    return f"硬件Align未启用{suffix}"


def _process_depth_frame(frame, depth_mode: str, cv2, np):
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


def _encode_jpeg(image, cv2) -> bytes | None:
    ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 60])
    if not ok:
        return None
    return encoded.tobytes()


def _put_latest_frame(frame_queue, frame) -> None:
    while True:
        try:
            frame_queue.put_nowait(frame)
            return
        except queue.Full:
            try:
                frame_queue.get_nowait()
            except queue.Empty:
                time.sleep(0.001)
        except Exception:
            return


def render_depth_3d(depth_mm, cv2, np):
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
    depth_colored = cv2.applyColorMap(depth_to_8bit(depth_mm, np), cv2.COLORMAP_JET)
    invalid = (depth_mm < MIN_DEPTH_MM) | (depth_mm > MAX_DEPTH_MM)
    depth_colored[invalid] = 0
    return depth_colored


def depth_to_8bit(depth_mm, np):
    depth_clipped = np.clip(depth_mm, MIN_DEPTH_MM, MAX_DEPTH_MM)
    depth_norm = (depth_clipped - MIN_DEPTH_MM) / (MAX_DEPTH_MM - MIN_DEPTH_MM + 1e-6)
    return (np.power(depth_norm, 0.8) * 255).astype(np.uint8)


def recording_segment_path(base_path: Path, segment_index: int) -> Path:
    base_path = Path(base_path)
    suffix = base_path.suffix or ".bag"
    return base_path.with_name(f"{base_path.stem}_{int(segment_index):03d}{suffix}")


def _move_first_segment_recording(base_path: Path, target_path: Path) -> Path:
    base_path = Path(base_path)
    target_path = Path(target_path)
    if base_path == target_path:
        return target_path
    _replace_path_if_exists(base_path, target_path)
    _safe_unlink(camera_intrinsics_path_for_bag(base_path))
    _replace_path_if_exists(point_cloud_dir_for_bag(base_path), point_cloud_dir_for_bag(target_path))
    return target_path


def _write_segment_metadata(
    bag_path: Path,
    base_bag_path: Path,
    segment_index: int,
    pipeline,
    device,
    camera_index: int,
    stream_mode: str,
    depth_mode: str,
    hardware_align_enabled: bool,
    hardware_align_status: str,
) -> None:
    _write_camera_recording_metadata(
        bag_path=bag_path,
        pipeline=pipeline,
        device=device,
        camera_index=camera_index,
        stream_mode=stream_mode,
        depth_mode=depth_mode,
        hardware_align_enabled=hardware_align_enabled,
        hardware_align_status=hardware_align_status,
        segment_index=segment_index,
        segment_seconds=RECORDING_SEGMENT_SECONDS,
        base_bag_path=base_bag_path,
    )


def _replace_path_if_exists(source: Path, target: Path) -> None:
    source = Path(source)
    target = Path(target)
    if not source.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.is_dir():
            raise FileExistsError(f"target directory already exists: {target}")
        target.unlink()
    source.rename(target)


def _safe_unlink(path: Path) -> None:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass


def camera_intrinsics_path_for_bag(bag_path: Path) -> Path:
    bag_path = Path(bag_path)
    stem = bag_path.stem
    if stem == "rgb_depth":
        return bag_path.with_name("camera_intrinsics.json")
    if stem.endswith("_rgb_depth"):
        return bag_path.with_name(f"{stem.removesuffix('_rgb_depth')}_intrinsics.json")
    return bag_path.with_name(f"{stem}_intrinsics.json")


def point_cloud_dir_for_bag(bag_path: Path) -> Path:
    bag_path = Path(bag_path)
    stem = bag_path.stem
    if stem == "rgb_depth":
        return bag_path.with_name("point_clouds")
    if stem.endswith("_rgb_depth"):
        return bag_path.with_name(f"{stem.removesuffix('_rgb_depth')}_point_clouds")
    return bag_path.with_name(f"{stem}_point_clouds")


class PointCloudRecorder:
    def __init__(self, bag_path: Path):
        from pyorbbecsdk import AlignFilter, OBFormat, OBStreamType, PointCloudFilter
        from pyorbbecsdk import save_point_cloud_to_ply

        self.output_dir = point_cloud_dir_for_bag(bag_path)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.output_dir / "index.csv"
        self._fp = self.index_path.open("w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(
            self._fp,
            fieldnames=[
                "frame_index",
                "recorded_at",
                "color_timestamp_us",
                "color_system_timestamp_us",
                "depth_timestamp_us",
                "depth_system_timestamp_us",
                "ply_path",
            ],
        )
        self._writer.writeheader()
        self._frame_index = 0
        self._align_filter = AlignFilter(align_to_stream=OBStreamType.COLOR_STREAM)
        self._point_cloud_filter = PointCloudFilter()
        self._point_cloud_filter.set_create_point_format(OBFormat.RGB_POINT)
        self._save_point_cloud_to_ply = save_point_cloud_to_ply

    def record(self, frames, color_frame, depth_frame) -> None:
        if frames is None or depth_frame is None:
            return
        point_cloud_frame = self._point_cloud_filter.process(self._align_filter.process(frames))
        if point_cloud_frame is None:
            return
        ply_name = f"point_cloud_{self._frame_index:06d}.ply"
        ply_path = self.output_dir / ply_name
        self._save_point_cloud_to_ply(str(ply_path), point_cloud_frame)
        self._writer.writerow(
            {
                "frame_index": self._frame_index,
                "recorded_at": datetime.now().isoformat(timespec="microseconds"),
                "color_timestamp_us": _frame_timestamp_us(color_frame),
                "color_system_timestamp_us": _frame_system_timestamp_us(color_frame),
                "depth_timestamp_us": _frame_timestamp_us(depth_frame),
                "depth_system_timestamp_us": _frame_system_timestamp_us(depth_frame),
                "ply_path": ply_name,
            }
        )
        self._fp.flush()
        self._frame_index += 1

    def close(self) -> None:
        try:
            self._fp.close()
        except Exception:
            pass


def _frame_timestamp_us(frame) -> int | None:
    return _call_frame_timestamp(frame, ("get_timestamp_us", "get_timestamp"))


def _frame_system_timestamp_us(frame) -> int | None:
    return _call_frame_timestamp(frame, ("get_system_timestamp_us", "get_system_timestamp"))


def _call_frame_timestamp(frame, method_names: tuple[str, ...]) -> int | None:
    if frame is None:
        return None
    for method_name in method_names:
        method = getattr(frame, method_name, None)
        if callable(method):
            try:
                return int(method())
            except Exception:
                pass
    return None


def _write_camera_recording_metadata(
    bag_path: Path,
    pipeline,
    device,
    camera_index: int,
    stream_mode: str,
    depth_mode: str,
    hardware_align_enabled: bool = False,
    hardware_align_status: str = "",
    segment_index: int = 1,
    segment_seconds: float | None = None,
    base_bag_path: Path | None = None,
) -> None:
    metadata_path = camera_intrinsics_path_for_bag(bag_path)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "schema": "multi_camera_datacollect.orbbec_camera_intrinsics.v1",
        "recorded_at": datetime.now().isoformat(timespec="microseconds"),
        "bag_path": str(Path(bag_path).name),
        "base_bag_path": str(Path(base_bag_path or bag_path).name),
        "segment_index": int(segment_index),
        "segment_seconds": segment_seconds,
        "camera_index": int(camera_index),
        "camera_number": int(camera_index) + 1,
        "stream_mode": str(stream_mode),
        "depth_preview_mode": str(depth_mode),
        "depth_recording_alignment": "hardware_d2c" if hardware_align_enabled else "raw_depth",
        "hardware_align_enabled": bool(hardware_align_enabled),
        "hardware_align_status": str(hardware_align_status),
        "device": _device_info_dict(device),
        "camera_param": None,
    }
    try:
        metadata["camera_param"] = _camera_param_to_dict(pipeline.get_camera_param())
    except Exception as exc:
        metadata["camera_param_error"] = str(exc)
    with metadata_path.open("w", encoding="utf-8") as fp:
        json.dump(metadata, fp, ensure_ascii=False, indent=2)


def _device_info_dict(device) -> dict[str, str]:
    if device is None:
        return {}
    try:
        info = device.get_device_info()
    except Exception:
        return {}
    fields = {
        "name": "get_name",
        "serial_number": "get_serial_number",
        "connection_type": "get_connection_type",
        "firmware_version": "get_firmware_version",
        "hardware_version": "get_hardware_version",
    }
    data = {}
    for key, method_name in fields.items():
        method = getattr(info, method_name, None)
        if callable(method):
            try:
                data[key] = str(method())
            except Exception:
                pass
    return data


def _camera_param_to_dict(param) -> dict:
    return {
        "rgb_intrinsic": _intrinsic_to_dict(getattr(param, "rgb_intrinsic", None)),
        "depth_intrinsic": _intrinsic_to_dict(getattr(param, "depth_intrinsic", None)),
        "rgb_distortion": _distortion_to_dict(getattr(param, "rgb_distortion", None)),
        "depth_distortion": _distortion_to_dict(getattr(param, "depth_distortion", None)),
        "depth_to_color_extrinsic": _extrinsic_to_dict(getattr(param, "transform", None)),
    }


def _intrinsic_to_dict(intrinsic) -> dict | None:
    if intrinsic is None:
        return None
    return {
        "width": _number(getattr(intrinsic, "width", None), int),
        "height": _number(getattr(intrinsic, "height", None), int),
        "fx": _number(getattr(intrinsic, "fx", None), float),
        "fy": _number(getattr(intrinsic, "fy", None), float),
        "cx": _number(getattr(intrinsic, "cx", None), float),
        "cy": _number(getattr(intrinsic, "cy", None), float),
        "camera_matrix": [
            [_number(getattr(intrinsic, "fx", 0.0), float), 0.0, _number(getattr(intrinsic, "cx", 0.0), float)],
            [0.0, _number(getattr(intrinsic, "fy", 0.0), float), _number(getattr(intrinsic, "cy", 0.0), float)],
            [0.0, 0.0, 1.0],
        ],
    }


def _distortion_to_dict(distortion) -> dict | None:
    if distortion is None:
        return None
    coeff_names = ("k1", "k2", "p1", "p2", "k3", "k4", "k5", "k6")
    coeffs = {
        name: _number(getattr(distortion, name, 0.0), float)
        for name in coeff_names
        if hasattr(distortion, name)
    }
    ordered = [coeffs[name] for name in ("k1", "k2", "p1", "p2", "k3") if name in coeffs]
    return {
        **coeffs,
        "opencv_coefficients": ordered,
    }


def _extrinsic_to_dict(extrinsic) -> dict | None:
    if extrinsic is None:
        return None
    rot = _float_list(getattr(extrinsic, "rot", []))
    transform = _float_list(getattr(extrinsic, "transform", []))
    return {
        "rotation_row_major": rot,
        "rotation_matrix": [rot[index : index + 3] for index in range(0, min(len(rot), 9), 3)],
        "translation_mm": transform,
    }


def _float_list(values) -> list[float]:
    try:
        return [float(value) for value in values]
    except Exception:
        return []


def _number(value, number_type):
    try:
        return number_type(value)
    except Exception:
        return number_type()


def frame_to_bgr_image(frame, cv2, np, ob_format):
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
