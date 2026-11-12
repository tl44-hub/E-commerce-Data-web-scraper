"""
Matches the same product across different shops.

Reads   data/products.csv, data/history.csv      (made by export_csv.py)
        match_overrides.csv                       (optional; your own corrections, see below)
Writes  data/matched_products.csv    latest rows + product_group_id, match_confidence, match_method
        data/matched_history.csv     history rows + product_group_id (looked up by product link)
        data/price_comparison.csv    one row per product sold by 2+ shops, with the price spread
        data/matches_to_review.csv   groups the script is less sure about, for a quick human check

How matching works (cautious on purpose: a wrong match is worse than a missed one):
  1. Barcode: the same barcode-like SKU (8/12/13/14 digits) at two shops is a match.
  2. Name + size: names are normalised (case, punctuation, plurals, filler words), sizes are
     converted to grams / millilitres (so 14.5 kg == 32 lb), and two products match only if
        - they are at different shops,
        - the size agrees (within 3%),
        - species do not conflict (dog vs cat),
        - "attributes" agree exactly: life stage, size class, protein/flavour, wet/canned, diet...
        - the remaining name words are similar enough (Jaccard >= 0.70, or 0.85 with no size).
  3. A group never holds two products from the same shop.

Your corrections: create match_overrides.csv in the main folder with the header
        url_a,url_b,decision
and rows such as   https://shop-a.com/products/x,https://shop-b.com/products/y,same
decision is "same" (force a match) or "different" (never match). Overrides always win.

Uses only the Python standard library.
"""
import csv
import hashlib
import os
import re
import sys
import unicodedata
from collections import defaultdict
from urllib.parse import urlparse

IN_DIR = "data"
PRODUCTS_IN = os.path.join(IN_DIR, "products.csv")
HISTORY_IN = os.path.join(IN_DIR, "history.csv")
OVERRIDES_IN = "match_overrides.csv"

MATCHED_PRODUCTS = os.path.join(IN_DIR, "matched_products.csv")
MATCHED_HISTORY = os.path.join(IN_DIR, "matched_history.csv")
COMPARISON = os.path.join(IN_DIR, "price_comparison.csv")
REVIEW = os.path.join(IN_DIR, "matches_to_review.csv")

SIZE_TOLERANCE = 0.03          # sizes within 3% count as the same size
MIN_SIMILARITY = 0.70          # name similarity needed when a size is present
MIN_SIMILARITY_NO_SIZE = 0.85  # stricter when there is no size to confirm
REVIEW_BELOW = 0.85            # matches under this confidence go to matches_to_review.csv
MAX_BUCKET = 3000              # skip absurdly common words when generating candidates

# --------------------------------------------------------------------------- #
# Word lists (edit freely; these are the rules the matcher uses)
# --------------------------------------------------------------------------- #
NOISE = {"the", "and", "for", "with", "of", "a", "an", "in", "by", "new", "dry", "food",
         "recipe", "formula", "pet", "adult"}
DOG_WORDS = {"dog", "puppy", "canine"}
CAT_WORDS = {"cat", "kitten", "feline"}
SYNONYMS = {"lite": "light", "mature": "senior", "sterilised": "sterilized", "spayed": "sterilized",
            "neutered": "sterilized", "canned": "wet", "can": "wet", "pate": "wet", "stew": "wet",
            "gravy": "wet", "loaf": "wet", "pouch": "wet", "xlarge": "xl"}
# Words that must be IDENTICAL on both sides for two products to be the same product.
ATTRIBUTES = {
    "puppy", "kitten", "senior", "junior",                                      # life stage
    "small", "mini", "medium", "large", "giant", "xs", "xl", "xxl",             # size class
    "chicken", "beef", "lamb", "salmon", "turkey", "duck", "pork", "fish", "tuna", "venison",
    "rabbit", "bison", "whitefish", "herring", "mackerel", "sardine", "cod", "veal", "goat",
    "kangaroo", "quail", "pheasant", "boar", "egg", "cheese", "peanut", "bacon", "liver",
    "wet",                                                                      # wet / canned
    "grain", "free", "limited", "sensitive", "light", "indoor", "sterilized", "hairball",
    "dental", "digestive", "urinary", "renal", "weight",                        # diet / health
}

# --------------------------------------------------------------------------- #
# Name analysis
# --------------------------------------------------------------------------- #
_MEASURE = re.compile(
    r"(?:(?P<mult>\d{1,3})\s*[x\u00d7]\s*)?"
    r"(?P<n>\d+(?:[.,]\d+)?)\s*"
    r"(?P<u>fl\.?\s*oz|kgs?|grams?|g|lbs?|pounds?|oz|ml|l|lt|ltr|litres?|liters?|cts?|count|pk|pack|pcs?|pieces?)\b",
    re.IGNORECASE)


