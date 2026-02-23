# app.py
# Aifuge Freight Engine V6 — 5家成本 + 对客报价(SELL) + 毛利
# Auto-load from ./data/ (无需每次手动上传)

import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import streamlit as st


# =========================
# 0) Paths (data/)
# =========================
DATA_DIR = "data"

PATHS = {
    "DHL": os.path.join(DATA_DIR, "DHL_Frachtkosten.xlsx"),
    "RABEN": os.path.join(DATA_DIR, "Raben_Frachtkosten.xlsx"),
    "RABEN_RULES": os.path.join(DATA_DIR, "Raben 立方米及装载米规则.xlsx"),
    "SCHENKER": os.path.join(DATA_DIR, "Schenker_Frachtkosten.xlsx"),
    "SCHENKER_MAUT": os.path.join(DATA_DIR, "Schenker_Maut.xlsx"),
    "HELLMANN": os.path.join(DATA_DIR, "Hellmann_Frachtkosten_2026.xlsx"),
    "FEDEX": os.path.join(DATA_DIR, "FedEx_Frachtkosten.xlsx"),
    "SELL": os.path.join(DATA_DIR, "SELL_Frachtkosten.xlsx"),
}

SUPPLIERS = ["DHL", "Raben", "Schenker", "Hellmann", "FedEx"]


# =========================
# 1) Helpers
# =========================
def norm_col(c: str) -> str:
    return re.sub(r"\s+", "", str(c)).strip().lower()


def find_key_col(df: pd.DataFrame) -> Optional[str]:
    for c in df.columns:
        if norm_col(c) == "key":
            return c
    # fallback: first col if looks like keys
    if df.shape[1] > 0:
        c0 = df.columns[0]
        if df[c0].astype(str).str.contains(r"^[A-Z]+-[A-Z]{2}-", regex=True, na=False).mean() > 0.3:
            return c0
    return None


def bis_cols(df: pd.DataFrame) -> List[Tuple[str, float]]:
    """
    Return list of (col_name, limit_kg) for cols like bis-30, bis-2500
    """
    out = []
    for c in df.columns:
        s = str(c).strip()
        m = re.match(r"^bis[-_ ]?(\d+(\.\d+)?)$", s, flags=re.IGNORECASE)
        if m:
            out.append((c, float(m.group(1))))
    out.sort(key=lambda x: x[1])
    return out


def pick_bracket(bis_list: List[Tuple[str, float]], charge_kg: float) -> Optional[str]:
    for col, limit in bis_list:
        if charge_kg <= limit + 1e-9:
            return col
    return None


def parse_zone_from_input(zone_input: str) -> str:
    """
    支持:
    - 数字邮编前2位：'38112'->'38', '08xxx'->'08'
    - 爱尔兰/特殊：'DUB'/'COR' 等 -> 'DUB'
    """
    s = (zone_input or "").strip().upper()
    # take first 2 digits if any digits exist
    digits = re.findall(r"\d", s)
    if digits:
        dd = "".join(digits)
        return dd[:2].zfill(2)
    # otherwise keep letters (up to 6)
    letters = re.findall(r"[A-Z]", s)
    if letters:
        return "".join(letters)[:6]
    return ""


def build_key(prefix: str, cc: str, zone: str) -> str:
    return f"{prefix}-{cc}-{zone}"


def safe_read_excel(path: str) -> pd.DataFrame:
    # 只读xlsx；若用户误传xls，这里也尽量兼容（依赖环境可能没有xlrd）
    return pd.read_excel(path, sheet_name=0)


def money(x) -> float:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return 0.0
    try:
        return float(x)
    except Exception:
        return 0.0


