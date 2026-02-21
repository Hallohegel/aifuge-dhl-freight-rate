import streamlit as st
import pandas as pd
import re
from io import BytesIO
from datetime import datetime

st.set_page_config(page_title="Aifuge Freight Cost Engine", layout="wide")

# =========================
# 路径配置（请确保仓库 data/ 下文件名一致）
# =========================
DHL_PATH     = "data/Frachtkosten DHL Freight EU Neu.xlsx"
RABEN_PATH   = "data/Raben_Frachtkosten_FINAL_filled.xlsx"
SCHENKER_PATH= "data/Schenker_Frachtkosten.xlsx"
MAUT_PATH    = "data/Mauttabelle Stand 01.07.2024 (1).xlsx"

# =========================
# 工具函数
# =========================
def normalize_prefix(prefix):
    return re.sub(r"\D", "", str(prefix)).zfill(2)[:2]

def build_key(carrier, country, prefix):
    return f"{carrier}-{country.upper()}--{normalize_prefix(prefix)}"

def volumetric_weight(l, w, h, factor):
    return (l/100) * (w/100) * (h/100) * factor

def sorted_weight_cols(cols):
    weight_cols = [c for c in cols if str(c).startswith("bis-")]
    return sorted(weight_cols, key=lambda x: int(str(x).split("-")[1]))

def pick_weight_col(weight_cols_sorted, billable_weight):
    for c in weight_cols_sorted:
        upper = int(str(c).split("-")[1])
        if billable_weight <= upper:
            return c
    return weight_cols_sorted[-1]

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

# DHL 保险表（你给的完整版本：500~100000，三大区域）
def get_dhl_insurance_cost(value, region):
    """
    region: 'DE' / 'WEST' / 'EAST'
    value: EUR
    """
    table = [
        (500,   3.28,  3.94,  5.25),
        (1000,  3.28,  3.94,  5.25),
        (1500,  3.28,  3.94,  5.25),
        (2000,  3.28,  3.94,  5.25),
        (2500,  3.86,  4.11,  5.25),
        (3000,  4.55,  4.98,  6.14),
        (3500,  5.24,  5.85,  7.30),
        (4000,  5.93,  6.72,  8.46),
        (4500,  6.62,  7.59,  9.62),
        (5000,  7.31,  8.46, 10.78),
        (5500,  8.00,  9.33, 11.94),
        (6000,  8.69, 10.20, 13.10),
        (6500,  9.38, 11.07, 14.26),
        (7000, 10.07, 11.94, 15.42),
        (7500, 10.76, 12.81, 16.58),
        (8000, 11.45, 13.68, 17.74),
        (8500, 12.14, 14.55, 18.90),
        (9000, 12.83, 15.42, 20.06),
        (9500, 13.52, 16.29, 21.22),
        (10000,14.21, 17.16, 22.38),
        (15000,21.11, 25.86, 33.98),
        (20000,28.01, 34.56, 45.58),
        (25000,34.91, 43.26, 57.18),
        (30000,41.82, 51.96, 68.78),
        (35000,48.72, 60.66, 80.38),
        (40000,55.62, 69.36, 91.98),
        (45000,62.52, 78.06,103.58),
        (50000,69.42, 86.76,115.18),
        (100000,138.44,173.76,231.18),
    ]
    for limit, de, west, east in table:
        if value <= limit:
            if region == "DE":
                return float(de)
            elif region == "WEST":
                return float(west)
            else:
                return float(east)
    return 0.0

# =========================
# 读取报价表（DHL / Raben / Schenker）
# =========================
@st.cache_data(show_spinner=False)
def load_rate_table(path, sheet_name=0):
    df = pd.read_excel(path, sheet_name=sheet_name)
    key_col = df.columns[0]
    wcols = sorted_weight_cols(df.columns)
    return df, key_col, wcols

