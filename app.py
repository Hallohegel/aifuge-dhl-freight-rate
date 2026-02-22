# app.py
# Aifuge Freight Cost Engine V5.4
# 4家干线 + FedEx 同屏对比：DHL / Raben / Schenker(DSV) / Hellmann / FedEx
#
# 计费重（最终确认版）：
# - DHL：billable = max(实重, 体积重), factor=200（所有国家）
# - Raben：billable = max(实重, 体积重), factor=从规则表读取
# - Schenker：billable = max(实重, 体积重), factor=DE150 / 其它200
# - Hellmann：billable = max(实重, 体积重), factor=DE150 / 其它200 + 国家Maut%/Staat% + Dieselfloater + DG/B2C/Avis/长件
# - FedEx：每件 billable_piece = max(实重, 体积重(200), 68)；总 billable = Σ(billable_piece*qty)；运费=€/kg*billable；all-in
#
# 报价表格式：system upload（key + bis-xxx）
# Raben factor：来自“Raben 立方米及装载米规则.xlsx”（国家->kg/cbm）

import re
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import streamlit as st


# =========================
# Helpers
# =========================
def _norm_country(s: str) -> str:
    return (s or "").strip().upper()


def _digits_only(s: str) -> str:
    return re.sub(r"\D+", "", s or "")


def _pc_prefix2(postal_code: str) -> str:
    d = _digits_only(postal_code)
    return d[:2] if len(d) >= 2 else ""


def _to_float(x, default=0.0) -> float:
    try:
        if pd.isna(x):
            return float(default)
        if isinstance(x, str):
            x = x.replace("€", "").replace("%", "").strip()
            x = x.replace(".", "").replace(",", ".") if re.search(r"\d+,\d+", x) else x
        return float(x)
    except Exception:
        return float(default)


def _ceil_to_band(value: float, bands: List[float]) -> float:
    for b in bands:
        if value <= b:
            return b
    return bands[-1]


def billable_max(gross_kg: float, cbm: float, factor_kg_per_cbm: float) -> float:
    return float(max(gross_kg, cbm * factor_kg_per_cbm))


def factor_de150_else200(country: str) -> float:
    return 150.0 if _norm_country(country) == "DE" else 200.0


# =========================
# Load system rate table (key + bis-xxx)
# =========================
@st.cache_data(show_spinner=False)
def load_system_rate_table_from_excel(file_bytes: bytes, sheet_name: Optional[str] = None) -> pd.DataFrame:
    df = pd.read_excel(file_bytes, sheet_name=sheet_name)
    df.columns = [str(c).strip() for c in df.columns]
    if "key" not in df.columns:
        raise ValueError("Rate table must contain a 'key' column.")
    weight_cols = [c for c in df.columns if str(c).startswith("bis-")]
    if not weight_cols:
        raise ValueError("Rate table must contain columns like 'bis-30', 'bis-50', ...")

    def _w(c):
        return _to_float(str(c).replace("bis-", ""), default=0)

    weight_cols = sorted(weight_cols, key=_w)
    df = df[["key"] + weight_cols].copy()
    for c in weight_cols:
        df[c] = df[c].apply(_to_float)
    return df


def rate_lookup(df: pd.DataFrame, key: str, weight_kg: float) -> float:
    row = df.loc[df["key"] == key]
    if row.empty:
        raise KeyError(f"Rate key not found: {key}")
    row = row.iloc[0]
    weight_cols = [c for c in df.columns if c.startswith("bis-")]
    bands = [float(c.replace("bis-", "")) for c in weight_cols]
    chosen = _ceil_to_band(weight_kg, bands)
    target = f"bis-{int(chosen) if float(chosen).is_integer() else chosen}"
    if target not in row.index:
        target = min(weight_cols, key=lambda c: abs(_to_float(c.replace("bis-", "")) - chosen))
    return float(row[target])


