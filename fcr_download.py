"""
fcr_download.py
Stahuje FCR výsledky z regelleistung.net a ukládá do PostgreSQL.
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

DB_URL         = os.environ.get("DATABASE_URL", "")
GMAIL_USER     = "oldrich.bazala@gmail.com"
GMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
FIRST_RUN      = os.environ.get("FIRST_RUN", "true").lower() == "true"

if not DB_URL:
    raise ValueError("DATABASE_URL není nastavena!")
if not GMAIL_PASSWORD:
    raise ValueError("GMAIL_APP_PASSWORD není nastavena!")

engine = create_engine(
    DB_URL.replace("postgres://", "postgresql://", 1),
    connect_args={"connect_timeout": 10},
    pool_pre_ping=True,
    pool_recycle=300,
)

def log_email_sent(eng, run_date, step):
    try:
        from sqlalchemy import text as _lt
        with eng.connect() as _lc:
            _lc.execute(_lt("""
                CREATE TABLE IF NOT EXISTS pipeline_log (
                    id SERIAL PRIMARY KEY, run_date DATE NOT NULL,
                    step TEXT NOT NULL, email_sent BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(run_date, step)
                )
            """))
            _lc.execute(_lt("""
                INSERT INTO pipeline_log (run_date, step, email_sent)
                VALUES (:d, :s, TRUE)
                ON CONFLICT (run_date, step) DO UPDATE SET email_sent = TRUE
            """), {"d": str(run_date), "s": step})
            _lc.commit()
        print(f"pipeline_log: {step} ✅")
    except Exception as _le:
        print(f"⚠ pipeline_log selhal: {_le}")


def send_email(subject, body, attachment_bytes=None, attachment_name=None):
    msg = MIMEMultipart()
    msg["From"]    = GMAIL_USER
    msg["To"]      = ", ".join(EMAIL_RECIPIENTS)
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
        server.sendmail(GMAIL_USER, EMAIL_RECIPIENTS, msg.as_string())
    print(f"Email odeslán: {subject}")


def df_to_excel_bytes(df):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    return buf.getvalue()


def update_tracking(trade_date, df_fcr):
    """Zapíše actual_price do fcr_tracking."""
    try:
        fresh = create_engine(DB_URL.replace("postgres://", "postgresql://", 1))
        with fresh.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS fcr_tracking (
                    id SERIAL PRIMARY KEY, forecast_date DATE NOT NULL,
                    block TEXT NOT NULL, forecast_bid FLOAT,
                    our_bid FLOAT, actual_price FLOAT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(forecast_date, block)
                )
            """))
            conn.commit()
            for _, r in df_fcr.iterrows():
                conn.execute(text("""
                    INSERT INTO fcr_tracking (forecast_date, block, actual_price)
                    VALUES (:fd, :block, :ap)
                    ON CONFLICT (forecast_date, block) DO UPDATE SET
                        actual_price = EXCLUDED.actual_price
                """), {"fd": trade_date,
                       "block": str(r["PRODUCTNAME"]),
                       "ap": float(r["CZECH_REPUBLIC_SETTLEMENTCAPACITY_PRICE_[EUR/MW]"])})
            conn.commit()
        print(f"FCR tracking aktualizován: {trade_date}")
    except Exception as e:
        print(f"⚠ FCR tracking selhal: {e}")


# Datum
delivery_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
print(f"Stahuji FCR pro: {delivery_date}")

# Check DB
already_in_db = False
try:
    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM fcr_overview WHERE trade_date = :d"), {"d": delivery_date}).scalar()
    if count > 0:
        already_in_db = True
except Exception:
    pass

