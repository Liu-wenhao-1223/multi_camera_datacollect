from __future__ import annotations

import gc
import json
import queue
import time
from pathlib import Path

from app.disk_guard import DiskGuardError


DEFAULT_SYNC_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "multi_device_sync_config.json"
MAX_SYNC_CAMERAS = 3


def run_orbbec_multi_camera_sync_process(
    camera_count: int,
    initial_depth_mode: str,
    sync_config_path: str,
    event_queue,
    frame_queue,
    command_queue,
) -> None:
    import cv2
    import numpy as np
    from pyorbbecsdk import Config, Context, OBFormat, OBSensorType, Pipeline, RecordDevice

    try:
        from pyorbbecsdk import OBAlignMode
    except Exception:
        OBAlignMode = None

    from app.camera_process import (
        SegmentedBagRecorder,
        _process_depth_frame,
        _hardware_align_text,
        _start_rgbd_pipeline_with_hw_align_fallback,
        frame_to_bgr_image,
    )

    depth_mode = "3d" if str(initial_depth_mode).lower() == "3d" else "2d"
    requested_camera_count = max(1, min(MAX_SYNC_CAMERAS, int(camera_count)))
    camera_count = requested_camera_count
    context = None
    pipelines = []
    stream_modes: dict[int, str] = {}
    hardware_align_enabled_by_index: dict[int, bool] = {}
    hardware_align_status_by_index: dict[int, str] = {}
    recordings: dict[int, SegmentedBagRecorder] = {}
    point_cloud_recording_enabled = False
    running = True

    def emit(event: str, *payload) -> None:
        try:
            event_queue.put((event, *payload), timeout=0.2)
        except Exception:
            pass

    def finish_recording(index: int | None = None) -> None:
        indexes = sorted(recordings) if index is None else [index]
        for camera_index in indexes:
            recording = recordings.pop(camera_index, None)
            if recording is not None:
                path = recording.close()
                gc.collect()
                emit("recording_stopped", camera_index, str(path or ""))

    def handle_command(command) -> None:
        nonlocal depth_mode, point_cloud_recording_enabled, running
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
        if action != "start_recording" or not isinstance(payload, dict):
            return
        for camera_index, path in payload.items():
            try:
                index = int(camera_index)
                if index in recordings or index < 0 or index >= len(pipelines):
                    continue
                recording = SegmentedBagRecorder(
                    record_device_cls=RecordDevice,
                    pipeline=pipelines[index],
                    device=pipelines[index].get_device(),
                    camera_index=index,
                    stream_mode=stream_modes.get(index, "RGB-D raw"),
                    depth_mode=depth_mode,
                    hardware_align_enabled=hardware_align_enabled_by_index.get(index, False),
                    hardware_align_status=hardware_align_status_by_index.get(index, ""),
                    point_cloud_enabled=point_cloud_recording_enabled,
                    status_callback=lambda message, camera_index=index: emit(
                        "status",
                        camera_index,
                        f"Camera {camera_index + 1}: {message}",
                    ),
                )
                active_path = recording.start(Path(path))
                recordings[index] = recording
                emit("recording_started", index, str(active_path))
            except Exception as exc:
                try:
                    finish_recording(int(camera_index))
                except Exception:
                    pass
                emit("recording_error", int(camera_index), str(exc))

    def rotate_recordings_if_needed() -> None:
        for index, recording in list(recordings.items()):
            try:
                closed_path = recording.rotate_if_due()
                if closed_path is not None:
                    active_path = recording.active_path
                    emit("recording_segment_saved", index, str(closed_path))
                    if active_path is not None:
                        emit(
                            "status",
                            index,
                            f"Camera {index + 1}: saved {closed_path.name}; "
                            f"recording {active_path.name}",
                        )
            except DiskGuardError as exc:
                failed_recording = recordings.pop(index, None)
                if failed_recording is not None:
                    try:
                        failed_recording.close()
                    except Exception:
                        pass
                gc.collect()
                emit("recording_error", index, str(exc))
            except Exception as exc:
                failed_recording = recordings.pop(index, None)
                if failed_recording is not None:
                    try:
                        failed_recording.close()
                    except Exception:
                        pass
                gc.collect()
                emit("recording_error", index, f"Recording segment rotation failed: {exc}")

    def drain_commands() -> None:
        while True:
            try:
                handle_command(command_queue.get_nowait())
            except queue.Empty:
                return

    try:
        context = Context()
        devices = context.query_devices()
        device_count = devices.get_count()
        if device_count == 0:
            emit("error", -1, "No Orbbec camera detected. Check the USB connection.")
            return
        camera_count = min(requested_camera_count, device_count)
        if camera_count < 2:
            emit(
                "error",
                -1,
                f"同步模式至少需要 2 台 Orbbec 相机，当前检测到 {device_count} 台。",
            )
            return
        if camera_count < requested_camera_count:
            emit(
                "status",
                -1,
                f"检测到 {device_count} 台 Orbbec 相机，本次按 {camera_count} 路同步连接。",
            )

        sync_config_entries = _load_sync_config(Path(sync_config_path))
        device_plan = _build_sync_device_plan(devices, camera_count, sync_config_entries)
        camera_count = len(device_plan)
        emit(
            "status",
            -1,
            "同步配置: "
            f"{'按序列号匹配角色' if _uses_serial_config(sync_config_entries) else '使用默认USB顺序'}; "
            f"config={Path(sync_config_path)}; "
            f"serials={_serials_text(sync_config_entries)}",
        )
        for index, planned_device in enumerate(device_plan):
            device = planned_device["device"]
            usb_index = int(planned_device["usb_index"])
            serial = str(planned_device["serial"])
            name = str(planned_device["name"])
            connection = str(planned_device["connection"])
            try:
                applied_sync_config = _apply_sync_config(
                    device,
                    serial,
                    index,
                    planned_device.get("config"),
                    source=str(planned_device.get("source", "default-usb-index")),
                )
            except Exception as exc:
                emit(
                    "error",
                    index,
                    f"Camera {index + 1} 设置多设备同步失败: "
                    f"USB index={usb_index}, SN={serial or '-'}, name={name}, "
                    f"connection={connection}, config_serials={_serials_text(sync_config_entries)}, "
                    f"error={exc}",
                )
                return
            emit(
                "status",
                index,
                "Sync config applied: "
                f"source={applied_sync_config['source']}, "
                f"usb_index={usb_index}, "
                f"SN={serial or '-'}, "
                f"mode={applied_sync_config['mode']}, "
                f"trigger_out={applied_sync_config['trigger_out_enable']}, "
                f"color_delay_us={applied_sync_config['color_delay_us']}, "
                f"depth_delay_us={applied_sync_config['depth_delay_us']}, "
                f"frames_per_trigger={applied_sync_config['frames_per_trigger']}",
            )
            pipeline = Pipeline(device)
            pipelines.append(pipeline)
            emit("device_info", index, name, serial, connection)

        for index, pipeline in enumerate(pipelines):
            try:
                stream_mode, hardware_align_enabled, hardware_align_status = (
                    _start_rgbd_pipeline_with_hw_align_fallback(
                        pipeline,
                        Config=Config,
                        OBSensorType=OBSensorType,
                        OBFormat=OBFormat,
                        OBAlignMode=OBAlignMode,
                        enable_imu=True,
                        status_callback=lambda message, camera_index=index: emit(
                            "status",
                            camera_index,
                            f"Camera {camera_index + 1}: {message}",
                        ),
                    )
                )
            except Exception as exc:
                emit("error", index, f"Camera {index + 1} sync stream start failed: {exc}")
                return
            stream_modes[index] = stream_mode
            hardware_align_enabled_by_index[index] = hardware_align_enabled
            hardware_align_status_by_index[index] = hardware_align_status
            emit("align_status", index, hardware_align_enabled, hardware_align_status)
            emit(
                "status",
                index,
                f"Camera {index + 1} sync stream started; {stream_mode}; "
                f"{_hardware_align_text(hardware_align_enabled, hardware_align_status)}",
            )

        context.enable_multi_device_sync(60000)
        emit("started", camera_count)

        while running:
            drain_commands()
            rotate_recordings_if_needed()
            for index, pipeline in enumerate(pipelines):
                frames = pipeline.wait_for_frames(10)
                if frames is None:
                    continue
                color_frame = frames.get_color_frame()
                depth_frame = frames.get_depth_frame()
                color_bytes = None
                depth_bytes = None
                if color_frame is not None:
                    color_image = frame_to_bgr_image(color_frame, cv2, np, OBFormat)
                    if color_image is not None:
                        color_bytes = _encode_jpeg(color_image, cv2)
                if depth_frame is not None:
                    depth_image = _process_depth_frame(depth_frame, depth_mode, cv2, np)
                    if depth_image is not None:
                        depth_bytes = _encode_jpeg(depth_image, cv2)
                if index in recordings:
                    recordings[index].record_point_cloud(frames, color_frame, depth_frame)
                if color_bytes is not None or depth_bytes is not None:
                    _put_latest_frame(frame_queue, (index, color_bytes, depth_bytes))
            time.sleep(0.001)
    except Exception as exc:
        emit("error", -1, str(exc))
    finally:
        finish_recording()
        for index, pipeline in enumerate(pipelines):
            try:
                pipeline.stop()
            except Exception:
                pass
            emit("stopped", index)
        pipelines.clear()
        context = None
        gc.collect()
        time.sleep(0.2)