# =========================
# 读取 Maut 表：自动识别“距离区间两行 + 重量区间矩阵”
# =========================
@st.cache_data(show_spinner=False)
def load_maut_table():
    df = pd.read_excel(MAUT_PATH, sheet_name="Mauttabelle", header=None)

    def is_num(x):
        try:
            float(x)
            return True
        except:
            return False

    # 1) 找到两行“km_low / km_high”
    km_low_row = None
    km_high_row = None

    for r in range(0, min(80, df.shape[0]-1)):
        row1 = df.iloc[r, 3:].tolist()
        row2 = df.iloc[r+1, 3:].tolist()

        nums1 = [float(x) for x in row1 if is_num(x)]
        nums2 = [float(x) for x in row2 if is_num(x)]

        # 要求两行都有足够多的数字，且长度接近
        if len(nums1) >= 6 and len(nums2) >= 6 and abs(len(nums1)-len(nums2)) <= 2:
            # 再要求大概率是递增区间
            if all(nums1[i] <= nums1[i+1] for i in range(len(nums1)-1)) and all(nums2[i] <= nums2[i+1] for i in range(len(nums2)-1)):
                km_low_row = r
                km_high_row = r + 1
                break

    if km_low_row is None:
        raise ValueError("无法识别 Maut 表的距离区间行（km_low/km_high），请检查表格格式。")

    km_low  = df.iloc[km_low_row,  3:].dropna().astype(float).tolist()
    km_high = df.iloc[km_high_row, 3:].dropna().astype(float).tolist()
    km_cols = len(km_low)

    # 2) 距离行后面开始找重量区间（通常在第2/3列）
    start = None
    w_low = []
    w_high = []
    values = []

    for r in range(km_high_row+1, df.shape[0]):
        if is_num(df.iloc[r, 1]) and is_num(df.iloc[r, 2]):
            start = r
            break

    if start is None:
        raise ValueError("无法识别 Maut 表的重量区间起始行（w_low/w_high）。")

    for r in range(start, df.shape[0]):
        if not (is_num(df.iloc[r, 1]) and is_num(df.iloc[r, 2])):
            break
        w_low.append(float(df.iloc[r, 1]))
        w_high.append(float(df.iloc[r, 2]))
        row_vals = df.iloc[r, 3:3+km_cols].tolist()
        row_vals = [float(x) if is_num(x) else 0.0 for x in row_vals]
        values.append(row_vals)

    if not w_low or not values:
        raise ValueError("Maut 表解析失败：未获取到重量区间/矩阵。")

    return km_low, km_high, w_low, w_high, values

def maut_cost(weight_kg: float, distance_km: float) -> float:
    km_low, km_high, w_low, w_high, values = load_maut_table()

    if weight_kg <= 0 or distance_km <= 0:
        return 0.0

    # 找重量区间
    wi = None
    for i, (lo, hi) in enumerate(zip(w_low, w_high)):
        if lo <= weight_kg <= hi:
            wi = i
            break
    if wi is None:
        wi = len(w_low) - 1  # 超过最大重量：先按最后一档

    # 找距离区间
    ki = None
    for j, (lo, hi) in enumerate(zip(km_low, km_high)):
        if lo <= distance_km <= hi:
            ki = j
            break
    if ki is None:
        ki = len(km_low) - 1  # 超过最大距离：先按最后一档

    return float(values[wi][ki])

# =========================
# 加载三家报价表
# =========================
try:
    df_dhl, dhl_key_col, dhl_wcols = load_rate_table(DHL_PATH, sheet_name="Frachtkosten DHL Freight")
except Exception as e:
    st.error(f"读取 DHL 报价失败：{e}")
    st.stop()

try:
    df_raben, raben_key_col, raben_wcols = load_rate_table(RABEN_PATH, sheet_name=0)
except Exception as e:
    st.error(f"读取 Raben 报价失败：{e}")
    st.stop()

try:
    df_schenker, schenker_key_col, schenker_wcols = load_rate_table(SCHENKER_PATH, sheet_name=0)
except Exception as e:
    st.error(f"读取 Schenker 报价失败：{e}")
    st.stop()

# =========================
# 页面开始
# =========================
st.title("Aifuge GmbH | Freight Cost Engine (DHL + Raben + Schenker)")

# 线路输入
col1, col2 = st.columns(2)
with col1:
    country = st.text_input("目的地国家代码", value="DE").upper()
with col2:
    prefix = st.text_input("邮编前2位", value="38")

# 泡重系数（你现在的统一方式：体积重=体积*factor）
DEFAULT_FACTOR = {"DE":200, "NL":200, "FR":250, "IT":250}
factor_default = DEFAULT_FACTOR.get(country, 200)

manual_factor = st.checkbox("手动修改泡重系数")
if manual_factor:
    factor = st.number_input("泡重系数 kg/m³", value=float(factor_default))
else:
    factor = factor_default
    st.info(f"当前泡重系数：{factor} kg/m³")

# =========================
# 货物输入（自动计算体积/体积重/计费重）
# =========================
st.subheader("货物明细（输入左侧，右侧自动计算）")

base_df = pd.DataFrame([{"数量":1, "长(cm)":60, "宽(cm)":40, "高(cm)":40, "实重(kg)":20}])
data = st.data_editor(base_df, num_rows="dynamic", use_container_width=True)

need_cols = ["数量","长(cm)","宽(cm)","高(cm)","实重(kg)"]
for c in need_cols:
    if c not in data.columns:
        st.error("货物表字段缺失，请刷新页面。")
        st.stop()
data = data.fillna(0)

