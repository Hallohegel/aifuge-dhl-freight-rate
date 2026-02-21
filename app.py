import streamlit as st
import pandas as pd
import re
from io import BytesIO
from datetime import datetime

st.set_page_config(page_title="Aifuge Freight Cost Engine", layout="wide")

# =========================
# 基础工具
# =========================
def normalize_prefix(prefix: str) -> str:
    return re.sub(r"\D", "", str(prefix)).zfill(2)[:2]

def build_key(carrier: str, country: str, prefix: str) -> str:
    return f"{carrier}-{country.upper()}--{normalize_prefix(prefix)}"

def sorted_weight_cols(cols):
    weight_cols = [c for c in cols if str(c).startswith("bis-")]
    return sorted(weight_cols, key=lambda x: int(str(x).split("-")[1]))

def pick_weight_col(weight_cols_sorted, billable_weight):
    for c in weight_cols_sorted:
        upper = int(str(c).split("-")[1])
        if billable_weight <= upper:
            return c
    return weight_cols_sorted[-1]

def volumetric_weight_kg(l_cm, w_cm, h_cm, factor_kg_per_m3):
    # kg = m3 * factor
    return (l_cm/100) * (w_cm/100) * (h_cm/100) * factor_kg_per_m3

# =========================
# DHL 柴油附加费（你原来的表）
# =========================
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

# =========================
# DHL 保险（29区间段，DE/WEST/EAST）——已按你图更新
# =========================
def dhl_insurance_cost(value_eur: float, region: str) -> float:
    """
    region: "DE" / "WEST" / "EAST"
    """
    # (upper_limit, DE, WEST, EAST)
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
    if value_eur <= 0:
        return 0.0
    for limit, de, west, east in table:
        if value_eur <= limit:
            if region == "DE":
                return float(de)
            if region == "WEST":
                return float(west)
            return float(east)
    # 超过100000，先按最高档（如你后续有新规则再改）
    last = table[-1]
    return float(last[1] if region == "DE" else (last[2] if region == "WEST" else last[3]))

# =========================
# Raben 保险：0.9‰，最低5.95
# =========================
def raben_insurance_cost(value_eur: float) -> float:
    if value_eur <= 0:
        return 0.0
    fee = value_eur * 0.9 / 1000.0
    return max(fee, 5.95)

# =========================
# Schenker/DSV Maut 表读取（固定结构版本）
# =========================
MAUT_PATH = "data/Mauttabelle Stand 01.07.2024 (1).xlsx"

@st.cache_data(show_spinner=False)
def load_maut_table():
    df = pd.read_excel(MAUT_PATH, sheet_name="Mauttabelle", header=None)

    # 固定结构（你这张表的结构）：
    # 第10行：km lower（index=9）
    # 第11行：km upper（index=10）
    # 第12行开始：重量区间 + 矩阵
    km_low  = df.iloc[9,  3:].dropna().astype(float).tolist()
    km_high = df.iloc[10, 3:].dropna().astype(float).tolist()

    def to_num(x):
        try:
            return float(x)
        except:
            return None

    w_low_series  = df.iloc[11:, 1].apply(to_num)
    w_high_series = df.iloc[11:, 2].apply(to_num)

    mask = w_low_series.notna() & w_high_series.notna()
    if not mask.any():
        raise ValueError("Maut表读取失败：未找到重量区间行（列B/C）。请确认sheet名= Mauttabelle 且结构未变。")

    start = w_low_series[mask].index[0]
    end   = w_low_series[mask].index[-1]

    w_low  = w_low_series.loc[start:end].astype(float).tolist()
    w_high = w_high_series.loc[start:end].astype(float).tolist()

    values = df.iloc[start:end+1, 3:3+len(km_low)].astype(float).values
    return km_low, km_high, w_low, w_high, values

def maut_cost(distance_km: float, weight_kg: float) -> float:
    km_low, km_high, w_low, w_high, values = load_maut_table()

    # 找km列
    col_idx = None
    for j, (lo, hi) in enumerate(zip(km_low, km_high)):
        if distance_km >= lo and distance_km <= hi:
            col_idx = j
            break
    if col_idx is None:
        # 超出范围：按最大列
        col_idx = len(km_low) - 1

    # 找重量行
    row_idx = None
    for i, (lo, hi) in enumerate(zip(w_low, w_high)):
        if weight_kg >= lo and weight_kg <= hi:
            row_idx = i
            break
    if row_idx is None:
        # 超出范围：按最大行
        row_idx = len(w_low) - 1

    return float(values[row_idx, col_idx])

