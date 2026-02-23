# ============================================
# AIFUGE FREIGHT ENGINE V7.1 (PRODUCTION FIX)
# Fix: FedEx Excel numeric parsing (0,19 / text / empty)
# ============================================

import os
import re
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from typing import Dict, List, Tuple, Optional

import pandas as pd
import streamlit as st


APP_VERSION = "V7.1"
DATA_DIR = "data"

# =============================
# File paths (match repo files)
# =============================
PATHS = {
    "DHL": f"{DATA_DIR}/DHL_Frachtkosten.xlsx",
    "RABEN": f"{DATA_DIR}/Raben_Frachtkosten.xlsx",
    "SCHENKER": f"{DATA_DIR}/Schenker_Frachtkosten.xlsx",
    "HELLMANN": f"{DATA_DIR}/Hellmann_Frachtkosten_2026.xlsx",
    "FEDEX": f"{DATA_DIR}/FedEx_Frachtkosten.xlsx",
    "SELL_XLS": f"{DATA_DIR}/超大件账号价格-2026.01.01.xls",
    "SELL_XLSX": f"{DATA_DIR}/超大件账号价格-2026.01.01.xlsx",
}

st.set_page_config(page_title="Aifuge Freight Engine V7.1", layout="wide")
st.title(f"Aifuge Freight Engine {APP_VERSION} — 成本 + 对客报价 + 毛利")

# ============================================
# Helpers
# ============================================

def cc(x) -> str:
    return str(x).strip().upper()

def zip2(z) -> str:
    m = re.search(r"(\d{2})", str(z))
    return m.group(1) if m else ""

def make_key(prefix: str, country: str, zz: str) -> str:
    # IMPORTANT: double dash format PREFIX-CC--ZZ
    return f"{prefix}-{cc(country)}--{zz}"

def weight_cols(df: pd.DataFrame) -> List[str]:
    cols = [c for c in df.columns if str(c).strip().lower().startswith("bis-")]
    def w(x):
        m = re.search(r"bis-(\d+)", str(x))
        return int(m.group(1)) if m else 999999
    return sorted(cols, key=w)

def pick_bracket(cols: List[str], w: float) -> str:
    for c in cols:
        m = re.search(r"bis-(\d+)", str(c))
        if m and w <= float(m.group(1)):
            return c
    return cols[-1]

def parse_float_maybe(v) -> Optional[float]:
    """
    Robust numeric parser:
    - "0,19" -> 0.19
    - "0.1900 €/kg" -> 0.19
    - None/"" -> None
    """
    if v is None:
        return None
    if isinstance(v, (int, float)) and pd.notna(v):
        return float(v)
    s = str(v).strip()
    if s == "" or s.lower() in {"nan", "none"}:
        return None
    # keep digits, comma, dot, minus
    s2 = re.sub(r"[^0-9,\.\-]", "", s)
    if s2 == "" or s2 in {"-", ".", ",", "-.", "-,"}:
        return None
    # if comma used as decimal separator
    if "," in s2 and "." not in s2:
        s2 = s2.replace(",", ".")
    # if both present, assume comma is thousands sep: "1,234.56"
    if "," in s2 and "." in s2:
        s2 = s2.replace(",", "")
    try:
        return float(s2)
    except Exception:
        return None

# ============================================
# Cargo model
# ============================================

@dataclass
class Cargo:
    qty: int
    weight: float
    l: float
    w: float
    h: float

def piece_cbm(r: Cargo) -> float:
    return (r.l * r.w * r.h) / 1_000_000

def total_cbm(rows: List[Cargo]) -> float:
    return sum(piece_cbm(r) * r.qty for r in rows)

def total_actual(rows: List[Cargo]) -> float:
    return sum(r.weight * r.qty for r in rows)

def chargeable(actual: float, cbm: float, factor: float) -> float:
    return max(actual, cbm * factor)

def fedex_chargeable(rows: List[Cargo]) -> float:
    """
    FedEx: factor=200, and MIN 68kg per piece
    Chargeable per piece = max(actual_piece, volumetric_piece, 68)
    Total = sum(piece_chargeable * qty)
    """
    total = 0.0
    for r in rows:
        vol_piece = piece_cbm(r) * 200
        per_piece = max(r.weight, vol_piece, 68.0)
        total += per_piece * r.qty
    return total