# =========================
# 2) Load tables
# =========================
@st.cache_data(show_spinner=False)
def load_rate_table(path: str) -> Tuple[Optional[pd.DataFrame], str]:
    """
    Load a rate sheet with a 'key' column + bis-* columns.
    Returns (df, msg). If fail -> (None, err_msg)
    """
    if not os.path.exists(path):
        return None, f"文件不存在: {path}"
    try:
        df = safe_read_excel(path)
        kcol = find_key_col(df)
        if not kcol:
            return None, f"未找到 key 列: {os.path.basename(path)}"
        # rename key col to 'key'
        if kcol != "key":
            df = df.rename(columns={kcol: "key"})
        df["key"] = df["key"].astype(str).str.strip()
        # ensure bis cols exist
        bcols = bis_cols(df)
        if not bcols:
            return None, f"未找到任何 bis-* 价格列: {os.path.basename(path)}"
        return df, "ok"
    except Exception as e:
        return None, f"读取失败: {os.path.basename(path)} | {type(e).__name__}: {e}"


@st.cache_data(show_spinner=False)
def load_raben_rules(path: str) -> Tuple[Dict[str, float], str]:
    """
    Raben 体积重系数表：返回 {ISO2: factor}
    规则表结构不确定，所以做“尽可能”识别：
    - 找到包含 country/land/iso 的列 + factor/volumetric 的列
    - 否则返回空 dict
    """
    if not os.path.exists(path):
        return {}, f"文件不存在: {path}"
    try:
        df = safe_read_excel(path)
        cols = {norm_col(c): c for c in df.columns}
        country_col = None
        for k in cols:
            if k in ("country", "land", "iso", "iso2", "länder", "laender"):
                country_col = cols[k]
                break
        factor_col = None
        for k in cols:
            if "factor" in k or "volum" in k or "cbm" in k or "立方" in k:
                factor_col = cols[k]
                break

        if not country_col or not factor_col:
            # try heuristic: first col country, second col factor
            if df.shape[1] >= 2:
                country_col = df.columns[0]
                factor_col = df.columns[1]
            else:
                return {}, "Raben规则表列结构无法识别(已忽略，将使用默认200)"

        lut: Dict[str, float] = {}
        for _, r in df.iterrows():
            cc = str(r.get(country_col, "")).strip().upper()
            if not cc or cc == "NAN":
                continue
            val = r.get(factor_col, None)
            try:
                f = float(val)
            except Exception:
                continue
            # 只接受合理范围
            if 50 <= f <= 1000:
                lut[cc] = f
        if not lut:
            return {}, "Raben规则表未解析出有效factor(将使用默认200)"
        return lut, "ok"
    except Exception as e:
        return {}, f"Raben规则表读取失败(已忽略): {type(e).__name__}: {e}"


@st.cache_data(show_spinner=False)
def load_schenker_maut(path: str) -> Tuple[Optional[pd.DataFrame], str]:
    """
    Schenker maut 表格结构不稳定；本版本“可读就读”，否则走手动输入。
    """
    if not os.path.exists(path):
        return None, f"文件不存在: {path}"
    try:
        df = safe_read_excel(path)
        return df, "ok"
    except Exception as e:
        return None, f"Schenker Maut表读取失败(将仅手动): {type(e).__name__}: {e}"


