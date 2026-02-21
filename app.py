import streamlit as st
import pandas as pd
import re
from dataclasses import dataclass

st.set_page_config(page_title="Aifuge Freight Cost Engine V5", layout="wide")


# =========================================================
# Hellmann 2026 全国家规则（最终生产字典 V5）
# 字段统一：vol_factor / maut_pct / state_pct  (percent)
# =========================================================
HELLMANN_RULES = {
    "DE": {"maut_pct": 18.2, "state_pct": 0.0, "vol_factor": 150},

    "AT": {"maut_pct": 13.3, "state_pct": 6.6, "vol_factor": 200},
    "BE": {"maut_pct": 9.7,  "state_pct": 2.1, "vol_factor": 200},
    "BG": {"maut_pct": 6.2,  "state_pct": 9.9, "vol_factor": 200},
    "CZ": {"maut_pct": 8.6,  "state_pct": 5.4, "vol_factor": 200},
    "DK": {"maut_pct": 8.6,  "state_pct": 0.1, "vol_factor": 200},
    "EE": {"maut_pct": 7.2,  "state_pct": 0.0, "vol_factor": 200},
    "ES": {"maut_pct": 6.7,  "state_pct": 0.0, "vol_factor": 200},
    "FI": {"maut_pct": 4.8,  "state_pct": 3.1, "vol_factor": 200},
    "FR": {"maut_pct": 7.7,  "state_pct": 0.5, "vol_factor": 200},
    "GR": {"maut_pct": 7.8,  "state_pct": 10.0,"vol_factor": 200},
    "HR": {"maut_pct": 9.1,  "state_pct": 11.6,"vol_factor": 200},
    "HU": {"maut_pct": 11.5, "state_pct": 15.2,"vol_factor": 200},
    "IE": {"maut_pct": 6.1,  "state_pct": 3.6, "vol_factor": 200},
    "IT": {"maut_pct": 10.3, "state_pct": 7.0, "vol_factor": 200},
    "LT": {"maut_pct": 7.6,  "state_pct": 0.0, "vol_factor": 200},
    "LU": {"maut_pct": 10.9, "state_pct": 0.0, "vol_factor": 200},
    "LV": {"maut_pct": 7.0,  "state_pct": 0.0, "vol_factor": 200},
    "NL": {"maut_pct": 8.9,  "state_pct": 0.0, "vol_factor": 200},
    "PL": {"maut_pct": 10.2, "state_pct": 2.6, "vol_factor": 200},
    "PT": {"maut_pct": 7.7,  "state_pct": 0.0, "vol_factor": 200},
    "RO": {"maut_pct": 7.0,  "state_pct": 10.6,"vol_factor": 200},
    "SE": {"maut_pct": 3.6,  "state_pct": 0.7, "vol_factor": 200},
    "SI": {"maut_pct": 12.5, "state_pct": 15.3,"vol_factor": 200},
    "SK": {"maut_pct": 8.5,  "state_pct": 5.9, "vol_factor": 200},
    "XK": {"maut_pct": 3.4,  "state_pct": 4.3, "vol_factor": 200},
}

# Hellmann DG 国际：两档
HELLMANN_DG_30 = set([
    "AL","AT","BA","BE","BG","CH","CZ","DK","EE","ES","FR","HR","HU","IT","LT",
    "LU","LV","ME","MK","NL","PL","PT","RO","RS","SI","SK","XK"
])
HELLMANN_DG_75 = set(["FI","GB","GR","IE","NO","SE"])


# =========================================================
# 工具函数
# =========================================================
def safe_float(x, default=0.0) -> float:
    try:
        if x is None or (isinstance(x, float) and pd.isna(x)):
            return default
        if isinstance(x, str):
            x = x.replace(",", ".").strip()
        return float(x)
    except Exception:
        return default

def normalize_prefix(prefix: str) -> str:
    s = re.sub(r"\D", "", str(prefix))
    return s.zfill(2)[:2] if s else "00"

def build_key(carrier: str, country: str, prefix: str) -> str:
    return f"{carrier.upper()}-{country.upper()}--{normalize_prefix(prefix)}"

