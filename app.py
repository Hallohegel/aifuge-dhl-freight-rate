import streamlit as st
import pandas as pd
import re
from io import BytesIO
from datetime import datetime

st.set_page_config(page_title="Aifuge Freight Cost Engine", layout="wide")

# =========================
# 工具函数
# =========================
def normalize_prefix(prefix):
    return re.sub(r"\D", "", str(prefix)).zfill(2)[:2]

def build_key(carrier, country, prefix):
    return f"{carrier}-{country.upper()}--{normalize_prefix(prefix)}"

def volumetric_weight(l, w, h, factor):
    return (l/100) * (w/100) * (h/100) * factor

def get_diesel_surcharge_percent(price_cent):
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

def sorted_weight_cols(cols):
    # cols like bis-30, bis-50, ...
    weight_cols = [c for c in cols if str(c).startswith("bis-")]
    return sorted(weight_cols, key=lambda x: int(str(x).split("-")[1]))

def pick_weight_col(weight_cols_sorted, billable_weight):
    for c in weight_cols_sorted:
        upper = int(str(c).split("-")[1])
        if billable_weight <= upper:
            return c
    return weight_cols_sorted[-1]

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
    # 我给你的 Raben_Frachtkosten_FINAL_filled.xlsx：默认第一列是“国家+邮编前缀”key
    df = pd.read_excel(RABEN_PATH, sheet_name=0)
    first_col = df.columns[0]
    wcols = sorted_weight_cols(df.columns)
    return df, first_col, wcols

try:
    df_dhl, dhl_key_col, dhl_wcols = load_dhl()
except Exception as e:
    st.error(f"读取DHL报价失败：{e}")
    st.stop()

try:
    df_raben, raben_key_col, raben_wcols = load_raben()
except Exception as e:
    st.error(f"读取Raben报价失败：{e}")
    st.stop()

# =========================
# 页面开始
# =========================
st.title("Aifuge GmbH | Freight Cost Engine (DHL + Raben)")

col1, col2 = st.columns(2)
with col1:
    country = st.text_input("目的地国家代码", value="DE").upper()
with col2:
    prefix = st.text_input("邮编前2位", value="38")

# =========================
# 泡重规则
# =========================
DEFAULT_FACTOR = {"DE":200,"NL":200,"FR":250,"IT":250}
factor_default = DEFAULT_FACTOR.get(country,200)

manual_factor = st.checkbox("手动修改泡重系数")
if manual_factor:
    factor = st.number_input("泡重系数 kg/m³", value=float(factor_default))
else:
    factor = factor_default
    st.info(f"当前泡重系数：{factor} kg/m³")

# =========================
# 货物输入：自动算体积/体积重/计费重 + SUM
# =========================
st.subheader("货物明细（输入左侧，右侧自动计算）")

base_df = pd.DataFrame([{"数量":1,"长(cm)":60,"宽(cm)":40,"高(cm)":40,"实重(kg)":20}])
data = st.data_editor(base_df, num_rows="dynamic", use_container_width=True)

# 防止空表/空行
for c in ["数量","长(cm)","宽(cm)","高(cm)","实重(kg)"]:
    if c not in data.columns:
        st.error("货物表字段缺失，请刷新页面。")
        st.stop()
data = data.fillna(0)

# 右侧自动计算
data["体积(m³)"] = (data["长(cm)"]/100)*(data["宽(cm)"]/100)*(data["高(cm)"]/100)*data["数量"]
data["体积重(kg/件)"] = data.apply(lambda r: volumetric_weight(r["长(cm)"],r["宽(cm)"],r["高(cm)"],factor), axis=1)
data["计费重(kg/件)"] = data[["实重(kg)","体积重(kg/件)"]].max(axis=1)
data["实重合计(kg)"] = data["实重(kg)"]*data["数量"]
data["计费重合计(kg)"] = data["计费重(kg/件)"]*data["数量"]

total_real_w   = float(data["实重合计(kg)"].sum())
total_volume   = float(data["体积(m³)"].sum())
total_charge_w_raw = float(data["计费重合计(kg)"].sum())