# =========================
# 3) Hellmann rules (V5 - 全国家字典)
# =========================
HELLMANN_RULES_V5 = {
    # cc: {"maut_pct": x, "state_pct": y, "vol_factor": 200 or 150 by route}
    # NOTE: vol_factor在代码里按 DE=150, 其它=200 处理；这里主要放百分比
    "DE": {"maut_pct": 18.2, "state_pct": 0.0},

    "AT": {"maut_pct": 13.3, "state_pct": 6.6},
    "BE": {"maut_pct": 9.7, "state_pct": 2.1},
    "BG": {"maut_pct": 6.2, "state_pct": 9.9},
    "CZ": {"maut_pct": 8.6, "state_pct": 5.4},
    "DK": {"maut_pct": 8.6, "state_pct": 0.1},
    "EE": {"maut_pct": 7.2, "state_pct": 0.0},
    "ES": {"maut_pct": 6.7, "state_pct": 0.0},
    "FI": {"maut_pct": 4.8, "state_pct": 3.1},
    "FR": {"maut_pct": 7.7, "state_pct": 0.5},
    "GR": {"maut_pct": 7.8, "state_pct": 10.0},
    "HR": {"maut_pct": 9.1, "state_pct": 11.6},
    "HU": {"maut_pct": 11.5, "state_pct": 15.2},
    "IE": {"maut_pct": 6.1, "state_pct": 3.6},
    "IT": {"maut_pct": 10.3, "state_pct": 7.0},
    "LT": {"maut_pct": 7.6, "state_pct": 0.0},
    "LU": {"maut_pct": 10.9, "state_pct": 0.0},
    "LV": {"maut_pct": 7.0, "state_pct": 0.0},
    "NL": {"maut_pct": 8.9, "state_pct": 0.0},
    "PL": {"maut_pct": 10.2, "state_pct": 2.6},
    "PT": {"maut_pct": 7.7, "state_pct": 0.0},
    "RO": {"maut_pct": 7.0, "state_pct": 10.6},
    "SE": {"maut_pct": 3.6, "state_pct": 0.7},
    "SI": {"maut_pct": 12.5, "state_pct": 15.3},
    "SK": {"maut_pct": 8.5, "state_pct": 5.9},
    "XK": {"maut_pct": 3.4, "state_pct": 4.3},
}

# DG surcharge lists (用户给的条款)
HELLMANN_DG_30 = set("AL AT BA BE BG CH CZ DK EE ES FL FR HR HU IT LT LU LV ME MK NL PL PT RO RS SI SK XK".split())
HELLMANN_DG_75 = set("FI GB GR IE NO SE".split())


def hellmann_diesel_pct(diesel_price_eur_per_l: float) -> float:
    """
    根据Dieselfloater表：
    <=1.48:0%
    <=1.50:0.5%
    <=1.52:1.0%
    <=1.54:1.5%
    <=1.56:2.0%
    <=1.58:2.5%
    <=1.60:3.0%
    <=1.62:3.5%
    之后每+0.02 => +0.5
    """
    p = float(diesel_price_eur_per_l)
    thresholds = [
        (1.48, 0.0),
        (1.50, 0.5),
        (1.52, 1.0),
        (1.54, 1.5),
        (1.56, 2.0),
        (1.58, 2.5),
        (1.60, 3.0),
        (1.62, 3.5),
    ]
    for t, pct in thresholds:
        if p <= t + 1e-9:
            return pct
    # above 1.62
    extra = p - 1.62
    steps = int(np.floor(extra / 0.02 + 1e-9))
    return 3.5 + steps * 0.5


# =========================
# 4) Chargeable weight
# =========================
def calc_totals(lines: pd.DataFrame) -> Tuple[float, float, float]:
    """
    Returns: total_pieces, total_actual_kg, total_cbm
    """
    total_pieces = float((lines["qty"] if "qty" in lines else 0).sum())
    total_actual_kg = float((lines["qty"] * lines["weight_kg"]).sum())
    # cbm = L*W*H / 1e6 in m3 * qty
    cbm = (lines["l_cm"] * lines["w_cm"] * lines["h_cm"]) / 1_000_000.0
    total_cbm = float((lines["qty"] * cbm).sum())
    return total_pieces, total_actual_kg, total_cbm


def charge_weight_generic(total_actual_kg: float, total_cbm: float, factor: float) -> float:
    return float(max(total_actual_kg, total_cbm * factor))


def charge_weight_fedex(lines: pd.DataFrame, factor: float = 200.0, min_piece_kg: float = 68.0) -> float:
    """
    FedEx: 每件最低计重68kg，且计费重= max(实重,体积重) 再与68取max（逐件）
    """
    cbm_piece = (lines["l_cm"] * lines["w_cm"] * lines["h_cm"]) / 1_000_000.0
    vol_kg_piece = cbm_piece * factor
    piece_charge = np.maximum(np.maximum(lines["weight_kg"].astype(float), vol_kg_piece.astype(float)), min_piece_kg)
    return float((piece_charge * lines["qty"].astype(float)).sum())


