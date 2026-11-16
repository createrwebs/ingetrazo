# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Marco Sumari Tellez and IngeTrazo contributors.
"""The takeoff dialog: the Tools menu door, the empty-model guidance,
rates that travel with the document, row→object selection, and the
CSV (UTF-8 BOM) / PDF exports."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QVector3D

from core import i18n
from core.bim import tag_faces
from core.scene import Scene
from views import takeoff_dialog as td


def V(x, y, z=0.0):
    return QVector3D(x, y, z)


def _box(scene, x0=0.0, size=(2.0, 1.0, 3.0)):
    sx, sy, sz = size
    c = [V(x0 + dx * sx, dy * sy, dz * sz)
         for dz in (0, 1) for dy in (0, 1) for dx in (0, 1)]
    quads = [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4),
             (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)]
    return [scene.mesh.add_face([c[i] for i in q]) for q in quads]


@pytest.fixture
def win():
    from views.main_window import MainWindow
    w = MainWindow()
    yield w
    w._saved_version = w.viewport.scene.version
    w.close()


def _tagged(win):
    tag_faces(_box(win.viewport.scene, size=(0.2, 0.2, 3.0)),
              "IfcColumn", "C1", 1)


def test_the_menu_door_opens_the_dialog_on_a_tagged_model(win):
    _tagged(win)
    win._on_takeoff()
    dlg = win._takeoff_dialog
    assert dlg._result is not None and dlg._result.rows
    assert dlg.tree.topLevelItemCount() > 0
    assert dlg.shop_tree.topLevelItemCount() > 0


def test_an_empty_model_points_at_the_bim_panel(win, monkeypatch):
    import views.main_window as mw
    notes = []
    monkeypatch.setattr(mw.QMessageBox, "information",
                        staticmethod(lambda *a, **k: notes.append(a)))
    win._on_takeoff()
    assert len(notes) == 1
    assert getattr(win, "_takeoff_dialog", None) is None


def test_rates_change_travels_with_the_document(win):
    _tagged(win)
    win._on_takeoff()
    dlg = win._takeoff_dialog
    kind, combo = dlg._fields["concrete_mix"]
    combo.setCurrentIndex(combo.findData("1:1.5:3"))
    dlg.recompute()
    assert win.viewport.scene.takeoff["concrete_mix"] == "1:1.5:3"
    assert dlg._result.options.concrete_mix == "1:1.5:3"


def test_opening_the_dialog_does_not_dirty_an_untouched_file(win):
    _tagged(win)
    before = win.viewport.scene.version
    win._on_takeoff()
    assert win.viewport.scene.version == before


def test_a_preset_switches_the_whole_basis(win):
    _tagged(win)
    win._on_takeoff()
    dlg = win._takeoff_dialog
    dlg._apply_preset("house_aac")
    assert dlg._collect_options().brick == "aac75"


def test_clicking_a_source_row_selects_the_object(win):
    _tagged(win)
    win._on_takeoff()
    dlg = win._takeoff_dialog
    row = dlg.tree.topLevelItem(0).child(0)
    src = row.child(0)
    assert src is not None
    dlg._on_pick(src, 0)
    assert len(win.viewport.scene.selection) > 0


def test_csv_export_has_the_excel_bom(win, tmp_path, monkeypatch):
    _tagged(win)
    win._on_takeoff()
    dlg = win._takeoff_dialog
    path = tmp_path / "takeoff.csv"
    monkeypatch.setattr(td.file_dialogs, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(path), "")))
    dlg._export_csv()
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")          # UTF-8 BOM for Excel
    assert b"Cement (50 kg bags)" in raw


def test_pdf_export_writes_a_real_pdf(win, tmp_path, monkeypatch):
    _tagged(win)
    win._on_takeoff()
    dlg = win._takeoff_dialog
    path = tmp_path / "takeoff.pdf"
    monkeypatch.setattr(td.file_dialogs, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(path), "")))
    dlg._export_pdf()
    raw = path.read_bytes()
    assert raw.startswith(b"%PDF") and len(raw) > 1000


def test_the_dialog_speaks_thai(win):
    _tagged(win)
    before = i18n.current_language()
    try:
        i18n.set_language("th")
        dlg = td.TakeoffDialog(win)
        assert dlg.windowTitle() == "ถอดแบบก่อสร้าง"
        dlg.recompute()
        top = dlg.tree.topLevelItem(0)
        assert top.text(0) == "งานคอนกรีต"
        dlg.close()
    finally:
        i18n.set_language(before)


# ---- BOQ (off by default) ----------------------------------------------------

def test_boq_is_off_by_default(win):
    QSettings().setValue("Takeoff/boq_enabled", "0")
    _tagged(win)
    win._on_takeoff()
    dlg = win._takeoff_dialog
    assert not dlg._boq_on.isChecked()
    assert dlg._boq is None


def test_boq_toggle_builds_the_bill(win):
    QSettings().setValue("Takeoff/boq_enabled", "0")
    _tagged(win)
    win._on_takeoff()
    dlg = win._takeoff_dialog
    dlg._boq_on.setChecked(True)
    assert dlg._boq is not None and dlg._boq["lines"]
    assert dlg._boq_table.topLevelItemCount() == len(dlg._boq["lines"])
    assert dlg._boq["grand"] > 0
    dlg._boq_on.setChecked(False)
    QSettings().setValue("Takeoff/boq_enabled", "0")


def test_editing_a_rate_sticks_to_the_project(win):
    QSettings().setValue("Takeoff/boq_enabled", "1")
    _tagged(win)
    win._on_takeoff()
    dlg = win._takeoff_dialog
    target = None
    for i in range(dlg._boq_table.topLevelItemCount()):
        it = dlg._boq_table.topLevelItem(i)
        line = it.data(0, Qt.UserRole)
        if line.kind == "material" and line.item == "Cement (50 kg bags)":
            target = it
            break
    assert target is not None
    target.setText(4, "165")
    prices = win.viewport.scene.takeoff.get("prices", {})
    assert prices["Cement (50 kg bags)|bag50"]["thb"] == 165.0
    QSettings().setValue("Takeoff/boq_enabled", "0")


def test_boq_csv_export(win, tmp_path, monkeypatch):
    QSettings().setValue("Takeoff/boq_enabled", "1")
    _tagged(win)
    win._on_takeoff()
    dlg = win._takeoff_dialog
    path = tmp_path / "boq.csv"
    monkeypatch.setattr(td.file_dialogs, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(path), "")))
    dlg._export_boq()
    text = path.read_text(encoding="utf-8-sig")
    assert "Cement (50 kg bags)" in text and "Grand total" in text
    QSettings().setValue("Takeoff/boq_enabled", "0")


def test_price_template_export_and_quote_import(win, tmp_path, monkeypatch):
    QSettings().setValue("Takeoff/boq_enabled", "1")
    _tagged(win)
    win._on_takeoff()
    dlg = win._takeoff_dialog
    tpl = tmp_path / "prices.csv"
    monkeypatch.setattr(td.file_dialogs, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(tpl), "")))
    dlg._export_template()
    text = tpl.read_text(encoding="utf-8-sig").replace("148.00", "160.00")
    quote = tmp_path / "quote.csv"
    quote.write_text(text, encoding="utf-8-sig")
    monkeypatch.setattr(td.file_dialogs, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(quote), "")))
    dlg._import_prices()
    prices = win.viewport.scene.takeoff["prices"]
    assert prices["Cement (50 kg bags)|bag50"]["thb"] == 160.0
    QSettings().setValue("Takeoff/boq_enabled", "0")


def test_sync_done_caches_and_reprices(win):
    QSettings().setValue("Takeoff/boq_enabled", "1")
    _tagged(win)
    win._on_takeoff()
    dlg = win._takeoff_dialog
    entries = {"Cement (50 kg bags)|bag50":
               {"thb": 150.0, "source": "moc", "date": "2569-09",
                "province": "กรุงเทพมหานคร"}}
    dlg._on_sync_done(entries, "Synced {n} prices from MOC ({province})",
                      {"n": 1, "province": "กรุงเทพมหานคร"})
    from core.prices import load_online_cache, save_online_cache
    assert load_online_cache()["Cement (50 kg bags)|bag50"]["thb"] == 150.0
    line = next(l for l in dlg._boq["lines"]
                if l.kind == "material" and l.item == "Cement (50 kg bags)")
    assert line.rate == 150.0
    save_online_cache({})                 # leave the session cache clean
    QSettings().setValue("Takeoff/boq_enabled", "0")


# ---- Phase F: schedules, room options, snapshots, auto-tag -------------------

def test_schedules_tab_has_all_sections(win):
    _tagged(win)
    tag_faces(_box(win.viewport.scene, x0=20, size=(4, 3, 2.8)),
              "IfcSpace", "Room", 2)
    win._on_takeoff()
    dlg = win._takeoff_dialog
    assert dlg._sched_tree.topLevelItemCount() == 6
    rooms_sec = dlg._sched_tree.topLevelItem(0)
    assert rooms_sec.childCount() >= 1


def test_room_option_change_persists(win):
    _tagged(win)
    win._on_takeoff()
    dlg = win._takeoff_dialog
    kind, combo = dlg._room_fields["wall"]
    combo.setCurrentIndex(combo.findData("wallpaper"))
    dlg.recompute()
    assert win.viewport.scene.takeoff["rooms"]["wall"] == "wallpaper"


def test_snapshot_save_and_diff_after_change(win):
    _tagged(win)
    win._on_takeoff()
    dlg = win._takeoff_dialog
    dlg._save_snapshot()
    assert len(dlg._snapshots()) == 1
    dlg._run_diff()
    assert dlg._diff_tree.topLevelItemCount() == 0   # unchanged yet
    tag_faces(_box(win.viewport.scene, x0=5, size=(0.2, 0.2, 3.0)),
              "IfcColumn", "C2", 2)
    dlg.recompute()
    dlg._run_diff()
    assert dlg._diff_tree.topLevelItemCount() > 0


def test_suggest_dialog_tags_only_confirmed(win):
    from core.history import History, MakeGroupCommand
    from views.tag_suggest_dialog import TagSuggestDialog
    scene = win.viewport.scene
    h = History(scene)
    faces = _box(scene, size=(4.0, 0.07, 2.6))
    h.execute(MakeGroupCommand(faces, list(scene.mesh.edges)))
    dlg = TagSuggestDialog(win)
    assert len(dlg._rows) == 1
    proposed_group = dlg._rows[0][2]
    dlg._apply()
    assert proposed_group.ifc["class"] == "IfcWall"
    dlg.close()


# ---- Phase G: zone grouping, Excel, finishes PDF ------------------------------

def test_zone_grouping_switch(win):
    _tagged(win)
    win._on_takeoff()
    dlg = win._takeoff_dialog
    dlg._group_mode.setCurrentIndex(dlg._group_mode.findData("zone"))
    assert dlg.tree.topLevelItemCount() >= 1
    dlg._group_mode.setCurrentIndex(dlg._group_mode.findData("category"))
    assert dlg.tree.topLevelItemCount() >= 1


def test_excel_export_writes_spreadsheetml(win, tmp_path, monkeypatch):
    _tagged(win)
    win._on_takeoff()
    dlg = win._takeoff_dialog
    path = tmp_path / "takeoff.xls"
    monkeypatch.setattr(td.file_dialogs, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(path), "")))
    dlg._export_excel()
    text = path.read_text(encoding="utf-8")
    assert text.startswith("<?xml") and "<Workbook" in text
    assert "Worksheet" in text


def test_finishes_pdf_export(win, tmp_path, monkeypatch):
    _tagged(win)
    tag_faces(_box(win.viewport.scene, x0=20, size=(4, 3, 2.8)),
              "IfcSpace", "Room", 2)
    win._on_takeoff()
    dlg = win._takeoff_dialog
    path = tmp_path / "finishes.pdf"
    monkeypatch.setattr(td.file_dialogs, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(path), "")))
    dlg._export_schedules_pdf()
    assert path.read_bytes().startswith(b"%PDF")