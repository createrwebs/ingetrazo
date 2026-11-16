# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Marco Sumari Tellez and IngeTrazo contributors.
"""The auto-tag assistant: proposed IFC classes for untagged groups.

core.bim_suggest proposes from shape; this dialog lists the proposals
with a checkbox and an editable class each, and applies only what the
user confirms. Nothing is tagged silently — a wrong guess costs one
click, a silent wrong tag would corrupt the takeoff.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel,
                               QPushButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout)

from core.bim import IFC_CLASSES, IFC_CLASS_NAMES, tag_group
from core.bim_suggest import suggestions
from core.i18n import tr


class TagSuggestDialog(QDialog):
    def __init__(self, window) -> None:
        super().__init__(window)
        self._window = window
        self._rows: list = []
        self.setWindowTitle(tr("Suggest BIM tags"))
        self.resize(660, 480)
        lay = QVBoxLayout(self)
        hint = QLabel(tr(
            "Proposals from geometry — review and tick what to tag. Nothing "
            "is applied until you press Apply. Group loose geometry first "
            "(Ctrl+G); untagged loose faces are counted below."))
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels([tr("Tag"), tr("Group"),
                                   tr("Proposed class"), tr("Size (m)")])
        self.tree.setColumnWidth(0, 50)
        self.tree.setColumnWidth(1, 200)
        self.tree.setColumnWidth(2, 220)
        lay.addWidget(self.tree, 1)
        self._info = QLabel("")
        lay.addWidget(self._info)
        row = QHBoxLayout()
        for label, slot in ((tr("Tick all"), lambda: self._tick(True)),
                            (tr("Untick all"), lambda: self._tick(False)),
                            (tr("Apply tagged"), self._apply),
                            (tr("Close"), self.close)):
            b = QPushButton(label)
            b.setAutoDefault(False)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.refresh()

    def _scene(self):
        return self._window.viewport.scene

    def refresh(self) -> None:
        self.tree.clear()
        data = suggestions(self._scene())
        self._rows = []
        for s in data["groups"]:
            it = QTreeWidgetItem(["", s["name"] or tr("(unnamed)"), "", ""])
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(0, Qt.Checked)
            combo = QComboBox()
            for cls in IFC_CLASSES:
                combo.addItem(f"{cls} — {tr(IFC_CLASS_NAMES[cls])}", cls)
            i = combo.findData(s["class"])
            if i >= 0:
                combo.setCurrentIndex(i)
            self.tree.setItemWidget(it, 2, combo)
            it.setText(3, f"{s['dx']:.2f} × {s['dy']:.2f} × {s['dz']:.2f}")
            self.tree.addTopLevelItem(it)
            self._rows.append((it, combo, s["group"]))
        self._info.setText(tr(
            "{n} proposals · {loose} untagged loose faces (group them first)",
            n=len(self._rows), loose=data["loose_untagged"]))

    def _tick(self, on: bool) -> None:
        for it, _c, _g in self._rows:
            it.setCheckState(0, Qt.Checked if on else Qt.Unchecked)

    def _apply(self) -> None:
        scene = self._scene()
        n = 0
        for it, combo, g in self._rows:
            if it.checkState(0) != Qt.Checked:
                continue
            cls = combo.currentData()
            tag_group(g, cls, g.name or cls.removeprefix("Ifc"))
            n += 1
        if n:
            scene.version += 1
            self._window.viewport.update()
            panel = getattr(getattr(self._window, "bim_tray", None),
                            "bim", None)
            if panel is not None:
                panel.refresh()
        self._window.statusBar().showMessage(
            tr("Tagged {n} groups", n=n), 4000)
        self.refresh()