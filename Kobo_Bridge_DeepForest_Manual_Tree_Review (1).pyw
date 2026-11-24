import os
import sys
import json
import threading
import re
import urllib.parse
import tkinter as tk
import math
import pgs_store
from tkinter import messagebox, ttk
from datetime import datetime, timezone, timedelta
import time
from collections import Counter
from urllib.parse import quote
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import io
import requests
from concurrent.futures import ThreadPoolExecutor
try:
    from PIL import Image, ImageDraw
    PIL_OK = True
except ImportError:
    PIL_OK = False
# Divide Work needs 3 extra libraries. If they are missing the main tool still starts normally
# and only the Divide Work button explains what to install (pip install requests gspread google-auth).
try:
    import gspread
    from google.oauth2.service_account import Credentials
    DW_LIBS_OK = True
    DW_LIBS_ERROR = ""
except ImportError as _dw_err:
    DW_LIBS_OK = False
    DW_LIBS_ERROR = str(_dw_err)

latest_attachments = []
pending_open = None   # documents waiting for the extension to pick up

# -------------------- Portable Paths & Geometry -------------------- #
if getattr(sys, "frozen", False):
    SCRIPT_DIR = os.path.dirname(sys.executable)
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def load_window_geometry() -> str:
    g = pgs_store.get("window_geometry", "")
    return g if isinstance(g, str) and "x" in g and "+" in g else "440x580+100+100"

def save_window_geometry() -> None:
    try:
        pgs_store.set("window_geometry", root.geometry())
    except Exception:
        pass

# -------------------- Rule Validation Engine -------------------- #
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_STRIP_RE = re.compile(r"[\u0640\u064B-\u065F\u200B-\u200F\u202A-\u202E\u2066-\u2069\ufeff]")

from functools import lru_cache

@lru_cache(maxsize=4096)
def _norm_cached(s: str) -> str:
    s = s.translate(_AR_DIGITS)
    s = _STRIP_RE.sub("", s)
    s = s.replace("\u00a0", " ")
    for a in "أإآ":
        s = s.replace(a, "ا")
    s = s.replace("ى", "ي").replace("ة", "ه")
    return " ".join(s.split()).strip()

def norm(s) -> str:
    return "" if s is None else _norm_cached(str(s))
def L(row) -> str: return norm(row.get("label"))
def V(row) -> str: return norm(row.get("value"))
def K(row) -> str: return str(row.get("link") or "").strip()

def is_yes(v: str) -> bool:
    return norm(v).lstrip("-. ").startswith(norm("نعم"))

def to_float(s):
    s = norm(s).replace("٫", ".").replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None

def first_row(table, text, exact=False):
    t = norm(text)
    for row in table:
        l = L(row)
        if (l == t) if exact else (t in l):
            return row
    return None

def has_link(table, text, exact=False, soft=False) -> bool:
    if soft:
        return True
    t = norm(text)
    for row in table:
        l = L(row)
        if ((l == t) if exact else (t in l)) and (K(row) or V(row)):
            return True
    return False

def section(table, start_text, end_texts):
    st = norm(start_text)
    start = None
    for i, row in enumerate(table):
        if st in L(row):
            start = i
            break
    if start is None:
        return []
    ends = [norm(e) for e in end_texts]
    end = len(table)
    for j in range(start + 1, len(table)):
        if any(e in L(table[j]) for e in ends):
            end = j
            break
    return table[start:end]

def is_valid_egypt_phone(phone_str: str) -> bool:
    clean = "".join(c for c in norm(phone_str) if c.isdigit())
    if clean.startswith("0020"): clean = "0" + clean[4:]
    elif clean.startswith("20") and len(clean) in (11, 12): clean = "0" + clean[2:]
    if len(clean) == 10 and clean.startswith(("10", "11", "12", "15")): clean = "0" + clean

    if len(clean) == 11 and clean.startswith(('010', '011', '012', '015')): return True
    landline_prefixes = ('02', '03', '040', '045', '047', '048', '050', '055', '057',
                         '062', '064', '065', '066', '068', '069', '082', '084',
                         '086', '088', '092', '093', '095', '096', '097')
    return len(clean) in (9, 10) and clean.startswith(landline_prefixes)

def parse_kobo_date(date_str: str):
    formats = ["%b %d, %Y", "%B %d, %Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"]
    for fmt in formats:
        try: return datetime.strptime(str(date_str).strip(), fmt).date()
        except ValueError: pass
    return None

def point_in_polygon(lat, lng, poly):
    """True if the point is inside the polygon (ray casting). poly = [(lat, lng), ...]"""
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        yi, xi = poly[i][0], poly[i][1]
        yj, xj = poly[j][0], poly[j][1]
        if (yi > lat) != (yj > lat) and lng < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside

def meters_to_polygon_edge(lat, lng, poly):
    """Shortest distance (in meters) from a point to the polygon border."""
    m_lat = 111132.92
    m_lng = 111412.84 * math.cos(math.radians(lat))
    px, py = 0.0, 0.0
    best = float("inf")
    for i in range(len(poly)):
        a, b = poly[i], poly[(i + 1) % len(poly)]
        ax, ay = (a[1] - lng) * m_lng, (a[0] - lat) * m_lat
        bx, by = (b[1] - lng) * m_lng, (b[0] - lat) * m_lat
        dx, dy = bx - ax, by - ay
        seg = dx * dx + dy * dy
        t = 0.0 if seg == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg))
        cx, cy = ax + t * dx, ay + t * dy
        best = min(best, math.hypot(cx - px, cy - py))
    return best

def soil_points_from_table(table):
    """Read the soil sample coordinates exactly as they appear in the checklist table."""
    pts = []
    for row in table:
        if norm("نقاط اخذ عينة للتربة") in L(row):
            nums = re.findall(r"-?\d+\.\d+", str(row.get("value") or ""))   # header 'x.y' has no digits so it is skipped
            if len(nums) >= 2: pts.append((float(nums[0]), float(nums[1])))
    return pts

def parse_area_feddan(s):
    """'4 فدان' -> 4 | 'فدان' -> 1 | 'فدانين' -> 2 | '3 قيراط' -> 3/24 | '4' -> 4"""
    s = norm(s).replace("٫", ".").replace(",", ".")
    if "فدانين" in s: return 2.0
    units = {"فدان": 1.0, "قيراط": 1 / 24, "سهم": 1 / 576}
    total, found = 0.0, False
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(فدان|قيراط|سهم)", s):
        total += float(m.group(1)) * units[m.group(2)]; found = True
    if found: return total
    if "فدان" in s: return 1.0
    return to_float(s)

SOIL_MIN_SAMPLES = 4          # 4 is OK, less than 4 is a problem
SOIL_EDGE_TOLERANCE_M = 5     # a sample up to 5 meters outside the border is still accepted (GPS noise)

