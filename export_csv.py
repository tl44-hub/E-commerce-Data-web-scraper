"""Saves the latest scrape and adds today's snapshot to the history file."""
import csv
import os
import sqlite3
import sys
from datetime import datetime, timezone

DB = sys.argv[1] if len(sys.argv) > 1 else "products.db"
OUT_DIR = "data"
LATEST = os.path.join(OUT_DIR, "products.csv")
HISTORY = os.path.join(OUT_DIR, "history.csv")
COLUMNS = ["site", "product_name", "price", "currency", "sku",
           "availability", "image_url", "product_url", "scraped_at"]

conn = sqlite3.connect(DB)
rows = conn.execute(
    """
    SELECT site, product_name, price, currency, sku,
           CASE availability WHEN 1 THEN 'In stock'
                             WHEN 0 THEN 'Out of stock'
                             ELSE '' END,
           image_url, product_url, scraped_at
    FROM products
    ORDER BY site, product_name
    """
).fetchall()

if not rows:
    print("No products were scraped, so the files were left unchanged")
    sys.exit(0)

os.makedirs(OUT_DIR, exist_ok=True)
today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# 1) The latest results only
with open(LATEST, "w", newline="", encoding="utf-8-sig") as f:
    writer = csv.writer(f)
    writer.writerow(COLUMNS)
    writer.writerows(rows)

# 2) History: keep all earlier days, replace today's snapshot if it already exists
history = []
if os.path.exists(HISTORY):
    with open(HISTORY, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        next(reader, None)                       # skip the header row
        history = [r for r in reader if r and r[0] != today]

with open(HISTORY, "w", newline="", encoding="utf-8-sig") as f:
    writer = csv.writer(f)
    writer.writerow(["run_date"] + COLUMNS)
    writer.writerows(history)
    writer.writerows([today, *r] for r in rows)

print(f"Saved {len(rows)} products for {today}; history now has "
      f"{len(history) + len(rows)} rows")
