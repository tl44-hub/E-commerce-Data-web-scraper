"""
Cleans the latest scrape, then saves:
  data/products.csv  - clean results from the latest run
  data/history.csv   - every day's clean snapshot stacked together
  data/rejected.csv  - rows that were removed, with the reason (so nothing disappears silently)

Cleaning rules: drop rows with no name or no usable price, remove duplicates, tidy text,
standardise currency and stock labels, and FLAG (never silently change) suspicious prices.
"""
import csv
import html as html_lib
import os
import re
import sqlite3
import statistics
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone

DB = sys.argv[1] if len(sys.argv) > 1 else "products.db"
OUT_DIR = "data"
LATEST = os.path.join(OUT_DIR, "products.csv")
HISTORY = os.path.join(OUT_DIR, "history.csv")
REJECTED = os.path.join(OUT_DIR, "rejected.csv")

COLUMNS = ["site", "product_name", "price", "currency", "sku", "availability",
           "image_url", "product_url", "scraped_at", "review_reason"]
REJECT_COLUMNS = ["reason", "site", "product_name", "price", "sku", "product_url", "scraped_at"]

OUTLIER_FACTOR = 10     # flag prices more than 10x above / below the site's median price
MIN_ROWS_FOR_OUTLIERS = 10


def clean_text(value):
    """Tidy any text: decode leftovers like &amp;, normalise odd characters, collapse spaces."""
    if value is None:
        return ""
    text = html_lib.unescape(str(value))
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", text)     # invisible zero-width characters
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)                 # control characters
    return re.sub(r"\s+", " ", text).strip()


def fix_caps(name):
    """'ULTRABOOK PRO 15' -> 'Ultrabook PRO 15'. Short words and model numbers stay as they are."""
    if not name.isupper() or sum(ch.isalpha() for ch in name) <= 3:
        return name
    words = []
    for word in name.split(" "):
        keep = len(word) <= 3 or any(ch.isdigit() for ch in word)
        words.append(word if keep else word.capitalize())
    return " ".join(words)


def clean_url(value):
    url = clean_text(value)
    return url if re.match(r"^https?://", url, re.IGNORECASE) else ""


def clean_currency(value):
    code = clean_text(value).upper()
    return code if re.fullmatch(r"[A-Z]{3}", code) else ""


def stock_label(value):
    return {1: "In stock", 0: "Out of stock"}.get(value, "Unknown")


conn = sqlite3.connect(DB)
raw = conn.execute(
    """SELECT site, product_name, price, currency, sku, availability,
              image_url, product_url, scraped_at FROM products"""
).fetchall()

if not raw:
    print("No products were scraped, so the files were left unchanged")
    sys.exit(0)

os.makedirs(OUT_DIR, exist_ok=True)
rejected = []   # (reason, site, name, price, sku, url, scraped_at)
rows = []       # cleaned products as dicts


def reject(reason, row):
    rejected.append([reason, row["site"], row["product_name"], row["price"],
                     row["sku"], row["product_url"], row["scraped_at"]])


# 1) Tidy every field, and drop rows that are unusable
for site, name, price, currency, sku, availability, image, url, scraped_at in raw:
    row = {
        "site": clean_text(site),
        "product_name": fix_caps(clean_text(name)),
        "price": price,
        "currency": clean_currency(currency),
        "sku": clean_text(sku),
        "availability": stock_label(availability),
        "image_url": clean_url(image),
        "product_url": clean_url(url),
        "scraped_at": clean_text(scraped_at),
        "review_reason": "",
    }
    if not row["product_name"]:
        reject("missing product name", row)
    elif price is None:
        reject("missing price", row)
    elif price <= 0:
        reject("price is zero or negative", row)
    else:
        row["price"] = round(float(price), 2)
        rows.append(row)

# 2) Remove duplicates (same shop + same SKU, or same name when there is no SKU); keep the newest
best = {}
for row in rows:
    key = (row["site"], row["sku"].lower() if row["sku"] else row["product_name"].lower())
    previous = best.get(key)
    if previous is None:
        best[key] = row
    elif row["scraped_at"] > previous["scraped_at"]:
        reject("duplicate", previous)
        best[key] = row
    else:
        reject("duplicate", row)
rows = sorted(best.values(), key=lambda r: (r["site"], r["product_name"].lower()))

# 3) Flag (don't change) suspicious data so a human can check it
by_site = defaultdict(list)
for row in rows:
    by_site[row["site"]].append(row)
for site_rows in by_site.values():
    if len(site_rows) >= MIN_ROWS_FOR_OUTLIERS:
        median = statistics.median(r["price"] for r in site_rows)
        for row in site_rows:
            if row["price"] > median * OUTLIER_FACTOR or row["price"] < median / OUTLIER_FACTOR:
                row["review_reason"] = "price outlier"
for row in rows:
    if not row["currency"]:
        row["review_reason"] = "; ".join(filter(None, [row["review_reason"], "missing currency"]))

# 4) Save the rejected-rows report (always, so you can see what was removed)
with open(REJECTED, "w", newline="", encoding="utf-8-sig") as f:
    writer = csv.writer(f)
    writer.writerow(REJECT_COLUMNS)
    writer.writerows(rejected)

if not rows:
    print(f"Every row was rejected ({len(rejected)}); products and history left unchanged")
    sys.exit(0)

# 5) Save the latest clean results
with open(LATEST, "w", newline="", encoding="utf-8-sig") as f:
    writer = csv.DictWriter(f, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(rows)

# 6) History: keep earlier days (padding any new columns), replace today's snapshot
today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
history_columns = ["run_date"] + COLUMNS
old_rows = []
if os.path.exists(HISTORY):
    with open(HISTORY, newline="", encoding="utf-8-sig") as f:
        old_rows = [r for r in csv.DictReader(f) if r.get("run_date") and r["run_date"] != today]

with open(HISTORY, "w", newline="", encoding="utf-8-sig") as f:
    writer = csv.DictWriter(f, fieldnames=history_columns, restval="", extrasaction="ignore")
    writer.writeheader()
    writer.writerows(old_rows)
    writer.writerows({"run_date": today, **row} for row in rows)

flagged = sum(1 for r in rows if r["review_reason"])
print(f"Kept {len(rows)} products ({flagged} flagged for review), rejected {len(rejected)}; "
      f"history now has {len(old_rows) + len(rows)} rows")
