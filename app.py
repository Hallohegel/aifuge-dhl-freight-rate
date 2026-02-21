import streamlit as st
import pandas as pd
import re
from io import BytesIO
from datetime import datetime

from openpyxl import load_workbook

st.set_page_config(page_title="Aifuge Freight Cost Engine", layout="wide")

# =========================
# 通用工具
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
# DHL 柴油附加费表（你原来的）
# =========================
def get_dhl_diesel_surcharge_percent(price_cent):
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
# DHL 保险（29区间段，DE/WEST/EAST）
# =========================
def dhl_insurance_cost(value_eur: float, region: str) -> float:
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
# Schenker Maut 表（固定结构解析）
# =========================
MAUT_PATH = "data/Mauttabelle Stand 01.07.2024 (1).xlsx"

@st.cache_data(show_spinner=False)
def load_maut_table():
    df = pd.read_excel(MAUT_PATH, sheet_name="Mauttabelle", header=None)
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
        raise ValueError("Maut表读取失败：未找到重量区间行（列B/C）。")

    start = w_low_series[mask].index[0]
    end   = w_low_series[mask].index[-1]
    w_low  = w_low_series.loc[start:end].astype(float).tolist()
    w_high = w_high_series.loc[start:end].astype(float).tolist()

    values = df.iloc[start:end+1, 3:3+len(km_low)].astype(float).values
    return km_low, km_high, w_low, w_high, values

def maut_cost(distance_km: float, weight_kg: float) -> float:
    km_low, km_high, w_low, w_high, values = load_maut_table()

    col_idx = None
    for j, (lo, hi) in enumerate(zip(km_low, km_high)):
        if distance_km >= lo and distance_km <= hi:
            col_idx = j
            break
    if col_idx is None:
        col_idx = len(km_low) - 1

    row_idx = None
    for i, (lo, hi) in enumerate(zip(w_low, w_high)):
        if weight_kg >= lo and weight_kg <= hi:
            row_idx = i
            break
    if row_idx is None:
        row_idx = len(w_low) - 1

    return float(values[row_idx, col_idx])

# =========================
# 读取报价表
# =========================
DHL_PATH      = "data/Frachtkosten DHL Freight EU Neu.xlsx"
RABEN_PATH    = "data/Raben_Frachtkosten_FINAL_filled.xlsx"
SCHENKER_PATH = "data/Schenker_Frachtkosten.xlsx"
HELLMANN_PATH = "data/Hellmann_Frachtkosten_2026_SYSTEM_QC.xlsx"

@st.cache_data(show_spinner=False)
def load_ratebook(path, sheet_name=0):
    df = pd.read_excel(path, sheet_name=sheet_name)
    key_col = df.columns[0]
    wcols = sorted_weight_cols(df.columns)
    return df, key_col, wcols

# DHL（固定sheet）
@st.cache_data(show_spinner=False)
def load_dhl():
    df = pd.read_excel(DHL_PATH, sheet_name="Frachtkosten DHL Freight")
    key_col = df.columns[0]
    wcols = sorted_weight_cols(df.columns)
    return df, key_col, wcols

# Hellmann（Frachtkosten Hellmann + Meta）
@st.cache_data(show_spinner=False)
def load_hellmann():
    df = pd.read_excel(HELLMANN_PATH, sheet_name="Frachtkosten Hellmann")
    key_col = df.columns[0]
    wcols = sorted_weight_cols(df.columns)

    meta = pd.read_excel(HELLMANN_PATH, sheet_name="Meta")
    # 期望列：Country, Maut_%_of_Fracht, Staatliche_%_of_Fracht, Volumetric_factor_kg_per_cbm
    meta = meta.rename(columns={
        "Maut_%_of_Fracht": "maut_pct",
        "Staatliche_%_of_Fracht": "state_pct",
        "Volumetric_factor_kg_per_cbm": "vol_factor",
        "Country": "country"
    })
    meta_map = {}
    for _, r in meta.iterrows():
        c = str(r.get("country","")).strip().upper()
        if not c:
            continue
        meta_map[c] = {
            "maut_pct": float(r.get("maut_pct", 0.0) or 0.0),
            "state_pct": float(r.get("state_pct", 0.0) or 0.0),
            "vol_factor": float(r.get("vol_factor", 200.0) or 200.0),
        }
    return df, key_col, wcols, meta_map

