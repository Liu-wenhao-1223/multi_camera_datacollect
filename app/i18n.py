from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from PyQt6.QtWidgets import (
    QAbstractButton,
    QGroupBox,
    QLabel,
    QLineEdit,
    QTabWidget,
    QWidget,
)


LOCALES_DIR = Path(__file__).resolve().parent / "locales"
LANGUAGE_FILES = {
    "Chinese": LOCALES_DIR / "zh_CN.json",
    "English": LOCALES_DIR / "en_US.json",
}


def normalize_language(language: str) -> str:
    return "English" if str(language).strip().lower().startswith("english") else "Chinese"


@lru_cache(maxsize=2)
def load_catalog(language: str) -> dict[str, str]:
    path = LANGUAGE_FILES[normalize_language(language)]
    with path.open("r", encoding="utf-8") as fp:
        data = json.load(fp)
    return {str(key): str(value) for key, value in data.items()}


@lru_cache(maxsize=2)
def _exact_translation_map(language: str) -> dict[str, str]:
    target = load_catalog(language)
    result: dict[str, str] = {}
    for source_language in LANGUAGE_FILES:
        source = load_catalog(source_language)
        for key, value in source.items():
            if not key.startswith("phrase.") and key in target:
                result[value] = target[key]
    return result


@lru_cache(maxsize=2)
def _phrase_translation_map(language: str) -> tuple[tuple[str, str], ...]:
    target = load_catalog(language)
    result: dict[str, str] = {}
    for source_language in LANGUAGE_FILES:
        source = load_catalog(source_language)
        for key, value in source.items():
            if key.startswith("phrase.") and key in target:
                result[value] = target[key]
    return tuple(sorted(result.items(), key=lambda item: len(item[0]), reverse=True))


@lru_cache(maxsize=2)
def _fragment_translation_map(language: str) -> tuple[tuple[str, str], ...]:
    target = load_catalog(language)
    result: dict[str, str] = {}
    for source_language in LANGUAGE_FILES:
        source = load_catalog(source_language)
        for key, value in source.items():
            if key.startswith("phrase.") or key not in target:
                continue
            if len(value) >= 4 or any("\u4e00" <= char <= "\u9fff" for char in value):
                result[value] = target[key]
    return tuple(sorted(result.items(), key=lambda item: len(item[0]), reverse=True))


def translate_text(text: str, language: str) -> str:
    if not text:
        return text
    exact = _exact_translation_map(language).get(text)
    if exact is not None:
        return exact
    translated = text
    for source, target in _fragment_translation_map(language):
        if source in translated:
            translated = translated.replace(source, target)
    for source, target in _phrase_translation_map(language):
        if source in translated:
            translated = translated.replace(source, target)
    return translated


def translate_widget_tree(root: QWidget, language: str) -> None:
    """Translate known visible widget text without touching user-entered values."""

    normalized = normalize_language(language)
    widgets = [root, *root.findChildren(QWidget)]
    for widget in widgets:
        if isinstance(widget, QLabel):
            translated = translate_text(widget.text(), normalized)
            if translated != widget.text():
                widget.setText(translated)
        elif isinstance(widget, QAbstractButton):
            translated = translate_text(widget.text(), normalized)
            if translated != widget.text():
                widget.setText(translated)
        elif isinstance(widget, QGroupBox):
            translated = translate_text(widget.title(), normalized)
            if translated != widget.title():
                widget.setTitle(translated)

        tooltip = widget.toolTip()
        if tooltip:
            translated = translate_text(tooltip, normalized)
            if translated != tooltip:
                widget.setToolTip(translated)
        if widget.windowTitle():
            translated = translate_text(widget.windowTitle(), normalized)
            if translated != widget.windowTitle():
                widget.setWindowTitle(translated)
        if isinstance(widget, QLineEdit) and widget.placeholderText():
            translated = translate_text(widget.placeholderText(), normalized)
            if translated != widget.placeholderText():
                widget.setPlaceholderText(translated)
        if widget.property("i18nTranslateItems") and all(
            hasattr(widget, name) for name in ("count", "itemText", "setItemText")
        ):
            widget.blockSignals(True)
            for index in range(widget.count()):
                translated = translate_text(widget.itemText(index), normalized)
                if translated != widget.itemText(index):
                    widget.setItemText(index, translated)
            widget.blockSignals(False)
        if isinstance(widget, QTabWidget):
            for index in range(widget.count()):
                translated = translate_text(widget.tabText(index), normalized)
                if translated != widget.tabText(index):
                    widget.setTabText(index, translated)
