#!/usr/bin/env python3
"""
Produce fcf_quarterly_merged.csv with:
Ticker, Report Date, Price, 1Y_Price_growth, 2Y_Price_growth,
3Y_Price_growth, 6M_Price_growth,
Market_Cap,
Revenue, Net Income,
FCF, FCF_per_share,
Yo6M_FCFps_growth, 1Y_FCFps_growth, 2Y_FCFps_growth, 3Y_FCFps_growth
"""

import os, time
from pathlib import Path
from datetime import timedelta

import pandas as pd
import numpy as np
import simfin as sf
from dotenv import load_dotenv
from requests.exceptions import HTTPError

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────
load_dotenv()
MARKET, START_YEAR = "US", 2000
DATA_DIR, OUT_CSV  = Path("simfin_data"), Path("fcf_dataset.csv")
MAX_RETRY, RETRY_DELAY = 5, 8
CAPEX_CANDS = [
    "Change in Fixed Assets & Intangibles",
    "Purchase of PPE & Intangibles, net",
    "Capital Expenditures (Fixed Assets)",
    "Capital Expenditures"
]

# ──────────────────────────────────────────────────────────────────────────────
# RETRY HELPER
# ──────────────────────────────────────────────────────────────────────────────
def retry(func, *args, **kwargs):
    for i in range(MAX_RETRY):
        try:
            return func(*args, **kwargs)
        except HTTPError as e:
            if i == MAX_RETRY - 1 or not (500 <= e.response.status_code < 600):
                raise
            wait = RETRY_DELAY * 2 ** i
            print(f"  Got {e.response.status_code}: retrying in {wait}s…")
            time.sleep(wait)

# ──────────────────────────────────────────────────────────────────────────────
# 1) FUNDAMENTALS
# ──────────────────────────────────────────────────────────────────────────────
print("Loading cash-flow & income …")
sf.set_api_key(os.getenv("SIMFIN_API_KEY", "free"))
sf.set_data_dir(str(DATA_DIR))

cf = retry(sf.load_cashflow, variant="quarterly", market=MARKET, refresh_days=365)
inc = retry(sf.load_income,   variant="quarterly", market=MARKET, refresh_days=365)

cf, inc = cf.reset_index(), inc.reset_index()

# choose a CapEx column
capex_col = next((c for c in CAPEX_CANDS if c in cf.columns), None)
if not capex_col:
    raise KeyError(f"CapEx column not found. Tried {CAPEX_CANDS}")

# compute FCF
cf = cf[["Ticker", "Report Date", "Net Cash from Operating Activities", capex_col]]
cf.rename(columns={
    "Net Cash from Operating Activities": "OCF",
    capex_col: "CAPEX"
}, inplace=True)
cf["FCF"] = cf["OCF"] + cf["CAPEX"]   # CAPEX ≤ 0

# include shares, revenue, earnings
inc = inc[[
    "Ticker", "Report Date",
    "Shares (Basic)",
    "Revenue",
    "Net Income"
]]

# merge & filter
fund = (
    cf.merge(inc, on=["Ticker", "Report Date"])
      .loc[lambda d: d["Report Date"].dt.year >= START_YEAR]
)
fund["FCF_per_share"] = fund["FCF"] / fund["Shares (Basic)"]

# growth metrics (2q, 4q, 8q, 12q lags)
fund.sort_values(["Ticker", "Report Date"], inplace=True, ignore_index=True)
grp = fund.groupby("Ticker")

fund["FCFps_lag2"]  = grp["FCF_per_share"].shift(2)   # 6 M
fund["FCFps_lag4"]  = grp["FCF_per_share"].shift(4)   # 1 Y
fund["FCFps_lag8"]  = grp["FCF_per_share"].shift(8)   # 2 Y
fund["FCFps_lag12"] = grp["FCF_per_share"].shift(12)  # 3 Y

fund["Yo6M_FCFps_growth"] = (fund["FCF_per_share"] - fund["FCFps_lag2"])  / fund["FCFps_lag2"]
fund["1Y_FCFps_growth"]  = (fund["FCF_per_share"] - fund["FCFps_lag4"])  / fund["FCFps_lag4"]
fund["2Y_FCFps_growth"] = (fund["FCF_per_share"] - fund["FCFps_lag8"])  / fund["FCFps_lag8"]
fund["3Y_FCFps_growth"] = (fund["FCF_per_share"] - fund["FCFps_lag12"]) / fund["FCFps_lag12"]

# ──────────────────────────────────────────────────────────────────────────────
# 2) PRICES
# ──────────────────────────────────────────────────────────────────────────────
print("Loading daily share prices …")
px = retry(sf.load_shareprices, variant="daily", market=MARKET, refresh_days=365)
px = (
    px.reset_index()[["Ticker", "Date", "Adj. Close"]]
      .rename(columns={"Date": "TradeDate", "Adj. Close": "Price"})
      .assign(TradeDate=lambda d: pd.to_datetime(d["TradeDate"]))
      .sort_values(["Ticker", "TradeDate"])
)

# align prices to report dates (within +7 days)
fund_keyed = fund[["Ticker", "Report Date"]].rename(columns={"Report Date": "RptDate"})
print("Aligning prices within +-7 days")
temp = (
    fund_keyed.merge(px, on="Ticker", how="left")
      .loc[lambda d: (d["TradeDate"] >= d["RptDate"]) &
                     (d["TradeDate"] <= d["RptDate"] + pd.Timedelta(days=7))]
      .sort_values(["Ticker", "RptDate", "TradeDate"])
      .groupby(["Ticker", "RptDate"], as_index=False)
      .first()
)

# merge back onto fundamentals
merged = (
    fund.merge(temp,
               left_on=["Ticker", "Report Date"],
               right_on=["Ticker", "RptDate"],
               how="left")
         .drop(columns="RptDate")
)
merged.dropna(subset=["Price"], inplace=True)

# ──────────────────────────────────────────────────────────────────────────────
# 3) METRICS & SAVE
# ──────────────────────────────────────────────────────────────────────────────
print("Computing market cap & price growths …")
merged["Market_Cap"] = merged["Price"] * merged["Shares (Basic)"]

grp2 = merged.groupby("Ticker")
merged["Price_lag2"]  = grp2["Price"].shift(2)
merged["Price_lag4"]  = grp2["Price"].shift(4)
merged["Price_lag8"]  = grp2["Price"].shift(8)
merged["Price_lag12"] = grp2["Price"].shift(12)

merged["6M_Price_growth"] = (merged["Price"] - merged["Price_lag2"])  / merged["Price_lag2"]
merged["1Y_Price_growth"]  = (merged["Price"] - merged["Price_lag4"])  / merged["Price_lag4"]
merged["2Y_Price_growth"] = (merged["Price"] - merged["Price_lag8"])  / merged["Price_lag8"]
merged["3Y_Price_growth"] = (merged["Price"] - merged["Price_lag12"]) / merged["Price_lag12"]

# select & order columns
final = merged[[
    "Ticker", "Report Date",
    "Price", "6M_Price_growth", "1Y_Price_growth",
    "2Y_Price_growth", "3Y_Price_growth",
    "Market_Cap",
    "Revenue", "Net Income",
    "FCF", "FCF_per_share",
    "Yo6M_FCFps_growth", "1Y_FCFps_growth",
    "2Y_FCFps_growth", "3Y_FCFps_growth"
]]

print(f"Rows: {len(final):,}")
final.to_csv(OUT_CSV, index=False, float_format="%.3f")
print(f"Saved → {OUT_CSV}")
