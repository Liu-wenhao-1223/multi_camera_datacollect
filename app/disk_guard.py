from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


DISK_AUTO_STOP_PERCENT = 90.0
DISK_START_BLOCK_PERCENT = 98.0
DISK_CHECK_INTERVAL_SECONDS = 0.25


@dataclass(frozen=True)
class RecordingDiskUsage:
    path: Path
    total_bytes: int
    used_bytes: int
    free_bytes: int
    percent_used: float

    @property
    def auto_stop_reached(self) -> bool:
        return self.percent_used >= DISK_AUTO_STOP_PERCENT

    @property
    def start_block_reached(self) -> bool:
        return self.percent_used >= DISK_START_BLOCK_PERCENT


class DiskGuardError(RuntimeError):
    """Base error for fail-safe recording disk checks."""


class DiskUsageLimitExceeded(DiskGuardError):
    def __init__(self, usage: RecordingDiskUsage):
        self.usage = usage
        super().__init__(disk_limit_message(usage))


class DiskAutoStopReached(DiskGuardError):
    def __init__(self, usage: RecordingDiskUsage):
        self.usage = usage
        super().__init__(disk_auto_stop_message(usage))


class DiskUsageCheckFailed(DiskGuardError):
    pass


def get_recording_disk_usage(path: str | Path) -> RecordingDiskUsage:
    target = _nearest_existing_path(Path(path).expanduser())
    total, used, free = shutil.disk_usage(target)
    if total <= 0:
        raise OSError(f"磁盘总容量无效: {target}")
    return RecordingDiskUsage(
        path=target,
        total_bytes=int(total),
        used_bytes=int(used),
        free_bytes=int(free),
        percent_used=float(used) * 100.0 / float(total),
    )


def ensure_recording_disk_available(path: str | Path) -> RecordingDiskUsage:
    try:
        usage = get_recording_disk_usage(path)
    except OSError as exc:
        message = f"无法检查录制磁盘占用，已停止录制: {exc}"
        raise DiskUsageCheckFailed(message) from exc
    if usage.start_block_reached:
        raise DiskUsageLimitExceeded(usage)
    return usage


def disk_limit_message(usage: RecordingDiskUsage) -> str:
    return (
        f"磁盘占用已达 {usage.percent_used:.1f}%"
        f"（硬上限 {DISK_START_BLOCK_PERCENT:.0f}%），"
        "录制已停止，并禁止继续录制，以防磁盘写满。"
    )


def disk_auto_stop_message(usage: RecordingDiskUsage) -> str:
    return (
        f"磁盘占用已达 {usage.percent_used:.1f}%"
        f"（自动暂停线 {DISK_AUTO_STOP_PERCENT:.0f}%），录制已自动暂停；"
        f"{DISK_START_BLOCK_PERCENT:.0f}% 前可手动继续录制。"
    )


def _nearest_existing_path(path: Path) -> Path:
    candidate = path.resolve(strict=False)
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise OSError(f"找不到可检查的录制磁盘路径: {path}")
        candidate = parent
    return candidate
