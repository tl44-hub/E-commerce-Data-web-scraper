"""
parts_match.py - INDUSTRIAL project only. Lines up the same PART across distributors and
builds the price comparison. Matching is by manufacturer part number (MPN), not by product name.

Reads   data/industrial/offers.csv                     (made by parts_export.py)
        part_crossrefs_industrial.csv                  optional, header: brand_a,mpn_a,brand_b,mpn_b
                                                       (declares two part numbers to be the same part)
        target_parts_industrial.csv                    optional, header: brand,mpn
                                                       (the parts you care about; used for a coverage report)
Writes  data/industrial/offers_matched.csv       every offer + part_id + match_method
        data/industrial/parts.csv                one row per part
        data/industrial/price_comparison.csv     parts sold by 2+ distributors, unit prices side by side
        data/industrial/parts_to_review.csv      matches or comparisons worth a human look
        data/industrial/target_coverage.csv      for each target part: which distributors list it

Matching rules (cautious on purpose):
  1. Same brand + same normalised MPN                      -> match ("mpn+brand")
  2. Same barcode (GTIN)                                   -> match ("gtin")
  3. Same MPN, one side has no brand, and the MPN belongs to ONE brand everywhere else
                                                           -> match ("mpn-only", reviewed)
  4. Part numbers you list in the crossref file            -> match ("crossref", reviewed)
  An MPN that appears under several different brands is never matched without a brand.

Prices are compared as USD PER UNIT, using the pack size parsed from the description.
Uses only the Python standard library.
"""
import csv
import hashlib
import os
import re
import sys
from collections import defaultdict

OUT_DIR = os.environ.get("DATA_DIR", "data/industrial")
OFFERS_IN = os.path.join(OUT_DIR, "offers.csv")
OFFERS_MATCHED = os.path.join(OUT_DIR, "offers_matched.csv")
PARTS = os.path.join(OUT_DIR, "parts.csv")
COMPARISON = os.path.join(OUT_DIR, "price_comparison.csv")
REVIEW = os.path.join(OUT_DIR, "parts_to_review.csv")
COVERAGE = os.path.join(OUT_DIR, "target_coverage.csv")
CROSSREFS_IN = os.environ.get("PART_CROSSREFS", "part_crossrefs_industrial.csv")
TARGETS_IN = os.environ.get("TARGET_PARTS", "target_parts_industrial.csv")
ALIASES_FILE = os.environ.get("BRAND_ALIASES", "brand_aliases_industrial.csv")

MIN_MPN_LENGTH = 3            # shorter part numbers are too generic to trust
MIN_MPN_LENGTH_NO_BRAND = 5   # stricter when the brand is missing
LARGE_SPREAD_PCT = 50.0       # price spreads above this are listed for review


# ---- normalisation (same rules as parts_export.py) ----
_SUFFIXES = {"inc", "llc", "corp", "corporation", "co", "company", "ltd", "limited", "gmbh", "ag", "ab",
             "usa", "us", "america", "americas", "industries", "industrial", "international", "intl",
             "group", "products", "mfg", "manufacturing"}


def raw_brand_key(brand):
    import unicodedata
    text = unicodedata.normalize("NFKD", (brand or "")).encode("ascii", "ignore").decode().lower()
    tokens = [t for t in re.findall(r"[a-z0-9]+", text.replace("&", " and ")) if t != "the"]
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
    return re.sub(r"[^A-Z0-9]", "", (mpn or "").upper())