# 加载
try:
    df_dhl, dhl_key_col, dhl_wcols = load_dhl()
    df_raben, raben_key_col, raben_wcols = load_ratebook(RABEN_PATH, sheet_name=0)
    df_schenk, sch_key_col, sch_wcols = load_ratebook(SCHENKER_PATH, sheet_name=0)
    df_hell, hell_key_col, hell_wcols, hell_meta = load_hellmann()
except Exception as e:
    st.error(f"读取报价表失败：{e}")
    st.stop()

# =========================
# 页面
# =========================
st.title("Aifuge GmbH | Freight Cost Engine (DHL + Raben + Schenker + Hellmann)")

col1, col2 = st.columns(2)
with col1:
    country = st.text_input("目的地国家代码", value="DE").upper()
with col2:
    prefix = st.text_input("邮编前2位", value="38")

st.divider()

# =========================
# 货物输入（基础：以 DHL 因子做展示；各承运商单独计算）
# =========================
st.subheader("货物明细（输入左侧，右侧自动计算）")

base_df = pd.DataFrame([{"数量":1, "长(cm)":60, "宽(cm)":40, "高(cm)":40, "实重(kg)":20}])
cargo = st.data_editor(base_df, num_rows="dynamic", use_container_width=True).fillna(0)

required_cols = ["数量","长(cm)","宽(cm)","高(cm)","实重(kg)"]
for c in required_cols:
    if c not in cargo.columns:
        st.error("货物表字段缺失，请刷新页面。")
        st.stop()

cargo["体积(m³)"] = (cargo["长(cm)"]/100)*(cargo["宽(cm)"]/100)*(cargo["高(cm)"]/100)*cargo["数量"]
cargo["实重合计(kg)"] = cargo["实重(kg)"]*cargo["数量"]

total_real_w = float(cargo["实重合计(kg)"].sum())
total_volume = float(cargo["体积(m³)"].sum())

c1, c2 = st.columns(2)
c1.metric("总实重(kg)", f"{total_real_w:.2f}")
c2.metric("总体积(m³)", f"{total_volume:.4f}")

st.divider()

# =========================
# 通用输入（保险货值等）
# =========================
st.subheader("通用参数")
cargo_value = st.number_input("货值 EUR（用于保险）", value=0.0)

st.divider()

# =========================
# DHL 参数
# =========================
st.subheader("DHL 参数")
dhl_factor_default = 200.0
manual_dhl_factor = st.checkbox("手动修改 DHL 体积系数 (kg/m³)", value=False)
dhl_factor = st.number_input("DHL 体积系数 kg/m³", value=dhl_factor_default) if manual_dhl_factor else dhl_factor_default
st.caption(f"DHL 当前体积系数：{dhl_factor} kg/m³（你之前规则）")

diesel_price = st.number_input("DHL 柴油价格 Cent/L（燃油附加费）", value=185.0)
dhl_fuel_pct = get_dhl_diesel_surcharge_percent(diesel_price)

dhl_avis = st.checkbox("DHL Avisierung 11€")
dhl_avis_cost = 11.0 if dhl_avis else 0.0

dhl_region = st.selectbox("DHL 保险区域", ["DE","WEST","EAST"], index=0)

MIN_CHARGE_DHL = st.number_input("DHL 最低收费 EUR", value=0.0)

st.divider()

# =========================
# Raben 参数
# =========================
st.subheader("Raben 参数")
MIN_CHARGE_RABEN = st.number_input("Raben 最低收费 EUR", value=0.0)
raben_ins_on = st.checkbox("Raben Versicherung（0.9‰，最低5.95€）", value=False)

st.divider()

