# ============================================
# AIFUGE FREIGHT ENGINE V7.2 (FINAL STABLE)
# FedEx = matrix mode
# SELL = XLSX only
# ============================================

import os
import re
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from typing import Dict, List

import pandas as pd
import streamlit as st

APP_VERSION = "V7.2"
DATA_DIR = "data"

PATHS = {
    "DHL": f"{DATA_DIR}/DHL_Frachtkosten.xlsx",
    "RABEN": f"{DATA_DIR}/Raben_Frachtkosten.xlsx",
    "SCHENKER": f"{DATA_DIR}/Schenker_Frachtkosten.xlsx",
    "HELLMANN": f"{DATA_DIR}/Hellmann_Frachtkosten_2026.xlsx",
    "FEDEX": f"{DATA_DIR}/FedEx_Frachtkosten.xlsx",
    "SELL": f"{DATA_DIR}/超大件账号价格-2026.01.01.xlsx",
}

st.set_page_config(layout="wide")
st.title(f"Aifuge Freight Engine {APP_VERSION}")

# --------------------------------------------
# Helpers
# --------------------------------------------

def cc(x): return str(x).strip().upper()

def zip2(z):
    m = re.search(r"(\d{2})", str(z))
    return m.group(1) if m else ""

def key(prefix, country, zz):
    return f"{prefix}-{cc(country)}--{zz}"

def weight_cols(df):
    cols = [c for c in df.columns if str(c).lower().startswith("bis-")]
    def w(x):
        m = re.search(r"bis-(\d+)", str(x))
        return int(m.group(1)) if m else 999999
    return sorted(cols, key=w)

def pick(cols, w):
    for c in cols:
        m = re.search(r"bis-(\d+)", str(c))
        if m and w <= float(m.group(1)):
            return c
    return cols[-1]

# --------------------------------------------
# Load matrix
# --------------------------------------------

@st.cache_data
def load_matrix(path):
    df = pd.read_excel(path)
    df.columns = [str(c).strip() for c in df.columns]
    keycol = df.columns[0]
    cols = weight_cols(df)

    lut = {}
    for _, r in df.iterrows():
        k = str(r[keycol]).strip()
        if not k: continue
        lut[k] = {}
        for c in cols:
            if pd.notna(r[c]):
                lut[k][c] = float(r[c])
    return lut, cols

def load_safe(path):
    if not os.path.exists(path):
        return None, None
    return load_matrix(path)

DHL, DHL_COLS = load_safe(PATHS["DHL"])
RABEN, RABEN_COLS = load_safe(PATHS["RABEN"])
SCH, SCH_COLS = load_safe(PATHS["SCHENKER"])
HEL, HEL_COLS = load_safe(PATHS["HELLMANN"])
FX, FX_COLS = load_safe(PATHS["FEDEX"])

# --------------------------------------------
# SELL
# --------------------------------------------

@st.cache_data
def load_sell():
    if not os.path.exists(PATHS["SELL"]):
        return None, None
    df = pd.read_excel(PATHS["SELL"])
    df.columns = [str(c).strip() for c in df.columns]
    cols = weight_cols(df)
    keycol = df.columns[0]
    lut = {}
    for _, r in df.iterrows():
        k = str(r[keycol]).strip()
        if not k: continue
        lut[k] = {}
        for c in cols:
            if pd.notna(r[c]):
                lut[k][c] = float(r[c])
    return lut, cols

SELL, SELL_COLS = load_sell()

# --------------------------------------------
# Cargo
# --------------------------------------------

@dataclass
class Cargo:
    qty:int; weight:float; l:float; w:float; h:float

def piece_cbm(r): return (r.l*r.w*r.h)/1_000_000
def total_cbm(rows): return sum(piece_cbm(r)*r.qty for r in rows)
def total_w(rows): return sum(r.weight*r.qty for r in rows)

st.subheader("输入")
ccode = cc(st.text_input("国家","DE"))
plz = st.text_input("邮编","38112")
zz = zip2(plz)

cargo_df = st.data_editor(
    pd.DataFrame([{"qty":1,"weight":20,"l":60,"w":40,"h":40}]),
    num_rows="dynamic"
)

rows=[]
for _,r in cargo_df.iterrows():
    rows.append(Cargo(int(r.qty),float(r.weight),float(r.l),float(r.w),float(r.h)))

actual = total_w(rows)
cbm = total_cbm(rows)

charge = max(actual, cbm*200)

# --------------------------------------------
# Calculation
# --------------------------------------------

def calc(name,lut,cols):
    if not lut: return False,0,"无价格表"
    k = key(name,ccode,zz)
    if k not in lut: return False,0,f"未找到线路 {k}"
    bc = pick(cols,charge)
    return True,lut[k][bc],bc

results=[]

for name,lut,cols in [
("DHL",DHL,DHL_COLS),
("RABEN",RABEN,RABEN_COLS),
("SCHENKER",SCH,SCH_COLS),
("HELLMANN",HEL,HEL_COLS),
("FEDEX",FX,FX_COLS),
]:
    ok,val,info = calc(name,lut,cols)
    results.append([name,ok,val,info])

df = pd.DataFrame(results,columns=["Carrier","OK","Cost","Bracket"])

# SELL price
sell_key = key("SELL",ccode,zz)
if SELL and sell_key in SELL:
    bc = pick(SELL_COLS,charge)
    sell = SELL[sell_key][bc]
    df["Sell"] = sell
    df["Profit"] = sell - df["Cost"]

st.dataframe(df,use_container_width=True)

# --------------------------------------------
# Export
# --------------------------------------------

def export():
    output = BytesIO()
    with pd.ExcelWriter(output,engine="openpyxl") as w:
        df.to_excel(w,index=False)
    return output.getvalue()

st.download_button("导出Excel",data=export(),file_name="freight.xlsx")
