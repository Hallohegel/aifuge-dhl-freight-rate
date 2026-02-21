import streamlit as st
import pandas as pd
import re
from io import BytesIO
from datetime import datetime
import math

st.set_page_config(page_title="Aifuge Freight Cost Engine", layout="wide")

# =========================
# 通用工具
# =========================
def normalize_prefix(prefix: str) -> str:
    return re.sub(r"\D", "", str(prefix)).zfill(2)[:2]

def build_key(carrier: str, country: str, prefix: str) -> str:
    return f"{carrier}-{country.upper()}--{normalize_prefix(prefix)}"

def sorted_weight_cols(cols) -> list:
    w = [c for c in cols if isinstance(c, str) and c.strip().startswith("bis-")]
    return sorted(w, key=lambda x: int(x.split("-")[1]))

def pick_weight_col(weight_cols_sorted: list, billable_weight: float) -> str:
    for c in weight_cols_sorted:
        upper = int(c.split("-")[1])
        if billable_weight <= upper:
            return c
    return weight_cols_sorted[-1]

def safe_float(x, default=0.0) -> float:
    try:
        if pd.isna(x):
            return default
        return float(x)
    except Exception:
        return default

def ceil_to_step(value: float, step: float) -> float:
    """向上取整到指定步长 step（例如 0.01 / 0.1 / 1 / 10）"""
    if step <= 0:
        return float(value)
    return math.ceil(float(value) / float(step)) * float(step)

# =========================
# 柴油附加费（沿用你原表）
# =========================
def get_diesel_surcharge_percent(price_cent: float) -> int:
    table = [
        (0.00,147.05,0),(147.06,151.51,1),(151.52,155.97,2),
        (155.98,160.43,3),(160.44,164.89,4),(164.90,169.35,5),
        (169.36,173.81,6),(173.82,178.27,7),(178.28,182.73,8),
        (182.74,187.19,9),(187.20,191.65,10),(191.66,196.11,11),
        (196.12,200.57,12),(200.58,205.03,13),(205.04,209.49,14),
        (209.50,213.95,15),(213.96,218.41,16),(218.42,222.87,17),
        (222.88,227.33,18),(227.34,231.79,19),(231.80,236.25,20),
        (236.26,240.71,21),
    ]
    for low, high, pct in table:
        if low <= price_cent <= high:
            return pct
    return 0

# =========================
# DHL 保险（你确认过的29段+3区）
# =========================
DHL_INSURANCE_TABLE = [
    (500,    3.28,  3.94,   5.25),
    (1000,   3.28,  3.94,   5.25),
    (1500,   3.28,  3.94,   5.25),
    (2000,   3.28,  3.94,   5.25),
    (2500,   3.86,  4.11,   5.25),
    (3000,   4.55,  4.98,   6.14),
    (3500,   5.24,  5.85,   7.30),
    (4000,   5.93,  6.72,   8.46),
    (4500,   6.62,  7.59,   9.62),
    (5000,   7.31,  8.46,  10.78),
    (5500,   8.00,  9.33,  11.94),
    (6000,   8.69, 10.20,  13.10),
    (6500,   9.38, 11.07,  14.26),
    (7000,  10.07, 11.94,  15.42),
    (7500,  10.76, 12.81,  16.58),
    (8000,  11.45, 13.68,  17.74),
    (8500,  12.14, 14.55,  18.90),
    (9000,  12.83, 15.42,  20.06),
    (9500,  13.52, 16.29,  21.22),
    (10000, 14.21, 17.16,  22.38),
    (15000, 21.11, 25.86,  33.98),
    (20000, 28.01, 34.56,  45.58),
    (25000, 34.91, 43.26,  57.18),
    (30000, 41.82, 51.96,  68.78),
    (35000, 48.72, 60.66,  80.38),
    (40000, 55.62, 69.36,  91.98),
    (45000, 62.52, 78.06, 103.58),
    (50000, 69.42, 86.76, 115.18),
    (100000,138.44,173.76,231.18),
]

