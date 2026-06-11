"""
mfrr_plus_download.py
Stahuje mFRR+ orderbook z ENTSO-E a ukládá do PostgreSQL.
Pokud data nejsou dostupná, pošle email notifikaci a skončí s exit code 2.
"""

import requests
import zipfile
import io
import xml.etree.ElementTree as ET
import pandas as pd
import os
import sys
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine, text
from config import EMAIL_RECIPIENTS

# ── KONFIGURACE ────────────────────────────────────
DB_URL          = os.environ.get("DATABASE_URL", "")
API_TOKEN       = os.environ.get("ENTSOE_API_TOKEN", "")
GMAIL_USER      = "oldrich.bazala@gmail.com"
GMAIL_PASSWORD  = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO        = ", ".join(EMAIL_RECIPIENTS)
FIRST_RUN       = os.environ.get("FIRST_RUN", "true").lower() == "true"

BASE_URL        = "https://web-api.tp.entsoe.eu/api"
CZ_BIDDING_ZONE = "10YCZ-CEPS-----N"

if not DB_URL:
    raise ValueError("DATABASE_URL není nastavena!")
if not API_TOKEN:
    raise ValueError("ENTSOE_API_TOKEN není nastaven!")
if not GMAIL_PASSWORD:
    raise ValueError("GMAIL_APP_PASSWORD není nastavena!")

engine = create_engine(DB_URL.replace("postgres://", "postgresql://", 1))

# ── DATUM: zítřek (delivery day) ──────────────────
delivery_date = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
print(f"Stahuji mFRR+ pro: {delivery_date}")

# interval: 23:00 UTC dnes → 23:00 UTC zítra
today_utc    = datetime.now(timezone.utc).replace(hour=23, minute=0, second=0, microsecond=0)
tomorrow_utc = today_utc + timedelta(days=1)

def entsoe_time(dt_utc: datetime) -> str:
    return dt_utc.strftime("%Y%m%d%H%M")

# ── POMOCNÉ FUNKCE ─────────────────────────────────
def send_email(subject, body):
    msg = MIMEMultipart()
    msg["From"]    = GMAIL_USER
    msg["To"]      = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_PASSWORD)
        server.sendmail(GMAIL_USER, EMAIL_TO, msg.as_string())
    print(f"Email odeslán: {subject}")

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


def marginal_price_for_volume(orderbook: pd.DataFrame, volume: float):
    if orderbook.empty:
        return None
    mask = orderbook["cum_quantity_MW"] >= volume
    if mask.any():
        return float(orderbook[mask].iloc[0]["price_EUR_MW"])
    return None


# ── STAŽENÍ mFRR+ ──────────────────────────────────
params = {
    "securityToken":              API_TOKEN,
    "documentType":               "A15",
    "processType":                "A47",
    "area_Domain":                CZ_BIDDING_ZONE,
    "periodStart":                entsoe_time(today_utc),
    "periodEnd":                  entsoe_time(tomorrow_utc),
    "offset":                     0,
    "Type_MarketAgreement.Type":  "A01",
}

resp = requests.get(BASE_URL, params=params, timeout=120)
print(f"ENTSO-E status: {resp.status_code} | {len(resp.content)} B")

# ── KONTROLA DAT ────────────────────────────────────
data_ok = False
df_result = None

if resp.status_code == 200 and len(resp.content) > 100:
    try:
        content_bytes = resp.content
        if content_bytes[:2] == b"PK":
            z = zipfile.ZipFile(io.BytesIO(content_bytes))
            xml_bytes = z.read(z.namelist()[0])
        else:
            xml_bytes = content_bytes

        root = ET.fromstring(xml_bytes)

        if "Acknowledgement_MarketDocument" in root.tag:
            reason_el = root.find(".//{*}Reason")
            code = reason_el.find("{*}code").text if reason_el is not None else "?"
            text_ = reason_el.find("{*}text").text if reason_el is not None else "?"
            print(f"ACK od ENTSO-E: code={code}, text={text_}")
        else:
            ns = {"n": "urn:iec62325.351:tc57wg16:451-6:balancingdocument:4:1"}
            rows = []
            for ts in root.findall("n:TimeSeries", ns):
                ts_id        = ts.findtext("n:mRID", namespaces=ns)
                direction    = ts.findtext("n:flowDirection.direction", namespaces=ns)
                product_type = ts.findtext("n:standard_MarketProduct.marketProductType", namespaces=ns)
                psr_type     = ts.findtext("n:mktPSRType.psrType", namespaces=ns)
                curve_type   = ts.findtext("n:curveType", namespaces=ns)

                for period in ts.findall("n:Period", ns):
                    ti = period.find("n:timeInterval", ns)
                    if ti is None:
                        continue
                    interval_start_el = ti.find("n:start", ns)
                    if interval_start_el is None:
                        continue
                    interval_start = interval_start_el.text
                    resolution     = period.findtext("n:resolution", namespaces=ns)
                    if not interval_start or not resolution:
                        continue
                    start_dt = datetime.fromisoformat(interval_start.replace("Z", "+00:00"))
                    step = timedelta(hours=1) if resolution == "PT60M" else timedelta(minutes=15)

                    for point in period.findall("n:Point", ns):
                        pos_txt   = point.findtext("n:position", namespaces=ns)
                        qty_txt   = point.findtext("n:quantity", namespaces=ns)
                        price_txt = point.findtext("n:procurement_Price.amount", namespaces=ns)
                        if pos_txt is None or qty_txt is None or price_txt is None:
                            continue
                        rows.append({
                            "timeseries_id": ts_id,
                            "product_type":  product_type,
                            "psr_type":      psr_type,
                            "curve_type":    curve_type,
                            "direction":     direction,
                            "interval_start": interval_start,
                            "resolution":    resolution,
                            "position":      int(pos_txt),
                            "datetime_utc":  start_dt + step * (int(pos_txt) - 1),
                            "quantity_MW":   float(qty_txt),
                            "price_EUR_MW":  float(price_txt),
                        })

            df = pd.DataFrame(rows)
            if not df.empty:
                df2 = df[
                    (df["direction"] == "A01") &
                    (df["quantity_MW"] > 0) &
                    (df["price_EUR_MW"] > 0)
                ].copy()

                if not df2.empty:
                    df2["datetime_utc"] = pd.to_datetime(df2["datetime_utc"], utc=True).dt.tz_localize(None)
                    df2 = df2.sort_values(["datetime_utc", "price_EUR_MW", "quantity_MW"], ascending=[True, True, False])
                    df2["cum_quantity_MW"] = pd.NA
                    mask_pos1 = df2["position"] == 1
                    df2.loc[mask_pos1, "cum_quantity_MW"] = (
                        df2[mask_pos1].groupby("datetime_utc")["quantity_MW"].cumsum()
                    )
                    df2["trade_date"] = delivery_date
                    df_result = df2
                    data_ok = True
                    print(f"Data OK: {len(df_result)} řádků")

    except Exception as e:
        print(f"Chyba při parsování: {e}")

