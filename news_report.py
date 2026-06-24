# -*- coding: utf-8 -*-
"""
news_report.py — ranní přehled energetických zpráv FR + DE.

Zdroj: Google News RSS (zdarma, bez API klíče). Pouze stdlib (urllib + xml).

Dva způsoby použití:
  1) SAMOSTATNĚ – pošle vlastní e-mail:
        python news_report.py
     (potřebuje SMTP nastavení v proměnných prostředí, viz níže)

  2) Z PIPELINE – jen vrátí HTML blok, který přilepíš do svého ranního mailu:
        from news_report import build_news_html
        html_blok = build_news_html()
        # ... vlož html_blok do těla svého e-mailu v bess_morning.py

Konfigurace SMTP (env proměnné – stejné, jaké už nejspíš máš v pipeline):
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_FROM, MAIL_TO
  (MAIL_TO může být víc adres oddělených čárkou)
"""

import os
import ssl
import json
import html
import smtplib
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime, formataddr
from datetime import datetime, timezone, timedelta

# ── NASTAVENÍ ────────────────────────────────────────────────────────────────

# Jak staré zprávy brát (hodiny). Ranní report → 24 h obvykle stačí.
LOOKBACK_HOURS = 28

# Max počet zpráv na sekci (po dedupu, seřazeno od nejnovější).
MAX_PER_SECTION = 8

# Překlad titulků do češtiny (bezklíčový Google Translate endpoint).
# Když překlad selže, použije se originální titulek (žádný pád).
TRANSLATE = True
TARGET_LANG = "cs"
SHOW_ORIGINAL = True   # pod český titulek přidat šedě i originál

# Jazyk/region výsledků Google News (hl, gl, ceid).
# Mezinárodní (en-GB) dává nejlepší pokrytí FR/DE událostí (Reuters, Montel…).
# České zprávy se ale musí tahat z české verze GN, jinak je mineš → CZ_LOCALE.
DEFAULT_LOCALE = ("en-GB", "GB", "GB:en")
CZ_LOCALE      = ("cs",    "CZ", "CZ:cs")

# Sekce: (nadpis, [dotazy], locale).  locale = None → DEFAULT_LOCALE.
# Pozn.: "when:2d" omezí Google News na poslední ~2 dny; tvrdý časový filtr
#        v kódu to ještě dojistí dle LOOKBACK_HOURS.
# Geo-brána pro FR/DE: titulek musí zmiňovat zemi nebo konkrétní elektrárnu,
# jinak je to zpráva o jiné zemi (Austrálie/UK/Belgie…), co dotaz vytáhl omylem.
GEO_FR = [
    "france", "french", "edf", "rte ", "golfech", "blayais", "bugey",
    "tricastin", "saint-alban", "cattenom", "chooz", "gravelines", "penly",
    "flamanville", "paluel", "civaux", "chinon", "dampierre", "cruas",
    "nogent", "belleville", "fessenheim",
]
GEO_DE = [
    "germany", "german", "deutschland", "eex", "regelleistung", "amprion",
    "tennet", "50hertz", "transnetbw", "bundesnetzagentur", "leipzig",
]

SECTIONS = [
    ("🇫🇷 Francie — jádro & trh", [
        "EDF nuclear France output when:2d",
        "France nuclear river temperature heatwave when:2d",
        "France electricity price power market when:2d",
        "RTE France power grid when:2d",
    ], None, GEO_FR),
    ("🇩🇪 Německo — trh & OZE", [
        "Germany electricity price power market when:2d",
        "Germany power EEX EPEX when:2d",
        "Germany wind solar renewables power grid when:2d",
        "regelleistung balancing power Germany when:2d",
    ], None, GEO_DE),
    ("🇨🇿 Česko — trh & jádro", [
        "ČEPS elektřina přenosová soustava when:2d",
        "OTE cena elektřiny trh when:2d",
        "ČEZ jaderná elektrárna Temelín Dukovany when:2d",
        "Česko energetika elektřina ceny when:2d",
        "podpůrné služby aFRR mFRR baterie when:2d",
    ], CZ_LOCALE, None),
    ("🌍 Evropský trh / ceny / rezervy", [
        "European power prices EPEX spot when:2d",
        "aFRR mFRR balancing capacity price Europe when:2d",
    ], None, None),
]