WEST_EU = {"AT","BE","NL","LU","FR","ES","PT","IT","DK","SE","FI","IE","GB","NO","CH"}
def dhl_region(country_code: str) -> str:
    cc = (country_code or "").upper()
    if cc == "DE":
        return "DE"
    if cc in WEST_EU:
        return "WEST"
    return "EAST"

def get_dhl_insurance_cost(goods_value_eur: float, country_code: str) -> float:
    if goods_value_eur <= 0:
        return 0.0
    region = dhl_region(country_code)
    for limit, de, west, east in DHL_INSURANCE_TABLE:
        if goods_value_eur <= limit:
            return de if region == "DE" else (west if region == "WEST" else east)
    _, de, west, east = DHL_INSURANCE_TABLE[-1]
    return de if region == "DE" else (west if region == "WEST" else east)

# =========================
# Raben 保险：0.9‰，最低 5.95€
# =========================
def get_raben_insurance_cost(goods_value_eur: float) -> float:
    if goods_value_eur <= 0:
        return 0.0
    return max(goods_value_eur * 0.9 / 1000.0, 5.95)

# =========================
# Raben 规则表解析（含 LDM 阈值 + PP 阈值）
# =========================
RABEN_RULES_PATH = "data/Raben 立方米及装载米规则.xlsx"

def _first_int_in_text(s):
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return None
    m = re.search(r"(\d+)", str(s))
    return int(m.group(1)) if m else None

def _first_float_in_text(pattern, s):
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return None
    m = re.search(pattern, str(s))
    if not m:
        return None
    return float(m.group(1).replace(",", "."))

@st.cache_data(show_spinner=False)
def load_raben_rules():
    df = pd.read_excel(RABEN_RULES_PATH, sheet_name=0)
    df = df.rename(columns={df.columns[0]: "ROW"})
    df["ROW"] = df["ROW"].astype(str).str.strip()
    return df

def get_raben_rule(df_rules: pd.DataFrame, country_name: str):
    if country_name not in df_rules.columns:
        return None

    cbm_cell = df_rules.loc[df_rules["ROW"].str.lower().eq("1 cbm"), country_name].values
    cbm_cell = cbm_cell[0] if len(cbm_cell) else None

    ldm_cell = df_rules.loc[df_rules["ROW"].str.lower().eq("1 ldm"), country_name].values
    ldm_cell = ldm_cell[0] if len(ldm_cell) else None

    cbm_kg = _first_int_in_text(cbm_cell) or 0
    ldm_kg = _first_int_in_text(ldm_cell) or 0

    # 解析阈值：bis 2,4 LDM / 6 Eurostellplätze
    ldm_threshold = _first_float_in_text(r"bis\s*(\d+[,\.]\d+)\s*LDM", cbm_cell)
    pp_threshold  = _first_int_in_text(re.search(r"/\s*(\d+)\s*Eurostellpl", str(cbm_cell)).group(1)) if ("Eurostellpl" in str(cbm_cell)) else None

    # 保险起见：如果正则没抓到 PP 阈值，尝试另一种写法
    if pp_threshold is None:
        pp_threshold = _first_int_in_text(_first_float_in_text(r"/\s*(\d+)\s*Eurostellpl", cbm_cell))

    def _min(pattern: str):
        r = df_rules.loc[df_rules["ROW"].str.contains(pattern, case=False, na=False), country_name]
        if r.empty:
            return 0
        return _first_int_in_text(r.values[0]) or 0

    return {
        "cbm_kg": cbm_kg,
        "ldm_kg": ldm_kg,
        "ldm_threshold": ldm_threshold,   # None 代表没写
        "pp_threshold": pp_threshold,     # None 代表没写
        "min_pack": {
            "Kartons/Pakete/kleine Packstücke": _min("Kartons"),
            "Halbpaletten/kleine Paletten": _min("Halbpaletten"),
            "Euromaß Paletten": _min("Euromaß"),
            "Sonstige Paletten": _min("Sonstige"),
        }
    }

