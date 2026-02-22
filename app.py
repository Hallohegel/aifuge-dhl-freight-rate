import streamlit as st
import pandas as pd
import re
from io import BytesIO
from datetime import datetime

st.set_page_config(page_title="Aifuge Freight Cost Engine V5.7", layout="wide")


# =========================================================
# 基础工具
# =========================================================
def normalize_prefix(prefix) -> str:
    return re.sub(r"\D", "", str(prefix)).zfill(2)[:2]

def build_key(carrier: str, country: str, prefix2: str) -> str:
    return f"{carrier}-{country.upper()}--{normalize_prefix(prefix2)}"

def safe_float(x, default=0.0):
    try:
        if pd.isna(x):
            return default
        if isinstance(x, str):
            x = x.replace(",", ".").strip()
        return float(x)
    except:
        return default

def sorted_weight_cols(cols):
    w = [c for c in cols if str(c).startswith("bis-")]
    def upper(x):
        try:
            return int(str(x).split("-")[1])
        except:
            return 10**9
    return sorted(w, key=upper)

def pick_weight_col(wcols_sorted, billable_weight):
    if not wcols_sorted:
        return None
    for c in wcols_sorted:
        try:
            upper = int(str(c).split("-")[1])
        except:
            continue
        if billable_weight <= upper:
            return c
    return wcols_sorted[-1]

def volumetric_weight_cm(l_cm, w_cm, h_cm, factor_kg_per_m3):
    return (float(l_cm)/100.0) * (float(w_cm)/100.0) * (float(h_cm)/100.0) * float(factor_kg_per_m3)

def normalize_key_col(df: pd.DataFrame):
    # 统一第一列为 key
    c0 = df.columns[0]
    if str(c0).strip().lower() != "key":
        df = df.rename(columns={c0: "key"})
    df["key"] = df["key"].astype(str).str.strip()
    return df

def df_has_col(df, candidates):
    lower_map = {str(c).strip().lower(): c for c in df.columns}
    for n in candidates:
        k = str(n).strip().lower()
        if k in lower_map:
            return lower_map[k]
    return None


# =========================================================
# 固定路径（方案A：自动读取 data/）
# =========================================================
DHL_PATH      = "data/DHL_Frachtkosten.xlsx"
RABEN_PATH    = "data/Raben_Frachtkosten.xlsx"
SCHENKER_PATH = "data/Schenker_Frachtkosten.xlsx"
MAUT_PATH     = "data/Schenker_Maut.xlsx"
HELLMANN_PATH = "data/Hellmann_Frachtkosten_2026.xlsx"
FEDEX_PATH    = "data/FedEx_Frachtkosten.xlsx"


# =========================================================
# 加载报价表（自动缓存）
# =========================================================
@st.cache_data(show_spinner=False)
def load_rate_table(path, sheet_name=0):
    df = pd.read_excel(path, sheet_name=sheet_name)
    if df is None or df.empty:
        raise ValueError(f"读取失败或为空：{path}")
    df = normalize_key_col(df)
    wcols = sorted_weight_cols(df.columns)
    if not wcols:
        raise ValueError(f"未找到 bis-xx 重量列：{path}")
    return df, wcols

@st.cache_data(show_spinner=False)
def load_fedex_table(path):
    """
    期望两列结构：country + eur_per_kg
    如果列名不同也会尝试自动识别
    """
    df = pd.read_excel(path, sheet_name=0)
    if df is None or df.empty:
        raise ValueError("FedEx Excel为空或无法读取。")

    country_col = df_has_col(df, ["country", "land", "ziel_land", "dest_country", "to_country"])
    rate_col    = df_has_col(df, ["eur_per_kg", "rate", "preis_pro_kg", "€/kg", "euro_per_kg", "price_per_kg"])

    if country_col is None:
        country_col = df.columns[0]
    if rate_col is None:
        if len(df.columns) >= 2:
            rate_col = df.columns[1]
        else:
            raise ValueError("FedEx表无法识别 €/kg 列（请整理为 country + eur_per_kg 两列）。")

    out = df[[country_col, rate_col]].copy()
    out.columns = ["country", "eur_per_kg"]
    out["country"] = out["country"].astype(str).str.upper().str.strip()
    out["eur_per_kg"] = pd.to_numeric(out["eur_per_kg"], errors="coerce")
    out = out.dropna(subset=["country", "eur_per_kg"])
    out = out[out["eur_per_kg"] > 0]
    return out