# =========================
# Schenker 参数
# =========================
st.subheader("Schenker / DSV 参数（先上线版：KM手动输入）")
sch_distance_km = st.number_input("Schenker 距离 Distance (KM) — 手动输入", value=0.0)
maut_weight_basis = st.selectbox("Schenker Maut 计费重量用哪个？", ["Fra.Gew (计费重)","Wirk.Gew (实重)"], index=0)
sch_floating_pct = st.number_input("Schenker Floating(%) — 手动输入", value=8.5)
sch_avis = st.checkbox("Schenker Avis 电话预约派送 20€/票", value=False)
sch_avis_cost = 20.0 if sch_avis else 0.0
MIN_CHARGE_SCHENKER = st.number_input("Schenker 最低收费 EUR", value=0.0)

st.divider()

# =========================
# Hellmann 参数
# =========================
st.subheader("Hellmann 参数")

# 从 Meta 读取目的地国家参数
hell_cfg = hell_meta.get(country, {"maut_pct":0.0, "state_pct":0.0, "vol_factor":200.0})
hell_maut_pct = float(hell_cfg["maut_pct"])
hell_state_pct = float(hell_cfg["state_pct"])
hell_vol_factor = float(hell_cfg["vol_factor"])

st.info(
    f"Hellmann 自动参数（来自 Meta）：体积系数={hell_vol_factor} kg/cbm | "
    f"Maut={hell_maut_pct:.1f}% × Fracht | Staatliche={hell_state_pct:.1f}% × Fracht"
)

hell_diesel_pct = st.number_input("Hellmann Dieselzuschlag(%) — 手动输入（按月）", value=0.0)
hell_avis = st.checkbox("Hellmann Avis（如账单有固定费用，先手动输入）", value=False)
hell_avis_cost = st.number_input("Hellmann Avis 固定费用(EUR)", value=0.0) if hell_avis else 0.0
MIN_CHARGE_HELLMANN = st.number_input("Hellmann 最低收费 EUR", value=0.0)

st.divider()

# =========================
# 每家承运商：计算计费重（各自体积系数）
# =========================
def calc_billable_weight(total_real_weight, total_volume_m3, vol_factor_kg_per_m3, min_billable=0.0):
    vol_w = total_volume_m3 * float(vol_factor_kg_per_m3)
    billable = max(float(total_real_weight), float(vol_w))
    billable = max(billable, float(min_billable))
    return billable, vol_w

MIN_BILLABLE_GLOBAL = st.number_input("全局最低计费重量(kg)（可选）", value=0.0)

# DHL billable
dhl_billable_w, dhl_vol_w = calc_billable_weight(total_real_w, total_volume, dhl_factor, MIN_BILLABLE_GLOBAL)
# Raben billable（暂用全局的体积系数/或你后续用Raben规则表替换）
# 这里先沿用 DHL 的 factor 作为临时；你之前说“Raben按规则表替换”，后续我们会替换为你的规则Excel
raben_billable_w, raben_vol_w = calc_billable_weight(total_real_w, total_volume, dhl_factor, MIN_BILLABLE_GLOBAL)
# Schenker（通常按重量报价，本版先同样用 DHL 因子作为展示）
sch_billable_w, sch_vol_w = calc_billable_weight(total_real_w, total_volume, dhl_factor, MIN_BILLABLE_GLOBAL)
# Hellmann（来自 Meta 的体积系数：DE=150, EU=200）
hell_billable_w, hell_vol_w = calc_billable_weight(total_real_w, total_volume, hell_vol_factor, MIN_BILLABLE_GLOBAL)

st.subheader("计费重对比")
df_bw = pd.DataFrame([
    ["DHL", dhl_billable_w, dhl_vol_w],
    ["Raben", raben_billable_w, raben_vol_w],
    ["Schenker", sch_billable_w, sch_vol_w],
    ["Hellmann", hell_billable_w, hell_vol_w],
], columns=["承运商", "计费重(kg)", "体积重(kg)"])
st.dataframe(df_bw, use_container_width=True)

