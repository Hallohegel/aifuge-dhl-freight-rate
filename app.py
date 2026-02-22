import streamlit as st
import pandas as pd
import re
from io import BytesIO
from datetime import datetime
from typing import Dict, Tuple, Optional, List, Any

st.set_page_config(page_title="Aifuge Freight Cost Engine V5.4", layout="wide")

# =========================================================
# 基础工具
# =========================================================
def normalize_prefix(prefix: Any) -> str:
    return re.sub(r"\D", "", str(prefix)).zfill(2)[:2]

def build_key(carrier: str, country: str, prefix2: Any) -> str:
    return f"{carrier.upper()}-{country.upper()}--{normalize_prefix(prefix2)}"

def try_float(x, default=0.0) -> float:
    try:
        if x is None:
            return float(default)
        if isinstance(x, str):
            x = x.replace(",", ".").strip()
        return float(x)
    except Exception:
        return float(default)

def volumetric_weight_kg_per_piece(l_cm: float, w_cm: float, h_cm: float, factor_kg_per_m3: float) -> float:
    # m³ * factor
    return (l_cm/100.0) * (w_cm/100.0) * (h_cm/100.0) * factor_kg_per_m3

def sorted_weight_cols(cols: List[str]) -> List[str]:
    # weight cols like bis-30, bis-50, ...
    wcols = []
    for c in cols:
        s = str(c).strip()
        if s.startswith("bis-"):
            try:
                int(s.split("-")[1])
                wcols.append(s)
            except Exception:
                pass
    return sorted(wcols, key=lambda x: int(str(x).split("-")[1]))

def pick_weight_col(wcols_sorted: List[str], billable_weight: float) -> Optional[str]:
    if not wcols_sorted:
        return None
    for c in wcols_sorted:
        upper = int(str(c).split("-")[1])
        if billable_weight <= upper:
            return c
    # 超过最大区间
    return None

def max_weight_upper(wcols_sorted: List[str]) -> Optional[int]:
    if not wcols_sorted:
        return None
    return int(wcols_sorted[-1].split("-")[1])

# =========================================================
# DHL Fuel（你原表）
# =========================================================
def dhl_diesel_surcharge_percent(price_cent_per_l: float) -> float:
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
        if low <= price_cent_per_l <= high:
            return float(pct)
    return 0.0

# =========================================================
# Hellmann Dieselfloater（按你截图那套：柴油 €/L -> %）
#  <=1.48:0; <=1.50:0.5; <=1.52:1.0; ... <=1.62:3.5
#  每再增加0.02€ -> +0.5%
# =========================================================
def hellmann_diesel_float_percent(diesel_eur_per_l: float) -> float:
    d = try_float(diesel_eur_per_l, 0.0)
    if d <= 1.48:
        return 0.0
    # 从 1.48 往上，每0.02增加0.5%，并且在 1.50 时应为0.5
    # 计算步数：ceil? 这里用 floor 以区间上限方式处理
    # 1.48~1.50 => 0.5
    step = int((d - 1.48) / 0.02)  # 1.50 => step=1
    return round(step * 0.5, 2)

