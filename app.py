# app.py
# Aifuge GmbH | Freight Cost Engine V6.0
# Vendors: DHL Freight, Raben, DSV/DB Schenker, Hellmann, FedEx
# Auto-load rate tables from ./data (no need to upload every time)
#
# Expected files in ./data:
# - DHL_Frachtkosten.xlsx
# - Raben_Frachtkosten.xlsx
# - Raben 立方米及装载米规则.xlsx
# - Schenker_Frachtkosten.xlsx
# - Schenker_Maut.xlsx
# - Hellmann_Frachtkosten_2026.xlsx
# - FedEx_Frachtkosten.xlsx
#
# Notes:
# - Rate matrices: must include a 'key' column + weight bracket columns like 'bis-30','bis-50',... (kg)
# - Keys convention (must match your SYSTEM xlsx):
#     DHL-<CC>-<ZIP2>
#     RABEN-<CC>-<ZIP2>
#     SCHENKER-<CC>-<ZIP2>
#     HELLMANN-<CC>-<ZIP2>
# - FedEx: country-based €/kg table, final cost = €/kg * chargeable_weight
# - Chargeable weight rule: max(actual_kg, volumetric_kg) for all vendors
# - Volumetric factors:
#     DHL: 200 everywhere
#     Raben: factor from rule table (kg/cbm)
#     Schenker: DE=150 else=200
#     Hellmann: DE=150 else=200
#     FedEx: 200 + min 68kg per piece
# - Extras:
#     Schenker: Maut computed from table using (kg, km), optional Avis +20€ (per shipment)
#     Hellmann: Maut % (DE 18.2%), Staatliche Abgaben (DE default 0%), Diesel float % via UI,
#               DG/B2C/Avis/Längen (>240cm) surcharges additive
#     Raben: you can mark "该区域不服务"
#
# V6 adds:
# - Cheapest highlight + recommended vendor
# - Profit/margin calculator (your sell price)
# - Quote logging to ./data/quote_log.csv + history viewer

import os
import re
import math
import json
import time
import datetime as dt
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st

# ---------------------------
# Basic config
# ---------------------------
st.set_page_config(page_title="Aifuge Freight Cost Engine V6", layout="wide")

APP_TITLE = "Aifuge GmbH | Freight Cost Engine V6.0 (Auto-load from data/)"
DATA_DIR = "data"
LOG_PATH = os.path.join(DATA_DIR, "quote_log.csv")

# ---------------------------
# Helpers
# ---------------------------
def _now_iso() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _safe_float(x, default=0.0) -> float:
    try:
        if x is None:
            return default
        if isinstance(x, str):
            x = x.strip().replace(",", ".")
        return float(x)
    except Exception:
        return default

def _round_money(x: float) -> float:
    # keep 2 decimals
    return float(f"{x:.2f}")

def _country_norm(cc: str) -> str:
    cc = (cc or "").strip().upper()
    # Common alias fix (if any)
    if cc in ["UK"]:
        return "GB"
    return cc

def _zip2(zip_code: str) -> str:
    z = re.sub(r"\D", "", str(zip_code or ""))
    return (z[:2] if len(z) >= 2 else z).zfill(2)

def _kg_bracket_cols(df: pd.DataFrame) -> List[Tuple[int, str]]:
    """
    Parse columns like 'bis-30' => upper bound 30.
    Return sorted list of (upper_bound, col_name).
    """
    cols = []
    for c in df.columns:
        m = re.match(r"^\s*bis[-_ ]?(\d+)\s*$", str(c), flags=re.IGNORECASE)
        if m:
            cols.append((int(m.group(1)), c))
    cols.sort(key=lambda t: t[0])
    return cols

def _pick_bracket(brackets: List[Tuple[int, str]], weight_kg: float) -> Optional[str]:
    """
    Pick first bracket where weight <= upper bound.
    """
    if not brackets:
        return None
    w = float(weight_kg)
    for ub, col in brackets:
        if w <= ub + 1e-9:
            return col
    return None  # out of range

def _fmt_eur(x: float) -> str:
    return f"{_round_money(x):.2f}"

def _ensure_dir(path: str) -> None:
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

# ---------------------------
# Cargo model
# ---------------------------
@dataclass
class CargoLine:
    qty: int
    length_cm: float
    width_cm: float
    height_cm: float
    weight_kg: float

    @property
    def cbm_one(self) -> float:
        return (self.length_cm * self.width_cm * self.height_cm) / 1_000_000.0

    @property
    def cbm_total(self) -> float:
        return self.cbm_one * self.qty

    @property
    def weight_total(self) -> float:
        return self.weight_kg * self.qty

    @property
    def max_edge_cm(self) -> float:
        return max(self.length_cm, self.width_cm, self.height_cm)

def cargo_summary(lines: List[CargoLine]) -> Dict[str, float]:
    total_qty = sum(int(l.qty) for l in lines)
    total_cbm = sum(l.cbm_total for l in lines)
    total_kg = sum(l.weight_total for l in lines)
    max_edge = 0.0
    for l in lines:
        if l.qty > 0:
            max_edge = max(max_edge, l.max_edge_cm)
    return {
        "total_qty": float(total_qty),
        "total_cbm": float(total_cbm),
        "total_kg": float(total_kg),
        "max_edge_cm": float(max_edge),
    }