st.divider()

# =========================
# 报价函数
# =========================
MARPOL_COUNTRIES = ["DK","EE","FI","GB","IE","LT","LV","NO","SE"]

def quote_from_table(df, key_col, wcols, key, billable_w, min_charge):
    row = df[df[key_col] == key]
    if row.empty:
        return None, f"未找到线路：{key}"
    weight_col = pick_weight_col(wcols, billable_w)
    base = float(row.iloc[0][weight_col])
    base_after_min = max(base, float(min_charge))
    return (weight_col, base, base_after_min), None

def quote_dhl():
    key = build_key("DHL", country, prefix)
    res, err = quote_from_table(df_dhl, dhl_key_col, dhl_wcols, key, dhl_billable_w, MIN_CHARGE_DHL)
    if err:
        return {"carrier":"DHL","key":key,"found":False,"error":err}
    weight_col, base_raw, base = res

    fuel_cost = base_raw * dhl_fuel_pct / 100.0
    marpol_cost = base_raw * 0.04 if country in MARPOL_COUNTRIES else 0.0
    ekaer_cost = 10.0 if country == "HU" else 0.0
    ins_cost = dhl_insurance_cost(float(cargo_value), dhl_region)

    total = base + fuel_cost + marpol_cost + ekaer_cost + dhl_avis_cost + ins_cost

    breakdown = pd.DataFrame([
        ["基础运费", base],
        ["燃油附加费", fuel_cost],
        ["MARPOL", marpol_cost],
        ["EKAER", ekaer_cost],
        ["Avisierung", dhl_avis_cost],
        ["保险", ins_cost],
        ["总计", total],
    ], columns=["项目","金额(EUR)"])

    return {"carrier":"DHL","key":key,"found":True,"weight_col":weight_col,"base":base,"total":total,"breakdown":breakdown}

def quote_raben():
    key = build_key("RABEN", country, prefix)
    res, err = quote_from_table(df_raben, raben_key_col, raben_wcols, key, raben_billable_w, MIN_CHARGE_RABEN)
    if err:
        return {"carrier":"Raben","key":key,"found":False,"error":err}
    weight_col, base_raw, base = res

    fuel_cost = 0.0  # Raben燃油规则后续再加
    ins_cost = raben_insurance_cost(float(cargo_value)) if raben_ins_on else 0.0
    total = base + fuel_cost + ins_cost

    breakdown = pd.DataFrame([
        ["基础运费", base],
        ["燃油附加费", fuel_cost],
        ["保险", ins_cost],
        ["总计", total],
    ], columns=["项目","金额(EUR)"])

    return {"carrier":"Raben","key":key,"found":True,"weight_col":weight_col,"base":base,"total":total,"breakdown":breakdown}

def quote_schenker():
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
        return {"carrier":"Schenker","key":keys_try[0],"found":False,"error":"未找到线路（检查Schenker_Frachtkosten key）"}

    weight_col = pick_weight_col(sch_wcols, sch_billable_w)
    base_raw = float(row.iloc[0][weight_col])
    base = max(base_raw, float(MIN_CHARGE_SCHENKER))

    floating_cost = base * float(sch_floating_pct) / 100.0

    if maut_weight_basis.startswith("Fra"):
        maut_w = float(sch_billable_w)
    else:
        maut_w = float(total_real_w)

    maut = 0.0
    if float(sch_distance_km) > 0 and maut_w > 0:
        maut = maut_cost(float(sch_distance_km), maut_w)

    total = base + floating_cost + maut + sch_avis_cost

    breakdown = pd.DataFrame([
        ["基础运费", base],
        ["Maut", maut],
        ["Floating", floating_cost],
        ["Avis(电话预约派送)", sch_avis_cost],
        ["总计", total],
    ], columns=["项目","金额(EUR)"])

    return {"carrier":"Schenker","key":used_key,"found":True,"weight_col":weight_col,"base":base,"total":total,"breakdown":breakdown}