# =========================================================
# Hellmann 国家规则字典（你发过的全部国家）
# 说明：数值是百分比（如 18.2% -> 0.182）
# =========================================================
HELLMANN_RULES_2026: Dict[str, Dict[str, float]] = {
    "DE": {"maut_pct": 0.182, "abgaben_pct": 0.0,  "factor": 150.0},
    "AT": {"maut_pct": 0.133, "abgaben_pct": 0.066, "factor": 200.0},
    "BE": {"maut_pct": 0.097, "abgaben_pct": 0.021, "factor": 200.0},
    "BG": {"maut_pct": 0.062, "abgaben_pct": 0.099, "factor": 200.0},
    "CZ": {"maut_pct": 0.086, "abgaben_pct": 0.054, "factor": 200.0},
    "DK": {"maut_pct": 0.086, "abgaben_pct": 0.001, "factor": 200.0},
    "EE": {"maut_pct": 0.072, "abgaben_pct": 0.0,   "factor": 200.0},
    "ES": {"maut_pct": 0.067, "abgaben_pct": 0.0,   "factor": 200.0},
    "FI": {"maut_pct": 0.048, "abgaben_pct": 0.031, "factor": 200.0},
    "FR": {"maut_pct": 0.077, "abgaben_pct": 0.005, "factor": 200.0},
    "GR": {"maut_pct": 0.078, "abgaben_pct": 0.10,  "factor": 200.0},
    "HR": {"maut_pct": 0.091, "abgaben_pct": 0.116, "factor": 200.0},  # 来自你图里的克罗地亚页
    "HU": {"maut_pct": 0.115, "abgaben_pct": 0.152, "factor": 200.0},
    "IE": {"maut_pct": 0.061, "abgaben_pct": 0.036, "factor": 200.0},
    "IT": {"maut_pct": 0.103, "abgaben_pct": 0.07,  "factor": 200.0},
    "LT": {"maut_pct": 0.076, "abgaben_pct": 0.0,   "factor": 200.0},
    "LU": {"maut_pct": 0.109, "abgaben_pct": 0.0,   "factor": 200.0},
    "LV": {"maut_pct": 0.07,  "abgaben_pct": 0.0,   "factor": 200.0},  # 来自你图里的拉脱维亚页
    "NL": {"maut_pct": 0.089, "abgaben_pct": 0.0,   "factor": 200.0},
    "PL": {"maut_pct": 0.102, "abgaben_pct": 0.026, "factor": 200.0},
    "PT": {"maut_pct": 0.077, "abgaben_pct": 0.0,   "factor": 200.0},
    "RO": {"maut_pct": 0.07,  "abgaben_pct": 0.106, "factor": 200.0},
    "SE": {"maut_pct": 0.036, "abgaben_pct": 0.007, "factor": 200.0},
    "SI": {"maut_pct": 0.125, "abgaben_pct": 0.153, "factor": 200.0},
    "SK": {"maut_pct": 0.085, "abgaben_pct": 0.059, "factor": 200.0},
    "XK": {"maut_pct": 0.034, "abgaben_pct": 0.043, "factor": 200.0},
}

# Hellmann DG 规则（你给的说明）
HELLMANN_DG_30_COUNTRIES = {
    "AL","AT","BA","BE","BG","CH","CZ","DK","EE","ES","FI","FR","HR","HU","IT","LT",
    "LU","LV","ME","MK","NL","PL","PT","RO","RS","SI","SK","XK"
}
HELLMANN_DG_75_COUNTRIES = {"FI","GB","GR","IE","NO","SE"}  # 你写的是 FI, GB, GR, IE, NO, SE

# =========================================================
# 读取“统一上传格式”的运价表（第一列=key，后面是 bis-xx）
# =========================================================
@st.cache_data(show_spinner=False)
def load_rate_table_from_excel(file_bytes: bytes, sheet_name=0) -> Tuple[pd.DataFrame, str, List[str]]:
    df = pd.read_excel(BytesIO(file_bytes), sheet_name=sheet_name)
    if df is None or df.empty:
        raise ValueError("Excel为空或无法读取。")
    key_col = df.columns[0]
    wcols = sorted_weight_cols([str(c) for c in df.columns])
    if not wcols:
        raise ValueError("未找到重量区间列（要求列名类似 bis-30, bis-50 ...）。")
    # key列统一转字符串
    df[key_col] = df[key_col].astype(str).str.strip()
    return df, key_col, wcols

def load_rate_table_uploader(uploader, fallback_path: Optional[str], sheet_name=0):
    if uploader is not None:
        file_bytes = uploader.getvalue()
        return load_rate_table_from_excel(file_bytes, sheet_name=sheet_name)
    if fallback_path:
        # 允许你在 data/ 下放默认文件
        try:
            with open(fallback_path, "rb") as f:
                return load_rate_table_from_excel(f.read(), sheet_name=sheet_name)
        except Exception:
            pass
    return None, None, None

# =========================================================
# FedEx：读取国家 €/kg
# 兼容两种格式：
#  A) columns: country, eur_per_kg
#  B) 任意列，只要能识别国家列 + 单价列
# =========================================================
@st.cache_data(show_spinner=False)
def load_fedex_rate(file_bytes: bytes) -> pd.DataFrame:
    df = pd.read_excel(BytesIO(file_bytes), sheet_name=0)
    if df is None or df.empty:
        raise ValueError("FedEx Excel为空或无法读取。")

    cols = [str(c).strip().lower() for c in df.columns]
    df.columns = cols

    # 猜测国家列
    country_candidates = [c for c in cols if c in ("country", "land", "laender", "国家", "country_code")]
    price_candidates = [c for c in cols if "kg" in c or "eur" in c or "price" in c or "rate" in c or "preis" in c]

    if not country_candidates:
        # fallback：第一列
        country_col = cols[0]
    else:
        country_col = country_candidates[0]

    # 价格列优先找 "eur_per_kg" 或包含 kg
    price_col = None
    for c in cols:
        if c in ("eur_per_kg", "rate_eur_per_kg"):
            price_col = c
            break
    if price_col is None:
        for c in cols:
            if "kg" in c and ("eur" in c or "preis" in c or "rate" in c):
                price_col = c
                break
    if price_col is None:
        # fallback：第二列
        if len(cols) < 2:
            raise ValueError("FedEx表无法识别价格列（需要国家 + €/kg）。")
        price_col = cols[1]

    out = df[[country_col, price_col]].copy()
    out.columns = ["country", "eur_per_kg"]
    out["country"] = out["country"].astype(str).str.upper().str.strip()
    out["eur_per_kg"] = out["eur_per_kg"].apply(lambda x: try_float(x, 0.0))
    out = out[out["country"].str.len() > 0]
    out = out[out["eur_per_kg"] > 0]
    return out