@st.cache_data(show_spinner=False)
def load_schenker_maut(path):
    """
    优先识别标准列：
    w_from,w_to,km_from,km_to,maut
    若识别失败，返回 None（系统自动切换为手填Maut）
    """
    df = pd.read_excel(path, sheet_name=0)
    if df is None or df.empty:
        return None

    w_from = df_has_col(df, ["w_from", "min_w", "weight_from", "von_kg", "kg_von", "from_kg"])
    w_to   = df_has_col(df, ["w_to", "max_w", "weight_to", "bis_kg", "kg_bis", "to_kg"])
    km_from= df_has_col(df, ["km_from", "min_km", "from_km", "von_km"])
    km_to  = df_has_col(df, ["km_to", "max_km", "to_km", "bis_km"])
    val    = df_has_col(df, ["maut", "value", "betrag", "eur", "price"])

    if None in [w_from, w_to, km_from, km_to, val]:
        return None

    out = df[[w_from, w_to, km_from, km_to, val]].copy()
    out.columns = ["w_from", "w_to", "km_from", "km_to", "maut"]
    for c in ["w_from", "w_to", "km_from", "km_to", "maut"]:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.dropna()
    return out

def lookup_maut(maut_df_norm: pd.DataFrame, weight_kg: float, distance_km: float):
    if maut_df_norm is None:
        return None
    hit = maut_df_norm[
        (maut_df_norm["w_from"] <= weight_kg) &
        (maut_df_norm["w_to"]   >= weight_kg) &
        (maut_df_norm["km_from"]<= distance_km) &
        (maut_df_norm["km_to"]  >= distance_km)
    ]
    if hit.empty:
        return None
    return safe_float(hit.iloc[0]["maut"], None)


# =========================================================
# Hellmann 规则（V5：最终生产级字典）
# =========================================================
HELLMANN_RULES = {
    "DE": {"maut_pct": 18.2, "state_pct": 0.0,  "vol_factor": 150},
    "AT": {"maut_pct": 13.3, "state_pct": 6.6,  "vol_factor": 200},
    "BE": {"maut_pct": 9.7,  "state_pct": 2.1,  "vol_factor": 200},
    "BG": {"maut_pct": 6.2,  "state_pct": 9.9,  "vol_factor": 200},
    "CZ": {"maut_pct": 8.6,  "state_pct": 5.4,  "vol_factor": 200},
    "DK": {"maut_pct": 8.6,  "state_pct": 0.1,  "vol_factor": 200},
    "EE": {"maut_pct": 7.2,  "state_pct": 0.0,  "vol_factor": 200},
    "ES": {"maut_pct": 6.7,  "state_pct": 0.0,  "vol_factor": 200},
    "FI": {"maut_pct": 4.8,  "state_pct": 3.1,  "vol_factor": 200},
    "FR": {"maut_pct": 7.7,  "state_pct": 0.5,  "vol_factor": 200},
    "GR": {"maut_pct": 7.8,  "state_pct": 10.0, "vol_factor": 200},
    "HU": {"maut_pct": 11.5, "state_pct": 15.2, "vol_factor": 200},
    "IT": {"maut_pct": 10.3, "state_pct": 7.0,  "vol_factor": 200},
    "LT": {"maut_pct": 7.6,  "state_pct": 0.0,  "vol_factor": 200},
    "LU": {"maut_pct": 10.9, "state_pct": 0.0,  "vol_factor": 200},
    "LV": {"maut_pct": 7.0,  "state_pct": 0.0,  "vol_factor": 200},
    "NL": {"maut_pct": 8.9,  "state_pct": 0.0,  "vol_factor": 200},
    "PL": {"maut_pct": 10.2, "state_pct": 2.6,  "vol_factor": 200},
    "PT": {"maut_pct": 7.7,  "state_pct": 0.0,  "vol_factor": 200},
    "RO": {"maut_pct": 7.0,  "state_pct": 10.6, "vol_factor": 200},
    "SE": {"maut_pct": 3.6,  "state_pct": 0.7,  "vol_factor": 200},
    "SI": {"maut_pct": 12.5, "state_pct": 15.3, "vol_factor": 200},
    "SK": {"maut_pct": 8.5,  "state_pct": 5.9,  "vol_factor": 200},
    "XK": {"maut_pct": 3.4,  "state_pct": 4.3,  "vol_factor": 200},
}