def sorted_weight_cols(cols):
    wcols = [c for c in cols if str(c).strip().lower().startswith("bis-")]
    def key_fn(c):
        m = re.findall(r"bis-(\d+)", str(c))
        return int(m[0]) if m else 10**9
    return sorted(wcols, key=key_fn)

def pick_weight_col(weight_cols_sorted, billable_weight):
    for c in weight_cols_sorted:
        upper = int(str(c).split("-")[1])
        if billable_weight <= upper:
            return c
    return None

def detect_key_col(df: pd.DataFrame) -> str:
    # 优先找 "key"，找不到就用第1列
    cols = [str(c).strip() for c in df.columns]
    for c in cols[:20]:
        if str(c).strip().lower() == "key":
            return c
    return cols[0]

@dataclass
class RateTable:
    df: pd.DataFrame
    key_col: str
    wcols: list

def load_rate_table_from_upload(uploaded_file) -> RateTable:
    # 统一读第一个sheet，避免 sheet 名不同导致报错
    df = pd.read_excel(uploaded_file, sheet_name=0)
    df.columns = [str(c).strip() for c in df.columns]
    key_col = detect_key_col(df)
    wcols = sorted_weight_cols(df.columns)
    if not wcols:
        raise ValueError("报价表里未检测到 bis-xx 重量列（例如 bis-30, bis-50...）")
    return RateTable(df=df, key_col=key_col, wcols=wcols)

def find_rate(rt: RateTable, key: str, billable_weight: float):
    df = rt.df
    key_col = rt.key_col
    wcols = rt.wcols

    row = df[df[key_col].astype(str) == str(key)]
    if row.empty:
        return None, None, f"未找到 key={key}"

    col = pick_weight_col(wcols, billable_weight)
    if col is None:
        return None, None, f"计费重 {billable_weight:.2f}kg 超出报价表最大重量段"
    price = safe_float(row.iloc[0][col], default=None)
    if price is None:
        return None, None, f"命中 {col} 但单元格为空/不可解析"
    return price, col, ""

def calc_ldm(l_cm, w_cm, qty):
    # 常用装载米：长*宽 / 2.4
    return ((l_cm/100.0) * (w_cm/100.0) / 2.4) * qty

def compute_cargo_metrics(cargo_df: pd.DataFrame):
    df = cargo_df.copy()
    df = df.fillna(0)

    for col in ["数量", "长(cm)", "宽(cm)", "高(cm)", "实重(kg)"]:
        if col not in df.columns:
            raise ValueError(f"货物明细缺少字段：{col}")

    df["体积(m³)"] = (df["长(cm)"]/100.0) * (df["宽(cm)"]/100.0) * (df["高(cm)"]/100.0) * df["数量"]
    df["实重合计(kg)"] = df["实重(kg)"] * df["数量"]
    df["LDM"] = df.apply(lambda r: calc_ldm(r["长(cm)"], r["宽(cm)"], r["数量"]), axis=1)
    df["最长边(cm)"] = df[["长(cm)", "宽(cm)", "高(cm)"]].max(axis=1)

    total_real = float(df["实重合计(kg)"].sum())
    total_cbm = float(df["体积(m³)"].sum())
    total_ldm = float(df["LDM"].sum())
    max_edge = float(df["最长边(cm)"].max()) if len(df) else 0.0
    return df, total_real, total_cbm, total_ldm, max_edge


# =========================================================
# Raben 计费重（保守生产版：可用你规则表替换）
# 这里给默认：min_pack=15kg/件、cbm_factor=333kg/m³、ldm_threshold=2.4、ldm_factor=1850
# =========================================================
def raben_billable_weight(country, cargo_df):
    min_pack = 15.0
    cbm_factor = 333.0
    ldm_threshold = 2.4
    ldm_factor = 1850.0

    total_real = float(cargo_df["实重合计(kg)"].sum())
    total_cbm = float(cargo_df["体积(m³)"].sum())
    total_ldm = float(cargo_df["LDM"].sum())

    cbm_w = total_cbm * cbm_factor
    ldm_w = total_ldm * ldm_factor if total_ldm >= ldm_threshold else 0.0
    min_pack_total = float(((cargo_df["数量"] * cargo_df["实重(kg)"].clip(lower=min_pack))).sum())

    billable = max(total_real, cbm_w, ldm_w, min_pack_total)
    meta = dict(min_pack=min_pack, cbm_factor=cbm_factor, ldm_threshold=ldm_threshold, ldm_factor=ldm_factor,
                total_real=total_real, total_cbm=total_cbm, total_ldm=total_ldm,
                cbm_w=cbm_w, ldm_w=ldm_w, min_pack_total=min_pack_total)
    return billable, meta