# =========================
# 读取报价表
# =========================
DHL_PATH     = "data/Frachtkosten DHL Freight EU Neu.xlsx"
RABEN_PATH   = "data/Raben_Frachtkosten_FINAL_filled.xlsx"
SCHENKER_PATH = "data/Schenker_Frachtkosten.xlsx"

@st.cache_data(show_spinner=False)
def load_ratebook(path, sheet_name=0):
    df = pd.read_excel(path, sheet_name=sheet_name)
    key_col = df.columns[0]
    wcols = sorted_weight_cols(df.columns)
    return df, key_col, wcols

# DHL（固定sheet）
df_dhl = pd.read_excel(DHL_PATH, sheet_name="Frachtkosten DHL Freight")
dhl_key_col = df_dhl.columns[0]
dhl_wcols = sorted_weight_cols(df_dhl.columns)

# Raben / Schenker
df_raben, raben_key_col, raben_wcols = load_ratebook(RABEN_PATH, sheet_name=0)
df_schenk, sch_key_col, sch_wcols = load_ratebook(SCHENKER_PATH, sheet_name=0)

# =========================
# 页面
# =========================
st.title("Aifuge GmbH | Freight Cost Engine (DHL + Raben + Schenker)")

col1, col2 = st.columns(2)
with col1:
    country = st.text_input("目的地国家代码", value="DE").upper()
with col2:
    prefix = st.text_input("邮编前2位", value="38")

# =========================
# 体积重（你原来那套）
# =========================
DEFAULT_FACTOR = {"DE":200, "NL":200, "FR":250, "IT":250}
factor_default = DEFAULT_FACTOR.get(country, 200)

manual_factor = st.checkbox("手动修改泡重系数")
if manual_factor:
    factor = st.number_input("泡重系数 kg/m³", value=float(factor_default))
else:
    factor = factor_default
    st.info(f"当前泡重系数：{factor} kg/m³")

# =========================
# 货物明细（自动计算）
# =========================
st.subheader("货物明细（输入左侧，右侧自动计算）")

base_df = pd.DataFrame([{"数量":1, "长(cm)":60, "宽(cm)":40, "高(cm)":40, "实重(kg)":20}])
data = st.data_editor(base_df, num_rows="dynamic", use_container_width=True)
data = data.fillna(0)

required_cols = ["数量","长(cm)","宽(cm)","高(cm)","实重(kg)"]
for c in required_cols:
    if c not in data.columns:
        st.error("货物表字段缺失，请刷新页面。")
        st.stop()

data["体积(m³)"] = (data["长(cm)"]/100)*(data["宽(cm)"]/100)*(data["高(cm)"]/100)*data["数量"]
data["体积重(kg/件)"] = data.apply(lambda r: volumetric_weight_kg(r["长(cm)"], r["宽(cm)"], r["高(cm)"], factor), axis=1)
data["计费重(kg/件)"] = data[["实重(kg)","体积重(kg/件)"]].max(axis=1)
data["实重合计(kg)"] = data["实重(kg)"]*data["数量"]
data["计费重合计(kg)"] = data["计费重(kg/件)"]*data["数量"]

total_real_w = float(data["实重合计(kg)"].sum())
total_volume = float(data["体积(m³)"].sum())
total_charge_w_raw = float(data["计费重合计(kg)"].sum())
total_vol_w = float(total_volume * factor)

c1, c2, c3, c4 = st.columns(4)
c1.metric("总实重(kg)", f"{total_real_w:.2f}")
c2.metric("总体积(m³)", f"{total_volume:.4f}")
c3.metric("总体积重(kg)", f"{total_vol_w:.2f}")
c4.metric("总计费重(kg)", f"{total_charge_w_raw:.2f}")

MIN_BILLABLE = st.number_input("最低计费重量(kg)", value=0.0)
total_charge_w = max(total_charge_w_raw, float(MIN_BILLABLE))