# =========================================================
# Schenker Maut：优先读取你上传的 Mauttabelle（如果无法解析就手动输入）
# 这里做一个“通用网格”解析：
# - 第一列是重量区间（bis-xxx 或 xxx）
# - 后续列是距离区间（bis-100, bis-200... 或 0-100 等）
# 单元格就是对应 Maut 金额
# =========================================================
@st.cache_data(show_spinner=False)
def load_schenker_maut_table(file_bytes: bytes) -> Tuple[pd.DataFrame, str, List[str], List[str]]:
    df = pd.read_excel(BytesIO(file_bytes), sheet_name=0)
    if df is None or df.empty:
        raise ValueError("Maut表为空或无法读取。")

    df = df.copy()
    first_col = df.columns[0]
    df[first_col] = df[first_col].astype(str).str.strip()

    # 识别重量行：第一列中含 bis- 或纯数字
    # 识别距离列：列名含 bis- 或类似 0-100
    dist_cols = []
    for c in df.columns[1:]:
        s = str(c).strip()
        if s.startswith("bis-"):
            dist_cols.append(s)
        elif re.match(r"^\d+\s*-\s*\d+$", s):
            dist_cols.append(s)
        elif re.match(r"^\d+$", s):
            dist_cols.append(s)
    # 如果列名没识别出来，就直接用除第一列外全部列
    if not dist_cols:
        dist_cols = [str(c).strip() for c in df.columns[1:]]

    # 将列名标准化为字符串
    df.columns = [str(c).strip() for c in df.columns]
    return df, str(first_col), dist_cols, [str(c).strip() for c in df.columns]

def pick_dist_col(dist_cols: List[str], km: float) -> Optional[str]:
    if not dist_cols:
        return None
    for c in dist_cols:
        s = str(c).strip()
        if s.startswith("bis-"):
            try:
                upper = float(s.split("-")[1])
                if km <= upper:
                    return c
            except Exception:
                pass
        m = re.match(r"^(\d+)\s*-\s*(\d+)$", s)
        if m:
            low = float(m.group(1))
            high = float(m.group(2))
            if low <= km <= high:
                return c
        if re.match(r"^\d+$", s):
            # 把纯数字当 upper
            if km <= float(s):
                return c
    return None

def pick_weight_row(df: pd.DataFrame, weight_col_name: str, billable_weight: float) -> Optional[pd.Series]:
    # 允许 weight_col 里是 "bis-2500" 或 "2500"
    best_row = None
    best_upper = None
    for _, r in df.iterrows():
        s = str(r[weight_col_name]).strip()
        upper = None
        if s.startswith("bis-"):
            try:
                upper = float(s.split("-")[1])
            except Exception:
                continue
        else:
            try:
                upper = float(re.sub(r"[^\d.]", "", s))
            except Exception:
                continue
        if upper is None:
            continue
        if billable_weight <= upper:
            if best_upper is None or upper < best_upper:
                best_upper = upper
                best_row = r
    return best_row