def quote_hellmann():
    key = build_key("HELLMANN", country, prefix)
    res, err = quote_from_table(df_hell, hell_key_col, hell_wcols, key, hell_billable_w, MIN_CHARGE_HELLMANN)
    if err:
        return {"carrier":"Hellmann","key":key,"found":False,"error":err}
    weight_col, base_raw, base = res

    # 按“Frachtkosten”为基数计算（不含Maut/Abgaben/Diesel/Avis）
    maut_cost_eur = base_raw * (hell_maut_pct / 100.0)
    state_cost_eur = base_raw * (hell_state_pct / 100.0)
    diesel_cost_eur = base_raw * (float(hell_diesel_pct) / 100.0)

    total = base + maut_cost_eur + state_cost_eur + diesel_cost_eur + float(hell_avis_cost)

    breakdown = pd.DataFrame([
        ["基础运费(Fracht)", base],
        ["Maut", maut_cost_eur],
        ["Staatliche Abgaben", state_cost_eur],
        ["Dieselzuschlag", diesel_cost_eur],
        ["Avis(如有)", float(hell_avis_cost)],
        ["总计", total],
    ], columns=["项目","金额(EUR)"])

    return {"carrier":"Hellmann","key":key,"found":True,"weight_col":weight_col,"base":base,"total":total,"breakdown":breakdown}

q_dhl = quote_dhl()
q_raben = quote_raben()
q_sch  = quote_schenker()
q_hell = quote_hellmann()

# =========================
# 对比展示
# =========================
st.subheader("报价对比（DHL vs Raben vs Schenker vs Hellmann）")

def summary_row(q):
    if not q.get("found"):
        return [q["carrier"], q.get("key","-"), "❌", "-", "-", "-", q.get("error","")]
    return [q["carrier"], q["key"], "✅", q["weight_col"], f"{q['base']:.2f}", f"{q['total']:.2f}", ""]

df_compare = pd.DataFrame(
    [summary_row(q_dhl), summary_row(q_raben), summary_row(q_sch), summary_row(q_hell)],
    columns=["承运商","线路Key","是否命中","匹配区间","基础运费(EUR)","总成本(EUR)","备注"]
)
st.dataframe(df_compare, use_container_width=True)

tab1, tab2, tab3, tab4 = st.tabs(["DHL 明细", "Raben 明细", "Schenker 明细", "Hellmann 明细"])
with tab1:
    st.dataframe(q_dhl["breakdown"], use_container_width=True) if q_dhl.get("found") else st.error(q_dhl.get("error",""))
with tab2:
    st.dataframe(q_raben["breakdown"], use_container_width=True) if q_raben.get("found") else st.error(q_raben.get("error",""))
with tab3:
    st.dataframe(q_sch["breakdown"], use_container_width=True) if q_sch.get("found") else st.error(q_sch.get("error",""))
with tab4:
    st.dataframe(q_hell["breakdown"], use_container_width=True) if q_hell.get("found") else st.error(q_hell.get("error",""))

# =========================
# 导出Excel
# =========================
def to_excel():
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        cargo.to_excel(writer, index=False, sheet_name="Cargo")
        df_bw.to_excel(writer, index=False, sheet_name="BillableWeight")
        df_compare.to_excel(writer, index=False, sheet_name="Compare")
        if q_dhl.get("found"):
            q_dhl["breakdown"].to_excel(writer, index=False, sheet_name="DHL_Cost")
        if q_raben.get("found"):
            q_raben["breakdown"].to_excel(writer, index=False, sheet_name="Raben_Cost")
        if q_sch.get("found"):
            q_sch["breakdown"].to_excel(writer, index=False, sheet_name="Schenker_Cost")
        if q_hell.get("found"):
            q_hell["breakdown"].to_excel(writer, index=False, sheet_name="Hellmann_Cost")
    return output.getvalue()

st.download_button(
    "下载Excel（Cargo + Compare + 明细）",
    data=to_excel(),
    file_name=f"Freight_Compare_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
)
