"""
parts_export.py - INDUSTRIAL project only. Turns industrial.db into clean "offers" files.

An "offer" is one distributor's listing of a part: who sells it, their own item number, the price,
the pack size and stock. The manufacturer + manufacturer part number (MPN) identify the PART;
parts_match.py uses them to line up offers from different distributors.

Usage:   python parts_export.py industrial.db
Reads    industrial.db          (made by main.py)
         brand_aliases_industrial.csv   optional, header: alias,canonical   (e.g. "SKF USA",SKF)
Writes   data/industrial/offers.csv           latest clean offers
         data/industrial/offers_history.csv   every snapshot, with a run_date column
         data/industrial/offers_rejected.csv  rows removed, with the reason
(The folder can be changed with the DATA_DIR environment variable.)

Uses only the Python standard library.
"""
import csv
import html as html_lib
import json
import os
import re
import sqlite3
import sys
import unicodedata
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

DB = sys.argv[1] if len(sys.argv) > 1 else "industrial.db"
OUT_DIR = os.environ.get("DATA_DIR", "data/industrial")
OFFERS = os.path.join(OUT_DIR, "offers.csv")
HISTORY = os.path.join(OUT_DIR, "offers_history.csv")
REJECTED = os.path.join(OUT_DIR, "offers_rejected.csv")
ALIASES_FILE = os.environ.get("BRAND_ALIASES", "brand_aliases_industrial.csv")

COLUMNS = ["distributor", "brand", "brand_key", "mpn", "mpn_key", "gtin", "distributor_sku", "description",
           "price", "currency", "price_usd", "usd_rate", "rate_date", "pack_qty", "pack_source",
           "unit_price_usd", "availability", "product_url", "scraped_at", "quality_flag"]
REJECT_COLUMNS = ["reason", "distributor", "description", "price", "distributor_sku", "product_url", "scraped_at"]

# --------------------------------------------------------------------------- #
# Text, brand and part-number normalisation
# --------------------------------------------------------------------------- #
_SUFFIXES = {"inc", "llc", "corp", "corporation", "co", "company", "ltd", "limited", "gmbh", "ag", "ab",
             "usa", "us", "america", "americas", "industries", "industrial", "international", "intl",
             "group", "products", "mfg", "manufacturing"}


def clean_text(value):
    if value is None:
        return ""
    text = html_lib.unescape(str(value))
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", text)
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def raw_brand_key(brand):
    """'The 3M Company' -> '3m'; 'SKF USA Inc.' -> 'skf'. Lowercase letters/digits only."""
    text = unicodedata.normalize("NFKD", clean_text(brand)).encode("ascii", "ignore").decode().lower()
    tokens = re.findall(r"[a-z0-9]+", text.replace("&", " and "))
    tokens = [t for t in tokens if t != "the"]
    kept = [t for t in tokens if t not in _SUFFIXES] or tokens
    return "".join(kept)