# =========================
# 5) Price lookup
# =========================
def lookup_price(df: pd.DataFrame, key: str, charge_kg: float) -> Tuple[Optional[float], Optional[str], str]:
    """
    Returns: (price, bracket_col, note)
    """
    if df is None or df.empty:
        return None, None, "价格表未加载"
    hit = df[df["key"].astype(str).str.strip() == key]
    if hit.empty:
        return None, None, f"未找到线路 key={key}"
    row = hit.iloc[0]
    bcols = bis_cols(df)
    bname = pick_bracket(bcols, charge_kg)
    if not bname:
        max_b = max(x[1] for x in bcols) if bcols else None
        return None, None, f"计费重{charge_kg:.2f}kg 超出最大档位({max_b})"
    val = row.get(bname, None)
    try:
        price = float(val)
    except Exception:
        return None, bname, f"档位{bname}无有效价格"
    return price, bname, "ok"


# =========================
# 6) App UI
# =========================
st.set_page_config(page_title="Aifuge Freight Engine V6", layout="wide")
st.title("Aifuge Freight Engine V6 — 成本 + 对客报价(SELL) + 毛利")

with st.expander("📦 数据文件状态 (data/ 自动读取)", expanded=False):
    for k, p in PATHS.items():
        st.write(f"- **{k}**: `{p}` {'✅' if os.path.exists(p) else '❌'}")

# Load tables
DHL_DF, DHL_MSG = load_rate_table(PATHS["DHL"])
RABEN_DF, RABEN_MSG = load_rate_table(PATHS["RABEN"])
SCHENKER_DF, SCHENKER_MSG = load_rate_table(PATHS["SCHENKER"])
HELLMANN_DF, HELLMANN_MSG = load_rate_table(PATHS["HELLMANN"])
FEDEX_DF, FEDEX_MSG = load_rate_table(PATHS["FEDEX"])
SELL_DF, SELL_MSG = load_rate_table(PATHS["SELL"])

RABEN_FACTORS, RABEN_RULES_MSG = load_raben_rules(PATHS["RABEN_RULES"])
SCHENKER_MAUT_DF, SCHENKER_MAUT_MSG = load_schenker_maut(PATHS["SCHENKER_MAUT"])

warns = []
for name, msg in [
    ("DHL", DHL_MSG),
    ("Raben", RABEN_MSG),
    ("Schenker", SCHENKER_MSG),
    ("Hellmann", HELLMANN_MSG),
    ("FedEx", FEDEX_MSG),
    ("SELL(对客)", SELL_MSG),
]:
    if msg != "ok":
        warns.append(f"{name}: {msg}")
if RABEN_RULES_MSG != "ok":
    warns.append(f"Raben规则: {RABEN_RULES_MSG}")
if SCHENKER_MAUT_MSG != "ok":
    warns.append(f"SchenkerMaut: {SCHENKER_MAUT_MSG}")

if warns:
    st.warning(" | ".join(warns))

# -------------------------
# Inputs
# -------------------------
c1, c2, c3 = st.columns([1, 1, 2])
with c1:
    dest_cc = st.text_input("目的国(ISO2)", value="DE").strip().upper()
with c2:
    zone_input = st.text_input("邮编/区域(用于key的ZZ)", value="38112")
with c3:
    st.caption("说明：系统会把你输入的邮编/区域转成 ZZ：数字取前2位(保留0)，字母如 DUB 直接用。")

zone = parse_zone_from_input(zone_input)
if not zone:
    st.error("无法解析 ZZ (请填写邮编前2位或区域代码，如 38112 / 08xxx / DUB)")
    st.stop()

st.markdown("### 货物明细（多件：qty/weight/L/W/H）")

