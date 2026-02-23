import os
import re
from dataclasses import dataclass
from io import BytesIO
from typing import Dict, Tuple, Optional, List

import pandas as pd
import streamlit as st

# ==========================================================
# Aifuge Freight Engine V8
# - 5 carriers cost + sell price + margin
# - auto-load from data/
# - key format: PREFIX-CC--ZZ (double dash)
# - robust key matching (tries multiple zone candidates)
# ==========================================================

APP_VERSION = "V8"
DATA_DIR = "data"

PATHS = {
    "DHL": f"{DATA_DIR}/DHL_Frachtkosten.xlsx",
    "RABEN": f"{DATA_DIR}/Raben_Frachtkosten.xlsx",
    "RABEN_RULES": f"{DATA_DIR}/Raben 立方米及装载米规则.xlsx",
    "SCHENKER": f"{DATA_DIR}/Schenker_Frachtkosten.xlsx",
    "SCHENKER_MAUT": f"{DATA_DIR}/Schenker_Maut.xlsx",
    "HELLMANN": f"{DATA_DIR}/Hellmann_Frachtkosten_2026.xlsx",
    "FEDEX": f"{DATA_DIR}/FedEx_Frachtkosten.xlsx",
    "SELL": f"{DATA_DIR}/超大件账号价格-2026.01.01.xlsx",
}

st.set_page_config(layout="wide")
st.title(f"Aifuge Freight Engine {APP_VERSION} — 成本 + 对客报价 + 毛利")

# -----------------------------
# Common helpers
# -----------------------------

def cc(x: str) -> str:
    return str(x).strip().upper()

def digits(s: str) -> str:
    return re.sub(r"\D+", "", str(s or ""))

def zip_prefix_candidates(postal: str) -> List[str]:
    """
    Return multiple candidates to match keys robustly.
    Example DE 38112 -> ["38112","3811","381","38"]
    """
    d = digits(postal)
    if not d:
        return []
    cands = []
    for k in [5,4,3,2]:
        if len(d) >= k:
            cands.append(d[:k])
    # Deduplicate preserve order
    out = []
    for x in cands:
        if x not in out:
            out.append(x)
    return out

def make_key(prefix: str, country: str, zone: str) -> str:
    return f"{prefix}-{cc(country)}--{zone}"

def weight_cols(df: pd.DataFrame) -> List[str]:
    cols = [c for c in df.columns if str(c).strip().lower().startswith("bis-")]
    def w(c):
        m = re.search(r"bis-(\d+)", str(c))
        return int(m.group(1)) if m else 10**9
    return sorted(cols, key=w)

def pick_bracket(cols: List[str], w_kg: float) -> str:
    # choose the first bis-X that >= w_kg
    for c in cols:
        m = re.search(r"bis-(\d+)", str(c))
        if m and w_kg <= float(m.group(1)):
            return c
    return cols[-1] if cols else ""

def safe_float(x, default=0.0) -> float:
    try:
        if pd.isna(x):
            return default
        return float(x)
    except Exception:
        return default

# -----------------------------
# Cargo model
# -----------------------------

@dataclass
class CargoLine:
    qty: int
    piece_weight_kg: float
    l_cm: float
    w_cm: float
    h_cm: float

    def piece_cbm(self) -> float:
        return (self.l_cm * self.w_cm * self.h_cm) / 1_000_000.0

    def max_dim_cm(self) -> float:
        return max(self.l_cm, self.w_cm, self.h_cm)

def cargo_totals(lines: List[CargoLine]) -> Tuple[float, float]:
    total_w = sum(max(0, ln.qty) * max(0.0, ln.piece_weight_kg) for ln in lines)
    total_cbm = sum(max(0, ln.qty) * max(0.0, ln.piece_cbm()) for ln in lines)
    return total_w, total_cbm

def any_piece_over_240(lines: List[CargoLine]) -> bool:
    return any(ln.max_dim_cm() > 240 for ln in lines)