# =========================
# Raben factor rule table (country -> kg/cbm)
# =========================
@st.cache_data(show_spinner=False)
def load_raben_rule_table(file_bytes: bytes) -> pd.DataFrame:
    """
    容错解析：在所有sheet里寻找 “国家列 + kg/cbm列”
    输出：country, factor
    """
    xls = pd.ExcelFile(file_bytes)
    for sh in xls.sheet_names:
        df = pd.read_excel(file_bytes, sheet_name=sh)
        df.columns = [str(c).strip() for c in df.columns]
        cols = [c.lower() for c in df.columns.astype(str)]

        # country column
        country_col = None
        for i, c in enumerate(cols):
            if any(k in c for k in ["land", "country", "国家", "laender", "länder"]):
                country_col = df.columns[i]
                break
        if country_col is None:
            continue

        # factor column
        factor_col = None
        for i, c in enumerate(cols):
            if any(k in c for k in ["kg je cbm", "kg/cbm", "kg je m", "m³", "cbm", "frachtvolumen", "volumenverhältnis", "volum"]):
                factor_col = df.columns[i]
                break
        if factor_col is None:
            continue

        tmp = df[[country_col, factor_col]].copy()
        tmp.columns = ["country", "factor_raw"]
        tmp["country"] = tmp["country"].astype(str).str.strip().str.upper()
        tmp["factor"] = tmp["factor_raw"].apply(_to_float)
        tmp = tmp[(tmp["country"].str.len() >= 2) & (tmp["factor"] > 0)]
        if len(tmp) > 0:
            return tmp[["country", "factor"]].drop_duplicates("country").reset_index(drop=True)

    raise ValueError("无法从 Raben 规则表解析出 factor（国家->kg/cbm）。请确认表内有国家列+kg/cbm列。")


def raben_factor_for_country(rule_df: pd.DataFrame, country: str) -> float:
    c = _norm_country(country)
    m = rule_df[rule_df["country"] == c]
    if m.empty:
        raise KeyError(f"Raben 规则表未找到国家 {c} 的 factor。")
    return float(m.iloc[0]["factor"])


# =========================
# Schenker Maut matrix
# =========================
@dataclass
class MautMatrix:
    weight_ends: List[float]
    dist_ends: List[float]
    costs: np.ndarray


@st.cache_data(show_spinner=False)
def load_schenker_maut_matrix(file_bytes: bytes, sheet_name: Optional[str] = "Mauttabelle") -> MautMatrix:
    raw = pd.read_excel(file_bytes, sheet_name=sheet_name).copy()
    cols = list(raw.columns)

    c_w_start = cols[1]
    c_w_end = cols[2]

    start_row = None
    for i in range(len(raw)):
        a = raw.iloc[i][c_w_start]
        b = raw.iloc[i][c_w_end]
        if isinstance(a, (int, float, np.integer, np.floating)) and isinstance(b, (int, float, np.integer, np.floating)):
            if not pd.isna(a) and not pd.isna(b) and float(a) > 0 and float(b) >= float(a):
                start_row = i
                break
    if start_row is None:
        raise ValueError("Cannot find weight band start in Maut table.")

    dist_header_row = None
    for i in range(start_row - 1, -1, -1):
        rowvals = raw.iloc[i].astype(str).tolist()
        if any("Tarifentfernung" in x for x in rowvals):
            dist_header_row = i
            break
    if dist_header_row is None:
        dist_header_row = max(0, start_row - 3)

    r_de = dist_header_row + 2
    dist_cols = cols[3:]
    dist_ends, usable_dist_cols = [], []
    for c in dist_cols:
        de = raw.iloc[r_de][c]
        if isinstance(de, (int, float, np.integer, np.floating)) and not pd.isna(de):
            dist_ends.append(float(de))
            usable_dist_cols.append(c)
        else:
            break

    weight_ends, costs = [], []
    for i in range(start_row, len(raw)):
        we = raw.iloc[i][c_w_end]
        ws = raw.iloc[i][c_w_start]
        if not (isinstance(ws, (int, float, np.integer, np.floating)) and isinstance(we, (int, float, np.integer, np.floating))):
            break
        if pd.isna(ws) or pd.isna(we):
            break

        row_costs = []
        ok = True
        for c in usable_dist_cols:
            v = raw.iloc[i][c]
            if pd.isna(v):
                ok = False
                break
            row_costs.append(_to_float(v))
        if not ok:
            break

        weight_ends.append(float(we))
        costs.append(row_costs)

    if not weight_ends or not dist_ends:
        raise ValueError("Failed to parse Maut matrix.")
    return MautMatrix(weight_ends=weight_ends, dist_ends=dist_ends, costs=np.array(costs, dtype=float))


