# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Marco Sumari Tellez and IngeTrazo contributors.
"""Material takeoff: the rate math, the per-class conversion, honesty
(watertight vs fallback, tagged rebar vs ratios), persistence and CSV.

The rate expectations are the published Thai standards the engine cites
(rakadd.com, thailandinnodesign.com, nsquare.co, steellead.com): 1:2:4
concrete → 6.336 bags + 0.44 sand + 0.88 gravel per m³; plaster 15 mm
1:3 → 7.02 kg cement/m²; concrete block 39×19 + 10 mm joint → 12.5/m².
"""
from __future__ import annotations

import math

from PySide6.QtGui import QVector3D

from core import i18n
from core.bim import tag_faces
from core.history import History, MakeGroupCommand
from core.scene import Scene
from core.takeoff import (TakeoffOptions, bricks_per_m2, compute_takeoff,
                          concrete_mix_materials, labor_base,
                          mortar_joint_fraction, mortar_mix_materials,
                          preset_options, round_order, takeoff_csv, wythes)
from formats import igz


def V(x, y, z=0.0):
    return QVector3D(x, y, z)


def _box(scene, x0=0.0, size=(2.0, 1.0, 3.0)):
    """A closed box on the loose mesh; its six faces (as test_bim builds)."""
    sx, sy, sz = size
    c = [V(x0 + dx * sx, dy * sy, dz * sz)
         for dz in (0, 1) for dy in (0, 1) for dx in (0, 1)]
    quads = [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4),
             (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)]
    return [scene.mesh.add_face([c[i] for i in q]) for q in quads]


def _rows(result, item, category=None):
    return [r for r in result.rows
            if r.item == item and (category is None or r.category == category)]


def _net(result, item, category=None):
    return sum(r.net for r in _rows(result, item, category))


# ---- The published rate math -------------------------------------------------

def test_concrete_1_2_4_per_cubic_metre():
    ck, sd, gv, wt = concrete_mix_materials(1.0, "1:2:4")
    assert abs(ck - 316.8) < 0.01            # 1.54/7 × 1440
    assert abs(ck / 50.0 - 6.336) < 0.001    # the published 6.3 bags
    assert abs(sd - 0.44) < 1e-9
    assert abs(gv - 0.88) < 1e-9
    assert abs(wt - ck * 0.5) < 1e-9


def test_concrete_1_1_5_3_per_cubic_metre():
    ck, sd, gv, _ = concrete_mix_materials(1.0, "1:1.5:3")
    assert abs(ck / 50.0 - 8.064) < 0.001    # the published 8.1 bags
    assert abs(sd - 0.42) < 1e-9 and abs(gv - 0.84) < 1e-9


def test_plaster_15mm_1_3_cement_per_m2():
    ck, sd = mortar_mix_materials(0.015, "1:3")   # 1 m² × 15 mm wet
    assert abs(ck - 7.02) < 0.01                  # the published ~7 kg/m²
    assert abs(sd - 0.015 * 1.30 * 0.75) < 1e-9


def test_bricks_per_m2_by_type():
    assert abs(bricks_per_m2("block7") - 12.5) < 1e-9
    assert abs(bricks_per_m2("mon_half") - 143.37) < 0.01   # ≈ the 140 norm
    assert abs(bricks_per_m2("aac75") - 8.169) < 0.001      # 3 mm glue joint


def test_mortar_joint_fraction_and_wythes():
    assert abs(mortar_joint_fraction("mon_half") - 0.2724) < 0.0001
    assert wythes(0.10, "mon_half") == 1     # 10 cm wall = 1 wythe + plaster
    assert wythes(0.14, "mon_half") == 2     # full-brick wall
    assert wythes(0.07, "block7") == 1


def test_order_rounds_up_to_purchase_units():
    assert round_order(6.336, 5.0, "bag50") == 7.0
    assert round_order(0.44, 5.0, "m3") == 0.47      # 0.462 → 0.01 steps
    assert round_order(10.0, 0.0, "kg") == 10.0
    assert round_order(0.1, 0.0, "gallon") == 1.0