# ============================================
# Load matrix tables (DHL/Raben/Schenker/Hellmann)
# ============================================

@st.cache_data
def load_matrix(path: str):
    df = pd.read_excel(path)
    df.columns = [str(c).strip() for c in df.columns]
    keycol = df.columns[0]
    cols = weight_cols(df)

    lut: Dict[str, Dict[str, float]] = {}
    for _, r in df.iterrows():
        k = str(r[keycol]).strip()
        if not k or k.lower() == "nan":
            continue
        row_map = {}
        for c in cols:
            val = parse_float_maybe(r.get(c))
            if val is not None:
                row_map[c] = val
        if row_map:
            lut[k] = row_map
    return lut, cols

def load_safe(label: str, path: str):
    if not os.path.exists(path):
        st.warning(f"⚠️ {label} 价格表未找到：{path}")
        return None, None
    try:
        return load_matrix(path)
    except Exception as e:
        st.warning(f"⚠️ {label} 价格表读取失败：{e}")
        return None, None

DHL_LUT, DHL_COLS = load_safe("DHL", PATHS["DHL"])
RABEN_LUT, RABEN_COLS = load_safe("Raben", PATHS["RABEN"])
SCH_LUT, SCH_COLS = load_safe("Schenker", PATHS["SCHENKER"])
HEL_LUT, HEL_COLS = load_safe("Hellmann", PATHS["HELLMANN"])

# ============================================
# Load FedEx (€/kg by country)
# ============================================

@st.cache_data
def load_fedex(path: str) -> Dict[str, float]:
    df = pd.read_excel(path)
    df.columns = [str(c).strip() for c in df.columns]

    # try to detect columns by name
    # expected: country + rate
    c0 = df.columns[0]
    c1 = df.columns[1] if len(df.columns) > 1 else None
    if c1 is None:
        raise ValueError("FedEx 表格至少需要2列：国家 + €/kg")

    lut: Dict[str, float] = {}
    for _, r in df.iterrows():
        country = cc(r.get(c0, ""))
        if not country or country.lower() == "nan":
            continue
        rate = parse_float_maybe(r.get(c1))
        if rate is None:
            # skip non-numeric rows
            continue
        lut[f"FEDEX-{country}"] = float(rate)
    if not lut:
        raise ValueError("FedEx 价格表未解析出任何有效国家/价格（检查第二列是否为数字）")
    return lut

FEDEX_LUT: Dict[str, float] = {}
if os.path.exists(PATHS["FEDEX"]):
    try:
        FEDEX_LUT = load_fedex(PATHS["FEDEX"])
    except Exception as e:
        st.warning(f"⚠️ FedEx 价格表读取失败：{e}")

# ============================================
# Load Sell table (customer pricing)
# ============================================

@st.cache_data
def load_sell():
    p = PATHS["SELL_XLSX"] if os.path.exists(PATHS["SELL_XLSX"]) else PATHS["SELL_XLS"]
    if not os.path.exists(p):
        return None, None

    df = pd.read_excel(p)
    df.columns = [str(c).strip() for c in df.columns]
    cols = weight_cols(df)
    keycol = df.columns[0]

    lut: Dict[str, Dict[str, float]] = {}

    country_map = {
        "DEUTSCHLAND": "DE", "GERMANY": "DE", "德国": "DE",
        "ÖSTERREICH": "AT", "OESTERREICH": "AT", "AUSTRIA": "AT", "奥地利": "AT",
        "BELGIEN": "BE", "BELGIUM": "BE", "比利时": "BE",
        "NIEDERLANDE": "NL", "NETHERLANDS": "NL", "荷兰": "NL",
        "POLEN": "PL", "POLAND": "PL", "波兰": "PL",
        "FRANKREICH": "FR", "FRANCE": "FR", "法国": "FR",
        "ITALIEN": "IT", "ITALY": "IT", "意大利": "IT",
        "SPANIEN": "ES", "SPAIN": "ES", "西班牙": "ES",
    }

    for _, r in df.iterrows():
        raw = str(r.get(keycol, "")).strip()
        if not raw or raw.lower() == "nan":
            continue

        zz = zip2(raw)
        if not zz:
            continue

        cname = re.sub(r"\d+", "", raw).strip().upper()
        if cname not in country_map:
            continue

        k = make_key("SELL", country_map[cname], zz)
        row_map = {}
        for c in cols:
            val = parse_float_maybe(r.get(c))
            if val is not None:
                row_map[c] = float(val)
        if row_map:
            lut[k] = row_map

    return lut, cols

