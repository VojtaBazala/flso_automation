"""
afrr_download.py
Stahuje aFRR výsledky z regelleistung.net a ukládá do PostgreSQL.
"""

import requests
import pandas as pd
import io
import os
import sys
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime, timedelta
from sqlalchemy import create_engine, text

# ── KONFIGURACE ────────────────────────────────────
DB_URL         = os.environ.get("DATABASE_URL", "")
GMAIL_USER     = "oldrich.bazala@gmail.com"
GMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO       = "oldrich@bhfund.eu"
FIRST_RUN      = os.environ.get("FIRST_RUN", "true").lower() == "true"

if not DB_URL:
    raise ValueError("DATABASE_URL není nastavena!")
if not GMAIL_PASSWORD:
    raise ValueError("GMAIL_APP_PASSWORD není nastavena!")

engine = create_engine(DB_URL.replace("postgres://", "postgresql://", 1))

def send_email(subject, body, attachments=None):
    msg = MIMEMultipart()
    msg["From"]    = GMAIL_USER
    msg["To"]      = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    if attachments:
        for name, data in attachments:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(data)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename={name}")
            msg.attach(part)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_PASSWORD)
        server.sendmail(GMAIL_USER, EMAIL_TO, msg.as_string())
    print(f"Email odeslan: {subject}")


def df_to_excel_bytes(df):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    return buf.getvalue()


# ── STAŽENÍ aFRR ────────────────────────────────────
delivery_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
print(f"Stahuji aFRR pro: {delivery_date}")

BASE = "https://www.regelleistung.net/apps/crds/api/v2/tenders/results"
hdrs = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
    "Referer": "https://www.regelleistung.net/apps/datacenter/tenders/",
}

resp_afrr = requests.get(f"{BASE}/aggregated?productType=aFRR&market=CAPACITY&exportFormat=xlsx&deliveryDate={delivery_date}", headers=hdrs, timeout=30)
resp_list = requests.get(f"{BASE}/anonymous?productType=aFRR&market=CAPACITY&exportFormat=xlsx&deliveryDate={delivery_date}", headers=hdrs, timeout=30)

print(f"aFRR overview: {resp_afrr.status_code} | {len(resp_afrr.content)} B")
print(f"aFRR list:     {resp_list.status_code} | {len(resp_list.content)} B")

# ── KONTROLA DAT ────────────────────────────────────
data_ok  = False
df_afrr  = None
df_list  = None

if resp_afrr.status_code == 200 and len(resp_afrr.content) > 100 and \
   resp_list.status_code == 200 and len(resp_list.content) > 100:
    try:
        df_afrr = pd.read_excel(io.BytesIO(resp_afrr.content), engine="openpyxl")
        df_list = pd.read_excel(io.BytesIO(resp_list.content), engine="openpyxl")
        trade_date = str(pd.to_datetime(df_afrr["DATE_FROM"].iloc[0]).date())
        if trade_date == delivery_date:
            data_ok = True
            print(f"Data OK pro {trade_date}")
        else:
            print(f"Data jsou pro {trade_date}, ne pro {delivery_date}")
    except Exception as e:
        print(f"Chyba pri parsovani: {e}")

if not data_ok:
    if FIRST_RUN:
        send_email(
            subject=f"aFRR [{delivery_date}] – data zatím nejsou k dispozici",
            body=(
                f"Data aFRR pro {delivery_date} momentálně nejsou na regelleistung.net k dispozici.\n"
                f"Další pokus o stažení za 5 minut.\n\n"
                f"Čas pokusu: {datetime.now().strftime('%H:%M:%S')}"
            )
        )
    print("Data nejsou dostupna - konec s exit code 2")
    sys.exit(2)

# ── ULOŽENÍ DO DB ──────────────────────────────────
with engine.connect() as conn:
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS afrr_overview (
            id SERIAL PRIMARY KEY, trade_date DATE NOT NULL, product TEXT,
            total_marginal_price FLOAT, total_avg_price FLOAT,
            cz_min_price FLOAT, cz_avg_price FLOAT, cz_marginal_price FLOAT,
            cz_import_export FLOAT, cz_allocated_mw FLOAT,
            UNIQUE(trade_date, product)
        )
    """))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS afrr_orderbook (
            id SERIAL PRIMARY KEY, trade_date DATE NOT NULL,
            product TEXT, country TEXT, capacity_price FLOAT,
            offered_mw FLOAT, allocated_mw FLOAT,
            UNIQUE(trade_date, product, country, capacity_price, offered_mw)
        )
    """))
    conn.commit()

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
    conn.commit()
print(f"aFRR ulozen: overview={len(df_afrr)}, orderbook CZ={len(df_cz)}")

# ── EMAIL S VÝSLEDKEM ──────────────────────────────
send_email(
    subject=f"aFRR [{delivery_date}] – data stažena ✅",
    body=(
        f"aFRR výsledky pro {delivery_date} byly úspěšně staženy a uloženy.\n"
        f"Overview: {len(df_afrr)} řádků\n"
        f"Orderbook CZ: {len(df_cz)} řádků\n"
        f"Čas stažení: {datetime.now().strftime('%H:%M:%S')}\n\n"
        f"Tabulky v příloze."
    ),
    attachments=[
        (f"aFRR_overview_{delivery_date}.xlsx", df_to_excel_bytes(df_afrr)),
        (f"aFRR_orderbook_CZ_{delivery_date}.xlsx", df_to_excel_bytes(df_cz)),
    ]
)

print("Hotovo!")
