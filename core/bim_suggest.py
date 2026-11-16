# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Marco Sumari Tellez and IngeTrazo contributors.
"""Auto-tag suggestions — geometry heuristics that PROPOSE IFC classes.

The takeoff only measures tagged geometry, and tagging a whole house by
hand is the biggest friction. This walks the untagged groups and proposes
a class from shape: a tall thin vertical solid is a wall, a flat
horizontal one a slab, a small vertical post a column, a horizontal
member up in the air a beam, sloped faces a roof, a box below grade a
footing, a small thin vertical sheet a door or window.

NOTHING is applied automatically: the dialog lists the proposals with
checkboxes and an editable class, and the user confirms. A wrong guess
costs one click; a silent wrong tag would corrupt the quantities.
"""
from __future__ import annotations

from core.bim import _holds_face_tags, _placed_groups


def _tagged_below(g) -> bool:
    if getattr(g, "ifc", None) or _holds_face_tags(g.mesh):
        return True
    return any(_tagged_below(k) for k in (getattr(g, "children", None) or ()))


def classify(dx: float, dy: float, dz: float, zmin: float,
             faces=None) -> str | None:
    """The proposed IFC class from world extents, or None (no guess)."""
    thin, wide = sorted((dx, dy))
    if zmin < -0.05 and dz < 1.0 and thin > 0.3:
        return "IfcFooting"                    # a box below grade
    if thin <= 0.12 and 0.6 <= wide <= 2.0:
        if 1.8 <= dz <= 2.5 and zmin <= 0.15:
            return "IfcDoor"                   # a thin sheet to the floor
        if 0.6 <= dz <= 1.8 and zmin >= 0.3:
            return "IfcWindow"                 # a thin sheet up in a wall
    if dz >= 1.5 and thin <= 0.35 and wide >= 1.0:
        return "IfcWall"
    if dz >= 1.0 and wide <= 0.6:
        return "IfcColumn"
    if dz <= 0.6 and wide >= 2.0 and thin <= 0.6 and zmin >= 1.0:
        return "IfcBeam"
    if dz <= 0.4 and wide >= 1.0 and thin >= 1.0:
        return "IfcSlab"
    if faces and zmin >= 1.0:
        sloped = [f for f in faces if 0.2 < abs(f.normal().z()) < 0.95]
        if sloped:
            return "IfcRoof"
    return None


def suggestions(scene) -> dict:
    """{"groups": [{group, name, class, dx, dy, dz, faces}],
    "loose_untagged": n} — proposals for untagged groups, plus a count
    of loose faces no group holds (group them first, Ctrl+G)."""
    out = []
    for g, base in _placed_groups(scene.groups):
        if getattr(g, "ifc", None) or _tagged_below(g):
            continue
        faces = list(g.mesh.faces)
        if not faces:
            continue
        pts = [v.position for f in faces for lp in (f.loop, *f.hole_loops)
               for v in lp]
        if base is not None:
            pts = [base.map(p) for p in pts]
        xs = [p.x() for p in pts]
        ys = [p.y() for p in pts]
        zs = [p.z() for p in pts]
        dx, dy, dz = max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)
        cls = classify(dx, dy, dz, min(zs), faces)
        if cls:
            out.append({"group": g, "name": g.name or "", "class": cls,
                        "dx": dx, "dy": dy, "dz": dz, "faces": len(faces)})
    loose = sum(1 for f in scene.loose_mesh.faces
                if not f.attrs.get("ifc"))
    return {"groups": out, "loose_untagged": loose}