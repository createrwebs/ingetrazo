# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Marco Sumari Tellez and IngeTrazo contributors.
"""This build speaks Thai by default.

An unset language choice starts in Thai (main._init_language reads the
build default, core.i18n.DEFAULT_LANGUAGE), «Same as the system» still
follows the OS (#818), a saved choice always wins — and the Thai script
composes on Windows through Leelawadee UI (views/script_fonts.py, the
Lao #850 lesson applied to this build's default language).
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import PySide6.QtCore as QtCore
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from core import i18n

_inst = QApplication.instance()
if _inst is None:
    _app = QApplication([])

import main  # noqa: E402
from views.script_fonts import script_font_for  # noqa: E402


class _FakeLocale:
    """An English OS: the build default must win over it when unset."""
    languages = ["en-US"]

    @classmethod
    def system(cls):
        return cls

    @classmethod
    def uiLanguages(cls):
        return cls.languages


@pytest.fixture
def startup(tmp_path, monkeypatch):
    path = tmp_path / "start.ini"
    monkeypatch.setattr(QtCore, "QLocale", _FakeLocale)
    monkeypatch.setattr(main, "QSettings",
                        lambda: QSettings(str(path), QSettings.IniFormat))
    monkeypatch.setattr(main, "_install_qt_translator", lambda _l: None)
    before = i18n.current_language()
    yield path
    i18n.set_language(before)


@pytest.mark.parametrize("saved, expected", [
    (None, "th"),            # nothing chosen yet: this build's default
    ("", "th"),              # an empty stored value is «nothing» too
    ("system", "en"),        # explicit «Same as the system» follows the OS
    ("es", "es"),            # a saved choice always wins
])
def test_startup_language(startup, saved, expected):
    st = QSettings(str(startup), QSettings.IniFormat)
    if saved is not None:
        st.setValue("language", saved)
    st.sync()
    main._init_language()
    assert i18n.current_language() == expected


def test_the_default_is_a_shipped_catalog():
    assert i18n.DEFAULT_LANGUAGE in i18n.available_languages()
    assert i18n.LANGUAGE_NAMES[i18n.DEFAULT_LANGUAGE] == "ไทย"


def test_thai_script_font_on_windows():
    installed = ["Segoe UI", "Leelawadee UI", "Tahoma"]
    assert script_font_for("th", installed, "win32") == "Leelawadee UI"
    assert script_font_for("th", ["Segoe UI", "Tahoma"], "win32") == "Tahoma"
    assert script_font_for("th", ["Segoe UI"], "win32") is None
    # Other platforms pick their own fallback, exactly as for Lao.
    assert script_font_for("th", installed, "linux") is None