SELL_LUT, SELL_COLS = load_sell()

# ============================================
# Hellmann rules V5 (country: (vol_factor, maut%, state%, dg_fee))
# DG fee: DE=15; EU 30/75 based on your rule list
# ============================================

HELLMANN_RULES = {
    "DE": (150, 0.182, 0.0, 15),
    "AT": (200, 0.133, 0.066, 30),
    "BE": (200, 0.097, 0.021, 30),
    "BG": (200, 0.062, 0.099, 30),
    "CZ": (200, 0.086, 0.054, 30),
    "DK": (200, 0.086, 0.001, 30),
    "EE": (200, 0.072, 0.0, 30),
    "ES": (200, 0.067, 0.0, 30),
    "FI": (200, 0.048, 0.031, 75),
    "FR": (200, 0.077, 0.005, 30),
    "GR": (200, 0.078, 0.100, 75),
    "HU": (200, 0.115, 0.152, 30),
    "HR": (200, 0.091, 0.116, 30),
    "IE": (200, 0.061, 0.036, 75),
    "IT": (200, 0.103, 0.070, 30),
    "LT": (200, 0.076, 0.0, 30),
    "LU": (200, 0.109, 0.0, 30),
    "LV": (200, 0.070, 0.0, 30),
    "NL": (200, 0.089, 0.0, 30),
    "PL": (200, 0.102, 0.026, 30),
    "PT": (200, 0.077, 0.0, 30),
    "RO": (200, 0.070, 0.106, 30),
    "SE": (200, 0.036, 0.007, 75),
    "SI": (200, 0.125, 0.153, 30),
    "SK": (200, 0.085, 0.059, 30),
    "XK": (200, 0.034, 0.043, 30),
}

# ============================================
# UI INPUT
# ============================================

c1, c2 = st.columns(2)
with c1:
    dest_cc = cc(st.text_input("目的国家 ISO2", "DE"))
with c2:
    dest_plz = st.text_input("目的邮编", "38112")

zz = zip2(dest_plz)
if not zz:
    st.error("请输入有效邮编（至少包含两位数字）")
    st.stop()

st.subheader("货物明细（件数、实重kg、长宽高cm）")
cargo_df = st.data_editor(
    pd.DataFrame([{"qty": 1, "weight": 20, "l": 60, "w": 40, "h": 40}]),
    num_rows="dynamic",
    use_container_width=True
)

rows: List[Cargo] = []
for _, r in cargo_df.iterrows():
    rows.append(Cargo(
        qty=int(r["qty"]),
        weight=float(r["weight"]),
        l=float(r["l"]),
        w=float(r["w"]),
        h=float(r["h"]),
    ))

actual = total_actual(rows)
cbm = total_cbm(rows)

# volumetric factor by carrier
charge_dhl = chargeable(actual, cbm, 200)
charge_raben = chargeable(actual, cbm, 200)
charge_sch = chargeable(actual, cbm, 150 if dest_cc == "DE" else 200)
charge_hel = chargeable(actual, cbm, 150 if dest_cc == "DE" else 200)
charge_fx = fedex_chargeable(rows)

st.caption(
    f"总实重={actual:.2f} kg | 总体积={cbm:.4f} cbm | "
    f"计费重: DHL={charge_dhl:.2f}, Raben={charge_raben:.2f}, "
    f"Schenker={charge_sch:.2f}, Hellmann={charge_hel:.2f}, FedEx={charge_fx:.2f} (含每件≥68kg)"
)

# ============================================
# Core calc
# ============================================

def calc_matrix(lut, cols, prefix, charge_w):
    k = make_key(prefix, dest_cc, zz)
    if not lut or k not in lut:
        return False, k, 0.0, "-"
    bc = pick_bracket(cols, charge_w)
    if bc not in lut[k]:
        return False, k, 0.0, bc
    return True, k, float(lut[k][bc]), bc

rows_out = []