HELLMANN_DIESEL_TABLE = [
    (0.00, 1.48, 0.0),
    (1.48, 1.50, 0.5),
    (1.50, 1.52, 1.0),
    (1.52, 1.54, 1.5),
    (1.54, 1.56, 2.0),
    (1.56, 1.58, 2.5),
    (1.58, 1.60, 3.0),
    (1.60, 1.62, 3.5),
]

def hellmann_diesel_pct(diesel_eur_per_l: float) -> float:
    d = float(diesel_eur_per_l)
    for lo, hi, pct in HELLMANN_DIESEL_TABLE:
        if lo <= d <= hi:
            return pct
    if d > 1.62:
        steps = (d - 1.62) / 0.02
        return 3.5 + (int(steps + 1e-9) * 0.5)
    return 0.0

HELLMANN_DG_30_COUNTRIES = set([
    "AL","AT","BA","BE","BG","CH","CZ","DK","EE","ES","FL","FR","HR","HU","IT","LT",
    "LU","LV","ME","MK","NL","PL","PT","RO","RS","SI","SK","XK"
])
HELLMANN_DG_75_COUNTRIES = set(["FI","GB","GR","IE","NO","SE"])


# =========================================================
# 计费重系数（最终确认）
# =========================================================
def factor_dhl(country):      return 200
def factor_raben_default():   return 200
def factor_schenker(country): return 150 if country.upper() == "DE" else 200
def factor_hellmann(country):
    cc = country.upper()
    rule = HELLMANN_RULES.get(cc)
    if rule and "vol_factor" in rule:
        return rule["vol_factor"]
    return 150 if cc == "DE" else 200
def factor_fedex(country):    return 200


# =========================================================
# UI
# =========================================================
st.title("Aifuge GmbH | Freight Cost Engine V5.7 (Auto-load from data/)")

# 读取所有表
try:
    dhl_df, dhl_wcols = load_rate_table(DHL_PATH)
except Exception as e:
    dhl_df, dhl_wcols = None, None
    st.warning(f"⚠️ DHL 价格表加载失败：{e}")

try:
    raben_df, raben_wcols = load_rate_table(RABEN_PATH)
except Exception as e:
    raben_df, raben_wcols = None, None
    st.warning(f"⚠️ Raben 价格表加载失败：{e}")

try:
    schenker_df, schenker_wcols = load_rate_table(SCHENKER_PATH)
except Exception as e:
    schenker_df, schenker_wcols = None, None
    st.warning(f"⚠️ Schenker 价格表加载失败：{e}")

try:
    hellmann_df, hellmann_wcols = load_rate_table(HELLMANN_PATH)
except Exception as e:
    hellmann_df, hellmann_wcols = None, None
    st.warning(f"⚠️ Hellmann 价格表加载失败：{e}")

try:
    fedex_df = load_fedex_table(FEDEX_PATH)
except Exception as e:
    fedex_df = None
    st.warning(f"⚠️ FedEx 价格表加载失败：{e}")

maut_df_norm = load_schenker_maut(MAUT_PATH)

with st.expander("📌 数据源检查（方案A：自动读取 data/）", expanded=False):
    st.write("如果某家没报价：通常是 data/ 下文件缺失、文件名不一致、或 key 不存在。")
    st.write("- DHL:", "OK" if dhl_df is not None else "NOT LOADED")
    st.write("- Raben:", "OK" if raben_df is not None else "NOT LOADED")
    st.write("- Schenker:", "OK" if schenker_df is not None else "NOT LOADED")
    st.write("- Hellmann:", "OK" if hellmann_df is not None else "NOT LOADED")
    st.write("- FedEx:", "OK" if fedex_df is not None else "NOT LOADED")
    st.write("- Schenker Maut:", "OK(标准列识别)" if maut_df_norm is not None else "NOT PARSED（将用手动输入）")

# 基础输入
c1, c2 = st.columns(2)
with c1:
    country = st.text_input("目的地国家代码（ISO2）", value="DE").upper().strip()
with c2:
    prefix2 = st.text_input("邮编前2位", value="38")