def _unit_info(unit):
    u = unit.lower().replace(" ", "").replace(".", "")
    if u in ("kg", "kgs"):
        return "mass", 1000.0
    if u in ("g", "gram", "grams"):
        return "mass", 1.0
    if u in ("lb", "lbs", "pound", "pounds"):
        return "mass", 453.592
    if u == "oz":
        return "mass", 28.3495
    if u == "floz":
        return "volume", 29.5735
    if u == "ml":
        return "volume", 1.0
    if u in ("l", "lt", "ltr", "litre", "litres", "liter", "liters"):
        return "volume", 1000.0
    return "count", 1.0


def parse_size(name):
    """Return (size_class, value): 'mass' in grams, 'volume' in ml, 'count', or ('none', 0)."""
    measures = []
    for m in _MEASURE.finditer(name):
        cls, factor = _unit_info(m.group("u"))
        value = float(m.group("n").replace(",", ".")) * factor
        if m.group("mult"):
            value *= int(m.group("mult"))
        measures.append((cls, value))
    for wanted in ("mass", "volume", "count"):          # first measure of the most specific kind
        for cls, value in measures:
            if cls == wanted:
                return cls, value
    return "none", 0.0


def _singular(token):
    if len(token) > 3 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us")):
        return token[:-1]
    return token


def analyze(name):
    """Return (core_words, attributes, species, size_class, size_value)."""
    size_class, size_value = parse_size(name)
    text = _MEASURE.sub(" ", name)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    text = text.replace("&", " and ").replace("'", "")
    species, attrs, core = set(), set(), []
    for raw in re.findall(r"[a-z0-9]+", text):
        token = _singular(raw)
        if token in DOG_WORDS:
            species.add("dog")
        if token in CAT_WORDS:
            species.add("cat")
        token = SYNONYMS.get(token, token)
        if token in ATTRIBUTES:
            attrs.add(token)
        elif token in NOISE or token in DOG_WORDS or token in CAT_WORDS:
            continue
        elif token not in core:
            core.append(token)
    return core, frozenset(attrs), frozenset(species), size_class, size_value


def barcode_of(sku):
    sku = (sku or "").strip()
    if sku.isdigit() and len(sku) in (8, 12, 13, 14):
        return sku.lstrip("0")
    return None


def shop_of(row):
    host = (urlparse(row.get("product_url") or "").hostname or "").lower().removeprefix("www.")
    return host or row.get("site", "")


class Item:
    __slots__ = ("idx", "row", "shop", "name", "url", "core", "coreset", "attrs", "species",
                 "size_class", "size_value", "barcode")

    def __init__(self, idx, row):
        self.idx = idx
        self.row = row
        self.shop = shop_of(row)
        self.name = row.get("product_name", "")
        self.url = row.get("product_url", "")
        self.core, self.attrs, self.species, self.size_class, self.size_value = analyze(self.name)
        self.coreset = frozenset(self.core)
        self.barcode = barcode_of(row.get("sku"))


def compare(a, b):
    """Similarity score in [0,1] if a and b look like the same product, else None."""
    if a.shop == b.shop:
        return None
    if a.species and b.species and not (a.species & b.species):
        return None
    if a.attrs != b.attrs or a.size_class != b.size_class:
        return None
    if a.size_class in ("mass", "volume"):
        if abs(a.size_value - b.size_value) / max(a.size_value, b.size_value) > SIZE_TOLERANCE:
            return None
    elif a.size_class == "count" and a.size_value != b.size_value:
        return None
    if min(len(a.coreset), len(b.coreset)) < 2:
        return None
    score = len(a.coreset & b.coreset) / len(a.coreset | b.coreset)
    needed = MIN_SIMILARITY if a.size_class != "none" else MIN_SIMILARITY_NO_SIZE
    return round(score, 4) if score >= needed else None