# DHL / Raben / Schenker
for name, lut, cols, charge_w in [
    ("DHL", DHL_LUT, DHL_COLS, charge_dhl),
    ("Raben", RABEN_LUT, RABEN_COLS, charge_raben),
    ("Schenker", SCH_LUT, SCH_COLS, charge_sch),
]:
    ok, k, base, bracket = calc_matrix(lut, cols, name.upper(), charge_w)
    note = "" if ok else f"未找到线路 key={k}"
    rows_out.append({
        "carrier": name,
        "ok": ok,
        "key": k,
        "bracket": bracket,
        "base": round(base, 2),
        "extras": 0.0,
        "total": round(base, 2),
        "note": note
    })

# Hellmann
hel_ok = False
hel_base = 0.0
hel_total = 0.0
hel_bracket = "-"
hel_key = make_key("HELLMANN", dest_cc, zz)

if dest_cc not in HELLMANN_RULES:
    rows_out.append({
        "carrier": "Hellmann",
        "ok": False,
        "key": hel_key,
        "bracket": "-",
        "base": 0.0,
        "extras": 0.0,
        "total": 0.0,
        "note": f"Hellmann 规则缺失：{dest_cc}"
    })
else:
    vol_factor, maut_pct, state_pct, _dg_fee = HELLMANN_RULES[dest_cc]
    hel_ok, hel_key, hel_base, hel_bracket = calc_matrix(HEL_LUT, HEL_COLS, "HELLMANN", charge_hel)
    if hel_ok:
        extras = hel_base * maut_pct + hel_base * state_pct
        hel_total = hel_base + extras
        rows_out.append({
            "carrier": "Hellmann",
            "ok": True,
            "key": hel_key,
            "bracket": hel_bracket,
            "base": round(hel_base, 2),
            "extras": round(extras, 2),
            "total": round(hel_total, 2),
            "note": f"maut={maut_pct*100:.1f}%, state={state_pct*100:.1f}%, factor={vol_factor}"
        })
    else:
        rows_out.append({
            "carrier": "Hellmann",
            "ok": False,
            "key": hel_key,
            "bracket": hel_bracket,
            "base": 0.0,
            "extras": 0.0,
            "total": 0.0,
            "note": f"未找到线路 key={hel_key}"
        })

# FedEx
fx_key = f"FEDEX-{dest_cc}"
if fx_key in FEDEX_LUT:
    rate = FEDEX_LUT[fx_key]
    fx_cost = rate * charge_fx
    rows_out.append({
        "carrier": "FedEx",
        "ok": True,
        "key": fx_key,
        "bracket": "€/kg",
        "base": round(fx_cost, 2),
        "extras": 0.0,
        "total": round(fx_cost, 2),
        "note": f"rate={rate:.4f} €/kg; chargeable(FedEx)={charge_fx:.2f}kg(含每件≥68kg)"
    })
else:
    rows_out.append({
        "carrier": "FedEx",
        "ok": False,
        "key": fx_key,
        "bracket": "€/kg",
        "base": 0.0,
        "extras": 0.0,
        "total": 0.0,
        "note": f"FedEx 未找到国家 {dest_cc} 的 €/kg"
    })

# ============================================
# Sell price + Profit
# ============================================

sell_price = None
sell_key = make_key("SELL", dest_cc, zz)
sell_bracket = "-"
if SELL_LUT and sell_key in SELL_LUT:
    sell_charge = chargeable(actual, cbm, 200)  # customer side factor fixed 200
    sell_bracket = pick_bracket(SELL_COLS, sell_charge)
    if sell_bracket in SELL_LUT[sell_key]:
        sell_price = float(SELL_LUT[sell_key][sell_bracket])

df = pd.DataFrame(rows_out)

if sell_price is not None:
    df["sell_key"] = sell_key
    df["sell_bracket"] = sell_bracket
    df["sell_price"] = round(sell_price, 2)
    df["profit"] = round(df["sell_price"] - df["total"], 2)
else:
    df["sell_key"] = sell_key
    df["sell_bracket"] = sell_bracket
    df["sell_price"] = None
    df["profit"] = None

st.subheader("五家同步报价对比（含对客报价&毛利）")
st.dataframe(df, use_container_width=True)

# ============================================
# Export
# ============================================

def export_xlsx(df_to_save: pd.DataFrame) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as w:
        df_to_save.to_excel(w, index=False, sheet_name="compare")
    return output.getvalue()

st.download_button(
    "导出Excel",
    data=export_xlsx(df),
    file_name=f"Freight_{APP_VERSION}_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
)
