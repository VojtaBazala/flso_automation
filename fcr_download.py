"""
fcr_download.py
Stahuje FCR výsledky z regelleistung.net a ukládá do PostgreSQL.
Pokud data nejsou dostupná, pošle email notifikaci a skript skončí s exit code 2
(GitHub Actions to interpretuje jako "retry needed").
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
from config import EMAIL_RECIPIENTS

# ── KONFIGURACE ────────────────────────────────────
DB_URL         = os.environ.get("DATABASE_URL", "")
GMAIL_USER     = "oldrich.bazala@gmail.com"
GMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO       = ", ".join(EMAIL_RECIPIENTS)
FIRST_RUN      = os.environ.get("FIRST_RUN", "true").lower() == "true"

if not DB_URL:
    raise ValueError("DATABASE_URL není nastavena!")
if not GMAIL_PASSWORD:
    raise ValueError("GMAIL_APP_PASSWORD není nastavena!")

engine = create_engine(DB_URL.replace("postgres://", "postgresql://", 1))

# ── POMOCNÉ FUNKCE ─────────────────────────────────
def send_email(subject, body, attachment_bytes=None, attachment_name=None):
    msg = MIMEMultipart()
    msg["From"]    = GMAIL_USER
    msg["To"]      = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    if attachment_bytes and attachment_name:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(attachment_bytes)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f"attachment; filename={attachment_name}")
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


# ── STAŽENÍ FCR ────────────────────────────────────
delivery_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
today         = datetime.now().strftime("%Y-%m-%d")
print(f"Stahuji FCR pro: {delivery_date}")

BASE = "https://www.regelleistung.net/apps/crds/api/v2/tenders/results"
hdrs = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
    "Referer": "https://www.regelleistung.net/apps/datacenter/tenders/",
}

resp = requests.get(
    f"{BASE}/aggregated?productType=FCR&market=CAPACITY&exportFormat=xlsx&deliveryDate={delivery_date}",
    headers=hdrs, timeout=30
)
print(f"FCR status: {resp.status_code} | {len(resp.content)} B")

# ── KONTROLA DAT ────────────────────────────────────
data_ok = False
df_fcr  = None

if resp.status_code == 200 and len(resp.content) > 100:
    try:
        df_fcr = pd.read_excel(io.BytesIO(resp.content), engine="openpyxl")
        trade_date = str(pd.to_datetime(df_fcr["DATE_FROM"].iloc[0]).date())
        if trade_date == delivery_date:
            data_ok = True
            print(f"Data OK pro {trade_date} ({len(df_fcr)} radku)")
        else:
            print(f"Data jsou pro {trade_date}, ne pro {delivery_date} - stara data")
    except Exception as e:
        print(f"Chyba pri parsovani: {e}")

if not data_ok:
    if FIRST_RUN:
        send_email(
            subject=f"FCR [{delivery_date}] – data zatím nejsou k dispozici",
            body=(
                f"Data FCR pro {delivery_date} momentálně nejsou na regelleistung.net k dispozici.\n"
                f"Další pokus o stažení za 5 minut.\n\n"
                f"Čas pokusu: {datetime.now().strftime('%H:%M:%S')}"
            )
        )
    print("Data nejsou dostupna - konec s exit code 2")
    sys.exit(2)

# ── ULOŽENÍ DO DB ──────────────────────────────────
with engine.connect() as conn:
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS fcr_overview (
            id SERIAL PRIMARY KEY, trade_date DATE NOT NULL,
            product_name TEXT, crossborder_price FLOAT,
            cz_demand_mw FLOAT, cz_price FLOAT, cz_deficit_surplus FLOAT,
            UNIQUE(trade_date, product_name)
        )
    """))
    conn.commit()

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
    conn.commit()
print(f"FCR ulozen: {len(df_fcr)} radku")

# ── EMAIL S VÝSLEDKEM ──────────────────────────────
xlsx_bytes = df_to_excel_bytes(df_fcr[["PRODUCTNAME",
    "CROSSBORDER_SETTLEMENTCAPACITY_PRICE_[EUR/MW]",
    "CZECH_REPUBLIC_DEMAND_[MW]",
    "CZECH_REPUBLIC_SETTLEMENTCAPACITY_PRICE_[EUR/MW]",
    "CZECH_REPUBLIC_DEFICIT(-)_SURPLUS(+)_[MW]"]])

send_email(
    subject=f"FCR [{delivery_date}] – data stažena ✅",
    body=(
        f"FCR výsledky pro {delivery_date} byly úspěšně staženy a uloženy.\n"
        f"Počet řádků: {len(df_fcr)}\n"
        f"Čas stažení: {datetime.now().strftime('%H:%M:%S')}\n\n"
        f"Tabulka v příloze."
    ),
    attachment_bytes=xlsx_bytes,
    attachment_name=f"FCR_{delivery_date}.xlsx"
)

print("Hotovo!")