if not data_ok:
    if FIRST_RUN:
        send_email(
            subject=f"mFRR+ [{delivery_date}] – data zatím nejsou k dispozici",
            body=(
                f"Data mFRR+ pro {delivery_date} momentálně nejsou na ENTSO-E k dispozici.\n"
                f"Další pokus za 5 minut.\n\n"
                f"Čas pokusu: {datetime.now().strftime('%H:%M:%S')}"
            )
        )
    print("Data nejsou dostupna - cekam na dalsi pokus")
    sys.exit(0)

# ── ULOŽENÍ DO DB ──────────────────────────────────
import time
for _retry in range(3):
    try:
        _eng = create_engine(
            DB_URL.replace("postgres://", "postgresql://", 1),
            connect_args={"connect_timeout": 10},
            pool_pre_ping=True,
        )
        _eng.connect().close()
        engine = _eng
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
        CREATE TABLE IF NOT EXISTS mfrr_orderbook (
            id              SERIAL PRIMARY KEY,
            trade_date      DATE NOT NULL,
            timeseries_id   TEXT,
            product_type    TEXT,
            interval_start  TEXT,
            position        INTEGER,
            quantity_mw     FLOAT,
            price_eur_mw    FLOAT,
            cum_quantity_mw FLOAT,
            UNIQUE(trade_date, timeseries_id, interval_start, position)
        )
    """))
    conn.execute(text("ALTER TABLE mfrr_orderbook ADD COLUMN IF NOT EXISTS product_type TEXT"))
    conn.commit()

    conn.execute(text("DELETE FROM mfrr_orderbook WHERE trade_date = :d"), {"d": delivery_date})
    for _, r in df_result.iterrows():
        conn.execute(text("""
            INSERT INTO mfrr_orderbook
                (trade_date, timeseries_id, product_type, interval_start,
                 position, quantity_mw, price_eur_mw, cum_quantity_mw)
            VALUES
                (:trade_date, :timeseries_id, :product_type, :interval_start,
                 :position, :quantity_mw, :price_eur_mw, :cum_quantity_mw)
            ON CONFLICT DO NOTHING
        """), {
            "trade_date":      r["trade_date"],
            "timeseries_id":   str(r.get("timeseries_id", "")),
            "product_type":    str(r.get("product_type", "")),
            "interval_start":  str(r.get("interval_start", "")),
            "position":        int(r.get("position", 0)),
            "quantity_mw":     float(r.get("quantity_MW", 0)),
            "price_eur_mw":    float(r.get("price_EUR_MW", 0)),
            "cum_quantity_mw": float(r.get("cum_quantity_MW", 0)) if pd.notna(r.get("cum_quantity_MW")) else None,
        })
    conn.commit()
print(f"mFRR+ uložen: {len(df_result)} řádků pro {delivery_date}")

# ── EMAIL ──────────────────────────────────────────
send_email(
    subject=f"mFRR+ [{delivery_date}] – data stažena ✅",
    body=(
        f"mFRR+ orderbook pro {delivery_date} byl úspěšně stažen a uložen.\n"
        f"Počet řádků: {len(df_result)}\n"
        f"Čas stažení: {datetime.now().strftime('%H:%M:%S')}"
    )
)
log_email_sent(engine, delivery_date, "mfrr_plus_email")

print("Hotovo!")
