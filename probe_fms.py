"""
probe_fms.py – najde ve File Library (FMS) dataset Procured Balancing Capacity
a vypíše sloupce + vzorek, ať vím, jak napsat parser.

Spusť s TP přihlašovacími údaji v prostředí:
  ENTSOE_TP_USER='tvuj@email' ENTSOE_TP_PASS='heslo' python probe_fms.py
(lokálně, nebo Heroku → More → Run console se stejnými env proměnnými)

Nic nezapisuje. Heslo se nikam neukládá, čte se jen z env.
"""
import io
import os
import json
import zipfile

import requests
import pandas as pd

USER = os.environ.get("ENTSOE_TP_USER", "")
PWD  = os.environ.get("ENTSOE_TP_PASS", "")
if not USER or not PWD:
    raise SystemExit("Nastav ENTSOE_TP_USER a ENTSOE_TP_PASS v prostředí.")

KEYCLOAK = "https://keycloak.tp.entsoe.eu/realms/tp/protocol/openid-connect/token"
FMS      = "https://fms.tp.entsoe.eu/"

s = requests.Session()

# 1) token
tok = s.post(KEYCLOAK, data={
    "client_id": "tp-fms-public", "grant_type": "password",
    "username": USER, "password": PWD,
}, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
tok.raise_for_status()
TOKEN = tok.json()["access_token"]
print("token OK")
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def list_path(path):
    if not path.endswith("/"):
        path += "/"
    r = s.post(FMS + "listFolder", data=json.dumps({
        "path": path,
        "sorterList": [{"key": "periodCovered.from", "ascending": True}],
        "pageInfo": {"pageIndex": 0, "pageSize": 5000},
    }), headers=H, timeout=60)
    r.raise_for_status()
    return r.json().get("contentItemList", [])


def download_csv(folder, filename):
    if not folder.endswith("/"):
        folder += "/"
    r = s.post(FMS + "downloadFileContent", data=json.dumps({
        "folder": folder, "filename": filename,
        "downloadAsZip": True, "topLevelFolder": "TP_export",
    }), headers=H, timeout=120)
    r.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    with zf.open(zf.filelist[0].filename) as f:
        return pd.read_csv(f, sep="\t", encoding="utf-8-sig")


# 2) kořenové složky – najdi Procured Balancing Capacity
print("\n=== složky v /TP_export/ (kandidáti) ===")
roots = list_path("/TP_export/")
names = [x.get("name", "") for x in roots]
cand = [n for n in names if any(k in n.lower() for k in
        ("procur", "capacity", "balancingcapacity", "12.3.f", "reserve"))]
for n in cand:
    print("  ", n)
if not cand:
    print("  (nic zjevného – vypisuji všechny složky:)")
    for n in sorted(names):
        print("  ", n)

# vyber nejlepší shodu na Procured Balancing Capacity
target = None
for n in names:
    ln = n.lower()
    if "procur" in ln and "capacit" in ln:
        target = n
        break
if target is None and cand:
    target = cand[0]
print("\nZVOLENA SLOZKA:", target)
if not target:
    raise SystemExit("Procured Balancing Capacity složka nenalezena – pošli mi seznam složek výše.")

# 3) soubory ve složce – vezmi nejnovější (dle názvu)
files = list_path("/TP_export/" + target + "/")
fnames = sorted([x.get("name", "") for x in files])
print(f"\n=== soubory ({len(fnames)}), posledních 6 ===")
for fn in fnames[-6:]:
    print("  ", fn)
if not fnames:
    raise SystemExit("Složka je prázdná.")

newest = fnames[-1]
print("\nSTAHUJI:", newest)
df = download_csv("/TP_export/" + target + "/", newest)

print("\n=== SLOUPCE ===")
print(list(df.columns))
print("\nshape:", df.shape)

# 4) zkus filtr CZ + mFRR (názvy sloupců neznám, tak hrubě)
print("\n=== vzorek 8 řádků (raw) ===")
with pd.option_context("display.max_columns", None, "display.width", 200):
    print(df.head(8).to_string())

# pokus o CZ řádky
for col in df.columns:
    try:
        if df[col].astype(str).str.contains("10YCZ|CEPS|^CZ$", regex=True, na=False).any():
            print(f"\n(sloupec '{col}' obsahuje CZ – vzorek CZ řádků:)")
            with pd.option_context("display.max_columns", None, "display.width", 200):
                print(df[df[col].astype(str).str.contains("10YCZ|CEPS|^CZ$", regex=True, na=False)].head(6).to_string())
            break
    except Exception:
        pass
