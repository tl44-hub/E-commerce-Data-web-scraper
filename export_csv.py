"""Turns products.db into products.csv, which opens in Excel."""
import csv
import sqlite3
import sys

db_path = sys.argv[1] if len(sys.argv) > 1 else "products.db"
out_path = sys.argv[2] if len(sys.argv) > 2 else "products.csv"

conn = sqlite3.connect(db_path)
cur = conn.execute(
    """
    SELECT site, product_name, price, currency, sku,
           CASE availability WHEN 1 THEN 'In stock'
                             WHEN 0 THEN 'Out of stock'
                             ELSE '' END AS availability,
           image_url, product_url, scraped_at
    FROM products
    ORDER BY site, product_name
    """
)

# utf-8-sig makes Excel show symbols like £ correctly
with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
    writer = csv.writer(f)
    writer.writerow([col[0] for col in cur.description])
    count = 0
    for row in cur:
        writer.writerow(row)
        count += 1

print(f"Wrote {count} products to {out_path}")
