# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Marco Sumari Tellez and IngeTrazo contributors.
"""Prices and BOQ: the layered lookup (override > online > bundled), the
verified 2568 baselines, type-aware brick/tile/roof prices, the supplier
CSV round-trip, and the bill math with optional markups."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QVector3D

from core import i18n
from core.bim import tag_faces
from core.prices import (PriceBook, SRC_CSV, SRC_MANUAL, SRC_MOC,
                         SRC_VERIFIED, boq_csv, compute_boq)
from core.scene import Scene
from core.takeoff import TakeoffOptions, TakeoffRow, compute_takeoff, labor_base
from formats import igz


def V(x, y, z=0.0):
    return QVector3D(x, y, z)


def _box(scene, x0=0.0, size=(2.0, 1.0, 3.0)):
    sx, sy, sz = size
    c = [V(x0 + dx * sx, dy * sy, dz * sz)
         for dz in (0, 1) for dy in (0, 1) for dx in (0, 1)]
    quads = [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4),
             (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)]
    return [scene.mesh.add_face([c[i] for i in q]) for q in quads]


def _house():
    scene = Scene()
    tag_faces(_box(scene, size=(4.0, 0.07, 2.6)), "IfcWall", "W1", 1)
    tag_faces(_box(scene, x0=10.0, size=(4.0, 3.0, 0.15)), "IfcSlab", "S1", 2)
    return scene


def _row(item, unit="bag50", **args):
    return TakeoffRow("concrete", item, unit, item_args=args)


def test_the_verified_2568_baselines():
    book = PriceBook()
    cement = book.get(_row("Cement (50 kg bags)"))
    assert cement.thb == 148.0 and cement.source == SRC_VERIFIED
    assert book.get(_row("Sand (m³)", "m3")).thb == 508.0
    assert book.get(_row("Gravel (m³)", "m3")).thb == 620.0
    assert book.get(_row("Laying cement (50 kg bags)")).thb == 157.0


def test_measurement_rows_carry_no_price():
    book = PriceBook()
    assert book.get(_row("Concrete — {part}", "m3", part="Column")) is None
    assert book.get(_row("Water (L)", "L")) is None
    assert book.get(_row("Lean concrete (m³)", "m3")) is None


def test_brick_and_tile_prices_follow_the_type():
    book = PriceBook()
    clay = book.get(_row("Bricks — {type}", "piece",
                         type="Clay brick (half-brick wall)"))
    aac = book.get(_row("Bricks — {type}", "piece", type="AAC block 7.5 cm"))
    assert clay.thb == 2.0 and aac.thb == 25.0
    t60 = book.get(_row("Floor tiles {w}×{l} cm", "piece", w="60", l="60"))
    t30 = book.get(_row("Floor tiles {w}×{l} cm", "piece", w="30", l="30"))
    assert abs(t60.thb - 417.0 * 0.36) < 0.01
    assert abs(t30.thb - 417.0 * 0.09) < 0.01


def test_overrides_win_and_travel():
    book = PriceBook()
    book.set_override("Cement (50 kg bags)", "bag50", 165.0)
    p = book.get(_row("Cement (50 kg bags)"))
    assert p.thb == 165.0 and p.source == SRC_MANUAL
    back = PriceBook(overrides=book.overrides_dict())
    assert back.get(_row("Cement (50 kg bags)")).thb == 165.0


def test_the_online_layer_sits_between_override_and_bundled():
    key = "Cement (50 kg bags)|bag50"
    online = {key: {"thb": 150.0, "source": SRC_MOC, "date": "2569-01",
                    "province": "กรุงเทพมหานคร"}}
    book = PriceBook(online=online)
    assert book.get(_row("Cement (50 kg bags)")).thb == 150.0
    book.set_override("Cement (50 kg bags)", "bag50", 165.0)
    assert book.get(_row("Cement (50 kg bags)")).thb == 165.0


def test_supplier_csv_round_trip():
    scene = _house()
    res = compute_takeoff(scene)
    book = PriceBook()
    tpl = book.template_csv(res.shopping)
    assert "Cement (50 kg bags)" in tpl and "148.00" in tpl
    quote = tpl.replace("148.00", "160.00")
    taken = book.import_csv(quote, res.shopping)
    assert taken >= 1
    p = book.get(_row("Cement (50 kg bags)"))
    assert p.thb == 160.0 and p.source == SRC_CSV

def test_the_bill_math_with_markups():
    scene = _house()
    opts = TakeoffOptions()
    res = compute_takeoff(scene, opts)
    work = labor_base(scene, opts)
    book = PriceBook()
    boq = compute_boq(res, work, book, opts, 10.0, 5.0, 7.0)
    assert boq["material_total"] > 0 and boq["labor_total"] > 0
    masonry = next(l for l in boq["lines"] if l.item == "Masonry labor")
    assert abs(masonry.amount - round(work["masonry_m2"] * 94.0, 2)) < 0.02
    sub = boq["material_total"] + boq["labor_total"]
    assert abs(boq["subtotal"] - sub) < 0.01
    assert abs(boq["grand"] - sub * 1.10 * 1.05 * 1.07) < 0.05
    plain = compute_boq(res, work, book, opts)
    assert abs(plain["grand"] - plain["subtotal"]) < 0.01


def test_labor_rates_are_editable_too():
    scene = _house()
    opts = TakeoffOptions()
    res = compute_takeoff(scene, opts)
    work = labor_base(scene, opts)
    book = PriceBook()
    book.set_override("labor", "Masonry labor", 100.0)
    boq = compute_boq(res, work, book, opts)
    masonry = next(l for l in boq["lines"] if l.item == "Masonry labor")
    assert abs(masonry.amount - round(work["masonry_m2"] * 100.0, 2)) < 0.02


def test_prices_travel_with_the_document(tmp_path):
    scene = _house()
    book = PriceBook()
    book.set_override("Cement (50 kg bags)", "bag50", 165.0)
    scene.takeoff = {"concrete_mix": "1:2:4",
                     "prices": book.overrides_dict()}
    p = tmp_path / "prices.igz"
    igz.save_scene(scene, p)
    scene2 = Scene()
    igz.load_into(scene2, p)
    assert scene2.takeoff["prices"] == book.overrides_dict()
    opts = TakeoffOptions.from_dict(scene2.takeoff)   # prices key ignored
    assert opts.concrete_mix == "1:2:4"


def test_boq_csv_speaks_thai():
    scene = _house()
    opts = TakeoffOptions()
    res = compute_takeoff(scene, opts)
    boq = compute_boq(res, labor_base(scene, opts), PriceBook(), opts)
    before = i18n.current_language()
    try:
        i18n.set_language("th")
        text = boq_csv(boq)
        assert "ปูนซีเมนต์ (ถุง 50 กก.)" in text
        assert "ค่าแรงก่ออิฐ" in text and "ยอดรวมสุทธิ" in text
    finally:
        i18n.set_language(before)