def calc_ldm_from_dims_cm(length_cm: float, width_cm: float, qty: float, truck_width_m: float = 2.4) -> float:
    # 常用估算：LDM = footprint_area(m²) / truck_width
    l = max(float(length_cm), 0.0) / 100.0
    w = max(float(width_cm), 0.0) / 100.0
    q = max(float(qty), 0.0)
    area = l * w * q
    return area / float(truck_width_m) if area > 0 else 0.0

def calc_pp_from_dims_cm(length_cm: float, width_cm: float, qty: float, euro_pallet_area_m2: float = 0.96) -> float:
    # PP(Eurostellplätze) ≈ 占地面积 / 0.96
    l = max(float(length_cm), 0.0) / 100.0
    w = max(float(width_cm), 0.0) / 100.0
    q = max(float(qty), 0.0)
    area = l * w * q
    return area / float(euro_pallet_area_m2) if area > 0 else 0.0

def calc_raben_basis(cargo: pd.DataFrame, rule: dict, pack_type: str,
                     round_cbm=0.01, round_ldm=0.01, round_pp=0.01, round_basis_kg=1.0,
                     truck_width_m=2.4,
                     use_manual_pp=True, use_manual_ldm=True, use_manual_cbm=True):
    # totals raw
    total_real = float((cargo["实重(kg)"] * cargo["数量"]).sum())

    # CBM
    cbm_raw = float(((cargo["长(cm)"]/100)*(cargo["宽(cm)"]/100)*(cargo["高(cm)"]/100)*cargo["数量"]).sum())
    cbm = ceil_to_step(cbm_raw, round_cbm)

    # LDM
    ldm_raw = float(cargo.apply(lambda r: calc_ldm_from_dims_cm(r["长(cm)"], r["宽(cm)"], r["数量"], truck_width_m), axis=1).sum())
    ldm = ceil_to_step(ldm_raw, round_ldm)

    # PP
    pp_raw = float(cargo.apply(lambda r: calc_pp_from_dims_cm(r["长(cm)"], r["宽(cm)"], r["数量"]), axis=1).sum())
    pp = ceil_to_step(pp_raw, round_pp)

    # manual overrides (bill-level 必须支持)
    if use_manual_cbm and "CBM手动" in cargo.columns:
        v = safe_float(cargo["CBM手动"].replace("", 0).fillna(0).sum(), 0.0)
        if v > 0:
            cbm = ceil_to_step(v, round_cbm)

    if use_manual_ldm and "LDM手动" in cargo.columns:
        v = safe_float(cargo["LDM手动"].replace("", 0).fillna(0).sum(), 0.0)
        if v > 0:
            ldm = ceil_to_step(v, round_ldm)

    if use_manual_pp and "PP手动" in cargo.columns:
        v = safe_float(cargo["PP手动"].replace("", 0).fillna(0).sum(), 0.0)
        if v > 0:
            pp = ceil_to_step(v, round_pp)

    cbm_kg = float(rule["cbm_kg"])
    ldm_kg = float(rule["ldm_kg"])
    ldm_th = rule.get("ldm_threshold")  # e.g. 2.4 / 1.6
    pp_th  = rule.get("pp_threshold")   # e.g. 6 / 7 / 8

    # decide conv basis:
    # 切换条件：LDM超阈值 或 PP超阈值 -> LDM计费（前提该国有ldm_kg）
    must_use_ldm = False
    if ldm_th is not None and ldm > float(ldm_th):
        must_use_ldm = True
    if pp_th is not None and pp > float(pp_th):
        must_use_ldm = True

    if must_use_ldm and ldm_kg > 0:
        conv_weight = ldm * ldm_kg
        conv_basis = f"LDM×{int(ldm_kg)}"
        trigger = f"trigger=LDM>{ldm_th} or PP>{pp_th}"
    else:
        conv_weight = cbm * cbm_kg
        conv_basis = f"CBM×{int(cbm_kg)}"
        trigger = f"trigger=within bis-threshold"

    # min per piece
    min_per_piece = float(rule["min_pack"].get(pack_type, 0))
    total_pieces = float(cargo["数量"].sum())
    total_min = total_pieces * min_per_piece

    basis_raw = max(total_real, conv_weight, total_min)
    basis = ceil_to_step(basis_raw, round_basis_kg)

    detail = {
        "real_kg": total_real,
        "cbm_raw": cbm_raw, "cbm_rounded": cbm,
        "ldm_raw": ldm_raw, "ldm_rounded": ldm,
        "pp_raw": pp_raw, "pp_rounded": pp,
        "ldm_threshold": ldm_th, "pp_threshold": pp_th,
        "conv_basis": conv_basis, "conv_weight": conv_weight,
        "min_per_piece": min_per_piece, "total_min": total_min,
        "basis_raw": basis_raw, "basis_rounded": basis,
        "switch_reason": trigger
    }
    return basis, detail