st.divider()

# =========================
# 参数 & 附加费
# =========================
st.subheader("参数 & 附加费")

colA, colB, colC, colD = st.columns(4)
with colA:
    diesel_price = st.number_input("DHL 柴油价格 Cent/L（用于燃油附加费）", value=185.0)
with colB:
    MIN_CHARGE_DHL = st.number_input("DHL 最低收费 EUR", value=0.0)
with colC:
    MIN_CHARGE_RABEN = st.number_input("Raben 最低收费 EUR", value=0.0)
with colD:
    MIN_CHARGE_SCHENKER = st.number_input("Schenker 最低收费 EUR", value=0.0)

fuel_pct_dhl = get_diesel_surcharge_percent(diesel_price)

# DHL: Avisierung 11
dhl_avis = st.checkbox("DHL Avisierung 11€")
dhl_avis_cost = 11.0 if dhl_avis else 0.0

# DHL 保险区域
dhl_region = st.selectbox("DHL 保险区域", ["DE", "WEST", "EAST"], index=0)

# 货值（用于保险）
cargo_value = st.number_input("货值 EUR（用于保险）", value=0.0)

# Raben保险
raben_insurance_on = st.checkbox("Raben Versicherung（0.9‰，最低5.95€）", value=False)

st.divider()

# =========================
# Schenker / DSV（先上线版：KM手动输入 + Floating手动输入 + Avis 20€）
# =========================
st.subheader("DB Schenker / DSV（先上线：KM手动输入）")

cS1, cS2, cS3, cS4 = st.columns(4)
with cS1:
    sch_distance_km = st.number_input("Schenker 距离 Distance (KM) — 手动输入", value=0.0)
with cS2:
    maut_weight_basis = st.selectbox("Maut 计费重量用哪个？", ["Fra.Gew (计费重)", "Wirk.Gew (实重)"], index=0)
with cS3:
    sch_floating_pct = st.number_input("Schenker Floating(%) — 手动", value=8.5)
with cS4:
    sch_avis = st.checkbox("Schenker Avis 电话预约派送 20€/票", value=False)
sch_avis_cost = 20.0 if sch_avis else 0.0

# =========================
# 报价核心
# =========================
MARPOL_COUNTRIES = ["DK","EE","FI","GB","IE","LT","LV","NO","SE"]

def quote_dhl():
    key = build_key("DHL", country, prefix)
    row = df_dhl[df_dhl[dhl_key_col] == key]
    if row.empty:
        return {"carrier":"DHL", "key":key, "found":False, "error":"未找到该线路"}

    weight_col = pick_weight_col(dhl_wcols, total_charge_w)
    base = float(row.iloc[0][weight_col])

    base_after_min = max(base, float(MIN_CHARGE_DHL))
    fuel_cost = base * fuel_pct_dhl / 100.0
    marpol_cost = base * 0.04 if country in MARPOL_COUNTRIES else 0.0
    ekaer_cost = 10.0 if country == "HU" else 0.0

    ins_cost = dhl_insurance_cost(float(cargo_value), dhl_region)
    total = base_after_min + fuel_cost + marpol_cost + ekaer_cost + dhl_avis_cost + ins_cost

    breakdown = pd.DataFrame([
        ["基础运费", base_after_min],
        ["燃油附加费", fuel_cost],
        ["MARPOL", marpol_cost],
        ["EKAER", ekaer_cost],
        ["Avisierung", dhl_avis_cost],
        ["保险", ins_cost],
        ["总计", total],
    ], columns=["项目","金额(EUR)"])
    return {
        "carrier":"DHL", "key":key, "found":True,
        "weight_col":weight_col, "base":base_after_min, "total":total,
        "breakdown":breakdown
    }