# ---------------------------
# Loaders (auto from data/)
# ---------------------------
@st.cache_data(show_spinner=False)
def load_matrix(path: str) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name=0)
    # normalize column names
    df.columns = [str(c).strip() for c in df.columns]
    if "key" not in df.columns:
        raise KeyError("missing column 'key'")
    df["key"] = df["key"].astype(str).str.strip()
    return df

@st.cache_data(show_spinner=False)
def load_fedex(path: str) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name=0)
    df.columns = [str(c).strip() for c in df.columns]
    # Try to detect columns
    # acceptable: ['country','eur_per_kg'] or ['cc','rate'] etc.
    col_map = {}
    for c in df.columns:
        cl = c.lower()
        if cl in ["country", "land", "cc", "country_code"]:
            col_map[c] = "country"
        if cl in ["eur_per_kg", "€/kg", "euro_per_kg", "rate", "preis_pro_kg", "price_per_kg"]:
            col_map[c] = "eur_per_kg"
    df = df.rename(columns=col_map)
    if "country" not in df.columns or "eur_per_kg" not in df.columns:
        # last resort: first 2 columns
        if len(df.columns) >= 2:
            df = df.rename(columns={df.columns[0]: "country", df.columns[1]: "eur_per_kg"})
    df["country"] = df["country"].astype(str).str.strip().str.upper().map(_country_norm)
    df["eur_per_kg"] = df["eur_per_kg"].apply(_safe_float)
    return df

@st.cache_data(show_spinner=False)
def load_raben_factor_rules(path: str) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name=0)
    df.columns = [str(c).strip() for c in df.columns]
    # Try detect factor column (kg/cbm)
    # Common names: factor_kg_per_cbm, kg_per_cbm, volumetric_factor
    col_map = {}
    for c in df.columns:
        cl = c.lower()
        if cl in ["country", "land", "cc", "country_code"]:
            col_map[c] = "country"
        if "kg" in cl and ("cbm" in cl or "m3" in cl):
            col_map[c] = "kg_per_cbm"
        if cl in ["volumetric_factor", "factor", "kg_per_cbm"]:
            col_map[c] = "kg_per_cbm"
    df = df.rename(columns=col_map)
    if "country" not in df.columns or "kg_per_cbm" not in df.columns:
        # last resort: first 2 columns
        if len(df.columns) >= 2:
            df = df.rename(columns={df.columns[0]: "country", df.columns[1]: "kg_per_cbm"})
    df["country"] = df["country"].astype(str).str.strip().str.upper().map(_country_norm)
    df["kg_per_cbm"] = df["kg_per_cbm"].apply(_safe_float)
    return df

@st.cache_data(show_spinner=False)
def load_schenker_maut(path: str) -> pd.DataFrame:
    # We accept any layout, but prefer "long table":
    # columns: weight_from, weight_to, km_from, km_to, maut_eur
    df = pd.read_excel(path, sheet_name=0)
    df.columns = [str(c).strip() for c in df.columns]
    # normalize likely columns
    rename = {}
    for c in df.columns:
        cl = c.lower()
        if "weight" in cl and ("from" in cl or "von" in cl):
            rename[c] = "weight_from"
        elif "weight" in cl and ("to" in cl or "bis" in cl):
            rename[c] = "weight_to"
        elif "km" in cl and ("from" in cl or "von" in cl):
            rename[c] = "km_from"
        elif "km" in cl and ("to" in cl or "bis" in cl):
            rename[c] = "km_to"
        elif "maut" in cl and ("eur" in cl or "betrag" in cl or "value" in cl):
            rename[c] = "maut_eur"
        elif cl in ["maut", "maut_eur"]:
            rename[c] = "maut_eur"
    df = df.rename(columns=rename)

    # If already long-form, good.
    if {"weight_from", "weight_to", "km_from", "km_to", "maut_eur"}.issubset(set(df.columns)):
        for c in ["weight_from", "weight_to", "km_from", "km_to", "maut_eur"]:
            df[c] = df[c].apply(_safe_float)
        return df

    # Otherwise: maybe matrix with weight rows and km columns.
    # We'll try to detect:
    # - first column as weight upper bound (bis-xxx) or 'Gewicht bis'
    # - other columns numeric km upper bounds
    # Convert to long-form.
    df2 = df.copy()
    if df2.shape[1] >= 2:
        first = df2.columns[0]
        w_vals = df2[first].tolist()
        km_cols = df2.columns[1:]
        long_rows = []
        # parse weight upper bounds from values/strings
        for i, w in enumerate(w_vals):
            w_to = None
            if isinstance(w, (int, float)):
                w_to = float(w)
            else:
                m = re.search(r"(\d+(?:[.,]\d+)?)", str(w))
                if m:
                    w_to = _safe_float(m.group(1))
            if not w_to:
                continue
            w_from = 0.0
            if i > 0:
                prev = w_vals[i - 1]
                prev_to = None
                if isinstance(prev, (int, float)):
                    prev_to = float(prev)
                else:
                    m2 = re.search(r"(\d+(?:[.,]\d+)?)", str(prev))
                    if m2:
                        prev_to = _safe_float(m2.group(1))
                if prev_to is not None:
                    w_from = float(prev_to)
            for c in km_cols:
                # parse km_to
                km_to = None
                if isinstance(c, (int, float)):
                    km_to = float(c)
                else:
                    m3 = re.search(r"(\d+(?:[.,]\d+)?)", str(c))
                    if m3:
                        km_to = _safe_float(m3.group(1))
                if not km_to:
                    continue
                v = _safe_float(df2.loc[i, c], default=None)
                if v is None:
                    continue
                long_rows.append(
                    {"weight_from": w_from, "weight_to": w_to, "km_from": 0.0, "km_to": km_to, "maut_eur": v}
                )
        if long_rows:
            out = pd.DataFrame(long_rows)
            return out

    raise ValueError("Unsupported Schenker Maut table layout. Please export long-form (weight/km ranges).")