# =========================================================
# 货物输入 & 计费重计算（支持 FedEx 每件最低 68kg）
# =========================================================
def compute_cargo(df_input: pd.DataFrame, factor: float, per_piece_min_weight: Optional[float] = None) -> Tuple[pd.DataFrame, float, float, float, float, float]:
    data = df_input.copy()

    needed = ["数量","长(cm)","宽(cm)","高(cm)","实重(kg)"]
    for c in needed:
        if c not in data.columns:
            raise ValueError(f"货物表缺字段：{c}")

    data = data.fillna(0)
    # 强制数值
    for c in needed:
        data[c] = data[c].apply(lambda x: try_float(x, 0.0))

    data["体积(m³)"] = (data["长(cm)"]/100.0) * (data["宽(cm)"]/100.0) * (data["高(cm)"]/100.0) * data["数量"]
    data["体积重(kg/件)"] = data.apply(lambda r: volumetric_weight_kg_per_piece(r["长(cm)"], r["宽(cm)"], r["高(cm)"], factor), axis=1)
    data["计费重(kg/件)"] = data[["实重(kg)", "体积重(kg/件)"]].max(axis=1)

    if per_piece_min_weight is not None:
        data["计费重(kg/件)"] = data["计费重(kg/件)"].apply(lambda x: max(float(x), float(per_piece_min_weight)))

    data["实重合计(kg)"] = data["实重(kg)"] * data["数量"]
    data["计费重合计(kg)"] = data["计费重(kg/件)"] * data["数量"]

    total_real = float(data["实重合计(kg)"].sum())
    total_vol = float(data["体积(m³)"].sum())
    total_vol_w = float(total_vol * factor)
    total_charge = float(data["计费重合计(kg)"].sum())
    max_len = float((data["长(cm)"]).max() if len(data) else 0)
    max_edge = float(max(data["长(cm)"].max(), data["宽(cm)"].max(), data["高(cm)"].max()) if len(data) else 0)

    return data, total_real, total_vol, total_vol_w, total_charge, max_edge

# =========================================================
# 运价查询：统一 key + bis-xx
# =========================================================
def quote_from_table(df: pd.DataFrame, key_col: str, wcols: List[str], key: str, billable_weight: float, min_charge: float = 0.0) -> Dict[str, Any]:
    row = df[df[key_col].astype(str).str.strip() == str(key).strip()]
    if row.empty:
        return {"found": False, "error": f"未找到线路 key={key}"}

    wcols_sorted = sorted_weight_cols(wcols)
    col = pick_weight_col(wcols_sorted, billable_weight)
    if col is None:
        mx = max_weight_upper(wcols_sorted)
        return {"found": False, "error": f"计费重 {billable_weight:.2f}kg 超过最大区间（max={mx}kg），无报价。", "max": mx}

    base = try_float(row.iloc[0][col], 0.0)
    base_after_min = max(base, float(min_charge))
    return {"found": True, "weight_col": col, "base": base_after_min, "base_raw": base}

# =========================================================
# UI：上传文件（优先用上传，没有则用 data/ 默认）
# =========================================================
st.title("Aifuge GmbH | Freight Cost Engine V5.4 (DHL + Raben + Schenker + Hellmann + FedEx)")

with st.sidebar:
    st.header("上传报价表（优先使用上传）")
    up_dhl = st.file_uploader("DHL 价格表（系统格式 xlsx）", type=["xlsx"], key="up_dhl")
    up_raben = st.file_uploader("Raben 价格表（系统格式 xlsx）", type=["xlsx"], key="up_raben")
    up_schenker = st.file_uploader("Schenker 价格表（系统格式 xlsx）", type=["xlsx"], key="up_schenker")
    up_maut = st.file_uploader("Schenker Mauttabelle（xlsx，可选）", type=["xlsx"], key="up_maut")
    up_hellmann = st.file_uploader("Hellmann 价格表（系统格式 xlsx）", type=["xlsx"], key="up_hellmann")
    up_fedex = st.file_uploader("FedEx 价格表（国家 €/kg xlsx）", type=["xlsx"], key="up_fedex")

    st.caption("如果你在云端部署，建议全部用上传，不依赖 data/ 目录。")

# 你如果本地 data/ 里有默认文件，也能跑（没有也没关系）
DHL_DEFAULT = "data/DHL_Frachtkosten.xlsx"
RABEN_DEFAULT = "data/Raben_Frachtkosten.xlsx"
SCHENKER_DEFAULT = "data/Schenker_Frachtkosten.xlsx"
MAUT_DEFAULT = "data/Mauttabelle_Schenker.xlsx"
HELLMANN_DEFAULT = "data/Hellmann_Frachtkosten_2026.xlsx"
FEDEX_DEFAULT = "data/FedEx_Frachtkosten.xlsx"

# 尝试加载（失败不会直接 stop，只会在报价时提示）
dhl_df, dhl_key_col, dhl_wcols = load_rate_table_uploader(up_dhl, DHL_DEFAULT, sheet_name=0)
raben_df, raben_key_col, raben_wcols = load_rate_table_uploader(up_raben, RABEN_DEFAULT, sheet_name=0)
schenker_df, schenker_key_col, schenker_wcols = load_rate_table_uploader(up_schenker, SCHENKER_DEFAULT, sheet_name=0)
hellmann_df, hellmann_key_col, hellmann_wcols = load_rate_table_uploader(up_hellmann, HELLMANN_DEFAULT, sheet_name=0)

