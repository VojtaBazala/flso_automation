"""
probe_fms.py – stáhne aktuální ProcuredBalancingCapacity a vypíše CZ mFRR řádky.
Env: ENTSOE_TP_USER, ENTSOE_TP_PASS. Nic nezapisuje.
"""
import io, os, json, zipfile, datetime
import requests, pandas as pd

USER = os.environ.get("ENTSOE_TP_USER", ""); PWD = os.environ.get("ENTSOE_TP_PASS", "")
if not USER or not PWD: raise SystemExit("Nastav ENTSOE_TP_USER a ENTSOE_TP_PASS.")

KEYCLOAK = "https://keycloak.tp.entsoe.eu/realms/tp/protocol/openid-connect/token"
FMS = "https://fms.tp.entsoe.eu/"
FOLDER = "/TP_export/ProcuredBalancingCapacity_12.3.F_r3/"

s = requests.Session()
tok = s.post(KEYCLOAK, data={"client_id":"tp-fms-public","grant_type":"password",
    "username":USER,"password":PWD}, timeout=30); tok.raise_for_status()
H = {"Authorization": f"Bearer {tok.json()['access_token']}", "Content-Type":"application/json"}

def download_csv(folder, filename):
    r = s.post(FMS+"downloadFileContent", data=json.dumps({"folder":folder,"filename":filename,
        "downloadAsZip":True,"topLevelFolder":"TP_export"}), headers=H, timeout=120)
    r.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    with zf.open(zf.filelist[0].filename) as f:
        return pd.read_csv(f, sep="\t", encoding="utf-8-sig")

# aktuální + příští měsíc (delivery pro zítřek může spadnout do dalšího měsíce)
today = datetime.date.today()
months = {(today.year, today.month), ((today+datetime.timedelta(days=2)).year,(today+datetime.timedelta(days=2)).month)}
frames = []
for y,m in sorted(months):
    fn = f"{y}_{m:02d}_ProcuredBalancingCapacity_12.3.F_r3.csv"
    try:
        d = download_csv(FOLDER, fn); d["_file"]=fn; frames.append(d)
        print("stazeno:", fn, d.shape)
    except Exception as e:
        print("nelze:", fn, type(e).__name__, str(e)[:80])
df = pd.concat(frames, ignore_index=True)

# CZ + mFRR
cz = df[df["AreaCode"].astype(str).str.contains("10YCZ-CEPS", na=False)].copy()
print("\nCZ řádků celkem:", len(cz))
print("ReserveType hodnoty:", cz["ReserveType"].dropna().unique().tolist())
print("Direction hodnoty:", cz["Direction"].dropna().unique().tolist())

mf = cz[cz["ReserveType"].astype(str).str.contains("mFRR|Manual", case=False, na=False)].copy()
print("\nCZ mFRR řádků:", len(mf))
if len(mf):
    mf["DeliveryPeriodStart(UTC)"] = pd.to_datetime(mf["DeliveryPeriodStart(UTC)"])
    last_day = mf["DeliveryPeriodStart(UTC)"].dt.date.max()
    print("nejnovější delivery den:", last_day)
    sub = mf[mf["DeliveryPeriodStart(UTC)"].dt.date == last_day]
    cols = ["DeliveryPeriodStart(UTC)","DeliveryPeriodEnd(UTC)","Direction","Volume[MW]","Price","Currency","TypeOfProduct","ReserveSource"]
    for dire, g in sub.groupby("Direction"):
        print(f"\n--- {last_day} | Direction={dire} | {len(g)} řádků (ladder, sort dle ceny) ---")
        with pd.option_context("display.max_columns", None, "display.width", 220):
            print(g.sort_values(["DeliveryPeriodStart(UTC)","Price"])[cols].head(30).to_string(index=False))