# ---------------------------
# Rate lookup (matrix)
# ---------------------------
def matrix_price(df: pd.DataFrame, key: str, chargeable_kg: float) -> Tuple[Optional[float], str]:
    """
    Return (price, note). Price is the base freight cost from matrix.
    """
    key = str(key).strip()
    sub = df[df["key"] == key]
    if sub.empty:
        return None, f"未找到线路 key={key}"
    row = sub.iloc[0]
    brackets = _kg_bracket_cols(df)
    col = _pick_bracket(brackets, chargeable_kg)
    if col is None:
        return None, f"超出报价范围(>max bis): kg={chargeable_kg:.2f}"
    val = _safe_float(row[col], default=None)
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return None, f"报价单元为空: {col}"
    return float(val), f"{col}"

# ---------------------------
# Vendor charging weight
# ---------------------------
def volumetric_kg(total_cbm: float, factor_kg_per_cbm: float) -> float:
    return float(total_cbm) * float(factor_kg_per_cbm)

def fedex_chargeable_kg(lines: List[CargoLine], factor: float = 200.0, min_piece_kg: float = 68.0) -> float:
    # FedEx min 68kg per piece applies on chargeable per piece (max(actual, vol, min))
    total = 0.0
    for l in lines:
        if l.qty <= 0:
            continue
        cbm_one = l.cbm_one
        vol_one = cbm_one * factor
        one_charge = max(float(l.weight_kg), float(vol_one), float(min_piece_kg))
        total += one_charge * int(l.qty)
    return total

# ---------------------------
# Schenker Maut calculation
# ---------------------------
def calc_schenker_maut(maut_df_long: pd.DataFrame, weight_kg: float, km: float) -> Optional[float]:
    """
    Use long-form maut table:
    weight_from <= w <= weight_to AND km_from <= km <= km_to then pick maut_eur.
    If multiple matches, choose the tightest (min ranges).
    """
    w = float(weight_kg)
    d = float(km)
    df = maut_df_long.copy()

    # Ensure numeric
    for c in ["weight_from", "weight_to", "km_from", "km_to", "maut_eur"]:
        df[c] = df[c].apply(_safe_float)

    m = df[(df["weight_from"] <= w) & (w <= df["weight_to"]) & (df["km_from"] <= d) & (d <= df["km_to"])]
    if m.empty:
        return None
    # choose minimal (weight_to-weight_from, km_to-km_from)
    m = m.assign(_wr=(m["weight_to"] - m["weight_from"]).abs(), _dr=(m["km_to"] - m["km_from"]).abs())
    m = m.sort_values(["_wr", "_dr"], ascending=True)
    return float(m.iloc[0]["maut_eur"])

# ---------------------------
# Hellmann surcharges rules (V5 -> V6 production dict)
# ---------------------------
# IMPORTANT: you asked "一次性整理成完整字典"——这里直接落地为生产级字典（Maut% / Staatl% / DG规则组）
HELLMANN_RULES_2026: Dict[str, Dict[str, object]] = {
    # DE special (you confirmed Staatliche Abgaben = 0% on invoices)
    "DE": {"maut_pct": 0.182, "staatl_pct": 0.0, "vol_factor": 150, "dg_group": "EU30"},
    # EU/EEA + neighbors from your screenshots (Maut & Staatliche Abgaben by country)
    "AT": {"maut_pct": 0.133, "staatl_pct": 0.066, "vol_factor": 200, "dg_group": "EU30"},
    "BE": {"maut_pct": 0.097, "staatl_pct": 0.021, "vol_factor": 200, "dg_group": "EU30"},
    "BG": {"maut_pct": 0.062, "staatl_pct": 0.099, "vol_factor": 200, "dg_group": "EU30"},
    "CZ": {"maut_pct": 0.086, "staatl_pct": 0.054, "vol_factor": 200, "dg_group": "EU30"},
    "DK": {"maut_pct": 0.086, "staatl_pct": 0.001, "vol_factor": 200, "dg_group": "EU30"},
    "EE": {"maut_pct": 0.072, "staatl_pct": 0.0, "vol_factor": 200, "dg_group": "EU30"},
    "ES": {"maut_pct": 0.067, "staatl_pct": 0.0, "vol_factor": 200, "dg_group": "EU30"},
    "FI": {"maut_pct": 0.048, "staatl_pct": 0.031, "vol_factor": 200, "dg_group": "EU75"},
    "FR": {"maut_pct": 0.077, "staatl_pct": 0.005, "vol_factor": 200, "dg_group": "EU30"},
    "GR": {"maut_pct": 0.078, "staatl_pct": 0.10, "vol_factor": 200, "dg_group": "EU75"},
    "HR": {"maut_pct": 0.091, "staatl_pct": 0.116, "vol_factor": 200, "dg_group": "EU30"},
    "HU": {"maut_pct": 0.115, "staatl_pct": 0.152, "vol_factor": 200, "dg_group": "EU30"},
    "IE": {"maut_pct": 0.061, "staatl_pct": 0.036, "vol_factor": 200, "dg_group": "EU75"},
    "IT": {"maut_pct": 0.103, "staatl_pct": 0.07, "vol_factor": 200, "dg_group": "EU30"},
    "LT": {"maut_pct": 0.076, "staatl_pct": 0.0, "vol_factor": 200, "dg_group": "EU30"},
    "LU": {"maut_pct": 0.109, "staatl_pct": 0.0, "vol_factor": 200, "dg_group": "EU30"},
    "LV": {"maut_pct": 0.07, "staatl_pct": 0.0, "vol_factor": 200, "dg_group": "EU30"},
    "NL": {"maut_pct": 0.089, "staatl_pct": 0.0, "vol_factor": 200, "dg_group": "EU30"},
    "PL": {"maut_pct": 0.102, "staatl_pct": 0.026, "vol_factor": 200, "dg_group": "EU30"},
    "PT": {"maut_pct": 0.077, "staatl_pct": 0.0, "vol_factor": 200, "dg_group": "EU30"},
    "RO": {"maut_pct": 0.07, "staatl_pct": 0.106, "vol_factor": 200, "dg_group": "EU30"},
    "SE": {"maut_pct": 0.036, "staatl_pct": 0.007, "vol_factor": 200, "dg_group": "EU75"},
    "SI": {"maut_pct": 0.125, "staatl_pct": 0.153, "vol_factor": 200, "dg_group": "EU30"},
    "SK": {"maut_pct": 0.085, "staatl_pct": 0.059, "vol_factor": 200, "dg_group": "EU30"},
    "XK": {"maut_pct": 0.034, "staatl_pct": 0.043, "vol_factor": 200, "dg_group": "EU30"},
    # If you later add more countries, just extend here.
}

