# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Marco Sumari Tellez and IngeTrazo contributors.
"""The Thai catalog covers the whole interface.

Thai is this build's default language (core.i18n.DEFAULT_LANGUAGE): a
tr() string without a Thai entry would show English in a Thai program.
The audit (scripts/i18n_audit.py) walks the AST, so multi-line source
strings count as the one string they are; only symbols and proper names
are allowlisted. The BIM class picker's «IfcWall — ผนัง» names are
checked here too — they reach tr() through a variable, not a literal.
"""
from __future__ import annotations

import importlib.util

from core import i18n
from core.bim import IFC_CLASS_NAMES, IFC_CLASSES
from core.paths import app_root

_ROOT = app_root()
_spec = importlib.util.spec_from_file_location(
    "i18n_audit", _ROOT / "scripts" / "i18n_audit.py")
_audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_audit)


def test_every_source_string_has_a_thai_translation():
    missing = _audit.missing_strings("th")
    assert not missing, f"{len(missing)} untranslated: {missing[:5]}"


def test_the_audit_sees_the_whole_interface():
    # A broken walk (wrong root, parse errors) would «pass» coverage by
    # finding nothing: the interface has thousands of messages.
    assert len(_audit.source_strings()) > 2000


def test_the_bim_class_names_speak_thai():
    before = i18n.current_language()
    try:
        i18n.set_language("th")
        for cls in IFC_CLASSES:
            name = IFC_CLASS_NAMES[cls]
            assert i18n.tr(name) != name, f"{cls}: {name!r} has no Thai"
    finally:
        i18n.set_language(before)