from __future__ import annotations

from PyQt6.QtWidgets import QWidget


def set_button_category(widget: QWidget, category: str) -> None:
    widget.setProperty("category", category)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()