HELLMANN_SURCHARGES = {
    "DE": {"DG": 15.0, "B2C": 8.9, "AVIS": 12.5, "LEN": 30.0},
    "INT": {"DG_EU30": 30.0, "DG_EU75": 75.0, "B2C": 8.9, "AVIS": 12.5, "LEN": 30.0},
}

# ---------------------------
# UI: header + load data
# ---------------------------
st.title(APP_TITLE)

with st.sidebar:
    st.header("📦 自动加载报价表（来自 data/）")
    st.caption("若文件缺失/格式不对，会在页面上方提示。")
    st.caption("你现在无需每次手动上传，直接替换 data/ 里的xlsx即可生效。")
    st.divider()
    st.subheader("路径检查")
    st.write(f"DATA_DIR = `{DATA_DIR}/`")
    if os.path.exists(DATA_DIR):
        try:
            files = sorted(os.listdir(DATA_DIR))
            st.write("已检测到：")
            st.code("\n".join(files) if files else "(空)")
        except Exception as e:
            st.error(f"无法读取 data/: {e}")
    else:
        st.error("未找到 data/ 文件夹（请在仓库根目录创建 data/ 并上传xlsx）")

# File paths
DHL_PATH = os.path.join(DATA_DIR, "DHL_Frachtkosten.xlsx")
RABEN_PATH = os.path.join(DATA_DIR, "Raben_Frachtkosten.xlsx")
RABEN_RULE_PATH = os.path.join(DATA_DIR, "Raben 立方米及装载米规则.xlsx")
SCHENKER_PATH = os.path.join(DATA_DIR, "Schenker_Frachtkosten.xlsx")
SCHENKER_MAUT_PATH = os.path.join(DATA_DIR, "Schenker_Maut.xlsx")
HELLMANN_PATH = os.path.join(DATA_DIR, "Hellmann_Frachtkosten_2026.xlsx")
FEDEX_PATH = os.path.join(DATA_DIR, "FedEx_Frachtkosten.xlsx")

load_errors = []

def _try_load(label: str, fn, path: str):
    if not os.path.exists(path):
        load_errors.append(f"{label} 文件不存在: {path}")
        return None
    try:
        return fn(path)
    except Exception as e:
        load_errors.append(f"{label} 读取失败: {e}")
        return None

dhl_df = _try_load("DHL", load_matrix, DHL_PATH)
raben_df = _try_load("Raben", load_matrix, RABEN_PATH)
raben_rule_df = _try_load("Raben规则表", load_raben_factor_rules, RABEN_RULE_PATH)
schenker_df = _try_load("Schenker", load_matrix, SCHENKER_PATH)
schenker_maut_df = _try_load("Schenker Maut", load_schenker_maut, SCHENKER_MAUT_PATH)
hellmann_df = _try_load("Hellmann", load_matrix, HELLMANN_PATH)
fedex_df = _try_load("FedEx", load_fedex, FEDEX_PATH)

if load_errors:
    with st.expander("⚠️ 报价表加载提示（点击展开查看）", expanded=True):
        for e in load_errors:
            st.warning(e)

st.divider()

# ---------------------------
# UI: inputs
# ---------------------------
colA, colB, colC = st.columns([2, 2, 2])

with colA:
    dest_country = _country_norm(st.selectbox("目的国 Country (ISO2)", options=sorted(list(set(
        ["DE","AT","BE","BG","CZ","DK","EE","ES","FI","FR","GR","HR","HU","IE","IT","LU","LV","LT","NL","PL","PT","RO","SE","SI","SK","XK"]
    ))), index=1 if "AT" in sorted(list(set(
        ["DE","AT","BE","BG","CZ","DK","EE","ES","FI","FR","GR","HR","HU","IE","IT","LU","LV","LT","NL","PL","PT","RO","SE","SI","SK","XK"]
    ))) else 0))
    dest_zip = st.text_input("收件邮编（用于邮编前两位分区）", value="10")
    zip2 = _zip2(dest_zip)
    st.caption(f"邮编前两位 = **{zip2}**")

