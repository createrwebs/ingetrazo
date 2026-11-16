# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Marco Sumari Tellez and IngeTrazo contributors.
"""Prices and BOQ — the money side of the takeoff, OFF by default.

The takeoff (core/takeoff.py) says WHAT to order; this module says what
it costs. Layers, top wins:

1. project overrides — the user's own edits and supplier-quote CSVs,
   kept with the document in ``scene.takeoff["prices"]``;
2. the online cache — province averages from the Ministry of Commerce
   open data (core/price_sync.py), when a sync has succeeded;
3. the bundled baseline — the verified 2568 standard prices (บัญชีราคา
   ค่าวัสดุและค่าแรงงาน กรมโยธาธิการและผังเมือง, via OneStockHome's
   tables: sand 508/m³, gravel 620/m³, Portland cement 148/bag,
   laying/plastering cement 157/bag, ready-mix 2399/m³; labor: clay
   brick 94/m², block 84/m², AAC 56/m², plaster 63/m², formwork
   139/m², tiling 158–184/m²) plus clearly-marked market estimates
   for everything else. Every price carries its source and date.

Prices are for ordering budgets, not contracts: they exclude VAT and
transport (as the standard book does) until the BOQ adds them.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

from core.takeoff import BRICKS, PLYWOOD_M2, ROOF_TYPES, UNIT_LABELS, display_item

#: Price sources, shown next to every number.
SRC_VERIFIED = "dpt-2568"      # บัญชีราคากลางกรมโยธาฯ 2568 (verified)
SRC_ESTIMATE = "estimate"      # market average, editable
SRC_MANUAL = "manual"          # typed by the user
SRC_CSV = "csv"                # imported supplier quote
SRC_MOC = "moc"                # Ministry of Commerce province average

#: Verified 2568 standard material prices (baht, excl. VAT/transport).
BUNDLED_VERIFIED = {
    ("Cement (50 kg bags)", "bag50"): 148.0,
    ("Laying cement (50 kg bags)", "bag50"): 157.0,
    ("Plaster cement (50 kg bags)", "bag50"): 157.0,
    ("Sand (m³)", "m3"): 508.0,
    ("Gravel (m³)", "m3"): 620.0,
    ("Ready-mixed concrete — {part}", "m3"): 2399.0,
}

#: Market averages for the rest — estimates, every one editable.
BUNDLED_ESTIMATE = {
    ("Rebar steel (kg)", "kg"): 27.0,
    ("Roof truss steel (kg, estimate)", "kg"): 40.0,
    ("Plywood formwork sheets (1.22×2.44 m)", "sheet"): 420.0,
    ("Primer (gallons)", "gallon"): 600.0,
    ("Topcoat (gallons)", "gallon"): 900.0,
    ("AAC adhesive (20 kg bags)", "bag20"): 200.0,
    ("Tile adhesive (20 kg bags)", "bag20"): 180.0,
    ("Doors (sets)", "set"): 3500.0,
    ("Windows (sets)", "set"): 3000.0,
}

#: Per-piece brick prices by wall material key (estimates).
BRICK_PRICES = {"mon_half": 2.0, "block7": 9.0, "block19": 14.0,
                "aac75": 25.0, "aac10": 32.0}
_BRICK_BY_LABEL = {v["label"]: k for k, v in BRICKS.items() if "label" in v}

#: Roof covering prices per sheet (estimates), by type label.
ROOF_PRICES = {v["label"]: p for v, p in zip(
    ROOF_TYPES.values(), (65.0, 180.0))}

#: Floor tile price per m² (estimate) — per-piece scales with the size.
TILE_PRICE_M2 = 417.0          # ≈ 150 baht per 60×60 cm piece

#: Verified 2568 masonry labor by wall material (baht/m²).
LABOR_MASONRY = {"mon_half": 94.0, "block7": 84.0, "block19": 103.0,
                 "aac75": 56.0, "aac10": 60.0}

#: Other labor rates (baht per work unit). The first three are the
#: verified 2568 book; the rest are market estimates, editable.
LABOR = {"plaster": 63.0, "formwork": 139.0, "tile": 184.0,
         "rebar_fix": 4.0, "concrete_place": 400.0, "paint": 45.0,
         "roof": 100.0}
LABOR_ESTIMATES = {"rebar_fix", "concrete_place", "paint", "roof"}


@dataclass
class Price:
    thb: float
    source: str = SRC_ESTIMATE
    date: str = "2568"
    province: str = ""


@dataclass
class BoqLine:
    kind: str                  # "material" | "labor"
    item: str                  # English source string for tr()
    item_args: dict = field(default_factory=dict)
    qty: float = 0.0
    unit: str = ""
    rate: float = 0.0
    amount: float = 0.0
    source: str = ""

class PriceBook:
    """Resolves a takeoff row to its price: project overrides win over
    the online cache, which wins over the bundled baseline. Measurement
    rows (concrete volumes, water, unknown steel profiles) have no price
    — their materials are already billed."""

    def __init__(self, overrides: dict | None = None,
                 online: dict | None = None) -> None:
        self.overrides = dict(overrides or {})
        self.online = dict(online or {})

    @staticmethod
    def key(item: str, unit: str, args: dict | None = None) -> str:
        base = f"{item}|{unit}"
        if args:
            base += "|" + ",".join(f"{k}={v}"
                                   for k, v in sorted(args.items()))
        return base

    def get(self, row) -> Price | None:
        k = self.key(row.item, row.unit, row.item_args)
        hit = self.overrides.get(k) or self.online.get(k)
        if isinstance(hit, dict) and "thb" in hit:
            return Price(float(hit["thb"]),
                         str(hit.get("source", SRC_MANUAL)),
                         str(hit.get("date", "")),
                         str(hit.get("province", "")))
        return self._bundled(row)

    def _bundled(self, row) -> Price | None:
        item, unit, args = row.item, row.unit, row.item_args
        if item == "Bricks — {type}":
            k = _BRICK_BY_LABEL.get(args.get("type", ""))
            p = BRICK_PRICES.get(k)
            return Price(p, SRC_ESTIMATE) if p else None
        if item == "Floor tiles {w}×{l} cm":
            try:
                w = float(args.get("w", 60)) / 100.0
                l = float(args.get("l", 60)) / 100.0
            except (TypeError, ValueError):
                w = l = 0.6
            return Price(round(TILE_PRICE_M2 * w * l, 2), SRC_ESTIMATE)
        if item == "Roof tiles — {type}":
            p = ROOF_PRICES.get(args.get("type", ""))
            return Price(p, SRC_ESTIMATE) if p else None
        pair = (item, unit)
        if pair in BUNDLED_VERIFIED:
            return Price(BUNDLED_VERIFIED[pair], SRC_VERIFIED)
        if pair in BUNDLED_ESTIMATE:
            return Price(BUNDLED_ESTIMATE[pair], SRC_ESTIMATE)
        return None

    def set_override(self, item: str, unit: str, thb: float,
                     source: str = SRC_MANUAL, date: str = "",
                     province: str = "", args: dict | None = None) -> None:
        self.overrides[self.key(item, unit, args)] = {
            "thb": float(thb), "source": source, "date": date,
            "province": province}

    def overrides_dict(self) -> dict:
        return dict(self.overrides)

    def labor_rate(self, name: str, default: float) -> float:
        """Labor rates are editable too: an override keyed «labor|Item»
        wins over the bundled rate."""
        hit = self.overrides.get("labor|" + name)
        if isinstance(hit, dict) and "thb" in hit:
            return float(hit["thb"])
        return default

    # ---- Supplier quotes -----------------------------------------------------
    def template_csv(self, rows) -> str:
        """Every priceable shopping row with its current rate: the user
        overwrites the price column with the supplier's quote and imports
        the file back — no typing of item names."""
        from core.i18n import tr
        from core.takeoff import UNIT_LABELS
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["item", "unit", "price_thb"])
        for r in rows:
            pr = self.get(r)
            w.writerow([display_item(r),
                        tr(UNIT_LABELS.get(r.unit, r.unit)),
                        f"{pr.thb:.2f}" if pr else ""])
        return out.getvalue()

    def import_csv(self, text: str, rows) -> int:
        """Match a quote's rows by display name (running language) or
        English key; 2 columns (name, price) or 3 (name, unit, price).
        Returns how many prices were taken."""
        from core.i18n import tr
        by_name: dict = {}
        for r in rows:
            pr = self.get(r)
            if pr is None:
                continue
            by_name[display_item(r)] = (r.item, r.unit, r.item_args)
            by_name.setdefault(r.item, (r.item, r.unit, r.item_args))
        taken = 0
        today = _today_th()
        for rec in csv.reader(io.StringIO(text)):
            rec = [c.strip() for c in rec if c.strip()]
            if len(rec) < 2:
                continue
            name, price = rec[0], rec[-1]
            try:
                thb = float(price.replace(",", "").replace("บาท", "").strip())
            except ValueError:
                continue                      # header or a note line
            hit = by_name.get(name)
            if hit is None:
                continue
            self.set_override(hit[0], hit[1], thb, SRC_CSV, today,
                              args=hit[2])
            taken += 1
        return taken


def _today_th() -> str:
    import datetime
    d = datetime.date.today()
    return f"{d.year + 543}-{d.month:02d}-{d.day:02d}"   # พ.ศ.

def compute_boq(result, work: dict, book: PriceBook, opts,
                overhead_pct: float = 0.0, profit_pct: float = 0.0,
                vat_pct: float = 0.0) -> dict:
    """The bill: priced shopping rows (order quantities) + labor on the
    work quantities, then overhead / profit / VAT — all optional, all off
    by default. ``work`` comes from takeoff.labor_base."""
    lines: list = []
    for r in result.shopping:
        pr = book.get(r)
        if pr is None or pr.thb <= 0:
            continue
        lines.append(BoqLine("material", r.item, dict(r.item_args),
                             r.order, r.unit, pr.thb,
                             round(r.order * pr.thb, 2), pr.source))

    def labor(item, qty, unit, rate, source):
        if qty > 1e-9 and rate > 0:
            lines.append(BoqLine("labor", item, {}, float(qty), unit,
                                 rate, round(qty * rate, 2), source))

    labor("Masonry labor", work.get("masonry_m2", 0.0), "m²",
          book.labor_rate("Masonry labor",
                          LABOR_MASONRY.get(opts.brick, 94.0)), SRC_VERIFIED)
    labor("Plaster labor", work.get("plaster_m2", 0.0), "m²",
          book.labor_rate("Plaster labor", LABOR["plaster"]), SRC_VERIFIED)
    fw = next((r for r in result.shopping
               if r.item.startswith("Plywood formwork")), None)
    contact = (fw.net * PLYWOOD_M2 * max(1, int(opts.plywood_reuse))
               if fw else 0.0)
    labor("Formwork labor", contact, "m²",
          book.labor_rate("Formwork labor", LABOR["formwork"]), SRC_VERIFIED)
    rebar_kg = sum(r.net for r in result.shopping
                   if r.item == "Rebar steel (kg)")
    labor("Rebar fixing labor", rebar_kg, "kg",
          book.labor_rate("Rebar fixing labor",
                          LABOR["rebar_fix"]), SRC_ESTIMATE)
    labor("Concrete placing labor", work.get("concrete_m3", 0.0), "m³",
          book.labor_rate("Concrete placing labor",
                          LABOR["concrete_place"]), SRC_ESTIMATE)
    labor("Paint labor", work.get("paint_m2", 0.0), "m²",
          book.labor_rate("Paint labor", LABOR["paint"]), SRC_ESTIMATE)
    labor("Tiling labor", work.get("tile_m2", 0.0), "m²",
          book.labor_rate("Tiling labor", LABOR["tile"]), SRC_VERIFIED)
    labor("Roofing labor", work.get("roof_m2", 0.0), "m²",
          book.labor_rate("Roofing labor", LABOR["roof"]), SRC_ESTIMATE)

    mat = sum(l.amount for l in lines if l.kind == "material")
    lab = sum(l.amount for l in lines if l.kind == "labor")
    sub = mat + lab
    oh = sub * max(0.0, overhead_pct) / 100.0
    profit = (sub + oh) * max(0.0, profit_pct) / 100.0
    vat = (sub + oh + profit) * max(0.0, vat_pct) / 100.0
    return {"lines": lines, "material_total": mat, "labor_total": lab,
            "subtotal": sub, "overhead": oh, "profit": profit,
            "vat": vat, "grand": sub + oh + profit + vat}


def boq_csv(boq: dict) -> str:
    from core.i18n import tr
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([tr("Kind"), tr("Item"), tr("Quantity"), tr("Unit"),
                tr("Rate (THB)"), tr("Amount (THB)"), tr("Source")])
    for l in boq["lines"]:
        w.writerow([tr("Material") if l.kind == "material" else tr("Labor"),
                    display_item(l), f"{l.qty:.3f}",
                    tr(UNIT_LABELS.get(l.unit, l.unit)),
                    f"{l.rate:.2f}", f"{l.amount:.2f}", l.source])
    for key, label in (("material_total", "Material total"),
                       ("labor_total", "Labor total"),
                       ("subtotal", "Subtotal"), ("overhead", "Overhead"),
                       ("profit", "Profit"), ("vat", "VAT"),
                       ("grand", "Grand total")):
        w.writerow(["", tr(label), "", "", "", f"{boq[key]:.2f}", ""])
    w.writerow([])
    w.writerow([tr("Estimates for ordering — not structural design. "
                   "Verify with the engineer's drawings.")])
    return out.getvalue()


# ---- Online cache (core/price_sync.py fills it) ------------------------------

def cache_path():
    from pathlib import Path
    from PySide6.QtCore import QStandardPaths
    base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    folder = Path(base or ".") / "prices"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "moc-cache.json"


def load_online_cache() -> dict:
    import json
    try:
        data = json.loads(cache_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_online_cache(data: dict) -> None:
    import json
    try:
        cache_path().write_text(json.dumps(data, ensure_ascii=False),
                                encoding="utf-8")
    except OSError:
        pass                    # a cache that cannot be written is no crisis