# Lehký whitelist klíčových slov – titulek musí obsahovat aspoň jedno
# (energetická relevance). Obsahuje EN i CZ kmeny.
# Pozn.: "heatwave/drought/river" schválně NEJSOU – pouštěly katastrofické
#        titulky ("40 drown in France as heatwave peaks").
KEYWORDS = [
    # EN — široká energetická relevance (bez heatwave/drought/river,
    #      které pouštěly katastrofické titulky)
    "nuclear", "edf", "reactor", "power", "electricity", "grid", "price",
    "prices", "market", "epex", "eex", "spot", "wind", "solar", "renewab",
    "gas", "afrr", "mfrr", "fcr", "balancing", "reserve", "regelleistung",
    "rte", "entso", "outage", "curtail", "capacity", "megawatt", "mwh",
    "gwh", "energy", "utility", "tariff",
    # CZ
    "elektř", "energetik", "jadern", "čez", "čeps", "ote", "baterie",
    "plyn", "obnoviteln", "fotovolt", "přenosov", "rozvodn", "rezerv",
    "regulačn", "podpůrn", "teplárn", "soustav", "temelín", "dukovan",
    "větrn", "solárn", "megawatt",
]

# Blocklist zdrojů – pseudo-výzkumný / SEO spam, ne zprávy.
BLOCK_SOURCES = [
    "indexbox", "pulse 2.0", "openpr", "globenewswire", "prnewswire",
    "market.us", "marketresearch", "research and markets",
]

# Blocklist frází v titulku – podpisy „market research" spamu.
BLOCK_PATTERNS = [
    "market growth", "market analysis", "market demand", "market size",
    "market outlook", "market report", "market share", "market trends",
    "market forecast", "cagr", "forecast to 20", "outlook to 20",
    "growth outlook", "market value",
]

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 20

# ── JÁDRO ──────────────────────────────────────────────────────────────────


def _gn_url(query: str, locale=None) -> str:
    hl, gl, ceid = locale or DEFAULT_LOCALE
    q = urllib.parse.quote(query)
    return (f"https://news.google.com/rss/search?q={q}"
            f"&hl={hl}&gl={gl}&ceid={ceid}")


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


_tr_cache = {}


def translate_cs(text: str) -> str:
    """Přeloží text do češtiny přes bezklíčový Google Translate endpoint.
    Při jakékoli chybě vrátí originál (nikdy nespadne)."""
    if not TRANSLATE or not text or not text.strip():
        return text
    if text in _tr_cache:
        return _tr_cache[text]
    try:
        params = urllib.parse.urlencode({
            "client": "gtx", "sl": "auto", "tl": TARGET_LANG, "dt": "t", "q": text,
        })
        url = "https://translate.googleapis.com/translate_a/single?" + params
        data = json.loads(_fetch(url).decode("utf-8"))
        out = "".join(seg[0] for seg in data[0] if seg and seg[0]).strip()
        out = out or text
    except Exception:
        out = text
    _tr_cache[text] = out
    return out


def _parse_items(xml_bytes: bytes):
    """Vrátí seznam dictů {title, link, source, dt} z RSS."""
    out = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return out
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        src_el = item.find("source")
        source = (src_el.text.strip() if src_el is not None and src_el.text else "")
        pub = item.findtext("pubDate") or ""
        try:
            dt = parsedate_to_datetime(pub)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            dt = None
        if title and link:
            out.append({"title": title, "link": link, "source": source, "dt": dt})
    return out


def _clean_title(title: str):
    """Google News přidává ' - Zdroj' na konec titulku → oddělíme zdroj."""
    src = ""
    if " - " in title:
        head, _, tail = title.rpartition(" - ")
        if head and tail and len(tail) < 60:
            title, src = head, tail
    return title.strip(), src.strip()


def _keep(title: str) -> bool:
    if not KEYWORDS:
        return True
    t = title.lower()
    return any(k in t for k in KEYWORDS)