def check_rules(table: list, calc_area=None, farm_polygon=None, soil_samples=None) -> list:
    broken_rules = []
    add = broken_rules.append
    today = datetime.now().date()
    n = len(table)
    links_ok = any(K(r) for r in table)

    total_farm_area = 0.0
    r = first_row(table, "إجمالي المساحة بالفدان")
    if r is not None:
        total_farm_area = to_float(r.get("value")) or 0.0

    # drawn polygon area vs the area written in the form (we accept +-10%)
    if calc_area and total_farm_area > 0:
        if abs(calc_area - total_farm_area) / total_farm_area > 0.10:
            add(f"المزرعة المرسومة {round(calc_area, 2):g} فدان لكن الاستمارة مكتوب فيها {total_farm_area:g} فدان")

    # ---- soil samples: count + inside the farm polygon ----
    # The checklist table is the source of truth. The map can send a ghost point that is not in the checklist.
    soils = soil_points_from_table(table) or [(float(p[0]), float(p[1])) for p in (soil_samples or []) if p and len(p) >= 2]
    if len(soils) < SOIL_MIN_SAMPLES:
        add(f"عينات التربة ناقصة ({len(soils)} من {SOIL_MIN_SAMPLES})")
    poly = [(float(p[0]), float(p[1])) for p in (farm_polygon or []) if p and len(p) >= 2]
    if len(poly) >= 3 and soils:
        outside = [i + 1 for i, (la, ln) in enumerate(soils)
                   if not point_in_polygon(la, ln, poly) and meters_to_polygon_edge(la, ln, poly) > SOIL_EDGE_TOLERANCE_M]
        inside_count = len(soils) - len(outside)
        # if at least 4 samples are inside, extra samples outside the farm are ignored
        if outside and inside_count < SOIL_MIN_SAMPLES:
            nums = "، ".join(str(x) for x in outside)
            add(f"عينة التربة رقم {nums} برا حدود المزرعة" if len(outside) == 1 else f"عينات التربة رقم {nums} برا حدود المزرعة")
    
    r = first_row(table, "تاريخ الزيارة")
    if r is not None:
        v_date = parse_kobo_date(r.get("value", ""))
        if v_date and v_date > today: add("تاريخ الزيارة في المستقبل")

    r = first_row(table, "رقم تليفون ممثل")
    if r is not None:
        phone_val = str(r.get("value") or "").strip()
        if phone_val and not is_valid_egypt_phone(phone_val): add("رقم تليفون ممثل المزرعة غلط")

    auditor_photos_count = sum(1 for row in table if norm("صورة لعضو لجنة المراجعة") in L(row) and (K(row) or V(row)))
    if links_ok and auditor_photos_count < 3: add(f"صور لجنة المراجعة ناقصة ({auditor_photos_count} من 3)")

    ownership_type = ""
    r = first_row(table, "مملوكة لمالك واحد") or first_row(table, "لمجموعة مزارعين")
    if r is not None: ownership_type = V(r)

    if norm("مجموعة مزارعين") in ownership_type:
        farmer_names, basin_areas, farmer_phones = [], [], []
        last_owner = ""
        for row in table:
            lbl, val = L(row), V(row)
            if norm("اسم صاحب الحوض") in lbl and val:
                farmer_names.append(val)
                last_owner = str(row.get("value")).strip()
            elif norm("مساحة الحوض بالفدان") in lbl and val:
                f = to_float(val)
                if f is not None: basin_areas.append(f)
            elif lbl == norm("التليفون") and val: farmer_phones.append((last_owner, str(row.get("value")).strip()))

        if len(farmer_names) == 1: add("قائمة المزارعين فيها مزارع واحد بس")
        for owner, ph in farmer_phones:
            if not is_valid_egypt_phone(ph):
                add(f"رقم تليفون {owner} غلط" if owner else "فيه رقم تليفون غلط في قائمة المزارعين")
        summed_basin_area = sum(basin_areas)
        if abs(summed_basin_area - total_farm_area) > 0.001:
            add(f"مساحات المزارعين ({summed_basin_area:g} فدان) مش زي مساحة المزرعة ({total_farm_area:g} فدان)")

    if not has_link(table, "ارفق صورة الحيازة"): add("الحيازة مش مرفقة")
    if not has_link(table, "الرجاء ارفاق بيان التنوع الحيوي"): add("بيان التنوع الحيوي مش مرفق")
    if not has_link(table, "الرجاء ارفاق بيان GMO"): add("بيان GMO مش مرفق")

    CROP_NAME = norm("اسم المحصول")
    BOUNDARIES = {CROP_NAME, norm("رقم الحوض"), norm("نوع المحصول")}
    CUR, PREV = norm("الموسم الحالي"), norm("الموسم السابق")
    PHOTO_LABELS = (norm("ارفق الصورة"), norm("ارفق صورة"))

    current_crop_areas, previous_crop_areas = [], []
    has_current_crop_photo = True
    has_previous_crop_data = False

    for i in range(n):
        if L(table[i]) != CROP_NAME: continue
        crop_name = V(table[i])

        end, marker = n, None
        for j in range(i + 1, n):
            lj = L(table[j])
            if lj in BOUNDARIES: end = j; break
            if CUR in lj: marker, end = "cur", j + 1; break
            if PREV in lj: marker, end = "prev", j + 1; break
        group = table[i:end]

        if marker is None:
            labels = [L(g) for g in group]
            marker = "cur" if norm("الانتاجية المتوقعه") in labels else ("prev" if norm("الانتاجية") in labels else "cur")

        area = next((parse_area_feddan(g.get("value")) for g in group if L(g) == norm("المساحة")), None)

        if marker == "cur":
            if area is not None: current_crop_areas.append(area)
            photo_ok = any(L(g) in PHOTO_LABELS and (K(g) or V(g)) for g in group)
            k = end
            while not photo_ok and k < n and k < end + 3 and L(table[k]) not in BOUNDARIES:
                if L(table[k]) in PHOTO_LABELS: photo_ok = bool(K(table[k]) or V(table[k])); break
                k += 1
            if links_ok and not photo_ok: has_current_crop_photo = False
        else:
            if crop_name: has_previous_crop_data = True
            if area is not None: previous_crop_areas.append(area)

    if not has_current_crop_photo: add("صورة المحصول الحالي مش مرفقة")
    sum_current_area = sum(current_crop_areas)
    if abs(sum_current_area - total_farm_area) > 0.001:
        add(f"المساحة المزروعة هذا الموسم {total_farm_area:g} فدان حسب الاستمارة، لكن بيانات المحاصيل المدخلة مجموعها {sum_current_area:g} فدان فقط")

    if not has_previous_crop_data: add("بيانات المحاصيل السابقة ناقصة")
    else:
        sum_prev_area = sum(previous_crop_areas)
        if abs(sum_prev_area - total_farm_area) > 0.001:
            add(f"المساحة المزروعة الموسم السابق {total_farm_area:g} فدان حسب الاستمارة، لكن بيانات المحاصيل المدخلة مجموعها {sum_prev_area:g} فدان فقط")

    if not has_link(table, "ارفاق صورة الدفتر", soft=not links_ok): add("دفتر المزرعة مش مرفق")

    r = first_row(table, "هل توجد اشجار فى المزرعه")
    if r is not None and is_yes(r.get("value")):
        tree_rows = section(table, "هل توجد اشجار", ["هل توجد حيوانات"])
        if not any(L(x) == norm("النوع") and V(x) for x in tree_rows): add("نوع الأشجار ناقص")
        if links_ok and not any(L(x) in PHOTO_LABELS and (K(x) or V(x)) for x in tree_rows): add("صورة الأشجار مش مرفقة")

    r = first_row(table, "هل توجد حيوانات فى المزرعه")
    if r is not None and is_yes(r.get("value")):
        rows = section(table, "هل توجد حيوانات", ["نوع التربة"])
        animal_type = animal_count = animal_age = feed_components = ""
        animal_img = stable_file = False
        for x in rows:
            lbl, val, link = L(x), V(x), K(x)
            if lbl == norm("النوع") and val: animal_type = val
            elif lbl == norm("العدد") and val: animal_count = val
            elif lbl == norm("العمر") and val: animal_age = val
            elif lbl in PHOTO_LABELS and (link or val): animal_img = True
            elif lbl == norm("مكونات العلف") and val: feed_components = val
            elif lbl == norm("ارفق ملف الاسطبل") and (val or link): stable_file = True

        if not animal_type: add("نوع الحيوان ناقص")
        if not animal_count: add("عدد الحيوانات ناقص")
        if not animal_age: add("عمر الحيوانات ناقص")
        if links_ok and not animal_img: add("صورة الحيوانات مش مرفقة")
        if not feed_components: add("مكونات العلف ناقصة")
        if not stable_file: add("ملف الاسطبل مش مرفق")

    if not any(L(x) == norm("نوع التربة") and V(x) for x in table): add("نوع التربة ناقص")

    fert_rows = section(table, "نوع السماد", ["مستحضرات", "هل يتوفر للعمال"])
    fertilizer_type = fertilizer_source = ""
    animal_waste_pct = plant_waste_pct = 0.0
    pct_present = fertilizer_img = False

    for x in fert_rows:
        lbl, val = L(x), V(x)
        if lbl == norm("نوع السماد") and val and not fertilizer_type: fertilizer_type = val
        elif lbl == norm("المصدر") and val: fertilizer_source = val
        elif norm("نسبة المخلفات الحيوانية") in lbl and val:
            f = to_float(val)
            if f is not None: animal_waste_pct, pct_present = f, True
        elif norm("نسبة المخلفات النباتية") in lbl and val:
            f = to_float(val)
            if f is not None: plant_waste_pct, pct_present = f, True
        elif lbl in PHOTO_LABELS and (K(x) or val): fertilizer_img = True

    if not fertilizer_type: add("نوع السماد ناقص")
    if norm("من داخل المزرعه") in fertilizer_source and not (r is not None and is_yes(r.get("value"))):
        add("السماد من داخل المزرعة ومفيش حيوانات")
    if (norm("كمبوست") in fertilizer_type or pct_present) and abs((animal_waste_pct + plant_waste_pct) - 100.0) > 0.001:
        add("نسبة المخلفات الحيوانية والنباتية مجموعها مش 100%")
    if links_ok and not fertilizer_img: add("صورة السماد مش مرفقة")

    r_bio = first_row(table, "مستحضرات بايودينامكية")
    if r_bio is not None and is_yes(r_bio.get("value")):
        if not has_link(table, "ارفق اذن الصرف", soft=not links_ok): add("اذن الصرف مش مرفق")

    labor_rules = [
        ("اذكر قائمة باسماء العماله النسائية الدائمة والموسمية", "قائمة العمالة النسائية مش مرفقة"),
        ("الرجاء ارفاق قائمه باسماء العمالة الدائمه والموسميه", "قائمة العمالة مش مرفقة"),
        ("الرجاء ارفاق سياسة تشغيل النساء", "سياسة تشغيل النساء مش مرفقة"),
        ("محضر الاجتماعات", "محضر الاجتماعات مش مرفق"),
    ]
    for req_label, err_msg in labor_rules:
        if not has_link(table, req_label): add(err_msg)

    inspection_type, has_owner_video = "", False
    for x in table:
        lbl = L(x)
        if norm("نوع زيارة التفتيش") in lbl: inspection_type = V(x)
        if norm("مقابلة مع مالك المزرعة") in lbl and (K(x) or V(x)): has_owner_video = True

    is_follow_up = any(w in inspection_type.lower() for w in ("follow", "yearly", norm("سنوي"), norm("متابعه")))
    if not is_follow_up:
        is_follow_up = any(("follow_up" in str(x.get("value") or "").lower() or "yearly" in str(x.get("value") or "").lower()) for x in table)
    if not is_follow_up:
        if not has_owner_video: add("فيديو مقابلة مالك المزرعة مش مرفق")

    found_power_source = False
    POWER = norm("مصدر الطاقة")
    for i in range(n):
        if L(table[i]) != POWER: continue
        found_power_source = True
        power_type = V(table[i])
        sub_rows = table[i+1:min(i+8, n)]

        if power_type == norm("ديزل"):
            eq = next((V(x) for x in sub_rows if L(x) == norm("نوع المعده")), "")
            cnt = next((V(x) for x in sub_rows if L(x) == norm("العدد")), "")
            pw = next((V(x) for x in sub_rows if L(x) == norm("قدرة المولد")), "")
            img = any(L(x) in PHOTO_LABELS and (K(x) or V(x)) for x in sub_rows)
            if not eq: add("نوع معدة الديزل ناقص")
            if not cnt: add("عدد معدات الديزل ناقص")
            if not pw: add("قدرة مولد الديزل ناقصة")
            if links_ok and not img: add("صورة معدة الديزل مش مرفقة")
        elif power_type == norm("حكومي"):
            if links_ok and not any(norm("ارفق صورة للفاتورة او العداد") in L(x) and (K(x) or V(x)) for x in sub_rows):
                add("صورة فاتورة أو عداد الكهرباء مش مرفقة")
        elif power_type == norm("متجدد"):
            panels = next((V(x) for x in sub_rows if L(x) == norm("عدد الالواح")), "")
            power_h = next((V(x) for x in sub_rows if norm("القدرة") in L(x)), "")
            img = any(L(x) in PHOTO_LABELS and (K(x) or V(x)) for x in sub_rows)
            if not panels: add("عدد ألواح الطاقة المتجددة ناقص")
            if not power_h: add("قدرة الطاقة المتجددة ناقصة")
            if links_ok and not img: add("صورة الطاقة المتجددة مش مرفقة")

    if not found_power_source: add("مصدر الطاقة ناقص")

    return broken_rules

def format_problems(problems: list) -> str:
    """One line:  - problem 1 -- problem 2 -- problem 3 -"""
    clean = [" ".join(str(p).split()) for p in problems if p]
    return ("- " + " -- ".join(clean) + " -") if clean else ""

# -------------------- Farm history (last 15 farms) -------------------- #
HISTORY_MAX = 5
farm_history = []   # newest first: [{"code": "...", "gov": "..."}]

def load_history() -> None:
    global farm_history
    data = pgs_store.get("farm_history", [])
    farm_history = [h for h in data if isinstance(h, dict) and h.get("code")][:HISTORY_MAX] if isinstance(data, list) else []

def save_history() -> None:
    try:
        pgs_store.set("farm_history", farm_history)
    except Exception:
        pass

def add_to_history(code: str, gov: str) -> None:
    global farm_history
    code, gov = (code or "").strip(), (gov or "").strip()
    if not code or code == "--":
        return
    if farm_history and farm_history[0]["code"] == code and farm_history[0].get("gov") == gov:
        return
    farm_history = [h for h in farm_history if h["code"] != code]
    farm_history.insert(0, {"code": code, "gov": gov})
    del farm_history[HISTORY_MAX:]
    save_history()

load_history()

# -------------------- Target Documents & Extraction -------------------- #
TARGET_DOC_RULES = [
    ("الحيازة", "ارفق صورة الحيازة", "الحيازة", False),
    ("التنوع الحيوي", "الرجاء ارفاق بيان التنوع الحيوي", "التنوع الحيوي", False),
    ("GMO", "الرجاء ارفاق بيان GMO", "GMO", False),
    ("الدفتر", "ارفاق صورة الدفتر", "الدفتر", False),
    ("ملف الاسطبل", "ارفق ملف الاسطبل", "ملف الاسطبل", False),
    ("اذن الصرف", "ارفق اذن الصرف موضحا فيه الكمية", "اذن الصرف", False),
    ("العماله النسائية", "اذكر قائمة باسماء العماله النسائية الدائمة والموسمية", "اذكر قائمة باسماء العماله النسائية الدائمة والموسمية", True),
    ("العمالة الدائمه", "الرجاء ارفاق قائمه باسماء العمالة الدائمه والموسميه", "العمالة الدائمه", False),
    ("تشغيل النساء", "الرجاء ارفاق سياسة تشغيل النساء", "تشغيل النساء", False),
    ("محضر الاجتماعات", "محضر الاجتماعات", "محضر الاجتماعات", False),
    ("مقابلة مع مالك المزرعة", "مقابلة مع مالك المزرعة", "مقابلة مع مالك المزرعة", False),
]

def extract_attachments(table: list) -> list:
    attachments = []
    file_exts = (".jpg", ".jpeg", ".png", ".pdf", ".mp4", ".doc", ".docx", ".xlsx")

    for rule_key, rule_search, header_title, is_pink in TARGET_DOC_RULES:
        for row in table:
            lbl = norm(row.get("label"))
            val = str(row.get("value", "")).strip()
            link = str(row.get("link", "")).strip()
            target_url = link if link.startswith("http") else val

            if norm(rule_search) in lbl and target_url and (
                link.startswith("http")
                or any(target_url.lower().endswith(ext) for ext in file_exts)
            ):
                if not any(a["url"] == target_url for a in attachments):
                    attachments.append({
                        "label": row.get("label", "").strip(),
                        "short_name": rule_key,
                        "header_title": header_title,
                        "is_pink": is_pink,
                        "url": target_url,
                    })
                break
    return attachments

