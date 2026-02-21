import streamlit as st
import pandas as pd
import re
from io import BytesIO
from datetime import datetime
import math

st.set_page_config(page_title="Aifuge Freight Cost Engine", layout="wide")

# =========================
# 基础工具函数
# =========================
def normalize_prefix(prefix: str) -> str:
    return re.sub(r"\D", "", str(prefix)).zfill(2)[:2]

def build_key(carrier: str, country: str, prefix: str) -> str:
    return f"{carrier}-{country.upper()}--{normalize_prefix(prefix)}"

def sorted_weight_cols(cols):
    weight_cols = [c for c in cols if str(c).startswith("bis-")]
    return sorted(weight_cols, key=lambda x: int(str(x).split("-")[1]))

def pick_weight_col(weight_cols_sorted, billable_weight: float):
    for c in weight_cols_sorted:
        upper = int(str(c).split("-")[1])
        if billable_weight <= upper:
            return c
    return weight_cols_sorted[-1]

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
# DHL 保险（你现在的版本先保留不动；后续你要我再按29段更新也可以直接替换这里）
# =========================
def get_insurance_cost(value, region):
    table = [
        (500,3.28,3.94,5.25),
        (1000,3.28,3.94,5.25),
        (1500,3.28,3.94,5.25),
        (2000,3.28,3.94,5.25),
        (2500,3.86,4.11,5.25),
        (3000,4.55,4.98,6.14),
        (5000,7.31,8.46,10.78),
        (10000,14.21,17.16,22.38),
        (20000,28.01,34.56,45.58),
        (50000,69.42,86.76,115.18),
        (100000,138.44,173.76,231.18),
    ]
    for limit,de,west,east in table:
        if value <= limit:
            if region=="DE":
                return de
            elif region=="WEST":
                return west
            else:
                return east
    return 0

# =========================
# RABEN：按“立方米及装载米规则.xlsx”计算计费重
# =========================
RABEN_RULES_PATH = "data/Raben 立方米及装载米规则.xlsx"

def _first_int_in_text(s: str):
    if s is None:
        return None
    m = re.search(r"(\d+)", str(s))
    return int(m.group(1)) if m else None

def _first_float_ldm_in_text(s: str):
    # 抓 “2,4 LDM” 或 “1,6 LDM”
    if s is None:
        return None
    m = re.search(r"(\d+[,\.]\d+)\s*LDM", str(s))
    if not m:
        return None
    return float(m.group(1).replace(",", "."))

@st.cache_data(show_spinner=False)
def load_raben_rules():
    df = pd.read_excel(RABEN_RULES_PATH, sheet_name=0)
    # 第一列是 Land: / 行项目；其余列是国家
    # 行名：1 cbm / 1 LDM / 各包装最小值
    df = df.rename(columns={df.columns[0]: "ROW"})
    df["ROW"] = df["ROW"].astype(str).str.strip()
    return df

def get_raben_rule(df_rules: pd.DataFrame, country_name: str):
    """
    从规则表中取出某个国家列，并解析：
      - cbm_kg
      - cbm_ldm_max (例如 2.4 或 1.6；若没有写，就视为始终用cbm规则)
      - ldm_kg (可能为空，比如某些国家表里没写)
      - min_pack_weights：四类包装最低计费重
    """
    if country_name not in df_rules.columns:
        return None

    col = country_name

    cbm_cell = df_rules.loc[df_rules["ROW"].str.lower().eq("1 cbm"), col].values
    cbm_cell = cbm_cell[0] if len(cbm_cell) else None

    ldm_cell = df_rules.loc[df_rules["ROW"].str.lower().eq("1 ldm"), col].values
    ldm_cell = ldm_cell[0] if len(ldm_cell) else None

    cbm_kg = _first_int_in_text(cbm_cell)
    cbm_ldm_max = _first_float_ldm_in_text(cbm_cell)  # “bis 2,4 LDM / …”
    ldm_kg = _first_int_in_text(ldm_cell)

    # 最低计费重（按 packstück 类型）
    def _min_row(label_contains: str):
        r = df_rules.loc[df_rules["ROW"].str.contains(label_contains, case=False, na=False), col]
        if r.empty:
            return None
        return _first_int_in_text(r.values[0])

    min_carton = _min_row("Kartons")
    min_half_pallet = _min_row("Halbpaletten")
    min_euro_pallet = _min_row("Euromaß")
    min_other = _min_row("Sonstige")

    return {
        "cbm_kg": cbm_kg,
        "cbm_ldm_max": cbm_ldm_max,   # None表示没写阈值
        "ldm_kg": ldm_kg,             # None表示没写LDM规则
        "min_pack": {
            "Kartons/Pakete": min_carton,
            "Halbpaletten/klein": min_half_pallet,
            "Euromaß Paletten": min_euro_pallet,
            "Sonstige Paletten": min_other,
        }
    }