def test_options_round_trip_and_merge_over_defaults():
    opts = TakeoffOptions(concrete_mix="1:1.5:3")
    opts.waste["brick"] = 8.0
    back = TakeoffOptions.from_dict(opts.to_dict())
    assert back.concrete_mix == "1:1.5:3" and back.waste["brick"] == 8.0
    partial = TakeoffOptions.from_dict({"waste": {"brick": 7.0}})
    assert partial.concrete_mix == "1:2:4"           # default kept
    assert partial.waste["brick"] == 7.0 and partial.waste["concrete"] == 5.0
    assert preset_options("house_aac").brick == "aac75"
    assert preset_options("concrete_building").brick == "concrete"

# ---- One tagged object, end to end -------------------------------------------

def test_column_concrete_rebar_and_formwork():
    scene = Scene()
    tag_faces(_box(scene, size=(0.2, 0.2, 3.0)), "IfcColumn", "C1", 1)
    res = compute_takeoff(scene)
    assert abs(_net(res, "Concrete — {part}") - 0.12) < 1e-5
    assert abs(_net(res, "Cement (50 kg bags)") - 0.12 * 316.8 / 50) < 1e-4
    rebar = _rows(res, "Rebar steel (kg)")
    assert len(rebar) == 1 and abs(rebar[0].net - 0.12 * 130) < 1e-5
    assert rebar[0].estimated                       # a ratio, not a design
    # formwork: the four sides 2×(0.2+0.2)×3 = 2.4 m², 3 reuses
    sheets = _rows(res, "Plywood formwork sheets (1.22×2.44 m)")
    assert abs(sheets[0].net - 2.4 / (1.22 * 2.44) / 3) < 1e-5


def test_clay_brick_wall_full_chain():
    scene = Scene()
    tag_faces(_box(scene, size=(4.0, 0.07, 2.6)), "IfcWall", "W1", 1)
    res = compute_takeoff(scene)
    net_area = 4.0 * 2.6
    assert abs(_net(res, "Bricks — {type}") - net_area * 143.369) < 0.1
    mv = net_area * 0.07 * mortar_joint_fraction("mon_half")
    ck, _sd = mortar_mix_materials(mv, "1:3")
    assert abs(_net(res, "Laying cement (50 kg bags)") - ck / 50) < 1e-4
    ck, _ = mortar_mix_materials(net_area * 2 * 0.015, "1:3")
    assert abs(_net(res, "Plaster cement (50 kg bags)") - ck / 50) < 1e-4
    assert abs(_net(res, "Primer (gallons)") - net_area * 2 / 32) < 1e-5
    assert abs(_net(res, "Topcoat (gallons)") - net_area * 2 * 2 / 32) < 1e-5


def test_aac_wall_uses_glue_not_mortar():
    scene = Scene()
    tag_faces(_box(scene, size=(4.0, 0.075, 2.6)), "IfcWall", "W1", 1)
    res = compute_takeoff(scene, preset_options("house_aac"))
    assert abs(_net(res, "Bricks — {type}") - 10.4 * 8.169) < 0.1
    assert abs(_net(res, "AAC adhesive (20 kg bags)") - 10.4 * 5 / 20) < 1e-5
    assert not _rows(res, "Laying cement (50 kg bags)")


def test_concrete_wall_option():
    scene = Scene()
    tag_faces(_box(scene, size=(4.0, 0.15, 2.6)), "IfcWall", "W1", 1)
    res = compute_takeoff(scene, preset_options("concrete_building"))
    assert abs(_net(res, "Concrete — {part}") - 4 * 0.15 * 2.6) < 1e-5
    assert abs(_net(res, "Rebar steel (kg)") - 1.56 * 80) < 1e-5
    assert not _rows(res, "Bricks — {type}")


