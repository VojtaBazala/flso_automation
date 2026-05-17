"""
regelleistung_download.py
Stahuje FCR a aFRR výsledky z regelleistung.net a ukládá do PostgreSQL.
Spouštěno automaticky přes GitHub Actions každý den v 9:30 SEČ.
"""

import requests
import pandas as pd
import io
import os
from datetime import datetime, timedelta
from sqlalchemy import create_engine, text

# DB URL z GitHub Secrets
DB_URL = os.environ.get("DATABASE_URL", "")
if not DB_URL:
    raise ValueError("DATABASE_URL není nastavena!")

engine = create_engine(DB_URL.replace("postgres://", "postgresql://", 1))

# Datum dodávky = zítřek
delivery_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
print(f"Stahuji data pro: {delivery_date}")

BASE = "https://www.regelleistung.net/apps/crds/api/v2/tenders/results"
hdrs = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
    "Referer": "https://www.regelleistung.net/apps/datacenter/tenders/",
}

resp_fcr  = requests.get(f"{BASE}/aggregated?productType=FCR&market=CAPACITY&exportFormat=xlsx&deliveryDate={delivery_date}", headers=hdrs, timeout=30)
resp_afrr = requests.get(f"{BASE}/aggregated?productType=aFRR&market=CAPACITY&exportFormat=xlsx&deliveryDate={delivery_date}", headers=hdrs, timeout=30)
resp_list = requests.get(f"{BASE}/anonymous?productType=aFRR&market=CAPACITY&exportFormat=xlsx&deliveryDate={delivery_date}", headers=hdrs, timeout=30)

print(f"FCR:       {resp_fcr.status_code} | {len(resp_fcr.content)} B")
print(f"aFRR:      {resp_afrr.status_code} | {len(resp_afrr.content)} B")
print(f"aFRR list: {resp_list.status_code} | {len(resp_list.content)} B")

if resp_fcr.status_code != 200 or len(resp_fcr.content) == 0:
    raise ValueError(f"FCR stazeni selhalo: status={resp_fcr.status_code}")
if resp_afrr.status_code != 200 or len(resp_afrr.content) == 0:
    raise ValueError(f"aFRR stazeni selhalo: status={resp_afrr.status_code}")
if resp_list.status_code != 200 or len(resp_list.content) == 0:
    raise ValueError(f"aFRR list stazeni selhalo: status={resp_list.status_code}")

df_fcr  = pd.read_excel(io.BytesIO(resp_fcr.content),  engine="openpyxl")
df_afrr = pd.read_excel(io.BytesIO(resp_afrr.content), engine="openpyxl")
df_list = pd.read_excel(io.BytesIO(resp_list.content), engine="openpyxl")

trade_date = str(pd.to_datetime(df_fcr["DATE_FROM"].iloc[0]).date())
print(f"Trade date: {trade_date}")