st.subheader("货物明细（逐件：实重 vs 体积重）")
base_df = pd.DataFrame([{"数量":1, "长(cm)":60, "宽(cm)":40, "高(cm)":40, "实重(kg/件)":20}])
cargo = st.data_editor(base_df, num_rows="dynamic", use_container_width=True).fillna(0)

need = ["数量","长(cm)","宽(cm)","高(cm)","实重(kg/件)"]
for c in need:
    if c not in cargo.columns:
        st.error("货物表字段缺失，请刷新页面。")
        st.stop()

# 指标
cargo["体积(m³)"] = (cargo["长(cm)"]/100)*(cargo["宽(cm)"]/100)*(cargo["高(cm)"]/100) * cargo["数量"]
total_volume = float(cargo["体积(m³)"].sum())
total_real_weight = float((cargo["实重(kg/件)"]*cargo["数量"]).sum())

# 逐家计费重
def calc_billable(carrier: str):
    cc = country.upper()
    if carrier == "DHL":
        factor = factor_dhl(cc)
    elif carrier == "RABEN":
        factor = factor_raben_default()
    elif carrier == "SCHENKER":
        factor = factor_schenker(cc)
    elif carrier == "HELLMANN":
        factor = factor_hellmann(cc)
    elif carrier == "FEDEX":
        factor = factor_fedex(cc)
    else:
        factor = 200

    vol_piece = cargo.apply(lambda r: volumetric_weight_cm(r["长(cm)"], r["宽(cm)"], r["高(cm)"], factor), axis=1)
    real_piece = cargo["实重(kg/件)"].astype(float)
    bill_piece = pd.concat([real_piece, vol_piece], axis=1).max(axis=1)

    # FedEx 每件最低 68kg
    if carrier == "FEDEX":
        bill_piece = bill_piece.apply(lambda x: max(float(x), 68.0))

    qty = cargo["数量"].astype(float)
    return float((bill_piece * qty).sum()), float(factor)

bw_dhl, f_dhl = calc_billable("DHL")
bw_rab, f_rab = calc_billable("RABEN")
bw_sch, f_sch = calc_billable("SCHENKER")
bw_hel, f_hel = calc_billable("HELLMANN")
bw_fdx, f_fdx = calc_billable("FEDEX")

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("总实重(kg)", f"{total_real_weight:.2f}")
m2.metric("总体积(m³)", f"{total_volume:.4f}")
m3.metric("DHL计费重(kg)", f"{bw_dhl:.2f}")
m4.metric("Hellmann计费重(kg)", f"{bw_hel:.2f}")
m5.metric("FedEx计费重(kg)", f"{bw_fdx:.2f}（含每件≥68kg）")

st.caption(f"体积系数：DHL={f_dhl} / Raben={f_rab} / Schenker={f_sch} / Hellmann={f_hel} / FedEx={f_fdx}")

st.divider()

# 附加费参数
st.subheader("附加费参数（可上线：先手动输入/勾选）")

p1, p2, p3, p4 = st.columns(4)
with p1:
    schenker_km = st.number_input("Schenker 距离KM（手动）", value=0.0, min_value=0.0)
with p2:
    schenker_floating_pct = st.number_input("Schenker Floating%（手动）", value=0.0, min_value=0.0)
with p3:
    hellmann_diesel_eur_l = st.number_input("Hellmann Diesel €/L（当月手动）", value=1.50, min_value=0.0, step=0.01)
with p4:
    longest_edge_cm = st.number_input("单件最长边(cm)（>240触发长度费）", value=0.0, min_value=0.0)

schenker_avis = st.checkbox("Schenker Avis（电话预约派送）+20€ / 票", value=False)

h1, h2, h3 = st.columns(3)
with h1:
    hellmann_b2c = st.checkbox("Hellmann B2C +8.9€ / 票", value=False)
with h2:
    hellmann_avis = st.checkbox("Hellmann Avis +12.5€ / 票", value=False)
with h3:
    hellmann_dg = st.checkbox("Hellmann 危险品DG（叠加）", value=False)

hellmann_length_fee = 30.0 if longest_edge_cm > 240 else 0.0

st.divider()