# --------------------------------------------------------------------------- #
# Grouping (union-find; a group never holds two products from one shop)
# --------------------------------------------------------------------------- #
class Groups:
    def __init__(self, items, forbidden):
        self.items = items
        self.parent = list(range(len(items)))
        self.members = {i: [i] for i in range(len(items))}
        self.shops = {i: {items[i].shop} for i in range(len(items))}
        self.min_score = {i: 1.0 for i in range(len(items))}
        self.methods = {i: set() for i in range(len(items))}
        self.forbidden = forbidden                      # set of frozenset({url_a, url_b})

    def find(self, i):
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, i, j, score, method):
        ri, rj = self.find(i), self.find(j)
        if ri == rj or self.shops[ri] & self.shops[rj]:
            return False
        if self.forbidden:
            for a in self.members[ri]:
                for b in self.members[rj]:
                    if frozenset((self.items[a].url, self.items[b].url)) in self.forbidden:
                        return False
        self.parent[rj] = ri
        self.members[ri] += self.members.pop(rj)
        self.shops[ri] |= self.shops.pop(rj)
        self.min_score[ri] = min(self.min_score[ri], self.min_score.pop(rj), score)
        self.methods[ri] |= self.methods.pop(rj) | {method}
        return True


def load_overrides():
    same, different = [], set()
    if not os.path.exists(OVERRIDES_IN):
        return same, different
    with open(OVERRIDES_IN, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            a, b = (row.get("url_a") or "").strip(), (row.get("url_b") or "").strip()
            decision = (row.get("decision") or "").strip().lower()
            if not a or not b:
                continue
            if decision == "same":
                same.append((a, b))
            elif decision == "different":
                different.add(frozenset((a, b)))
    return same, different


def candidate_pairs(items):
    """Pairs worth comparing: items that share one of their first / rarest name words."""
    doc_freq = defaultdict(int)
    for it in items:
        for token in it.coreset:
            doc_freq[token] += 1
    buckets = defaultdict(list)
    for it in items:
        keys = set(it.core[:1])
        keys.update(sorted(it.coreset, key=lambda t: (doc_freq[t], t))[:2])
        for key in keys:
            buckets[key].append(it.idx)
    seen = set()
    for key, idxs in buckets.items():
        if len(idxs) > MAX_BUCKET:
            print(f"Skipping the very common word {key!r} ({len(idxs)} products) when looking for matches")
            continue
        for x in range(len(idxs)):
            for y in range(x + 1, len(idxs)):
                pair = (idxs[x], idxs[y])
                if pair not in seen:
                    seen.add(pair)
                    yield pair


def build_groups(items):
    same, different = load_overrides()
    groups = Groups(items, different)
    by_url = {it.url: it.idx for it in items if it.url}

    # 1) your manual "same" rulings
    for a, b in same:
        if a in by_url and b in by_url:
            groups.union(by_url[a], by_url[b], 1.0, "manual")

    # 2) identical barcodes
    by_barcode = defaultdict(list)
    for it in items:
        if it.barcode:
            by_barcode[it.barcode].append(it)
    for members in by_barcode.values():
        members.sort(key=lambda it: (it.shop, it.name))
        for first, other in zip(members, members[1:]):
            groups.union(first.idx, other.idx, 1.0, "barcode")
        for other in members[2:]:
            groups.union(members[0].idx, other.idx, 1.0, "barcode")

    # 3) name + size, best scores first
    edges = []
    for i, j in candidate_pairs(items):
        if frozenset((items[i].url, items[j].url)) in different:
            continue
        score = compare(items[i], items[j])
        if score is not None:
            edges.append((-score, i, j))
    edges.sort()
    for neg, i, j in edges:
        a, b = items[i], items[j]
        method = "name+size" if a.size_class != "none" else "name"
        groups.union(i, j, -neg, method)
    return groups


def group_id(members, methods):
    members = sorted(members, key=lambda it: (it.shop, it.name))
    barcodes = sorted({it.barcode for it in members if it.barcode})
    if "barcode" in methods and barcodes:
        key = "gtin:" + barcodes[0]
    else:
        first = members[0]
        key = " ".join(sorted(first.coreset)) + "|" + "|".join(sorted(first.attrs)) + "|" + \
            first.size_class + ":" + str(round(first.size_value))
    return "g" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def to_float(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return list(reader), reader.fieldnames or []


def write_csv(path, fieldnames, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore", restval="")
        writer.writeheader()
        writer.writerows(rows)


def main():
    if not os.path.exists(PRODUCTS_IN):
        print(f"{PRODUCTS_IN} not found; run export_csv.py first. Nothing was matched.")
        sys.exit(0)
    rows, fields = read_csv(PRODUCTS_IN)
    if not rows:
        print("No products to match.")
        sys.exit(0)

    items = [Item(i, row) for i, row in enumerate(rows)]
    groups = build_groups(items)

    # ---- assign ids / confidence to every row ----
    new_cols = ["product_group_id", "match_confidence", "match_method", "shops_in_group"]
    used_ids, by_group, url_to_group = set(), {}, {}
    for root, members in list(groups.members.items()):
        member_items = [items[i] for i in members]
        gid = group_id(member_items, groups.methods[root])
        base, n = gid, 2
        while gid in used_ids:                          # keep ids unique
            gid, n = f"{base}-{n}", n + 1
        used_ids.add(gid)
        multi = len(member_items) > 1
        methods = groups.methods[root]
        method = ("manual" if "manual" in methods else "barcode" if "barcode" in methods
                  else "name+size" if "name+size" in methods else "name") if multi else ""
        confidence = round(groups.min_score[root], 2) if multi else ""
        shops_n = len(groups.shops[root])
        by_group[gid] = (member_items, confidence, method)
        for it in member_items:
            it.row.update(product_group_id=gid, match_confidence=confidence,
                          match_method=method, shops_in_group=shops_n)
            if it.url:
                url_to_group[it.url] = (gid, confidence, method, shops_n)

    out_rows = sorted(rows, key=lambda r: (r["product_group_id"], r.get("shop") or r.get("site", "")))
    write_csv(MATCHED_PRODUCTS, list(fields) + new_cols, out_rows)

    # ---- history: look each old row up by its product link ----
    if os.path.exists(HISTORY_IN):
        hist_rows, hist_fields = read_csv(HISTORY_IN)
        for row in hist_rows:
            gid, conf, method, n = url_to_group.get(row.get("product_url", ""), ("", "", "", ""))
            row.update(product_group_id=gid, match_confidence=conf, match_method=method, shops_in_group=n)
        write_csv(MATCHED_HISTORY, list(hist_fields) + new_cols, hist_rows)

    # ---- price comparison (groups sold by 2+ shops) ----
    shop_names = sorted({it.shop for it in items})
    comparison = []
    for gid, (members, confidence, method) in by_group.items():
        if len(members) < 2:
            continue
        per_shop = {it.shop: it.row for it in members}
        priced = []
        for shop, row in per_shop.items():
            usd = to_float(row.get("price_usd"))
            if usd is None and row.get("currency") == "USD":
                usd = to_float(row.get("price"))
            if usd is not None:
                priced.append((usd, shop, row.get("availability", "")))
        buyable = [p for p in priced if p[2] != "Out of stock"] or priced
        record = {
            "product_group_id": gid,
            "product_name": sorted(members, key=lambda it: (it.shop, it.name))[0].name,
            "match_confidence": confidence, "match_method": method, "shops_count": len(members),
        }
        if len(buyable) >= 2:
            low, high = min(buyable), max(buyable)
            record.update(cheapest_shop=low[1], min_price_usd=round(low[0], 2),
                          max_price_usd=round(high[0], 2),
                          spread_pct=round((high[0] - low[0]) / low[0] * 100, 1) if low[0] else "")
        for shop in shop_names:
            row = per_shop.get(shop)
            record[f"price_usd__{shop}"] = (row.get("price_usd") or "") if row else ""
            record[f"availability__{shop}"] = (row.get("availability") or "") if row else ""
        comparison.append(record)
    comparison.sort(key=lambda r: (-(r.get("spread_pct") or 0), r["product_name"]))
    comp_fields = ["product_group_id", "product_name", "match_confidence", "match_method", "shops_count",
                   "cheapest_shop", "min_price_usd", "max_price_usd", "spread_pct"]
    comp_fields += [f"price_usd__{s}" for s in shop_names] + [f"availability__{s}" for s in shop_names]
    write_csv(COMPARISON, comp_fields, comparison)

    # ---- review list ----
    review = []
    for gid, (members, confidence, method) in by_group.items():
        if len(members) >= 2 and method in ("name+size", "name") and confidence != "" and confidence < REVIEW_BELOW:
            for it in sorted(members, key=lambda it: it.shop):
                review.append({"product_group_id": gid, "match_confidence": confidence, "match_method": method,
                               "shop": it.shop, "product_name": it.name, "price": it.row.get("price", ""),
                               "currency": it.row.get("currency", ""), "product_url": it.url})
    write_csv(REVIEW, ["product_group_id", "match_confidence", "match_method", "shop", "product_name",
                       "price", "currency", "product_url"], review)

    matched_groups = sum(1 for m, _, _ in by_group.values() if len(m) > 1)
    print(f"Matched {len(items)} rows into {len(by_group)} products; {matched_groups} are sold by 2+ shops; "
          f"{len({r['product_group_id'] for r in review})} group(s) listed for review")


if __name__ == "__main__":
    main()
