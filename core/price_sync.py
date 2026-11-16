# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Marco Sumari Tellez and IngeTrazo contributors.
"""Online price sync — Ministry of Commerce province averages.

The MOC open data API (dataapi.moc.go.th/csi-indexes, documented at
data.moc.go.th/OpenData/CSIProductIndexes) publishes monthly average
construction material prices PER PROVINCE — the same numbers used for
government standard prices. No API key. This module fetches one
province's latest year, maps the Thai product names onto the takeoff's
item keys (conservatively: name keyword AND a compatible unit), and
hands the dialog a cache dict for core.prices.load_online_cache.

Offline-first, like the component library: a failed sync changes
nothing — the cached and bundled prices keep working, and the dialog
says what happened. ``$INGETRAZO_PRICES`` points at a JSON mirror (a
file path or URL) for testing and for shops that publish their own feed,
the same escape hatch ``$INGETRAZO_LIBRARY`` gives the library.

The fetch runs in a worker thread (urllib, the update_check pattern);
PriceSync.finished carries the result back to the GUI thread.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.request

from PySide6.QtCore import QObject, Signal

from core.prices import SRC_MOC
from core.version import USER_AGENT, __version__

API = "https://dataapi.moc.go.th/csi-indexes"
TIMEOUT = 12.0
MAX_BYTES = 8_000_000
SYNC_KEY = "Takeoff/last_sync"
PROVINCE_KEY = "Takeoff/province"

#: The 77 province codes of the MOC feed (code → Thai name).
PROVINCES = {
    "10": "กรุงเทพมหานคร", "11": "สมุทรปราการ", "12": "นนทบุรี",
    "13": "ปทุมธานี", "14": "พระนครศรีอยุธยา", "15": "อ่างทอง",
    "16": "ลพบุรี", "17": "สิงห์บุรี", "18": "ชัยนาท", "19": "สระบุรี",
    "20": "ชลบุรี", "21": "ระยอง", "22": "จันทบุรี", "23": "ตราด",
    "24": "ฉะเชิงเทรา", "25": "ปราจีนบุรี", "26": "นครนายก",
    "27": "สระแก้ว", "30": "นครราชสีมา", "31": "บุรีรัมย์",
    "32": "สุรินทร์", "33": "ศรีสะเกษ", "34": "อุบลราชธานี",
    "35": "ยโสธร", "36": "ชัยภูมิ", "37": "อำนาจเจริญ", "38": "บึงกาฬ",
    "39": "หนองบัวลำภู", "40": "ขอนแก่น", "41": "อุดรธานี", "42": "เลย",
    "43": "หนองคาย", "44": "มหาสารคาม", "45": "ร้อยเอ็ด",
    "46": "กาฬสินธุ์", "47": "สกลนคร", "48": "นครพนม", "49": "มุกดาหาร",
    "50": "เชียงใหม่", "51": "ลำพูน", "52": "ลำปาง", "53": "อุตรดิตถ์",
    "54": "แพร่", "55": "น่าน", "56": "พะเยา", "57": "เชียงราย",
    "58": "แม่ฮ่องสอน", "60": "นครสวรรค์", "61": "อุทัยธานี",
    "62": "กำแพงเพชร", "63": "ตาก", "64": "สุโขทัย", "65": "พิษณุโลก",
    "66": "พิจิตร", "67": "เพชรบูรณ์", "70": "ราชบุรี",
    "71": "กาญจนบุรี", "72": "สุพรรณบุรี", "73": "นครปฐม",
    "74": "สมุทรสาคร", "75": "สมุทรสงคราม", "76": "เพชรบุรี",
    "77": "ประจวบคีรีขันธ์", "80": "นครศรีธรรมราช", "81": "กระบี่",
    "82": "พังงา", "83": "ภูเก็ต", "84": "สุราษฎร์ธานี", "85": "ระนอง",
    "86": "ชุมพร", "90": "สงขลา", "91": "สตูล", "92": "ตรัง",
    "93": "พัทลุง", "94": "ปัตตานี", "95": "ยะลา", "96": "นราธิวาส",
}

#: Thai product-name keywords → (takeoff item, unit, compatible units).
#: Order matters: the first match wins, so specific names come first.
#: A record is only taken when its unit is compatible — no guessing
#: between a 50 kg bag and a 20 kg bag, or a kg and a 10 m bar.
NAME_MAP = [
    (("กาวก่ออิฐมวลเบา", "ปูนก่ออิฐมวลเบา"),
     [("AAC adhesive (20 kg bags)", "bag20", ("ถุง",))]),
    (("ปูนกาว", "กาวปูกระเบื้อง"),
     [("Tile adhesive (20 kg bags)", "bag20", ("ถุง",))]),
    (("คอนกรีตผสมเสร็จ",),
     [("Ready-mixed concrete — {part}", "m3", ("ลบ.ม.", "คิว"))]),
    (("ปูนซีเมนต์ปอร์ตแลนด์",),
     [("Cement (50 kg bags)", "bag50", ("ถุง",))]),
    (("ปูนซีเมนต์ผสม",),
     [("Laying cement (50 kg bags)", "bag50", ("ถุง",)),
      ("Plaster cement (50 kg bags)", "bag50", ("ถุง",))]),
    (("ทรายหยาบ",), [("Sand (m³)", "m3", ("ลบ.ม.", "คิว"))]),
    (("หินย่อย", "หินคลุก"), [("Gravel (m³)", "m3", ("ลบ.ม.", "คิว"))]),
    (("อิฐมอญ", "อิฐแดง"),
     [("Bricks — {type}", "piece", ("ก้อน",),
       {"type": "Clay brick (half-brick wall)"})]),
    (("อิฐบล็อก", "บล็อกคอนกรีต", "คอนกรีตบล็อก"),
     [("Bricks — {type}", "piece", ("ก้อน",),
       {"type": "Concrete block 7 cm"})]),
    (("อิฐมวลเบา", "มวลเบา"),
     [("Bricks — {type}", "piece", ("ก้อน",), {"type": "AAC block 7.5 cm"})]),
    (("เหล็กข้ออ้อย", "เหล็กเส้น", "เหล็กกลม"),
     [("Rebar steel (kg)", "kg", ("กก.",))]),
    (("กระเบื้องลอนคู่", "กระเบื้องหลังคา", "กระเบื้องคอนกรีต"),
     [("Roof tiles — {type}", "sheet", ("แผ่น",),
       {"type": "Double-roll fibre-cement tile 50×120 cm"})]),
    (("สีน้ำพลาสติก", "สีทาบ้าน", "สีอะคริลิก"),
     [("Topcoat (gallons)", "gallon", ("แกลลอน",))]),
]

def build_url(province: str, year: int) -> str:
    return f"{API}?province_code={province}&from_year={year}&to_year={year}"


def parse_records(raw: bytes) -> list:
    """The feed's JSON array → clean records; junk rows are dropped, a
    non-array answer raises (the caller reports it, nothing is cached)."""
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, list):
        raise ValueError("MOC answer is not a list")
    out = []
    for rec in data:
        if not isinstance(rec, dict):
            continue
        try:
            price = float(rec.get("price"))
            year = int(rec.get("year"))
            month = int(rec.get("month"))
        except (TypeError, ValueError):
            continue
        name = str(rec.get("com_name_th") or "")
        unit = str(rec.get("units") or rec.get("unit") or "")
        if price > 0 and name:
            out.append({"name": name, "unit": unit, "price": price,
                        "year": year, "month": month,
                        "province_name": str(rec.get("province_name") or "")})
    return out


def map_records(records: list, province: str) -> dict:
    """Latest month per product, matched conservatively (name keyword +
    compatible unit) → the cache dict core.prices consumes."""
    from core.prices import PriceBook
    latest: dict = {}
    for rec in records:
        # The feed lists one product under several units (a bar vs a kg):
        # dedupe by name AND unit, or the compatible unit loses the race.
        key = (rec["name"], rec["unit"])
        prev = latest.get(key)
        if prev is None or (rec["year"], rec["month"]) > (prev["year"],
                                                          prev["month"]):
            latest[key] = rec
    out: dict = {}
    for rec in latest.values():
        for keywords, targets in NAME_MAP:
            if not any(k in rec["name"] for k in keywords):
                continue
            for target in targets:
                item, unit, units = target[0], target[1], target[2]
                args = target[3] if len(target) > 3 else {}
                if not any(u in rec["unit"] for u in units):
                    continue
                key = PriceBook.key(item, unit, args)
                out[key] = {
                    "thb": rec["price"], "source": SRC_MOC,
                    "date": f"{rec['year'] + 543}-{rec['month']:02d}",
                    "province": rec["province_name"]
                    or PROVINCES.get(province, province)}
            break                       # first matching keyword group wins
    return out


def _get(url: str) -> bytes:
    req = urllib.request.Request(
        url, headers={"User-Agent": f"{USER_AGENT} IngeTrazo/{__version__}"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read(MAX_BYTES)


def _read_mirror(mirror: str) -> bytes:
    if mirror.startswith(("http://", "https://")):
        return _get(mirror)
    path = mirror[7:] if mirror.startswith("file://") else mirror
    with open(path, "rb") as fh:
        return fh.read(MAX_BYTES)


def fetch_prices(province: str, year: int | None = None) -> list:
    """One province's records: this year, falling back to last year when
    the feed has nothing yet. ``$INGETRAZO_PRICES`` (a JSON file path or
    URL) replaces the network entirely — mirrors and tests."""
    import datetime
    mirror = os.environ.get("INGETRAZO_PRICES", "")
    if mirror:
        return parse_records(_read_mirror(mirror))
    year = year or datetime.date.today().year
    records = parse_records(_get(build_url(province, year)))
    if not records:
        records = parse_records(_get(build_url(province, year - 1)))
    return records


class PriceSync(QObject):
    """The worker behind the dialog's sync button: fetch off the GUI
    thread, deliver (entries, message template, kwargs) back on it."""
    finished = Signal(dict, str, dict)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def start(self, province: str) -> None:
        if self._running:
            return
        self._running = True
        threading.Thread(target=self._work, args=(province,),
                         daemon=True).start()

    def _work(self, province: str) -> None:
        name = PROVINCES.get(province, province)
        try:
            entries = map_records(fetch_prices(province), province)
            if entries:
                self.finished.emit(entries, "Synced {n} prices from MOC ({province})",
                                   {"n": len(entries), "province": name})
            else:
                self.finished.emit({}, "No matching products in the MOC feed for {province}",
                                   {"province": name})
        except Exception as exc:        # noqa: BLE001 — reported, never raised
            self.finished.emit({}, "Sync failed: {error}",
                               {"error": str(exc)[:120]})
        finally:
            self._running = False