# -----------------------------
# Load "matrix" Excel (key + bis-xx columns)
# -----------------------------

@st.cache_data
def load_matrix(path: str) -> Tuple[Dict[str, Dict[str, float]], List[str], Optional[str]]:
    """
    Returns:
      lut[key][bis-col] = price
      cols = sorted bis cols
      keycol = first column name
    """
    df = pd.read_excel(path)
    df.columns = [str(c).strip() for c in df.columns]
    if df.shape[1] < 2:
        return {}, [], None
    keycol = df.columns[0]
    cols = weight_cols(df)
    lut: Dict[str, Dict[str, float]] = {}
    for _, r in df.iterrows():
        k = str(r.get(keycol, "")).strip()
        if not k:
            continue
        row_map = {}
        for c in cols:
            v = r.get(c)
            if pd.notna(v):
                row_map[c] = safe_float(v)
        if row_map:
            lut[k] = row_map
    return lut, cols, keycol

def try_load_matrix(path: str, label: str):
    if not os.path.exists(path):
        st.warning(f"⚠️ {label} 价格表未找到：{path}")
        return {}, [], None
    try:
        return load_matrix(path)
    except Exception as e:
        st.warning(f"⚠️ {label} 价格表读取失败：{e}")
        return {}, [], None

DHL_LUT, DHL_COLS, _ = try_load_matrix(PATHS["DHL"], "DHL")
RABEN_LUT, RABEN_COLS, _ = try_load_matrix(PATHS["RABEN"], "Raben")
SCH_LUT, SCH_COLS, _ = try_load_matrix(PATHS["SCHENKER"], "Schenker")
HEL_LUT, HEL_COLS, _ = try_load_matrix(PATHS["HELLMANN"], "Hellmann")
FX_LUT, FX_COLS, _ = try_load_matrix(PATHS["FEDEX"], "FedEx")
SELL_LUT, SELL_COLS, _ = try_load_matrix(PATHS["SELL"], "对客报价(Sell)")

# -----------------------------
# Raben volumetric factor rules (fallback = 200)
# -----------------------------

@st.cache_data
def load_raben_factor_rules(path: str) -> Dict[str, float]:
    """
    Try to find a sheet with columns like country/factor, or any table.
    We keep this tolerant: if cannot parse, returns empty -> default 200.
    """
    if not os.path.exists(path):
        return {}
    try:
        xls = pd.ExcelFile(path)
        best: Dict[str, float] = {}
        for sh in xls.sheet_names:
            df = xls.parse(sh)
            df.columns = [str(c).strip().lower() for c in df.columns]
            # heuristic: find country + factor columns
            col_country = None
            col_factor = None
            for c in df.columns:
                if "country" in c or "land" in c:
                    col_country = c
                if "factor" in c or "kg/cbm" in c or "kg je cbm" in c or "volum" in c:
                    col_factor = c
            if col_country and col_factor:
                for _, r in df.iterrows():
                    ctry = cc(r.get(col_country, ""))
                    fac = safe_float(r.get(col_factor, None), default=0.0)
                    if ctry and fac > 0:
                        best[ctry] = fac
        return best
    except Exception:
        return {}

RABEN_FACTOR_RULES = load_raben_factor_rules(PATHS["RABEN_RULES"])

def raben_factor(country: str) -> float:
    return float(RABEN_FACTOR_RULES.get(cc(country), 200.0))

# -----------------------------
# Hellmann country rules (Maut% + Staatliche Abgaben%)
# FINAL V5 dict based on your screenshots
# -----------------------------