with colB:
    st.markdown("### 货物明细（逐件）")
    st.caption("规则：各家取 max(实重, 体积重)。FedEx 还会对每件 적용最低68kg。")
    # Default 1 line
    default_rows = 1
    rows = st.number_input("行数（件型）", min_value=1, max_value=50, value=default_rows, step=1)

with colC:
    st.markdown("### 对客户报价（用于利润测算）")
    sell_price = st.number_input("你对客户的报价（EUR，可为空=0）", min_value=0.0, value=0.0, step=10.0)
    st.caption("V6：会自动算每家毛利 & 毛利率，并推荐最低成本承运商。")

# Build editable table for cargo lines
cargo_table = []
for i in range(int(rows)):
    cargo_table.append({"qty": 1, "L(cm)": 60.0, "W(cm)": 40.0, "H(cm)": 40.0, "weight_kg": 20.0})

cargo_df_in = st.data_editor(
    pd.DataFrame(cargo_table),
    use_container_width=True,
    key="cargo_editor",
    num_rows="fixed",
    column_config={
        "qty": st.column_config.NumberColumn("数量 qty", min_value=1, step=1),
        "L(cm)": st.column_config.NumberColumn("长 L(cm)", min_value=0.0, step=1.0),
        "W(cm)": st.column_config.NumberColumn("宽 W(cm)", min_value=0.0, step=1.0),
        "H(cm)": st.column_config.NumberColumn("高 H(cm)", min_value=0.0, step=1.0),
        "weight_kg": st.column_config.NumberColumn("实重(kg)/件", min_value=0.0, step=0.1),
    },
)

lines: List[CargoLine] = []
for _, r in cargo_df_in.iterrows():
    lines.append(CargoLine(
        qty=int(_safe_float(r.get("qty"), 1)),
        length_cm=_safe_float(r.get("L(cm)"), 0),
        width_cm=_safe_float(r.get("W(cm)"), 0),
        height_cm=_safe_float(r.get("H(cm)"), 0),
        weight_kg=_safe_float(r.get("weight_kg"), 0),
    ))

s = cargo_summary(lines)
st.markdown("### 货物汇总")
c1, c2, c3, c4 = st.columns(4)
with c1:
    st.metric("总件数", int(s["total_qty"]))
with c2:
    st.metric("总实重(kg)", _round_money(s["total_kg"]))
with c3:
    st.metric("总体积(m³)", _round_money(s["total_cbm"]))
with c4:
    st.metric("单件最长边(cm)", _round_money(s["max_edge_cm"]))

st.divider()

# ---------------------------
# UI: extras / parameters
# ---------------------------
st.markdown("### 附加费参数（可选）")

p1, p2, p3, p4, p5 = st.columns([1.2, 1.2, 1.2, 1.2, 1.2])

with p1:
    st.markdown("**Schenker / DSV**")
    sch_km = st.number_input("Schenker 距离 KM（手动）", min_value=0.0, value=0.0, step=1.0)
    sch_floating_pct = st.number_input("Schenker Floating(%)（手动）", min_value=0.0, value=0.0, step=0.1)
    sch_avis = st.checkbox("Schenker Avis 电话预约 (+20€)", value=False)

with p2:
    st.markdown("**Hellmann**")
    hell_diesel_pct = st.number_input("Hellmann Diesel浮动(%)（手动）", min_value=0.0, value=0.0, step=0.1)
    hell_b2c = st.checkbox("Hellmann B2C (+8.9€)", value=False)
    hell_avis = st.checkbox("Hellmann Avis 电话预约 (+12.5€)", value=False)

with p3:
    st.markdown("**Hellmann DG / 长度费**")
    hell_dg = st.checkbox("Hellmann 危险品DG（叠加）", value=False)
    hell_len = st.checkbox("单件最长边 >240cm（长度费 +30€）", value=(s["max_edge_cm"] > 240.0))

with p4:
    st.markdown("**Raben**")
    raben_no_service = st.checkbox("Raben该区域不服务（直接不报价）", value=False)

with p5:
    st.markdown("**日志**")
    auto_log = st.checkbox("每次计算自动写入日志", value=False)
    st.caption("日志路径：data/quote_log.csv")

st.divider()

