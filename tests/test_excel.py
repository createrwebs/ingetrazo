# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Marco Sumari Tellez and IngeTrazo contributors.
"""Excel SpreadsheetML export: well-formed XML, a worksheet per section,
numbers typed so Excel sums them, Thai preserved."""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QVector3D

from core import i18n
from core.bim import tag_faces
from core.excel import takeoff_excel
from core.prices import PriceBook, compute_boq
from core.rooms import ffe_schedule, room_schedule
from core.scene import Scene
from core.takeoff import compute_takeoff, labor_base

_NS = {"ss": "urn:schemas-microsoft-com:office:spreadsheet"}
_NAME = "{urn:schemas-microsoft-com:office:spreadsheet}Name"


def V(x, y, z=0.0):
    return QVector3D(x, y, z)


def _box(scene, x0=0.0, size=(2.0, 1.0, 3.0)):
    sx, sy, sz = size
    c = [V(x0 + dx * sx, dy * sy, dz * sz)
         for dz in (0, 1) for dy in (0, 1) for dx in (0, 1)]
    quads = [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4),
             (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)]
    return [scene.mesh.add_face([c[i] for i in q]) for q in quads]


def _sheet_names(xml):
    root = ET.fromstring(xml)
    return [s.get(_NAME) for s in root.findall("ss:Worksheet", _NS)]


def test_excel_is_wellformed_with_material_and_shopping_sheets():
    scene = Scene()
    tag_faces(_box(scene, size=(0.2, 0.2, 3.0)), "IfcColumn", "C1", 1)
    xml = takeoff_excel(compute_takeoff(scene))
    assert xml.startswith("<?xml")
    names = _sheet_names(xml)               # parses → well-formed
    assert len(names) >= 2


def test_excel_includes_boq_and_rooms_when_given():
    scene = Scene()
    tag_faces(_box(scene, size=(4, 3, 0.15)), "IfcSlab", "S", 1)
    tag_faces(_box(scene, x0=10, size=(4, 3, 2.8)), "IfcSpace", "Room", 2)
    res = compute_takeoff(scene)
    boq = compute_boq(res, labor_base(scene, res.options), PriceBook(),
                      res.options)
    xml = takeoff_excel(res, boq=boq, rooms=room_schedule(scene),
                        ffe=ffe_schedule(scene))
    names = _sheet_names(xml)
    assert "BOQ" in names


def test_excel_types_numbers():
    scene = Scene()
    tag_faces(_box(scene, size=(0.2, 0.2, 3.0)), "IfcColumn", "C1", 1)
    xml = takeoff_excel(compute_takeoff(scene))
    assert 'ss:Type="Number"' in xml


def test_excel_keeps_thai_and_stays_wellformed():
    scene = Scene()
    tag_faces(_box(scene, size=(0.2, 0.2, 3.0)), "IfcColumn", "C1", 1)
    res = compute_takeoff(scene)
    before = i18n.current_language()
    try:
        i18n.set_language("th")
        xml = takeoff_excel(res)
        assert "ปูนซีเมนต์" in xml and "หมวดงาน" in xml
        ET.fromstring(xml)
    finally:
        i18n.set_language(before)