default_lines = pd.DataFrame(
    [{"qty": 1, "weight_kg": 20.0, "l_cm": 60.0, "w_cm": 40.0, "h_cm": 40.0}]
)

lines = st.data_editor(
    default_lines,
    num_rows="dynamic",
    use_container_width=True,
    key="lines_editor",
)

# validate numeric
for col in ["qty", "weight_kg", "l_cm", "w_cm", "h_cm"]:
    if col not in lines.columns:
        st.error(f"缺少列: {col}")
        st.stop()
lines = lines.fillna(0)
for col in ["qty", "weight_kg", "l_cm", "w_cm", "h_cm"]:
    try:
        lines[col] = lines[col].astype(float)
    except Exception:
        st.error(f"列 {col} 需要是数字")
        st.stop()

total_pieces, total_actual_kg, total_cbm = calc_totals(lines)

st.markdown(
    f"**合计实重(kg)**: `{total_actual_kg:.2f}`  |  "
    f"**合计体积(m³)**: `{total_cbm:.4f}`  |  "
    f"**件数(行内qty合计)**: `{total_pieces:.0f}`"
)

# -------------------------
# Extras / Parameters
# -------------------------
st.markdown("### 附加费/参数（可上线：先手动，后续可接API）")

e1, e2, e3 = st.columns(3)

with e1:
    dhl_avis = st.checkbox("DHL Avis（电话预约）+11€", value=False)

with e2:
    sch_avis = st.checkbox("Schenker Avis（电话预约）+20€", value=False)
    sch_diesel_pct = st.number_input("Schenker Diesel Floating %（手动）", min_value=0.0, max_value=100.0, value=0.0, step=0.1)
    sch_maut_pct_manual = st.number_input("Schenker Maut %（手动）", min_value=0.0, max_value=100.0, value=0.0, step=0.1)

with e3:
    # Hellmann extras
    hell_b2c = st.checkbox("Hellmann B2C +8.9€（可与DG/Avis叠加）", value=False)
    hell_avis = st.checkbox("Hellmann Avis +12.5€（可与DG/B2C叠加）", value=False)
    hell_dg = st.checkbox("Hellmann 危险品 DG（可与B2C/Avis叠加）", value=False)
    diesel_price = st.number_input("Hellmann Diesel €/L（用于Dieselfloater）", min_value=0.0, max_value=10.0, value=1.50, step=0.01)

# Length surcharge: any piece longest edge >240
max_edge_cm = float(np.max(np.maximum.reduce([lines["l_cm"], lines["w_cm"], lines["h_cm"]]))) if len(lines) else 0.0
hell_len_auto = max_edge_cm > 240.0
hell_len = st.checkbox(f"Hellmann Längenzuschlag +30€（单件最长边>240cm；当前最大边={max_edge_cm:.0f}cm）", value=hell_len_auto)

# -------------------------
# Compute chargeable weights
# -------------------------
def factor_for(carrier: str, cc: str) -> float:
    cc = (cc or "").upper()
    if carrier == "DHL":
        return 200.0
    if carrier == "Raben":
        return float(RABEN_FACTORS.get(cc, 200.0))
    if carrier == "Schenker":
        return 150.0 if cc == "DE" else 200.0
    if carrier == "Hellmann":
        return 150.0 if cc == "DE" else 200.0
    if carrier == "FedEx":
        return 200.0
    return 200.0


# SELL（对客）固定 200 kg/m3
sell_charge_kg = charge_weight_generic(total_actual_kg, total_cbm, 200.0)

# supplier charge kg
charge_by_carrier = {}
for c in SUPPLIERS:
    if c == "FedEx":
        charge_by_carrier[c] = charge_weight_fedex(lines, factor=200.0, min_piece_kg=68.0)
    else:
        charge_by_carrier[c] = charge_weight_generic(total_actual_kg, total_cbm, factor_for(c, dest_cc))