def calc_ldm_from_dims_cm(length_cm: float, width_cm: float, qty: float) -> float:
    """
    常用估算：LDM = footprint_area(m²) / 2.4
    footprint_area = L * W（不考虑叠放）
    """
    l = max(float(length_cm), 0.0) / 100.0
    w = max(float(width_cm), 0.0) / 100.0
    q = max(float(qty), 0.0)
    area = l * w * q
    return area / 2.4 if area > 0 else 0.0

def raben_billable_weight(data: pd.DataFrame, rule: dict, pack_type: str):
    """
    按规则表：
      - 先算总实重
      - 再算总CBM、总LDM
      - 根据阈值判断：用CBM折算 or 用LDM折算
      - 同时套用 packstück 最低计费重
      - 返回：计费重(kg)、以及用于展示的明细
    """
    # totals
    total_real = float((data["实重(kg)"] * data["数量"]).sum())
    total_cbm = float(((data["长(cm)"]/100) * (data["宽(cm)"]/100) * (data["高(cm)"]/100) * data["数量"]).sum())
    total_ldm = float(data.apply(lambda r: calc_ldm_from_dims_cm(r["长(cm)"], r["宽(cm)"], r["数量"]), axis=1).sum())

    cbm_kg = rule.get("cbm_kg") or 0
    ldm_kg = rule.get("ldm_kg") or 0
    cbm_ldm_max = rule.get("cbm_ldm_max")  # None 或数字

    # 按阈值选择“主要折算方式”
    # - 若表里写了阈值，并且总LDM > 阈值：优先按LDM折算（若该国没有LDM规则，则退回CBM）
    # - 否则：按CBM折算
    if cbm_ldm_max is not None and total_ldm > float(cbm_ldm_max) and ldm_kg > 0:
        conv_weight = total_ldm * ldm_kg
        conv_basis = f"LDM × {ldm_kg}"
    else:
        conv_weight = total_cbm * cbm_kg
        conv_basis = f"CBM × {cbm_kg}"

    # 最低计费重（按 packstück）
    min_per_piece = rule["min_pack"].get(pack_type)
    if min_per_piece is None:
        min_per_piece = 0
    total_min = float((data["数量"].sum()) * float(min_per_piece))

    billable = max(total_real, conv_weight, total_min)

    detail = {
        "total_real": total_real,
        "total_cbm": total_cbm,
        "total_ldm": total_ldm,
        "conv_basis": conv_basis,
        "conv_weight": float(conv_weight),
        "min_per_piece": float(min_per_piece),
        "total_min": total_min,
        "billable": float(billable),
    }
    return billable, detail

# =========================
# 读取运价表
# =========================
DHL_PATH   = "data/Frachtkosten DHL Freight EU Neu.xlsx"
RABEN_PATH = "data/Raben_Frachtkosten_FINAL_filled.xlsx"

@st.cache_data(show_spinner=False)
def load_dhl():
    df = pd.read_excel(DHL_PATH, sheet_name="Frachtkosten DHL Freight")
    first_col = df.columns[0]
    wcols = sorted_weight_cols(df.columns)
    return df, first_col, wcols