# ---------------------------
# Compute all vendors
# ---------------------------
def compute_all():
    notes = []
    results = []

    cc = dest_country
    z2 = zip2

    # --- chargeable weights
    # DHL
    dhl_factor = 200.0
    dhl_vol = volumetric_kg(s["total_cbm"], dhl_factor)
    dhl_charge = max(s["total_kg"], dhl_vol)

    # Raben
    raben_factor = None
    if raben_rule_df is not None:
        m = raben_rule_df[raben_rule_df["country"] == cc]
        if not m.empty:
            raben_factor = float(m.iloc[0]["kg_per_cbm"])
    if raben_factor is None or raben_factor <= 0:
        # fallback: assume 200 (but mark note)
        raben_factor = 200.0
        notes.append(f"Raben 规则表未找到国家 {cc}，临时用200 kg/cbm")
    raben_vol = volumetric_kg(s["total_cbm"], raben_factor)
    raben_charge = max(s["total_kg"], raben_vol)

    # Schenker
    sch_factor = 150.0 if cc == "DE" else 200.0
    sch_vol = volumetric_kg(s["total_cbm"], sch_factor)
    sch_charge = max(s["total_kg"], sch_vol)

    # Hellmann
    hell_factor = 150.0 if cc == "DE" else 200.0
    hell_vol = volumetric_kg(s["total_cbm"], hell_factor)
    hell_charge = max(s["total_kg"], hell_vol)

    # FedEx (per piece min 68)
    fed_charge = fedex_chargeable_kg(lines, factor=200.0, min_piece_kg=68.0)

    # --- DHL
    if dhl_df is not None:
        key = f"DHL-{cc}-{z2}"
        base, note = matrix_price(dhl_df, key, dhl_charge)
        if base is None:
            results.append({"carrier": "DHL", "ok": False, "key": key, "bracket": "-", "base": None, "extras": None, "total": None, "note": note})
        else:
            # DHL table assumed "all-in base" (your system already uses discounted tables)
            total = float(base)
            results.append({"carrier": "DHL", "ok": True, "key": key, "bracket": note, "base": base, "extras": 0.0, "total": total, "note": ""})
    else:
        results.append({"carrier": "DHL", "ok": False, "key": "-", "bracket": "-", "base": None, "extras": None, "total": None, "note": "DHL表未加载"})

    # --- Raben
    if raben_no_service:
        results.append({"carrier": "Raben", "ok": False, "key": f"RABEN-{cc}-{z2}", "bracket": "-", "base": None, "extras": None, "total": None, "note": "该区域不服务"})
    else:
        if raben_df is not None:
            key = f"RABEN-{cc}-{z2}"
            base, note = matrix_price(raben_df, key, raben_charge)
            if base is None:
                results.append({"carrier": "Raben", "ok": False, "key": key, "bracket": "-", "base": None, "extras": None, "total": None, "note": note})
            else:
                total = float(base)
                results.append({"carrier": "Raben", "ok": True, "key": key, "bracket": note, "base": base, "extras": 0.0, "total": total, "note": f"factor={_round_money(raben_factor)} kg/cbm"})
        else:
            results.append({"carrier": "Raben", "ok": False, "key": "-", "bracket": "-", "base": None, "extras": None, "total": None, "note": "Raben表未加载"})

    # --- Schenker (base + maut + floating + avis)
    if schenker_df is not None:
        key = f"SCHENKER-{cc}-{z2}"
        base, note = matrix_price(schenker_df, key, sch_charge)
        if base is None:
            results.append({"carrier": "Schenker", "ok": False, "key": key, "bracket": "-", "base": None, "extras": None, "total": None, "note": note})
        else:
            extras = 0.0
            # Maut by table if possible
            maut_val = None
            if sch_km > 0 and schenker_maut_df is not None:
                maut_val = calc_schenker_maut(schenker_maut_df, sch_charge, sch_km)
            if maut_val is not None:
                extras += float(maut_val)
            elif sch_km > 0:
                notes.append("Schenker Maut：未从表中匹配到（请检查Maut表格式/区间）")

            # Floating: percent on freight base (as in your invoices)
            if sch_floating_pct > 0:
                extras += float(base) * (float(sch_floating_pct) / 100.0)

            # Avis
            if sch_avis:
                extras += 20.0

            total = float(base) + extras
            results.append({"carrier": "Schenker", "ok": True, "key": key, "bracket": note, "base": base, "extras": extras, "total": total,
                            "note": f"vol_factor={sch_factor}; maut={'-' if maut_val is None else _fmt_eur(maut_val)}"})
    else:
        results.append({"carrier": "Schenker", "ok": False, "key": "-", "bracket": "-", "base": None, "extras": None, "total": None, "note": "Schenker表未加载"})

    # --- Hellmann (base + maut% + staatl% + diesel% + DG/B2C/Avis/Len)
    if hellmann_df is not None:
        key = f"HELLMANN-{cc}-{z2}"
        base, note = matrix_price(hellmann_df, key, hell_charge)
        if base is None:
            results.append({"carrier": "Hellmann", "ok": False, "key": key, "bracket": "-", "base": None, "extras": None, "total": None, "note": note})
        else:
            extras = 0.0
            rule = HELLMANN_RULES_2026.get(cc, {"maut_pct": 0.0, "staatl_pct": 0.0, "vol_factor": (150 if cc == "DE" else 200), "dg_group": "EU30"})
            maut_pct = float(rule.get("maut_pct", 0.0))
            staatl_pct = float(rule.get("staatl_pct", 0.0))

            # percent on freight cost (as per your terms)
            if maut_pct > 0:
                extras += float(base) * maut_pct
            if staatl_pct > 0:
                extras += float(base) * staatl_pct

            # diesel floating percent on freight cost
            if hell_diesel_pct > 0:
                extras += float(base) * (float(hell_diesel_pct) / 100.0)

            # DG additive
            if hell_dg:
                if cc == "DE":
                    extras += HELLMANN_SURCHARGES["DE"]["DG"]
                else:
                    grp = str(rule.get("dg_group", "EU30"))
                    extras += HELLMANN_SURCHARGES["INT"]["DG_EU75"] if grp == "EU75" else HELLMANN_SURCHARGES["INT"]["DG_EU30"]

            # B2C additive
            if hell_b2c:
                extras += HELLMANN_SURCHARGES["DE"]["B2C"] if cc == "DE" else HELLMANN_SURCHARGES["INT"]["B2C"]

            # Avis additive
            if hell_avis:
                extras += HELLMANN_SURCHARGES["DE"]["AVIS"] if cc == "DE" else HELLMANN_SURCHARGES["INT"]["AVIS"]

            # Length surcharge additive (single piece max edge > 240cm)
            if hell_len:
                extras += HELLMANN_SURCHARGES["DE"]["LEN"] if cc == "DE" else HELLMANN_SURCHARGES["INT"]["LEN"]

            total = float(base) + extras
            results.append({
                "carrier": "Hellmann", "ok": True, "key": key, "bracket": note, "base": base, "extras": extras, "total": total,
                "note": f"maut={maut_pct*100:.1f}%; staatl={staatl_pct*100:.1f}%; vol_factor={hell_factor}"
            })
    else:
        results.append({"carrier": "Hellmann", "ok": False, "key": "-", "bracket": "-", "base": None, "extras": None, "total": None, "note": "Hellmann表未加载"})

    # --- FedEx (€/kg all-in, min 68kg/pcs already in chargeable_kg)
    if fedex_df is not None:
        m = fedex_df[fedex_df["country"] == cc]
        if m.empty:
            results.append({"carrier": "FedEx", "ok": False, "key": f"FEDEX-{cc}", "bracket": "€/kg", "base": None, "extras": None, "total": None, "note": f"FedEx 未找到国家 {cc} 的 €/kg"})
        else:
            eur_per_kg = float(m.iloc[0]["eur_per_kg"])
            base = eur_per_kg * fed_charge
            total = base
            results.append({"carrier": "FedEx", "ok": True, "key": f"FEDEX-{cc}", "bracket": "€/kg", "base": base, "extras": 0.0, "total": total,
                            "note": f"rate={eur_per_kg:.4f} €/kg; chargeable={_round_money(fed_charge)}kg(含每件≥68kg)"})
    else:
        results.append({"carrier": "FedEx", "ok": False, "key": "-", "bracket": "-", "base": None, "extras": None, "total": None, "note": "FedEx表未加载"})

    # append global notes
    if notes:
        results.append({"carrier": "备注", "ok": True, "key": "-", "bracket": "-", "base": None, "extras": None, "total": None, "note": " | ".join(notes)})

    return results, {
        "dhl_charge": dhl_charge,
        "raben_charge": raben_charge,
        "schenker_charge": sch_charge,
        "hellmann_charge": hell_charge,
        "fedex_charge": fed_charge,
        "raben_factor": raben_factor,
        "sch_factor": sch_factor,
        "hell_factor": hell_factor,
    }