def schenker_maut_cost(maut: MautMatrix, weight_kg: float, distance_km: float) -> float:
    wi = next((i for i, we in enumerate(maut.weight_ends) if weight_kg <= we), len(maut.weight_ends) - 1)
    di = next((j for j, de in enumerate(maut.dist_ends) if distance_km <= de), len(maut.dist_ends) - 1)
    return float(maut.costs[wi, di])


# =========================
# Hellmann rules (Maut% / Staat% + DieselFloater + DG/B2C/Avis/长件)
# =========================
def hellmann_diesel_pct(diesel_price_eur_per_l: float) -> float:
    p = float(diesel_price_eur_per_l)
    steps = [
        (1.48, 0.0),
        (1.50, 0.5),
        (1.52, 1.0),
        (1.54, 1.5),
        (1.56, 2.0),
        (1.58, 2.5),
        (1.60, 3.0),
        (1.62, 3.5),
    ]
    for upper, pct in steps:
        if p <= upper + 1e-9:
            return pct / 100.0
    extra = p - 1.62
    n = int(np.ceil(extra / 0.02 - 1e-12))
    return (3.5 + 0.5 * n) / 100.0


HELLMANN_EU_DG_75 = {"FI", "GB", "GR", "IE", "NO", "SE"}

HELLMANN_RULES_2026: Dict[str, Dict[str, float]] = {
    "DE": {"maut_pct": 18.2, "staat_pct": 0.0},
    "AT": {"maut_pct": 13.3, "staat_pct": 6.6},
    "BE": {"maut_pct": 9.7,  "staat_pct": 2.1},
    "BG": {"maut_pct": 6.2,  "staat_pct": 9.9},
    "CZ": {"maut_pct": 8.6,  "staat_pct": 5.4},
    "DK": {"maut_pct": 8.6,  "staat_pct": 0.1},
    "EE": {"maut_pct": 7.2,  "staat_pct": 0.0},
    "ES": {"maut_pct": 6.7,  "staat_pct": 0.0},
    "FI": {"maut_pct": 4.8,  "staat_pct": 3.1},
    "FR": {"maut_pct": 7.7,  "staat_pct": 0.5},
    "GR": {"maut_pct": 7.8,  "staat_pct": 10.0},
    "HR": {"maut_pct": 9.1,  "staat_pct": 11.6},
    "HU": {"maut_pct": 11.5, "staat_pct": 15.2},
    "IE": {"maut_pct": 6.1,  "staat_pct": 3.6},
    "IT": {"maut_pct": 10.3, "staat_pct": 7.0},
    "LT": {"maut_pct": 7.6,  "staat_pct": 0.0},
    "LU": {"maut_pct": 10.9, "staat_pct": 0.0},
    "LV": {"maut_pct": 7.0,  "staat_pct": 0.0},
    "NL": {"maut_pct": 8.9,  "staat_pct": 0.0},
    "PL": {"maut_pct": 10.2, "staat_pct": 2.6},
    "PT": {"maut_pct": 7.7,  "staat_pct": 0.0},
    "RO": {"maut_pct": 7.0,  "staat_pct": 10.6},
    "SE": {"maut_pct": 3.6,  "staat_pct": 0.7},
    "SI": {"maut_pct": 12.5, "staat_pct": 15.3},
    "SK": {"maut_pct": 8.5,  "staat_pct": 5.9},
    "XK": {"maut_pct": 3.4,  "staat_pct": 4.3},
}