def reset_orbbec_devices_to_standalone(event_queue=None, sync_config_path: str | None = None) -> None:
    from pyorbbecsdk import Context

    context = Context()
    devices = context.query_devices()
    for index in range(devices.get_count()):
        device = devices.get_device_by_index(index)
        _set_device_standalone(device)
        if event_queue is not None:
            info = device.get_device_info()
            try:
                event_queue.put(("standalone", index, info.get_serial_number()), timeout=0.2)
            except Exception:
                pass


def _load_sync_config(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as fp:
        data = json.load(fp)
    devices = data.get("devices", []) if isinstance(data, dict) else []
    entries = []
    for index, device in enumerate(devices):
        if not isinstance(device, dict):
            continue
        serial = _normalize_serial(device.get("serial_number"))
        config = device.get("config", {})
        entries.append(
            {
                "index": index,
                "serial": serial,
                "config": config if isinstance(config, dict) else {},
            }
        )
    return entries


def _build_sync_device_plan(devices, camera_count: int, config_entries: list[dict]) -> list[dict]:
    connected_devices = []
    for usb_index in range(devices.get_count()):
        device = devices.get_device_by_index(usb_index)
        info = device.get_device_info()
        try:
            serial = _normalize_serial(info.get_serial_number())
        except Exception:
            serial = ""
        try:
            name = info.get_name()
        except Exception:
            name = ""
        try:
            connection = info.get_connection_type()
        except Exception:
            connection = ""
        connected_devices.append(
            {
                "device": device,
                "usb_index": usb_index,
                "serial": serial,
                "name": name,
                "connection": connection,
            }
        )

    real_serial_entries = [
        entry
        for entry in config_entries
        if not _is_placeholder_serial(str(entry.get("serial", "")))
    ]
    config_by_serial = {
        _serial_key(str(entry.get("serial", ""))): entry.get("config", {})
        for entry in real_serial_entries
    }
    has_configured_primary = any(
        _config_mode(entry.get("config", {})) == "PRIMARY"
        for entry in real_serial_entries
    )

    plan: list[dict] = []
    matched_primary = False
    for logical_index, connected in enumerate(connected_devices[:camera_count]):
        planned = dict(connected)
        serial_config = config_by_serial.get(_serial_key(str(connected.get("serial", ""))))
        if isinstance(serial_config, dict):
            planned["config"] = serial_config
            planned["source"] = "serial"
            if _config_mode(serial_config) == "PRIMARY":
                matched_primary = True
        else:
            fallback_config = _placeholder_config_for_index(config_entries, logical_index)
            if fallback_config is not None and not real_serial_entries:
                planned["config"] = fallback_config
                planned["source"] = "config-index"
            else:
                planned["config"] = None
                planned["source"] = "default-usb-index"
        plan.append(planned)

    for logical_index, planned in enumerate(plan):
        if isinstance(planned.get("config"), dict):
            continue
        if has_configured_primary and matched_primary:
            planned["config"] = _secondary_sync_config()
            planned["source"] = "default-secondary"
        else:
            planned["config"] = _default_sync_config(logical_index)
            planned["source"] = "default-usb-index"

    return plan[:camera_count]


def _apply_sync_config(
    device,
    serial: str,
    index: int,
    config_json: dict | None,
    *,
    source: str = "default-usb-index",
) -> dict[str, object]:
    from pyorbbecsdk import OBMultiDeviceSyncMode

    serial = _normalize_serial(serial)
    if not isinstance(config_json, dict):
        config_json = _default_sync_config(index)
        source = "default-usb-index"

    default_config = _default_sync_config(index)
    mode = str(config_json.get("mode", default_config["mode"])).strip().upper()
    color_delay_us = int(config_json.get("color_delay_us", 0))
    depth_delay_us = int(config_json.get("depth_delay_us", 0))
    trigger_to_image_delay_us = int(config_json.get("trigger_to_image_delay_us", 0))
    trigger_out_default = _default_trigger_out_enable(mode, index)
    trigger_out_enable = _bool_from_config(
        config_json.get("trigger_out_enable", trigger_out_default)
    )
    if not _sync_mode_allows_trigger_out(mode):
        trigger_out_enable = False
    trigger_out_delay_us = int(config_json.get("trigger_out_delay_us", 0))
    frames_per_trigger = int(config_json.get("frames_per_trigger", 1))

    sync_config = device.get_multi_device_sync_config()
    sync_config.mode = _sync_mode_from_str(mode, OBMultiDeviceSyncMode)
    sync_config.color_delay_us = color_delay_us
    sync_config.depth_delay_us = depth_delay_us
    if hasattr(sync_config, "trigger_to_image_delay_us"):
        sync_config.trigger_to_image_delay_us = trigger_to_image_delay_us
    sync_config.trigger_out_enable = trigger_out_enable
    sync_config.trigger_out_delay_us = trigger_out_delay_us
    sync_config.frames_per_trigger = frames_per_trigger
    try:
        device.set_multi_device_sync_config(sync_config)
    except Exception as exc:
        raise RuntimeError(
            "set_multi_device_sync_config 失败 "
            f"(SN={serial or '-'}, index={index}, source={source}, mode={mode}, "
            f"trigger_out_enable={trigger_out_enable}, "
            f"color_delay_us={color_delay_us}, depth_delay_us={depth_delay_us}, "
            f"trigger_to_image_delay_us={trigger_to_image_delay_us}, "
            f"trigger_out_delay_us={trigger_out_delay_us}, "
            f"frames_per_trigger={frames_per_trigger}): {exc}"
        ) from exc
    return {
        "source": source,
        "mode": mode,
        "color_delay_us": color_delay_us,
        "depth_delay_us": depth_delay_us,
        "trigger_to_image_delay_us": trigger_to_image_delay_us,
        "trigger_out_enable": trigger_out_enable,
        "trigger_out_delay_us": trigger_out_delay_us,
        "frames_per_trigger": frames_per_trigger,
    }


def _normalize_serial(value) -> str:
    return str(value or "").strip()


def _serial_key(value) -> str:
    return _normalize_serial(value).upper()


def _uses_serial_config(config_entries: list[dict]) -> bool:
    return any(
        not _is_placeholder_serial(str(entry.get("serial", "")))
        for entry in config_entries
    )


def _is_placeholder_serial(serial: str) -> bool:
    serial = _normalize_serial(serial).upper()
    return not serial or (serial.startswith("CAMERA_") and serial.endswith("_SERIAL"))


def _placeholder_config_for_index(config_entries: list[dict], index: int) -> dict | None:
    if index < 0 or index >= len(config_entries):
        return None
    entry = config_entries[index]
    if not _is_placeholder_serial(str(entry.get("serial", ""))):
        return None
    config = entry.get("config", {})
    return config if isinstance(config, dict) else None


def _config_mode(config_json) -> str:
    if not isinstance(config_json, dict):
        return ""
    return str(config_json.get("mode", "")).strip().upper()


def _secondary_sync_config() -> dict:
    return {
        "mode": "SECONDARY",
        "depth_delay_us": 0,
        "color_delay_us": 0,
        "trigger_to_image_delay_us": 0,
        "trigger_out_enable": False,
        "trigger_out_delay_us": 0,
        "frames_per_trigger": 1,
    }


def _serials_text(config_entries: list[dict]) -> str:
    serials = sorted(
        str(entry.get("serial", ""))
        for entry in config_entries
        if not _is_placeholder_serial(str(entry.get("serial", "")))
    )
    return ", ".join(serials) if serials else "-"


def _bool_from_config(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _set_device_standalone(device) -> None:
    from pyorbbecsdk import OBMultiDeviceSyncMode

    sync_config = device.get_multi_device_sync_config()
    sync_config.mode = OBMultiDeviceSyncMode.STANDALONE
    sync_config.depth_delay_us = 0
    sync_config.color_delay_us = 0
    sync_config.trigger_to_image_delay_us = 0
    sync_config.trigger_out_enable = False
    sync_config.trigger_out_delay_us = 0
    sync_config.frames_per_trigger = 1
    device.set_multi_device_sync_config(sync_config)


def _sync_mode_from_str(value: str, mode_type):
    value = str(value).upper()
    if value == "FREE_RUN":
        return mode_type.FREE_RUN
    if value == "STANDALONE":
        return mode_type.STANDALONE
    if value == "PRIMARY":
        return mode_type.PRIMARY
    if value == "SECONDARY":
        return mode_type.SECONDARY
    if value == "SECONDARY_SYNCED":
        return mode_type.SECONDARY_SYNCED
    if value == "SOFTWARE_TRIGGERING":
        return mode_type.SOFTWARE_TRIGGERING
    if value == "HARDWARE_TRIGGERING":
        return mode_type.HARDWARE_TRIGGERING
    raise ValueError(f"Invalid Orbbec sync mode: {value}")


def _default_sync_config(index: int) -> dict:
    return {
        "mode": "PRIMARY" if index == 0 else "SECONDARY",
        "depth_delay_us": 0,
        "color_delay_us": 0,
        "trigger_out_enable": index == 0,
        "trigger_out_delay_us": 0,
        "frames_per_trigger": 1,
    }


def _default_trigger_out_enable(mode: str, index: int) -> bool:
    return str(mode).strip().upper() == "PRIMARY" or index == 0 and not mode


def _sync_mode_allows_trigger_out(mode: str) -> bool:
    return str(mode).strip().upper() == "PRIMARY"


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