# =========================
# 读取运价表
# =========================
DHL_PATH   = "data/Frachtkosten DHL Freight EU Neu.xlsx"
RABEN_PATH = "data/Raben_Frachtkosten_FINAL_filled.xlsx"

@st.cache_data(show_spinner=False)
def load_dhl():
    df = pd.read_excel(DHL_PATH, sheet_name="Frachtkosten DHL Freight")
    return df, df.columns[0], sorted_weight_cols(df.columns)

@st.cache_data(show_spinner=False)
def load_raben():
    df = pd.read_excel(RABEN_PATH, sheet_name=0)
    return df, df.columns[0], sorted_weight_cols(df.columns)

df_dhl, dhl_key_col, dhl_wcols = load_dhl()
df_raben, raben_key_col, raben_wcols = load_raben()
df_rules = load_raben_rules()

COUNTRY_NAME_MAP = {
    "DE": "Deutschland",
    "AT": "Österreich",
    "BE": "Belgien",
    "BG": "Bulgarien",
    "CZ": "Tschechische Republik",
    "DK": "Dänemark",
    "EE": "Estland",
    "ES": "Spanien",
    "FI": "Finnland",
    "FR": "Frankreich",
    "GR": "Griechenland",
    "HU": "Ungarn",
    "HR": "Kroatien",
    "IT": "Italien",
    "LU": "Luxemburg",
    "LV": "Lettland",
    "LT": "Litauen",
    "NL": "Niederlande",
    "PL": "Polen",
    "PT": "Portugal",
    "RO": "Rumänien",
    "SE": "Schweden",
    "SI": "Slowwenien",
    "SK": "Slowakai",
}

# =========================
# UI
# =========================
st.title("Aifuge GmbH | Bill-Accurate Freight Cost Engine (DHL + Raben)")

c1, c2 = st.columns(2)
with c1:
    country = st.text_input("目的地国家代码", value="DE").upper()
with c2:
    prefix = st.text_input("邮编前2位", value="38")

st.subheader("货物明细（账单级：建议你手填 PP/LDM/CBM 覆盖值以实现 100% 对账）")

base_df = pd.DataFrame([{
    "数量": 1, "长(cm)": 120, "宽(cm)": 80, "高(cm)": 120, "实重(kg)": 200,
    "PP手动": 0.0, "LDM手动": 0.0, "CBM手动": 0.0
}])

cargo = st.data_editor(base_df, num_rows="dynamic", use_container_width=True).fillna(0)

for col in ["数量","长(cm)","宽(cm)","高(cm)","实重(kg)","PP手动","LDM手动","CBM手动"]:
    if col in cargo.columns:
        cargo[col] = pd.to_numeric(cargo[col], errors="coerce").fillna(0)