# =========================
# FedEx logic (min 68kg per piece, all countries)
# =========================
def fedex_billable_weight_kg(pieces_df: pd.DataFrame) -> float:
    p = pieces_df.copy().fillna(0)
    for col in ["qty", "weight_kg", "cbm"]:
        if col not in p.columns:
            raise KeyError(f"FedEx pieces missing column: {col}")
    p["qty"] = p["qty"].astype(float)
    p["weight_kg"] = p["weight_kg"].astype(float)
    p["cbm"] = p["cbm"].astype(float)

    p["vol_kg"] = p["cbm"] * 200.0
    p["billable_piece"] = p[["weight_kg", "vol_kg"]].max(axis=1)
    # 68kg per piece MIN applies to ALL countries
    p["billable_piece"] = p["billable_piece"].apply(lambda x: max(float(x), 68.0))
    p["billable_total"] = p["billable_piece"] * p["qty"]
    return float(p["billable_total"].sum())


# =========================
# CostBreakdown + cost functions
# =========================
@dataclass
class CostBreakdown:
    carrier: str
    billable_kg: float
    base: float
    maut: float
    staat: float
    fuel: float
    surcharges: float
    total: float
    debug: dict


def cost_table(rate_df: pd.DataFrame, carrier_code: str, carrier_name: str,
               country: str, postal: str, billable_kg: float) -> CostBreakdown:
    c = _norm_country(country)
    p2 = _pc_prefix2(postal)
    key = f"{carrier_code}-{c}--{p2}"
    base = rate_lookup(rate_df, key, billable_kg)
    return CostBreakdown(carrier_name, billable_kg, base, 0.0, 0.0, 0.0, 0.0, base, {"key": key})


def cost_schenker(rate_df: pd.DataFrame, maut: Optional[MautMatrix],
                  country: str, postal: str, gross_kg: float, cbm: float,
                  distance_km: float, floating_pct: float, avis: bool) -> CostBreakdown:
    factor = factor_de150_else200(country)
    billable_kg = billable_max(gross_kg, cbm, factor)

    c = _norm_country(country)
    p2 = _pc_prefix2(postal)
    key = f"SCHENKER-{c}--{p2}"
    base = rate_lookup(rate_df, key, billable_kg)

    maut_cost = schenker_maut_cost(maut, billable_kg, distance_km) if (maut and distance_km > 0) else 0.0
    fuel = base * (floating_pct / 100.0)
    sur = 20.0 if avis else 0.0
    total = base + maut_cost + fuel + sur
    return CostBreakdown("DB Schenker / DSV", billable_kg, base, maut_cost, 0.0, fuel, sur, total, {"key": key, "factor": factor})


def cost_hellmann(rate_df: pd.DataFrame,
                  country: str, postal: str, gross_kg: float, cbm: float,
                  diesel_eur_l: float, b2c: bool, avis: bool, dg: bool, max_side_cm: float) -> CostBreakdown:
    factor = factor_de150_else200(country)
    billable_kg = billable_max(gross_kg, cbm, factor)

    c = _norm_country(country)
    p2 = _pc_prefix2(postal)
    key = f"HELLMANN-{c}--{p2}"
    base = rate_lookup(rate_df, key, billable_kg)

    rule = HELLMANN_RULES_2026.get(c, {"maut_pct": 0.0, "staat_pct": 0.0})
    maut_cost = base * (float(rule.get("maut_pct", 0.0)) / 100.0)
    staat_cost = base * (float(rule.get("staat_pct", 0.0)) / 100.0)
    fuel_pct = hellmann_diesel_pct(diesel_eur_l)
    fuel = base * fuel_pct

    sur = 0.0
    if dg:
        sur += 15.0 if c == "DE" else (75.0 if c in HELLMANN_EU_DG_75 else 30.0)
    if b2c:
        sur += 8.9
    if avis:
        sur += 12.5
    if max_side_cm and max_side_cm > 240.0:
        sur += 30.0

    total = base + maut_cost + staat_cost + fuel + sur
    return CostBreakdown("Hellmann", billable_kg, base, maut_cost, staat_cost, fuel, sur, total,
                         {"key": key, "factor": factor, "diesel_pct": fuel_pct})