# 通用查表
def quote_from_table(carrier: str, df: pd.DataFrame, wcols, billable_weight: float):
    if df is None:
        return None, f"{carrier} 价格表未加载"
    key = build_key(carrier, country, prefix2)
    hit = df[df["key"] == key]
    if hit.empty:
        return None, f"未找到线路 key={key}"
    wcol = pick_weight_col(wcols, billable_weight)
    base = safe_float(hit.iloc[0][wcol], None)
    if base is None:
        return None, f"{carrier} 匹配到 {wcol} 但该格为空"
    return {"key": key, "wcol": wcol, "base": float(base)}, None

# DHL
def quote_dhl():
    q, err = quote_from_table("DHL", dhl_df, dhl_wcols, bw_dhl)
    if err:
        return {"carrier":"DHL","found":False,"error":err}
    total = q["base"]
    breakdown = [("基础运费", q["base"]), ("总计", total)]
    return {"carrier":"DHL","found":True, **q, "total":total, "breakdown":breakdown}

# Raben：未命中=不服务（🚫 Not served）
def quote_raben():
    q, err = quote_from_table("RABEN", raben_df, raben_wcols, bw_rab)
    if err:
        return {"carrier":"Raben","found":False,"not_served":True,"error":"Not served（Raben无此线路/区域覆盖）"}
    total = q["base"]
    breakdown = [("基础运费", q["base"]), ("总计", total)]
    return {"carrier":"Raben","found":True, **q, "total":total, "breakdown":breakdown}

# Schenker
def quote_schenker():
    q, err = quote_from_table("SCHENKER", schenker_df, schenker_wcols, bw_sch)
    if err:
        return {"carrier":"Schenker","found":False,"error":err}

    base = q["base"]
    floating_cost = base * float(schenker_floating_pct) / 100.0
    avis_cost = 20.0 if schenker_avis else 0.0

    maut_amount = lookup_maut(maut_df_norm, bw_sch, schenker_km)
    if maut_amount is None:
        maut_amount = st.number_input("Schenker Maut 金额（表未命中时手填）", value=0.0, min_value=0.0)
        maut_note = "（手填）"
    else:
        maut_note = "（表自动）"

    total = base + float(floating_cost) + float(avis_cost) + float(maut_amount)

    breakdown = [
        ("基础运费", base),
        (f"Floating {schenker_floating_pct:.2f}%", float(floating_cost)),
        ("Avis", float(avis_cost)),
        (f"Maut {maut_note}", float(maut_amount)),
        ("总计", total),
    ]
    return {"carrier":"Schenker","found":True, **q, "total":total, "breakdown":breakdown}

# Hellmann
def quote_hellmann():
    q, err = quote_from_table("HELLMANN", hellmann_df, hellmann_wcols, bw_hel)
    if err:
        return {"carrier":"Hellmann","found":False,"error":err}

    cc = country.upper()
    rule = HELLMANN_RULES.get(cc, {"maut_pct": 0.0, "state_pct": 0.0, "vol_factor": (150 if cc=="DE" else 200)})

    base = q["base"]
    maut_cost  = base * float(rule.get("maut_pct",0.0)) / 100.0
    state_cost = base * float(rule.get("state_pct",0.0)) / 100.0
    diesel_pct = hellmann_diesel_pct(hellmann_diesel_eur_l)
    diesel_cost = base * float(diesel_pct) / 100.0

    b2c_cost  = 8.9  if hellmann_b2c else 0.0
    avis_cost = 12.5 if hellmann_avis else 0.0

    dg_cost = 0.0
    if hellmann_dg:
        if cc == "DE":
            dg_cost = 15.0
        else:
            dg_cost = 75.0 if cc in HELLMANN_DG_75_COUNTRIES else 30.0

    length_cost = float(hellmann_length_fee)

    total = base + maut_cost + state_cost + diesel_cost + b2c_cost + avis_cost + dg_cost + length_cost

    breakdown = [
        ("基础运费", base),
        (f"Maut {rule.get('maut_pct',0.0)}%", maut_cost),
        (f"Staatliche Abgaben {rule.get('state_pct',0.0)}%", state_cost),
        (f"Diesel Floater {diesel_pct:.1f}%", diesel_cost),
        ("B2C", b2c_cost),
        ("Avis", avis_cost),
        ("危险品DG", dg_cost),
        ("长度费(>240cm)", length_cost),
        ("总计", total),
    ]
    return {"carrier":"Hellmann","found":True, **q, "total":total, "breakdown":breakdown}