HELLMANN_RULES = {
    "DE": {"maut_pct": 18.2, "state_pct": 0.0, "vol_factor": 150.0},
    "AT": {"maut_pct": 13.3, "state_pct": 6.6, "vol_factor": 200.0},
    "BE": {"maut_pct": 9.7,  "state_pct": 2.1, "vol_factor": 200.0},
    "BG": {"maut_pct": 6.2,  "state_pct": 9.9, "vol_factor": 200.0},
    "CZ": {"maut_pct": 8.6,  "state_pct": 5.4, "vol_factor": 200.0},
    "DK": {"maut_pct": 8.6,  "state_pct": 0.1, "vol_factor": 200.0},
    "EE": {"maut_pct": 7.2,  "state_pct": 0.0, "vol_factor": 200.0},
    "ES": {"maut_pct": 6.7,  "state_pct": 0.0, "vol_factor": 200.0},
    "FI": {"maut_pct": 4.8,  "state_pct": 3.1, "vol_factor": 200.0},
    "FR": {"maut_pct": 7.7,  "state_pct": 0.5, "vol_factor": 200.0},
    "GR": {"maut_pct": 7.8,  "state_pct": 10.0,"vol_factor": 200.0},
    "HR": {"maut_pct": 9.1,  "state_pct": 11.6,"vol_factor": 200.0},
    "HU": {"maut_pct": 11.5, "state_pct": 15.2,"vol_factor": 200.0},
    "IE": {"maut_pct": 6.1,  "state_pct": 3.6, "vol_factor": 200.0},
    "IT": {"maut_pct": 10.3, "state_pct": 7.0, "vol_factor": 200.0},
    "LT": {"maut_pct": 7.6,  "state_pct": 0.0, "vol_factor": 200.0},
    "LU": {"maut_pct": 10.9, "state_pct": 0.0, "vol_factor": 200.0},
    "LV": {"maut_pct": 7.0,  "state_pct": 0.0, "vol_factor": 200.0},
    "NL": {"maut_pct": 8.9,  "state_pct": 0.0, "vol_factor": 200.0},
    "PL": {"maut_pct": 10.2, "state_pct": 2.6, "vol_factor": 200.0},
    "PT": {"maut_pct": 7.7,  "state_pct": 0.0, "vol_factor": 200.0},
    "RO": {"maut_pct": 7.0,  "state_pct": 10.6,"vol_factor": 200.0},
    "SE": {"maut_pct": 3.6,  "state_pct": 0.7, "vol_factor": 200.0},
    "SI": {"maut_pct": 12.5, "state_pct": 15.3,"vol_factor": 200.0},
    "SK": {"maut_pct": 8.5,  "state_pct": 5.9, "vol_factor": 200.0},
    "XK": {"maut_pct": 3.4,  "state_pct": 4.3, "vol_factor": 200.0},
}

HELLMANN_DG_30_COUNTRIES = {
    "AL","AT","BA","BE","BG","CH","CZ","DK","EE","ES","FI","FR","HR","HU","IT","LT","LU","LV","ME","MK","NL","PL","PT","RO","RS","SI","SK","XK"
}
HELLMANN_DG_75_COUNTRIES = {"FI","GB","GR","IE","NO","SE"}  # per your note

def hellmann_rule(country: str) -> Dict[str, float]:
    c = cc(country)
    base = HELLMANN_RULES.get(c, {"maut_pct": 0.0, "state_pct": 0.0, "vol_factor": 200.0})
    return base

# -----------------------------
# Diesel floater helper (Hellmann)
# based on the table in your screenshot:
# up to 1.48 -> 0.0
# 1.50 -> 0.5
# 1.52 -> 1.0
# ...
# 1.62 -> 3.5
# then each +0.02 => +0.5
# -----------------------------

def hellmann_diesel_pct_from_price(diesel_price_per_l: float) -> float:
    p = float(diesel_price_per_l)
    if p <= 1.48:
        return 0.0
    # anchor at 1.50 => 0.5
    if p <= 1.50:
        return 0.5
    # base at 1.50
    extra = max(0.0, p - 1.50)
    steps = int(extra / 0.02 + 1e-9)  # full 0.02 steps
    return 0.5 + steps * 0.5