def cost_fedex(fedex_df: pd.DataFrame, country: str, pieces_df: pd.DataFrame) -> CostBreakdown:
    c = _norm_country(country)
    df = fedex_df.copy()
    df.columns = [str(x).strip() for x in df.columns]

    # detect country column
    ccol = None
    for cand in ["Land", "LAND", "Country", "COUNTRY", "Destination", "destination"]:
        if cand in df.columns:
            ccol = cand
            break
    if ccol is None:
        ccol = df.columns[0]

    # detect rate column
    rcol = None
    for cand in ["EUR/kg", "€/kg", "EUR_per_kg", "rate", "Rate", "Preis", "Preis/kg"]:
        if cand in df.columns:
            rcol = cand
            break
    if rcol is None:
        for col in df.columns[1:]:
            if pd.api.types.is_numeric_dtype(df[col]):
                rcol = col
                break
    if rcol is None:
        raise ValueError("FedEx file: cannot find €/kg rate column.")

    def _norm(x: str) -> str:
        return re.sub(r"\s+", "", str(x).strip().upper())

    m = df[_norm(df[ccol]) == _norm(c)]
    if m.empty:
        m = df[df[ccol].astype(str).str.upper().str.contains(c, na=False)]
    if m.empty:
        raise KeyError(f"FedEx rate not found for: {c}")

    eur_per_kg = _to_float(m.iloc[0][rcol])
    billable_kg = fedex_billable_weight_kg(pieces_df)
    base = eur_per_kg * billable_kg  # all-in
    return CostBreakdown("FedEx (€/kg all-in)", billable_kg, base, 0.0, 0.0, 0.0, 0.0, base, {"eur_per_kg": eur_per_kg})


# =========================
# Streamlit UI
# =========================
st.set_page_config(page_title="Aifuge Freight Engine V5.4", layout="wide")
st.title("Aifuge GmbH | Freight Cost Engine V5.4")
st.caption("五家同步对比：DHL / Raben / Schenker / Hellmann / FedEx（含FedEx每件最低68kg）")

with st.sidebar:
    st.subheader("📦 上传报价表（优先使用上传覆盖）")
    up_dhl = st.file_uploader("DHL 系统报价表（key+bis-xxx）", type=["xlsx"], key="u_dhl")
    up_raben = st.file_uploader("Raben 系统报价表（key+bis-xxx）", type=["xlsx"], key="u_raben")
    up_sch = st.file_uploader("Schenker 系统报价表（key+bis-xxx）", type=["xlsx"], key="u_sch")
    up_maut = st.file_uploader("Schenker Maut 表（Mauttabelle）", type=["xlsx"], key="u_maut")
    up_hell = st.file_uploader("Hellmann 2026 系统报价表（key+bis-xxx）", type=["xlsx"], key="u_hell")
    up_fedex = st.file_uploader("FedEx €/kg 报价表", type=["xlsx"], key="u_fedex")
    up_raben_rule = st.file_uploader("Raben 规则表（国家->kg/cbm）", type=["xlsx"], key="u_raben_rule")

    st.divider()
    st.subheader("data/ 默认文件名（可不改）")
    dhl_path = st.text_input("DHL", "data/DHL_Frachtkosten.xlsx")
    raben_path = st.text_input("Raben", "data/Raben_Frachtkosten.xlsx")
    sch_path = st.text_input("Schenker", "data/Schenker_Frachtkosten.xlsx")
    maut_path = st.text_input("Maut", "data/Mauttabelle_Schenker.xlsx")
    hell_path = st.text_input("Hellmann", "data/Hellmann_Frachtkosten_2026.xlsx")
    fedex_path = st.text_input("FedEx", "data/FedEx_Frachtkosten.xlsx")
    raben_rule_path = st.text_input("Raben规则表", "data/Raben 立方米及装载米规则.xlsx")

    show_debug = st.checkbox("显示调试信息", value=False)