def to_float(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_csv(path, fieldnames, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore", restval="")
        writer.writeheader()
        writer.writerows(rows)


class DSU:
    def __init__(self, n):
        self.parent = list(range(n))
        self.methods = defaultdict(set)

    def find(self, i):
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a, b, method):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra
            self.methods[ra] |= self.methods.pop(rb, set())
        self.methods[self.find(a)].add(method)


def main():
    offers = read_csv(OFFERS_IN)
    if not offers:
        print(f"{OFFERS_IN} not found or empty; run parts_export.py first. Nothing was matched.")
        sys.exit(0)
    for row in offers:                                   # trust the keys, but recompute if missing
        row["brand_key"] = row.get("brand_key") or brand_key(row.get("brand"))
        row["mpn_key"] = row.get("mpn_key") or mpn_key(row.get("mpn"))
    n = len(offers)
    dsu = DSU(n)
    review_notes = defaultdict(set)                      # root index -> reasons (filled after grouping)
    ambiguous = set()                                    # offer indexes whose MPN is shared by several brands

    # 1) brand + MPN
    by_part = defaultdict(list)
    for i, row in enumerate(offers):
        if row["brand_key"] and len(row["mpn_key"]) >= MIN_MPN_LENGTH:
            by_part[(row["brand_key"], row["mpn_key"])].append(i)
    for members in by_part.values():
        for other in members[1:]:
            dsu.union(members[0], other, "mpn+brand")
        dsu.methods[dsu.find(members[0])].add("mpn+brand")

    # 2) barcode
    by_gtin = defaultdict(list)
    for i, row in enumerate(offers):
        if row.get("gtin"):
            by_gtin[row["gtin"]].append(i)
    for members in by_gtin.values():
        for other in members[1:]:
            dsu.union(members[0], other, "gtin")

    # 3) MPN without a brand
    brands_for_mpn = defaultdict(set)
    for row in offers:
        if row["brand_key"] and row["mpn_key"]:
            brands_for_mpn[row["mpn_key"]].add(row["brand_key"])
    no_brand_groups = defaultdict(list)
    for i, row in enumerate(offers):
        if row["brand_key"] or len(row["mpn_key"]) < MIN_MPN_LENGTH_NO_BRAND:
            continue
        brands = brands_for_mpn.get(row["mpn_key"], set())
        if len(brands) == 1:
            dsu.union(by_part[(next(iter(brands)), row["mpn_key"])][0], i, "mpn-only")
        elif len(brands) > 1:
            ambiguous.add(i)
        else:
            no_brand_groups[row["mpn_key"]].append(i)
    for members in no_brand_groups.values():
        if len({offers[i]["distributor"] for i in members}) >= 2:
            for other in members[1:]:
                dsu.union(members[0], other, "mpn-only")

    # 4) your crossrefs
    by_mpn = defaultdict(list)
    for i, row in enumerate(offers):
        if row["mpn_key"]:
            by_mpn[row["mpn_key"]].append(i)

    def find_offers(brand, mpn):
        bk, mk = brand_key(brand), mpn_key(mpn)
        return [i for i in by_mpn.get(mk, []) if not bk or offers[i]["brand_key"] == bk]

    for ref in read_csv(CROSSREFS_IN):
        side_a, side_b = find_offers(ref.get("brand_a"), ref.get("mpn_a")), find_offers(ref.get("brand_b"), ref.get("mpn_b"))
        for i in side_a + side_b:
            if (side_a and side_b):
                dsu.union((side_a + side_b)[0], i, "crossref")

    # ---- groups -> part ids ----
    groups = defaultdict(list)
    for i in range(n):
        groups[dsu.find(i)].append(i)

    def make_id(members):
        ordered = sorted(members, key=lambda i: (offers[i]["brand_key"] == "", offers[i]["brand_key"], offers[i]["mpn_key"]))
        first = offers[ordered[0]]
        if first["mpn_key"]:
            key = f"{first['brand_key']}|{first['mpn_key']}"
        elif first.get("gtin"):
            key = "gtin:" + first["gtin"]
        else:
            key = f"{first['distributor']}|{first.get('distributor_sku') or first.get('product_url')}"
        return ("p" if (first["mpn_key"] or first.get("gtin")) else "u") + hashlib.sha1(key.encode()).hexdigest()[:10]

    part_of, group_info, used = {}, {}, set()
    for root, members in groups.items():
        pid = make_id(members)
        base, k = pid, 2
        while pid in used:
            pid, k = f"{base}-{k}", k + 1
        used.add(pid)
        methods = dsu.methods.get(root, set())
        method = ("crossref" if "crossref" in methods else "mpn-only" if "mpn-only" in methods
                  else "gtin" if "gtin" in methods else "mpn+brand" if "mpn+brand" in methods else "")
        group_info[pid] = (members, method)
        for i in members:
            part_of[i] = (pid, method if len(members) > 1 else "")

    # ---- offers_matched.csv ----
    out_fields = list(offers[0].keys())
    for extra in ("part_id", "match_method"):
        if extra not in out_fields:
            out_fields.append(extra)
    for i, row in enumerate(offers):
        row["part_id"], row["match_method"] = part_of[i]
        if i in ambiguous and "ambiguous brand" not in (row.get("quality_flag") or ""):
            row["quality_flag"] = "; ".join(filter(None, [row.get("quality_flag"), "mpn shared by several brands"]))
    write_csv(OFFERS_MATCHED, out_fields, sorted(offers, key=lambda r: (r["part_id"], r["distributor"])))

    # ---- parts, comparison, review ----
    parts_rows, comparison, review = [], [], []
    distributors = sorted({r["distributor"] for r in offers})
    for pid, (members, method) in group_info.items():
        rows = [offers[i] for i in members]
        if not (rows[0]["mpn_key"] or rows[0].get("gtin")) and not any(r["mpn_key"] or r.get("gtin") for r in rows):
            continue                                       # unidentifiable offers are not "parts"
        rep = sorted(rows, key=lambda r: (r["brand_key"] == "", -len(r.get("description", ""))))[0]

        def most_common(field):
            counts = defaultdict(int)
            for r in rows:
                if r.get(field):
                    counts[r[field]] += 1
            return sorted(counts, key=lambda v: (-counts[v], len(v), v))[0] if counts else ""
        shown_brand, shown_mpn = most_common("brand"), most_common("mpn")
        by_dist = defaultdict(list)
        for r in rows:
            by_dist[r["distributor"]].append(r)
        parts_rows.append({"part_id": pid, "brand": shown_brand, "mpn": shown_mpn,
                           "description": rep.get("description", ""), "distributors_count": len(by_dist),
                           "offers_count": len(rows), "match_method": method})
        if len(by_dist) < 2:
            continue

        best_per_dist = {}
        for dist, offs in by_dist.items():
            priced = [o for o in offs if to_float(o.get("unit_price_usd")) is not None]
            if not priced:
                continue
            buyable = [o for o in priced if o.get("availability") != "Out of stock"] or priced
            best_per_dist[dist] = min(buyable, key=lambda o: to_float(o["unit_price_usd"]))
        record = {"part_id": pid, "brand": shown_brand, "mpn": shown_mpn,
                  "description": rep.get("description", ""), "distributors_count": len(by_dist),
                  "match_method": method}
        reasons = set()
        if method in ("mpn-only", "crossref"):
            reasons.add(f"{method} match")
        pack_sizes = {o["pack_qty"] for o in best_per_dist.values()}
        if len(pack_sizes) > 1:
            reasons.add("pack size differs between distributors")
        if any(o.get("pack_source") == "assumed 1" for o in best_per_dist.values()) and any(o.get("pack_source") == "parsed" for o in best_per_dist.values()):
            reasons.add("pack size assumed at one distributor")
        if len(best_per_dist) >= 2:
            in_stock = [kv for kv in best_per_dist.items() if kv[1].get("availability") != "Out of stock"]
            pool = in_stock if len(in_stock) >= 2 else list(best_per_dist.items())
            ranked = sorted(pool, key=lambda kv: to_float(kv[1]["unit_price_usd"]))
            low, high = ranked[0], ranked[-1]
            low_p, high_p = to_float(low[1]["unit_price_usd"]), to_float(high[1]["unit_price_usd"])
            spread = round((high_p - low_p) / low_p * 100, 1) if low_p else ""
            record.update(cheapest_distributor=low[0], min_unit_price_usd=round(low_p, 4),
                          max_unit_price_usd=round(high_p, 4), spread_pct=spread)
            if spread != "" and spread > LARGE_SPREAD_PCT:
                reasons.add("large price spread")
        record["review_reason"] = "; ".join(sorted(reasons))
        for dist in distributors:
            o = best_per_dist.get(dist)
            record[f"unit_price_usd__{dist}"] = o["unit_price_usd"] if o else ""
            record[f"pack_qty__{dist}"] = o["pack_qty"] if o else ""
            record[f"availability__{dist}"] = o["availability"] if o else ""
        comparison.append(record)
        if reasons:
            for r in rows:
                review.append({"part_id": pid, "reason": "; ".join(sorted(reasons)), "distributor": r["distributor"],
                               "brand": r.get("brand", ""), "mpn": r.get("mpn", ""), "description": r.get("description", ""),
                               "price": r.get("price", ""), "currency": r.get("currency", ""), "pack_qty": r.get("pack_qty", ""),
                               "unit_price_usd": r.get("unit_price_usd", ""), "product_url": r.get("product_url", "")})

    comparison.sort(key=lambda r: (-(r.get("spread_pct") or 0), r["mpn"]))
    write_csv(PARTS, ["part_id", "brand", "mpn", "description", "distributors_count", "offers_count", "match_method"], parts_rows)
    comp_fields = ["part_id", "brand", "mpn", "description", "distributors_count", "match_method", "cheapest_distributor",
                   "min_unit_price_usd", "max_unit_price_usd", "spread_pct", "review_reason"]
    comp_fields += [f"unit_price_usd__{d}" for d in distributors] + [f"pack_qty__{d}" for d in distributors] + [f"availability__{d}" for d in distributors]
    write_csv(COMPARISON, comp_fields, comparison)
    write_csv(REVIEW, ["part_id", "reason", "distributor", "brand", "mpn", "description", "price", "currency",
                       "pack_qty", "unit_price_usd", "product_url"], review)

    # ---- coverage of the parts you asked about ----
    targets = read_csv(TARGETS_IN)
    if targets:
        coverage = []
        for t in targets:
            bk, mk = brand_key(t.get("brand")), mpn_key(t.get("mpn"))
            hits = [r for r in offers if r["mpn_key"] == mk and (not bk or r["brand_key"] in (bk, ""))] if mk else []
            dists = sorted({r["distributor"] for r in hits})
            prices = [to_float(r.get("unit_price_usd")) for r in hits]
            prices = [p for p in prices if p is not None]
            coverage.append({"brand": t.get("brand", ""), "mpn": t.get("mpn", ""), "distributors_found": len(dists),
                             "which_distributors": "; ".join(dists), "min_unit_price_usd": round(min(prices), 4) if prices else ""})
        write_csv(COVERAGE, ["brand", "mpn", "distributors_found", "which_distributors", "min_unit_price_usd"], coverage)

    multi = len(comparison)
    print(f"Matched {n} offers into {len(parts_rows)} parts; {multi} sold by 2+ distributors; "
          f"{len({r['part_id'] for r in review})} flagged for review")


if __name__ == "__main__":
    main()