@st.cache_data(show_spinner=False)
def load_raben():
    df = pd.read_excel(RABEN_PATH, sheet_name=0)
    first_col = df.columns[0]
    wcols = sorted_weight_cols(df.columns)
    return df, first_col, wcols

df_dhl, dhl_key_col, dhl_wcols = load_dhl()
df_raben, raben_key_col, raben_wcols = load_raben()
df_raben_rules = load_raben_rules()

# =========================
# UI
# =========================
st.title("Aifuge GmbH | Freight Cost Engine (DHL + Raben)")

col1, col2 = st.columns(2)
with col1:
    country = st.text_input("目的地国家代码", value="DE").upper()
with col2:
    prefix = st.text_input("邮编前2位", value="38")

st.subheader("货物明细（输入左侧，右侧自动计算）")

base_df = pd.DataFrame([{"数量":1,"长(cm)":60,"宽(cm)":40,"高(cm)":40,"实重(kg)":20}])
data = st.data_editor(base_df, num_rows="dynamic", use_container_width=True)
data = data.fillna(0)

# 基础派生字段（通用展示）
data["体积(m³)"] = (data["长(cm)"]/100)*(data["宽(cm)"]/100)*(data["高(cm)"]/100)*data["数量"]
data["LDM(估算)"] = data.apply(lambda r: calc_ldm_from_dims_cm(r["长(cm)"], r["宽(cm)"], r["数量"]), axis=1)
data["实重合计(kg)"] = data["实重(kg)"] * data["数量"]

total_real_w = float(data["实重合计(kg)"].sum())
total_cbm = float(data["体积(m³)"].sum())
total_ldm = float(data["LDM(估算)"].sum())

c1, c2, c3 = st.columns(3)
c1.metric("总实重(kg)", f"{total_real_w:.2f}")
c2.metric("总体积(m³)", f"{total_cbm:.4f}")
c3.metric("总LDM(估算)", f"{total_ldm:.3f}")

st.divider()

st.subheader("附加费 & 参数")
colA, colB, colC = st.columns(3)
with colA:
    diesel_price = st.number_input("柴油价格 Cent/L（用于燃油附加费）", value=185.0)
with colB:
    MIN_CHARGE_DHL = st.number_input("DHL 最低收费 EUR", value=0.0)
with colC:
    MIN_CHARGE_RABEN = st.number_input("Raben 最低收费 EUR", value=0.0)

fuel_pct = get_diesel_surcharge_percent(float(diesel_price))

avisierung = st.checkbox("Avisierung 11€（示例：后续可按承运商拆分）")
avisierung_cost = 11 if avisierung else 0

cargo_value = st.number_input("货值 EUR（用于保险）", value=0.0)
insurance_cost = get_insurance_cost(float(cargo_value), "DE")

MARPOL_COUNTRIES = ["DK","EE","FI","GB","IE","LT","LV","NO","SE"]
ekaer_cost = 10 if country == "HU" else 0

# Raben 包装类型（用于最低计费重）
st.subheader("Raben 计费重规则参数")
pack_type = st.selectbox(
    "包装类型（用于最低计费重：Mindestabrechnungsgewichte）",
    ["Kartons/Pakete", "Halbpaletten/klein", "Euromaß Paletten", "Sonstige Paletten"],
    index=2
)

# =========================
# 计费重：DHL 与 Raben 分开算
# =========================
# DHL：仍用你当前体积重系数逻辑（后续你若要也可改成DHL按LDM/CBM）
DEFAULT_FACTOR = {"DE":200, "NL":200, "FR":250, "IT":250}
dhl_factor = DEFAULT_FACTOR.get(country, 200)

dhl_billable = max(total_real_w, total_cbm * dhl_factor)
MIN_BILLABLE_DHL = st.number_input("DHL 最低计费重量(kg)", value=0.0)
dhl_billable = max(dhl_billable, float(MIN_BILLABLE_DHL))

