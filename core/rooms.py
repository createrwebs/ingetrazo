# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Marco Sumari Tellez and IngeTrazo contributors.
"""Schedules — the interior designer's and engineer's companion views.

- Room schedule (IfcSpace): floor area, height, perimeter (skirting),
  wall and ceiling areas, with finish estimates per the document's room
  options (``scene.takeoff["rooms"]``).
- Paint schedule: tagged wall/covering faces grouped by their REAL
  material identity — registry name, texture, or colour (the Paint
  tray's idiom, core.materials).
- Openings schedule: doors and windows grouped by type and size.
- Rebar schedule: tagged bars by nominal diameter, stock-length counts.
- Preflight: what to fix before trusting the takeoff.

These are SEPARATE views, not additions to the main takeoff: room
finishes overlap the element-based paint/tiles lines. The Schedules tab
says so; the shopping list stays element-based.
"""
from __future__ import annotations

import math
from pathlib import Path

from core.bim import (IFC_CLASS_NAMES, class_quantities, collect_objects,
                      face_net_area)
from core.takeoff import ADHESIVE_BAG_KG, round_order

#: Room finish defaults; scene.takeoff["rooms"] overrides them.
ROOM_DEFAULTS = {
    "floor": "tile",            # tile | none
    "wall": "paint",            # paint | wallpaper | tile | none
    "ceiling": "paint",         # paint | none
    "skirting": True,
    "wallpaper_roll_m2": 5.3,   # a 0.53 × 10 m roll's coverage
    "tile_w": 0.60, "tile_l": 0.60,
    "adhesive_kg_m2": 5.0,
    "paint_coverage": 32.0, "primer_coats": 1, "top_coats": 2,
    "waste": 10.0,
}

#: The rebar diameters the Thai market stocks (mm).
STANDARD_BAR_MM = (6, 9, 12, 16, 20, 25, 28, 32)
BAR_STOCK_M = 10.0


def room_options(scene) -> dict:
    opts = dict(ROOM_DEFAULTS)
    saved = (getattr(scene, "takeoff", None) or {}).get("rooms")
    if isinstance(saved, dict):
        opts.update({k: v for k, v in saved.items() if k in ROOM_DEFAULTS})
    return opts


def _faces_of(obj):
    faces = obj.get("faces")
    return faces if faces is not None else list(obj["group"].mesh.faces)


def _quantities(obj):
    _qs, entries, _m = class_quantities(obj["class"], _faces_of(obj))
    return {name: val for _k, name, val in entries}


def _perimeter(face) -> float:
    loop = face.loop
    return sum((loop[i].position
                - loop[(i + 1) % len(loop)].position).length()
               for i in range(len(loop)))