maut_df = None
maut_weight_col = None
maut_dist_cols = None
if up_maut is not None:
    try:
        maut_df, maut_weight_col, maut_dist_cols, _ = load_schenker_maut_table(up_maut.getvalue())
    except Exception as e:
        st.sidebar.warning(f"Maut表读取失败，将改用手动输入：{e}")
elif False:
    # 如你要启用默认 maut 文件，把 False 改 True
    pass

fedex_rates = None
if up_fedex is not None:
    try:
        fedex_rates = load_fedex_rate(up_fedex.getvalue())
    except Exception as e:
        st.sidebar.warning(f"FedEx表读取失败：{e}")

# =========================================================
# 基础输入
# =========================================================
col1, col2, col3 = st.columns([1, 1, 2])
with col1:
    country = st.text_input("目的地国家代码（DE/NL/FR/…）", value="DE").upper().strip()
with col2:
    prefix2 = st.text_input("邮编前2位（DE/多数国家用）", value="38")
with col3:
    st.write("")

st.subheader("货物明细（输入左侧，右侧自动计算）")
base_df = pd.DataFrame([{"数量":1,"长(cm)":60,"宽(cm)":40,"高(cm)":40,"实重(kg)":20}])
cargo_input = st.data_editor(base_df, num_rows="dynamic", use_container_width=True)

st.divider()

# =========================================================
# 各承运商参数（含 factor / 附加费）
# =========================================================
st.subheader("参数 & 附加费（生产口径）")

p1, p2, p3, p4 = st.columns(4)

# DHL
with p1:
    st.markdown("### DHL")
    dhl_diesel_cent = st.number_input("DHL 柴油价格（Cent/L）", value=185.0, step=0.5)
    dhl_min_charge = st.number_input("DHL 最低收费（€，可为0）", value=0.0, step=1.0)
    dhl_avis = st.checkbox("DHL Avisierung（示例 11€）", value=False)
    dhl_avis_cost = 11.0 if dhl_avis else 0.0

# Raben
with p2:
    st.markdown("### Raben")
    raben_min_charge = st.number_input("Raben 最低收费（€，可为0）", value=0.0, step=1.0)
    raben_factor = st.number_input("Raben 体积系数 factor（kg/m³）", value=200.0, step=10.0)

# Schenker
with p3:
    st.markdown("### Schenker / DSV")
    sch_min_charge = st.number_input("Schenker 最低收费（€，可为0）", value=0.0, step=1.0)
    sch_factor = 150.0 if country == "DE" else 200.0
    st.caption(f"Schenker factor 自动：{'150(DE)' if country=='DE' else '200(其它)'}")
    sch_floating_pct = st.number_input("Schenker Floating（% 手动）", value=8.5, step=0.1)
    sch_km = st.number_input("Schenker 距离 KM（手动输入）", value=0.0, step=1.0)
    sch_maut_manual = st.number_input("Schenker Maut（€ 手动覆盖，留0=用表/不加）", value=0.0, step=1.0)
    sch_avis = st.checkbox("Schenker Avis（电话预约派送 20€）", value=False)
    sch_avis_cost = 20.0 if sch_avis else 0.0

# Hellmann
with p4:
    st.markdown("### Hellmann")
    hell_min_charge = st.number_input("Hellmann 最低收费（€，可为0）", value=0.0, step=1.0)
    hell_rule = HELLMANN_RULES_2026.get(country, {"maut_pct": 0.0, "abgaben_pct": 0.0, "factor": (150.0 if country=="DE" else 200.0)})
    hell_factor = float(hell_rule.get("factor", 200.0))
    st.caption(f"Hellmann factor 自动：{hell_factor:g} kg/m³")
    hell_diesel_eur_l = st.number_input("Hellmann Diesel（€/L，用于 Dieselfloat）", value=1.50, step=0.01)
    hell_b2c = st.checkbox("Hellmann B2C（8.9€/票）", value=False)
    hell_avis = st.checkbox("Hellmann Avis（12.5€/票）", value=False)
    hell_dg = st.checkbox("Hellmann 危险品 DG（叠加）", value=False)

st.divider()

# =========================================================
# 计算各承运商计费重
# =========================================================
# DHL factor
dhl_factor = 200.0  # 你要求：DHL DE/其它都 200

# FedEx factor & per-piece min
fedex_factor = 200.0
fedex_piece_min = 68.0

# 先统一算最大边（用于 Hellmann length surcharge）
#（对各家都统一从 cargo_input 算）
try:
    _, _, _, _, _, max_edge_cm_global = compute_cargo(cargo_input, factor=200.0, per_piece_min_weight=None)
