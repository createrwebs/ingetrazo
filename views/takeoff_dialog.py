# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Marco Sumari Tellez and IngeTrazo contributors.
"""Construction takeoff — the material list of the tagged model.

Three doors to the same numbers: the BIM panel button, Tools ▸
Construction takeoff…, and the command search. The takeoff is computed
on demand (the #857 lesson: never re-measure on every selection), every
row audits back to its BIM objects — click a source row and the model
selects and frames that object — and the rates travel with the document
(``scene.takeoff``), so a colleague opening the file estimates on the
same basis.

The dialog is a thin shell over core/takeoff.py: results tree, merged
shopping list, the rates form, and CSV / PDF / clipboard exports. The
PDF goes through QPdfWriter (QtGui), like the sheet composer — it works
in builds without QtPrintSupport.
"""
from __future__ import annotations

import datetime

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPdfWriter, QPageSize
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
                               QDoubleSpinBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QPushButton, QScrollArea,
                               QTabWidget, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from core import takeoff as tk
from core.bim import IFC_CLASS_NAMES
from core.i18n import tr
from core.prices import (PriceBook, boq_csv, compute_boq, load_online_cache,
                         save_online_cache)
from core.price_sync import PROVINCES, PriceSync
from core.excel import takeoff_excel
from core.rooms import (ROOM_DEFAULTS, ffe_schedule, openings_schedule,
                        paint_schedule, preflight, rebar_schedule,
                        room_options, room_schedule)
from core.takeoff import (BRICKS, CATEGORY_LABELS, CONCRETE_MIXES,
                          MORTAR_MIXES, PRESET_LABELS, ROOF_TYPES,
                          UNIT_LABELS, TakeoffOptions, compute_takeoff,
                          diff_snapshots, display_item, labor_base,
                          preset_options, rows_by_zone, snapshot,
                          takeoff_csv)
from views.filedialogs import file_dialogs