# -----------------------------
# Schenker Maut table (best-effort parser + manual override)
# You said: maut depends on "total weight" + "distance km"
# We'll compute based on chargeable weight (Fra.Gew) by default.
# If parsing fails, allow manual entry.
# -----------------------------

@st.cache_data
def load_schenker_maut_table(path: str) -> Optional[pd.DataFrame]:
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_excel(path)
        df.columns = [str(c).strip().lower() for c in df.columns]
        # If already normalized with columns containing kg/km and amount:
        # Try find columns
        cols = df.columns
        if any("km" in c for c in cols) and any("kg" in c or "gew" in c for c in cols):
            return df
        return df
    except Exception:
        return None

SCHENKER_MAUT_DF = load_schenker_maut_table(PATHS["SCHENKER_MAUT"])

def schenker_maut_lookup(df: Optional[pd.DataFrame], w_kg: float, km: float) -> Optional[float]:
    """
    Best effort:
    Expected columns (any variant):
      kg_von, kg_bis, km_von, km_bis, maut
    If not found -> None
    """
    if df is None or df.empty:
        return None

    cols = list(df.columns)

    def find_col(keys):
        for k in keys:
            for c in cols:
                if k in c:
                    return c
        return None

    c_kg_from = find_col(["kg_von","kg from","von_kg","weight_from","gew_von"])
    c_kg_to   = find_col(["kg_bis","kg to","bis_kg","weight_to","gew_bis"])
    c_km_from = find_col(["km_von","km from","von_km","dist_from","distance_from"])
    c_km_to   = find_col(["km_bis","km to","bis_km","dist_to","distance_to"])
    c_val     = find_col(["maut","toll","betrag","amount","eur"])

    if not all([c_kg_from, c_kg_to, c_km_from, c_km_to, c_val]):
        return None

    # filter
    sub = df.copy()
    sub[c_kg_from] = pd.to_numeric(sub[c_kg_from], errors="coerce")
    sub[c_kg_to]   = pd.to_numeric(sub[c_kg_to], errors="coerce")
    sub[c_km_from] = pd.to_numeric(sub[c_km_from], errors="coerce")
    sub[c_km_to]   = pd.to_numeric(sub[c_km_to], errors="coerce")
    sub[c_val]     = pd.to_numeric(sub[c_val], errors="coerce")

    sub = sub.dropna(subset=[c_kg_from,c_kg_to,c_km_from,c_km_to,c_val])

    w = float(w_kg)
    d = float(km)

    hit = sub[
        (sub[c_kg_from] <= w) & (w <= sub[c_kg_to]) &
        (sub[c_km_from] <= d) & (d <= sub[c_km_to])
    ]

    if hit.empty:
        return None

    # take first (should be unique)
    return float(hit.iloc[0][c_val])

# -----------------------------
# Robust key matching
# -----------------------------

def lookup_price(lut: Dict[str, Dict[str, float]], cols: List[str], prefix: str, country: str, postal: str, w_kg: float):
    """
    Try multiple key candidates:
      PREFIX-CC--<postal 5/4/3/2>
    Return (ok, key_used, bracket, price)
    """
    if not lut:
        return False, "", "", 0.0, "无价格表"

    ctry = cc(country)
    candidates = zip_prefix_candidates(postal)
    tried_keys = []

    for z in candidates:
        k = make_key(prefix, ctry, z)
        tried_keys.append(k)
        if k in lut:
            bracket = pick_bracket(cols, w_kg)
            if not bracket:
                return False, k, "", 0.0, "缺少重量区间列(bis-*)"
            price = lut[k].get(bracket)
            if price is None:
                return False, k, bracket, 0.0, "该重量区间无价格"
            return True, k, bracket, float(price), ""

    # Also try exact key as stored if user typed already like "38" etc (done above)
    return False, "", "", 0.0, f"未找到线路（已尝试 {len(tried_keys)} 个key，如：{tried_keys[:2]} ...）"

# -----------------------------
# Chargeable weight rules
# -----------------------------

