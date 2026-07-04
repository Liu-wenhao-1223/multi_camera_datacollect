from __future__ import annotations

from PyQt6.QtWidgets import QWidget

from .camera_page_base import CameraPageBase


class CameraPage1280(CameraPageBase):
    """1280x800 compact camera collection page."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        show_sync_mode_controls: bool = False,
        default_multi_camera_sync: bool = False,
    ):
        super().__init__(
            parent,
            route_key="camera_1280",
            compact_layout=True,
            show_sync_mode_controls=show_sync_mode_controls,
            default_multi_camera_sync=default_multi_camera_sync,
        )