# =========================================================
# Hellmann Diesel floater: €/L -> %（按你给的条款表）
# =========================================================
def hellmann_dieselfloater_percent(diesel_eur_per_l: float) -> float:
    x = safe_float(diesel_eur_per_l, 0.0)
    bands = [
        (1.48, 0.0),
        (1.50, 0.5),
        (1.52, 1.0),
        (1.54, 1.5),
        (1.56, 2.0),
        (1.58, 2.5),
        (1.60, 3.0),
        (1.62, 3.5),
    ]
    for up, pct in bands:
        if x <= up:
            return pct
    # 每增加 0.02€ +0.5%
    extra = x - 1.62
    steps = int(extra / 0.02) + 1
    return 3.5 + steps * 0.5


# =========================================================
# 报价计算（4家）
# =========================================================
def quote_dhl(rt: RateTable, country: str, prefix: str, billable_weight: float):
    key = build_key("DHL", country, prefix)
    base, col, err = find_rate(rt, key, billable_weight)
    if err:
        return None, err
    return {
        "carrier": "DHL Freight",
        "key": key,
        "weight_col": col,
        "billable_kg": billable_weight,
        "base": base,
        "surcharges": [],
        "total": base
    }, ""

def quote_raben(rt: RateTable, country: str, prefix: str, cargo_df: pd.DataFrame):
    billable, meta = raben_billable_weight(country, cargo_df)
    key = build_key("RABEN", country, prefix)
    base, col, err = find_rate(rt, key, billable)
    if err:
        return None, err
    return {
        "carrier": "Raben",
        "key": key,
        "weight_col": col,
        "billable_kg": billable,
        "base": base,
        "surcharges": [],
        "total": base,
        "meta": meta
    }, ""

def quote_schenker(rt: RateTable, country: str, prefix: str,
                   billable_weight: float,
                   sch_km: float,
                   sch_maut_eur: float,
                   sch_floating_pct: float,
                   sch_avis_20: bool):
    key = build_key("SCHENKER", country, prefix)
    base, col, err = find_rate(rt, key, billable_weight)
    if err:
        return None, err

    sur = []
    # Maut：你说先手工输入距离/或直接输入Maut金额（这里支持直接输 Maut €）
    if sch_maut_eur > 0:
        sur.append(("Maut (manual €)", sch_maut_eur))
    # Floating：手动输入 %
    if sch_floating_pct > 0:
        sur.append((f"Floating {sch_floating_pct:.2f}%", base * sch_floating_pct / 100.0))
    # Avis 20€
    if sch_avis_20:
        sur.append(("Avis +20€", 20.0))

    total = base + sum(x[1] for x in sur)
    return {
        "carrier": "DB Schenker / DSV",
        "key": key,
        "weight_col": col,
        "billable_kg": billable_weight,
        "base": base,
        "surcharges": sur,
        "total": total,
        "meta": {"km_manual": sch_km}
    }, ""

