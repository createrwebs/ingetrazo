# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Marco Sumari Tellez and IngeTrazo contributors.
"""Excel export — SpreadsheetML 2003 (.xls), several worksheets, no deps.

The takeoff, shopping list, BOQ and the schedules in ONE workbook that
Excel and LibreOffice open directly. SpreadsheetML is plain XML: no
openpyxl, no xlwt — the project's minimal-stack rule. Thai text is UTF-8;
quantities are typed as numbers so Excel sums and sorts them.
"""
from __future__ import annotations

from xml.sax.saxutils import escape

from core.bim import IFC_CLASS_NAMES
from core.i18n import tr
from core.takeoff import CATEGORY_LABELS, UNIT_LABELS, display_item

_HEADER = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<?mso-application progid="Excel.Sheet"?>\n'
           '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" '
           'xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">\n')


def _cell(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<Cell><Data ss:Type="Number">{value}</Data></Cell>'
    return f'<Cell><Data ss:Type="String">{escape(str(value))}</Data></Cell>'


def _row(values):
    return "<Row>" + "".join(_cell(v) for v in values) + "</Row>"


def _sheet(name, rows):
    safe = "".join(c for c in str(name) if c not in '[]:*?/\\')[:31] or "Sheet"
    return (f'<Worksheet ss:Name="{escape(safe)}"><Table>'
            + "".join(_row(r) for r in rows) + "</Table></Worksheet>\n")


def takeoff_excel(result, boq=None, rooms=None, paints=None,
                  openings=None, rebar=None, ffe=None) -> str:
    """Build the workbook XML. Optional sections are included when given."""
    sheets = []
    rows = [[tr("Category"), tr("Item"), tr("Net quantity"), tr("Waste %"),
             tr("Order quantity"), tr("Unit"), tr("Status")]]
    for r in result.rows:
        waste = (result.options.waste.get(r.waste_key, 0.0)
                 if result.options else 0.0)
        rows.append([tr(CATEGORY_LABELS.get(r.category, r.category)),
                     display_item(r), round(r.net, 4), waste,
                     round(r.order, 3), tr(UNIT_LABELS.get(r.unit, r.unit)),
                     tr("estimate") if r.estimated else tr("measured")])
    sheets.append(_sheet(tr("Materials by work"), rows))

    rows = [[tr("Item"), tr("Net quantity"), tr("Order quantity"), tr("Unit")]]
    for r in result.shopping:
        rows.append([display_item(r), round(r.net, 4), round(r.order, 3),
                     tr(UNIT_LABELS.get(r.unit, r.unit))])
    sheets.append(_sheet(tr("Shopping list"), rows))

    if boq:
        rows = [[tr("Kind"), tr("Item"), tr("Quantity"), tr("Unit"),
                 tr("Rate (THB)"), tr("Amount (THB)"), tr("Source")]]
        for l in boq["lines"]:
            rows.append([tr("Material") if l.kind == "material"
                         else tr("Labor"), display_item(l), round(l.qty, 3),
                         tr(UNIT_LABELS.get(l.unit, l.unit)), l.rate,
                         l.amount, l.source])
        for key, label in (("material_total", "Material total"),
                           ("labor_total", "Labor total"),
                           ("subtotal", "Subtotal"), ("overhead", "Overhead"),
                           ("profit", "Profit"), ("vat", "VAT"),
                           ("grand", "Grand total")):
            rows.append(["", tr(label), "", "", "", round(boq[key], 2), ""])
        sheets.append(_sheet("BOQ", rows))

    if rooms:
        rows = [[tr("Room"), tr("floor area"), tr("Height"),
                 tr("Perimeter (skirting)"), tr("Wall area (gross)"),
                 tr("Ceiling area")]]
        for r in rooms:
            rows.append([r["name"], round(r["floor_m2"], 3),
                         round(r["height_m"], 3), round(r["perimeter_m"], 3),
                         round(r["wall_m2"], 3), round(r["ceiling_m2"], 3)])
        sheets.append(_sheet(tr("Rooms (IfcSpace)"), rows))

    if paints:
        rows = [[tr("Material"), tr("Area (m²)"), tr("Faces")]]
        for g in paints:
            rows.append([g["label"], round(g["area"], 3), g["faces"]])
        sheets.append(_sheet(tr("Paint by material"), rows))

    if openings:
        rows = [[tr("Type"), tr("Name"), tr("Width (m)"), tr("Height (m)"),
                 tr("Count"), tr("Area (m²)")]]
        for g in openings:
            rows.append([tr(IFC_CLASS_NAMES.get(g["class"], g["class"])),
                         g["name"], g["width"], g["height"], g["count"],
                         round(g["area"], 3)])
        sheets.append(_sheet(tr("Doors & windows schedule"), rows))

    if rebar:
        rows = [[tr("Diameter (mm)"), tr("Weight (kg)"), tr("Length (m)"),
                 tr("Stock bars (10 m)")]]
        for g in rebar:
            rows.append([g["diameter_mm"], round(g["weight_kg"], 2),
                         round(g["length_m"], 2), g["bars"]])
        sheets.append(_sheet(tr("Rebar by diameter"), rows))

    if ffe:
        rows = [[tr("Item"), tr("Count")]]
        for e in ffe:
            rows.append([e["name"], e["count"]])
        sheets.append(_sheet(tr("Furniture & fixtures"), rows))

    return _HEADER + "".join(sheets) + "</Workbook>\n"