def test_open_slab_falls_back_to_thickness_and_warns():
    scene = Scene()
    f = scene.mesh.add_face([V(0, 0), V(3, 0), V(3, 2), V(0, 2)])
    tag_faces([f], "IfcSlab", "S1", 1)
    res = compute_takeoff(scene)
    conc = _rows(res, "Concrete — {part}")
    assert abs(conc[0].net - 6.0 * 0.15) < 1e-5 and conc[0].estimated
    assert any("not watertight" in w[0] for w in res.warnings)
    tiles = _rows(res, "Floor tiles {w}×{l} cm")
    assert abs(tiles[0].net - 6.0 / 0.36) < 1e-5
    assert tiles[0].order == 19.0                  # 16.67 + 10% → ceil
    assert abs(_net(res, "Lean concrete (m³)") - 6.0 * 0.05) < 1e-5


def test_closed_slab_volume_is_measured_not_assumed():
    scene = Scene()
    tag_faces(_box(scene, size=(4.0, 3.0, 0.15)), "IfcSlab", "S1", 1)
    res = compute_takeoff(scene)
    conc = _rows(res, "Concrete — {part}")
    assert abs(conc[0].net - 1.8) < 1e-5 and not conc[0].estimated
    assert not res.warnings


def test_door_and_window_schedules():
    scene = Scene()
    d = scene.mesh.add_face([V(0, 0, 0), V(0.9, 0, 0), V(0.9, 0, 2), V(0, 0, 2)])
    tag_faces([d], "IfcDoor", "D1", 1)
    w = scene.mesh.add_face([V(5, 0, 1), V(6.2, 0, 1), V(6.2, 0, 2.2), V(5, 0, 2.2)])
    tag_faces([w], "IfcWindow", "W1", 2)
    res = compute_takeoff(scene)
    doors = _rows(res, "Doors (sets)")
    assert doors[0].net == 1.0 and "0.90 × 2.00" in doors[0].note
    assert _rows(res, "Windows (sets)")[0].net == 1.0


def test_roof_sheets_and_truss_estimate():
    scene = Scene()
    f = scene.mesh.add_face([V(0, 0, 0), V(4, 0, 0), V(4, 3, 1.5), V(0, 3, 1.5)])
    tag_faces([f], "IfcRoof", "R1", 1)
    res = compute_takeoff(scene)
    area = 4.0 * math.sqrt(3.0 ** 2 + 1.5 ** 2)
    sheets = _rows(res, "Roof tiles — {type}")
    assert abs(sheets[0].net - area * 2.2) < 1e-4
    truss = _rows(res, "Roof truss steel (kg, estimate)")
    assert abs(truss[0].net - area * 15) < 1e-4 and truss[0].estimated


def test_tagged_rebar_wins_over_ratios_in_auto_mode():
    scene = Scene()
    tag_faces(_box(scene, size=(0.2, 0.2, 3.0)), "IfcColumn", "C1", 1)
    bar = _box(scene, x0=10.0, size=(0.02, 0.02, 3.0))
    tag_faces(bar, "IfcReinforcingBar", "B1", 2)
    res = compute_takeoff(scene)
    rebar = _rows(res, "Rebar steel (kg)")
    assert len(rebar) == 1
    assert abs(rebar[0].net - 0.02 * 0.02 * 3.0 * 7850) < 1e-3
    assert not rebar[0].estimated                  # measured, not a ratio


def test_ratios_mode_ignores_tagged_bars_with_a_warning():
    scene = Scene()
    tag_faces(_box(scene, size=(0.2, 0.2, 3.0)), "IfcColumn", "C1", 1)
    tag_faces(_box(scene, x0=10.0, size=(0.02, 0.02, 3.0)),
              "IfcReinforcingBar", "B1", 2)
    opts = TakeoffOptions(rebar_mode="ratios")
    res = compute_takeoff(scene, opts)
    assert abs(_net(res, "Rebar steel (kg)") - 0.12 * 130) < 1e-5
    assert any("ignored" in w[0] for w in res.warnings)