# 参数区：取整规则（账单级核心）
with st.expander("账单级匹配参数（取整/阈值/估算口径）", expanded=True):
    a1, a2, a3, a4 = st.columns(4)
    with a1:
        round_cbm = st.number_input("Raben：CBM 取整步长", value=0.01)
    with a2:
        round_ldm = st.number_input("Raben：LDM 取整步长", value=0.01)
    with a3:
        round_pp = st.number_input("Raben：PP 取整步长", value=0.01)
    with a4:
        round_basis = st.number_input("Raben：Basis(kg) 取整步长", value=1.0)

    b1, b2, b3 = st.columns(3)
    with b1:
        truck_width = st.number_input("LDM 估算车宽(m)", value=2.4)
    with b2:
        use_manual = st.checkbox("优先使用手动 PP/LDM/CBM 覆盖值（推荐）", value=True)
    with b3:
        st.caption("提示：对账时把账单里的 PP/LDM/CBM 填入手动列，误差会显著下降。")

# 附加费 & 保险
st.subheader("附加费 & 保险")

x1, x2, x3 = st.columns(3)
with x1:
    diesel_price = st.number_input("柴油价格 Cent/L", value=185.0)
with x2:
    MIN_CHARGE_DHL = st.number_input("DHL 最低收费 EUR", value=0.0)
with x3:
    MIN_CHARGE_RABEN = st.number_input("Raben 最低收费 EUR", value=0.0)

fuel_pct = get_diesel_surcharge_percent(float(diesel_price))

y1, y2, y3 = st.columns(3)
with y1:
    avisierung = st.checkbox("Avisierung 11€（先做通用示例）", value=False)
with y2:
    goods_value = st.number_input("货值 EUR（用于保险）", value=0.0)
with y3:
    apply_ins_dhl = st.checkbox("计入 DHL 保险（29段）", value=True)

apply_ins_raben = st.checkbox("计入 Raben 保险（0.9‰ min 5.95）", value=False)
avisierung_cost = 11.0 if avisierung else 0.0

# DHL 特有
MARPOL_COUNTRIES = ["DK","EE","FI","GB","IE","LT","LV","NO","SE"]
apply_marpol = st.checkbox("DHL：MARPOL（部分国家 4%）", value=True)
apply_ekaer = st.checkbox("DHL：EKAER（HU 10€）", value=True)
ekaer_cost = 10.0 if (apply_ekaer and country == "HU") else 0.0

# Raben pack type
pack_type = st.selectbox(
    "Raben：包装类型（影响最低计费重）",
    ["Kartons/Pakete/kleine Packstücke", "Halbpaletten/kleine Paletten", "Euromaß Paletten", "Sonstige Paletten"],
    index=2
)

# =========================
# DHL 计费重（保持你现有 factor 逻辑；后续可再升级成账单级）
# =========================
DEFAULT_FACTOR = {"DE":200, "NL":200, "FR":250, "IT":250}
dhl_factor = float(DEFAULT_FACTOR.get(country, 200))

total_real = float((cargo["实重(kg)"] * cargo["数量"]).sum())
total_cbm = float(((cargo["长(cm)"]/100)*(cargo["宽(cm)"]/100)*(cargo["高(cm)"]/100)*cargo["数量"]).sum())
dhl_billable = max(total_real, total_cbm * dhl_factor)

# =========================
# Raben Basis（账单级）
# =========================
raben_name = COUNTRY_NAME_MAP.get(country)
rule = get_raben_rule(df_rules, raben_name) if raben_name else None

if rule is None:
    st.error("Raben：规则表未匹配到该国家（检查 COUNTRY_NAME_MAP / 规则表列名）")
    st.stop()

raben_basis, raben_detail = calc_raben_basis(
    cargo, rule, pack_type,
    round_cbm=round_cbm, round_ldm=round_ldm, round_pp=round_pp, round_basis_kg=round_basis,
    truck_width_m=truck_width,
    use_manual_pp=use_manual, use_manual_ldm=use_manual, use_manual_cbm=use_manual
)

m1, m2 = st.columns(2)
m1.metric("DHL 计费重(kg)", f"{dhl_billable:.2f}")
m2.metric("Raben Basis(kg)（账单级）", f"{raben_basis:.2f}")

with st.expander("查看 Raben Basis 计算明细（用于对账）", expanded=True):
    st.json(raben_detail)

