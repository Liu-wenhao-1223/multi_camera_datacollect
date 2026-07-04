from __future__ import annotations

import json
from pathlib import Path

from .paths import APP_SETTINGS_PATH, PROJECT_ROOT, RECORD_DIR


def load_app_settings() -> dict:
    if not APP_SETTINGS_PATH.exists():
        return {}
    try:
        with APP_SETTINGS_PATH.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_project_path(path: str | Path) -> Path:
    output_dir = Path(path).expanduser()
    if not output_dir.is_absolute():
        output_dir = PROJECT_ROOT / output_dir
    return output_dir.resolve()


def recording_output_dir() -> Path:
    configured = load_app_settings().get("recording_output_dir")
    if not configured:
        return RECORD_DIR.resolve()
    return _resolve_project_path(str(configured))


def set_recording_output_dir(path: str | Path) -> Path:
    text = str(path).strip()
    if not text:
        raise ValueError("记录数据保存路径不能为空。")

    output_dir = _resolve_project_path(text)
    output_dir.mkdir(parents=True, exist_ok=True)
    if not output_dir.is_dir():
        raise ValueError(f"记录数据保存路径不是文件夹: {output_dir}")

    settings = load_app_settings()
    settings["recording_output_dir"] = str(output_dir)
    APP_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with APP_SETTINGS_PATH.open("w", encoding="utf-8") as fp:
        json.dump(settings, fp, ensure_ascii=False, indent=2)
        fp.write("\n")
    return output_dir