def test_untagged_groups_are_counted_for_guidance():
    scene = Scene()
    h = History(scene)
    faces = _box(scene)
    h.execute(MakeGroupCommand(faces, list(scene.mesh.edges)))
    res = compute_takeoff(scene)
    assert res.rows == [] and res.untagged_groups == 1

# ---- Persistence, shopping totals, labor base, CSV ---------------------------

def test_options_travel_with_the_document(tmp_path):
    scene = Scene()
    tag_faces(_box(scene, size=(0.2, 0.2, 3.0)), "IfcColumn", "C1", 1)
    opts = TakeoffOptions(concrete_mix="1:1.5:3")
    opts.waste["brick"] = 8.0
    scene.takeoff = opts.to_dict()
    p = tmp_path / "takeoff.igz"
    igz.save_scene(scene, p)
    scene2 = Scene()
    igz.load_into(scene2, p)
    assert scene2.takeoff == scene.takeoff
    res = compute_takeoff(scene2)
    assert res.options.concrete_mix == "1:1.5:3"
    assert abs(_net(res, "Cement (50 kg bags)") - 0.12 * 403.2 / 50) < 1e-4


def test_old_documents_open_with_defaults(tmp_path):
    scene = Scene()
    p = tmp_path / "old.igz"
    igz.save_scene(scene, p)
    scene2 = Scene()
    igz.load_into(scene2, p)
    assert scene2.takeoff == {}
    assert compute_takeoff(scene2).options.concrete_mix == "1:2:4"


def test_shopping_merges_sand_across_categories():
    scene = Scene()
    tag_faces(_box(scene, size=(4.0, 0.07, 2.6)), "IfcWall", "W1", 1)
    tag_faces(_box(scene, x0=10.0, size=(4.0, 3.0, 0.15)), "IfcSlab", "S1", 2)
    res = compute_takeoff(scene)
    sands = [r for r in res.shopping if r.item == "Sand (m³)"]
    assert len(sands) == 1
    # laying + plaster + concrete + lean + bedding sand, all in one line
    assert sands[0].net > 0.5
    assert len(sands[0].sources) >= 4


def test_labor_base_quantities():
    scene = Scene()
    tag_faces(_box(scene, size=(4.0, 0.07, 2.6)), "IfcWall", "W1", 1)
    tag_faces(_box(scene, x0=10.0, size=(4.0, 3.0, 0.15)), "IfcSlab", "S1", 2)
    work = labor_base(scene, TakeoffOptions())
    assert abs(work["masonry_m2"] - 10.4) < 1e-5
    assert abs(work["plaster_m2"] - 20.8) < 1e-5
    assert abs(work["paint_m2"] - 20.8) < 1e-5
    assert abs(work["concrete_m3"] - 1.8) < 1e-5
    assert abs(work["tile_m2"] - 12.0) < 1e-5


def test_csv_speaks_the_running_language():
    scene = Scene()
    tag_faces(_box(scene, size=(0.2, 0.2, 3.0)), "IfcColumn", "C1", 1)
    res = compute_takeoff(scene)
    before = i18n.current_language()
    try:
        i18n.set_language("th")
        csv = takeoff_csv(res)
        assert "หมวดงาน" in csv and "ปูนซีเมนต์ (ถุง 50 กก.)" in csv
        assert "ประมาณการเพื่อสั่งซื้อ" in csv
        assert "วัดจริง" in csv           # the measured concrete volume
        i18n.set_language("en")
        csv = takeoff_csv(res)
        assert "Category" in csv and "Cement (50 kg bags)" in csv
    finally:
        i18n.set_language(before)