except Exception as e:
    st.error(f"货物表输入有问题：{e}")
    st.stop()

hell_length_surcharge = 30.0 if max_edge_cm_global > 240.0 else 0.0  # >240cm 触发
hell_b2c_cost = 8.9 if hell_b2c else 0.0
hell_avis_cost = 12.5 if hell_avis else 0.0

# Hellmann DG 成本（按国家组）
def hellmann_dg_cost(country_code: str, enabled: bool) -> float:
    if not enabled:
        return 0.0
    c = country_code.upper()
    if c == "DE":
        return 15.0
    if c in HELLMANN_DG_75_COUNTRIES:
        return 75.0
    if c in HELLMANN_DG_30_COUNTRIES:
        return 30.0
    # 未覆盖国家：默认 30（你后续可补）
    return 30.0

hell_dg_cost = hellmann_dg_cost(country, hell_dg)

# 计算每家货物汇总（各家 factor 不同）
try:
    cargo_dhl, dhl_real, dhl_vol, dhl_vol_w, dhl_bill_w, _ = compute_cargo(cargo_input, factor=dhl_factor, per_piece_min_weight=None)
    cargo_raben, raben_real, raben_vol, raben_vol_w, raben_bill_w, _ = compute_cargo(cargo_input, factor=raben_factor, per_piece_min_weight=None)
    cargo_sch, sch_real, sch_vol, sch_vol_w, sch_bill_w, _ = compute_cargo(cargo_input, factor=sch_factor, per_piece_min_weight=None)
    cargo_hell, hell_real, hell_vol, hell_vol_w, hell_bill_w, _ = compute_cargo(cargo_input, factor=hell_factor, per_piece_min_weight=None)
    cargo_fedex, fed_real, fed_vol, fed_vol_w, fed_bill_w, _ = compute_cargo(cargo_input, factor=fedex_factor, per_piece_min_weight=fedex_piece_min)
except Exception as e:
    st.error(f"货物计算失败：{e}")
    st.stop()

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("DHL 计费重(kg)", f"{dhl_bill_w:.2f}")
m2.metric("Raben 计费重(kg)", f"{raben_bill_w:.2f}")
m3.metric("Schenker 计费重(kg)", f"{sch_bill_w:.2f}")
m4.metric("Hellmann 计费重(kg)", f"{hell_bill_w:.2f}")
m5.metric("FedEx 计费重(kg)", f"{fed_bill_w:.2f} (含每件≥68kg)")

st.divider()

# =========================================================
# 报价逻辑（逐家）
# =========================================================
MARPOL_COUNTRIES = {"DK","EE","FI","GB","IE","LT","LV","NO","SE"}
def quote_dhl() -> Dict[str, Any]:
    if dhl_df is None:
        return {"found": False, "error": "DHL 价格表未加载（请上传）"}
    key = build_key("DHL", country, prefix2)
    q = quote_from_table(dhl_df, dhl_key_col, dhl_wcols, key, dhl_bill_w, min_charge=dhl_min_charge)
    if not q.get("found"):
        q["carrier"] = "DHL"
        q["key"] = key
        return q

    base = float(q["base"])
    fuel_pct = dhl_diesel_surcharge_percent(try_float(dhl_diesel_cent, 0.0))
    fuel_cost = base * fuel_pct / 100.0
    marpol_cost = base * 0.04 if country in MARPOL_COUNTRIES else 0.0
    ekaer_cost = 10.0 if country == "HU" else 0.0

    total = base + fuel_cost + marpol_cost + ekaer_cost + float(dhl_avis_cost)

    breakdown = pd.DataFrame([
        ["基础运费", base],
        [f"燃油附加费({fuel_pct:.1f}%)", fuel_cost],
        ["MARPOL(4%)", marpol_cost],
        ["EKAER(HU)", ekaer_cost],
        ["Avisierung", float(dhl_avis_cost)],
        ["总计", total],
    ], columns=["项目", "金额(EUR)"])

    return {
        "carrier":"DHL", "key":key, "found":True,
        "weight_col": q["weight_col"], "base":base, "total":total, "breakdown":breakdown
    }

def quote_raben() -> Dict[str, Any]:
    if raben_df is None:
        return {"found": False, "error": "Raben 价格表未加载（请上传）"}
    key = build_key("RABEN", country, prefix2)
    q = quote_from_table(raben_df, raben_key_col, raben_wcols, key, raben_bill_w, min_charge=raben_min_charge)
    if not q.get("found"):
        q["carrier"] = "RABEN"
        q["key"] = key
        return q
    base = float(q["base"])
    total = base
    breakdown = pd.DataFrame([
        ["基础运费", base],
        ["总计", total],
    ], columns=["项目", "金额(EUR)"])
    return {"carrier":"RABEN","key":key,"found":True,"weight_col":q["weight_col"],"base":base,"total":total,"breakdown":breakdown}