# FedEx
def quote_fedex():
    if fedex_df is None:
        return {"carrier":"FedEx","found":False,"error":"FedEx 价格表未加载或格式不对（请整理为 country + eur_per_kg 两列）"}
    cc = country.upper()
    hit = fedex_df[fedex_df["country"] == cc]
    if hit.empty:
        return {"carrier":"FedEx","found":False,"error":f"FedEx 未找到国家 {cc} 的 €/kg"}
    eur_per_kg = safe_float(hit.iloc[0]["eur_per_kg"], None)
    if eur_per_kg is None:
        return {"carrier":"FedEx","found":False,"error":f"FedEx 国家 {cc} 的 €/kg 无效"}
    total = float(eur_per_kg) * float(bw_fdx)
    breakdown = [
        (f"费率 €/kg", float(eur_per_kg)),
        ("计费重(kg)（含每件≥68kg）", float(bw_fdx)),
        ("总计（已含燃油/路桥/Avis）", total),
    ]
    return {"carrier":"FedEx","found":True,"key":f"FEDEX-{cc}","wcol":"€/kg","base":total,"total":total,"breakdown":breakdown}

# 生成报价
q_dhl = quote_dhl()
q_rab = quote_raben()
q_sch = quote_schenker()
q_hel = quote_hellmann()
q_fdx = quote_fedex()

def summary_row(q):
    if not q.get("found"):
        if q.get("not_served"):
            return [q["carrier"], q.get("key","-"), "🚫", "-", "-", "-", q.get("error","")]
        return [q["carrier"], q.get("key","-"), "❌", "-", "-", "-", q.get("error","")]
    return [
        q["carrier"],
        q.get("key","-"),
        "✅",
        q.get("wcol","-"),
        f"{safe_float(q.get('base',0)):.2f}",
        f"{safe_float(q.get('total',0)):.2f}",
        ""
    ]

st.subheader("📊 五家同步报价对比")
df_compare = pd.DataFrame(
    [summary_row(q_dhl), summary_row(q_rab), summary_row(q_sch), summary_row(q_hel), summary_row(q_fdx)],
    columns=["承运商","线路Key","是否命中","匹配区间/模式","基础/费率(EUR)","总成本(EUR)","备注"]
)
st.dataframe(df_compare, use_container_width=True)

tabs = st.tabs(["DHL 明细","Raben 明细","Schenker 明细","Hellmann 明细","FedEx 明细","排错提示"])
for t, q in zip(tabs, [q_dhl, q_rab, q_sch, q_hel, q_fdx, None]):
    with t:
        if q is None:
            st.markdown("""
**常见原因：**
- ❌ 未找到线路 key=...：报价表里没有这条线路（国家+邮编前两位未覆盖 / key拼法不一致）。
- 🚫 Not served：表示承运商本来就不服务该区域（Raben已按这个逻辑处理）。
- Schenker Maut 查不到：说明 Maut 表没识别到标准列或范围没覆盖；可先手填上线。
- FedEx 未找到国家：FedEx表里没有该国家的 €/kg（country 列必须是 ISO2，如 PL/NL/DE）。
""")
        else:
            if not q.get("found"):
                st.error(q.get("error","Unknown error"))
            else:
                st.dataframe(pd.DataFrame(q["breakdown"], columns=["项目","金额(EUR)"]), use_container_width=True)

st.divider()

# 导出 Excel
def to_excel():
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        cargo.to_excel(writer, index=False, sheet_name="Cargo")
        df_compare.to_excel(writer, index=False, sheet_name="Compare")

        def dump_breakdown(q, name):
            if q.get("found"):
                pd.DataFrame(q["breakdown"], columns=["项目","金额(EUR)"]).to_excel(writer, index=False, sheet_name=name)

        dump_breakdown(q_dhl, "DHL_Cost")
        dump_breakdown(q_rab, "Raben_Cost")
        dump_breakdown(q_sch, "Schenker_Cost")
        dump_breakdown(q_hel, "Hellmann_Cost")
        dump_breakdown(q_fdx, "FedEx_Cost")

    return output.getvalue()

st.download_button(
    "下载Excel（Cargo + Compare + 5家明细）",
    data=to_excel(),
    file_name=f"Freight_Compare_V57_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
)