st.markdown(
    "#### 计费重(kg)\n"
    + " | ".join([f"**{k}**: `{v:.2f}`" for k, v in charge_by_carrier.items()])
    + f" | **SELL**: `{sell_charge_kg:.2f}`"
)

# -------------------------
# Main compare table
# -------------------------
def dg_fee_for_country(cc: str) -> float:
    cc = (cc or "").upper()
    if cc == "DE":
        return 15.0
    if cc in HELLMANN_DG_75:
        return 75.0
    # 默认落到30组
    return 30.0


def hellmann_percent_fees(cc: str, base: float) -> Tuple[float, str]:
    """
    Apply Hellmann Maut% + Staatliche% to base.
    """
    cc = (cc or "").upper()
    rule = HELLMANN_RULES_V5.get(cc, None)
    if not rule:
        # 没有规则就按0
        return 0.0, "Hellmann规则缺失：Maut/State按0%"
    maut = float(rule.get("maut_pct", 0.0))
    state = float(rule.get("state_pct", 0.0))
    fee = base * (maut + state) / 100.0
    return fee, f"Maut {maut:.1f}% + State {state:.1f}%"


def schenker_percent_fees(base: float) -> float:
    # Schenker: 本版本按手动输入
    return base * (sch_diesel_pct + sch_maut_pct_manual) / 100.0


def sell_lookup(cc: str, zone: str, sell_charge_kg: float) -> Tuple[Optional[float], Optional[str], str]:
    k = build_key("SELL", cc, zone)
    return lookup_price(SELL_DF, k, sell_charge_kg)


def supplier_lookup_and_cost(carrier: str) -> Dict:
    cc = dest_cc
    zz = zone
    prefix = carrier.upper()
    k = build_key(prefix, cc, zz)
    charge_kg = charge_by_carrier[carrier]

    if carrier == "DHL":
        base, bracket, note = lookup_price(DHL_DF, k, charge_kg)
        if base is None:
            return {"carrier": carrier, "ok": False, "key": k, "charge_kg": charge_kg, "bracket": bracket or "-", "base": 0.0,
                    "extras": 0.0, "total_cost": 0.0, "note": note}
        extras = 0.0
        if dhl_avis:
            extras += 11.0
        total = base + extras
        return {"carrier": carrier, "ok": True, "key": k, "charge_kg": charge_kg, "bracket": bracket, "base": base,
                "extras": extras, "total_cost": total, "note": "ok"}

    if carrier == "Raben":
        base, bracket, note = lookup_price(RABEN_DF, k, charge_kg)
        if base is None:
            return {"carrier": carrier, "ok": False, "key": k, "charge_kg": charge_kg, "bracket": bracket or "-", "base": 0.0,
                    "extras": 0.0, "total_cost": 0.0, "note": note}
        # 目前未接入额外费用
        extras = 0.0
        total = base + extras
        return {"carrier": carrier, "ok": True, "key": k, "charge_kg": charge_kg, "bracket": bracket, "base": base,
                "extras": extras, "total_cost": total, "note": f"ok (vol_factor={factor_for('Raben', cc):.0f})"}

    if carrier == "Schenker":
        base, bracket, note = lookup_price(SCHENKER_DF, k, charge_kg)
        if base is None:
            return {"carrier": carrier, "ok": False, "key": k, "charge_kg": charge_kg, "bracket": bracket or "-", "base": 0.0,
                    "extras": 0.0, "total_cost": 0.0, "note": note}
        extras = 0.0
        if sch_avis:
            extras += 20.0
        # percent fees
        extras += schenker_percent_fees(base)
        total = base + extras
        note2 = f"ok (Schenker factor={factor_for('Schenker', cc):.0f}; diesel%+maut%={sch_diesel_pct+sch_maut_pct_manual:.1f}%)"
        return {"carrier": carrier, "ok": True, "key": k, "charge_kg": charge_kg, "bracket": bracket, "base": base,
                "extras": extras, "total_cost": total, "note": note2}

    if carrier == "Hellmann":
        base, bracket, note = lookup_price(HELLMANN_DF, k, charge_kg)
        if base is None:
            return {"carrier": carrier, "ok": False, "key": k, "charge_kg": charge_kg, "bracket": bracket or "-", "base": 0.0,
                    "extras": 0.0, "total_cost": 0.0, "note": note}
        extras = 0.0

        # percent fees: maut+state
        pct_fee, pct_note = hellmann_percent_fees(cc, base)
        extras += pct_fee

        # diesel floater percent on freight cost (base)
        d_pct = hellmann_diesel_pct(diesel_price)
        extras += base * d_pct / 100.0

        # fixed extras (stackable)
        if hell_b2c:
            extras += 8.9
        if hell_avis:
            extras += 12.5
        if hell_len:
            extras += 30.0
        if hell_dg:
            extras += dg_fee_for_country(cc)

        total = base + extras
        note2 = f"ok ({pct_note}; diesel={d_pct:.1f}%; factor={factor_for('Hellmann', cc):.0f})"
        return {"carrier": carrier, "ok": True, "key": k, "charge_kg": charge_kg, "bracket": bracket, "base": base,
                "extras": extras, "total_cost": total, "note": note2}

    if carrier == "FedEx":
        base, bracket, note = lookup_price(FEDEX_DF, k, charge_kg)
        if base is None:
            return {"carrier": carrier, "ok": False, "key": k, "charge_kg": charge_kg, "bracket": bracket or "-", "base": 0.0,
                    "extras": 0.0, "total_cost": 0.0, "note": note}
        # FedEx：你说明已包含燃油/路桥/Avis，因此 extras=0
        extras = 0.0
        total = base + extras
        return {"carrier": carrier, "ok": True, "key": k, "charge_kg": charge_kg, "bracket": bracket, "base": base,
                "extras": extras, "total_cost": total, "note": "ok (inclusive; factor=200; min_piece=68kg)"}

    return {"carrier": carrier, "ok": False, "key": k, "charge_kg": charge_kg, "bracket": "-", "base": 0.0,
            "extras": 0.0, "total_cost": 0.0, "note": "unknown carrier"}