def get_news():
    """Stáhne a setřídí zprávy. Vrátí list (nadpis_sekce, [zprávy])."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    result = []
    seen_global = set()  # dedup napříč sekcemi (titulky se opakují)

    for section in SECTIONS:
        section_title, queries = section[0], section[1]
        locale  = section[2] if len(section) > 2 else None
        require = section[3] if len(section) > 3 else None
        items = []
        seen_local = set()
        for q in queries:
            try:
                raw = _fetch(_gn_url(q, locale))
            except Exception:
                continue
            for it in _parse_items(raw):
                title, src_from_title = _clean_title(it["title"])
                source = it["source"] or src_from_title
                tl = title.lower()
                sl = source.lower()
                key = tl[:90]
                if key in seen_local or key in seen_global:
                    continue
                if it["dt"] and it["dt"] < cutoff:
                    continue
                # blocklist spam zdrojů a frází
                if any(b in sl for b in BLOCK_SOURCES):
                    continue
                if any(p in tl for p in BLOCK_PATTERNS):
                    continue
                # geo-brána (FR/DE musí zmiňovat zemi/elektrárnu)
                if require and not any(g in tl for g in require):
                    continue
                # energetická relevance
                if not _keep(title):
                    continue
                seen_local.add(key)
                seen_global.add(key)
                items.append({
                    "title": title,
                    "title_cs": translate_cs(title),
                    "link": it["link"],
                    "source": source,
                    "dt": it["dt"],
                })
        # nejnovější nahoře (zprávy bez data dolů)
        items.sort(key=lambda x: x["dt"] or datetime.min.replace(tzinfo=timezone.utc),
                   reverse=True)
        result.append((section_title, items[:MAX_PER_SECTION]))
    return result


# ── HTML ─────────────────────────────────────────────────────────────────────


def build_news_html(news=None) -> str:
    """Vrátí HTML blok s přehledem (k vložení do těla e-mailu)."""
    if news is None:
        news = get_news()

    now_local = datetime.now().strftime("%d.%m.%Y %H:%M")
    parts = [
        '<div style="font-family:Arial,Helvetica,sans-serif;color:#1a1a1a;'
        'max-width:720px;">',
        f'<h2 style="margin:0 0 2px 0;font-size:18px;">⚡ Energetické zprávy — '
        f'Francie &amp; Německo</h2>',
        f'<div style="color:#777;font-size:12px;margin-bottom:14px;">'
        f'sestaveno {now_local} · posledních {LOOKBACK_HOURS} h · zdroj: Google News</div>',
    ]

    any_news = False
    for section_title, items in news:
        parts.append(
            f'<h3 style="margin:16px 0 6px 0;font-size:15px;'
            f'border-bottom:2px solid #e0c200;padding-bottom:3px;">'
            f'{html.escape(section_title)}</h3>'
        )
        if not items:
            parts.append('<div style="color:#999;font-size:13px;margin:4px 0;">'
                         '— nic nového —</div>')
            continue
        any_news = True
        parts.append('<ul style="margin:4px 0 0 0;padding-left:18px;">')
        for it in items:
            t_cs = html.escape(it.get("title_cs") or it["title"])
            t_orig = html.escape(it["title"])
            link = html.escape(it["link"], quote=True)
            src = html.escape(it["source"]) if it["source"] else ""
            when = it["dt"].astimezone().strftime("%d.%m %H:%M") if it["dt"] else ""
            meta = " · ".join(x for x in (src, when) if x)
            show_orig = SHOW_ORIGINAL and t_orig and t_orig != t_cs
            parts.append(
                f'<li style="margin:7px 0;font-size:13px;line-height:1.35;">'
                f'<a href="{link}" style="color:#0b5cad;text-decoration:none;'
                f'font-weight:600;">{t_cs}</a>'
                + (f'<span style="color:#999;"> &nbsp;({meta})</span>' if meta else "")
                + (f'<div style="color:#aaa;font-size:11px;font-style:italic;'
                   f'margin-top:1px;">{t_orig}</div>' if show_orig else "")
                + '</li>'
            )
        parts.append('</ul>')

    if not any_news:
        parts.append('<div style="color:#999;font-size:13px;">'
                     'Za sledované období nepřišly žádné relevantní zprávy.</div>')

    parts.append('</div>')
    return "".join(parts)


# ── E-MAIL (samostatný režim) ────────────────────────────────────────────────


def send_email(html_body: str):
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ.get("SMTP_USER")
    pwd = os.environ.get("SMTP_PASS")
    mail_from = os.environ.get("MAIL_FROM", user or "")
    mail_to = [a.strip() for a in os.environ.get("MAIL_TO", "").split(",") if a.strip()]

    if not (host and user and pwd and mail_to):
        raise RuntimeError("Chybí SMTP konfigurace (SMTP_HOST/SMTP_USER/SMTP_PASS/MAIL_TO).")

    subject = "⚡ Ranní energetické zprávy — FR & DE — " + datetime.now().strftime("%d.%m.%Y")
    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr(("FLSO News", mail_from))
    msg["To"] = ", ".join(mail_to)

    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=TIMEOUT) as s:
            s.login(user, pwd)
            s.sendmail(mail_from, mail_to, msg.as_string())
    else:  # 587 STARTTLS
        with smtplib.SMTP(host, port, timeout=TIMEOUT) as s:
            s.starttls(context=ctx)
            s.login(user, pwd)
            s.sendmail(mail_from, mail_to, msg.as_string())


def main():
    news = get_news()
    body = build_news_html(news)
    try:
        send_email(body)
        n = sum(len(items) for _, items in news)
        print(f"OK — odesláno, {n} zpráv.")
    except Exception as e:
        # když není SMTP nastaveno, aspoň ulož náhled
        with open("news_preview.html", "w", encoding="utf-8") as f:
            f.write(body)
        print(f"E-mail neodeslán ({e}). Náhled uložen do news_preview.html")


if __name__ == "__main__":
    main()