def test_item_arguments_are_translated_too():
    scene = Scene()
    tag_faces(_box(scene, size=(0.2, 0.2, 3.0)), "IfcColumn", "C1", 1)
    tag_faces(_box(scene, x0=5.0, size=(4.0, 0.07, 2.6)), "IfcWall", "W1", 2)
    res = compute_takeoff(scene)
    before = i18n.current_language()
    try:
        i18n.set_language("th")
        from core.takeoff import display_item
        conc = _rows(res, "Concrete — {part}")[0]
        assert display_item(conc) == "คอนกรีต — เสา"
        bricks = _rows(res, "Bricks — {type}")[0]
        assert display_item(bricks) == "อิฐ — อิฐมอญ (ก่อครึ่งแผ่น)"
        assert "คอนกรีต — เสา" in takeoff_csv(res)
    finally:
        i18n.set_language(before)


def test_snapshot_and_diff_track_a_design_change():
    from core.takeoff import diff_snapshots, snapshot
    scene = Scene()
    tag_faces(_box(scene, size=(0.2, 0.2, 3.0)), "IfcColumn", "C1", 1)
    snap = snapshot(compute_takeoff(scene))
    assert snap["rows"] and "ts" in snap
    assert diff_snapshots(snap, snapshot(compute_takeoff(scene))) == []
    tag_faces(_box(scene, x0=5.0, size=(0.2, 0.2, 3.0)), "IfcColumn", "C2", 2)
    delta = diff_snapshots(snap, snapshot(compute_takeoff(scene)))
    assert delta and all(d["delta"] >= 0 for d in delta)
    conc = next(d for d in delta if d["item"] == "Concrete — {part}")
    assert abs(conc["old_net"] - 0.12) < 1e-5
    assert abs(conc["new_net"] - 0.24) < 1e-5


# ---- Phase G: earthwork, tile pattern, zone grouping -------------------------

def test_earthwork_for_a_footing():
    scene = Scene()
    tag_faces(_box(scene, size=(1.0, 1.0, 0.3)), "IfcFooting", "F1", 1)
    res = compute_takeoff(scene, TakeoffOptions(earthwork=True))
    exc = _rows(res, "Excavation (m³)")
    assert exc and exc[0].estimated
    # depth = 0 (base at grade) + 0.30 working; 1×1×0.3×1.25 = 0.375
    assert abs(exc[0].net - 0.375) < 1e-5
    assert _rows(res, "Backfill (m³)")


def test_earthwork_is_off_by_default():
    scene = Scene()
    tag_faces(_box(scene, size=(1.0, 1.0, 0.3)), "IfcFooting", "F1", 1)
    assert not _rows(compute_takeoff(scene), "Excavation (m³)")


def test_diagonal_tiles_waste_more_than_straight():
    scene = Scene()
    tag_faces(_box(scene, size=(4.0, 3.0, 0.15)), "IfcSlab", "S", 1)
    straight = compute_takeoff(scene, TakeoffOptions(tile_pattern="straight"))
    diagonal = compute_takeoff(scene, TakeoffOptions(tile_pattern="diagonal"))
    ts = _rows(straight, "Floor tiles {w}×{l} cm")[0]
    td = _rows(diagonal, "Floor tiles {w}×{l} cm")[0]
    assert abs(ts.net - td.net) < 1e-9          # same net area
    assert td.order > ts.order                  # 15% vs 10% waste


def test_rows_group_by_floor_zone():
    from core.takeoff import rows_by_zone
    scene = Scene()
    h = History(scene)
    faces = _box(scene, size=(0.2, 0.2, 3.0))
    tag_faces(faces, "IfcColumn", "C1", 1)
    h.execute(MakeGroupCommand(faces, list(scene.mesh.edges)))
    scene.groups[-1].name = "Floor 1"
    loose = scene.mesh.add_face([V(10, 0), V(13, 0), V(13, 2), V(10, 2)])
    tag_faces([loose], "IfcSlab", "S", 2)
    res = compute_takeoff(scene)
    zones = rows_by_zone(res, scene)
    assert "Floor 1" in zones and "" in zones   # named zone + general
    assert any(r.item == "Concrete — {part}" for r in zones["Floor 1"])