results, weights_dbg = compute_all()

# ---------------------------
# Results table + cheapest highlight + recommendation
# ---------------------------
st.markdown("## 🧾 五家同步报价对比（V6）")

df_out = pd.DataFrame(results)

# Profit calc columns
if sell_price and sell_price > 0:
    def _profit(row):
        if row.get("ok") and isinstance(row.get("total"), (int, float)) and row.get("total") is not None:
            return float(sell_price) - float(row["total"])
        return None

    def _margin(row):
        p = _profit(row)
        if p is None or sell_price <= 0:
            return None
        return p / float(sell_price)

    df_out["profit"] = df_out.apply(_profit, axis=1)
    df_out["margin"] = df_out.apply(_margin, axis=1)

# Find cheapest among ok carriers (exclude 备注)
candidates = df_out[(df_out["carrier"] != "备注") & (df_out["ok"] == True) & (df_out["total"].notna())]
cheapest_carrier = None
cheapest_cost = None
if not candidates.empty:
    idx = candidates["total"].astype(float).idxmin()
    cheapest_carrier = df_out.loc[idx, "carrier"]
    cheapest_cost = float(df_out.loc[idx, "total"])

def highlight_cheapest(row):
    if cheapest_carrier is None:
        return [""] * len(row)
    if row["carrier"] == cheapest_carrier:
        return ["background-color: #d9fdd3"] * len(row)  # light green
    return [""] * len(row)

show_cols = ["carrier", "ok", "key", "bracket", "base", "extras", "total", "note"]
if "profit" in df_out.columns:
    show_cols += ["profit", "margin"]

df_show = df_out.copy()

# formatting
def _fmt_val(x):
    if x is None:
        return ""
    if isinstance(x, float) and math.isnan(x):
        return ""
    return x

for col in ["base", "extras", "total", "profit"]:
    if col in df_show.columns:
        df_show[col] = df_show[col].apply(lambda v: "" if v is None or (isinstance(v, float) and math.isnan(v)) else _fmt_eur(float(v)))