def compute_schenker_maut_amount(billable_weight: float, km: float) -> float:
    # 手动覆盖优先
    if try_float(sch_maut_manual, 0.0) > 0:
        return float(sch_maut_manual)

    # 有表则按表算
    if maut_df is None or maut_weight_col is None or maut_dist_cols is None:
        return 0.0

    row = pick_weight_row(maut_df, maut_weight_col, billable_weight)
    if row is None:
        return 0.0
    dc = pick_dist_col(maut_dist_cols, km)
    if dc is None:
        return 0.0

    v = row.get(dc, 0.0)
    return try_float(v, 0.0)

def quote_schenker() -> Dict[str, Any]:
    if schenker_df is None:
        return {"found": False, "error": "Schenker 价格表未加载（请上传）"}
    key = build_key("SCHENKER", country, prefix2)
    q = quote_from_table(schenker_df, schenker_key_col, schenker_wcols, key, sch_bill_w, min_charge=sch_min_charge)
    if not q.get("found"):
        q["carrier"] = "SCHENKER"
        q["key"] = key
        return q

    base = float(q["base"])
    floating_cost = base * try_float(sch_floating_pct, 0.0) / 100.0
    maut_cost = compute_schenker_maut_amount(sch_bill_w, try_float(sch_km, 0.0))
    total = base + floating_cost + maut_cost + float(sch_avis_cost)

    breakdown = pd.DataFrame([
        ["基础运费", base],
        [f"Floating({try_float(sch_floating_pct,0.0):.2f}%)", floating_cost],
        ["Maut", maut_cost],
        ["Avis(电话预约派送)", float(sch_avis_cost)],
        ["总计", total],
    ], columns=["项目", "金额(EUR)"])

    return {"carrier":"SCHENKER","key":key,"found":True,"weight_col":q["weight_col"],"base":base,"total":total,"breakdown":breakdown}

def quote_hellmann() -> Dict[str, Any]:
    if hellmann_df is None:
        return {"found": False, "error": "Hellmann 价格表未加载（请上传）"}
    key = build_key("HELLMANN", country, prefix2)
    q = quote_from_table(hellmann_df, hellmann_key_col, hellmann_wcols, key, hell_bill_w, min_charge=hell_min_charge)
    if not q.get("found"):
        q["carrier"] = "HELLMANN"
        q["key"] = key
        return q

    base = float(q["base"])
    rule = HELLMANN_RULES_2026.get(country, {"maut_pct":0.0,"abgaben_pct":0.0,"factor":(150.0 if country=="DE" else 200.0)})
    maut_pct = float(rule.get("maut_pct", 0.0))
    abg_pct  = float(rule.get("abgaben_pct", 0.0))

    maut_cost = base * maut_pct
    abg_cost  = base * abg_pct

    diesel_pct = hellmann_diesel_float_percent(try_float(hell_diesel_eur_l, 0.0))
    diesel_cost = base * diesel_pct / 100.0

    total = base + maut_cost + abg_cost + diesel_cost + hell_b2c_cost + hell_avis_cost + hell_dg_cost + hell_length_surcharge

    breakdown = pd.DataFrame([
        ["基础运费", base],
        [f"Maut({maut_pct*100:.2f}%)", maut_cost],
        [f"Staatliche Abgaben({abg_pct*100:.2f}%)", abg_cost],
        [f"Dieselfloat({diesel_pct:.2f}%)", diesel_cost],
        ["B2C", float(hell_b2c_cost)],
        ["Avis", float(hell_avis_cost)],
        ["危险品 DG", float(hell_dg_cost)],
        ["Längenzuschlag(>240cm)", float(hell_length_surcharge)],
        ["总计", total],
    ], columns=["项目", "金额(EUR)"])

    return {"carrier":"HELLMANN","key":key,"found":True,"weight_col":q["weight_col"],"base":base,"total":total,"breakdown":breakdown}

