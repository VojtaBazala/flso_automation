# flso_automation – PROJECT STATUS

## Poslední aktualizace
2026-05-17

---

## Co je hotové

### GitHub Actions workflow (`regelleistung.yml`)
- 4 joby běžící paralelně: `fcr`, `afrr`, `mfrr_plus`, `mfrr_minus`
- Scheduled runs každý pracovní den (Po–Pá)
- Retry logika: každých 5 minut v prvních hodinách, pak každých 30 minut do 23:00
- Check před každým stažením — pokud data už jsou v DB, job se přeskočí
- `actions/checkout@v5` + `actions/setup-python@v6` (bez deprecation warnings)

### Časový plán stahování (SEČ/SELČ)
| Produkt | První pokus | Retry do |
|---------|-------------|----------|
| FCR     | 8:16        | 9:16 (každých 5 min), pak 30 min do 23:00 |
| aFRR    | 9:16        | 10:16 (každých 5 min), pak 30 min do 23:00 |
| mFRR+/- | 10:16      | 11:16 (každých 5 min), pak 30 min do 23:00 |

### Skripty
| Soubor | Zdroj dat | DB tabulka(y) |
|--------|-----------|---------------|
| `fcr_download.py` | regelleistung.net | `fcr_overview` |
| `afrr_download.py` | regelleistung.net | `afrr_overview`, `afrr_orderbook` |
| `mfrr_plus_download.py` | ENTSO-E API | `mfrr_orderbook` |
| `mfrr_minus_download.py` | ENTSO-E API | `mfrr_minus_orderbook` |

### Konfigurace
- `config.py` — centrální seznam příjemců emailů (`EMAIL_RECIPIENTS`)
- Všechny 4 skripty importují z `config.py`

### GitHub Secrets
| Secret | Popis |
|--------|-------|
| `DATABASE_URL` | PostgreSQL connection string (Heroku) |
| `GMAIL_APP_PASSWORD` | Gmail app heslo pro odesílání emailů |
| `ENTSOE_API_TOKEN` | API token pro ENTSO-E (mFRR data) |

### Email notifikace
- Odesílatel: `oldrich.bazala@gmail.com`
- Příjemci: definováni v `config.py`
- Při úspěchu: email s přílohou (xlsx)
- Při nedostupnosti dat: email pouze při prvním pokusu (`FIRST_RUN=true`)

---

## Databáze (PostgreSQL / Heroku)

### Tabulky
- `fcr_overview` — FCR výsledky (trade_date, product_name, ceny, MW)
- `afrr_overview` — aFRR přehled (trade_date, product, ceny, MW)
- `afrr_orderbook` — aFRR orderbook CZ (trade_date, product, capacity_price, offered_mw, allocated_mw)
- `mfrr_orderbook` — mFRR+ orderbook (trade_date, timeseries_id, position, quantity_mw, price_eur_mw, cum_quantity_mw)
- `mfrr_minus_orderbook` — mFRR- orderbook (stejná struktura jako mfrr_orderbook)

---

## Co zbývá / TODO
- [ ] Ověřit první ostrý automatický run (zítra ráno)
- [ ] Dashboard vizualizace dat z DB (samostatný projekt)

---

## Poznámky
- `regelleistung_download.py` — starý skript, stále v repozitáři, ale **nepoužívá se** (není volán z workflow)
- mFRR data z ENTSO-E vrací delivery day = zítřek (stejná logika jako FCR/aFRR)
- ENTSO-E API token je uložen jako Secret, **ne v kódu**