def charge_weight_total(actual_kg: float, cbm: float, vol_factor: float) -> float:
    return max(float(actual_kg), float(cbm) * float(vol_factor))

def fedex_charge_weight_piecewise(lines: List[CargoLine], vol_factor: float = 200.0, min_piece_kg: float = 68.0) -> float:
    total = 0.0
    for ln in lines:
        for _ in range(max(0, ln.qty)):
            piece_actual = max(0.0, ln.piece_weight_kg)
            piece_vol = max(0.0, ln.piece_cbm()) * vol_factor
            piece_charge = max(piece_actual, piece_vol, min_piece_kg)
            total += piece_charge
    return total

# -----------------------------
# UI Inputs
# -----------------------------

st.subheader("输入")

colA, colB = st.columns([1,2])
with colA:
    dest_country = cc(st.text_input("目的国家 (ISO2)", "DE"))
with colB:
    dest_postal = st.text_input("收件邮编 (用于key匹配)", "38112")

# cargo editor
cargo_df = st.data_editor(
    pd.DataFrame([{"qty": 1, "weight_kg": 20.0, "l_cm": 60.0, "w_cm": 40.0, "h_cm": 40.0}]),
    num_rows="dynamic",
    use_container_width=True
)

lines: List[CargoLine] = []
for _, r in cargo_df.iterrows():
    lines.append(
        CargoLine(
            qty=int(r.get("qty", 0) or 0),
            piece_weight_kg=float(r.get("weight_kg", 0) or 0),
            l_cm=float(r.get("l_cm", 0) or 0),
            w_cm=float(r.get("w_cm", 0) or 0),
            h_cm=float(r.get("h_cm", 0) or 0),
        )
    )

actual_kg, total_cbm = cargo_totals(lines)
sell_charge_kg = charge_weight_total(actual_kg, total_cbm, 200.0)  # customer volumetric factor always 200 kg/m³ (your rule)

st.markdown(
    f"**合计实重(kg)：** {actual_kg:.2f}  ｜  **合计体积(m³)：** {total_cbm:.4f}  ｜  "
    f"**对客计费重(kg)：** {sell_charge_kg:.2f}（max(实重, 体积×200)）"
)

st.divider()

st.subheader("附加费/参数（可上线：先手动，后续再接API）")

p1, p2, p3 = st.columns(3)

with p1:
    dhl_avis = st.checkbox("DHL Avis（电话预约）+11€", value=False)
    schenker_avis = st.checkbox("Schenker Avis（电话预约）+20€", value=False)
    schenker_floating_pct = st.number_input("Schenker Diesel Floating %（手动）", min_value=0.0, max_value=100.0, value=0.0, step=0.1)

with p2:
    schenker_distance_km = st.number_input("Schenker 运输距离 km（手动）", min_value=0.0, value=0.0, step=1.0)
    schenker_maut_manual = st.number_input("Schenker Maut €（手动覆盖，0=不用）", min_value=0.0, value=0.0, step=0.01)
    # if you want choose Fra/Wirk later:
    schenker_maut_use_charge = st.checkbox("Schenker Maut 使用计费重(Fra.Gew)", value=True)

with p3:
    hellmann_b2c = st.checkbox("Hellmann B2C +8.9€", value=False)
    hellmann_avis = st.checkbox("Hellmann Avis +12.5€", value=False)
    hellmann_dg = st.checkbox("Hellmann 危险品 DG（叠加B2C/Avis）", value=False)
    hellmann_diesel_price = st.number_input("Hellmann Diesel €/L（用于Diesel-Floater）", min_value=0.0, value=1.50, step=0.01)

length_over_240 = any_piece_over_240(lines)
st.caption("单件最长边 > 240cm 时，Hellmann Längenzuschlag 可叠加（你说的规则）。")
hellmann_length = st.checkbox("Hellmann Längenzuschlag +30€（单件最长边>240cm）", value=length_over_240)

st.divider()

# -----------------------------
# Calculate per carrier
# -----------------------------