if "margin" in df_show.columns:
    df_show["margin"] = df_show["margin"].apply(lambda v: "" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{float(v)*100:.1f}%")

st.dataframe(
    df_show[show_cols].style.apply(highlight_cheapest, axis=1),
    use_container_width=True,
    height=320,
)

if cheapest_carrier:
    st.success(f"✅ 推荐承运商：**{cheapest_carrier}**（当前最低成本：**{_fmt_eur(cheapest_cost)} EUR**）")
else:
    st.warning("当前没有可用报价（请检查 key / 国家 / 邮编分区 / 或勾选了不服务）。")

# Debug weights
with st.expander("🔧 计费重自检（展开查看）", expanded=False):
    st.write({
        "total_actual_kg": _round_money(s["total_kg"]),
        "total_cbm": _round_money(s["total_cbm"]),
        "DHL_chargeable_kg": _round_money(weights_dbg["dhl_charge"]),
        "Raben_chargeable_kg": _round_money(weights_dbg["raben_charge"]),
        "Schenker_chargeable_kg": _round_money(weights_dbg["schenker_charge"]),
        "Hellmann_chargeable_kg": _round_money(weights_dbg["hellmann_charge"]),
        "FedEx_chargeable_kg": _round_money(weights_dbg["fedex_charge"]),
        "Raben_factor_kg_per_cbm": _round_money(weights_dbg["raben_factor"]),
        "Schenker_vol_factor": weights_dbg["sch_factor"],
        "Hellmann_vol_factor": weights_dbg["hell_factor"],
    })

st.divider()

# ---------------------------
# Logging (V6)
# ---------------------------
def log_quote(df_results: pd.DataFrame):
    _ensure_dir(LOG_PATH)

    # Only real carriers
    base_rows = df_results[(df_results["carrier"] != "备注")].copy()
    # pack a single record (wide) for quick analysis
    record = {
        "ts": _now_iso(),
        "country": dest_country,
        "zip2": zip2,
        "sell_price": float(sell_price),
        "total_actual_kg": float(s["total_kg"]),
        "total_cbm": float(s["total_cbm"]),
        "max_edge_cm": float(s["max_edge_cm"]),
        "dhl_chargeable_kg": float(weights_dbg["dhl_charge"]),
        "raben_chargeable_kg": float(weights_dbg["raben_charge"]),
        "schenker_chargeable_kg": float(weights_dbg["schenker_charge"]),
        "hellmann_chargeable_kg": float(weights_dbg["hellmann_charge"]),
        "fedex_chargeable_kg": float(weights_dbg["fedex_charge"]),
        "schenker_km": float(sch_km),
        "schenker_floating_pct": float(sch_floating_pct),
        "schenker_avis": bool(sch_avis),
        "hellmann_diesel_pct": float(hell_diesel_pct),
        "hellmann_b2c": bool(hell_b2c),
        "hellmann_avis": bool(hell_avis),
        "hellmann_dg": bool(hell_dg),
        "hellmann_len": bool(hell_len),
        "raben_no_service": bool(raben_no_service),
        "recommended": str(cheapest_carrier or ""),
        "recommended_cost": float(cheapest_cost) if cheapest_cost is not None else None,
    }

    # add each carrier total
    for carrier in ["DHL", "Raben", "Schenker", "Hellmann", "FedEx"]:
        r = base_rows[base_rows["carrier"] == carrier]
        if r.empty or not bool(r.iloc[0]["ok"]) or r.iloc[0]["total"] is None or (isinstance(r.iloc[0]["total"], float) and math.isnan(r.iloc[0]["total"])):
            record[f"{carrier}_ok"] = False
            record[f"{carrier}_total"] = None
            record[f"{carrier}_note"] = str(r.iloc[0]["note"]) if not r.empty else "missing"
        else:
            record[f"{carrier}_ok"] = True
            record[f"{carrier}_total"] = float(r.iloc[0]["total"])
            record[f"{carrier}_note"] = str(r.iloc[0]["note"])

    # write/append
    rec_df = pd.DataFrame([record])
    if os.path.exists(LOG_PATH):
        old = pd.read_csv(LOG_PATH)
        out = pd.concat([old, rec_df], ignore_index=True)
        out.to_csv(LOG_PATH, index=False)
    else:
        rec_df.to_csv(LOG_PATH, index=False)

c_log1, c_log2 = st.columns([1, 2])
with c_log1:
    if st.button("📝 保存本次比价到日志", type="primary"):
        try:
            log_quote(pd.DataFrame(results))
            st.success(f"已写入日志：{LOG_PATH}")
        except Exception as e:
            st.error(f"写日志失败：{e}")

with c_log2:
    st.caption("你可以把 quote_log.csv 下载走做BI分析；也可以后续接数据库（PostgreSQL / BigQuery）。")

if auto_log:
    try:
        # avoid spam: only write when user changes inputs -> streamlit rerun.
        # We'll do a tiny debounce using session_state timestamp.
        last_ts = st.session_state.get("_last_autolog_ts", 0.0)
        if time.time() - float(last_ts) > 3.0:
            log_quote(pd.DataFrame(results))
            st.session_state["_last_autolog_ts"] = time.time()
            st.toast("已自动写入日志", icon="🧾")
    except Exception as e:
        st.warning(f"自动写日志失败：{e}")

# History viewer
st.markdown("## 📚 历史比价记录（最近100条）")
if os.path.exists(LOG_PATH):
    try:
        hist = pd.read_csv(LOG_PATH)
        hist_tail = hist.tail(100).copy()
        # show key columns
        cols = ["ts","country","zip2","total_actual_kg","total_cbm","sell_price","recommended","recommended_cost",
                "DHL_total","Raben_total","Schenker_total","Hellmann_total","FedEx_total"]
        cols = [c for c in cols if c in hist_tail.columns]
        st.dataframe(hist_tail[cols], use_container_width=True, height=260)
    except Exception as e:
        st.error(f"读取日志失败：{e}")
else:
    st.info("暂无日志。点击“保存本次比价到日志”即可开始积累。")

st.divider()

# ---------------------------
# Download current compare table (simple)
# ---------------------------
st.markdown("## ⬇️ 导出当前对比结果（Excel）")

def to_excel_bytes(df: pd.DataFrame) -> bytes:
    import io
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="compare")
    return buf.getvalue()

export_df = df_out.copy()
# keep numeric totals for export
st.download_button(
    label="下载 Excel（compare.xlsx）",
    data=to_excel_bytes(export_df),
    file_name="compare.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
