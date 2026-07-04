from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"
RECORD_DIR = PROJECT_ROOT / "record"
APP_SETTINGS_PATH = CONFIG_DIR / "app_settings.json"


def ensure_project_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    RECORD_DIR.mkdir(parents=True, exist_ok=True)

