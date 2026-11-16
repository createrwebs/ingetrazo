# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Marco Sumari Tellez and IngeTrazo contributors.
"""Material takeoff — BIM quantities become the materials a builder orders.

core.bim measures tagged geometry honestly (net m² with openings deducted,
watertight m³, tagged rebar kg). This module converts those measurements
into purchasable materials — bricks, cement bags, sand and gravel m³,
rebar kg, formwork sheets, paint gallons, tiles, roof sheets — at Thai
standard consumption rates, every one of them an option the user can
change (TakeoffOptions, saved with the document in ``scene.takeoff``).

ESTIMATES FOR ORDERING, NOT STRUCTURAL DESIGN. Rebar ratios and roof
truss weights are industry averages for preliminary budgets; the real
sizes come from the engineer's drawings. The dialog and every export
carry this warning.

Rate sources (checked 2026-11):
- concrete dry volume ×1.54, cement 1440 kg/m³, 1:2:4 → 6.3 bags (50 kg)
  + 0.44 m³ sand + 0.88 m³ gravel per m³: rakadd.com/tools/concrete-calculator
- mortar dry volume ×1.30; plaster 15 mm 1:3 → ~7 kg cement/m²:
  rakadd.com/tools/mortar-calculator
- bricks/m² = 1/((face length + joint)(face height + joint)):
  thailandinnodesign.com/th/guides/bricks-per-square-metre, nsquare.co
- bar weight d²/162 kg/m, 10 m stock lengths, 5–10% lap/waste:
  steellead.com/how-to-estimate-rebar-concrete.html
- double-roll fibre-cement roof tiles 2.2 sheets/m² (50×120 cm):
  hiachet.com/blog/double-corrugated-roof-tile
- paint 30–35 m² per gallon (3.785 L) per coat: rakadd.com/tools/paint-calculator

The engine is pure data (no GUI): ``compute_takeoff(scene, options)`` →
``TakeoffResult``; views/takeoff_dialog.py renders and exports it. Every
row keeps its provenance (which BIM objects, which formula) so an
engineer can audit it back to the geometry.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from core.bim import (IFC_CLASS_NAMES, _extents, class_quantities,
                      collect_objects, face_net_area)

# ---- Consumption constants (sources in the module docstring) ----------------
CEMENT_DENSITY = 1440.0        # kg/m³, loose Portland cement
BAG_KG = 50.0                  # the standard Thai cement bag
ADHESIVE_BAG_KG = 20.0         # ready-mix mortar / tile adhesive bag
DRY_CONCRETE = 1.54            # dry volume per wet m³ (1.52–1.57 range)
DRY_MORTAR = 1.30              # dry volume per wet m³ of mortar
WC_RATIO = 0.5                 # water/cement by weight, indicative
GALLON_L = 3.785               # the paint gallon
BAR_STOCK_M = 10.0             # rebar stock length in the Thai market
PLYWOOD_M2 = 1.22 * 2.44       # one formwork plywood sheet, m²
AAC_GLUE_KG_M2 = 5.0           # AAC thin-bed adhesive, kg per wall m²

#: Concrete mixes by volume (cement : sand : gravel).
CONCRETE_MIXES = {
    "1:2:4": (1.0, 2.0, 4.0),      # general work, ~180–210 ksc
    "1:1.5:3": (1.0, 1.5, 3.0),    # heavier work, ~240–280 ksc
    "1:3:5": (1.0, 3.0, 5.0),      # coarse work
    "1:3:6": (1.0, 3.0, 6.0),      # lean concrete / blinding
}

#: Mortar mixes by volume (cement : sand).
MORTAR_MIXES = {"1:2": (1.0, 2.0), "1:3": (1.0, 3.0), "1:4": (1.0, 4.0)}

#: Wall materials. ``face_l × face_h`` is the brick face visible on the
#: wall (NOT the thickness), ``thick`` the wall one wythe builds, ``joint``
#: the laying joint. Counts follow 1/((face_l+joint)(face_h+joint));
#: AAC is laid with thin-bed adhesive, not cement-sand mortar.
BRICKS = {
    "mon_half": {"label": "Clay brick (half-brick wall)",
                 "face_l": 0.145, "face_h": 0.035, "thick": 0.065,
                 "joint": 0.010, "binder": "mortar"},
    "block7": {"label": "Concrete block 7 cm",
               "face_l": 0.39, "face_h": 0.19, "thick": 0.07,
               "joint": 0.010, "binder": "mortar"},
    "block19": {"label": "Concrete block 19 cm",
                "face_l": 0.39, "face_h": 0.19, "thick": 0.19,
                "joint": 0.010, "binder": "mortar"},
    "aac75": {"label": "AAC block 7.5 cm",
              "face_l": 0.60, "face_h": 0.20, "thick": 0.075,
              "joint": 0.003, "binder": "adhesive"},
    "aac10": {"label": "AAC block 10 cm",
              "face_l": 0.60, "face_h": 0.20, "thick": 0.10,
              "joint": 0.003, "binder": "adhesive"},
    # A concrete wall: no bricks — concrete + rebar + formwork instead.
    "concrete": {"label": "Concrete wall", "binder": None},
}

#: Roof coverings: sheets per m² of the real sloped surface.
ROOF_TYPES = {
    "double_roll": {"label": "Double-roll fibre-cement tile 50×120 cm",
                    "per_m2": 2.2},
    "metal_sheet": {"label": "Metal sheet (with laps)", "per_m2": 1.08},
}

#: Purchase units → their display names (English source strings for tr()).
UNIT_LABELS = {
    "bag50": "bags (50 kg)", "bag20": "bags (20 kg)", "m3": "m³",
    "kg": "kg", "piece": "pieces", "sheet": "sheets",
    "gallon": "gallons (3.785 L)", "L": "litres", "set": "sets", "m": "m",
}

#: Work categories → display names, in the order a building is built.
CATEGORY_LABELS = {
    "earthwork": "Earthwork",
    "concrete": "Concrete work", "bedding": "Bedding / lean concrete",
    "masonry": "Masonry", "plaster": "Plastering", "rebar": "Reinforcement",
    "formwork": "Formwork", "roof": "Roofing", "paint": "Painting",
    "tiles": "Tiling", "openings": "Doors & windows", "steel": "Steel members",
}
CATEGORY_ORDER = list(CATEGORY_LABELS)

@dataclass
class TakeoffOptions:
    """Every rate the takeoff uses — the document's estimating basis.

    Defaults are the Thai residential standard (sources in the module
    docstring); the dialog edits them and ``scene.takeoff`` keeps them
    with the file. ``from_dict`` merges over the defaults so a document
    saved by an older version still gets every new key.
    """
    # Concrete
    concrete_mix: str = "1:2:4"
    ready_mix: bool = False            # order ready-mixed m³, not site materials
    # Masonry
    brick: str = "mon_half"            # a key of BRICKS ("concrete" = no bricks)
    laying_mix: str = "1:3"
    # Plaster
    plaster_mm: float = 15.0
    plaster_sides: int = 2
    plaster_mix: str = "1:3"
    # Reinforcement: auto = tagged bars when any exist, else the ratios;
    # ratios = always the kg/m³ estimates; tagged = only tagged bars.
    rebar_mode: str = "auto"
    rebar_ratios: dict = field(default_factory=lambda: {
        "IfcColumn": 130.0, "IfcBeam": 150.0, "IfcSlab": 90.0,
        "IfcFooting": 70.0, "IfcStair": 100.0, "IfcRamp": 80.0,
        "IfcWall": 80.0, "IfcPile": 100.0})
    # Paint
    paint: bool = True
    paint_sides: int = 2
    paint_coverage: float = 32.0       # m² per gallon per coat
    primer_coats: int = 1
    top_coats: int = 2
    # Floor finish on slabs
    floor_finish: str = "tile"         # "tile" | "none"
    tile_w: float = 0.60
    tile_l: float = 0.60
    adhesive_kg_m2: float = 5.0
    # Roof
    roof_type: str = "double_roll"     # a key of ROOF_TYPES
    roof_per_m2: float = 0.0           # > 0 overrides the type's rate
    truss_kg_m2: float = 15.0          # estimate, not a design
    # Formwork
    formwork: bool = True
    plywood_reuse: int = 3
    # Bedding under slabs and footings
    bedding: bool = True
    lean_mix: str = "1:3:6"
    lean_mm: float = 50.0
    sand_bed_mm: float = 50.0
    # Fallback thickness when a slab is not watertight (flagged estimate)
    slab_thickness_mm: float = 150.0
    # Earthwork under footings (excavation + backfill), off by default
    earthwork: bool = False
    soil_bulking: float = 1.25         # loose soil volume / in-situ volume
    excavation_working_m: float = 0.30  # extra depth below the footing base
    # Tile laying pattern: diagonal cuts more, so it wastes more
    tile_pattern: str = "straight"     # straight | diagonal
    # Waste / loss percentages per material family
    waste: dict = field(default_factory=lambda: {
        "concrete": 5.0, "brick": 5.0, "mortar": 10.0, "plaster": 10.0,
        "rebar": 8.0, "paint": 5.0, "tile": 10.0, "tile_diagonal": 15.0,
        "adhesive": 10.0, "roof": 5.0, "truss": 5.0, "formwork": 5.0,
        "lean": 5.0, "sand": 5.0, "earthwork": 0.0})

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "TakeoffOptions":
        data = data if isinstance(data, dict) else {}
        known = set(cls.__dataclass_fields__)
        kw = {k: v for k, v in data.items()
              if k in known and k not in ("waste", "rebar_ratios")}
        opts = cls(**kw)
        if isinstance(data.get("waste"), dict):
            opts.waste.update({k: float(v) for k, v in data["waste"].items()
                               if isinstance(v, (int, float))})
        if isinstance(data.get("rebar_ratios"), dict):
            opts.rebar_ratios.update(
                {k: float(v) for k, v in data["rebar_ratios"].items()
                 if isinstance(v, (int, float))})
        return opts


#: One-click estimating bases (the dialog's preset buttons).
PRESET_LABELS = {
    "house_clay": "House — clay brick",
    "house_aac": "House — AAC block",
    "concrete_building": "Concrete building",
}


def preset_options(name: str) -> TakeoffOptions:
    """A ready estimating basis; unknown names give the defaults."""
    opts = TakeoffOptions()
    if name == "house_aac":
        opts.brick = "aac75"
        opts.plaster_mm = 10.0          # AAC is skimmed thinner
    elif name == "concrete_building":
        opts.brick = "concrete"
        opts.floor_finish = "none"
        opts.paint = False
        opts.bedding = True
    return opts


@dataclass
class TakeoffRow:
    """One material line. ``item``/``note`` are English source strings
    (tr() at display time); ``item_args`` interpolates them. ``sources``
    keeps (BIM object, net quantity) so every number audits back to the
    geometry; ``estimated`` marks a fallback or ratio-based number."""
    category: str
    item: str
    unit: str
    waste_key: str = ""
    net: float = 0.0
    order: float = 0.0
    estimated: bool = False
    note: str = ""
    item_args: dict = field(default_factory=dict)
    sources: list = field(default_factory=list)


@dataclass
class TakeoffResult:
    rows: list = field(default_factory=list)        # aggregated by category
    shopping: list = field(default_factory=list)    # merged across categories
    objects: list = field(default_factory=list)     # (obj, per-object rows)
    warnings: list = field(default_factory=list)    # (source string, kwargs)
    untagged_groups: int = 0
    options: TakeoffOptions | None = None


# ---- Pure rate math (testable without a scene) -------------------------------

def concrete_mix_materials(volume_m3: float, mix) -> tuple:
    """(cement kg, sand m³, gravel m³, water L) for ``volume_m3`` of wet
    concrete at ``mix`` — the dry-volume method: dry = wet × 1.54, split
    by the mix proportions, cement volume × 1440 kg/m³."""
    c, s, g = CONCRETE_MIXES[mix] if isinstance(mix, str) else mix
    total = c + s + g
    dry = volume_m3 * DRY_CONCRETE
    cement = dry * c / total * CEMENT_DENSITY
    return cement, dry * s / total, dry * g / total, cement * WC_RATIO


def mortar_mix_materials(volume_m3: float, mix) -> tuple:
    """(cement kg, sand m³) for wet mortar volume — dry factor 1.30."""
    c, s = MORTAR_MIXES[mix] if isinstance(mix, str) else mix
    dry = volume_m3 * DRY_MORTAR
    return dry * c / (c + s) * CEMENT_DENSITY, dry * s / (c + s)


def bricks_per_m2(brick: str, joint: float | None = None) -> float:
    """Bricks per wall m²: 1 / ((face length + joint)(face height + joint)).
    The face is what shows on the wall — never the thickness."""
    b = BRICKS[brick]
    j = b["joint"] if joint is None else joint
    return 1.0 / ((b["face_l"] + j) * (b["face_h"] + j))


def mortar_joint_fraction(brick: str, joint: float | None = None) -> float:
    """The wall face fraction that is laying mortar (the joints):
    1 − brick face / (face + joints). Mortar volume = area × thickness ×
    this fraction — honest for any wall thickness, no negative volumes."""
    b = BRICKS[brick]
    j = b["joint"] if joint is None else joint
    return 1.0 - (b["face_l"] * b["face_h"]) / ((b["face_l"] + j)
                                               * (b["face_h"] + j))


def wythes(thickness: float, brick: str) -> int:
    """How many brick wythes a wall thickness builds: one up to 1.6 × the
    brick thickness (a 10 cm wall is one 6.5 cm wythe + plaster, not two),
    then the nearest whole count."""
    t = BRICKS[brick]["thick"]
    if thickness < 1.6 * t:
        return 1
    return max(1, round(thickness / t))


def round_order(net: float, waste_pct: float, unit: str) -> float:
    """The purchase quantity: net + waste, rounded UP to what shops sell —
    whole bags/pieces/sheets/gallons/kg/litres, 0.01 m³ (or m) for bulk."""
    raw = net * (1.0 + max(0.0, waste_pct) / 100.0)
    if unit in ("bag50", "bag20", "piece", "sheet", "set", "gallon",
                "kg", "L"):
        return float(math.ceil(raw - 1e-9))
    return math.ceil(raw * 100.0 - 1e-9) / 100.0


def _row_key(row: TakeoffRow) -> tuple:
    return (row.category, row.item, row.unit,
            tuple(sorted(row.item_args.items())))


def _aggregate(per_object: list, opts: TakeoffOptions) -> list:
    """Merge per-object rows into purchase lines; round once, on the total
    (rounding each object up would over-order a house of 20 walls)."""
    agg: dict = {}
    for obj, rows in per_object:
        for r in rows:
            key = _row_key(r)
            a = agg.get(key)
            if a is None:
                a = TakeoffRow(r.category, r.item, r.unit, r.waste_key,
                               note=r.note, item_args=dict(r.item_args))
                agg[key] = a
            a.net += r.net
            a.estimated = a.estimated or r.estimated
            a.sources.append((obj, r.net))
    out = list(agg.values())
    for a in out:
        a.order = round_order(a.net, opts.waste.get(a.waste_key, 0.0), a.unit)
    out.sort(key=lambda r: (CATEGORY_ORDER.index(r.category)
                            if r.category in CATEGORY_ORDER else 99,
                            r.item, str(r.item_args)))
    return out


def _shopping(rows: list, opts: TakeoffOptions) -> list:
    """The same lines merged ACROSS categories: one sand total, one cement
    total — what the shopping list orders. Provenance is kept."""
    agg: dict = {}
    for r in rows:
        key = (r.item, r.unit, tuple(sorted(r.item_args.items())))
        a = agg.get(key)
        if a is None:
            a = TakeoffRow("shopping", r.item, r.unit, r.waste_key,
                           note=r.note, item_args=dict(r.item_args))
            agg[key] = a
        a.net += r.net
        a.estimated = a.estimated or r.estimated
        a.sources.extend(r.sources)
    out = list(agg.values())
    for a in out:
        a.order = round_order(a.net, opts.waste.get(a.waste_key, 0.0), a.unit)
    out.sort(key=lambda r: (r.item, str(r.item_args)))
    return out


# ---- Per-object material rows ------------------------------------------------

def _concrete_rows(add, cls, vol, opts, est):
    part = IFC_CLASS_NAMES.get(cls, cls)
    if opts.ready_mix:
        add("concrete", "Ready-mixed concrete — {part}", "m3", vol,
            "concrete", estimated=est, part=part)
        return
    add("concrete", "Concrete — {part}", "m3", vol, "concrete",
        estimated=est, part=part)
    ck, sd, gv, wt = concrete_mix_materials(vol, opts.concrete_mix)
    add("concrete", "Cement (50 kg bags)", "bag50", ck / BAG_KG, "concrete",
        note=f"{opts.concrete_mix}, dry ×1.54, 1440 kg/m³")
    add("concrete", "Sand (m³)", "m3", sd, "concrete")
    add("concrete", "Gravel (m³)", "m3", gv, "concrete")
    add("concrete", "Water (L)", "L", wt, "")


def _paint_rows(add, area, opts):
    if not opts.paint or area <= 1e-9 or opts.paint_coverage <= 0:
        return
    if opts.primer_coats > 0:
        add("paint", "Primer (gallons)", "gallon",
            area * opts.primer_coats / opts.paint_coverage, "paint",
            note=f"{opts.primer_coats} coat(s), {opts.paint_coverage:g} m²/gallon")
    if opts.top_coats > 0:
        add("paint", "Topcoat (gallons)", "gallon",
            area * opts.top_coats / opts.paint_coverage, "paint",
            note=f"{opts.top_coats} coat(s), {opts.paint_coverage:g} m²/gallon")


def _formwork_rows(add, contact, opts, est=False):
    if not opts.formwork or contact <= 1e-9:
        return
    reuse = max(1, int(opts.plywood_reuse))
    add("formwork", "Plywood formwork sheets (1.22×2.44 m)", "sheet",
        contact / PLYWOOD_M2 / reuse, "formwork", estimated=est,
        note=f"contact {contact:.2f} m² ÷ {PLYWOOD_M2:.2f} ÷ reuse {reuse}")


def _rebar_rows(add, cls, vol, opts, use_ratios):
    if not use_ratios or vol <= 1e-9:
        return
    ratio = float(opts.rebar_ratios.get(cls, 0.0))
    if ratio <= 0:
        return
    add("rebar", "Rebar steel (kg)", "kg", vol * ratio, "rebar",
        estimated=True, note=f"{ratio:g} kg/m³ × {vol:.3f} m³ (estimate)")


def _bedding_rows(add, area, opts):
    if not opts.bedding or area <= 1e-9:
        return
    if opts.lean_mm > 0:
        vol = area * opts.lean_mm / 1000.0
        add("bedding", "Lean concrete (m³)", "m3", vol, "lean",
            note=f"{opts.lean_mm:g} mm under {area:.2f} m²")
        ck, sd, gv, _wt = concrete_mix_materials(vol, opts.lean_mix)
        add("concrete", "Cement (50 kg bags)", "bag50", ck / BAG_KG,
            "concrete", note=f"lean {opts.lean_mix}")
        add("concrete", "Sand (m³)", "m3", sd, "concrete", note="lean")
        add("concrete", "Gravel (m³)", "m3", gv, "concrete", note="lean")
    if opts.sand_bed_mm > 0:
        add("bedding", "Sand (m³)", "m3", area * opts.sand_bed_mm / 1000.0,
            "sand", note=f"bedding {opts.sand_bed_mm:g} mm under {area:.2f} m²")


def _contact_area(faces, mode: str, closed: bool) -> float:
    """Formwork contact from the real faces: «sides» = vertical faces
    (columns, footings, walls), «beam» = sides + soffit without the top,
    «soffit» = the underside (an open sheet counts its whole area)."""
    total = 0.0
    for f in faces:
        nz = f.normal().z()
        if mode == "sides":
            ok = abs(nz) < 0.5
        elif mode == "beam":
            ok = (nz < 0.7) if closed else (abs(nz) < 0.7)
        elif mode == "soffit":
            ok = (nz < -0.7) if closed else True
        else:
            ok = True
        if ok:
            total += face_net_area(f)
    return total

def _object_rows(obj, opts, use_ratios, use_tagged, warnings):
    """The material rows of ONE BIM object, from the same quantities the
    BIM panel and the IFC export show (class_quantities)."""
    cls = obj["class"]
    faces = obj.get("faces")
    if faces is None:
        faces = list(obj["group"].mesh.faces)
    label = obj.get("name") or IFC_CLASS_NAMES.get(cls, cls)
    volume = obj.get("volume")
    closed = volume is not None
    _qset, entries, _metrado = class_quantities(cls, faces)
    q = {name: val for _kind, name, val in entries}
    dx, dy, dz = _extents(faces)
    rows: list = []

    def add(category, item, unit, net, waste_key="", note="",
            estimated=False, **item_args):
        if net is None or net <= 1e-9:
            return
        rows.append(TakeoffRow(category, item, unit, waste_key,
                               net=float(net), estimated=estimated,
                               note=note, item_args=item_args))

    if cls == "IfcWall":
        net = q.get("NetSideArea", 0.0)
        thick = q.get("Width") or min(dx, dy)
        if net <= 1e-9 or thick <= 1e-9:
            return rows
        brick = opts.brick if opts.brick in BRICKS else "mon_half"
        b = BRICKS[brick]
        if b.get("binder") is None:            # concrete wall
            vol = volume if closed else net * thick
            if not closed:
                warnings.append(("Wall «{name}»: not watertight — volume "
                                 "from face area × thickness",
                                 {"name": label}))
            _concrete_rows(add, cls, vol, opts, not closed)
            _rebar_rows(add, cls, vol, opts, use_ratios)
            _formwork_rows(add, _contact_area(faces, "sides", closed),
                           opts, not closed)
            return rows
        n_wythes = wythes(thick, brick)
        per_m2 = bricks_per_m2(brick)
        add("masonry", "Bricks — {type}", "piece", net * per_m2 * n_wythes,
            "brick", type=b["label"],
            note=f"{per_m2:.1f}/m² × {n_wythes} wythe(s), openings deducted")
        if b["binder"] == "adhesive":
            add("masonry", "AAC adhesive (20 kg bags)", "bag20",
                net * AAC_GLUE_KG_M2 / ADHESIVE_BAG_KG, "adhesive",
                note=f"≈{AAC_GLUE_KG_M2:g} kg/m²")
        else:
            mv = net * thick * mortar_joint_fraction(brick)
            ck, sd = mortar_mix_materials(mv, opts.laying_mix)
            add("masonry", "Laying cement (50 kg bags)", "bag50",
                ck / BAG_KG, "mortar", note=f"{opts.laying_mix}, dry ×1.30")
            add("masonry", "Sand (m³)", "m3", sd, "mortar",
                note="laying mortar")
        if opts.plaster_mm > 0 and opts.plaster_sides > 0:
            pv = net * opts.plaster_sides * opts.plaster_mm / 1000.0
            ck, sd = mortar_mix_materials(pv, opts.plaster_mix)
            add("plaster", "Plaster cement (50 kg bags)", "bag50",
                ck / BAG_KG, "plaster",
                note=f"{opts.plaster_mm:g} mm × {opts.plaster_sides} side(s), dry ×1.30")
            add("plaster", "Sand (m³)", "m3", sd, "plaster", note="plaster")
        _paint_rows(add, net * opts.paint_sides, opts)

    elif cls == "IfcSlab":
        net = q.get("NetArea", 0.0)
        if net <= 1e-9:
            return rows
        if closed:
            vol = volume
        else:
            vol = net * opts.slab_thickness_mm / 1000.0
            warnings.append(("Slab «{name}»: not watertight — {mm} mm "
                             "thickness assumed",
                             {"name": label,
                              "mm": f"{opts.slab_thickness_mm:g}"}))
        _concrete_rows(add, cls, vol, opts, not closed)
        _rebar_rows(add, cls, vol, opts, use_ratios)
        _formwork_rows(add, _contact_area(faces, "soffit", closed),
                       opts, not closed)
        _bedding_rows(add, net, opts)
        if opts.floor_finish == "tile":
            tw, tl = max(0.05, opts.tile_w), max(0.05, opts.tile_l)
            tile_waste = ("tile_diagonal" if opts.tile_pattern == "diagonal"
                          else "tile")
            add("tiles", "Floor tiles {w}×{l} cm", "piece", net / (tw * tl),
                tile_waste, w=f"{tw * 100:g}", l=f"{tl * 100:g}")
            add("tiles", "Tile adhesive (20 kg bags)", "bag20",
                net * opts.adhesive_kg_m2 / ADHESIVE_BAG_KG, "adhesive",
                note=f"{opts.adhesive_kg_m2:g} kg/m²")

    elif cls == "IfcColumn":
        vol = volume if closed else dx * dy * dz
        if vol <= 1e-9:
            return rows
        if not closed:
            warnings.append(("Column «{name}»: not watertight — volume "
                             "from the bounding box", {"name": label}))
        _concrete_rows(add, cls, vol, opts, not closed)
        _rebar_rows(add, cls, vol, opts, use_ratios)
        contact = (_contact_area(faces, "sides", True) if closed
                   else 2.0 * (dx + dy) * dz)
        _formwork_rows(add, contact, opts, not closed)

    elif cls == "IfcBeam":
        vol = volume if closed else dx * dy * dz
        if vol <= 1e-9:
            return rows
        if not closed:
            warnings.append(("Beam «{name}»: not watertight — volume "
                             "from the bounding box", {"name": label}))
        _concrete_rows(add, cls, vol, opts, not closed)
        _rebar_rows(add, cls, vol, opts, use_ratios)
        if closed:
            contact = _contact_area(faces, "beam", True)
        else:
            lo, mid, hi = sorted((dx, dy, dz))
            contact = (2.0 * mid + lo) * hi     # sides + soffit, no top
        _formwork_rows(add, contact, opts, not closed)

    elif cls == "IfcFooting":
        vol = volume if closed else dx * dy * dz
        if vol <= 1e-9:
            return rows
        if not closed:
            warnings.append(("Footing «{name}»: not watertight — volume "
                             "from the bounding box", {"name": label}))
        _concrete_rows(add, cls, vol, opts, not closed)
        _rebar_rows(add, cls, vol, opts, use_ratios)
        contact = (_contact_area(faces, "sides", True) if closed
                   else 2.0 * (dx + dy) * dz)
        _formwork_rows(add, contact, opts, not closed)
        _bedding_rows(add, dx * dy, opts)
        if opts.earthwork:
            zs = [v.position.z() for f in faces
                  for lp in (f.loop, *f.hole_loops) for v in lp]
            depth = max(0.0, -(min(zs) if zs else 0.0)) \
                + opts.excavation_working_m
            exc = dx * dy * depth * opts.soil_bulking
            add("earthwork", "Excavation (m³)", "m3", exc, "earthwork",
                estimated=True,
                note=f"{dx * dy:.2f} m² × {depth:.2f} m × bulking "
                     f"{opts.soil_bulking:g}")
            add("earthwork", "Backfill (m³)", "m3",
                max(0.0, exc - (volume or 0.0)), "earthwork", estimated=True)

    elif cls == "IfcRoof":
        up = q.get("GrossArea", 0.0)            # upward net area = slopes
        if up <= 1e-9:
            return rows
        rt = ROOF_TYPES.get(opts.roof_type, ROOF_TYPES["double_roll"])
        per = opts.roof_per_m2 if opts.roof_per_m2 > 0 else rt["per_m2"]
        add("roof", "Roof tiles — {type}", "sheet", up * per, "roof",
            type=rt["label"], note=f"{per:g} sheets/m² × {up:.2f} m² sloped")
        if opts.truss_kg_m2 > 0:
            add("roof", "Roof truss steel (kg, estimate)", "kg",
                up * opts.truss_kg_m2, "truss", estimated=True,
                note=f"{opts.truss_kg_m2:g} kg/m² — estimate, not a design")

    elif cls in ("IfcStair", "IfcRamp", "IfcPile"):
        if not closed:
            warnings.append(("{cls} «{name}»: not watertight — no concrete "
                             "volume", {"cls": IFC_CLASS_NAMES.get(cls, cls),
                                        "name": label}))
            return rows
        _concrete_rows(add, cls, volume, opts, False)
        _rebar_rows(add, cls, volume, opts, use_ratios)
        if cls != "IfcPile":
            _formwork_rows(add, _contact_area(faces, "beam", True), opts)

    elif cls in ("IfcDoor", "IfcWindow"):
        w, h = q.get("Width", 0.0), q.get("Height", 0.0)
        item = "Doors (sets)" if cls == "IfcDoor" else "Windows (sets)"
        add("openings", item, "set", 1.0, "", note=f"{w:.2f} × {h:.2f} m")

    elif cls == "IfcReinforcingBar":
        if not use_tagged:
            return rows
        weight = q.get("Weight", 0.0)
        if weight <= 1e-9:
            warnings.append(("Rebar «{name}»: not watertight — weight "
                             "unknown", {"name": label}))
            return rows
        add("rebar", "Rebar steel (kg)", "kg", weight, "rebar",
            note="tagged bar: volume × 7850 kg/m³")

    elif cls == "IfcCovering":
        _paint_rows(add, q.get("NetArea", 0.0), opts)

    elif cls in ("IfcMember", "IfcRailing"):
        add("steel", "Steel member / railing (m)", "m",
            q.get("Length", 0.0), "", note="profile unknown — length only")

    # IfcSpace carries no materials (room schedules are a later phase).
    return rows

# ---- The takeoff itself ------------------------------------------------------

def _count_untagged(scene) -> int:
    """Groups holding geometry no BIM tag claims — the «tag these first»
    guidance. A group with a tagged descendant is covered, not counted."""
    from core.bim import _holds_face_tags, _placed_groups

    def covered(g):
        if getattr(g, "ifc", None) or _holds_face_tags(g.mesh):
            return True
        return any(covered(k) for k in (getattr(g, "children", None) or ()))

    n = 0
    for g, _base in _placed_groups(scene.groups):
        if getattr(g, "ifc", None):
            continue
        if not g.mesh.faces or covered(g):
            continue
        n += 1
    return n


def compute_takeoff(scene, options: TakeoffOptions | None = None,
                    memo: dict | None = None) -> TakeoffResult:
    """Measure every tagged BIM object and convert it to materials.
    Options default to the document's own basis (``scene.takeoff``);
    ``memo`` is the collect_objects cache the caller keeps between calls."""
    opts = options if options is not None else TakeoffOptions.from_dict(
        getattr(scene, "takeoff", None))
    objects = collect_objects(scene, memo)
    warnings: list = []
    bars = [o for o in objects if o["class"] == "IfcReinforcingBar"]
    has_tagged = any(o.get("volume") for o in bars)
    mode = opts.rebar_mode
    use_tagged = mode == "tagged" or (mode == "auto" and has_tagged)
    use_ratios = mode == "ratios" or (mode == "auto" and not has_tagged)
    if mode == "ratios" and bars:
        warnings.append(("{n} tagged rebar objects ignored (ratios mode)",
                         {"n": len(bars)}))
    if mode == "tagged" and not has_tagged:
        warnings.append(("No tagged rebar: elements carry no steel "
                         "estimate in tagged mode", {}))
    per_object = []
    for obj in objects:
        rows = _object_rows(obj, opts, use_ratios, use_tagged, warnings)
        if rows:
            per_object.append((obj, rows))
    rows = _aggregate(per_object, opts)
    return TakeoffResult(rows=rows, shopping=_shopping(rows, opts),
                         objects=per_object, warnings=warnings,
                         untagged_groups=_count_untagged(scene), options=opts)


def labor_base(scene, opts: TakeoffOptions, memo: dict | None = None) -> dict:
    """The work quantities a labor bill prices: m² of wall, plaster, paint,
    tiling and roofing, m³ of concrete. Same objects, same options."""
    work = {"masonry_m2": 0.0, "plaster_m2": 0.0, "concrete_m3": 0.0,
            "paint_m2": 0.0, "tile_m2": 0.0, "roof_m2": 0.0}
    for obj in collect_objects(scene, memo):
        cls = obj["class"]
        faces = obj.get("faces")
        if faces is None:
            faces = list(obj["group"].mesh.faces)
        _qs, entries, _m = class_quantities(cls, faces)
        q = {name: val for _k, name, val in entries}
        vol = obj.get("volume")
        if cls == "IfcWall":
            net = q.get("NetSideArea", 0.0)
            if opts.brick in BRICKS and BRICKS[opts.brick].get("binder"):
                work["masonry_m2"] += net
                if opts.plaster_mm > 0:
                    work["plaster_m2"] += net * opts.plaster_sides
            elif vol:
                work["concrete_m3"] += vol
            if opts.paint:
                work["paint_m2"] += net * opts.paint_sides
        elif cls == "IfcSlab":
            work["concrete_m3"] += vol or (q.get("NetArea", 0.0)
                                           * opts.slab_thickness_mm / 1000.0)
            if opts.floor_finish == "tile":
                work["tile_m2"] += q.get("NetArea", 0.0)
        elif cls in ("IfcColumn", "IfcBeam", "IfcFooting", "IfcStair",
                     "IfcRamp", "IfcPile") and vol:
            work["concrete_m3"] += vol
        elif cls == "IfcRoof":
            work["roof_m2"] += q.get("GrossArea", 0.0)
        elif cls == "IfcCovering" and opts.paint:
            work["paint_m2"] += q.get("NetArea", 0.0)
    return work


# ---- CSV export --------------------------------------------------------------

def display_item(row) -> str:
    """The row's item in the running language, with its {part}/{type}/
    {w}×{l} arguments translated too (they are English source strings:
    class names, brick and roof labels). Works for TakeoffRow and
    BoqLine alike."""
    from core.i18n import tr
    return tr(row.item, **{k: tr(str(v))
                           for k, v in row.item_args.items()})


def _csv_field(text) -> str:
    text = str(text)
    if any(ch in text for ch in ',"\n'):
        return '"' + text.replace('"', '""') + '"'
    return text


def takeoff_csv(result: TakeoffResult) -> str:
    """The takeoff as CSV in the running language: the category lines,
    then the merged shopping totals, then the standing warning. The
    dialog writes it with a UTF-8 BOM so Excel reads Thai correctly."""
    from core.i18n import tr
    head = ["Category", "Item", "Net quantity", "Waste %", "Order quantity",
            "Unit", "Status", "Note", "Source objects"]
    lines = [",".join(_csv_field(tr(h)) for h in head)]

    def row_line(r, with_cat=True):
        waste = (result.options.waste.get(r.waste_key, 0.0)
                 if result.options else 0.0)
        src = "; ".join(f"{o.get('name') or o['class']} ×{n:.3f}"
                        for o, n in r.sources[:10])
        fields = [tr(CATEGORY_LABELS.get(r.category, r.category))
                  if with_cat else "",
                  display_item(r), f"{r.net:.3f}", f"{waste:g}",
                  f"{r.order:.2f}", tr(UNIT_LABELS.get(r.unit, r.unit)),
                  tr("estimate") if r.estimated else tr("measured"),
                  tr(r.note) if r.note else "", src]
        return ",".join(_csv_field(f) for f in fields)

    for r in result.rows:
        lines.append(row_line(r))
    lines.append("")
    lines.append(_csv_field(tr("Shopping totals (merged, rounded for purchase)")))
    for r in result.shopping:
        lines.append(row_line(r, with_cat=False))
    lines.append("")
    lines.append(_csv_field(tr("Estimates for ordering — not structural "
                               "design. Verify with the engineer's drawings.")))
    return "\n".join(lines) + "\n"


# ---- Snapshots: compare the takeoff before and after a design change ---------

def snapshot(result: TakeoffResult) -> dict:
    """A frozen takeoff for later comparison (change orders): each row's
    identity and quantities, timestamped. Travels in scene.takeoff."""
    import datetime
    return {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "rows": [{"item": r.item, "args": dict(r.item_args),
                      "unit": r.unit, "net": r.net, "order": r.order}
                     for r in result.rows]}


def _snap_key(row: dict) -> tuple:
    return (row["item"], row["unit"],
            tuple(sorted((row.get("args") or {}).items())))


def diff_snapshots(old: dict, new: dict) -> list:
    """The rows whose quantities moved: old/new net and order, plus the
    order delta — what a change order adds or removes."""
    a = {_snap_key(r): r for r in old.get("rows", [])}
    b = {_snap_key(r): r for r in new.get("rows", [])}
    out = []
    for key in sorted(set(a) | set(b), key=str):
        ra, rb = a.get(key), b.get(key)
        na, oa = (ra["net"], ra["order"]) if ra else (0.0, 0.0)
        nb, ob = (rb["net"], rb["order"]) if rb else (0.0, 0.0)
        if abs(na - nb) > 1e-9 or abs(oa - ob) > 1e-9:
            row = rb or ra
            out.append({"item": row["item"], "args": row.get("args") or {},
                        "unit": row["unit"], "old_net": na, "new_net": nb,
                        "old_order": oa, "new_order": ob, "delta": ob - oa})
    return out


# ---- Zone / floor grouping ---------------------------------------------------

def zone_of(obj, scene) -> str:
    """The floor or zone an object belongs to: its top-level container's
    layer name (if set) or group name. Loose geometry = "" (general)."""
    g = obj.get("group") or obj.get("owner")
    if g is None:
        return ""
    from core.outliner import path_to
    path = path_to(scene.groups, g)
    top = path[0] if path else g
    return getattr(top, "layer", None) or top.name or ""


def rows_by_zone(result: TakeoffResult, scene) -> dict:
    """Re-aggregate the per-object rows by zone: {zone: [TakeoffRow]},
    each zone's lines rounded for purchase on their own totals."""
    buckets: dict = {}
    for obj, rows in result.objects:
        buckets.setdefault(zone_of(obj, scene), []).append((obj, rows))
    return {z: _aggregate(per_obj, result.options)
            for z, per_obj in buckets.items()}