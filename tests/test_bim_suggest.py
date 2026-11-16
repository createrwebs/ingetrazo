# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Marco Sumari Tellez and IngeTrazo contributors.
"""Auto-tag suggestions: shape heuristics propose, never apply; tagged
groups are skipped; loose untagged faces are counted for guidance."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QVector3D

from core.bim import tag_group
from core.bim_suggest import classify, suggestions
from core.history import History, MakeGroupCommand
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


def test_classify_recognizes_the_house_parts():
    assert classify(4.0, 0.07, 2.6, 0.0) == "IfcWall"
    assert classify(0.2, 0.2, 3.0, 0.0) == "IfcColumn"
    assert classify(4.0, 3.0, 0.15, 0.0) == "IfcSlab"
    assert classify(0.2, 4.0, 0.4, 2.5) == "IfcBeam"
    assert classify(1.0, 1.0, 0.3, -0.5) == "IfcFooting"
    assert classify(0.9, 0.05, 2.0, 0.0) == "IfcDoor"
    assert classify(1.2, 0.05, 1.2, 1.0) == "IfcWindow"
    assert classify(0.5, 0.5, 0.5, 0.0) is None     # no confident guess


def test_suggestions_propose_for_untagged_groups_only():
    scene = Scene()
    h = History(scene)
    faces = _box(scene, size=(4.0, 0.07, 2.6))
    h.execute(MakeGroupCommand(faces, list(scene.mesh.edges)))
    g = scene.groups[0]
    data = suggestions(scene)
    assert len(data["groups"]) == 1
    assert data["groups"][0]["class"] == "IfcWall"
    assert data["groups"][0]["group"] is g
    tag_group(g, "IfcWall", "W")
    assert suggestions(scene)["groups"] == []


def test_loose_untagged_faces_are_counted():
    scene = Scene()
    scene.mesh.add_face([V(0, 0), V(1, 0), V(1, 1), V(0, 1)])
    data = suggestions(scene)
    assert data["groups"] == [] and data["loose_untagged"] == 1