def calc_dhl() -> dict:
    vol_factor = 200.0
    cw = charge_weight_total(actual_kg, total_cbm, vol_factor)
    ok, k, br, base, note = lookup_price(DHL_LUT, DHL_COLS, "DHL", dest_country, dest_postal, cw)
    extras = 11.0 if dhl_avis else 0.0
    total = base + extras if ok else 0.0
    return {"carrier":"DHL","ok":ok,"key":k,"charge_kg":cw,"bracket":br,"base":base,"extras":extras,"total":total,"note":note}

def calc_raben() -> dict:
    vol_factor = raben_factor(dest_country)  # from rules or default 200
    cw = charge_weight_total(actual_kg, total_cbm, vol_factor)
    ok, k, br, base, note = lookup_price(RABEN_LUT, RABEN_COLS, "RABEN", dest_country, dest_postal, cw)
    # Raben: you said some zones not served -> if key missing that's expected.
    extras = 0.0
    total = base + extras if ok else 0.0
    if ok and vol_factor != 200.0:
        note = (note + "；" if note else "") + f"Raben体积系数={vol_factor:g}kg/cbm"
    return {"carrier":"Raben","ok":ok,"key":k,"charge_kg":cw,"bracket":br,"base":base,"extras":extras,"total":total,"note":note}

def calc_schenker() -> dict:
    vol_factor = 150.0 if cc(dest_country) == "DE" else 200.0
    cw = charge_weight_total(actual_kg, total_cbm, vol_factor)
    ok, k, br, base, note = lookup_price(SCH_LUT, SCH_COLS, "SCHENKER", dest_country, dest_postal, cw)

    # Diesel floating as % of freight cost (base)
    floating = (schenker_floating_pct/100.0) * base if ok else 0.0

    # Maut
    maut = 0.0
    if ok:
        if schenker_maut_manual > 0:
            maut = schenker_maut_manual
        else:
            w_for_maut = cw if schenker_maut_use_charge else actual_kg
            maut_guess = schenker_maut_lookup(SCHENKER_MAUT_DF, w_for_maut, schenker_distance_km)
            if maut_guess is not None:
                maut = maut_guess
            else:
                # If we can't parse the maut table, keep 0 but add note
                note = (note + "；" if note else "") + "Maut表未能自动解析（可手动输入覆盖）"

    avis = 20.0 if schenker_avis else 0.0
    extras = floating + maut + avis
    total = base + extras if ok else 0.0
    return {
        "carrier":"Schenker",
        "ok":ok,
        "key":k,
        "charge_kg":cw,
        "bracket":br,
        "base":base,
        "extras":extras,
        "total":total,
        "note":note + (f"；DieselFloating={schenker_floating_pct:.1f}%" if ok and schenker_floating_pct>0 else "")
    }

def calc_hellmann() -> dict:
    rule = hellmann_rule(dest_country)
    vol_factor = float(rule.get("vol_factor", 200.0))
    cw = charge_weight_total(actual_kg, total_cbm, vol_factor)

    ok, k, br, base, note = lookup_price(HEL_LUT, HEL_COLS, "HELLMANN", dest_country, dest_postal, cw)

    # Percent surcharges on base:
    maut_pct = float(rule.get("maut_pct", 0.0))
    state_pct = float(rule.get("state_pct", 0.0))

    maut = (maut_pct/100.0) * base if ok else 0.0
    state = (state_pct/100.0) * base if ok else 0.0

    # Diesel floater
    diesel_pct = hellmann_diesel_pct_from_price(hellmann_diesel_price)
    diesel = (diesel_pct/100.0) * base if ok else 0.0

    # Optional fixed surcharges
    b2c = 8.9 if hellmann_b2c else 0.0
    avis = 12.5 if hellmann_avis else 0.0
    length = 30.0 if hellmann_length else 0.0

    # DG varies:
    dg = 0.0
    if hellmann_dg:
        c = cc(dest_country)
        if c == "DE":
            dg = 15.0
        elif c in HELLMANN_DG_75_COUNTRIES:
            dg = 75.0
        else:
            dg = 30.0

    extras = maut + state + diesel + b2c + avis + length + dg
    total = base + extras if ok else 0.0

    if ok:
        note2 = []
        note2.append(f"Maut={maut_pct:g}%")
        note2.append(f"Staat={state_pct:g}%")
        note2.append(f"Diesel≈{diesel_pct:g}% (€/L={hellmann_diesel_price:.2f})")
        note = (note + "；" if note else "") + ", ".join(note2)

    return {"carrier":"Hellmann","ok":ok,"key":k,"charge_kg":cw,"bracket":br,"base":base,"extras":extras,"total":total,"note":note}