def room_schedule(scene, memo: dict | None = None) -> list:
    """One entry per tagged room (IfcSpace): its geometry and the finish
    materials the room options imply. Wall area is perimeter × height —
    gross, openings not deducted (the note says so)."""
    ro = room_options(scene)
    out = []
    for obj in collect_objects(scene, memo):
        if obj["class"] != "IfcSpace":
            continue
        q = _quantities(obj)
        floor = q.get("NetFloorArea", 0.0)
        height = q.get("Height", 0.0)
        faces = _faces_of(obj)
        horiz = [f for f in faces if abs(f.normal().z()) > 0.7]
        perim = (_perimeter(max(horiz, key=lambda f: f.area()))
                 if horiz else 0.0)
        wall = perim * height
        materials = []

        def add(item, qty, unit, args=None, note=""):
            if qty > 1e-9:
                materials.append({"item": item, "qty": qty, "unit": unit,
                                  "args": args or {}, "note": note})

        tw, tl = float(ro["tile_w"]), float(ro["tile_l"])
        targs = {"w": f"{tw * 100:g}", "l": f"{tl * 100:g}"}
        waste = float(ro["waste"])
        if ro["floor"] == "tile" and floor > 0:
            add("Floor tiles {w}×{l} cm",
                round_order(floor / (tw * tl), waste, "piece"), "piece",
                targs)
            add("Tile adhesive (20 kg bags)",
                round_order(floor * float(ro["adhesive_kg_m2"])
                            / ADHESIVE_BAG_KG, waste, "bag20"), "bag20")
        if wall > 0 and ro["wall"] == "paint":
            cov = max(1.0, float(ro["paint_coverage"]))
            add("Primer (gallons)", round_order(
                wall * int(ro["primer_coats"]) / cov, waste, "gallon"),
                "gallon")
            add("Topcoat (gallons)", round_order(
                wall * int(ro["top_coats"]) / cov, waste, "gallon"),
                "gallon")
        elif wall > 0 and ro["wall"] == "wallpaper":
            add("Wallpaper rolls", round_order(
                wall / max(0.5, float(ro["wallpaper_roll_m2"])),
                waste, "piece"), "piece", note="0.53 × 10 m")
        elif wall > 0 and ro["wall"] == "tile":
            add("Wall tiles {w}×{l} cm",
                round_order(wall / (tw * tl), waste, "piece"), "piece",
                targs)
            add("Tile adhesive (20 kg bags)",
                round_order(wall * float(ro["adhesive_kg_m2"])
                            / ADHESIVE_BAG_KG, waste, "bag20"), "bag20")
        if ro["ceiling"] == "paint" and floor > 0:
            add("Topcoat (gallons)", round_order(
                floor * int(ro["top_coats"])
                / max(1.0, float(ro["paint_coverage"])), waste, "gallon"),
                "gallon", note="ceiling")
        if ro["skirting"] and perim > 0:
            add("Skirting (m)", round_order(perim, waste, "m"), "m")
        out.append({"obj": obj, "name": obj.get("name") or "",
                    "floor_m2": floor, "height_m": height,
                    "perimeter_m": perim, "wall_m2": wall,
                    "ceiling_m2": floor, "materials": materials})
    return out

def _material_key(face):
    """The face's finish identity: registry name first (core.materials),
    then texture stem, then the colour itself — the Paint tray's idiom."""
    mat = face.attrs.get("mat")
    if mat:
        return ("mat", str(mat))
    tex = face.attrs.get("texture")
    if tex and tex.get("path"):
        return ("tex", Path(str(tex["path"])).stem)
    col = face.attrs.get("color")
    if col:
        return ("rgb", tuple(round(float(c), 3) for c in col[:3]))
    return ("none", "")


def _material_label(key) -> str:
    kind, val = key
    if kind in ("mat", "tex"):
        return str(val)
    if kind == "rgb":
        return "#{:02x}{:02x}{:02x}".format(*(int(round(c * 255))
                                              for c in val))
    return "Unpainted"


def paint_schedule(scene, memo: dict | None = None) -> list:
    """Tagged wall/covering faces grouped by finish identity: area per
    colour/material — the paint shop's mixing list. Wall tops/bottoms
    are skipped (only the paintable sides)."""
    groups: dict = {}
    for obj in collect_objects(scene, memo):
        if obj["class"] not in ("IfcWall", "IfcCovering"):
            continue
        for f in _faces_of(obj):
            if obj["class"] == "IfcWall" and abs(f.normal().z()) > 0.5:
                continue
            key = _material_key(f)
            g = groups.get(key)
            if g is None:
                g = {"label": _material_label(key), "key": key,
                     "area": 0.0, "faces": 0, "objects": set()}
                groups[key] = g
            g["area"] += face_net_area(f)
            g["faces"] += 1
            g["objects"].add(obj.get("name") or obj["class"])
    out = sorted(groups.values(), key=lambda g: -g["area"])
    for g in out:
        g["objects"] = sorted(g["objects"])
    return out


