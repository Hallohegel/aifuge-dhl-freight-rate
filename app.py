# ============================================
# AIFUGE FREIGHT ENGINE V7 (PRODUCTION LOCKED)
# ============================================

import os
import re
import math
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from typing import Dict, List, Tuple

import pandas as pd
import streamlit as st


APP_VERSION = "V7"
DATA_DIR = "data"

# =============================
# File paths (must match repo)
# =============================
PATHS = {
    "DHL": f"{DATA_DIR}/DHL_Frachtkosten.xlsx",
    "RABEN": f"{DATA_DIR}/Raben_Frachtkosten.xlsx",
    "SCHENKER": f"{DATA_DIR}/Schenker_Frachtkosten.xlsx",
    "HELLMANN": f"{DATA_DIR}/Hellmann_Frachtkosten_2026.xlsx",
    "FEDEX": f"{DATA_DIR}/FedEx_Frachtkosten.xlsx",
    "SELL_XLS": f"{DATA_DIR}/超大件账号价格-2026.01.01.xls",
    "SELL_XLSX": f"{DATA_DIR}/超大件账号价格-2026.01.01.xlsx",
}

st.set_page_config(page_title="Aifuge Freight Engine V7", layout="wide")
st.title(f"Aifuge Freight Engine {APP_VERSION} — 成本 + 对客报价 + 毛利")

# ============================================
# Helpers
# ============================================

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

def pick_bracket(cols, w):
    for c in cols:
        m = re.search(r"bis-(\d+)", str(c))
        if m and w <= float(m.group(1)):
            return c
    return cols[-1]

# ============================================
# Cargo model
# ============================================

@dataclass
class Cargo:
    qty: int
    weight: float
    l: float
    w: float
    h: float

def piece_cbm(r): return (r.l*r.w*r.h)/1_000_000

def total_cbm(rows): return sum(piece_cbm(r)*r.qty for r in rows)

def total_actual(rows): return sum(r.weight*r.qty for r in rows)

def chargeable(actual, cbm, factor): return max(actual, cbm*factor)

def fedex_charge(rows):
    total=0
    for r in rows:
        vol=piece_cbm(r)*200
        per=max(r.weight,vol,68)
        total+=per*r.qty
    return total

# ============================================
# Load matrix tables
# ============================================

@st.cache_data
def load_matrix(path):
    df=pd.read_excel(path)
    df.columns=[str(c).strip() for c in df.columns]
    keycol=df.columns[0]
    cols=weight_cols(df)
    lut={}
    for _,r in df.iterrows():
        k=str(r[keycol]).strip()
        if not k: continue
        lut[k]={}
        for c in cols:
            if pd.notna(r[c]):
                lut[k][c]=float(r[c])
    return lut,cols

def load_safe(label,path):
    if not os.path.exists(path):
        st.warning(f"{label} 文件不存在")
        return None,None
    return load_matrix(path)

DHL_LUT,DHL_COLS=load_safe("DHL",PATHS["DHL"])
RABEN_LUT,RABEN_COLS=load_safe("Raben",PATHS["RABEN"])
SCH_LUT,SCH_COLS=load_safe("Schenker",PATHS["SCHENKER"])
HEL_LUT,HEL_COLS=load_safe("Hellmann",PATHS["HELLMANN"])

@st.cache_data
def load_fedex(path):
    df=pd.read_excel(path)
    df.columns=[str(c).strip() for c in df.columns]
    lut={}
    for _,r in df.iterrows():
        lut[f"FEDEX-{cc(r[df.columns[0]])}"]=float(r[df.columns[1]])
    return lut

FEDEX_LUT=load_fedex(PATHS["FEDEX"]) if os.path.exists(PATHS["FEDEX"]) else {}

# ============================================
# Sell price table
# ============================================

@st.cache_data
def load_sell():
    p=PATHS["SELL_XLSX"] if os.path.exists(PATHS["SELL_XLSX"]) else PATHS["SELL_XLS"]
    if not os.path.exists(p):
        return None,None
    df=pd.read_excel(p)
    df.columns=[str(c).strip() for c in df.columns]
    cols=weight_cols(df)
    lut={}
    for _,r in df.iterrows():
        raw=str(r[df.columns[0]])
        zz=zip2(raw)
        cname=re.sub(r"\d+","",raw).strip()
        cname=cname.upper()
        country_map={
            "DEUTSCHLAND":"DE","GERMANY":"DE","德国":"DE",
            "ÖSTERREICH":"AT","奥地利":"AT",
            "BELGIEN":"BE","比利时":"BE",
            "NIEDERLANDE":"NL","荷兰":"NL",
            "POLEN":"PL","波兰":"PL",
        }
        if cname not in country_map: continue
        k=key("SELL",country_map[cname],zz)
        lut[k]={}
        for c in cols:
            if pd.notna(r[c]):
                lut[k][c]=float(r[c])
    return lut,cols