def quote_hellmann(rt: RateTable, country: str, prefix: str, cargo_df: pd.DataFrame,
                   diesel_eur_per_l: float,
                   is_b2c: bool,
                   is_avis: bool,
                   is_dg: bool,
                   is_len_gt_240: bool):
    cc = country.upper().strip()
    rule = HELLMANN_RULES.get(cc, {})
    vol_factor = float(rule.get("vol_factor", 200))  # ✅永不 KeyError

    total_real = float(cargo_df["实重合计(kg)"].sum())
    total_cbm = float(cargo_df["体积(m³)"].sum())
    billable = max(total_real, total_cbm * vol_factor)

    # Hellmann 报价 <=2500kg
    if billable > 2500:
        return None, f"Hellmann 计费重 {billable:.2f}kg > 2500kg（报价表无此段，需询价）"

    key = build_key("HELLMANN", cc, prefix)
    base, col, err = find_rate(rt, key, billable)
    if err:
        return None, err

    maut_pct = float(rule.get("maut_pct", 0.0))
    state_pct = float(rule.get("state_pct", 0.0))

    sur = []
    # Maut / Staatliche Abgaben 按 Frachtkosten 百分比
    if maut_pct > 0:
        sur.append((f"Maut {maut_pct:.1f}%", base * maut_pct / 100.0))
    if state_pct > 0:
        sur.append((f"Staatl. Abgaben {state_pct:.1f}%", base * state_pct / 100.0))

    # Diesel floater
    diesel_pct = hellmann_dieselfloater_percent(diesel_eur_per_l)
    if diesel_pct > 0:
        sur.append((f"Dieselfloater {diesel_pct:.1f}%", base * diesel_pct / 100.0))

    # 叠加项：DG + B2C + Avis（你已确认 DG 叠加 B2C/Avis）
    if is_b2c:
        sur.append(("B2C +8.9€", 8.9))
    if is_avis:
        sur.append(("Avis +12.5€", 12.5))

    if is_dg:
        if cc == "DE":
            dg_fee = 15.0
        elif cc in HELLMANN_DG_75:
            dg_fee = 75.0
        else:
            dg_fee = 30.0
        sur.append(("Gefahrgut (DG)", dg_fee))

    if is_len_gt_240:
        sur.append(("Längenzuschlag +30€", 30.0))

    total = base + sum(x[1] for x in sur)

    return {
        "carrier": "Hellmann",
        "key": key,
        "weight_col": col,
        "billable_kg": billable,
        "base": base,
        "surcharges": sur,
        "total": total,
        "meta": {"vol_factor": vol_factor, "diesel_pct": diesel_pct}
    }, ""


# =========================================================
# UI
# =========================================================
st.title("Aifuge GmbH | Freight Cost Engine V5 (DHL + Raben + Schenker/DSV + Hellmann)")

with st.sidebar:
    st.header("上传报价表（system upload 格式）")
    up_dhl = st.file_uploader("DHL Freight 报价表 (xlsx)", type=["xlsx"], key="dhl")
    up_raben = st.file_uploader("Raben 报价表 (xlsx)", type=["xlsx"], key="raben")
    up_sch = st.file_uploader("DB Schenker/DSV 报价表 (xlsx)", type=["xlsx"], key="sch")
    up_hell = st.file_uploader("Hellmann 2026 报价表 (xlsx)", type=["xlsx"], key="hell")

    st.caption("注意：必须包含列 key，以及 bis-30 / bis-50 ... 这种重量列。")

st.subheader("线路参数")
c1, c2, c3 = st.columns(3)
with c1:
    country = st.text_input("目的地国家代码 ISO2", value="DE").upper().strip()
with c2:
    prefix = st.text_input("邮编前两位", value="38").strip()
with c3:
    common_vol_factor = st.number_input("通用泡重系数 kg/m³（用于 DHL/Schenker）", value=200.0, step=10.0)

st.subheader("货物明细（可多行）")
base_df = pd.DataFrame([{"数量": 1, "长(cm)": 60, "宽(cm)": 40, "高(cm)": 40, "实重(kg)": 20.0}])
cargo = st.data_editor(base_df, num_rows="dynamic", use_container_width=True)

try:
    cargo2, total_real, total_cbm, total_ldm, max_edge = compute_cargo_metrics(cargo)
except Exception as e:
    st.error(f"货物明细错误：{e}")
    st.stop()

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("总实重(kg)", f"{total_real:.2f}")
m2.metric("总体积(m³)", f"{total_cbm:.4f}")
m3.metric("总LDM", f"{total_ldm:.3f}")
m4.metric("最长边(cm)", f"{max_edge:.0f}")
m5.metric("通用泡重(kg)", f"{(total_cbm*common_vol_factor):.2f}")

# 通用计费重：max(实重, 泡重)
billable_common = max(total_real, total_cbm * common_vol_factor)

st.divider()
st.subheader("Schenker / DSV 参数（先手动）")
s1, s2, s3, s4 = st.columns(4)
with s1:
    sch_km = st.number_input("Schenker Distance KM (手动)", value=0.0, step=10.0)
with s2:
    sch_maut_eur = st.number_input("Schenker Maut € (手动输入金额)", value=0.0, step=1.0)
with s3:
    sch_floating_pct = st.number_input("Schenker Floating % (手动)", value=0.0, step=0.5)