# 自动计算
data["体积(m³)"] = (data["长(cm)"]/100)*(data["宽(cm)"]/100)*(data["高(cm)"]/100)*data["数量"]
data["体积重(kg/件)"] = data.apply(lambda r: volumetric_weight(r["长(cm)"], r["宽(cm)"], r["高(cm)"], factor), axis=1)
data["计费重(kg/件)"] = data[["实重(kg)", "体积重(kg/件)"]].max(axis=1)
data["实重合计(kg)"] = data["实重(kg)"] * data["数量"]
data["计费重合计(kg)"] = data["计费重(kg/件)"] * data["数量"]

total_real_w = float(data["实重合计(kg)"].sum())
total_volume = float(data["体积(m³)"].sum())
total_vol_w  = float(total_volume * factor)
total_charge_w_raw = float(data["计费重合计(kg)"].sum())

c1, c2, c3, c4 = st.columns(4)
c1.metric("总实重(kg)", f"{total_real_w:.2f}")
c2.metric("总体积(m³)", f"{total_volume:.4f}")
c3.metric("总体积重(kg)", f"{total_vol_w:.2f}")
c4.metric("总计费重(kg)", f"{total_charge_w_raw:.2f}")

MIN_BILLABLE = st.number_input("最低计费重量(kg)", value=0.0)
total_charge_w = max(total_charge_w_raw, float(MIN_BILLABLE))

st.divider()

# =========================
# 通用/承运商参数
# =========================
st.subheader("参数 & 附加费")

# DHL/Raben：燃油以你原表逻辑（柴油 Cent/L -> 百分比）
colA, colB, colC = st.columns(3)
with colA:
    diesel_price = st.number_input("柴油价格 Cent/L（DHL/Raben 燃油）", value=185.0)
with colB:
    MIN_CHARGE_DHL = st.number_input("DHL 最低收费 EUR", value=0.0)
with colC:
    MIN_CHARGE_RABEN = st.number_input("Raben 最低收费 EUR", value=0.0)

fuel_pct = get_diesel_surcharge_percent(diesel_price)

# DHL保险（仅 DHL 用）
colI1, colI2 = st.columns(2)
with colI1:
    cargo_value = st.number_input("货值 EUR（DHL 保险用）", value=0.0)
with colI2:
    dhl_ins_region = st.selectbox("DHL 保险区域", ["DE", "WEST", "EAST"], index=0)

use_dhl_insurance = st.checkbox("启用 DHL 运输保险", value=False)
insurance_cost = get_dhl_insurance_cost(float(cargo_value), dhl_ins_region) if use_dhl_insurance else 0.0

# DHL特有（你原来逻辑示例）
MARPOL_COUNTRIES = ["DK","EE","FI","GB","IE","LT","LV","NO","SE"]
ekaer_cost = 10 if country == "HU" else 0

avisierung_dhl = st.checkbox("DHL Avisierung 11€（示例项）", value=False)
avisierung_cost_dhl = 11.0 if avisierung_dhl else 0.0

st.divider()

# =========================
# Schenker 参数：KM手动 + Floating手动 + Avis(电话预约) 20€ /票
# =========================
st.subheader("DB Schenker / DSV（先上线：KM手动输入）")

colS1, colS2, colS3, colS4 = st.columns(4)
with colS1:
    schenker_km = st.number_input("Schenker 距离 Distance (km)", min_value=0.0, value=0.0, step=1.0)
with colS2:
    maut_weight_basis = st.selectbox("Maut 用哪个重量？", ["总计费重(kg)", "总实重(kg)"], index=0)
with colS3:
    schenker_floating_pct = st.number_input("Schenker Floating(%)【手动】", min_value=0.0, max_value=100.0, value=8.5, step=0.1)
with colS4:
    schenker_avis = st.checkbox("Schenker Avis 电话预约派送(20€ /票)", value=False)

schenker_avis_cost = 20.0 if schenker_avis else 0.0

# =========================
# 报价核心函数
# =========================
def quote_dhl():
    key = build_key("DHL", country, prefix)
    row = df_dhl[df_dhl[dhl_key_col] == key]
    if row.empty:
        return {"carrier":"DHL","key":key,"found":False,"error":"未找到该线路"}

    weight_col = pick_weight_col(dhl_wcols, total_charge_w)
    base = float(row.iloc[0][weight_col])

    base_after_min = max(base, float(MIN_CHARGE_DHL))
    fuel_cost = base * fuel_pct / 100.0
    marpol_cost = base * 0.04 if country in MARPOL_COUNTRIES else 0.0
    ekaer = float(ekaer_cost)
    total = base_after_min + fuel_cost + marpol_cost + ekaer + float(avisierung_cost_dhl) + float(insurance_cost)

    breakdown = pd.DataFrame([
        ["基础运费", base_after_min],
        ["燃油附加费", fuel_cost],
        ["MARPOL", marpol_cost],
        ["EKAER", ekaer],
        ["Avisierung", float(avisierung_cost_dhl)],
        ["保险", float(insurance_cost)],
        ["总计", total],
    ], columns=["项目","金额(EUR)"])

    return {
        "carrier":"DHL","key":key,"found":True,
        "weight_col":weight_col,"base":base_after_min,"total":total,
        "breakdown":breakdown
    }

