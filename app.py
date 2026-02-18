import streamlit as st
import pandas as pd
import re
from io import BytesIO
from datetime import datetime

st.set_page_config(page_title="Aifuge DHL Freight Cost Engine", layout="wide")

# =========================
# 工具函数
# =========================

def normalize_prefix(prefix):
    return re.sub(r"\D", "", str(prefix)).zfill(2)[:2]

def build_key(country, prefix):
    return f"DHL-{country.upper()}--{normalize_prefix(prefix)}"

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

# =========================
# 读取运价表
# =========================

default_path = "data/Frachtkosten DHL Freight EU Neu.xlsx"

df_rates = pd.read_excel(default_path, sheet_name="Frachtkosten DHL Freight")
first_col = df_rates.columns[0]

weight_cols = [c for c in df_rates.columns if str(c).startswith("bis-")]
weight_cols_sorted = sorted(weight_cols, key=lambda x: int(x.split("-")[1]))

# =========================
# 页面开始
# =========================

st.title("Aifuge GmbH | DHL Freight Cost Engine")

col1, col2 = st.columns(2)
with col1:
    country = st.text_input("目的地国家代码", value="DE").upper()
with col2:
    prefix = st.text_input("邮编前2位", value="38")

key = build_key(country, prefix)

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
# 货物输入
# =========================

st.subheader("货物明细")

data = st.data_editor(
    pd.DataFrame([{"数量":1,"长(cm)":60,"宽(cm)":40,"高(cm)":40,"实重(kg)":20}]),
    num_rows="dynamic"
)

data["泡重(kg/件)"] = data.apply(lambda r: volumetric_weight(r["长(cm)"],r["宽(cm)"],r["高(cm)"],factor),axis=1)
data["计费重(kg/件)"] = data[["实重(kg)","泡重(kg/件)"]].max(axis=1)
data["计费重(kg)"] = data["计费重(kg/件)"]*data["数量"]
data["体积(m³)"] = (data["长(cm)"]/100)*(data["宽(cm)"]/100)*(data["高(cm)"]/100)*data["数量"]

total_charge_raw = data["计费重(kg)"].sum()
total_volume = data["体积(m³)"].sum()

MIN_BILLABLE = st.number_input("最低计费重量(kg)",value=0.0)
total_charge = max(total_charge_raw,MIN_BILLABLE)

# =========================
# 运费匹配
# =========================

row = df_rates[df_rates[first_col]==key]
if row.empty:
    st.error("未找到该线路")
    st.stop()

weight_col = next((c for c in weight_cols_sorted if total_charge<=int(c.split("-")[1])), weight_cols_sorted[-1])
base_freight = float(row.iloc[0][weight_col])

MIN_CHARGE = st.number_input("最低收费 EUR",value=0.0)
base_after_min = max(base_freight,MIN_CHARGE)

# =========================
# 附加费
# =========================

diesel_price = st.number_input("柴油价格 Cent/L",value=185.0)
fuel_pct = get_diesel_surcharge_percent(diesel_price)
fuel_cost = base_freight*fuel_pct/100

MARPOL_COUNTRIES = ["DK","EE","FI","GB","IE","LT","LV","NO","SE"]
marpol_cost = base_freight*0.04 if country in MARPOL_COUNTRIES else 0

ekaer_cost = 10 if country=="HU" else 0

avisierung = st.checkbox("Avisierung 11€")
avisierung_cost = 11 if avisierung else 0

cargo_value = st.number_input("货值 EUR",value=0.0)
insurance_cost = get_insurance_cost(cargo_value,"DE")

# =========================
# 总成本
# =========================

total_cost = base_after_min + fuel_cost + marpol_cost + ekaer_cost + avisierung_cost + insurance_cost

st.subheader("成本结果")
st.write(f"基础运费: €{base_after_min:.2f}")
st.write(f"燃油附加费: €{fuel_cost:.2f}")
st.write(f"MARPOL: €{marpol_cost:.2f}")
st.write(f"EKAER: €{ekaer_cost:.2f}")
st.write(f"Avisierung: €{avisierung_cost:.2f}")
st.write(f"保险: €{insurance_cost:.2f}")
st.success(f"总成本: €{total_cost:.2f}")

# =========================
# 成本明细表
# =========================

df_breakdown = pd.DataFrame([
    ["基础运费",base_after_min],
    ["燃油附加费",fuel_cost],
    ["MARPOL",marpol_cost],
    ["EKAER",ekaer_cost],
    ["Avisierung",avisierung_cost],
    ["保险",insurance_cost],
    ["总计",total_cost],
],columns=["项目","金额"])

st.dataframe(df_breakdown)

# =========================
# 导出Excel
# =========================

def to_excel():
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df_breakdown.to_excel(writer,index=False,sheet_name="Cost")
        data.to_excel(writer,index=False,sheet_name="Cargo")
    return output.getvalue()

st.download_button("下载Excel",data=to_excel(),
                   file_name=f"DHL_Cost_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx")