def quote_fedex() -> Dict[str, Any]:
    if fedex_rates is None:
        return {"found": False, "error": "FedEx 价格表未加载（请上传）"}
    c = country.upper().strip()
    row = fedex_rates[fedex_rates["country"] == c]
    if row.empty:
        return {"found": False, "error": f"FedEx 未找到国家 {c} 的 €/kg"}
    eur_per_kg = float(row.iloc[0]["eur_per_kg"])
    base = eur_per_kg * float(fed_bill_w)
    # FedEx 已包含 fuel/maut/avis，按你口径不加任何附加费
    breakdown = pd.DataFrame([
        [f"费率(€/kg) = {eur_per_kg:.4f}", 0.0],
        [f"计费重(kg)（含每件>=68kg & factor=200）", float(fed_bill_w)],
        ["费用", base],
        ["总计", base],
    ], columns=["项目", "金额(EUR)"])
    return {"carrier":"FEDEX","key":f"FEDEX-{c}","found":True,"weight_col":"€/kg","base":base,"total":base,"breakdown":breakdown}

# =========================================================
# 生成报价
# =========================================================
q1 = quote_dhl()
q2 = quote_raben()
q3 = quote_schenker()
q4 = quote_hellmann()
q5 = quote_fedex()

def summary_row(q: Dict[str, Any]) -> List[Any]:
    if not q.get("found"):
        return [q.get("carrier","-"), q.get("key","-"), "❌", "-", "-", "-", q.get("error","")]
    return [q["carrier"], q["key"], "✅", q.get("weight_col","-"), f"{q.get('base',0.0):.2f}", f"{q.get('total',0.0):.2f}", ""]

df_compare = pd.DataFrame(
    [summary_row(q) for q in [q1,q2,q3,q4,q5]],
    columns=["承运商","线路Key","是否命中","匹配区间","基础/费用(EUR)","总成本(EUR)","备注"]
)

st.subheader("📌 五家同步报价对比")
st.dataframe(df_compare, use_container_width=True)

tabs = st.tabs(["DHL 明细", "Raben 明细", "Schenker 明细", "Hellmann 明细", "FedEx 明细"])
for tab, q in zip(tabs, [q1,q2,q3,q4,q5]):
    with tab:
        if q.get("found"):
            st.dataframe(q["breakdown"], use_container_width=True)
        else:
            st.error(q.get("error","未知错误"))

st.divider()

# =========================================================
# 导出 Excel（Cargo + Compare + 各家明细）
# =========================================================
def to_excel() -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        cargo_input.to_excel(writer, index=False, sheet_name="Cargo_Input")
        # 把每家货物计算也写进去，方便核对
        cargo_dhl.to_excel(writer, index=False, sheet_name="Cargo_DHL")
        cargo_raben.to_excel(writer, index=False, sheet_name="Cargo_Raben")
        cargo_sch.to_excel(writer, index=False, sheet_name="Cargo_Schenker")
        cargo_hell.to_excel(writer, index=False, sheet_name="Cargo_Hellmann")
        cargo_fedex.to_excel(writer, index=False, sheet_name="Cargo_FedEx")

        df_compare.to_excel(writer, index=False, sheet_name="Compare")

        if q1.get("found"): q1["breakdown"].to_excel(writer, index=False, sheet_name="DHL_Cost")
        if q2.get("found"): q2["breakdown"].to_excel(writer, index=False, sheet_name="Raben_Cost")
        if q3.get("found"): q3["breakdown"].to_excel(writer, index=False, sheet_name="Schenker_Cost")
        if q4.get("found"): q4["breakdown"].to_excel(writer, index=False, sheet_name="Hellmann_Cost")
        if q5.get("found"): q5["breakdown"].to_excel(writer, index=False, sheet_name="FedEx_Cost")

    return output.getvalue()

st.download_button(
    "下载核算Excel（Cargo + Compare + 5家明细）",
    data=to_excel(),
    file_name=f"Freight_Compare_V5_4_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
)

# =========================================================
# 运行期自检提示（帮助你快速定位“为什么没命中”）
# =========================================================
with st.expander("🔧 自检信息（上线后可隐藏）", expanded=False):
    st.write("如果某家没命中，通常是：key 拼法不一致 / 邮编前两位不在表 / 超过最大重量区间。")
    st.write("当前输入：")
    st.json({
        "country": country,
        "prefix2": normalize_prefix(prefix2),
        "max_edge_cm": max_edge_cm_global,
        "billable_kg": {
            "DHL": dhl_bill_w,
            "Raben": raben_bill_w,
            "Schenker": sch_bill_w,
            "Hellmann": hell_bill_w,
            "FedEx": fed_bill_w,
        },
        "factors": {
            "DHL": dhl_factor,
            "Raben": raben_factor,
            "Schenker": sch_factor,
            "Hellmann": hell_factor,
            "FedEx": fedex_factor,
        },
        "hellmann_rule_used": hell_rule,
    })