rows = []
for c in SUPPLIERS:
    rows.append(supplier_lookup_and_cost(c))

cmp = pd.DataFrame(rows)

# SELL price (对客)
sell_key = build_key("SELL", dest_cc, zone)
sell_price, sell_bracket, sell_note = lookup_price(SELL_DF, sell_key, sell_charge_kg)

cmp["sell_key"] = sell_key
cmp["sell_bracket"] = sell_bracket if sell_bracket else "-"
cmp["sell_price"] = sell_price if sell_price is not None else np.nan
cmp["profit"] = (cmp["sell_price"] - cmp["total_cost"]) if sell_price is not None else np.nan

st.markdown("### 五家同步报价对比（成本 / 对客 / 毛利）")
st.dataframe(
    cmp[["carrier", "ok", "key", "charge_kg", "bracket", "base", "extras", "total_cost", "sell_key", "sell_bracket", "sell_price", "profit", "note"]],
    use_container_width=True,
    hide_index=True,
)

with st.expander("📌 对客(SELL) 解析信息", expanded=False):
    st.write(f"- SELL key: **{sell_key}**")
    st.write(f"- SELL charge_kg: **{sell_charge_kg:.2f}** (max(实重, 体积重*200))")
    st.write(f"- bracket: **{sell_bracket}**")
    st.write(f"- sell_price: **{sell_price}**")
    st.write(f"- note: **{sell_note}**")

# Export
def to_excel_bytes(df_out: pd.DataFrame) -> bytes:
    import io
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df_out.to_excel(writer, index=False, sheet_name="compare")
    return buf.getvalue()

st.download_button(
    "导出Excel（compare）",
    data=to_excel_bytes(cmp),
    file_name="aifuge_freight_compare_v6.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