def open_all_documents_auto(attachments):
    """Puts the ordered document list on the shelf; the extension picks it up and opens them one by one."""
    global pending_open
    if not attachments:
        return
    pending_open = [
        {
            "name": a["short_name"],
            "title": a["header_title"],
            "pink": bool(a.get("is_pink")),
            "url": a["url"],
        }
        for a in attachments
    ]
    log_to_console(f"📤 Sent {len(pending_open)} documents to the extension, opening one by one...")

def send_docs_to_extension():
    """Triggered directly by clicking the blue Docs button on the main UI."""
    if not latest_attachments:
        messagebox.showinfo("Documents", "No documents found in current submission.")
        return

    kobo_user = pgs_store.get("kobo_user", "")
    if not kobo_user:
        messagebox.showwarning("Kobo User", "Click the Kobo User button and paste your API token first.")
        return

    open_all_documents_auto(latest_attachments)

    code = field_vars["farm_code"].get().strip()
    gov = field_vars["governate"].get().strip()
    if not code or code == "--":
        log_to_console("⚠️ Docs opened, but no farm code was found - nothing was logged.")
        return
    threading.Thread(target=log_docs_click, args=(code, gov, kobo_user), daemon=True).start()

# -------------------- Manual tree review handoff -------------------- #
_tree_review_queue = []
_last_table_rows = []   # latest KoBo submission table, used by the console copy button
_tree_review_sessions = {}
_tree_review_active_code = ""
_tree_review_last_queued_code = ""
_tree_review_lock = threading.Lock()


def _tree_block_from_table(table):
    """Return ordered tree-section rows; never pick animal/equipment العدد rows."""
    start = None
    for i, row in enumerate(table):
        label = norm(row.get("label", ""))
        if label == norm("بيانات الاشجار") or label == norm("هل توجد اشجار فى المزرعه"):
            start = i
            break
    if start is None:
        return []
    end = len(table)
    stop_labels = ("هل توجد حيوانات فى المزرعه", "بيانات الحيوانات", "نوع التربة")
    for i in range(start + 1, len(table)):
        label = norm(table[i].get("label", ""))
        if any(norm(stop) in label for stop in stop_labels):
            end = i
            break
    return [
        {"label": str(row.get("label", "")).strip(), "value": str(row.get("value", "")).strip()}
        for row in table[start:end]
        if str(row.get("label", "")).strip() or str(row.get("value", "")).strip()
    ]


def _tree_counts_from_rows(rows):
    counts = []
    for row in rows:
        if norm(row.get("label", "")) == norm("العدد"):
            value = to_float(row.get("value", ""))
            if value is not None and value >= 0:
                counts.append(int(round(value)))
    return counts


def _safe_farm_folder(code):
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", str(code or "").strip()).strip("_")
    return safe or "farm"


# -------------------- Flask Server -------------------- #
app = Flask(__name__)
CORS(app)
import flask.cli
flask.cli.show_server_banner = lambda *a, **k: None
import logging
logging.getLogger("werkzeug").setLevel(logging.ERROR)

@app.route('/kobo-data', methods=['POST'])
def receive_kobo_data():
    global _last_table_rows
    try:
        data = request.get_json(force=True, silent=True) or {}
        table = data.get('table', [])
        extracted = data.get('extracted_info', {})

        farm_polygon = extracted.get('farm_polygon', [])
        soil_samples = extracted.get('soil_samples', [])
        area_faddan = extracted.get('area_faddan', 0)

        _last_table_rows = table
        process_incoming_payload(data)
        return jsonify({"status": "success", "message": "Data received successfully"}), 200

    except Exception as e:
        print(f"❌ Error processing request: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/open-queue', methods=['GET'])
def open_queue():
    """The extension asks this every second. Hands over the waiting documents once, then clears the shelf."""
    global pending_open
    docs, pending_open = pending_open, None
    return jsonify({"docs": docs or []})

@app.route('/tree-counter/open-queue', methods=['GET'])
def tree_counter_open_queue():
    """One-shot queue: background extension opens a review tab for each new farm code."""
    global _tree_review_queue
    with _tree_review_lock:
        jobs, _tree_review_queue = _tree_review_queue, []
    return jsonify({"jobs": jobs})


@app.route('/tree-counter/session', methods=['GET'])
def tree_counter_session():
    code = str(request.args.get('farm_code', '')).strip()
    with _tree_review_lock:
        session = _tree_review_sessions.get(code)
    if not session:
        return jsonify({"error": "Farm review session not found"}), 404
    return jsonify(session)


@app.route('/tree-counter/image/<path:farm_code>', methods=['GET'])
def tree_counter_image(farm_code):
    path = os.path.join(SAT_DIR, _safe_farm_folder(farm_code), "01_satellite_input_clean.png")
    if not os.path.isfile(path):
        return jsonify({"error": "Satellite image is not ready yet"}), 404
    return send_file(path, mimetype="image/png", conditional=True)


@app.route('/tree-counter/result', methods=['POST'])
def tree_counter_result():
    global _tree_review_active_code
    data = request.get_json(force=True, silent=True) or {}
    code = str(data.get("farm_code", "")).strip()
    try:
        manual_count = int(data.get("manual_count"))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Invalid manual count"}), 400
    if not code or manual_count < 0:
        return jsonify({"status": "error", "message": "Farm code/count is invalid"}), 400

    # Take the lock ONCE. The old version nested it, which froze the app.
    with _tree_review_lock:
        session = _tree_review_sessions.get(code)
        if not session:
            return jsonify({"status": "error", "message": "Unknown farm review"}), 404
        submitted = int(session.get("submitted_total", 0))
        too_many = manual_count > submitted
        too_few = submitted > 0 and manual_count < submitted and ((submitted - manual_count) / submitted) > 0.10
        mismatch = too_many or too_few
        tree_problem = (
            f"عدد الأشجار المعدودة {manual_count}، والعدد في الاستمارة {submitted}. برجاء التصحيح"
            if mismatch else ""
        )
        session["manual_count"] = manual_count
        session["tree_checked"] = True
        session["tree_problem"] = tree_problem
        _tree_review_sessions[code] = session
        active = (_tree_review_active_code == code)
        base_problems = list(session.get("base_problems", []))

        if active:
            def update_tree_status():
                problems = list(base_problems)
                if tree_problem:
                    problems.insert(0, tree_problem)
                set_rule_text(format_problems(problems), bool(problems))
                log_to_console(
                    f"🌳 Tree review completed for {code}: "
                    f"manual={manual_count}, checklist={submitted}; "
                    f"{'MISMATCH' if mismatch else 'OK'}"
                )
            root.after(0, update_tree_status)   # Tk must be updated from the main thread

        return jsonify({
                    "status": "success",
                    "mismatch": mismatch,
                    "tree_problem": tree_problem,
                    "manual_count": manual_count,
                    "checklist_count": submitted,
                }), 200


@app.after_request
def after_request(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

# -------------------- Tkinter GUI -------------------- #
BG = "#1e1e1e"
PANEL = "#2b2b2b"
GREEN = "#00ff90"
MUTED = "#aaaaaa"
RED_BG = "#721c24"
BORDER = "#3a3a3a"
FONT = ("Segoe UI", 9, "bold")

root = tk.Tk()
root.title("Kobo Bridge")
root.geometry(load_window_geometry())
root.configure(bg=BG)
root.attributes("-topmost", True)

root.columnconfigure(0, weight=1)
root.rowconfigure(4, weight=1)   # console takes the leftover space

# ---------- Farm fields (labels on the left, text right-to-left) ----------
FIELD_TARGETS = [
    ("Farm Code", "farm_code", "كود المزرعة", "كود"),
    ("Governate", "governate", "المحافظة", "محافظة"),
    ("Farm Area", "farm_area", "إجمالي المساحة بالفدان", "المساحة"),
    ("Farm Name", "farm_name", "اسم المزرعة", "المزرعة"),
    ("Farm Rep", "rep_name", "اسم ممثل المزرعة", "ممثل"),
]

fields_frame = tk.Frame(root, bg=BG)
fields_frame.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
fields_frame.columnconfigure(1, weight=1)

entries = {}
field_vars = {}

for idx, (label_text, key, _, _) in enumerate(FIELD_TARGETS):
    tk.Label(fields_frame, text=label_text, font=FONT, bg=BG, fg=MUTED,
             anchor="w", width=10).grid(row=idx, column=0, sticky="w", padx=(0, 6), pady=2)

    var = tk.StringVar(value="--")
    field_vars[key] = var

    ent = tk.Entry(fields_frame, textvariable=var, font=FONT, bg=PANEL, fg=GREEN,
                   relief="flat", bd=0, justify="right", insertbackground="white",
                   highlightthickness=1, highlightbackground=BORDER, highlightcolor="#2980b9")
    ent.grid(row=idx, column=1, sticky="ew", ipady=3, pady=2)
    entries[key] = ent

def set_kobo_user(name: str) -> None:
    kobo_user_var.set(name)
    kobo_user_btn.config(text=f"{name}  ▾" if name else "-- click: set my API token --")

def prompt_kobo_token() -> None:
    from tkinter import simpledialog
    tok = simpledialog.askstring(
        "Kobo API token",
        "Paste YOUR KoboToolbox API token.\n(Kobo → Account Settings → Security → API key)",
        show="*", parent=root)
    if not tok or not tok.strip():
        return
    tok = tok.strip()
    try:
        r = requests.get(f"{SERVER_URL.rstrip('/')}/me/?format=json",
        headers={"Authorization": f"Token {tok}"}, timeout=15)
        if r.status_code in (401, 403):
            messagebox.showerror("Kobo token", "Kobo says this token is not valid.")
            return
        r.raise_for_status()
        name = str(r.json().get("username") or "").strip()
    except Exception as e:
        messagebox.showerror("Kobo token", f"Could not check the token:\n{e}")
        return
    if not name:
        messagebox.showerror("Kobo token", "Kobo did not return a username for this token.")
        return
    global _uid_cache
    _uid_cache = None
    pgs_store.set("kobo_token", tok)
    pgs_store.set("kobo_user", name)
    set_kobo_user(name)

tk.Label(fields_frame, text="Kobo User", font=FONT, bg=BG, fg=MUTED,
anchor="w", width=10).grid(row=len(FIELD_TARGETS), column=0, sticky="w", padx=(0, 6), pady=2)

kobo_user_var = tk.StringVar(value=pgs_store.get("kobo_user", ""))
kobo_user_btn = tk.Button(fields_frame, text="", font=FONT, bg=PANEL, fg=GREEN,
activebackground=PANEL, activeforeground=GREEN, relief="flat", bd=0,
anchor="e", padx=6, cursor="hand2", command=prompt_kobo_token,
highlightthickness=1, highlightbackground=BORDER, highlightcolor="#2980b9")
kobo_user_btn.grid(row=len(FIELD_TARGETS), column=1, sticky="ew", ipady=3, pady=2)
set_kobo_user(kobo_user_var.get())

# ---------- Green copy bar: covers Farm Code + Governate, copies both as a 2-column table ----------
def copy_code_and_gov():
    text = f"{field_vars['farm_code'].get().strip()}\t{field_vars['governate'].get().strip()}"
    root.clipboard_clear()
    root.clipboard_append(text)
    root.update()
    cg_btn.config(text="✓")
    root.after(1000, lambda: cg_btn.config(text="📋"))

cg_btn = tk.Button(fields_frame, text="📋", font=("Segoe UI", 9, "bold"),
                   bg="#27ae60", fg="white", activebackground="#2ecc71", activeforeground="white",
                   relief="flat", bd=0, cursor="hand2", command=copy_code_and_gov)

def place_cg_btn(event=None):
    """Floats the green bar over the left edge of the Farm Code + Governate boxes (takes no layout space)."""
    try:
        a, b = entries["farm_code"], entries["governate"]
        y = a.winfo_y()
        h = b.winfo_y() + b.winfo_height() - y
        if h > 0:
            cg_btn.place(x=a.winfo_x(), y=y, width=26, height=h)
            cg_btn.lift()
    except tk.TclError:
        pass

fields_frame.bind("<Configure>", place_cg_btn)
root.after(300, place_cg_btn)

# ---------- Rule status ----------
rule_header = tk.Frame(root, bg=BG)
rule_header.grid(row=1, column=0, sticky="ew", padx=8, pady=(6, 2))
rule_header.columnconfigure(0, weight=1)

tk.Label(rule_header, text="Rule Status", font=FONT, bg=BG, fg=MUTED, anchor="w").grid(row=0, column=0, sticky="w")

def copy_rule_status():
    content = rule_text.get("1.0", tk.END).strip()
    root.clipboard_clear()
    root.clipboard_append(content)
    copy_btn.config(text="✓")
    root.after(1000, lambda: copy_btn.config(text="📋"))

doc_btn = tk.Button(rule_header, text="📂 Docs", font=("Segoe UI", 8, "bold"), bg="#2980b9", fg="white",
                    activebackground="#3498db", activeforeground="white", relief="flat", bd=0,
                    padx=8, cursor="hand2", command=send_docs_to_extension)
doc_btn.grid(row=0, column=1, sticky="e", padx=(0, 4))

copy_btn = tk.Button(rule_header, text="📋", font=("Segoe UI", 8, "bold"), bg="#e67e22", fg="white",
                     activebackground="#d35400", activeforeground="white", relief="flat", bd=0,
                     width=3, cursor="hand2", command=copy_rule_status)
copy_btn.grid(row=0, column=2, sticky="e")

rule_text = tk.Text(root, font=("Segoe UI", 10, "bold"), height=2, bg=PANEL, fg=GREEN,
                    relief="flat", bd=0, wrap="word", padx=8, pady=6, cursor="arrow",
                    highlightthickness=1, highlightbackground=BORDER)
rule_text.tag_configure("rtl", justify="right")
rule_text.insert("1.0", "No rules broken.", "rtl")
rule_text.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 4))