def openings_schedule(scene, memo: dict | None = None) -> list:
    """Doors and windows grouped by class + name + size: the schedule a
    builder orders from (count, width × height, area)."""
    groups: dict = {}
    for obj in collect_objects(scene, memo):
        if obj["class"] not in ("IfcDoor", "IfcWindow"):
            continue
        q = _quantities(obj)
        w = round(q.get("Width", 0.0), 2)
        h = round(q.get("Height", 0.0), 2)
        key = (obj["class"], obj.get("name") or "", w, h)
        g = groups.get(key)
        if g is None:
            g = {"class": obj["class"], "name": obj.get("name") or "",
                 "width": w, "height": h, "count": 0, "area": 0.0}
            groups[key] = g
        g["count"] += 1
        g["area"] += q.get("Area", w * h)
    return sorted(groups.values(),
                  key=lambda g: (g["class"], g["name"], -g["count"]))


def rebar_schedule(scene, memo: dict | None = None) -> list:
    """Tagged bars by nominal diameter (snapped to the stocked sizes):
    weight is exact (volume × 7850); length is the object's longest
    extent — exact for a single straight bar, indicative for a bundle."""
    by_d: dict = {}
    for obj in collect_objects(scene, memo):
        if obj["class"] != "IfcReinforcingBar":
            continue
        q = _quantities(obj)
        weight = q.get("Weight", 0.0)
        if weight <= 1e-9:
            continue
        length = q.get("Length", 0.0)
        d_mm = q.get("NominalDiameter", 0.0) * 1000.0
        d = (min(STANDARD_BAR_MM, key=lambda s: abs(s - d_mm))
             if d_mm > 0 else 0)
        g = by_d.get(d)
        if g is None:
            g = {"diameter_mm": d, "weight_kg": 0.0, "length_m": 0.0,
                 "bars": 0, "objects": 0}
            by_d[d] = g
        g["weight_kg"] += weight
        g["length_m"] += length
        g["bars"] += math.ceil(length / BAR_STOCK_M) if length > 0 else 0
        g["objects"] += 1
    return sorted(by_d.values(), key=lambda g: g["diameter_mm"])


def ffe_schedule(scene) -> list:
    """Furniture, fixtures & equipment: named component instances counted
    by name (one placed library object = one item). Structural BIM-tagged
    objects, face-me images and 3D text are excluded."""
    from core.bim import _placed_groups
    counts: dict = {}
    for g, _base in _placed_groups(scene.groups):
        if getattr(g, "ifc", None):
            continue
        if getattr(g, "billboard", False) or getattr(g, "text3d", None):
            continue
        if not g.is_component():
            continue
        name = (g.name or "").strip()
        if not name:
            continue
        e = counts.get(name)
        if e is None:
            e = {"name": name, "count": 0}
            counts[name] = e
        e["count"] += 1
    return sorted(counts.values(), key=lambda e: (-e["count"], e["name"]))


def preflight(scene, memo: dict | None = None) -> list:
    """What to fix before trusting the takeoff: open solids (their
    quantities fall back to estimates), untagged geometry, no tags at
    all. Each issue: (message template, kwargs, object or None)."""
    from core.takeoff import _count_untagged
    issues = []
    objects = collect_objects(scene, memo)
    solid_classes = ("IfcWall", "IfcSlab", "IfcColumn", "IfcBeam",
                     "IfcFooting", "IfcStair", "IfcRamp", "IfcPile",
                     "IfcReinforcingBar")
    for obj in objects:
        if obj["class"] in solid_classes and obj.get("volume") is None:
            issues.append(("«{name}» ({cls}): not watertight — quantities "
                           "fall back to estimates",
                           {"name": obj.get("name") or obj["class"],
                            "cls": IFC_CLASS_NAMES.get(obj["class"],
                                                       obj["class"])},
                           obj))
    n = _count_untagged(scene)
    if n:
        issues.append(("{n} groups carry geometry with no BIM tag",
                       {"n": n}, None))
    if not objects:
        issues.append(("No BIM tags at all — tag walls, slabs, columns "
                       "first", {}, None))
    return issues