SELL_LUT,SELL_COLS=load_sell()

# ============================================
# Hellmann 2026 V5 rules
# ============================================

HELLMANN_RULES={
"DE":(150,0.182,0,15),
"AT":(200,0.133,0.066,30),
"BE":(200,0.097,0.021,30),
"BG":(200,0.062,0.099,30),
"CZ":(200,0.086,0.054,30),
"DK":(200,0.086,0.001,30),
"EE":(200,0.072,0,30),
"ES":(200,0.067,0,30),
"FI":(200,0.048,0.031,75),
"FR":(200,0.077,0.005,30),
"GR":(200,0.078,0.100,75),
"HU":(200,0.115,0.152,30),
"HR":(200,0.091,0.116,30),
"IE":(200,0.061,0.036,75),
"IT":(200,0.103,0.070,30),
"LT":(200,0.076,0,30),
"LU":(200,0.109,0,30),
"LV":(200,0.070,0,30),
"NL":(200,0.089,0,30),
"PL":(200,0.102,0.026,30),
"PT":(200,0.077,0,30),
"RO":(200,0.070,0.106,30),
"SE":(200,0.036,0.007,75),
"SI":(200,0.125,0.153,30),
"SK":(200,0.085,0.059,30),
"XK":(200,0.034,0.043,30),
}

# ============================================
# UI INPUT
# ============================================

col1,col2=st.columns(2)
with col1:
    dest_cc=cc(st.text_input("目的国家 ISO2","DE"))
with col2:
    dest_plz=st.text_input("目的邮编","38112")

zz=zip2(dest_plz)
if not zz:
    st.stop()

st.subheader("货物明细")
cargo_df=st.data_editor(pd.DataFrame([{"qty":1,"weight":20,"l":60,"w":40,"h":40}]),num_rows="dynamic")

rows=[]
for _,r in cargo_df.iterrows():
    rows.append(Cargo(int(r.qty),float(r.weight),float(r.l),float(r.w),float(r.h)))

actual=total_actual(rows)
cbm=total_cbm(rows)

charge_dhl=chargeable(actual,cbm,200)
charge_raben=chargeable(actual,cbm,200)
charge_sch=chargeable(actual,cbm,150 if dest_cc=="DE" else 200)
charge_hel=chargeable(actual,cbm,150 if dest_cc=="DE" else 200)
charge_fx=fedex_charge(rows)

# ============================================
# Cost calculation
# ============================================

def calc_matrix(lut,cols,prefix,charge):
    k=key(prefix,dest_cc,zz)
    if not lut or k not in lut:
        return False,k,0,"未找到线路"
    bc=pick_bracket(cols,charge)
    return True,k,lut[k][bc],bc

results=[]

for name,lut,cols,charge in [
("DHL",DHL_LUT,DHL_COLS,charge_dhl),
("Raben",RABEN_LUT,RABEN_COLS,charge_raben),
("Schenker",SCH_LUT,SCH_COLS,charge_sch),
]:
    ok,k,base,info=calc_matrix(lut,cols,name.upper(),charge)
    results.append([name,ok,k,base,base,info])

# Hellmann
if dest_cc in HELLMANN_RULES:
    vol,maut,state,dg=HELLMANN_RULES[dest_cc]
    ok,k,base,info=calc_matrix(HEL_LUT,HEL_COLS,"HELLMANN",charge_hel)
    if ok:
        total=base+base*maut+base*state
    else:
        total=0
else:
    ok=False;k="";total=0;info="规则缺失"
results.append(["Hellmann",ok,k,base,total,info])

# FedEx
fx_key=f"FEDEX-{dest_cc}"
if fx_key in FEDEX_LUT:
    base=FEDEX_LUT[fx_key]*charge_fx
    results.append(["FedEx",True,fx_key,base,base,"€/kg"])
else:
    results.append(["FedEx",False,fx_key,0,0,"无国家价格"])

# Sell price
sell_key=key("SELL",dest_cc,zz)
sell_price=None
if SELL_LUT and sell_key in SELL_LUT:
    bc=pick_bracket(SELL_COLS,chargeable(actual,cbm,200))
    sell_price=SELL_LUT[sell_key][bc]

df=pd.DataFrame(results,columns=["Carrier","OK","Key","Base","TotalCost","Info"])
if sell_price:
    df["SellPrice"]=sell_price
    df["Profit"]=sell_price-df["TotalCost"]

st.subheader("报价对比 + 毛利")
st.dataframe(df,use_container_width=True)

# Export
def export():
    output=BytesIO()
    with pd.ExcelWriter(output,engine="openpyxl") as w:
        df.to_excel(w,index=False)
    return output.getvalue()

st.download_button("导出Excel",data=export(),file_name=f"Freight_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx")