def quote_raben():
    key = build_key("RABEN", country, prefix)
    row = df_raben[df_raben[raben_key_col] == key]
    if row.empty:
        return {"carrier":"Raben", "key":key, "found":False, "error":"未找到该线路（可能PLZ不服务/Zone为空已剔除）"}

    weight_col = pick_weight_col(raben_wcols, total_charge_w)
    base = float(row.iloc[0][weight_col])
    base_after_min = max(base, float(MIN_CHARGE_RABEN))

    # Raben这里先不套DHL的柴油表（你后续如果给Raben油价逻辑再加）
    fuel_cost = 0.0

    ins_cost = raben_insurance_cost(float(cargo_value)) if raben_insurance_on else 0.0
    total = base_after_min + fuel_cost + ins_cost

    breakdown = pd.DataFrame([
        ["基础运费", base_after_min],
        ["燃油附加费", fuel_cost],
        ["保险", ins_cost],
        ["总计", total],
    ], columns=["项目","金额(EUR)"])
    return {
        "carrier":"Raben", "key":key, "found":True,
        "weight_col":weight_col, "base":base_after_min, "total":total,
        "breakdown":breakdown
    }

def quote_schenker():
    # 兼容 key 可能叫 SCHENKER 或 DSV（你表里如果用SCHENKER就命中第一条）
    keys_try = [
        build_key("SCHENKER", country, prefix),
        build_key("DSV", country, prefix),
    ]
    row = None
    used_key = None
    for k in keys_try:
        r = df_schenk[df_schenk[sch_key_col] == k]
        if not r.empty:
            row = r
            used_key = k
            break

    if row is None:
        return {"carrier":"Schenker", "key":keys_try[0], "found":False, "error":"未找到该线路（检查Schenker_Frachtkosten第一列key是否一致）"}

    weight_col = pick_weight_col(sch_wcols, total_charge_w)
    base = float(row.iloc[0][weight_col])
    base_after_min = max(base, float(MIN_CHARGE_SCHENKER))

    # Floating：按基础运费百分比
    floating_cost = base_after_min * float(sch_floating_pct) / 100.0

    # Maut：用距离 + 重量
    if maut_weight_basis.startswith("Fra"):
        maut_w = float(total_charge_w)
    else:
        maut_w = float(total_real_w)

    maut = 0.0
    if float(sch_distance_km) > 0 and maut_w > 0:
        maut = maut_cost(float(sch_distance_km), maut_w)

    total = base_after_min + floating_cost + maut + sch_avis_cost

    breakdown = pd.DataFrame([
        ["基础运费", base_after_min],
        ["Maut", maut],
        ["Floating", floating_cost],
        ["Avis(电话预约派送)", sch_avis_cost],
        ["总计", total],
    ], columns=["项目","金额(EUR)"])

    return {
        "carrier":"Schenker", "key":used_key, "found":True,
        "weight_col":weight_col, "base":base_after_min, "total":total,
        "breakdown":breakdown
    }

q_dhl = quote_dhl()
q_raben = quote_raben()
q_sch = quote_schenker()

# =========================
# 对比展示
# =========================
st.subheader("报价对比（DHL vs Raben vs Schenker）")

def summary_row(q):
    if not q.get("found"):
        return [q["carrier"], q.get("key","-"), "❌", "-", "-", "-", q.get("error","")]
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
    [summary_row(q_dhl), summary_row(q_raben), summary_row(q_sch)],
    columns=["承运商","线路Key","是否命中","匹配区间","基础运费(EUR)","总成本(EUR)","备注"]
)
st.dataframe(df_compare, use_container_width=True)

tab1, tab2, tab3 = st.tabs(["DHL 明细", "Raben 明细", "Schenker 明细"])
with tab1:
    if q_dhl.get("found"):
        st.dataframe(q_dhl["breakdown"], use_container_width=True)
    else:
        st.error(q_dhl.get("error","DHL未知错误"))
with tab2:
    if q_raben.get("found"):
        st.dataframe(q_raben["breakdown"], use_container_width=True)
    else:
        st.error(q_raben.get("error","Raben未知错误"))
with tab3:
    if q_sch.get("found"):
        st.dataframe(q_sch["breakdown"], use_container_width=True)
        st.info("提示：Maut 目前按你上传的 Mauttabelle 表 + 手动KM计算；后续可接地图API自动算KM。")
    else:
        st.error(q_sch.get("error","Schenker未知错误"))

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
        if q_sch.get("found"):
            q_sch["breakdown"].to_excel(writer, index=False, sheet_name="Schenker_Cost")
    return output.getvalue()

st.download_button(
    "下载Excel（Cargo + Compare + 明细）",
    data=to_excel(),
    file_name=f"Freight_Compare_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
)