def quote_raben():
    key = build_key("RABEN", country, prefix)
    row = df_raben[df_raben[raben_key_col] == key]
    if row.empty:
        return {"carrier":"RABEN","key":key,"found":False,"error":"未找到该线路（可能该PLZ不服务/Zone为空已剔除）"}

    weight_col = pick_weight_col(raben_wcols, total_charge_w)
    base = float(row.iloc[0][weight_col])

    base_after_min = max(base, float(MIN_CHARGE_RABEN))
    fuel_cost = base * fuel_pct / 100.0

    # Raben：你后面要做“账单级精确匹配系统”时，这里会按你的规则表替换计费重+附加费
    total = base_after_min + fuel_cost

    breakdown = pd.DataFrame([
        ["基础运费", base_after_min],
        ["燃油附加费", fuel_cost],
        ["总计", total],
    ], columns=["项目","金额(EUR)"])

    return {
        "carrier":"RABEN","key":key,"found":True,
        "weight_col":weight_col,"base":base_after_min,"total":total,
        "breakdown":breakdown
    }

def quote_schenker():
    # 你的系统key前缀建议用 SCHENKER（如果你Excel里不是这个前缀，请改这里）
    key = build_key("SCHENKER", country, prefix)
    row = df_schenker[df_schenker[schenker_key_col] == key]
    if row.empty:
        return {"carrier":"SCHENKER","key":key,"found":False,"error":"未找到该线路"}

    weight_col = pick_weight_col(schenker_wcols, total_charge_w)
    base = float(row.iloc[0][weight_col])

    maut_weight = total_charge_w if maut_weight_basis == "总计费重(kg)" else total_real_w
    maut_fee = maut_cost(maut_weight, float(schenker_km))

    floating_fee = base * (float(schenker_floating_pct) / 100.0)

    total = base + maut_fee + floating_fee + float(schenker_avis_cost)

    breakdown = pd.DataFrame([
        ["基础运费", base],
        ["Maut", maut_fee],
        ["Floating", floating_fee],
        ["Avis电话预约", float(schenker_avis_cost)],
        ["总计", total],
    ], columns=["项目","金额(EUR)"])

    return {
        "carrier":"SCHENKER","key":key,"found":True,
        "weight_col":weight_col,"base":base,"total":total,
        "maut_weight":maut_weight,"km":float(schenker_km),
        "breakdown":breakdown
    }

# =========================
# 计算三家报价
# =========================
q_dhl = quote_dhl()
q_raben = quote_raben()
q_schenker = quote_schenker()

# =========================
# 结果展示：同屏对比
# =========================
st.subheader("报价对比（DHL vs Raben vs Schenker）")

def summary_row(q):
    if not q.get("found"):
        return [q.get("carrier"), q.get("key"), "❌", "-", "-", "-", q.get("error","")]
    return [
        q["carrier"], q["key"], "✅", q["weight_col"],
        f"{q['base']:.2f}", f"{q['total']:.2f}", ""
    ]

df_compare = pd.DataFrame(
    [summary_row(q_dhl), summary_row(q_raben), summary_row(q_schenker)],
    columns=["承运商","线路Key","是否命中","匹配区间","基础运费(EUR)","总成本(EUR)","备注"]
)
st.dataframe(df_compare, use_container_width=True)

tab1, tab2, tab3 = st.tabs(["DHL 明细", "Raben 明细", "Schenker 明细"])
with tab1:
    if q_dhl.get("found"):
        st.dataframe(q_dhl["breakdown"], use_container_width=True)
    else:
        st.error(q_dhl.get("error",""))

with tab2:
    if q_raben.get("found"):
        st.dataframe(q_raben["breakdown"], use_container_width=True)
    else:
        st.error(q_raben.get("error",""))

with tab3:
    if q_schenker.get("found"):
        st.dataframe(q_schenker["breakdown"], use_container_width=True)
        st.caption(f"Maut 查表：重量={q_schenker.get('maut_weight',0):.2f} kg，距离={q_schenker.get('km',0):.0f} km")
    else:
        st.error(q_schenker.get("error",""))

# =========================
# 导出Excel（Cargo + 三家明细 + 对比）
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
        if q_schenker.get("found"):
            q_schenker["breakdown"].to_excel(writer, index=False, sheet_name="Schenker_Cost")

    return output.getvalue()

st.download_button(
    "下载Excel（Cargo + DHL + Raben + Schenker + Compare）",
    data=to_excel(),
    file_name=f"Freight_Compare_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
)
