from __future__ import annotations

from PyQt6.QtWidgets import QWidget


class Page(QWidget):
    """Minimal page base retained from the upper-computer GUI."""

    def __init__(self, route_key: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName(route_key)

