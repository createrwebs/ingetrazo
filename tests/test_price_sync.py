# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Marco Sumari Tellez and IngeTrazo contributors.
"""MOC price sync, tested offline: parsing, the conservative name+unit
mapping, latest-month-wins, the mirror override, failure handling."""
from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from core import price_sync as ps
from core.prices import PriceBook, SRC_MOC
from core.takeoff import TakeoffRow


def _rec(name, unit, price, year=2026, month=9, province="กรุงเทพมหานคร"):
    return {"com_name_th": name, "units": unit, "price": price,
            "year": year, "month": month, "province_name": province,
            "province_code": "10", "com_code": "x"}


def test_parse_keeps_good_rows_and_drops_junk():
    raw = json.dumps([_rec("ปูนซีเมนต์ปอร์ตแลนด์", "ถุง", 150),
                      {"com_name_th": "broken", "price": "abc"},
                      "junk", _rec("ทรายหยาบ", "ลบ.ม.", 520)]).encode()
    recs = ps.parse_records(raw)
    assert len(recs) == 2
    with pytest.raises(ValueError):
        ps.parse_records(b'{"not": "a list"}')


def test_mapping_is_conservative_and_latest_month_wins():
    recs = ps.parse_records(json.dumps([
        _rec("ปูนซีเมนต์ปอร์ตแลนด์ 50 กก.", "ถุง", 140, month=8),
        _rec("ปูนซีเมนต์ปอร์ตแลนด์ 50 กก.", "ถุง", 150, month=9),
        _rec("ทรายหยาบ", "ลบ.ม.", 520),
        _rec("อิฐมอญ", "ก้อน", 2.5),
        _rec("อิฐมวลเบา 7.5 ซม.", "ก้อน", 26),
        _rec("เหล็กเส้นข้ออ้อย", "เส้น", 200),      # wrong unit → skipped
        _rec("เหล็กเส้นข้ออ้อย", "กก.", 28),
        _rec("ปูนซีเมนต์ผสม ก่อ ฉาบ", "ถุง", 160),
    ]).encode())
    entries = ps.map_records(recs, "10")
    cement = entries["Cement (50 kg bags)|bag50"]
    assert cement["thb"] == 150 and cement["date"] == "2569-09"
    assert cement["source"] == SRC_MOC
    assert entries["Sand (m³)|m3"]["thb"] == 520
    clay = "Bricks — {type}|piece|type=Clay brick (half-brick wall)"
    aac = "Bricks — {type}|piece|type=AAC block 7.5 cm"
    assert entries[clay]["thb"] == 2.5 and entries[aac]["thb"] == 26
    assert "Rebar steel (kg)|kg" in entries
    assert entries["Laying cement (50 kg bags)|bag50"]["thb"] == 160
    assert entries["Plaster cement (50 kg bags)|bag50"]["thb"] == 160


def test_the_book_reads_the_synced_layer():
    entries = ps.map_records([{"name": "ปูนซีเมนต์ปอร์ตแลนด์", "unit": "ถุง",
                               "price": 150.0, "year": 2026, "month": 9,
                               "province_name": "กรุงเทพมหานคร"}], "10")
    book = PriceBook(online=entries)
    row = TakeoffRow("concrete", "Cement (50 kg bags)", "bag50")
    p = book.get(row)
    assert p.thb == 150 and p.source == SRC_MOC
    assert p.province == "กรุงเทพมหานคร"


def test_the_mirror_override_replaces_the_network(tmp_path, monkeypatch):
    mirror = tmp_path / "feed.json"
    mirror.write_text(json.dumps([_rec("ทรายหยาบ", "ลบ.ม.", 510)]),
                      encoding="utf-8")
    monkeypatch.setenv("INGETRAZO_PRICES", str(mirror))
    recs = ps.fetch_prices("10")
    assert recs[0]["price"] == 510


def test_sync_failure_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setenv("INGETRAZO_PRICES", str(tmp_path / "missing.json"))
    got = []
    sync = ps.PriceSync()
    sync.finished.connect(lambda e, m, k: got.append((e, m, k)))
    sync._work("10")                      # synchronous, same thread
    assert got and got[0][0] == {} and "failed" in got[0][1]


def test_sync_success_emits_entries(tmp_path, monkeypatch):
    mirror = tmp_path / "feed.json"
    mirror.write_text(json.dumps([_rec("ทรายหยาบ", "ลบ.ม.", 510)]),
                      encoding="utf-8")
    monkeypatch.setenv("INGETRAZO_PRICES", str(mirror))
    got = []
    sync = ps.PriceSync()
    sync.finished.connect(lambda e, m, k: got.append((e, m, k)))
    sync._work("10")
    entries, message, kwargs = got[0]
    assert entries["Sand (m³)|m3"]["thb"] == 510
    assert message == "Synced {n} prices from MOC ({province})"
    assert kwargs["province"] == "กรุงเทพมหานคร"