with s4:
    sch_avis_20 = st.checkbox("Schenker Avis +20€", value=False)

st.divider()
st.subheader("Hellmann 参数（按你给的条款）")
h1, h2, h3, h4, h5 = st.columns(5)
with h1:
    hell_diesel = st.number_input("Hellmann Diesel €/L（用于Dieselfloater）", value=1.55, step=0.01)
with h2:
    hell_b2c = st.checkbox("Hellmann B2C +8.9€", value=False)
with h3:
    hell_avis = st.checkbox("Hellmann Avis +12.5€", value=False)
with h4:
    hell_dg = st.checkbox("Hellmann Gefahrgut (DG)", value=False)
with h5:
    hell_len = st.checkbox("单件最长边>240cm (Längenzuschlag +30€)", value=(max_edge > 240))

# =========================================================
# 计算按钮
# =========================================================
st.divider()
do_calc = st.button("计算四家报价对比", type="primary")

if do_calc:
    missing = []
    if up_dhl is None: missing.append("DHL")
    if up_raben is None: missing.append("Raben")
    if up_sch is None: missing.append("Schenker/DSV")
    if up_hell is None: missing.append("Hellmann")
    if missing:
        st.error("请先上传报价表：" + ", ".join(missing))
        st.stop()

    try:
        rt_dhl = load_rate_table_from_upload(up_dhl)
        rt_raben = load_rate_table_from_upload(up_raben)
        rt_sch = load_rate_table_from_upload(up_sch)
        rt_hell = load_rate_table_from_upload(up_hell)
    except Exception as e:
        st.error(f"读取报价表失败：{e}")
        st.stop()

    results = []
    errors = []

    # DHL
    q, err = quote_dhl(rt_dhl, country, prefix, billable_common)
    if err: errors.append(("DHL Freight", err))
    else: results.append(q)

    # Raben
    q, err = quote_raben(rt_raben, country, prefix, cargo2)
    if err: errors.append(("Raben", err))
    else: results.append(q)

    # Schenker/DSV
    q, err = quote_schenker(rt_sch, country, prefix, billable_common, sch_km, sch_maut_eur, sch_floating_pct, sch_avis_20)
    if err: errors.append(("DB Schenker/DSV", err))
    else: results.append(q)

    # Hellmann
    q, err = quote_hellmann(rt_hell, country, prefix, cargo2, hell_diesel, hell_b2c, hell_avis, hell_dg, hell_len)
    if err: errors.append(("Hellmann", err))
    else: results.append(q)

    if errors:
        st.warning("部分供应商无法报价（不会影响其他供应商显示）：")
        for name, msg in errors:
            st.write(f"- **{name}**：{msg}")

    if not results:
        st.error("四家都无法报价（key 不存在 / 重量超段 / 表结构不正确）")
        st.stop()

    # 汇总对比表
    comp = []
    for r in results:
        sur_sum = sum(x[1] for x in r["surcharges"])
        comp.append({
            "供应商": r["carrier"],
            "Key": r["key"],
            "命中重量段": r["weight_col"],
            "计费重(kg)": round(r["billable_kg"], 2),
            "基础运费€": round(r["base"], 2),
            "附加费合计€": round(sur_sum, 2),
            "总价€": round(r["total"], 2),
        })
    comp_df = pd.DataFrame(comp).sort_values("总价€")
    st.subheader("四家报价对比（按总价排序）")
    st.dataframe(comp_df, use_container_width=True)

    # 展开每家明细
    st.subheader("明细拆分")
    for r in results:
        with st.expander(f"{r['carrier']} | 总价 €{r['total']:.2f}"):
            st.write(f"Key：`{r['key']}`")
            st.write(f"计费重：**{r['billable_kg']:.2f} kg** | 命中：**{r['weight_col']}**")
            st.write(f"基础运费：**€{r['base']:.2f}**")

            if r["surcharges"]:
                d = pd.DataFrame(r["surcharges"], columns=["附加费项目", "金额€"])
                d["金额€"] = d["金额€"].map(lambda x: round(float(x), 2))
                st.dataframe(d, use_container_width=True)
            else:
                st.info("无附加费")

            if "meta" in r:
                st.caption(f"Meta: {r['meta']}")