c1, c2, c3, c4 = st.columns(4)
c1.metric("总实重(kg)", f"{total_real_w:.2f}")
c2.metric("总体积(m³)", f"{total_volume:.4f}")
c3.metric("总体积重(kg)", f"{(total_volume*factor):.2f}")
c4.metric("总计费重(kg)", f"{total_charge_w_raw:.2f}")

MIN_BILLABLE = st.number_input("最低计费重量(kg)", value=0.0)
total_charge_w = max(total_charge_w_raw, float(MIN_BILLABLE))

st.divider()

# =========================
# 附加费（先做一套通用项；以后可按承运商拆开）
# =========================
st.subheader("附加费 & 参数")

colA, colB, colC = st.columns(3)
with colA:
    diesel_price = st.number_input("柴油价格 Cent/L（用于燃油附加费）", value=185.0)
with colB:
    MIN_CHARGE_DHL   = st.number_input("DHL 最低收费 EUR", value=0.0)
with colC:
    MIN_CHARGE_RABEN = st.number_input("Raben 最低收费 EUR", value=0.0)

fuel_pct = get_diesel_surcharge_percent(diesel_price)

avisierung = st.checkbox("Avisierung 11€（仅示例：你可后续按承运商配置）")
avisierung_cost = 11 if avisierung else 0

cargo_value = st.number_input("货值 EUR（用于保险）", value=0.0)
insurance_cost = get_insurance_cost(float(cargo_value), "DE")

# DHL特有示例（你原来逻辑）
MARPOL_COUNTRIES = ["DK","EE","FI","GB","IE","LT","LV","NO","SE"]
ekaer_cost = 10 if country=="HU" else 0

# =========================
# 报价函数：DHL / Raben
# =========================
def quote_one(carrier):
    if carrier == "DHL":
        df, key_col, wcols = df_dhl, dhl_key_col, dhl_wcols
        key = build_key("DHL", country, prefix)
        min_charge = float(MIN_CHARGE_DHL)
    else:
        df, key_col, wcols = df_raben, raben_key_col, raben_wcols
        key = build_key("RABEN", country, prefix)
        min_charge = float(MIN_CHARGE_RABEN)

    row = df[df[key_col] == key]
    if row.empty:
        return {
            "carrier": carrier,
            "key": key,
            "found": False,
            "error": "未找到该线路（可能该PLZ不服务/Zone为空已被剔除）"
        }

    weight_col = pick_weight_col(wcols, total_charge_w)
    base = float(row.iloc[0][weight_col])

    # 最低收费
    base_after_min = max(base, min_charge)

    # 燃油
    fuel_cost = base * fuel_pct / 100.0

    # 这里先复用你原来的附加费逻辑（后续我们会按承运商拆分配置）
    marpol_cost = base * 0.04 if (carrier == "DHL" and country in MARPOL_COUNTRIES) else 0.0
    ekaer = float(ekaer_cost) if carrier == "DHL" else 0.0

    total = base_after_min + fuel_cost + marpol_cost + ekaer + float(avisierung_cost) + float(insurance_cost)

    breakdown = pd.DataFrame([
        ["基础运费", base_after_min],
        ["燃油附加费", fuel_cost],
        ["MARPOL", marpol_cost],
        ["EKAER", ekaer],
        ["Avisierung", float(avisierung_cost)],
        ["保险", float(insurance_cost)],
        ["总计", total],
    ], columns=["项目", "金额(EUR)"])

    return {
        "carrier": carrier,
        "key": key,
        "found": True,
        "weight_col": weight_col,
        "base": base_after_min,
        "fuel_pct": fuel_pct,
        "total": total,
        "breakdown": breakdown
    }

q_dhl = quote_one("DHL")
q_raben = quote_one("RABEN")

# =========================
# 结果展示：同屏对比
# =========================
st.subheader("报价对比（DHL vs Raben）")

def summary_row(q):
    if not q["found"]:
        return [q["carrier"], q["key"], "❌", "-", "-", "-", q["error"]]
    return [
        q["carrier"],
        q["key"],
        "✅",
        q["weight_col"],
        f"{q['base']:.2f}",
        f"{q['total']:.2f}",
        ""
    ]

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
# 导出Excel（Cargo + 两家明细 + 对比）
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
