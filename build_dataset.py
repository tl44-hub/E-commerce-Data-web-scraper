"""
Packages the cleaned scrape into files you can list on a data marketplace.

Reads  data/products.csv and data/history.csv   (made by export_csv.py)
Writes data/dataset/
    latest_prices.csv     the newest snapshot, one row per product (or size/variant)
    price_history.csv     every weekly snapshot stacked together, with a run_date column
    sample.csv            a small, evenly spread free sample for marketplace listings
    README.md             description, coverage, column dictionary and limitations
    dataset_summary.json  the same coverage numbers in machine-readable form

Uses only the Python standard library.
"""
import csv
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from urllib.parse import urlparse

# ---- Edit these to match what you are selling ---------------------------------------------
DATASET_NAME = "Pet Supplies Price & Stock Data"
UPDATE_FREQUENCY = "Weekly"
SAMPLE_SIZE = 100
# --------------------------------------------------------------------------------------------

IN_DIR = "data"
OUT_DIR = os.path.join(IN_DIR, "dataset")
LATEST_IN = os.path.join(IN_DIR, "products.csv")
HISTORY_IN = os.path.join(IN_DIR, "history.csv")

COLUMNS = ["shop", "product_name", "price", "currency", "price_usd", "usd_rate", "rate_date",
           "sku", "availability", "product_url", "image_url", "scraped_at", "quality_flag"]