def calc_fedex() -> dict:
    # FedEx: piecewise min 68kg each piece + factor 200
    cw = fedex_charge_weight_piecewise(lines, vol_factor=200.0, min_piece_kg=68.0)
    ok, k, br, base, note = lookup_price(FX_LUT, FX_COLS, "FEDEX", dest_country, dest_postal, cw)
    # FedEx price already includes fuel/toll/avis per your rule -> no extras
    extras = 0.0
    total = base if ok else 0.0
    if ok:
        note = (note + "；" if note else "") + "FedEx含每件≥68kg规则(已计入计费重)"
    return {"carrier":"FedEx","ok":ok,"key":k,"charge_kg":cw,"bracket":br,"base":base,"extras":extras,"total":total,"note":note}

def calc_sell() -> dict:
    # Sell uses volumetric factor 200 and max(total actual, total cbm*200)
    cw = sell_charge_kg
    ok, k, br, base, note = lookup_price(SELL_LUT, SELL_COLS, "SELL", dest_country, dest_postal, cw)
    return {"sell_ok":ok, "sell_key":k, "sell_bracket":br, "sell_price":base, "sell_note":note}

# -----------------------------
# Run calcs
# -----------------------------

dhl = calc_dhl()
raben = calc_raben()
sch = calc_schenker()
hel = calc_hellmann()
fx = calc_fedex()
sell = calc_sell()

rows = [dhl, raben, sch, hel, fx]
out = pd.DataFrame([{
    "carrier": r["carrier"],
    "ok": r["ok"],
    "key": r["key"],
    "charge_kg": round(r["charge_kg"], 2),
    "bracket": r["bracket"],
    "base": round(r["base"], 2),
    "extras": round(r["extras"], 2),
    "total_cost": round(r["total"], 2),
    "sell_price": round(sell["sell_price"], 2) if sell["sell_ok"] else None,
    "profit": (round(sell["sell_price"] - r["total"], 2) if (sell["sell_ok"] and r["ok"]) else None),
    "note": r["note"]
} for r in rows])

st.subheader("五家同步报价对比（成本/对客/毛利）")
st.dataframe(out, use_container_width=True)

# Show sell debug
with st.expander("对客报价(Sell) 解析信息", expanded=False):
    st.write({
        "sell_ok": sell["sell_ok"],
        "sell_key": sell["sell_key"],
        "sell_bracket": sell["sell_bracket"],
        "sell_price": sell["sell_price"],
        "sell_note": sell["sell_note"]
    })

# -----------------------------
# Export
# -----------------------------

def export_excel() -> bytes:
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        # summary
        out.to_excel(w, index=False, sheet_name="compare")
        # inputs
        pd.DataFrame([{
            "dest_country": dest_country,
            "dest_postal": dest_postal,
            "actual_kg": actual_kg,
            "cbm": total_cbm,
            "sell_charge_kg": sell_charge_kg,
        }]).to_excel(w, index=False, sheet_name="inputs")
        cargo_df.to_excel(w, index=False, sheet_name="cargo")
    return buf.getvalue()

st.download_button("导出Excel", data=export_excel(), file_name="freight_compare.xlsx")