def _read_bytes(uploaded, fallback_path: str) -> Optional[bytes]:
    if uploaded is not None:
        return uploaded.read()
    try:
        with open(fallback_path, "rb") as f:
            return f.read()
    except Exception:
        return None


errors = []
dhl_df = raben_df = sch_df = hell_df = None
maut_matrix = None
fedex_df = None
raben_rule_df = None

b = _read_bytes(up_dhl, dhl_path)
if b:
    try: dhl_df = load_system_rate_table_from_excel(b)
    except Exception as e: errors.append(f"DHL：{e}")

b = _read_bytes(up_raben, raben_path)
if b:
    try: raben_df = load_system_rate_table_from_excel(b)
    except Exception as e: errors.append(f"Raben：{e}")

b = _read_bytes(up_sch, sch_path)
if b:
    try: sch_df = load_system_rate_table_from_excel(b)
    except Exception as e: errors.append(f"Schenker：{e}")

b = _read_bytes(up_hell, hell_path)
if b:
    try: hell_df = load_system_rate_table_from_excel(b)
    except Exception as e: errors.append(f"Hellmann：{e}")

b = _read_bytes(up_maut, maut_path)
if b:
    try: maut_matrix = load_schenker_maut_matrix(b)
    except Exception as e: errors.append(f"Maut：{e}")

b = _read_bytes(up_fedex, fedex_path)
if b:
    try: fedex_df = pd.read_excel(b)
    except Exception as e: errors.append(f"FedEx：{e}")

b = _read_bytes(up_raben_rule, raben_rule_path)
if b:
    try: raben_rule_df = load_raben_rule_table(b)
    except Exception as e: errors.append(f"Raben规则表：{e}")

if errors:
    st.warning("部分文件加载失败（不影响其它承运商）：\n- " + "\n- ".join(errors))

st.divider()

# Inputs
c1, c2, c3 = st.columns(3)
with c1:
    dest_country = st.text_input("目的国（ISO2）", "DE").upper().strip()
with c2:
    dest_postal = st.text_input("目的邮编", "38112").strip()
with c3:
    diesel_eur_l = st.number_input("Hellmann Diesel €/L", value=1.50, step=0.01)

st.subheader("货物合计（总实重 + 总体积）")
c1, c2, c3 = st.columns(3)
with c1:
    gross_kg = st.number_input("总实重(kg)", min_value=0.0, value=30.0, step=0.5)
with c2:
    cbm = st.number_input("总体积(CBM)", min_value=0.0, value=0.0285, step=0.0005)
with c3:
    max_side_cm = st.number_input("单件最长边(cm)（Hellmann长件）", min_value=0.0, value=0.0, step=1.0)

st.subheader("附加项（只影响指定承运商）")
o1, o2, o3, o4, o5, o6 = st.columns(6)
with o1:
    schenker_km = st.number_input("Schenker 距离KM", min_value=0.0, value=0.0, step=10.0)
with o2:
    schenker_floating_pct = st.number_input("Schenker Floating %", min_value=0.0, value=8.5, step=0.1)
with o3:
    schenker_avis = st.checkbox("Schenker Avis +20€", value=False)
with o4:
    hell_b2c = st.checkbox("Hellmann B2C +8.9€", value=False)
with o5:
    hell_avis = st.checkbox("Hellmann Avis +12.5€", value=False)
