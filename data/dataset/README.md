# Pet Supplies Price & Stock Data

Prices and stock status for pet products, collected from the public websites of online pet shops
and cleaned into one consistent table.

- **Last built:** 2026-11-12 03:22 UTC
- **Update frequency:** Weekly
- **Shops covered:** 3
- **Products in the latest snapshot:** 3210
- **Products found at 2 or more shops:** 21
- **History covered:** 2026-10-30 to 2026-11-12 (13 snapshot(s))

## Files

| File | What it is |
|---|---|
| `latest_prices.csv` | The newest snapshot, one row per product (or size/variant). |
| `price_history.csv` | Every snapshot so far, so price and stock changes can be tracked over time. |
| `price_comparison.csv` | Products sold by 2 or more shops, side by side in US dollars, with the cheapest shop and the price spread. |
| `sample.csv` | A free sample of up to 100 rows spread across all shops. |
| `dataset_summary.json` | The coverage numbers below in machine-readable form. |

## Coverage

| Shop | Products | In stock | Out of stock | Currency |
|---|---|---|---|---|
| ashariopets.ca | 1462 | 1218 | 244 | CAD |
| canvasbackpets.com | 1000 | 784 | 216 | CAD |
| petmax.ca | 748 | 643 | 105 | CAD |

## Columns

| Column | Meaning |
|---|---|
| `shop` | The shop's web domain (for example petmax.ca). |
| `product_group_id` | Identifier shared by the same product at different shops. Rows with the same id are the same product. A product found at only one shop has an id of its own. |
| `product_name` | Product name as shown by the shop. Items sold in several sizes or flavours have one row per size or flavour. |
| `price` | Price in the shop's own currency, exactly as listed. |
| `currency` | 3-letter currency code of `price` (for example CAD). Blank if the shop did not state one. |
| `price_usd` | `price` converted to US dollars using `usd_rate`. Blank if no rate was available. |
| `usd_rate` | US dollars per 1 unit of the original currency on the day of collection. |
| `rate_date` | Date of the exchange rate used (European Central Bank reference rate). |
| `sku` | The shop's own product code. Often blank, because many shops leave it empty. |
| `availability` | In stock, Out of stock, or Unknown, as shown by the shop at collection time. |
| `product_url` | Link to the product page on the shop's website. |
| `image_url` | Link to the product image on the shop's website (a link only; no image files are included). |
| `scraped_at` | When the row was collected (UTC). |
| `match_confidence` | How sure the matching is, from 0.70 to 1.00 (1.00 = barcode or identical name and size). Blank when the product was found at only one shop. |
| `match_method` | How the match was made: barcode, name+size, name, or manual. Blank when unmatched. |
| `quality_flag` | Blank for normal rows. Otherwise a note such as `price outlier` or `missing currency` meaning the row deserves a second look. |
| `run_date` | (price_history.csv only) The date of the weekly snapshot the row belongs to. |

## How the data is prepared

- Collected automatically from the shops' public web pages or public product feeds.
- Rows with no product name or no usable price are removed; duplicates are removed.
- The same product at different shops is linked with a shared `product_group_id`.
- Text is tidied (extra spaces, stray characters), and currency and stock labels are standardised.
- Suspicious rows are **kept but flagged** in `quality_flag` rather than silently changed. 35 row(s) are flagged in the latest snapshot.
- US dollar prices use daily European Central Bank reference rates (via the Frankfurter service). They are indicative, not the rate any bank or card would charge.

## Known limitations

- Each file is a **snapshot**. Prices and stock can change between updates.
- Coverage is limited to the shops listed above, and may not include a shop's entire catalogue.
- Matching the same product across shops is automatic (barcode, then product name and size). It is cautious, so some genuine matches are missed, and a few may be wrong. Use `match_confidence` to judge each one; products are only matched when the size, animal, life stage and flavour agree.
- `product_group_id` is stable while a product's listing stays the same, but can change if a shop renames or removes it.
- Some shops leave the SKU empty.
- Not affiliated with or endorsed by any of the shops listed.

## License and terms

[Add your license / terms of use here. Decide these before you sell, and consider legal advice.]

## Contact

[Add your contact details here.]