# =========================
# 报价
# =========================
def quote_one(carrier: str):
    if carrier == "DHL":
        df, key_col, wcols = df_dhl, dhl_key_col, dhl_wcols
        key = build_key("DHL", country, prefix)
        billable_w = float(dhl_billable)
        min_charge = float(MIN_CHARGE_DHL)
    else:
        df, key_col, wcols = df_raben, raben_key_col, raben_wcols
        key = build_key("RABEN", country, prefix)
        billable_w = float(raben_basis)
        min_charge = float(MIN_CHARGE_RABEN)

    row = df[df[key_col] == key]
    if row.empty:
        return {"carrier": carrier, "key": key, "found": False, "error": "未找到该线路（PLZ不服务/Zone为空）"}

    weight_col = pick_weight_col(wcols, billable_w)
    base_raw = safe_float(row.iloc[0][weight_col], 0.0)
    base = max(base_raw, min_charge)

    fuel_cost = base * float(fuel_pct) / 100.0

    # insurance
    ins_cost = 0.0
    if carrier == "DHL" and apply_ins_dhl:
        ins_cost = get_dhl_insurance_cost(float(goods_value), country)
    if carrier != "DHL" and apply_ins_raben:
        ins_cost = get_raben_insurance_cost(float(goods_value))

    marpol_cost = 0.0
    if carrier == "DHL" and apply_marpol and country in MARPOL_COUNTRIES:
        marpol_cost = base * 0.04

    total = base + fuel_cost + marpol_cost + ekaer_cost + avisierung_cost + ins_cost

    breakdown = pd.DataFrame([
        ["计费基数(kg)", billable_w],
        ["匹配区间", weight_col],
        ["基础运费", base],
        [f"燃油附加费（{fuel_pct}%）", fuel_cost],
        ["MARPOL", marpol_cost],
        ["EKAER", ekaer_cost],
        ["Avisierung", avisierung_cost],
        ["保险", ins_cost],
        ["总计", total],
    ], columns=["项目", "金额/说明"])

    return {
        "carrier": carrier, "key": key, "found": True,
        "weight_col": weight_col, "base": base, "total": total,
        "breakdown": breakdown
    }

q_dhl = quote_one("DHL")
q_raben = quote_one("RABEN")

st.subheader("报价对比（DHL vs Raben）")

def summary_row(q):
    if not q["found"]:
        return [q["carrier"], q["key"], "❌", "-", "-", "-", q["error"]]
    return [q["carrier"], q["key"], "✅", q["weight_col"], f"{q['base']:.2f}", f"{q['total']:.2f}", ""]

df_compare = pd.DataFrame(
    [summary_row(q_dhl), summary_row(q_raben)],
    columns=["承运商","线路Key","是否命中","匹配区间","基础运费(EUR)","总成本(EUR)","备注"]
)
st.dataframe(df_compare, use_container_width=True)

tab1, tab2, tab3 = st.tabs(["Cargo", "DHL 明细", "Raben 明细"])
with tab1:
    st.dataframe(cargo, use_container_width=True)
with tab2:
    st.dataframe(q_dhl["breakdown"] if q_dhl.get("found") else pd.DataFrame([["error", q_dhl["error"]]]),
                 use_container_width=True)
with tab3:
    st.dataframe(q_raben["breakdown"] if q_raben.get("found") else pd.DataFrame([["error", q_raben["error"]]]),
                 use_container_width=True)

def to_excel_bytes():
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        cargo.to_excel(writer, index=False, sheet_name="Cargo")
        df_compare.to_excel(writer, index=False, sheet_name="Compare")
        if q_dhl.get("found"):
            q_dhl["breakdown"].to_excel(writer, index=False, sheet_name="DHL_Cost")
        if q_raben.get("found"):
            q_raben["breakdown"].to_excel(writer, index=False, sheet_name="Raben_Cost")
    return output.getvalue()

st.download_button(
    "下载Excel（Cargo + DHL + Raben + Compare）",
    data=to_excel_bytes(),
    file_name=f"Freight_Compare_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
)