with engine.connect() as conn:
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS fcr_overview (
            id SERIAL PRIMARY KEY,
            trade_date DATE NOT NULL,
            product_name TEXT,
            crossborder_price FLOAT,
            cz_demand_mw FLOAT,
            cz_price FLOAT,
            cz_deficit_surplus FLOAT,
            UNIQUE(trade_date, product_name)
        )
    """))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS afrr_overview (
            id SERIAL PRIMARY KEY,
            trade_date DATE NOT NULL,
            product TEXT,
            total_marginal_price FLOAT,
            total_avg_price FLOAT,
            cz_min_price FLOAT,
            cz_avg_price FLOAT,
            cz_marginal_price FLOAT,
            cz_import_export FLOAT,
            cz_allocated_mw FLOAT,
            UNIQUE(trade_date, product)
        )
    """))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS afrr_orderbook (
            id SERIAL PRIMARY KEY,
            trade_date DATE NOT NULL,
            product TEXT,
            country TEXT,
            capacity_price FLOAT,
            offered_mw FLOAT,
            allocated_mw FLOAT,
            UNIQUE(trade_date, product, country, capacity_price, offered_mw)
        )
    """))
    conn.commit()

    # FCR
    conn.execute(text("DELETE FROM fcr_overview WHERE trade_date = :d"), {"d": trade_date})
    for _, r in df_fcr.iterrows():
        conn.execute(text(
            "INSERT INTO fcr_overview (trade_date, product_name, crossborder_price, cz_demand_mw, cz_price, cz_deficit_surplus) "
            "VALUES (:trade_date, :product_name, :crossborder_price, :cz_demand_mw, :cz_price, :cz_deficit_surplus) "
            "ON CONFLICT DO NOTHING"
        ), {
            "trade_date":         trade_date,
            "product_name":       str(r["PRODUCTNAME"]),
            "crossborder_price":  float(r["CROSSBORDER_SETTLEMENTCAPACITY_PRICE_[EUR/MW]"]),
            "cz_demand_mw":       float(r["CZECH_REPUBLIC_DEMAND_[MW]"]),
            "cz_price":           float(r["CZECH_REPUBLIC_SETTLEMENTCAPACITY_PRICE_[EUR/MW]"]),
            "cz_deficit_surplus": float(r["CZECH_REPUBLIC_DEFICIT(-)_SURPLUS(+)_[MW]"]),
        })
    print(f"FCR ulozen: {len(df_fcr)} radku")

    # aFRR overview
    conn.execute(text("DELETE FROM afrr_overview WHERE trade_date = :d"), {"d": trade_date})
    for _, r in df_afrr.iterrows():
        conn.execute(text(
            "INSERT INTO afrr_overview (trade_date, product, total_marginal_price, total_avg_price, "
            "cz_min_price, cz_avg_price, cz_marginal_price, cz_import_export, cz_allocated_mw) "
            "VALUES (:trade_date, :product, :total_marginal_price, :total_avg_price, "
            ":cz_min_price, :cz_avg_price, :cz_marginal_price, :cz_import_export, :cz_allocated_mw) "
            "ON CONFLICT DO NOTHING"
        ), {
            "trade_date":           trade_date,
            "product":              str(r["PRODUCT"]),
            "total_marginal_price": float(r["TOTAL_MARGINAL_CAPACITY_PRICE_[(EUR/MW)/h]"]),
            "total_avg_price":      float(r["TOTAL_AVERAGE_CAPACITY_PRICE_[(EUR/MW)/h]"]),
            "cz_min_price":         float(r["CZECH_REPUBLIC_MIN_CAPACITY_PRICE_[(EUR/MW)/h]"]),
            "cz_avg_price":         float(r["CZECH_REPUBLIC_AVERAGE_CAPACITY_PRICE_[(EUR/MW)/h]"]),
            "cz_marginal_price":    float(r["CZECH_REPUBLIC_MARGINAL_CAPACITY_PRICE_[(EUR/MW)/h]"]),
            "cz_import_export":     float(r["CZECH_REPUBLIC_IMPORT(-)_EXPORT(+)_[MW]"]),
            "cz_allocated_mw":      float(r["CZECH_REPUBLIC_ALLOCATED_VOLUME_[MW]"]),
        })
    print(f"aFRR overview ulozen: {len(df_afrr)} radku")

    # aFRR orderbook – jen CZ
    df_cz = df_list[df_list["COUNTRY"] == "CZ"].copy()
    conn.execute(text("DELETE FROM afrr_orderbook WHERE trade_date = :d"), {"d": trade_date})
    for _, r in df_cz.iterrows():
        conn.execute(text(
            "INSERT INTO afrr_orderbook (trade_date, product, country, capacity_price, offered_mw, allocated_mw) "
            "VALUES (:trade_date, :product, :country, :capacity_price, :offered_mw, :allocated_mw) "
            "ON CONFLICT DO NOTHING"
        ), {
            "trade_date":     trade_date,
            "product":        str(r["PRODUCT"]),
            "country":        str(r["COUNTRY"]),
            "capacity_price": float(r["CAPACITY_PRICE_[(EUR/MW)/h]"]),
            "offered_mw":     float(r["OFFERED_CAPACITY_[MW]"]),
            "allocated_mw":   float(r["ALLOCATED_CAPACITY_[MW]"]),
        })
    print(f"aFRR orderbook ulozen: {len(df_cz)} CZ radku")
    conn.commit()

print(f"Vse ulozeno pro {trade_date}!")