def fit_rule_box(event=None):
    """Makes the problem box exactly as tall as its text (min 2 lines, max 12)."""
    try:
        rule_text.update_idletasks()
        n = int(rule_text.tk.call(rule_text._w, "count", "-displaylines", "1.0", "end-1c"))
    except Exception:
        n = 2
    n = max(2, min(n, 12))
    if int(rule_text.cget("height")) != n:
        rule_text.config(height=n)
        root.after_idle(resize_window)

rule_text.bind("<Configure>", fit_rule_box)

def set_rule_text(text: str, has_problems: bool) -> None:
    rule_text.config(bg=RED_BG if has_problems else PANEL, fg="#ffffff" if has_problems else GREEN)
    rule_text.delete("1.0", tk.END)
    rule_text.insert("1.0", text, "rtl")
    fit_rule_box()
    resize_window()

# ---------- History window (last 15 farms) ----------
history_win = None
history_tree = None
history_msg = None

def copy_to_clipboard(text: str) -> None:
    root.clipboard_clear()
    root.clipboard_append(text)
    root.update()

def refresh_history_list() -> None:
    if history_tree is None or history_win is None:
        return
    try:
        if not history_win.winfo_exists():
            return
        history_tree.delete(*history_tree.get_children())
        for i, h in enumerate(farm_history):
            history_tree.insert("", "end", iid=str(i), values=(h["code"], h.get("gov", "")))
    except tk.TclError:
        pass

def on_history_click(event):
    row = history_tree.identify_row(event.y)
    if not row:
        return
    history_tree.selection_set(row)
    h = farm_history[int(row)]
    copy_to_clipboard(f"{h['code']}\t{h.get('gov', '')}")
    history_msg.config(text=f"✓ Copied  {h['code']}")

def copy_all_history() -> None:
    if not farm_history:
        return
    copy_to_clipboard("\n".join(f"{h['code']}\t{h.get('gov', '')}" for h in farm_history))
    history_msg.config(text=f"✓ Copied {len(farm_history)} farms")

_hist_geo_job = None

def load_history_geometry() -> str:
    default = f"300x420+{root.winfo_x() + 30}+{root.winfo_y() + 30}"   # first time: on top of the main window
    g = pgs_store.get("history_geometry", "")
    return g if isinstance(g, str) and re.match(r"^\d+x\d+\+-?\d+\+-?\d+$", g) else default

def save_history_geometry() -> None:
    try:
        if history_win is not None and history_win.winfo_exists():
            pgs_store.set("history_geometry", history_win.geometry())
    except Exception:
        pass

def schedule_save_history_geometry() -> None:
    global _hist_geo_job
    if _hist_geo_job:
        root.after_cancel(_hist_geo_job)
    _hist_geo_job = root.after(500, save_history_geometry)

def close_history() -> None:
    save_history_geometry()
    history_win.destroy()

def show_history() -> None:
    global history_win, history_tree, history_msg
    if history_win is not None:
        try:
            if history_win.winfo_exists():
                history_win.deiconify()
                history_win.lift()
                refresh_history_list()
                return
        except tk.TclError:
            pass

    history_win = tk.Toplevel(root)
    history_win.title("History - last 5 farms")
    history_win.configure(bg=BG)
    history_win.attributes("-topmost", True)
    history_win.geometry(load_history_geometry())
    history_win.protocol("WM_DELETE_WINDOW", close_history)
    history_win.bind("<Configure>", lambda e: schedule_save_history_geometry() if e.widget is history_win else None)

    style = ttk.Style(history_win)
    style.theme_use("clam")
    style.configure("Hist.Treeview", background=PANEL, foreground=GREEN, fieldbackground=PANEL,
                    rowheight=26, borderwidth=0, font=("Segoe UI", 9, "bold"))
    style.configure("Hist.Treeview.Heading", background="#3a3a3a", foreground="white",
                    font=("Segoe UI", 9, "bold"), borderwidth=0)
    style.map("Hist.Treeview", background=[("selected", "#2980b9")], foreground=[("selected", "white")])

    tk.Label(history_win, text="Click a farm to copy it (Code + Governate)", font=("Segoe UI", 8),
             bg=BG, fg=MUTED).pack(anchor="w", padx=8, pady=(8, 4))

    frame = tk.Frame(history_win, bg=BG)
    frame.pack(fill="both", expand=True, padx=8)

    history_tree = ttk.Treeview(frame, columns=("code", "gov"), show="headings",
                                selectmode="browse", style="Hist.Treeview")
    history_tree.heading("code", text="Farm Code")
    history_tree.heading("gov", text="Governate")
    history_tree.column("code", width=110, anchor="w")
    history_tree.column("gov", width=140, anchor="e")
    history_tree.pack(side="left", fill="both", expand=True)

    sb = tk.Scrollbar(frame, command=history_tree.yview)
    sb.pack(side="right", fill="y")
    history_tree.config(yscrollcommand=sb.set)
    history_tree.bind("<ButtonRelease-1>", on_history_click)

    bottom = tk.Frame(history_win, bg=BG)
    bottom.pack(fill="x", padx=8, pady=8)
    history_msg = tk.Label(bottom, text="", font=("Segoe UI", 8, "bold"), bg=BG, fg=GREEN, anchor="w")
    history_msg.pack(side="left")
    tk.Button(bottom, text="Copy all", font=("Segoe UI", 8, "bold"), bg="#27ae60", fg="white",
              activebackground="#2ecc71", activeforeground="white", relief="flat", bd=0, padx=10,
              cursor="hand2", command=copy_all_history).pack(side="right")

    refresh_history_list()

btn_row = tk.Frame(root, bg=BG)
btn_row.grid(row=3, column=0, sticky="ew", padx=8, pady=(0, 6))
btn_row.columnconfigure(0, weight=1)

hist_btn = tk.Button(btn_row, text="🕘  History (last 5 farms)", font=FONT, bg="#34495e", fg="white",
                     activebackground="#3d566e", activeforeground="white", relief="flat", bd=0,
                     pady=4, cursor="hand2", command=show_history)
hist_btn.grid(row=0, column=0, sticky="ew")

divide_btn = tk.Button(btn_row, text="⚡ Divide Work", font=FONT, bg="#2e7d32", fg="white",
                       activebackground="#388e3c", activeforeground="white", relief="flat", bd=0,
                       padx=10, pady=4, cursor="hand2", command=lambda: start_divide_work())
divide_btn.grid(row=0, column=1, sticky="e", padx=(6, 0))

# ---------- Console ----------
console_frame = tk.Frame(root, bg=BG)
console_frame.grid(row=4, column=0, sticky="nsew", padx=8, pady=(0, 8))
console_frame.rowconfigure(0, weight=1)
console_frame.columnconfigure(0, weight=1)

console_scrollbar = tk.Scrollbar(console_frame)
console_scrollbar.grid(row=0, column=1, sticky="ns")

console_text = tk.Text(console_frame, height=10, font=("Consolas", 8), bg="#111111", fg=GREEN, insertbackground="white",
                       wrap="word", relief="flat", bd=0, yscrollcommand=console_scrollbar.set)
console_text.grid(row=0, column=0, sticky="nsew")
console_scrollbar.config(command=console_text.yview)

def log_to_console(msg: str) -> None:
    try:
        print(msg)
    except Exception:
        pass
    def append():
        try:
            if console_text.winfo_exists():
                console_text.insert(tk.END, msg + "\n")
                if int(console_text.index("end-1c").split(".")[0]) > 500:
                    console_text.delete("1.0", "100.0")
                console_text.see(tk.END)
        except Exception:
            pass
    root.after(0, append)

# ---------- Show / hide console + window that fits its content ----------
console_visible = False
console_frame.grid_remove()          # console is hidden when the app starts

def resize_window() -> None:
    """Makes the window exactly as tall as its content (width stays as you set it)."""
    try:
        root.update_idletasks()
        root.geometry(f"{max(root.winfo_width(), 300)}x{root.winfo_reqheight()}")
    except tk.TclError:
        pass

def toggle_console() -> None:
    global console_visible
    console_visible = not console_visible
    if console_visible:
        console_frame.grid()
        console_btn.config(text="🖥 Hide console")
    else:
        console_frame.grid_remove()
        console_btn.config(text="🖥 Console")
    resize_window()

def copy_current_table() -> None:
    """Copies the latest submission table to the clipboard and prints every row in the console."""
    rows = _last_table_rows
    if not rows:
        log_to_console("⚠️ No submission table yet - open a KoBo submission first.")
        return
    lines = ["#\tLabel\tValue\tLink"] + [
        f"{r.get('index', '')}\t{r.get('label', '')}\t{r.get('value', '')}\t{r.get('link', '')}" for r in rows
    ]
    root.clipboard_clear()
    root.clipboard_append("\n".join(lines))
    root.update()
    log_to_console(f"📋 Table copied: {len(rows)} rows for {_last_farm_code or 'current farm'}")
    for r in rows[:300]:
        value = str(r.get("value", "")).replace("\n", " ")
        log_to_console(f"{r.get('index', ''):>3} | {r.get('label', '')} | {value}")

console_btn = tk.Button(rule_header, text="🖥 Console", font=("Segoe UI", 8, "bold"), bg="#34495e", fg="white",
                        activebackground="#3d566e", activeforeground="white", relief="flat", bd=0,
                        padx=8, cursor="hand2", command=toggle_console)
console_btn.grid(row=0, column=3, sticky="e", padx=(4, 0))