if already_in_db:
    print(f"FCR pro {delivery_date} již v DB ({count} řádků) – přeskakuji stahování")
    # Načti z DB pro tracking a email
    try:
        fresh = create_engine(DB_URL.replace("postgres://", "postgresql://", 1))
        with fresh.connect() as conn:
            already_tracked = conn.execute(text(
                "SELECT COUNT(*) FROM fcr_tracking WHERE forecast_date=:d AND actual_price IS NOT NULL"
            ), {"d": delivery_date}).scalar()
        if already_tracked and already_tracked >= 6:
            print("FCR tracking již kompletní, přeskakuji")
            sys.exit(0)
        with fresh.connect() as _c:
            rows = _c.execute(text("SELECT product_name, cz_price FROM fcr_overview WHERE trade_date = :d"), {"d": delivery_date}).fetchall()
        for row in rows:
            try:
                fresh2 = create_engine(DB_URL.replace("postgres://", "postgresql://", 1))
                with fresh2.connect() as _c2:
                    _c2.execute(text("INSERT INTO fcr_tracking (forecast_date, block, actual_price) VALUES (:fd, :b, :ap) ON CONFLICT (forecast_date, block) DO UPDATE SET actual_price=EXCLUDED.actual_price"), {"fd": delivery_date, "b": str(row[0]), "ap": float(row[1])})
                    _c2.commit()
            except Exception as _te:
                print(f"⚠ tracking row: {_te}")
        print(f"FCR tracking aktualizován: {delivery_date}")
        df_db = pd.DataFrame([{"PRODUCTNAME": r[0], "cz_price": r[1]} for r in rows])
        xlsx = df_to_excel_bytes(df_db)
        send_email(
            subject=f"FCR [{delivery_date}] – data v DB ✅",
            body=f"FCR výsledky pro {delivery_date} jsou v DB.\nTracking aktualizován.\nČas: {datetime.now().strftime('%H:%M:%S')} UTC",
            attachment_bytes=xlsx,
            attachment_name=f"FCR_{delivery_date}.xlsx"
        )
        log_email_sent(engine, delivery_date, "fcr_email")
    except Exception as e:
        print(f"⚠ Skip branch selhal: {e}")
        import traceback; traceback.print_exc()
    sys.exit(0)

# Stažení
BASE = "https://www.regelleistung.net/apps/crds/api/v2/tenders/results"
hdrs = {"User-Agent": "Mozilla/5.0", "Accept": "*/*", "Referer": "https://www.regelleistung.net/apps/datacenter/tenders/"}
resp = requests.get(f"{BASE}/aggregated?productType=FCR&market=CAPACITY&exportFormat=xlsx&deliveryDate={delivery_date}", headers=hdrs, timeout=30)
print(f"FCR status: {resp.status_code} | {len(resp.content)} B")

data_ok = False
df_fcr  = None

if resp.status_code == 200 and len(resp.content) > 100:
    try:
        df_fcr = pd.read_excel(io.BytesIO(resp.content), engine="openpyxl")
        trade_date = str(pd.to_datetime(df_fcr["DATE_FROM"].iloc[0]).date())
        if trade_date == delivery_date:
            data_ok = True
            print(f"Data OK pro {trade_date} ({len(df_fcr)} řádků)")
        else:
            print(f"Data jsou pro {trade_date}, ne pro {delivery_date}")
    except Exception as e:
        print(f"Chyba parsování: {e}")

if not data_ok:
    if FIRST_RUN:
        send_email(
            subject=f"FCR [{delivery_date}] – data zatím nejsou k dispozici",
            body=f"Data FCR pro {delivery_date} nejsou na regelleistung.net.\nDalší pokus za 10 minut.\nČas: {datetime.now().strftime('%H:%M:%S')}"
        )
    print("Data nejsou dostupná – čekám na další pokus")
    sys.exit(0)

# Uložení do DB
import time
for _retry in range(3):
    try:
        engine2 = create_engine(
            DB_URL.replace("postgres://", "postgresql://", 1),
            connect_args={"connect_timeout": 10},
            pool_pre_ping=True,
        )
        engine2.connect().close()
        engine = engine2
        break
    except Exception as _re:
        print(f"DB connection pokus {_retry+1}/3 selhal: {_re}")
        if _retry < 2:
            time.sleep(10)
        else:
            print("DB nedostupná po 3 pokusech, konec")
            sys.exit(0)

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
print(f"FCR uložen: {len(df_fcr)} řádků")

# Tracking
update_tracking(trade_date, df_fcr)

# Email
cols = ["PRODUCTNAME", "CROSSBORDER_SETTLEMENTCAPACITY_PRICE_[EUR/MW]",
        "CZECH_REPUBLIC_DEMAND_[MW]", "CZECH_REPUBLIC_SETTLEMENTCAPACITY_PRICE_[EUR/MW]",
        "CZECH_REPUBLIC_DEFICIT(-)_SURPLUS(+)_[MW]"]
send_email(
    subject=f"FCR [{delivery_date}] – data stažena ✅",
    body=f"FCR výsledky pro {delivery_date} staženy a uloženy.\nPočet řádků: {len(df_fcr)}\nČas: {datetime.now().strftime('%H:%M:%S')} UTC",
    attachment_bytes=df_to_excel_bytes(df_fcr[cols]),
    attachment_name=f"FCR_{delivery_date}.xlsx"
)
log_email_sent(engine, delivery_date, "fcr_email")

print("Hotovo!")