# Raben：按规则表
# 规则表用“国家名称”列（德语），你可以按需扩展映射
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
    "HR": "Kroatien",
    "HU": "Ungarn",
    "IE": "Irland",
    "IT": "Italien",
    "LT": "Litauen",
    "LU": "Luxemburg",
    "LV": "Lettland",
    "NL": "Niederlande",
    "PL": "Polen",
    "PT": "Portugal",
    "RO": "Rumänien",
    "SE": "Schweden",
    "SI": "Slowwenien",
    "SK": "Slowakai",
}
raben_country_name = COUNTRY_NAME_MAP.get(country)

raben_rule = get_raben_rule(df_raben_rules, raben_country_name) if raben_country_name else None
if raben_rule is None:
    st.warning("未找到该国家的Raben规则（规则表列名/映射缺失）。Raben计费重将暂时回退为：max(实重, CBM×200)。")
    raben_billable = max(total_real_w, total_cbm * 200)
    raben_detail = None
else:
    raben_billable, raben_detail = raben_billable_weight(data, raben_rule, pack_type)

MIN_BILLABLE_RABEN = st.number_input("Raben 最低计费重量(kg)（额外兜底）", value=0.0)
raben_billable = max(float(raben_billable), float(MIN_BILLABLE_RABEN))

colx, coly = st.columns(2)
with colx:
    st.metric("DHL 计费重(kg)", f"{dhl_billable:.2f}")
with coly:
    st.metric("Raben 计费重(kg)（按规则表）", f"{raben_billable:.2f}")

if raben_detail:
    with st.expander("查看 Raben 计费重计算明细"):
        st.write(raben_detail)

st.divider()

# =========================
# 报价：分别按各自计费重匹配各自运价表
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
        billable_w = float(raben_billable)
        min_charge = float(MIN_CHARGE_RABEN)

    row = df[df[key_col] == key]
    if row.empty:
        return {
            "carrier": carrier, "key": key, "found": False,
            "error": "未找到该线路（可能该PLZ不服务/Zone为空已被剔除）"
        }

    weight_col = pick_weight_col(wcols, billable_w)
    base = float(row.iloc[0][weight_col])

    base_after_min = max(base, min_charge)
    fuel_cost = base * float(fuel_pct) / 100.0

    marpol_cost = base * 0.04 if (carrier == "DHL" and country in MARPOL_COUNTRIES) else 0.0
    ekaer = float(ekaer_cost) if carrier == "DHL" else 0.0

    total = base_after_min + fuel_cost + marpol_cost + ekaer + float(avisierung_cost) + float(insurance_cost)

    breakdown = pd.DataFrame([
        ["计费重(kg)", billable_w],
        ["匹配区间", weight_col],
        ["基础运费", base_after_min],
        ["燃油附加费", fuel_cost],
        ["MARPOL", marpol_cost],
        ["EKAER", ekaer],
        ["Avisierung", float(avisierung_cost)],
        ["保险", float(insurance_cost)],
        ["总计", total],
    ], columns=["项目", "金额/说明"])

    return {
        "carrier": carrier,
        "key": key,
        "found": True,
        "weight_col": weight_col,
        "base": base_after_min,
        "total": total,
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

tab1, tab2 = st.tabs(["DHL 明细", "Raben 明细"])
with tab1:
    if q_dhl["found"]:
        st.dataframe(q_dhl["breakdown"], use_container_width=True)
    else:
        st.error(q_dhl["error"])

with tab2:
    if q_raben["found"]:
        st.dataframe(q_raben["breakdown"], use_container_width=True)
    else:
        st.error(q_raben["error"])

# =========================
# 导出Excel
# =========================
def to_excel():
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        data.to_excel(writer, index=False, sheet_name="Cargo")
        df_compare.to_excel(writer, index=False, sheet_name="Compare")
        if q_dhl.get("found"):
            q_dhl["breakdown"].to_excel(writer, index=False, sheet_name="DHL_Cost")
        if q_raben.get("found"):
            q_raben["breakdown"].to_excel(writer, index=False, sheet_name="Raben_Cost")
    return output.getvalue()

st.download_button(
    "下载Excel（Cargo + DHL + Raben + Compare）",
    data=to_excel(),
    file_name=f"Freight_Compare_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
)