class TakeoffDialog(QDialog):
    """Non-modal: the model stays editable, «Recalculate» re-measures."""

    def __init__(self, window) -> None:
        super().__init__(window)
        self._window = window
        self._result = None
        self._memo: dict = {}
        self._fields: dict = {}
        self._room_fields: dict = {}
        self.setWindowTitle(tr("Construction takeoff"))
        self.resize(880, 640)
        lay = QVBoxLayout(self)
        self._warn = QLabel("")
        self._warn.setWordWrap(True)
        self._warn.setStyleSheet("color: #b45a00;")
        lay.addWidget(self._warn)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)
        self.tree = self._make_tree()
        results_page = QWidget()
        rlay = QVBoxLayout(results_page)
        rlay.setContentsMargins(0, 0, 0, 0)
        grow = QHBoxLayout()
        grow.addWidget(QLabel(tr("Group by")))
        self._group_mode = QComboBox()
        self._group_mode.addItem(tr("Work category"), "category")
        self._group_mode.addItem(tr("Floor / zone"), "zone")
        self._group_mode.currentIndexChanged.connect(self._refill_results)
        grow.addWidget(self._group_mode)
        grow.addStretch(1)
        rlay.addLayout(grow)
        rlay.addWidget(self.tree, 1)
        self.tabs.addTab(results_page, tr("Materials by work"))
        self.shop_tree = self._make_tree()
        self.tabs.addTab(self.shop_tree, tr("Shopping list"))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._build_settings())
        self.tabs.addTab(scroll, tr("Rates & options"))
        self._book = None
        self._boq = None
        self.tabs.addTab(self._build_boq(), tr("Prices / BOQ"))
        self.tabs.addTab(self._build_schedules(), tr("Schedules"))
        self.tabs.addTab(self._build_diff(), tr("Compare"))
        row = QHBoxLayout()
        for label, slot in (
                (tr("Recalculate"), self.recompute),
                (tr("Export CSV…"), self._export_csv),
                (tr("Export PDF…"), self._export_pdf),
                (tr("Export Excel…"), self._export_excel),
                (tr("Copy table"), self._copy),
                (tr("Close"), self.close)):
            b = QPushButton(label)
            b.setAutoDefault(False)
            b.setDefault(False)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self._load_options(TakeoffOptions.from_dict(
            getattr(self._scene(), "takeoff", None)))
        self._load_rooms(room_options(self._scene()))

    def _scene(self):
        return self._window.viewport.scene

    def _make_tree(self) -> QTreeWidget:
        tree = QTreeWidget()
        tree.setColumnCount(5)
        tree.setHeaderLabels([tr("Item"), tr("Net quantity"),
                              tr("Order quantity"), tr("Unit"), tr("Note")])
        tree.setColumnWidth(0, 330)
        tree.setColumnWidth(1, 90)
        tree.setColumnWidth(2, 90)
        tree.setColumnWidth(3, 110)
        tree.setRootIsDecorated(True)
        tree.setAlternatingRowColors(True)
        tree.itemClicked.connect(self._on_pick)
        return tree

    # ---- Results -------------------------------------------------------------
    def _fill_tree(self, tree: QTreeWidget, rows: list,
                   by_category: bool) -> None:
        tree.clear()
        if by_category:
            cats: dict = {}
            for r in rows:
                cats.setdefault(r.category, []).append(r)
            for cat in tk.CATEGORY_ORDER:
                if cat not in cats:
                    continue
                top = QTreeWidgetItem(
                    [tr(CATEGORY_LABELS.get(cat, cat)), "", "", "", ""])
                font = top.font(0)
                font.setBold(True)
                top.setFont(0, font)
                tree.addTopLevelItem(top)
                for r in cats[cat]:
                    self._add_row(top, r)
                top.setExpanded(True)
        else:
            for r in rows:
                self._add_row(tree.invisibleRootItem(), r)

    def _refill_results(self, *_) -> None:
        if self._result is None:
            return
        if self._group_mode.currentData() == "zone":
            self.tree.clear()
            zones = rows_by_zone(self._result, self._scene())
            for z in sorted(zones, key=lambda s: (s == "", s)):
                top = QTreeWidgetItem([z or tr("(general)"), "", "", "", ""])
                font = top.font(0)
                font.setBold(True)
                top.setFont(0, font)
                self.tree.addTopLevelItem(top)
                for r in zones[z]:
                    self._add_row(top, r)
                top.setExpanded(True)
        else:
            self._fill_tree(self.tree, self._result.rows, True)

    def _add_row(self, parent, r) -> None:
        status = tr("estimate") if r.estimated else tr("measured")
        note = tr(r.note) if r.note else ""
        item = QTreeWidgetItem([
            display_item(r), f"{r.net:.3f}", f"{r.order:.2f}",
            tr(UNIT_LABELS.get(r.unit, r.unit)),
            f"{status} — {note}" if note else status])
        item.setData(0, Qt.UserRole, ("row", r))
        if r.estimated:
            item.setForeground(4, QColor(180, 90, 0))
        parent.addChild(item)
        for obj, n in r.sources[:40]:
            label = obj.get("name") or tr(
                IFC_CLASS_NAMES.get(obj["class"], obj["class"]))
            src = QTreeWidgetItem([f"↳ {label}", f"{n:.3f}", "", "",
                                   obj["class"]])
            src.setData(0, Qt.UserRole, ("obj", obj))
            src.setForeground(0, QColor(90, 90, 90))
            item.addChild(src)

    def _on_pick(self, item, _col) -> None:
        """Click a row (or its source) → select that object in the model
        and frame it, the BIM panel's own pick behaviour."""
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        kind, payload = data
        if kind == "obj":
            obj = payload
        else:
            obj = payload.sources[0][0] if payload.sources else None
        if obj is not None:
            self._select(obj)

    def _select(self, obj) -> None:
        vp = self._window.viewport
        scene = self._scene()
        target = obj.get("group") or obj.get("owner")
        if target is not None:
            from core import outliner
            from views.outliner import open_to
            path = outliner.path_to(scene.groups, target)
            if "owner" in obj and path is not None:
                path = path + [target]
            if path is None or not open_to(vp, path):
                vp.flash_status(tr(
                    "A group around it is hidden or locked: show or unlock "
                    "it to select inside"), 3500)
                return
        scene.selection.clear()
        if "group" in obj:
            scene.selection.add(obj["group"])
        elif "owner" in obj:
            scene.selection.update(
                f for f in scene.mesh.faces
                if (f.attrs.get("ifc") or {}).get("id") == obj.get("tag_id"))
        else:
            scene.selection.update(obj.get("faces") or [])
        scene.bump_view()
        vp.update()
        self._window._on_zoom_selection()

    def recompute(self) -> None:
        """Measure the tagged model with the form's rates; keep the rates
        with the document only when they actually changed (opening the
        dialog must not dirty an untouched file)."""
        scene = self._scene()
        opts = self._collect_options()
        as_dict = opts.to_dict()
        # Compare against the EFFECTIVE basis (defaults merged with what
        # the document keeps): opening the dialog on an untouched file
        # must not dirty it, only a real change writes and bumps. Keys
        # that are not options (the price overrides) travel along.
        doc = dict(getattr(scene, "takeoff", None) or {})
        extra = {k: v for k, v in doc.items()
                 if k not in TakeoffOptions.__dataclass_fields__}
        current = TakeoffOptions.from_dict(doc)
        rooms = self._collect_rooms()
        cur_rooms = room_options(scene)
        rooms_changed = any(rooms.get(k) != cur_rooms.get(k) for k in rooms)
        if as_dict != current.to_dict() or rooms_changed:
            as_dict.update(extra)
            if rooms_changed:
                as_dict["rooms"] = rooms
            scene.takeoff = as_dict
            scene.version += 1
        self._result = compute_takeoff(scene, opts, self._memo)
        self._refill_results()
        self._fill_tree(self.shop_tree, self._result.shopping, False)
        warnings = [tr(w, **kw) for w, kw in self._result.warnings]
        if self._result.untagged_groups:
            warnings.append(tr(
                "{n} groups have no BIM tag — tag them to include their "
                "materials", n=self._result.untagged_groups))
        if not self._result.rows:
            warnings.append(tr(
                "No tagged BIM objects yet. Select faces or a group and "
                "tag them in the BIM panel (IfcWall, IfcSlab, …)."))
        self._warn.setText("\n".join(warnings))
        self._warn.setVisible(bool(warnings))
        self._refresh_boq()
        self._refresh_schedules()
        self._refresh_diff_combo()

    # ---- Rates form ----------------------------------------------------------
    def _build_settings(self) -> QWidget:
        page = QWidget()
        form = QVBoxLayout(page)
        presets = QHBoxLayout()
        for key, label in PRESET_LABELS.items():
            b = QPushButton(tr(label))
            b.setAutoDefault(False)
            b.clicked.connect(lambda _c=False, k=key: self._apply_preset(k))
            presets.addWidget(b)
        presets.addStretch(1)
        form.addLayout(presets)

        def combo(key, choices, labels=None):
            c = QComboBox()
            for i, ch in enumerate(choices):
                c.addItem(tr(labels[i]) if labels else str(ch), ch)
            self._fields[key] = ("combo", c)
            return c

        def spin(key, lo, hi, step=1.0, decimals=1):
            s = QDoubleSpinBox()
            s.setRange(lo, hi)
            s.setSingleStep(step)
            s.setDecimals(decimals)
            self._fields[key] = ("spin", s)
            return s

        def check(key):
            c = QCheckBox()
            self._fields[key] = ("check", c)
            return c

        def group(title):
            g = QGroupBox(title)
            f = QFormLayout(g)
            form.addWidget(g)
            return f

        f = group(tr("Concrete"))
        f.addRow(tr("Mix (cement:sand:gravel)"),
                 combo("concrete_mix", list(CONCRETE_MIXES)))
        f.addRow(tr("Ready-mixed concrete"), check("ready_mix"))
        f.addRow(tr("Slab fallback thickness (mm)"),
                 spin("slab_thickness_mm", 50, 500, 10, 0))

        f = group(tr("Masonry wall"))
        f.addRow(tr("Wall material"),
                 combo("brick", list(BRICKS),
                       [BRICKS[k]["label"] for k in BRICKS]))
        f.addRow(tr("Laying mortar mix"),
                 combo("laying_mix", list(MORTAR_MIXES)))

        f = group(tr("Plaster"))
        f.addRow(tr("Thickness (mm)"), spin("plaster_mm", 0, 50, 1, 0))
        f.addRow(tr("Sides"), spin("plaster_sides", 0, 2, 1, 0))
        f.addRow(tr("Mix (cement:sand)"),
                 combo("plaster_mix", list(MORTAR_MIXES)))

        f = group(tr("Reinforcement"))
        f.addRow(tr("Mode"), combo(
            "rebar_mode", ["auto", "ratios", "tagged"],
            ["Auto (tagged bars if any)", "Ratios (kg/m³ estimates)",
             "Tagged bars only"]))
        for cls in TakeoffOptions().rebar_ratios:
            f.addRow(tr(IFC_CLASS_NAMES.get(cls, cls)) + " (kg/m³)",
                     spin(f"rebar_ratios.{cls}", 0, 400, 5, 0))
        f = group(tr("Paint"))
        f.addRow(tr("Paint walls"), check("paint"))
        f.addRow(tr("Sides"), spin("paint_sides", 0, 4, 1, 0))
        f.addRow(tr("Coverage (m²/gallon/coat)"),
                 spin("paint_coverage", 5, 100, 1, 0))
        f.addRow(tr("Primer coats"), spin("primer_coats", 0, 3, 1, 0))
        f.addRow(tr("Topcoat coats"), spin("top_coats", 0, 5, 1, 0))

        f = group(tr("Floor finish"))
        f.addRow(tr("Finish"), combo("floor_finish", ["tile", "none"],
                                     ["Tile", "None"]))
        f.addRow(tr("Laying pattern"), combo(
            "tile_pattern", ["straight", "diagonal"],
            ["Straight", "Diagonal (more waste)"]))
        f.addRow(tr("Tile width (m)"), spin("tile_w", 0.05, 1.2, 0.05, 2))
        f.addRow(tr("Tile length (m)"), spin("tile_l", 0.05, 1.2, 0.05, 2))
        f.addRow(tr("Adhesive (kg/m²)"),
                 spin("adhesive_kg_m2", 0, 15, 0.5, 1))

        f = group(tr("Roof"))
        f.addRow(tr("Roof covering"), combo(
            "roof_type", list(ROOF_TYPES),
            [ROOF_TYPES[k]["label"] for k in ROOF_TYPES]))
        f.addRow(tr("Sheets per m² (0 = type default)"),
                 spin("roof_per_m2", 0, 20, 0.1, 2))
        f.addRow(tr("Truss steel (kg/m², estimate)"),
                 spin("truss_kg_m2", 0, 60, 1, 1))

        f = group(tr("Formwork"))
        f.addRow(tr("Compute formwork"), check("formwork"))
        f.addRow(tr("Plywood reuses"), spin("plywood_reuse", 1, 10, 1, 0))

        f = group(tr("Bedding"))
        f.addRow(tr("Lean + sand bed"), check("bedding"))
        f.addRow(tr("Lean concrete (mm)"), spin("lean_mm", 0, 200, 5, 0))
        f.addRow(tr("Lean mix"), combo("lean_mix", list(CONCRETE_MIXES)))
        f.addRow(tr("Sand bed (mm)"), spin("sand_bed_mm", 0, 200, 5, 0))

        f = group(tr("Earthwork"))
        f.addRow(tr("Excavation + backfill"), check("earthwork"))
        f.addRow(tr("Soil bulking factor"),
                 spin("soil_bulking", 1.0, 2.0, 0.05, 2))
        f.addRow(tr("Working depth below footing (m)"),
                 spin("excavation_working_m", 0, 1.0, 0.05, 2))

        f = group(tr("Room finishes (schedules)"))
        self._room_fields = {}

        def rcombo(key, choices, labels):
            c = QComboBox()
            for i, ch in enumerate(choices):
                c.addItem(tr(labels[i]), ch)
            self._room_fields[key] = ("combo", c)
            return c

        def rspin(key, lo, hi, step, dec):
            s = QDoubleSpinBox()
            s.setRange(lo, hi)
            s.setSingleStep(step)
            s.setDecimals(dec)
            self._room_fields[key] = ("spin", s)
            return s

        f.addRow(tr("Floor"), rcombo("floor", ["tile", "none"],
                                     ["Tile", "None"]))
        f.addRow(tr("Walls"), rcombo("wall",
                 ["paint", "wallpaper", "tile", "none"],
                 ["Paint", "Wallpaper", "Tile", "None"]))
        f.addRow(tr("Ceiling"), rcombo("ceiling", ["paint", "none"],
                                       ["Paint", "None"]))
        sc = QCheckBox()
        self._room_fields["skirting"] = ("check", sc)
        f.addRow(tr("Skirting"), sc)
        f.addRow(tr("Wallpaper coverage (m²/roll)"),
                 rspin("wallpaper_roll_m2", 1, 20, 0.5, 1))
        f.addRow(tr("Tile width (m)"), rspin("tile_w", 0.05, 1.2, 0.05, 2))
        f.addRow(tr("Tile length (m)"), rspin("tile_l", 0.05, 1.2, 0.05, 2))
        f.addRow(tr("Adhesive (kg/m²)"),
                 rspin("adhesive_kg_m2", 0, 15, 0.5, 1))
        f.addRow(tr("Coverage (m²/gallon/coat)"),
                 rspin("paint_coverage", 5, 100, 1, 0))
        f.addRow(tr("Primer coats"), rspin("primer_coats", 0, 3, 1, 0))
        f.addRow(tr("Topcoat coats"), rspin("top_coats", 0, 5, 1, 0))
        f.addRow(tr("Waste / loss (%)"), rspin("waste", 0, 50, 1, 1))

        f = group(tr("Waste / loss (%)"))
        waste_labels = {
            "concrete": "Concrete", "brick": "Bricks",
            "mortar": "Laying mortar", "plaster": "Plaster",
            "rebar": "Rebar", "paint": "Paint", "tile": "Tiles",
            "adhesive": "Adhesive", "roof": "Roof sheets",
            "truss": "Truss steel", "formwork": "Formwork",
            "lean": "Lean concrete", "sand": "Sand"}
        for key, label in waste_labels.items():
            f.addRow(tr(label), spin(f"waste.{key}", 0, 50, 1, 1))
        form.addStretch(1)
        return page

    def _apply_preset(self, key: str) -> None:
        self._load_options(preset_options(key))
        self.recompute()

    def _load_options(self, opts: TakeoffOptions) -> None:
        for key, (kind, w) in self._fields.items():
            if "." in key:
                base, sub = key.split(".", 1)
                value = getattr(opts, base).get(sub)
            else:
                value = getattr(opts, key)
            if value is None:
                continue
            if kind == "combo":
                i = w.findData(value)
                if i >= 0:
                    w.setCurrentIndex(i)
            elif kind == "spin":
                w.setValue(float(value))
            else:
                w.setChecked(bool(value))

    def _collect_options(self) -> TakeoffOptions:
        opts = TakeoffOptions()
        for key, (kind, w) in self._fields.items():
            if kind == "combo":
                value = w.currentData()
            elif kind == "spin":
                value = w.value()
            else:
                value = w.isChecked()
            if "." in key:
                base, sub = key.split(".", 1)
                getattr(opts, base)[sub] = value
            else:
                setattr(opts, key, value)
        return opts

    # ---- Prices / BOQ (off by default) ---------------------------------------
    def _build_boq(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        top = QHBoxLayout()
        self._boq_on = QCheckBox(tr("Show prices (BOQ)"))
        self._boq_on.setChecked(
            str(QSettings().value("Takeoff/boq_enabled", "0")) == "1")
        self._boq_on.toggled.connect(self._boq_toggled)
        top.addWidget(self._boq_on)
        top.addStretch(1)
        for label, slot in ((tr("Import price CSV…"), self._import_prices),
                            (tr("Export price template…"),
                             self._export_template),
                            (tr("Export BOQ CSV…"), self._export_boq)):
            b = QPushButton(label)
            b.setAutoDefault(False)
            b.clicked.connect(slot)
            top.addWidget(b)
        lay.addLayout(top)
        sync_row = QHBoxLayout()
        sync_row.addWidget(QLabel(tr("Province")))
        self._province = QComboBox()
        for code, name in PROVINCES.items():
            self._province.addItem(f"{name} ({code})", code)
        saved = str(QSettings().value("Takeoff/province", "10"))
        i = self._province.findData(saved)
        if i >= 0:
            self._province.setCurrentIndex(i)
        sync_row.addWidget(self._province)
        self._sync_btn = QPushButton(tr("Sync prices online"))
        self._sync_btn.setAutoDefault(False)
        self._sync_btn.clicked.connect(self._sync_prices)
        sync_row.addWidget(self._sync_btn)
        self._sync_status = QLabel("")
        self._sync_status.setWordWrap(True)
        sync_row.addWidget(self._sync_status, 1)
        lay.addLayout(sync_row)
        self._sync = PriceSync(self)
        self._sync.finished.connect(self._on_sync_done)
        self._boq_hint = QLabel(tr(
            "Prices are off. Turn them on for the bill of quantities: rates "
            "start from the 2568 standard price book and market estimates, "
            "every rate is editable per project, and a supplier quote CSV "
            "can fill them all at once."))
        self._boq_hint.setWordWrap(True)
        lay.addWidget(self._boq_hint)
        self._boq_table = QTreeWidget()
        self._boq_table.setColumnCount(7)
        self._boq_table.setHeaderLabels(
            [tr("Kind"), tr("Item"), tr("Quantity"), tr("Unit"),
             tr("Rate (THB)"), tr("Amount (THB)"), tr("Source")])
        self._boq_table.setColumnWidth(0, 70)
        self._boq_table.setColumnWidth(1, 250)
        self._boq_table.setColumnWidth(2, 80)
        self._boq_table.setColumnWidth(3, 70)
        self._boq_table.setColumnWidth(4, 90)
        self._boq_table.setColumnWidth(5, 110)
        self._boq_table.itemChanged.connect(self._boq_rate_edited)
        lay.addWidget(self._boq_table, 1)
        mk = QHBoxLayout()
        self._oh = QDoubleSpinBox()
        self._oh.setRange(0, 100)
        self._oh.setValue(float(QSettings().value("Takeoff/overhead", 0)))
        self._pr = QDoubleSpinBox()
        self._pr.setRange(0, 100)
        self._pr.setValue(float(QSettings().value("Takeoff/profit", 0)))
        self._vat = QCheckBox(tr("VAT 7%"))
        self._vat.setChecked(str(QSettings().value("Takeoff/vat", "0")) == "1")
        mk.addWidget(QLabel(tr("Overhead %")))
        mk.addWidget(self._oh)
        mk.addWidget(QLabel(tr("Profit %")))
        mk.addWidget(self._pr)
        mk.addWidget(self._vat)
        mk.addStretch(1)
        lay.addLayout(mk)
        self._boq_totals = QLabel("")
        self._boq_totals.setWordWrap(True)
        lay.addWidget(self._boq_totals)
        self._oh.valueChanged.connect(self._markup_changed)
        self._pr.valueChanged.connect(self._markup_changed)
        self._vat.toggled.connect(self._markup_changed)
        return page

    def _boq_toggled(self, on: bool) -> None:
        QSettings().setValue("Takeoff/boq_enabled", "1" if on else "0")
        self._refresh_boq()

    def _markup_changed(self, *_) -> None:
        st = QSettings()
        st.setValue("Takeoff/overhead", self._oh.value())
        st.setValue("Takeoff/profit", self._pr.value())
        st.setValue("Takeoff/vat", "1" if self._vat.isChecked() else "0")
        self._refresh_boq()

    def _refresh_boq(self) -> None:
        if getattr(self, "_boq_on", None) is None:
            return                      # called before the tab exists
        on = self._boq_on.isChecked()
        self._boq_hint.setVisible(not on)
        self._boq_table.setVisible(on)
        self._boq_totals.setVisible(on)
        if not on or self._result is None:
            return
        scene = self._scene()
        opts = self._result.options
        doc = getattr(scene, "takeoff", None) or {}
        self._book = PriceBook(doc.get("prices"), load_online_cache())
        work = labor_base(scene, opts, self._memo)
        self._boq = compute_boq(self._result, work, self._book, opts,
                                self._oh.value(), self._pr.value(),
                                7.0 if self._vat.isChecked() else 0.0)
        self._boq_table.blockSignals(True)
        self._boq_table.clear()
        for line in self._boq["lines"]:
            item = QTreeWidgetItem([
                tr("Material") if line.kind == "material" else tr("Labor"),
                display_item(line), f"{line.qty:.3f}",
                tr(UNIT_LABELS.get(line.unit, line.unit)),
                f"{line.rate:.2f}", f"{line.amount:,.2f}",
                self._source_label(line.source)])
            item.setFlags(item.flags() | Qt.ItemIsEditable)
            item.setData(0, Qt.UserRole, line)
            self._boq_table.addTopLevelItem(item)
        self._boq_table.blockSignals(False)
        b = self._boq
        self._boq_totals.setText(tr(
            "Materials {mat} · Labor {lab} · Subtotal {sub} · Overhead {oh} "
            "· Profit {pr} · VAT {vat} · GRAND TOTAL {grand} THB",
            mat=f"{b['material_total']:,.2f}",
            lab=f"{b['labor_total']:,.2f}", sub=f"{b['subtotal']:,.2f}",
            oh=f"{b['overhead']:,.2f}", pr=f"{b['profit']:,.2f}",
            vat=f"{b['vat']:,.2f}", grand=f"{b['grand']:,.2f}"))

    def _sync_prices(self) -> None:
        code = self._province.currentData()
        QSettings().setValue("Takeoff/province", code)
        self._sync_btn.setEnabled(False)
        self._sync_status.setText(tr("Syncing…"))
        self._sync.start(code)

    def _on_sync_done(self, entries: dict, message: str, kwargs: dict) -> None:
        self._sync_btn.setEnabled(True)
        self._sync_status.setText(tr(message, **kwargs))
        if entries:
            cache = load_online_cache()
            cache.update(entries)
            save_online_cache(cache)
            QSettings().setValue(
                "Takeoff/last_sync",
                datetime.datetime.now().isoformat(timespec="minutes"))
            self._refresh_boq()

    @staticmethod
    def _source_label(source: str) -> str:
        return {"dpt-2568": tr("Standard price book 2568"),
                "estimate": tr("Market estimate"),
                "manual": tr("User"),
                "csv": tr("Supplier CSV"),
                "moc": tr("MOC province average")}.get(source, source)

    def _boq_rate_edited(self, item, col) -> None:
        """Double-click a rate, type the shop's price — it sticks to the
        project (scene.takeoff["prices"]) and wins over every baseline."""
        if col != 4 or self._book is None:
            return
        line = item.data(0, Qt.UserRole)
        if line is None:
            return
        try:
            rate = float(item.text(4).replace(",", ""))
        except ValueError:
            self._refresh_boq()
            return
        if rate < 0:
            self._refresh_boq()
            return
        if line.kind == "material":
            self._book.set_override(line.item, line.unit, rate,
                                    args=line.item_args)
        else:
            self._book.set_override("labor", line.item, rate)
        self._save_prices()
        self._refresh_boq()

    def _save_prices(self) -> None:
        scene = self._scene()
        doc = dict(getattr(scene, "takeoff", None) or {})
        doc["prices"] = self._book.overrides_dict()
        scene.takeoff = doc
        scene.version += 1

    def _import_prices(self) -> None:
        if self._result is None:
            return
        path, _ = file_dialogs.getOpenFileName(
            self, tr("Import price CSV"), "",
            tr("CSV (*.csv);;All files (*)"))
        if not path:
            return
        try:
            with open(path, encoding="utf-8-sig") as fh:
                text = fh.read()
        except OSError as exc:
            self._window.statusBar().showMessage(str(exc), 4000)
            return
        if self._book is None:
            doc = getattr(self._scene(), "takeoff", None) or {}
            self._book = PriceBook(doc.get("prices"), load_online_cache())
        n = self._book.import_csv(text, self._result.shopping)
        self._save_prices()
        self._refresh_boq()
        self._window.statusBar().showMessage(
            tr("Imported {n} prices", n=n), 4000)

    def _export_template(self) -> None:
        if self._result is None:
            return
        path, _ = file_dialogs.getSaveFileName(
            self, tr("Export price template"), "prices.csv",
            tr("CSV (*.csv);;All files (*)"))
        if not path:
            return
        if self._book is None:
            self._book = PriceBook()
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            fh.write(self._book.template_csv(self._result.shopping))
        self._window.statusBar().showMessage(
            tr("Price template exported to {path}", path=path), 4000)

    def _export_boq(self) -> None:
        if not self._boq:
            return
        path, _ = file_dialogs.getSaveFileName(
            self, tr("Export BOQ CSV"), "boq.csv",
            tr("CSV (*.csv);;All files (*)"))
        if not path:
            return
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            fh.write(boq_csv(self._boq))
        self._window.statusBar().showMessage(
            tr("BOQ exported to {path}", path=path), 4000)

    # ---- Exports -------------------------------------------------------------
    def _export_csv(self) -> None:
        if self._result is None:
            return
        path, _ = file_dialogs.getSaveFileName(
            self, tr("Export takeoff CSV"), "takeoff.csv",
            tr("CSV (*.csv);;All files (*)"))
        if not path:
            return
        # UTF-8 BOM: Excel on Windows reads Thai correctly with it.
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            fh.write(takeoff_csv(self._result))
        self._window.statusBar().showMessage(
            tr("Takeoff exported to {path}", path=path), 4000)

    def _export_pdf(self) -> None:
        if self._result is None:
            return
        path, _ = file_dialogs.getSaveFileName(
            self, tr("Export takeoff PDF"), "takeoff.pdf",
            tr("PDF (*.pdf);;All files (*)"))
        if not path:
            return
        write_takeoff_pdf(self._result, path)
        self._window.statusBar().showMessage(
            tr("Takeoff PDF exported to {path}", path=path), 4000)

    def _export_excel(self) -> None:
        if self._result is None:
            return
        path, _ = file_dialogs.getSaveFileName(
            self, tr("Export Excel"), "takeoff.xls",
            tr("Excel XML (*.xls);;All files (*)"))
        if not path:
            return
        scene = self._scene()
        xml = takeoff_excel(
            self._result, boq=self._boq,
            rooms=room_schedule(scene, self._memo),
            paints=paint_schedule(scene, self._memo),
            openings=openings_schedule(scene, self._memo),
            rebar=rebar_schedule(scene, self._memo),
            ffe=ffe_schedule(scene))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(xml)
        self._window.statusBar().showMessage(
            tr("Excel exported to {path}", path=path), 4000)

    def _export_schedules_pdf(self) -> None:
        if self._result is None:
            return
        path, _ = file_dialogs.getSaveFileName(
            self, tr("Export finishes PDF"), "finishes.pdf",
            tr("PDF (*.pdf);;All files (*)"))
        if not path:
            return
        scene = self._scene()
        write_schedules_pdf(
            path, rooms=room_schedule(scene, self._memo),
            paints=paint_schedule(scene, self._memo),
            openings=openings_schedule(scene, self._memo),
            rebar=rebar_schedule(scene, self._memo),
            ffe=ffe_schedule(scene))
        self._window.statusBar().showMessage(
            tr("Finishes PDF exported to {path}", path=path), 4000)

    def _copy(self) -> None:
        if self._result is None:
            return
        head = "\t".join([tr("Item"), tr("Net quantity"),
                          tr("Order quantity"), tr("Unit")])
        lines = [head]

        def line(r):
            return "\t".join([display_item(r), f"{r.net:.3f}",
                              f"{r.order:.2f}",
                              tr(UNIT_LABELS.get(r.unit, r.unit))])

        for r in self._result.rows:
            lines.append(line(r))
        lines.append("")
        lines.append(tr("Shopping totals (merged, rounded for purchase)"))
        for r in self._result.shopping:
            lines.append(line(r))
        QApplication.clipboard().setText("\n".join(lines))
        self._window.statusBar().showMessage(
            tr("Copied {n} rows to the clipboard", n=len(self._result.rows)),
            3000)

    # ---- Room options (schedules) --------------------------------------------
    def _collect_rooms(self) -> dict:
        out = {}
        for key, (kind, w) in self._room_fields.items():
            if kind == "combo":
                out[key] = w.currentData()
            elif kind == "spin":
                out[key] = w.value()
            else:
                out[key] = w.isChecked()
        return out

    def _load_rooms(self, ro: dict) -> None:
        for key, (kind, w) in self._room_fields.items():
            value = ro.get(key, ROOM_DEFAULTS.get(key))
            if value is None:
                continue
            if kind == "combo":
                i = w.findData(value)
                if i >= 0:
                    w.setCurrentIndex(i)
            elif kind == "spin":
                w.setValue(float(value))
            else:
                w.setChecked(bool(value))

    # ---- Schedules tab --------------------------------------------------------
    def _build_schedules(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        note = QLabel(tr(
            "Companion schedules — separate views, not added to the main "
            "takeoff: room finishes overlap the element-based paint and "
            "tile lines. Room walls are perimeter × height (openings not "
            "deducted)."))
        note.setWordWrap(True)
        lay.addWidget(note)
        srow = QHBoxLayout()
        spdf = QPushButton(tr("Export finishes PDF…"))
        spdf.setAutoDefault(False)
        spdf.clicked.connect(self._export_schedules_pdf)
        srow.addWidget(spdf)
        srow.addStretch(1)
        lay.addLayout(srow)
        self._sched_tree = QTreeWidget()
        self._sched_tree.setColumnCount(4)
        self._sched_tree.setHeaderLabels(
            [tr("Item"), tr("Quantity"), tr("Unit"), tr("Note")])
        self._sched_tree.setColumnWidth(0, 340)
        self._sched_tree.setColumnWidth(1, 100)
        self._sched_tree.setColumnWidth(2, 110)
        self._sched_tree.itemClicked.connect(self._on_sched_pick)
        lay.addWidget(self._sched_tree, 1)
        return page

    def _on_sched_pick(self, item, _col) -> None:
        obj = item.data(0, Qt.UserRole)
        if isinstance(obj, dict) and "class" in obj:
            self._select(obj)

    def _refresh_schedules(self) -> None:
        if getattr(self, "_sched_tree", None) is None or self._result is None:
            return
        scene = self._scene()
        tree = self._sched_tree
        tree.clear()

        def section(title):
            top = QTreeWidgetItem([title, "", "", ""])
            font = top.font(0)
            font.setBold(True)
            top.setFont(0, font)
            tree.addTopLevelItem(top)
            top.setExpanded(True)
            return top

        def line(parent, texts, obj=None):
            it = QTreeWidgetItem(texts)
            if obj is not None:
                it.setData(0, Qt.UserRole, obj)
            parent.addChild(it)
            return it

        rooms = room_schedule(scene, self._memo)
        top = section(tr("Rooms (IfcSpace)"))
        if not rooms:
            line(top, [tr("No rooms tagged — tag a room volume as IfcSpace"),
                       "", "", ""])
        for r in rooms:
            head = line(top, [r["name"] or tr("Room"),
                              f"{r['floor_m2']:.2f}", "m²",
                              tr("floor area")], r["obj"])
            line(head, [tr("Height"), f"{r['height_m']:.2f}", "m", ""])
            line(head, [tr("Perimeter (skirting)"),
                        f"{r['perimeter_m']:.2f}", "m", ""])
            line(head, [tr("Wall area (gross)"), f"{r['wall_m2']:.2f}",
                        "m²", tr("openings not deducted")])
            line(head, [tr("Ceiling area"), f"{r['ceiling_m2']:.2f}",
                        "m²", ""])
            for mat in r["materials"]:
                label = tr(mat["item"],
                           **{k: tr(str(v)) for k, v in mat["args"].items()})
                line(head, [label, f"{mat['qty']:.2f}",
                            tr(UNIT_LABELS.get(mat["unit"], mat["unit"])),
                            tr(mat["note"]) if mat["note"] else ""])
        top = section(tr("Paint by material"))
        paints = paint_schedule(scene, self._memo)
        if not paints:
            line(top, [tr("No painted wall/covering faces"), "", "", ""])
        for g in paints:
            it = line(top, [g["label"], f"{g['area']:.2f}", "m²",
                            tr("{n} faces", n=g["faces"])])
            for name in g["objects"][:20]:
                line(it, [f"↳ {name}", "", "", ""])
        top = section(tr("Doors & windows schedule"))
        opens = openings_schedule(scene, self._memo)
        if not opens:
            line(top, [tr("No doors or windows tagged"), "", "", ""])
        for g in opens:
            cls = tr(IFC_CLASS_NAMES.get(g["class"], g["class"]))
            line(top, [f"{g['name'] or cls} ({cls})", f"{g['count']}",
                       tr("sets"),
                       f"{g['width']:.2f} × {g['height']:.2f} m · "
                       f"{g['area']:.2f} m²"])
        top = section(tr("Rebar by diameter"))
        bars = rebar_schedule(scene, self._memo)
        if not bars:
            line(top, [tr("No tagged rebar — ratios are used in the takeoff"),
                       "", "", ""])
        for g in bars:
            d = f"Ø{g['diameter_mm']}" if g["diameter_mm"] else tr("unknown Ø")
            line(top, [d, f"{g['weight_kg']:.2f}", "kg",
                       tr("{n} stock bars (10 m) · {m} m total",
                          n=g["bars"], m=f"{g['length_m']:.1f}")])
        top = section(tr("Furniture & fixtures"))
        ffe = ffe_schedule(scene)
        if not ffe:
            line(top, [tr("No named components placed"), "", "", ""])
        for e in ffe:
            line(top, [e["name"], f"{e['count']}", tr("sets"), ""])
        top = section(tr("Preflight"))
        issues = preflight(scene, self._memo)
        if not issues:
            line(top, [tr("All checks passed"), "", "", ""])
        for msg, kw, obj in issues:
            line(top, [tr(msg, **kw), "", "", ""], obj)

    # ---- Compare tab (snapshots) ----------------------------------------------
    def _build_diff(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Snapshot")))
        self._snap_combo = QComboBox()
        row.addWidget(self._snap_combo, 1)
        save = QPushButton(tr("Save snapshot of current takeoff"))
        save.setAutoDefault(False)
        save.clicked.connect(self._save_snapshot)
        row.addWidget(save)
        compare = QPushButton(tr("Compare with current"))
        compare.setAutoDefault(False)
        compare.clicked.connect(self._run_diff)
        row.addWidget(compare)
        lay.addLayout(row)
        hint = QLabel(tr(
            "Snapshots freeze the takeoff so a design change can be priced: "
            "save one before editing, compare after. They travel with the "
            "document (last 10 kept)."))
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self._diff_tree = QTreeWidget()
        self._diff_tree.setColumnCount(5)
        self._diff_tree.setHeaderLabels(
            [tr("Item"), tr("Old order"), tr("New order"), tr("Delta"),
             tr("Unit")])
        self._diff_tree.setColumnWidth(0, 320)
        lay.addWidget(self._diff_tree, 1)
        self._diff_total = QLabel("")
        lay.addWidget(self._diff_total)
        return page

    def _snapshots(self) -> list:
        doc = getattr(self._scene(), "takeoff", None) or {}
        snaps = doc.get("snapshots")
        return snaps if isinstance(snaps, list) else []

    def _refresh_diff_combo(self) -> None:
        if getattr(self, "_snap_combo", None) is None:
            return
        self._snap_combo.clear()
        for i, s in enumerate(self._snapshots()):
            self._snap_combo.addItem(
                f"{s.get('ts', '?')} · {len(s.get('rows', []))} "
                + tr("rows"), i)

    def _save_snapshot(self) -> None:
        if self._result is None:
            return
        scene = self._scene()
        doc = dict(getattr(scene, "takeoff", None) or {})
        snaps = doc.get("snapshots")
        snaps = list(snaps) if isinstance(snaps, list) else []
        snaps.append(snapshot(self._result))
        doc["snapshots"] = snaps[-10:]
        scene.takeoff = doc
        scene.version += 1
        self._refresh_diff_combo()
        self._snap_combo.setCurrentIndex(self._snap_combo.count() - 1)
        self._window.statusBar().showMessage(tr("Snapshot saved"), 3000)

    def _run_diff(self) -> None:
        self._diff_tree.clear()
        idx = self._snap_combo.currentData()
        snaps = self._snapshots()
        if idx is None or not (0 <= idx < len(snaps)) or self._result is None:
            self._diff_total.setText(
                tr("Save a snapshot first, then compare."))
            return
        delta = diff_snapshots(snaps[idx], snapshot(self._result))
        if not delta:
            self._diff_total.setText(
                tr("No quantity changes since the snapshot."))
            return
        for d in delta:
            label = tr(d["item"],
                       **{k: tr(str(v)) for k, v in d["args"].items()})
            it = QTreeWidgetItem([
                label, f"{d['old_order']:.2f}", f"{d['new_order']:.2f}",
                f"{d['delta']:+.2f}",
                tr(UNIT_LABELS.get(d["unit"], d["unit"]))])
            if d["delta"] > 0:
                it.setForeground(3, QColor(180, 60, 0))
            elif d["delta"] < 0:
                it.setForeground(3, QColor(0, 120, 60))
            self._diff_tree.addTopLevelItem(it)
        self._diff_total.setText(tr("{n} items changed", n=len(delta)))


def write_takeoff_pdf(result, path) -> None:
    """The A4 material report, through QPdfWriter (QtGui) like the sheet
    composer — no QtPrintSupport needed. Categories, the shopping totals,
    the warnings and the standing disclaimer; Thai renders with the
    application font (Leelawadee UI on Windows, views/script_fonts.py)."""
    from PySide6.QtCore import QRect
    writer = QPdfWriter(str(path))
    writer.setPageSize(QPageSize(QPageSize.A4))
    writer.setResolution(150)
    writer.setTitle(tr("Construction takeoff"))
    p = QPainter(writer)
    try:
        W, H = writer.width(), writer.height()
        margin = int(W * 0.06)
        usable = W - 2 * margin
        family = QApplication.font().family()
        f_title = QFont(family, 15)
        f_title.setBold(True)
        f_cat = QFont(family, 10)
        f_cat.setBold(True)
        f_body = QFont(family, 8)
        f_small = QFont(family, 7)
        y = margin

        def ensure(h):
            nonlocal y
            if y + h > H - margin:
                writer.newPage()
                y = margin

        def text_row(texts, font, fractions):
            nonlocal y
            p.setFont(font)
            fm = p.fontMetrics()
            rects, heights = [], []
            x = margin
            for t, fr in zip(texts, fractions):
                w = int(usable * fr)
                r = fm.boundingRect(QRect(x, 0, w - 6, 10000),
                                    Qt.TextWordWrap | Qt.AlignLeft, str(t))
                heights.append(r.height())
                rects.append((x, w))
                x += w
            h = max(heights) + 4
            ensure(h)
            for t, (xx, w) in zip(texts, rects):
                p.drawText(QRect(xx, y, w - 6, h),
                           Qt.TextWordWrap | Qt.AlignLeft, str(t))
            y += h

        p.setFont(f_title)
        p.drawText(QRect(margin, y, usable, 44), Qt.AlignLeft,
                   tr("Construction takeoff"))
        y += 48
        p.setFont(f_small)
        p.drawText(QRect(margin, y, usable, 20), Qt.AlignLeft,
                   datetime.date.today().isoformat())
        y += 26
        fractions = [0.40, 0.12, 0.12, 0.16, 0.20]
        header = [tr("Item"), tr("Net quantity"), tr("Order quantity"),
                  tr("Unit"), tr("Note")]

        def row_texts(r):
            status = tr("estimate") if r.estimated else tr("measured")
            note = tr(r.note) if r.note else ""
            return [display_item(r), f"{r.net:.3f}",
                    f"{r.order:.2f}", tr(UNIT_LABELS.get(r.unit, r.unit)),
                    f"{status} — {note}" if note else status]

        cats: dict = {}
        for r in result.rows:
            cats.setdefault(r.category, []).append(r)
        for cat in tk.CATEGORY_ORDER:
            if cat not in cats:
                continue
            ensure(30)
            p.setFont(f_cat)
            p.drawText(QRect(margin, y, usable, 24), Qt.AlignLeft,
                       tr(CATEGORY_LABELS.get(cat, cat)))
            y += 26
            text_row(header, f_small, fractions)
            for r in cats[cat]:
                text_row(row_texts(r), f_body, fractions)
            y += 8
        ensure(30)
        p.setFont(f_cat)
        p.drawText(QRect(margin, y, usable, 24), Qt.AlignLeft,
                   tr("Shopping totals (merged, rounded for purchase)"))
        y += 26
        text_row(header, f_small, fractions)
        for r in result.shopping:
            text_row(row_texts(r), f_body, fractions)
        y += 10
        for wtext, kw in result.warnings:
            text_row([tr(wtext, **kw)], f_small, [1.0])
        disc = tr("Estimates for ordering — not structural design. "
                  "Verify with the engineer's drawings.")
        p.setFont(f_small)
        h = p.fontMetrics().boundingRect(
            QRect(margin, 0, usable, 10000), Qt.TextWordWrap, disc).height()
        ensure(h + 10)
        p.drawText(QRect(margin, y, usable, h + 6), Qt.TextWordWrap, disc)
    finally:
        p.end()


def write_schedules_pdf(path, rooms=(), paints=(), openings=(), rebar=(),
                        ffe=()) -> None:
    """The finishes / schedules report (rooms, paint, openings, rebar,
    FF&E) as an A4 PDF — the interior designer's presentation sheet."""
    from PySide6.QtCore import QRect
    writer = QPdfWriter(str(path))
    writer.setPageSize(QPageSize(QPageSize.A4))
    writer.setResolution(150)
    writer.setTitle(tr("Finishes & schedules"))
    p = QPainter(writer)
    try:
        W, H = writer.width(), writer.height()
        margin = int(W * 0.06)
        usable = W - 2 * margin
        family = QApplication.font().family()
        f_title = QFont(family, 15)
        f_title.setBold(True)
        f_sec = QFont(family, 11)
        f_sec.setBold(True)
        f_body = QFont(family, 9)
        y = margin

        def ensure(h):
            nonlocal y
            if y + h > H - margin:
                writer.newPage()
                y = margin

        def line(text, font, indent=0):
            nonlocal y
            p.setFont(font)
            r = p.fontMetrics().boundingRect(
                QRect(margin + indent, 0, usable - indent, 10000),
                Qt.TextWordWrap, text)
            h = r.height() + 3
            ensure(h)
            p.drawText(QRect(margin + indent, y, usable - indent, h),
                       Qt.TextWordWrap, text)
            y += h

        p.setFont(f_title)
        p.drawText(QRect(margin, y, usable, 44), Qt.AlignLeft,
                   tr("Finishes & schedules"))
        y += 50
        if rooms:
            line(tr("Rooms (IfcSpace)"), f_sec)
            for r in rooms:
                line(f"{r['name'] or tr('Room')}: " + tr(
                    "floor {f} m² · height {h} m · perimeter {p} m · "
                    "wall {w} m²", f=f"{r['floor_m2']:.2f}",
                    h=f"{r['height_m']:.2f}", p=f"{r['perimeter_m']:.2f}",
                    w=f"{r['wall_m2']:.2f}"), f_body)
                for m in r["materials"]:
                    lbl = tr(m["item"], **{k: tr(str(v))
                                           for k, v in m["args"].items()})
                    line(f"– {lbl}: {m['qty']:.2f} "
                         f"{tr(UNIT_LABELS.get(m['unit'], m['unit']))}",
                         f_body, 20)
            y += 8
        if paints:
            line(tr("Paint by material"), f_sec)
            for g in paints:
                line(f"{g['label']}: {g['area']:.2f} m² ({g['faces']})",
                     f_body)
            y += 8
        if openings:
            line(tr("Doors & windows schedule"), f_sec)
            for g in openings:
                cls = tr(IFC_CLASS_NAMES.get(g["class"], g["class"]))
                line(f"{g['name'] or cls} ({cls}): {g['count']} × "
                     f"{g['width']:.2f}×{g['height']:.2f} m", f_body)
            y += 8
        if rebar:
            line(tr("Rebar by diameter"), f_sec)
            for g in rebar:
                line(f"Ø{g['diameter_mm']} mm: {g['weight_kg']:.2f} kg · "
                     f"{g['bars']} " + tr("stock bars (10 m)"), f_body)
            y += 8
        if ffe:
            line(tr("Furniture & fixtures"), f_sec)
            for e in ffe:
                line(f"{e['name']}: {e['count']}", f_body)
        y += 10
        line(tr("Estimates for ordering — not structural design. Verify "
                "with the engineer's drawings."), f_body)
    finally:
        p.end()