table_btn = tk.Button(rule_header, text="📋 Table", font=("Segoe UI", 8, "bold"), bg="#7c3aed", fg="white",
activebackground="#8b5cf6", activeforeground="white", relief="flat", bd=0,
padx=8, cursor="hand2", command=copy_current_table)
table_btn.grid(row=0, column=4, sticky="e", padx=(4, 0))

def open_tree_results_folder() -> None:
    """Open the local folder containing satellite and DeepForest diagnostics."""
    folder = os.path.join(SCRIPT_DIR, "farm_satellite_images")
    os.makedirs(folder, exist_ok=True)
    try:
        if hasattr(os, "startfile"):
            os.startfile(folder)
        else:
            import subprocess
            subprocess.Popen(["explorer", folder])
    except Exception as exc:
        messagebox.showerror("Tree results folder", f"Could not open the folder:\n{folder}\n\n{exc}")


def trigger_tree_count_now() -> None:
    if not console_visible:
        toggle_console()
    if not _last_farm_polygon or len(_last_farm_polygon) < 3:
        log_to_console("⚠️ No farm loaded yet — open a submission in KoBo first.")
        return
    log_to_console(f"🔄 Manual run started for {_last_farm_code or 'current farm'}...")
    threading.Thread(
        target=save_farm_satellite_image,
        args=(_last_farm_code, _last_farm_polygon),
        daemon=True
    ).start()

# -------------------- Google Satellite + DeepForest Tree Counter -------------------- #
SAT_ZOOM = 20
SAT_BUFFER_METERS = 5.0
SAT_PADDING_RATIO = 0.08
SAT_MAX_FARMS = 20
SAT_DIR = os.path.join(SCRIPT_DIR, "farm_satellite_images")

# DeepForest's pretrained crown detector is run on overlapping windows.
# 800 px is a practical starting point for this high-resolution satellite mosaic.
DEEPFOREST_PATCH_SIZE = 800
DEEPFOREST_PATCH_OVERLAP = 0.25
DEEPFOREST_IOU_THRESHOLD = 0.15

_last_sat_signature = None
_last_farm_code = ""
_last_farm_polygon = []
_DEEPFOREST_MODEL = None
_DEEPFOREST_MODEL_LOCK = threading.Lock()
_DEEPFOREST_INFERENCE_LOCK = threading.Lock()


def polygon_from_table(table: list) -> list[tuple[float, float]]:
    """Fallback parser if farm_polygon wasn't sent in extracted_info."""
    pts = []
    for row in table:
        lbl = L(row)
        if norm("ارسم") in lbl or norm("حدود") in lbl:
            val = str(row.get("value") or "")
            for m in re.finditer(r"(-?\d+\.\d{4,})\s+(-?\d+\.\d{4,})", val):
                lat, lng = float(m.group(1)), float(m.group(2))
                if 20 <= lat <= 33 and 24 <= lng <= 38:
                    pts.append((lat, lng))
    if len(pts) > 3 and pts[0] == pts[-1]:
        pts.pop()
    return pts


def expand_polygon_meters(poly: list[tuple[float, float]], offset_m: float = 5.0) -> list[tuple[float, float]]:
    """Expand a (latitude, longitude) polygon outwards by offset_m metres."""
    pts = [(float(p[0]), float(p[1])) for p in poly if p and len(p) >= 2]
    if len(pts) > 3 and pts[0] == pts[-1]:
        pts.pop()
    n = len(pts)
    if n < 3:
        return pts

    avg_lat = sum(p[0] for p in pts) / n
    avg_lng = sum(p[1] for p in pts) / n
    m_lat = 111132.92
    m_lng = 111412.84 * math.cos(math.radians(avg_lat))
    xy = [((lng - avg_lng) * m_lng, (lat - avg_lat) * m_lat) for lat, lng in pts]
    area2 = sum(
        xy[i][0] * xy[(i + 1) % n][1] - xy[(i + 1) % n][0] * xy[i][1]
        for i in range(n)
    )
    sign = 1.0 if area2 >= 0 else -1.0

    offset_edges = []
    for i in range(n):
        x1, y1 = xy[i]
        x2, y2 = xy[(i + 1) % n]
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy)
        if length < 1e-9:
            continue
        nx, ny = sign * (dy / length), sign * (-dx / length)
        offset_edges.append(((x1 + nx * offset_m, y1 + ny * offset_m), (dx, dy)))

    if len(offset_edges) < 3:
        return pts

    expanded_xy = []
    for i in range(len(offset_edges)):
        p1, d1 = offset_edges[(i - 1) % len(offset_edges)]
        p2, d2 = offset_edges[i]
        cross = d1[0] * d2[1] - d1[1] * d2[0]
        if abs(cross) < 1e-6:
            ix, iy = p2
        else:
            t = ((p2[0] - p1[0]) * d2[1] - (p2[1] - p1[1]) * d2[0]) / cross
            ix, iy = p1[0] + t * d1[0], p1[1] + t * d1[1]
            orig_x, orig_y = xy[i % n]
            dist = math.hypot(ix - orig_x, iy - orig_y)
            if dist > offset_m * 3.0:
                scale = (offset_m * 3.0) / dist
                ix = orig_x + (ix - orig_x) * scale
                iy = orig_y + (iy - orig_y) * scale
        expanded_xy.append((ix, iy))

    return [(avg_lat + y / m_lat, avg_lng + x / m_lng) for x, y in expanded_xy]


