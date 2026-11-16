# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Marco Sumari Tellez and IngeTrazo contributors.
"""Schedules: rooms (IfcSpace), paint by material identity, openings,
rebar by diameter, preflight — the companion views."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QVector3D

from core.bim import tag_faces
from core.history import History, MakeGroupCommand
from core.rooms import (STANDARD_BAR_MM, openings_schedule, paint_schedule,
                        preflight, rebar_schedule, room_options,
                        room_schedule)
from core.scene import Scene


def V(x, y, z=0.0):
    return QVector3D(x, y, z)


def _box(scene, x0=0.0, size=(2.0, 1.0, 3.0)):
    sx, sy, sz = size
    c = [V(x0 + dx * sx, dy * sy, dz * sz)
         for dz in (0, 1) for dy in (0, 1) for dx in (0, 1)]
    quads = [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4),
             (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)]
    return [scene.mesh.add_face([c[i] for i in q]) for q in quads]


def _mat(room, item):
    return next((m for m in room["materials"] if m["item"] == item), None)


def test_room_geometry_and_finishes():
    scene = Scene()
    tag_faces(_box(scene, size=(4.0, 3.0, 2.8)), "IfcSpace", "Living", 1)
    rooms = room_schedule(scene)
    assert len(rooms) == 1
    r = rooms[0]
    assert abs(r["floor_m2"] - 12.0) < 1e-4
    assert abs(r["height_m"] - 2.8) < 1e-4
    assert abs(r["perimeter_m"] - 14.0) < 1e-4
    assert abs(r["wall_m2"] - 39.2) < 1e-3
    assert abs(r["ceiling_m2"] - 12.0) < 1e-4
    tiles = _mat(r, "Floor tiles {w}×{l} cm")
    assert tiles["qty"] == 37.0                 # 33.33 + 10% → ceil
    assert _mat(r, "Skirting (m)")["qty"] == 15.4   # 14 + 10%, metres to 0.01
    assert _mat(r, "Primer (gallons)") is not None


def test_room_wallpaper_option():
    scene = Scene()
    tag_faces(_box(scene, size=(4.0, 3.0, 2.8)), "IfcSpace", "Living", 1)
    scene.takeoff = {"rooms": {"wall": "wallpaper"}}
    r = room_schedule(scene)[0]
    rolls = _mat(r, "Wallpaper rolls")
    assert rolls is not None and rolls["qty"] == 9.0   # 39.2/5.3 ×1.1 → ceil
    assert _mat(r, "Primer (gallons)") is None         # wall no longer paint


def test_room_options_merge_over_defaults():
    scene = Scene()
    scene.takeoff = {"rooms": {"floor": "none"}}
    ro = room_options(scene)
    assert ro["floor"] == "none" and ro["wall"] == "paint"


def test_paint_schedule_groups_by_material_identity():
    scene = Scene()
    faces = _box(scene, size=(4.0, 0.07, 2.6))
    for f in faces:
        if abs(f.normal().z()) < 0.5:
            f.attrs["mat"] = "White Paint"
            f.attrs["color"] = (1.0, 1.0, 1.0, 1.0)
    tag_faces(faces, "IfcWall", "W1", 1)
    sched = paint_schedule(scene)
    assert len(sched) == 1
    assert sched[0]["label"] == "White Paint"
    assert abs(sched[0]["area"] - (2 * 4 * 2.6 + 2 * 0.07 * 2.6)) < 1e-3


def test_openings_schedule_counts_by_type_and_size():
    scene = Scene()
    for x in (0.0, 3.0):
        d = scene.mesh.add_face([V(x, 0, 0), V(x + 0.9, 0, 0),
                                 V(x + 0.9, 0, 2), V(x, 0, 2)])
        tag_faces([d], "IfcDoor", "D1", 1 if x == 0 else 2)
    w = scene.mesh.add_face([V(6, 0, 1), V(7.2, 0, 1),
                             V(7.2, 0, 2.2), V(6, 0, 2.2)])
    tag_faces([w], "IfcWindow", "W1", 3)
    sched = openings_schedule(scene)
    doors = next(g for g in sched if g["class"] == "IfcDoor")
    assert doors["count"] == 2 and doors["width"] == 0.9
    assert any(g["class"] == "IfcWindow" for g in sched)


def test_rebar_schedule_weight_and_stock_bars():
    scene = Scene()
    tag_faces(_box(scene, size=(0.02, 0.02, 3.0)), "IfcReinforcingBar",
              "B1", 1)
    sched = rebar_schedule(scene)
    assert len(sched) == 1
    g = sched[0]
    assert abs(g["weight_kg"] - 0.02 * 0.02 * 3.0 * 7850) < 1e-2
    assert g["diameter_mm"] in STANDARD_BAR_MM
    assert g["bars"] == 1                       # 3 m → one 10 m stock bar


def test_preflight_flags_open_solids_and_untagged():
    scene = Scene()
    f = scene.mesh.add_face([V(0, 0), V(3, 0), V(3, 2), V(0, 2)])
    tag_faces([f], "IfcSlab", "Open", 1)
    issues = preflight(scene)
    assert any("not watertight" in m for m, _k, _o in issues)
    h = History(scene)
    h.execute(MakeGroupCommand(_box(scene, x0=10), list(scene.mesh.edges)))
    issues = preflight(scene)
    assert any("no BIM tag" in m for m, _k, _o in issues)


def test_ffe_counts_named_component_instances():
    from PySide6.QtGui import QMatrix4x4
    from core.group import Group
    from core.mesh import Mesh
    from core.rooms import ffe_schedule
    scene = Scene()
    m = Mesh()
    m.add_face([V(0, 0, 0), V(0.5, 0, 0), V(0.5, 0.5, 0), V(0, 0.5, 0)])
    for _ in range(2):
        g = Group(m, name="Chair")
        g.xform = QMatrix4x4()          # a placed instance = a component
        scene.groups.append(g)
    untagged_plain = Group(m, name="")   # unnamed → not an FF&E line
    untagged_plain.xform = QMatrix4x4()
    scene.groups.append(untagged_plain)
    sched = ffe_schedule(scene)
    chair = next(e for e in sched if e["name"] == "Chair")
    assert chair["count"] == 2
    assert all(e["name"] for e in sched)