COLUMN_HELP = {
    "shop": "The shop's web domain (for example petmax.ca).",
    "product_name": "Product name as shown by the shop. Items sold in several sizes or flavours have one row per size or flavour.",
    "price": "Price in the shop's own currency, exactly as listed.",
    "currency": "3-letter currency code of `price` (for example CAD). Blank if the shop did not state one.",
    "price_usd": "`price` converted to US dollars using `usd_rate`. Blank if no rate was available.",
    "usd_rate": "US dollars per 1 unit of the original currency on the day of collection.",
    "rate_date": "Date of the exchange rate used (European Central Bank reference rate).",
    "sku": "The shop's own product code. Often blank, because many shops leave it empty.",
    "availability": "In stock, Out of stock, or Unknown, as shown by the shop at collection time.",
    "product_url": "Link to the product page on the shop's website.",
    "image_url": "Link to the product image on the shop's website (a link only; no image files are included).",
    "scraped_at": "When the row was collected (UTC).",
    "quality_flag": "Blank for normal rows. Otherwise a note such as `price outlier` or `missing currency` meaning the row deserves a second look.",
    "run_date": "(price_history.csv only) The date of the weekly snapshot the row belongs to.",
}


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_csv(path, fieldnames, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore", restval="")
        writer.writeheader()
        writer.writerows(rows)


def shop_name(row):
    """Prefer the domain from the product link; fall back to the internal site name."""
    host = (urlparse(row.get("product_url") or "").hostname or "").lower().removeprefix("www.")
    return host or row.get("site", "")


def to_output_row(row):
    out = {col: row.get(col, "") for col in COLUMNS}
    out["shop"] = shop_name(row)
    out["quality_flag"] = row.get("review_reason", "")
    if "run_date" in row:
        out["run_date"] = row["run_date"]
    return out


def to_float(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def evenly_spaced_sample(rows, size):
    """A sample that covers every shop and is not biased towards the start of the alphabet."""
    by_shop = defaultdict(list)
    for row in rows:
        by_shop[row["shop"]].append(row)
    if not by_shop:
        return []
    quota = max(1, size // len(by_shop))
    sample = []
    for shop in sorted(by_shop):
        items = by_shop[shop]
        take = min(quota, len(items))
        sample.extend(items[int(i * len(items) / take)] for i in range(take))
    return sample[:size] if len(sample) > size else sample


def main():
    if not os.path.exists(LATEST_IN):
        print(f"{LATEST_IN} not found; run export_csv.py first. Nothing was built.")
        sys.exit(0)

    latest = sorted((to_output_row(r) for r in read_csv(LATEST_IN)),
                    key=lambda r: (r["shop"], r["product_name"].lower()))
    if not latest:
        print("The latest file has no rows; nothing was built.")
        sys.exit(0)
    history = [to_output_row(r) for r in read_csv(HISTORY_IN)] if os.path.exists(HISTORY_IN) else []

    os.makedirs(OUT_DIR, exist_ok=True)
    write_csv(os.path.join(OUT_DIR, "latest_prices.csv"), COLUMNS, latest)
    write_csv(os.path.join(OUT_DIR, "price_history.csv"), ["run_date"] + COLUMNS, history)

    clean_rows = [r for r in latest if not r["quality_flag"]] or latest
    sample = evenly_spaced_sample(clean_rows, SAMPLE_SIZE)
    write_csv(os.path.join(OUT_DIR, "sample.csv"), COLUMNS, sample)

    # ---- coverage numbers ----
    shops = []
    for shop in sorted({r["shop"] for r in latest}):
        rows = [r for r in latest if r["shop"] == shop]
        stock = Counter(r["availability"] or "Unknown" for r in rows)
        prices = [p for p in (to_float(r["price"]) for r in rows) if p is not None]
        shops.append({
            "shop": shop,
            "products": len(rows),
            "in_stock": stock.get("In stock", 0),
            "out_of_stock": stock.get("Out of stock", 0),
            "unknown_stock": stock.get("Unknown", 0),
            "currencies": sorted({r["currency"] for r in rows if r["currency"]}),
            "median_price": round(statistics.median(prices), 2) if prices else None,
        })
    run_dates = sorted({r["run_date"] for r in history if r.get("run_date")})
    flagged = sum(1 for r in latest if r["quality_flag"])
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    summary = {
        "dataset": DATASET_NAME,
        "generated_at": now,
        "update_frequency": UPDATE_FREQUENCY,
        "shops": shops,
        "latest_rows": len(latest),
        "flagged_rows": flagged,
        "history_rows": len(history),
        "history_snapshots": len(run_dates),
        "history_first_date": run_dates[0] if run_dates else None,
        "history_last_date": run_dates[-1] if run_dates else None,
    }
    with open(os.path.join(OUT_DIR, "dataset_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    # ---- README ----
    coverage_rows = "\n".join(
        f"| {s['shop']} | {s['products']} | {s['in_stock']} | {s['out_of_stock']} | "
        f"{', '.join(s['currencies']) or '-'} |" for s in shops)
    column_rows = "\n".join(f"| `{name}` | {text} |" for name, text in COLUMN_HELP.items())
    span = (f"{run_dates[0]} to {run_dates[-1]} ({len(run_dates)} snapshot(s))" if run_dates else "no history yet")
    readme = f"""# {DATASET_NAME}

Prices and stock status for pet products, collected from the public websites of online pet shops
and cleaned into one consistent table.

- **Last built:** {now}
- **Update frequency:** {UPDATE_FREQUENCY}
- **Shops covered:** {len(shops)}
- **Products in the latest snapshot:** {len(latest)}
- **History covered:** {span}

## Files

| File | What it is |
|---|---|
| `latest_prices.csv` | The newest snapshot, one row per product (or size/variant). |
| `price_history.csv` | Every snapshot so far, so price and stock changes can be tracked over time. |
| `sample.csv` | A free sample of up to {SAMPLE_SIZE} rows spread across all shops. |
| `dataset_summary.json` | The coverage numbers below in machine-readable form. |

## Coverage

| Shop | Products | In stock | Out of stock | Currency |
|---|---|---|---|---|
{coverage_rows}

## Columns

| Column | Meaning |
|---|---|
{column_rows}

## How the data is prepared

- Collected automatically from the shops' public web pages or public product feeds.
- Rows with no product name or no usable price are removed; duplicates are removed.
- Text is tidied (extra spaces, stray characters), and currency and stock labels are standardised.
- Suspicious rows are **kept but flagged** in `quality_flag` rather than silently changed. {flagged} row(s) are flagged in the latest snapshot.
- US dollar prices use daily European Central Bank reference rates (via the Frankfurter service). They are indicative, not the rate any bank or card would charge.

## Known limitations

- Each file is a **snapshot**. Prices and stock can change between updates.
- Coverage is limited to the shops listed above, and may not include a shop's entire catalogue.
- The same product is named differently by different shops, so products are **not matched across shops** in this version.
- Some shops leave the SKU empty.
- Not affiliated with or endorsed by any of the shops listed.

## License and terms

[Add your license / terms of use here. Decide these before you sell, and consider legal advice.]

## Contact

[Add your contact details here.]
"""
    with open(os.path.join(OUT_DIR, "README.md"), "w", encoding="utf-8") as f:
        f.write(readme)

    print(f"Built dataset in {OUT_DIR}: {len(latest)} latest rows, {len(history)} history rows, "
          f"{len(sample)} sample rows, {len(shops)} shop(s), {flagged} flagged")


if __name__ == "__main__":
    main()