def _lat_lng_to_pixel(lat: float, lng: float, zoom: int) -> tuple[float, float]:
    n = 2 ** zoom
    x_tile = (lng + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    y_tile = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
    return x_tile * 256.0, y_tile * 256.0


def _fetch_google_sat_tile(args):
    tx, ty, zoom = args
    url = f"https://mt1.google.com/vt/lyrs=s&x={tx}&y={ty}&z={zoom}"
    try:
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        if response.status_code == 200 and response.content:
            return tx, ty, Image.open(io.BytesIO(response.content)).convert("RGBA")
    except Exception:
        pass
    return tx, ty, None


def _point_in_poly_xy(x: float, y: float, poly_xy: list[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon test in image pixel coordinates."""
    inside = False
    j = len(poly_xy) - 1
    for i in range(len(poly_xy)):
        xi, yi = poly_xy[i]
        xj, yj = poly_xy[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _tree_log(log_path: str, message: str) -> None:
    """Write every tree-counter stage to both the Tk console and a farm log file."""
    log_to_console(message)
    try:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except OSError:
        pass


def _get_deepforest_model():
    """Load the official pretrained DeepForest tree model once per application run."""
    global _DEEPFOREST_MODEL
    if _DEEPFOREST_MODEL is not None:
        return _DEEPFOREST_MODEL

    with _DEEPFOREST_MODEL_LOCK:
        if _DEEPFOREST_MODEL is not None:
            return _DEEPFOREST_MODEL

        try:
            from deepforest import main as deepforest_main
        except ImportError as exc:
            raise RuntimeError(
                "DeepForest is not installed. Open Command Prompt and run: pip install deepforest"
            ) from exc

        model = deepforest_main.deepforest()
        if hasattr(model, "load_model"):
            # DeepForest 2.x: load the pretrained tree-crown model from Hugging Face.
            model.load_model(model_name="weecology/deepforest-tree", revision="main")
        elif hasattr(model, "use_release"):
            # Compatibility with older DeepForest releases.
            model.use_release()
        else:
            raise RuntimeError("This DeepForest installation has no supported pretrained-model loader.")

        _DEEPFOREST_MODEL = model
        return _DEEPFOREST_MODEL


def _predict_with_deepforest(model, image_path: str):
    """Run overlapping-window inference, adapting to DeepForest 1.x/2.x signatures."""
    import inspect

    predict_fn = model.predict_tile
    try:
        parameters = inspect.signature(predict_fn).parameters
    except (TypeError, ValueError):
        parameters = {}

    kwargs = {
        "patch_size": DEEPFOREST_PATCH_SIZE,
        "patch_overlap": DEEPFOREST_PATCH_OVERLAP,
        "iou_threshold": DEEPFOREST_IOU_THRESHOLD,
    }
    if "path" in parameters or not parameters:
        kwargs["path"] = image_path
    elif "raster_path" in parameters:
        kwargs["raster_path"] = image_path
    else:
        kwargs["path"] = image_path

    if "return_plot" in parameters:
        kwargs["return_plot"] = False
    if "mosaic" in parameters:
        kwargs["mosaic"] = True
    if "dataloader_strategy" in parameters:
        kwargs["dataloader_strategy"] = "single"

    with _DEEPFOREST_INFERENCE_LOCK:
        return predict_fn(**kwargs)


def _normalise_deepforest_predictions(predictions, image_width: int, image_height: int) -> list[dict]:
    """Convert DeepForest DataFrame rows into consistent pixel-space boxes."""
    if predictions is None:
        return []
    if not hasattr(predictions, "iterrows"):
        raise RuntimeError(f"Unexpected DeepForest result type: {type(predictions).__name__}")

    if predictions.empty:
        return []

    column_map = {str(column).lower(): column for column in predictions.columns}
    required = ("xmin", "ymin", "xmax", "ymax")
    if not all(name in column_map for name in required):
        raise RuntimeError(
            "DeepForest returned predictions without the expected xmin/ymin/xmax/ymax columns. "
            f"Returned columns: {list(predictions.columns)}"
        )

    detections = []
    for _, row in predictions.iterrows():
        try:
            x1 = float(row[column_map["xmin"]])
            y1 = float(row[column_map["ymin"]])
            x2 = float(row[column_map["xmax"]])
            y2 = float(row[column_map["ymax"]])
            if not all(math.isfinite(v) for v in (x1, y1, x2, y2)):
                continue
            x1, x2 = sorted((max(0.0, min(float(image_width), x1)), max(0.0, min(float(image_width), x2))))
            y1, y2 = sorted((max(0.0, min(float(image_height), y1)), max(0.0, min(float(image_height), y2))))
            if x2 <= x1 or y2 <= y1:
                continue
            score = None
            if "score" in column_map:
                try:
                    score = float(row[column_map["score"]])
                except (TypeError, ValueError):
                    pass
            label = str(row[column_map["label"]]) if "label" in column_map else "Tree"
            detections.append({
                "xmin": x1, "ymin": y1, "xmax": x2, "ymax": y2,
                "center_x": (x1 + x2) / 2.0,
                "center_y": (y1 + y2) / 2.0,
                "score": score,
                "label": label,
            })
        except (TypeError, ValueError, KeyError):
            continue
    return detections


def _save_detections_csv(path: str, detections: list[dict]) -> None:
    import csv
    fields = ["xmin", "ymin", "xmax", "ymax", "center_x", "center_y", "score", "label"]
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in detections:
            writer.writerow({key: item.get(key, "") for key in fields})


def _draw_detection_boxes(
    base_img: Image.Image,
    detections: list[dict],
    farm_poly: list[tuple[float, float]],
    banner_text: str,
    box_color: tuple[int, int, int],
) -> Image.Image:
    """Draw every predicted crown box and the original farm boundary."""
    out = base_img.copy().convert("RGB")
    draw = ImageDraw.Draw(out)
    for item in detections:
        x1, y1 = item["xmin"], item["ymin"]
        x2, y2 = item["xmax"], item["ymax"]
        draw.rectangle((x1, y1, x2, y2), outline=box_color, width=2)
        cx, cy = item["center_x"], item["center_y"]
        draw.ellipse((cx - 2, cy - 2, cx + 2, cy + 2), fill=box_color)

    if len(farm_poly) >= 3:
        draw.line(farm_poly + [farm_poly[0]], fill=(0, 220, 255), width=3)

    bar_w = min(out.width - 8, max(260, len(banner_text) * 8 + 24))
    draw.rectangle((8, 8, 8 + bar_w, 36), fill=(24, 24, 27), outline=box_color, width=2)
    draw.text((16, 16), banner_text, fill=(255, 255, 255))
    return out


def _cleanup_old_farm_sets(folder: str, max_farms: int = 20) -> int:
    """Keep the newest farm diagnostic folders; do not delete files outside this folder."""
    try:
        folders = [
            os.path.join(folder, name) for name in os.listdir(folder)
            if os.path.isdir(os.path.join(folder, name))
        ]
    except OSError:
        return 0
    folders.sort(key=lambda path: os.path.getmtime(path), reverse=True)
    for old_folder in folders[max_farms:]:
        try:
            import shutil
            shutil.rmtree(old_folder)
        except OSError:
            pass
    return min(len(folders), max_farms)


def _save_farm_satellite_image_impl(farm_code: str, raw_polygon: list) -> None:
    if not PIL_OK:
        log_to_console("⚠️ Pillow is not installed — run: pip install pillow")
        return

    started_at = time.time()
    safe_code = re.sub(r'[\\/:*?"<>|\s]+', "_", (farm_code or "").strip()).strip("_")
    if not safe_code or safe_code == "--":
        safe_code = f"farm_{int(time.time())}"

    expanded_poly = expand_polygon_meters(raw_polygon, SAT_BUFFER_METERS)
    original_poly = [(float(p[0]), float(p[1])) for p in raw_polygon if p and len(p) >= 2]
    if len(original_poly) > 3 and original_poly[0] == original_poly[-1]:
        original_poly.pop()
    if len(expanded_poly) < 3 or len(original_poly) < 3:
        log_to_console(f"⚠️ [{safe_code}] Invalid farm polygon (< 3 points) — skipped.")
        return

    os.makedirs(SAT_DIR, exist_ok=True)
    farm_dir = os.path.join(SAT_DIR, safe_code)
    os.makedirs(farm_dir, exist_ok=True)
    sat_path = os.path.join(farm_dir, "01_satellite_input_clean.png")
    boundary_path = os.path.join(farm_dir, "02_satellite_with_boundaries.png")
    raw_path = os.path.join(farm_dir, "03_deepforest_all_detections.png")
    inside_path = os.path.join(farm_dir, "04_trees_inside_farm.png")
    all_csv_path = os.path.join(farm_dir, "deepforest_all_detections.csv")
    inside_csv_path = os.path.join(farm_dir, "deepforest_inside_farm.csv")
    run_log_path = os.path.join(farm_dir, "run_log.txt")
    with open(run_log_path, "w", encoding="utf-8") as handle:
        handle.write(f"Kobo Bridge DeepForest run: {safe_code}\nStarted: {datetime.now().isoformat(timespec='seconds')}\n\n")

    def say(message: str) -> None:
        _tree_log(run_log_path, message)

    say(f"🚀 [{safe_code}] DeepForest worker started; polygon_points={len(original_poly)}")
    say(f"🗺️ Expanding image download boundary by {SAT_BUFFER_METERS:g} metres; detections will be filtered against the ORIGINAL farm boundary.")

    for zoom in range(SAT_ZOOM, 16, -1):
        expanded_px = [_lat_lng_to_pixel(lat, lng, zoom) for lat, lng in expanded_poly]
        xs, ys = [p[0] for p in expanded_px], [p[1] for p in expanded_px]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        span = max(max_x - min_x, max_y - min_y, 128.0)
        pad = span * SAT_PADDING_RATIO
        crop_x0, crop_y0 = min_x - pad, min_y - pad
        crop_x1, crop_y1 = max_x + pad, max_y + pad
        tx0, tx1 = int(math.floor(crop_x0 / 256.0)), int(math.floor(crop_x1 / 256.0))
        ty0, ty1 = int(math.floor(crop_y0 / 256.0)), int(math.floor(crop_y1 / 256.0))
        tasks = [(tx, ty, zoom) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)]
        if len(tasks) > 144:
            say(f"ℹ️ Zoom {zoom} requires {len(tasks)} tiles; stepping down to zoom {zoom - 1}.")
            continue

        say(f"📡 Downloading {len(tasks)} Google Satellite tiles at Zoom {zoom}...")
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(_fetch_google_sat_tile, tasks))
        failed = sum(1 for _, _, tile in results if tile is None)
        if failed:
            say(f"⚠️ {failed}/{len(tasks)} tiles failed at Zoom {zoom}; trying Zoom {zoom - 1}.")
            continue

        canvas = Image.new("RGBA", ((tx1 - tx0 + 1) * 256, (ty1 - ty0 + 1) * 256))
        for tx, ty, tile_img in results:
            canvas.paste(tile_img, ((tx - tx0) * 256, (ty - ty0) * 256))
        left = int(round(crop_x0 - tx0 * 256))
        top = int(round(crop_y0 - ty0 * 256))
        right = int(round(crop_x1 - tx0 * 256))
        bottom = int(round(crop_y1 - ty0 * 256))
        cropped = canvas.crop((left, top, right, bottom))

        expanded_rel = [(px - crop_x0, py - crop_y0) for px, py in expanded_px]
        original_px = [_lat_lng_to_pixel(lat, lng, zoom) for lat, lng in original_poly]
        original_rel = [(px - crop_x0, py - crop_y0) for px, py in original_px]
        # Keep detector input completely clean: boundary strokes are saved separately
        # so coloured outlines cannot bias DeepForest's tree-crown predictions.
        satellite = cropped.convert("RGB")
        satellite.save(sat_path, "PNG")
        boundary_preview = satellite.copy()
        boundary_draw = ImageDraw.Draw(boundary_preview)
        boundary_draw.line(expanded_rel + [expanded_rel[0]], fill=(250, 204, 21), width=3)
        boundary_draw.line(original_rel + [original_rel[0]], fill=(0, 220, 255), width=3)
        boundary_preview.save(boundary_path, "PNG")
        width, height = satellite.size
        say(f"🛰️ Saved clean satellite input: {width}x{height}px, zoom={zoom}.")
        say(f"🗺️ Saved boundary preview: yellow=+5m download boundary, cyan=actual farm boundary: {boundary_path}")
        say(f"📁 Farm diagnostic folder: {farm_dir}")

        try:
            say("🌲 Loading pretrained DeepForest model (first run may download model weights)...")
            model = _get_deepforest_model()
            say(f"🌲 Running DeepForest: patch_size={DEEPFOREST_PATCH_SIZE}, overlap={DEEPFOREST_PATCH_OVERLAP:.2f}, NMS IoU={DEEPFOREST_IOU_THRESHOLD:.2f}.")
            predictions = _predict_with_deepforest(model, sat_path)
            detections = _normalise_deepforest_predictions(predictions, width, height)
            say(f"🌲 DeepForest returned {len(detections)} raw tree-crown boxes.")

            inside_detections = [
                item for item in detections
                if _point_in_poly_xy(item["center_x"], item["center_y"], original_rel)
            ]
            say(f"📐 Original-boundary filter retained {len(inside_detections)} / {len(detections)} detections.")

            _save_detections_csv(all_csv_path, detections)
            _save_detections_csv(inside_csv_path, inside_detections)

            raw_image = _draw_detection_boxes(
                satellite, detections, original_rel,
                f"{safe_code} | DeepForest raw detections: {len(detections)}",
                (239, 68, 68),
            )
            raw_image.save(raw_path, "PNG")
            inside_image = _draw_detection_boxes(
                satellite, inside_detections, original_rel,
                f"{safe_code} | Inside actual farm: {len(inside_detections)} trees",
                (34, 197, 94),
            )
            inside_image.save(inside_path, "PNG")

            elapsed = time.time() - started_at
            say(f"🖼️ Saved all-detections image: {raw_path}")
            say(f"🖼️ Saved inside-farm image: {inside_path}")
            say(f"📄 Saved detection CSV files: {all_csv_path} and {inside_csv_path}")
            say(f"✅ DONE in {elapsed:.1f}s — DeepForest raw={len(detections)}, inside original farm={len(inside_detections)}.")
            with open(run_log_path, "a", encoding="utf-8") as handle:
                handle.write("\nSUMMARY\n")
                handle.write(f"farm_code={safe_code}\nimage_size={width}x{height}\nzoom={zoom}\n")
                handle.write(f"model=weecology/deepforest-tree\npatch_size={DEEPFOREST_PATCH_SIZE}\n")
                handle.write(f"patch_overlap={DEEPFOREST_PATCH_OVERLAP}\niou_threshold={DEEPFOREST_IOU_THRESHOLD}\n")
                handle.write(f"raw_detections={len(detections)}\ninside_original_farm={len(inside_detections)}\n")
                handle.write(f"elapsed_seconds={elapsed:.1f}\n")
            kept = _cleanup_old_farm_sets(SAT_DIR, SAT_MAX_FARMS)
            say(f"🗂️ Diagnostic retention: {kept}/{SAT_MAX_FARMS} latest farm folders kept.")
        except Exception as exc:
            import traceback
            say(f"❌ [{safe_code}] DEEPFOREST ERROR: {type(exc).__name__}: {exc}")
            say(traceback.format_exc())
            # Keep the satellite image and log even when model loading/inference fails.
        return

    say(f"❌ Could not download a complete satellite mosaic for {safe_code}; no detector was run.")


def save_farm_satellite_image(farm_code: str, raw_polygon: list) -> None:
    """Worker entry point with an outer guard so errors are always visible."""
    try:
        log_to_console(f"🚀 [Tree counter] Worker entered for {farm_code or 'unnamed farm'}; polygon_points={len(raw_polygon or [])}")
        _save_farm_satellite_image_impl(farm_code, raw_polygon)
        log_to_console(f"🏁 [Tree counter] Worker finished for {farm_code or 'unnamed farm'}.")
    except Exception as exc:
        import traceback
        log_to_console(f"❌ [Tree counter] UNHANDLED ERROR for {farm_code or 'unnamed farm'}: {type(exc).__name__}: {exc}")
        log_to_console(traceback.format_exc())


# -------------------- Data Processing -------------------- #
def extract_field_value(table: list, exact_label: str, keyword_fallback: str) -> str:
    for row in table:
        lbl = str(row.get("label", "")).strip()
        if lbl == exact_label:
            return str(row.get("value", "")).strip()

    for row in table:
        lbl = str(row.get("label", "")).strip()
        if keyword_fallback in lbl:
            return str(row.get("value", "")).strip()

    return "--"

def process_incoming_payload(data: dict) -> None:
    global latest_attachments
    table = data.get("table", [])
    extracted = data.get("extracted_info", {})
    if not table:
        return

    latest_attachments = extract_attachments(table)

    extracted_values = {
        key: extract_field_value(table, exact, fallback)
        for _, key, exact, fallback in FIELD_TARGETS
    }

    calc_area = extracted.get("calculated_area_faddan")

    try:
        broken_rules = check_rules(table, calc_area, extracted.get("farm_polygon"), extracted.get("soil_samples"))
    except Exception as ex:
        broken_rules = [f"حصل خطأ في فحص القواعد: {ex}"]

    # Keep the non-tree problems hidden until the reviewer returns the manual tree count.
    global _tree_review_active_code, _tree_review_last_queued_code
    farm_code_for_review = str(extracted_values.get("farm_code", "")).strip()
    tree_rows = _tree_block_from_table(table)
    submitted_counts = _tree_counts_from_rows(tree_rows)
    submitted_tree_total = sum(submitted_counts)
    if farm_code_for_review and farm_code_for_review != "--":
        review_session = {
            "farm_code": farm_code_for_review,
            "tree_rows": tree_rows,
            "submitted_counts": submitted_counts,
            "submitted_total": submitted_tree_total,
            "base_problems": list(broken_rules),
            "tree_checked": False,
            "image_url": f"http://127.0.0.1:5000/tree-counter/image/{urllib.parse.quote(_safe_farm_folder(farm_code_for_review))}"
        }
        with _tree_review_lock:
            _tree_review_active_code = farm_code_for_review
            _tree_review_sessions[farm_code_for_review] = review_session
            if farm_code_for_review != _tree_review_last_queued_code:
                _tree_review_queue.append({"farm_code": farm_code_for_review})
                _tree_review_last_queued_code = farm_code_for_review
    problems_line = format_problems(broken_rules)

    global _last_sat_signature, _last_farm_code, _last_farm_polygon
    poly = extracted.get("farm_polygon") or polygon_from_table(table)
    
    if poly and len(poly) >= 3:
        farm_code_val = extracted_values.get("farm_code", "")
        _last_farm_code = farm_code_val
        _last_farm_polygon = poly
        sig = (farm_code_val, tuple((round(float(p[0]), 6), round(float(p[1]), 6)) for p in poly))
        
        if sig != _last_sat_signature:
            _last_sat_signature = sig
            log_to_console(f"🚀 [{farm_code_val}] Starting satellite + DeepForest tree counter...")
            threading.Thread(
                target=save_farm_satellite_image,
                args=(farm_code_val, poly),
                daemon=True
            ).start()

    def update_gui():
        doc_btn.config(text=f"📂 Docs ({len(latest_attachments)})")

        for key, val in extracted_values.items():
            field_vars[key].set(val)

        add_to_history(extracted_values.get("farm_code", ""), extracted_values.get("governate", ""))
        refresh_history_list()

        if farm_code_for_review and farm_code_for_review != "--":
            set_rule_text("جارٍ تأكيد عدد الأشجار من خلال الإضافة...", False)
        elif problems_line:
            set_rule_text(problems_line, True)
        else:
            set_rule_text("No rules broken.", False)

        log_to_console("==========================================")
        log_to_console(f"📥 RECEIVED DATA ({datetime.now().strftime('%H:%M:%S')}) - Rows: {len(table)}")
        log_to_console("==========================================")
        log_to_console(f"   code={extracted_values.get('farm_code')}  gov={extracted_values.get('governate')}  docs={len(latest_attachments)}")

    root.after(0, update_gui)

# -------------------- Divide Work (Kobo -> Google Sheet) -------------------- #
# Needs: pip install requests gspread google-auth      and credentials.json next to this file.
LOG = log_to_console   # every line the Divide Work code logs goes to this tool's console

try:
    from zoneinfo import ZoneInfo
    CAIRO = ZoneInfo("Africa/Cairo")
except Exception:
    CAIRO = timezone(timedelta(hours=3))

# ------------------------- SETTINGS -------------------------
SERVER_URL = "https://kf.kobotoolbox.org"
FORM_NAME = "PGS - New"
ASSET_UID = ""
FARM_CODE_KEY = "at_house/farm_id"
GOV_KEY = "engineer_data/__002"

# Google Sheet Settings
SPREADSHEET_KEY = "1GYbkWSJao0S5skCEr3Y0BaiHQV2t24-aaCLMCP8UpJs"
SHEET_DIST_TAB = "_sys_dist"
SHEET_AUDIT_LOG_TAB = "_sys_audit_log"

TEAM_MEMBERS = [
    "mansy.ayman@ebda.earth",
    "haidy.abdelnasser@ebda.earth",
    "eslam.ashraf@ebda.earth",
    "roaa.ahmed@ebda.earth"
]

KOBO_TO_EMAIL = {
    "mansy_ayman": "mansy.ayman@ebda.earth",
    "haidyabdelnasser12345": "haidy.abdelnasser@ebda.earth",
    "eslamashraf": "eslam.ashraf@ebda.earth",
    "roaaaboelnaga66_": "roaa.ahmed@ebda.earth",
}

FIELDS = ["_id", "_validation_status", FARM_CODE_KEY, GOV_KEY]
GOVERNORATE_HINTS = ["governorate", "gov", "المحافظة", "المحافظه"]
FARM_CODE_HINTS = ["farm_id", "كود المزرعة", "كود"]
# ------------------------------------------------------------
try:
    from pgs_key import GOOGLE_KEY_JSON
except ImportError:
    GOOGLE_KEY_JSON = ""


def load_credentials_info():
    try:
        return json.loads(GOOGLE_KEY_JSON)
    except Exception:
        return None


def _auth_headers():
    tok = pgs_store.get("kobo_token", "")
    if not tok:
        raise RuntimeError("No Kobo API token saved. Click the Kobo User button and paste your token.")
    return {"Authorization": f"Token {tok}"}


_uid_cache = None
_gc = None
_sess = None

def _session():
    global _sess
    if _sess is None:
        _sess = requests.Session()
    return _sess

def get_all_pages(url, what="items", max_pages=None):
    results, page = [], 1
    while url:
        LOG(f"[GET] page {page} -> {url}")
        t = time.time()
        try:
            r = _session().get(url, headers=_auth_headers(), timeout=(10, 60))
        except requests.exceptions.ConnectTimeout:
            LOG("[FAIL] Could not connect within 10s")
            raise
        except requests.exceptions.ReadTimeout:
            LOG("[FAIL] Connected, but the server did not answer within 60s")
            raise
        except requests.exceptions.ConnectionError as e:
            LOG(f"[FAIL] Connection error: {e}")
            raise

        LOG(f"[HTTP] {r.status_code} {r.reason} in {time.time()-t:.1f}s")
        if not r.ok:
            LOG(f"[BODY] {r.text[:300]}")
        r.raise_for_status()

        data = r.json()
        got = data.get("results", [])
        results.extend(got)
        url = data.get("next")
        page += 1
        if max_pages and page > max_pages:
            break
    return results

def find_asset_uid(server):
    global _uid_cache
    if ASSET_UID:
        return ASSET_UID
    if _uid_cache:
        return _uid_cache
    assets = get_all_pages(f"{server}/api/v2/assets/?format=json&limit=100", "forms")
    wanted = FORM_NAME.strip().lower()
    for a in assets:
        if (a.get("name") or "").strip().lower() == wanted:
            _uid_cache = a["uid"]
            return _uid_cache
    raise RuntimeError(f'Form "{FORM_NAME}" not found.')

def get_submissions(server, light=True):
    uid = find_asset_uid(server)
    if light:
        url = f"{server}/api/v2/assets/{uid}/data/?format=json&limit=3000&fields={quote(json.dumps(FIELDS))}"
        return get_all_pages(url, "submissions")
    url = f"{server}/api/v2/assets/{uid}/data/?format=json&limit=3"
    return get_all_pages(url, "submissions", max_pages=1)


def find_value(sub, hints):
    for key, value in sub.items():
        last = key.split("/")[-1].lower()
        if any(h.lower() in last for h in hints):
            if value not in (None, ""):
                return str(value)
    return ""

def get_gspread_client():
    global _gc
    if _gc is None:
        info = load_credentials_info()
        if info is None:
            raise RuntimeError("The Google key is missing from this build (pgs_key.py).")
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        _gc = gspread.authorize(Credentials.from_service_account_info(info, scopes=scopes))
    return _gc
    
# -------------------- Work Log (every Docs click -> Google Sheet) -------------------- #
SHEET_WORK_LOG_TAB = "_sys_work_log"
WORK_LOG_HEADERS = ["Date", "Time", "Farm Code", "Governorate", "Kobo Username", "Validated", "Validation Status", "Validated By"]
_work_log_lock = threading.Lock()
_work_log_ws = None

_VAL_UID_MAP = {
    "validation_status_approved": "Approved",
    "validation_status_not_approved": "Not Approved",
    "validation_status_on_hold": "On Hold",
}


def _parse_validation_status(vs) -> tuple[str, str]:
    """Extracts (label, by_whom) from Kobo API v2 _validation_status dict."""
    if not isinstance(vs, dict) or not vs:
        return "", ""
    label = str(vs.get("label") or "").strip()
    if not label and vs.get("uid"):
        uid = str(vs.get("uid")).strip()
        label = _VAL_UID_MAP.get(uid, uid)
    by_whom = str(vs.get("by_whom") or "").strip()
    return label, by_whom

def _get_work_log_ws():
    global _work_log_ws
    if _work_log_ws is None:
        sh = get_gspread_client().open_by_key(SPREADSHEET_KEY)
        _work_log_ws = sh.worksheet(SHEET_WORK_LOG_TAB)
        if _work_log_ws.col_count < 8:
            _work_log_ws.add_cols(8 - _work_log_ws.col_count)
    return _work_log_ws

def _lookup_single_farm_validation(code: str) -> tuple[str, str, str]:
    """Checks Kobo API v2 for the LATEST submission of a single farm code when Docs is clicked."""
    try:
        server = SERVER_URL.rstrip("/")
        uid = find_asset_uid(server)
        q = quote(json.dumps({FARM_CODE_KEY: code}))
        f = quote(json.dumps(["_id", "_validation_status", FARM_CODE_KEY]))
        s = quote(json.dumps({"_id": -1}))
        url = f"{server}/api/v2/assets/{uid}/data/?format=json&query={q}&fields={f}&sort={s}&limit=20"
        r = _session().get(url, headers=_auth_headers(), timeout=(5, 15))
        if r.ok:
            results = r.json().get("results", [])
            if results:
                latest_sub = max(results, key=lambda x: x.get("_id") or 0)
                label, by = _parse_validation_status(latest_sub.get("_validation_status"))
                if label:
                    return "Yes", label, by
    except Exception:
        pass
    return "No", "", ""


def _append_log_rows(rows):
    ws = _get_work_log_ws()
    rows_to_add = []
    for r in rows:
        k = _farm_key(r[2])
        if not k:
            continue
        # Only skip if the latest Kobo submission is already validated
        if len(r) > 5 and str(r[5]).strip().lower() == "yes":
            continue
        padded = list(r) + ["No", "", ""]
        rows_to_add.append(padded[:8])

    if not rows_to_add:
        return 0

    first = len(ws.col_values(1)) + 1
    last = first + len(rows_to_add) - 1
    if ws.row_count < last:
        ws.add_rows(last - ws.row_count + 50)
    ws.update(range_name=f"A{first}:H{last}", values=rows_to_add, value_input_option="USER_ENTERED")
    return len(rows_to_add)


def log_docs_click(code, gov, kobo_user):
    global _work_log_ws
    now = datetime.now(CAIRO)
    val_flag, val_status, val_by = _lookup_single_farm_validation(code)
    status_label = "✓ Already validated"

    if val_flag == "Yes":
        LOG(f"[WORK LOG] ℹ️ {code} is already validated on Kobo ({val_status} by {val_by}) - skipped.")
    else:
        new_row = [now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"), code, gov, kobo_user, "No", "", ""]
        with _work_log_lock:
            rows = list(pgs_store.get("work_log_pending", []) or [])
            rows.append(new_row)
            try:
                _append_log_rows(rows)
                pgs_store.delete("work_log_pending")
                LOG(f"[WORK LOG] ✓ {code} / {kobo_user} saved at {new_row[1]} (Validated=No)")
                status_label = "✓ Logged"
            except Exception as e:
                _work_log_ws = None
                pgs_store.set("work_log_pending", rows)
                LOG(f"[WORK LOG] ✗ Could not send ({type(e).__name__}: {e!r}). Saved (encrypted), will retry on the next Docs click.")
                status_label = "⚠ Not sent"

    def flash():
        doc_btn.config(text=status_label)
        root.after(1500, lambda: doc_btn.config(text=f"📂 Docs ({len(latest_attachments)})"))

    root.after(0, flash)

# -------------------- Work Log check (runs on Divide Work) -------------------- #
WORK_LOG_MONTHS_TO_KEEP = 4

def _farm_key(code):
    """Makes farm codes comparable: no spaces, Arabic digits -> normal digits, upper case."""
    return re.sub(r"\s+", "", str(code or "")).translate(_AR_DIGITS).upper()


def _sheet_date(v):
    """Reads a date from the sheet (real date serial number OR text). Returns None if it can't."""
    if isinstance(v, (int, float)):
        return (datetime(1899, 12, 30) + timedelta(days=float(v))).date()
    s = str(v or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def sync_work_log(sh, subs):
    """1) Checks ONLY the latest Kobo submission per Farm Code (ignores older duplicate uploads).
       2) Never touches rows that already have 'Validated By' filled in Column H.
       3) Once the latest submission is validated, credits ONLY the first pending row and deletes newer duplicates.
       4) Deletes rows older than the 4-month window."""
    LOG("[WORK LOG] Checking logged farms against Kobo validations...")
    try:
        ws = sh.worksheet(SHEET_WORK_LOG_TAB)
    except gspread.WorksheetNotFound:
        LOG("[WORK LOG] _sys_work_log tab does not exist yet (no Docs clicks logged). Skipping.")
        return

    # Kobo side: check ONLY the latest submission (_id descending) for each farm code
    kobo_latest_valid, kobo_all = {}, set()
    seen_codes = set()
    for s in sorted(subs, key=lambda x: x.get("_id") or 0, reverse=True):
        code = _farm_key(s.get(FARM_CODE_KEY) or find_value(s, FARM_CODE_HINTS))
        if not code:
            continue
        kobo_all.add(code)
        if code in seen_codes:
            continue
        seen_codes.add(code)
        label, by = _parse_validation_status(s.get("_validation_status"))
        if label:
            kobo_latest_valid[code] = (label, by)

    values = ws.get_all_values(value_render_option="UNFORMATTED_VALUE")
    if len(values) < 2:
        LOG("[WORK LOG] Log is empty. Nothing to check.")
        return

    # First day of the oldest month we keep (current month + 3 previous months)
    t = datetime.now(CAIRO)
    y, m = t.year, t.month - (WORK_LOG_MONTHS_TO_KEEP - 1)
    while m < 1:
        m, y = m + 12, y - 1
    cutoff = datetime(y, m, 1).date()

    cell_updates = []
    rows_to_delete, not_in_kobo = [], []
    newly_credited_codes = set()
    n_valid = n_new = n_dups = 0

    for i, row in enumerate(values[1:], start=2):          # i = real row number in the sheet
        row = list(row) + [""] * (8 - len(row))
        d = _sheet_date(row[0])
        if d and d < cutoff:
            rows_to_delete.append(i)
            continue

        # Once Column H (Validated By) is filled, DO NOT touch this row at all
        if str(row[7]).strip():
            n_valid += 1
            continue

        code = _farm_key(row[2])
        if code and code in kobo_latest_valid:
            # Latest Kobo submission is validated: credit ONLY the first pending row, delete newer duplicates
            if code in newly_credited_codes:
                rows_to_delete.append(i)
                n_dups += 1
                continue

            newly_credited_codes.add(code)
            label, by = kobo_latest_valid[code]
            cell_updates.append({"range": f"F{i}:H{i}", "values": [["Yes", label, by]]})
            n_valid += 1
            n_new += 1
        else:
            # Not validated yet: keep all pending rows; only write "No" if F isn't already "No"
            if str(row[5]).strip().lower() != "no":
                cell_updates.append({"range": f"F{i}:H{i}", "values": [["No", "", ""]]})
            if code and code not in kobo_all:
                not_in_kobo.append(str(row[2]))

    if cell_updates:
        if ws.col_count < 8:
            ws.add_cols(8 - ws.col_count)
        ws.batch_update(cell_updates, value_input_option="USER_ENTERED")

    if rows_to_delete:                                       # Delete expired/duplicate rows, bottom block first
        rows_to_delete = sorted(set(rows_to_delete))
        blocks, start, prev = [], rows_to_delete[0], rows_to_delete[0]
        for r in rows_to_delete[1:]:
            if r == prev + 1:
                prev = r
            else:
                blocks.append((start, prev))
                start = prev = r
        blocks.append((start, prev))
        for a, b in reversed(blocks):
            ws.delete_rows(a, b)

    total = len(values) - 1 - len(rows_to_delete)
    LOG(f"[WORK LOG] {total} row(s) kept: {n_valid} validated ({n_new} newly confirmed), "
        f"{total - n_valid} still 'No', {n_dups} validated duplicate(s) removed.")
    if not_in_kobo:
        LOG(f"[WORK LOG] Not found in Kobo at all (check the farm code): {', '.join(sorted(set(not_in_kobo))[:10])}")

def read_team_modes(sh):
    """Reads or initializes team member modes from _sys_dist."""
    try:
        ws = sh.worksheet(SHEET_DIST_TAB)
        data = ws.get_all_values()
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_DIST_TAB, rows=100, cols=10)
        data = []

    headers = ["Member Email", "Today Mode", "Assigned Governorates", "Total Assigned Farms", "Kobo Validations Today", "Status Updates Today", "Total Effort Score"]

    modes = {}
    if len(data) < 2:
        ws.clear()
        initial_rows = [headers]
        for email in TEAM_MEMBERS:
            initial_rows.append([email, "Kobo", "", 0, 0, 0, 0.0])
            modes[email] = "Kobo"
        ws.update("A1", initial_rows)
    else:
        for row in data[1:5]:
            if row:
                email = row[0].strip()
                mode = row[1].strip() if len(row) > 1 and row[1].strip() else "Kobo"
                modes[email] = mode

    for email in TEAM_MEMBERS:
        if email not in modes:
            modes[email] = "Kobo"

    return ws, modes


def divide_work_fairly(unvalidated_farms, active_workers, max_gov_ratio=1.4):
    """Fairly divides unvalidated governorates among active Kobo workers."""
    if not active_workers:
        return {}, {}

    gov_groups = {}
    for code, gov in unvalidated_farms:
        gov_groups.setdefault(gov, []).append(code)

    total_farms = len(unvalidated_farms)
    num_workers = len(active_workers)
    target_share = total_farms / num_workers if num_workers > 0 else 0

    worker_farms = {w: [] for w in active_workers}
    worker_govs = {w: [] for w in active_workers}
    worker_counts = {w: 0 for w in active_workers}

    assignment_blocks = []
    for gov, codes in gov_groups.items():
        if len(codes) > (target_share * max_gov_ratio) and num_workers > 1:
            chunk_size = max(1, int(target_share))
            for i in range(0, len(codes), chunk_size):
                chunk_codes = codes[i:i + chunk_size]
                assignment_blocks.append((f"{gov} (P{i//chunk_size + 1})", gov, chunk_codes))
        else:
            assignment_blocks.append((gov, gov, codes))

    assignment_blocks.sort(key=lambda x: (-len(x[2]), x[0]))

    for block_label, real_gov, codes in assignment_blocks:
        target_worker = min(active_workers, key=lambda w: (worker_counts[w], w))
        for code in codes:
            worker_farms[target_worker].append((code, real_gov))
        if real_gov not in worker_govs[target_worker]:
            worker_govs[target_worker].append(real_gov)
        worker_counts[target_worker] += len(codes)

    return worker_farms, worker_govs


def run_divide_work_pipeline():
    """Main unified pipeline triggered by 'Divide Work' button."""
    server = SERVER_URL.rstrip("/")
    LOG("[STEP] Connecting to Google Sheets...")
    gc = get_gspread_client()
    sh = gc.open_by_key(SPREADSHEET_KEY)

    # 1. Fetch Submissions
    LOG("[STEP] Fetching submissions from KoboToolbox...")
    subs = get_submissions(server, light=True)

    # 3. Read Team Modes from _sys_dist
    LOG("[STEP 2/4] Reading team modes from _sys_dist...")
    dist_ws, modes = read_team_modes(sh)

    # 4. Extract Unvalidated Farms & Divide Work
    LOG("[STEP 3/4] Extracting unvalidated farms...")
    unvalidated = []
    for s in subs:
        if not (s.get("_validation_status") or {}).get("label"):
            code = s.get(FARM_CODE_KEY) or find_value(s, FARM_CODE_HINTS) or "?"
            gov = s.get(GOV_KEY) or find_value(s, GOVERNORATE_HINTS) or "?"
            unvalidated.append((code, gov))

    LOG(f"[INFO] Found {len(unvalidated)} unvalidated farms.")

    gov_counts = Counter(gov for _, gov in unvalidated)
    LOG("=" * 50)
    LOG("[GOV BREAKDOWN] Unvalidated farms per governorate:")
    for gov, count in sorted(gov_counts.items(), key=lambda x: x[1], reverse=True):
        LOG(f"   • {gov}: {count} farm(s)")
    LOG("=" * 50)

    active_kobo_workers = [email for email in TEAM_MEMBERS if modes.get(email) == "Kobo"]
    LOG(f"[INFO] Active Kobo workers today: {active_kobo_workers}")

    worker_farms, worker_govs = divide_work_fairly(unvalidated, active_kobo_workers)

    LOG("=" * 50)
    LOG("[ASSIGNMENT BREAKDOWN] Distribution details per worker:")
    for w in active_kobo_workers:
        farms = worker_farms.get(w, [])
        w_gov_counts = Counter(gov for _, gov in farms)
        details = ", ".join([f"{gov}: {cnt}" for gov, cnt in w_gov_counts.items()])
        LOG(f"   ➜ {w.split('@')[0]} (Total: {len(farms)}) -> [{details or 'No farms'}]")
    LOG("=" * 50)

    # 5. Update _sys_dist Summary (Columns A:D ONLY - Preserves Formulas in E, F, G)
    LOG("[STEP 4/4] Updating _sys_dist assigned farms (A1:D5 ONLY)...")
    headers = ["Member Email", "Today Mode", "Assigned Governorates", "Total Assigned Farms"]
    summary_rows = [headers]

    for email in TEAM_MEMBERS:
        mode = modes.get(email, "Kobo")
        govs_str = ", ".join(worker_govs.get(email, [])) if mode == "Kobo" else ""
        total_assigned = len(worker_farms.get(email, [])) if mode == "Kobo" else 0
        summary_rows.append([email, mode, govs_str, total_assigned])

    # Explicitly write A1:D5 so Columns E, F, G formulas remain untouched
    dist_ws.update("A1:D5", summary_rows)
    LOG("[SUCCESS] Divide Work pipeline completed!")

    # Sync _sys_work_log (check validations, delete validated duplicates, clean >4 months)
    sync_work_log(sh, subs)


def _divide_work_thread():
    try:
        LOG("=" * 50)
        LOG(f"[START] Divide Work Pipeline - {time.strftime('%H:%M:%S')}")
        run_divide_work_pipeline()
    except Exception as e:
        LOG(f"[ERROR] {type(e).__name__}: {e}")
    finally:
        root.after(0, lambda: divide_btn.config(state="normal", text="⚡ Divide Work"))

def start_divide_work() -> None:
    if not DW_LIBS_OK:
        messagebox.showerror(
            "Divide Work",
            "Missing library: " + DW_LIBS_ERROR + "\n\nInstall once, then restart this tool:\n"
            "pip install requests gspread google-auth"
        )
        return
    if load_credentials_info() is None:
        messagebox.showerror("Divide Work", "The Google key is missing from this build (pgs_key.py).")
        return
    if not pgs_store.get("kobo_token", ""):
        messagebox.showerror("Divide Work", "Click the Kobo User button and paste your API token first.")
        return
    divide_btn.config(state="disabled", text="⏳ Running...")
    if not console_visible:
        toggle_console()
    console_text.delete("1.0", tk.END)
    threading.Thread(target=_divide_work_thread, daemon=True).start()

# -------------------- App Lifecycle -------------------- #
threading.Thread(
    target=lambda: app.run(host='127.0.0.1', port=5000, debug=False, use_reloader=False),
    daemon=True
).start()

# -------------------- App Lifecycle -------------------- #
def _startup_key_checks():
    if not pgs_store.get("kobo_token", ""):
        prompt_kobo_token()



root.protocol("WM_DELETE_WINDOW", lambda: (save_window_geometry(), root.destroy()))
root.after(200, resize_window)
root.after(800, _startup_key_checks)
root.mainloop()