with o6:
    hell_dg = st.checkbox("Hellmann DG", value=False)

st.divider()

st.subheader("FedEx（按件最低68kg，所有国家适用）")
use_fedex = st.checkbox("启用 FedEx 对比（需要FedEx表）", value=False)
if "fedex_pieces" not in st.session_state:
    st.session_state["fedex_pieces"] = pd.DataFrame([{"qty": 1, "weight_kg": float(gross_kg), "cbm": float(cbm)}])

btn1, btn2 = st.columns([1, 3])
with btn1:
    if st.button("用合计生成 1件"):
        st.session_state["fedex_pieces"] = pd.DataFrame([{"qty": 1, "weight_kg": float(gross_kg), "cbm": float(cbm)}])
with btn2:
    st.caption("FedEx：每件 max(实重, 体积重(200), 68)*qty；总计费重 * €/kg。")

fedex_pieces = st.data_editor(
    st.session_state["fedex_pieces"],
    use_container_width=True,
    num_rows="dynamic",
    key="fedex_pieces_editor",
)
st.session_state["fedex_pieces"] = fedex_pieces

st.divider()
st.subheader("📊 同步报价对比")

results: List[CostBreakdown] = []

# DHL factor=200
if dhl_df is not None:
    try:
        dhl_bill = billable_max(gross_kg, cbm, 200.0)
        results.append(cost_table(dhl_df, "DHL", "DHL", dest_country, dest_postal, dhl_bill))
    except Exception as e:
        st.error(f"DHL 计算失败：{e}")

# Raben factor from rule table, billable=max
if raben_df is not None:
    try:
        if raben_rule_df is None:
            raise ValueError("Raben 规则表未加载：请上传/放置 Raben 立方米及装载米规则.xlsx")
        rab_factor = raben_factor_for_country(raben_rule_df, dest_country)
        raben_bill = billable_max(gross_kg, cbm, rab_factor)
        results.append(cost_table(raben_df, "RABEN", "Raben", dest_country, dest_postal, raben_bill))
    except Exception as e:
        st.error(f"Raben 计算失败：{e}")

# Schenker
if sch_df is not None:
    try:
        results.append(cost_schenker(sch_df, maut_matrix, dest_country, dest_postal, gross_kg, cbm,
                                    schenker_km, schenker_floating_pct, schenker_avis))
    except Exception as e:
        st.error(f"Schenker 计算失败：{e}")

# Hellmann
if hell_df is not None:
    try:
        results.append(cost_hellmann(hell_df, dest_country, dest_postal, gross_kg, cbm, diesel_eur_l,
                                    hell_b2c, hell_avis, hell_dg, max_side_cm))
    except Exception as e:
        st.error(f"Hellmann 计算失败：{e}")

# FedEx
if use_fedex and fedex_df is not None:
    try:
        results.append(cost_fedex(fedex_df, dest_country, fedex_pieces))
    except Exception as e:
        st.error(f"FedEx 计算失败：{e}")

if not results:
    st.info("请上传至少一个供应商报价表（或放到 data/ 目录并填写正确文件名）。")
else:
    df_out = pd.DataFrame([{
        "Carrier": r.carrier,
        "Billable(kg)": round(r.billable_kg, 2),
        "Base": round(r.base, 2),
        "Maut": round(r.maut, 2),
        "State": round(r.staat, 2),
        "Fuel": round(r.fuel, 2),
        "Surcharges": round(r.surcharges, 2),
        "Total": round(r.total, 2),
    } for r in results]).sort_values("Total", ascending=True)

    st.dataframe(df_out, use_container_width=True)
    best = df_out.iloc[0]
    st.success(f"✅ 当前最低：{best['Carrier']} | Total={best['Total']} EUR | Billable={best['Billable(kg)']} kg")

    if show_debug:
        st.subheader("调试信息")
        for r in results:
            st.markdown(f"**{r.carrier}**")
            st.json(r.debug)