def load_aliases():
    aliases = {}
    if os.path.exists(ALIASES_FILE):
        with open(ALIASES_FILE, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                a, c = raw_brand_key(row.get("alias")), raw_brand_key(row.get("canonical"))
                if a and c:
                    aliases[a] = c
    return aliases


ALIASES = load_aliases()


def brand_key(brand):
    key = raw_brand_key(brand)
    return ALIASES.get(key, key)


def mpn_key(mpn):
    """'6204-2RS' and '6204 2rs' both become '62042RS'."""
    return re.sub(r"[^A-Z0-9]", "", clean_text(mpn).upper())


def gtin_key(gtin):
    digits = re.sub(r"\D", "", clean_text(gtin))
    return digits.lstrip("0") if len(digits) in (8, 12, 13, 14) else ""


def distributor_of(url, site):
    host = (urlparse(url or "").hostname or "").lower().removeprefix("www.")
    return host or site


# --------------------------------------------------------------------------- #
# Pack size: a price per box of 100 must not be compared with a price per piece
# --------------------------------------------------------------------------- #
_PACK_PATTERNS = [
    re.compile(r"\b(?:pack|pk|box|case|bag|carton|bundle|bx|cs)\s*(?:of|-)\s*(\d{1,6})\b", re.I),
    re.compile(r"\b(\d{1,6})\s*(?:/|per)\s*(?:pack|pk|box|case|bag|carton|bundle|bx|cs)\b", re.I),
    re.compile(r"\b(\d{1,6})\s*[- ]?(?:pk|pack|pcs?|pieces?|ct)\b", re.I),
]
_EACH = re.compile(r"\b(?:each|ea)\b", re.I)


def parse_pack(text):
    for pattern in _PACK_PATTERNS:
        match = pattern.search(text)
        if match and 1 < int(match.group(1)) <= 100000:
            return int(match.group(1)), "parsed"
    if _EACH.search(text):
        return 1, "listed each"
    return 1, "assumed 1"


# --------------------------------------------------------------------------- #
# Exchange rates (only needed if a distributor prices in a non-USD currency)
# --------------------------------------------------------------------------- #
RATE_URLS = [
    "https://api.frankfurter.dev/v1/latest?base=USD&symbols={code}",
    "https://api.frankfurter.dev/v1/latest?from=USD&to={code}",
    "https://api.frankfurter.app/latest?from=USD&to={code}",
]


def fetch_json(url, timeout=15):
    request = urllib.request.Request(url, headers={"User-Agent": "parts-scraper/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def get_usd_rates(currencies):
    rates, dates = {"USD": 1.0}, {}
    for code in sorted(c for c in currencies if c and c != "USD"):
        for template in RATE_URLS:
            try:
                data = fetch_json(template.format(code=code))
                per_usd = float(data["rates"][code])
                if per_usd <= 0:
                    raise ValueError("non-positive rate")
                rates[code] = round(1.0 / per_usd, 6)
                dates[code] = str(data.get("date", ""))
                break
            except Exception as exc:
                print(f"Rate lookup for {code} failed ({template.split('?')[0]}): {exc!r}")
        else:
            print(f"No USD exchange rate found for {code}; its price_usd will be blank")
    return rates, dates


def write_csv(path, fieldnames, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore", restval="")
        writer.writeheader()
        writer.writerows(rows)


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        raw = [dict(r) for r in conn.execute("SELECT * FROM products")]
    except sqlite3.OperationalError:
        print("No products table found; nothing to export.")
        sys.exit(0)
    if not raw:
        print("No products were scraped, so the files were left unchanged")
        sys.exit(0)

    os.makedirs(OUT_DIR, exist_ok=True)
    rejected, rows = [], []

    for r in raw:
        description = clean_text(r.get("product_name"))
        price = r.get("price")
        offer = {
            "distributor": distributor_of(r.get("product_url"), r.get("site", "")),
            "brand": clean_text(r.get("brand")),
            "mpn": clean_text(r.get("mpn")),
            "gtin": clean_text(r.get("gtin")),
            "distributor_sku": clean_text(r.get("sku")),
            "description": description,
            "price": price,
            "currency": clean_text(r.get("currency")).upper() if re.fullmatch(r"[A-Za-z]{3}", clean_text(r.get("currency"))) else "",
            "availability": {1: "In stock", 0: "Out of stock"}.get(r.get("availability"), "Unknown"),
            "product_url": clean_text(r.get("product_url")),
            "scraped_at": clean_text(r.get("scraped_at")),
            "quality_flag": "",
        }
        offer["brand_key"], offer["mpn_key"], offer["gtin"] = brand_key(offer["brand"]), mpn_key(offer["mpn"]), gtin_key(offer["gtin"])
        reason = None
        if not description:
            reason = "missing description"
        elif price is None:
            reason = "missing price"
        elif price <= 0:
            reason = "price is zero or negative"
        if reason:
            rejected.append(dict(zip(REJECT_COLUMNS, [reason, offer["distributor"], description, price,
                                                      offer["distributor_sku"], offer["product_url"],
                                                      offer["scraped_at"]])))
            continue
        offer["price"] = round(float(price), 2)
        offer["pack_qty"], offer["pack_source"] = parse_pack(description)
        rows.append(offer)

    # one row per distributor listing (keep the newest)
    best = {}
    for row in rows:
        key = (row["distributor"], row["distributor_sku"] or row["product_url"] or (row["mpn_key"], row["description"]))
        if key not in best or row["scraped_at"] > best[key]["scraped_at"]:
            best[key] = row
    rows = sorted(best.values(), key=lambda r: (r["brand_key"], r["mpn_key"], r["distributor"]))

    # USD prices, per-unit prices, flags
    rates, dates = get_usd_rates({r["currency"] for r in rows})
    for row in rows:
        flags = []
        code = row["currency"]
        row["usd_rate"], row["price_usd"], row["rate_date"], row["unit_price_usd"] = "", "", "", ""
        if code in rates:
            row["usd_rate"] = rates[code]
            row["price_usd"] = round(row["price"] * rates[code], 2)
            row["rate_date"] = dates.get(code, "")
            row["unit_price_usd"] = round(row["price_usd"] / row["pack_qty"], 4)
        else:
            flags.append("no USD rate")
        if not row["mpn_key"] and not row["gtin"]:
            flags.append("no part number")
        if not row["brand_key"]:
            flags.append("no brand")
        row["quality_flag"] = "; ".join(flags)

    write_csv(REJECTED, REJECT_COLUMNS, rejected)
    if not rows:
        print(f"Every row was rejected ({len(rejected)}); offers left unchanged")
        sys.exit(0)
    write_csv(OFFERS, COLUMNS, rows)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    old = []
    if os.path.exists(HISTORY):
        with open(HISTORY, newline="", encoding="utf-8-sig") as f:
            old = [r for r in csv.DictReader(f) if r.get("run_date") and r["run_date"] != today]
    write_csv(HISTORY, ["run_date"] + COLUMNS, old + [{"run_date": today, **r} for r in rows])

    flagged = sum(1 for r in rows if r["quality_flag"])
    with_mpn = sum(1 for r in rows if r["mpn_key"])
    print(f"Kept {len(rows)} offers ({with_mpn} with a part number, {flagged} flagged), rejected {len(rejected)}; "
          f"history now has {len(old) + len(rows)} rows")


if __name__ == "__main__":
    main()
