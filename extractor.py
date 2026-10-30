"""
extractor.py - Turns HTML into structured product records using a SiteConfig.

Fault-tolerance contract (the "automated fallback"):
  * Every field is extracted independently. A failing selector produces a logged
    FieldError and a None value for that field only; other fields and other products
    on the page are unaffected.
  * Each field can declare several fallback selectors / attributes; they are tried in order.
  * Failures are aggregated per page and field (not per product) so a layout change
    produces one clear log line instead of thousands.
  * A page where *every* item fails a field is logged at ERROR: that is the signal a
    site redesign broke the config.

Two modes:
  * mode "selectors": the site config supplies CSS selectors (the original behaviour).
  * mode "auto": no selectors needed. Reads schema.org Product data (JSON-LD) that many shops
    embed for Google, falling back to Open Graph product meta tags.
"""
from __future__ import annotations

import asyncio
import html as html_lib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from site_config import OPTIONAL_FIELDS, REQUIRED_FIELDS, SelectorSpec, SiteConfig

try:                                   # lxml is much faster; fall back gracefully
    import lxml  # noqa: F401
    _PARSER = "lxml"
except ImportError:
    _PARSER = "html.parser"

logger = logging.getLogger("scraper.extract")

ALL_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS
_CURRENCY_SYMBOLS = {"$": "USD", "£": "GBP", "€": "EUR", "¥": "JPY", "₹": "INR"}


class FieldExtractionError(Exception):
    """One field could not be extracted or normalised."""


@dataclass
class FieldError:
    site: str
    url: str
    field: str                 # field name, or "__item_container__" for a missing product container
    selector: str
    reason: str                # reason for the first failure seen on the page
    failed_count: int          # how many items failed this field on this page
    total_items: int


@dataclass
class ParseResult:
    products: list[dict] = field(default_factory=list)
    errors: list[FieldError] = field(default_factory=list)
    next_page_url: str | None = None
    follow_urls: list[str] = field(default_factory=list)   # product links found on listing pages (auto mode)


# --------------------------------------------------------------------------- #
# Normalisers: raw string -> clean value. Raise FieldExtractionError if unusable.
# --------------------------------------------------------------------------- #
def _norm_text(raw: str, base_url: str) -> str:
    return re.sub(r"\s+", " ", raw).strip()


def _norm_url(raw: str, base_url: str) -> str:
    candidate = raw.split()[0].rstrip(",")           # srcset: keep the first candidate URL
    return urljoin(base_url, candidate)


def _norm_price(raw: str, base_url: str) -> float:
    match = re.search(r"\d[\d.,]*", raw)
    if not match:
        raise FieldExtractionError(f"no digits in price text {raw!r}")
    token = match.group(0).rstrip(".,")
    if "," in token and "." in token:                # the right-most separator is the decimal mark
        decimal = "," if token.rfind(",") > token.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        token = token.replace(thousands, "").replace(decimal, ".")
    elif "," in token:
        if re.fullmatch(r"\d{1,3}(,\d{3})+", token):  # "1,299" -> thousands separator
            token = token.replace(",", "")
        else:                                         # "12,50" -> decimal comma
            token = token.replace(",", ".")
    elif token.count(".") > 1:                        # "1.299.99"
        head, _, tail = token.rpartition(".")
        token = head.replace(".", "") + "." + tail
    try:
        return round(float(token), 2)
    except ValueError as exc:
        raise FieldExtractionError(f"cannot parse price {raw!r}") from exc


_OUT_OF_STOCK = ("out of stock", "outofstock", "sold out", "unavailable", "not available",
                 "notify me", "currently unavailable", "soldout")
_IN_STOCK = ("in stock", "instock", "available", "add to cart", "add to basket",
             "add to bag", "buy now", "ships")


def _norm_availability(raw: str, base_url: str) -> bool:
    text = raw.lower()
    if any(p in text for p in _OUT_OF_STOCK):        # checked first: "unavailable" contains "available"
        return False
    if any(p in text for p in _IN_STOCK):
        return True
    raise FieldExtractionError(f"unrecognised availability text {raw[:60]!r}")


_NORMALISERS = {
    "product_name": _norm_text,
    "price": _norm_price,
    "sku": _norm_text,
    "availability": _norm_availability,
    "image_url": _norm_url,
    "product_url": _norm_url,
}


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
def _extract_raw(node, spec: SelectorSpec) -> str:
    """Try each selector (and attribute) in order; return the first non-empty raw string."""
    reason = "no selector matched"
    for css in spec.selectors:
        try:
            element = node.select_one(css)
        except Exception as exc:                      # invalid CSS must not crash the run
            reason = f"invalid selector {css!r}: {exc}"
            continue
        if element is None:
            continue

        value = None
        if spec.attrs:
            for attr in spec.attrs:
                candidate = element.get(attr)
                if isinstance(candidate, list):       # multi-valued attrs such as class
                    candidate = " ".join(candidate)
                if candidate and str(candidate).strip():
                    value = str(candidate).strip()
                    break
        else:
            value = element.get_text(" ", strip=True)

        if not value:
            reason = f"selector {css!r} matched but value was empty"
            continue

        if spec.regex:
            m = re.search(spec.regex, value)
            if not m:
                reason = f"regex {spec.regex!r} did not match {value[:60]!r}"
                continue
            value = m.group(1) if m.groups() else m.group(0)
        return value

    raise FieldExtractionError(reason)


def _extract_record(node, site: SiteConfig, page_url: str) -> tuple[dict, list[tuple[str, str, str]]]:
    """Extract all fields for one product node. Never raises for a bad field."""
    record: dict = {name: None for name in ALL_FIELDS}
    record["currency"] = site.currency
    failures: list[tuple[str, str, str]] = []         # (field, selector description, reason)

    for name in ALL_FIELDS:
        spec = site.fields.get(name)
        if spec is None:                              # site opted out of this field
            continue
        try:
            raw = _extract_raw(node, spec)
            if name == "price" and record["currency"] is None:
                record["currency"] = next(
                    (code for sym, code in _CURRENCY_SYMBOLS.items() if sym in raw), None
                )
            record[name] = _NORMALISERS[name](raw, page_url)
        except FieldExtractionError as exc:
            failures.append((name, " | ".join(spec.selectors), str(exc)))
        except Exception as exc:                      # defensive: unexpected bug in a normaliser
            failures.append((name, " | ".join(spec.selectors), f"unexpected {exc!r}"))
    return record, failures


def _dedupe_key(record: dict) -> str:
    """Stable identity for upserts: SKU, else product URL, else name, else image."""
    for key in ("sku", "product_url", "product_name", "image_url"):
        if record.get(key):
            return str(record[key])
    return ""


# --------------------------------------------------------------------------- #
# Mode "selectors": CSS rules from the site config
# --------------------------------------------------------------------------- #
@dataclass
class _Extraction:
    """What one mode extracted from a page, before the shared bookkeeping runs."""
    pairs: list[tuple[dict, list[tuple[str, str, str]]]] = field(default_factory=list)  # (record, failures)
    container_error: tuple[str, str, str] | None = None                                # (field, selector, reason)
    follow_urls: list[str] = field(default_factory=list)


def _extract_with_selectors(soup: BeautifulSoup, site: SiteConfig, page_url: str) -> _Extraction:
    out = _Extraction()
    if site.item_selector:
        try:
            nodes = soup.select(site.item_selector)
            reason = "no product containers found (layout change, empty page, or soft block?)"
        except Exception as exc:
            nodes, reason = [], f"invalid item_selector: {exc}"
        if not nodes:
            out.container_error = ("__item_container__", site.item_selector, reason)
    else:
        nodes = [soup]                                # whole page is a single product page

    for node in nodes:
        try:
            out.pairs.append(_extract_record(node, site, page_url))
        except Exception:                             # one bad card must not kill the page
            logger.exception("[%s] unexpected error parsing an item on %s", site.name, page_url)
    return out


# --------------------------------------------------------------------------- #
# Mode "auto": schema.org JSON-LD (+ Open Graph fallback), no selectors needed
# --------------------------------------------------------------------------- #
_IN_STOCK_SCHEMA = {"instock", "limitedavailability", "onlineonly", "instoreonly"}
_OUT_OF_STOCK_SCHEMA = {"outofstock", "soldout", "discontinued"}
_AUTO_LABEL = "schema.org JSON-LD / meta tags"
_MAX_FOLLOW_PER_PAGE = 60


def _clean(value) -> str | None:
    """Make any JSON value into a tidy single-line string (or None)."""
    if isinstance(value, list):
        value = next((v for v in value if v not in (None, "")), None)
    if value is None or isinstance(value, (dict, bool)):
        return None
    text = re.sub(r"\s+", " ", html_lib.unescape(str(value))).strip()
    return text or None


def _schema_types(node: dict) -> list[str]:
    raw = node.get("@type")
    items = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    return [t.rsplit("/", 1)[-1] for t in items if isinstance(t, str)]


def _walk_jsonld(node):
    """Yield every dict in a JSON-LD tree, but do not descend into Product nodes
    (their children are usually 'related products', not products on this page)."""
    if isinstance(node, dict):
        yield node
        if "Product" in _schema_types(node):
            return
        for value in node.values():
            yield from _walk_jsonld(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_jsonld(value)


def _jsonld_nodes(soup: BeautifulSoup):
    for tag in soup.find_all("script", type="application/ld+json"):
        text = tag.string or tag.get_text()
        if not text or not text.strip():
            continue
        try:
            data = json.loads(text, strict=False)
        except ValueError:
            logger.debug("skipping unparseable JSON-LD block")
            continue
        yield from _walk_jsonld(data)


def _first_offer(offers) -> dict:
    if isinstance(offers, list):
        offers = next((o for o in offers if isinstance(o, dict)), None)
    return offers if isinstance(offers, dict) else {}


def _offer_price(offer: dict):
    for key in ("price", "lowPrice"):
        if offer.get(key) not in (None, ""):
            return offer[key]
    spec = offer.get("priceSpecification")
    if isinstance(spec, list):
        spec = next((x for x in spec if isinstance(x, dict) and x.get("price") not in (None, "")), None)
    if isinstance(spec, dict) and spec.get("price") not in (None, ""):
        return spec["price"]
    return None


def _first_image(value) -> str | None:
    if isinstance(value, list):
        for item in value:
            found = _first_image(item)
            if found:
                return found
        return None
    if isinstance(value, dict):
        return _clean(value.get("url") or value.get("contentUrl"))
    return _clean(value)


def _availability_from_schema(value) -> bool | None:
    text = _clean(value)
    if not text:
        return None
    tail = text.rsplit("/", 1)[-1].strip().lower()
    if tail in _IN_STOCK_SCHEMA:
        return True
    if tail in _OUT_OF_STOCK_SCHEMA:
        return False
    try:
        return _norm_availability(text, "")
    except FieldExtractionError:
        return None


def _record_from_product(product: dict, page_url: str, site: SiteConfig,
                         fallback_url: str | None) -> tuple[dict, list[tuple[str, str, str]]]:
    record: dict = {name: None for name in ALL_FIELDS}
    offer = _first_offer(product.get("offers"))

    record["product_name"] = _clean(product.get("name"))
    for key in ("sku", "mpn", "productID", "gtin13", "gtin12", "gtin14", "gtin"):
        record["sku"] = _clean(product.get(key))
        if record["sku"]:
            break

    image = _first_image(product.get("image"))
    record["image_url"] = urljoin(page_url, image) if image else None
    link = _clean(product.get("url")) or _clean(offer.get("url")) or fallback_url
    record["product_url"] = urljoin(page_url, link) if link else None

    raw_price = _offer_price(offer)
    if raw_price is not None:
        try:
            record["price"] = _norm_price(str(raw_price), page_url)
        except FieldExtractionError:
            pass
    record["currency"] = _clean(offer.get("priceCurrency")) or site.currency
    record["availability"] = _availability_from_schema(offer.get("availability"))

    failures = [(name, _AUTO_LABEL, "not present in the page's structured data")
                for name in REQUIRED_FIELDS if record[name] is None]
    return record, failures


def _meta_content(soup: BeautifulSoup, *names: str) -> str | None:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content") and str(tag["content"]).strip():
            return str(tag["content"]).strip()
    return None


def _record_from_meta(soup: BeautifulSoup, page_url: str, site: SiteConfig):
    """Open Graph / product meta tags: used only when a page has no JSON-LD Product."""
    amount = _meta_content(soup, "product:price:amount", "og:price:amount")
    if not amount:
        return None
    record: dict = {name: None for name in ALL_FIELDS}
    record["product_name"] = _clean(_meta_content(soup, "og:title", "twitter:title"))
    record["sku"] = _clean(_meta_content(soup, "product:retailer_item_id", "product:sku"))
    image = _meta_content(soup, "og:image")
    record["image_url"] = urljoin(page_url, image) if image else None
    record["product_url"] = urljoin(page_url, _meta_content(soup, "og:url") or page_url)
    try:
        record["price"] = _norm_price(amount, page_url)
    except FieldExtractionError:
        pass
    record["currency"] = _clean(_meta_content(soup, "product:price:currency", "og:price:currency")) or site.currency
    availability = _meta_content(soup, "product:availability", "og:availability")
    if availability:
        try:
            record["availability"] = _norm_availability(availability, page_url)
        except FieldExtractionError:
            pass
    failures = [(name, _AUTO_LABEL, "not present in the page's structured data")
                for name in REQUIRED_FIELDS if record[name] is None]
    return record, failures


def _extract_auto(soup: BeautifulSoup, site: SiteConfig, page_url: str) -> _Extraction:
    out = _Extraction()
    products: list[dict] = []
    seen_ids: set[str] = set()
    listed_urls: list[str] = []

    for node in _jsonld_nodes(soup):
        types = _schema_types(node)
        if "Product" in types:
            identity = _clean(node.get("sku")) or _clean(node.get("url")) or _clean(node.get("name")) or str(id(node))
            if identity not in seen_ids:
                seen_ids.add(identity)
                products.append(node)
        elif "ItemList" in types:                     # listing page: remember the product links it points to
            for element in node.get("itemListElement") or []:
                if not isinstance(element, dict):
                    continue
                item = element.get("item", element)
                if isinstance(item, dict) and "Product" in _schema_types(item):
                    continue                          # full product data is captured above
                target = item if isinstance(item, str) else (item.get("url") or item.get("@id")) if isinstance(item, dict) else None
                target = target or element.get("url")
                if isinstance(target, str) and target.strip():
                    listed_urls.append(urljoin(page_url, target.strip()))

    fallback_url = page_url if len(products) == 1 else None
    for product in products:
        out.pairs.append(_record_from_product(product, page_url, site, fallback_url))

    if not out.pairs:
        meta = _record_from_meta(soup, page_url, site)
        if meta:
            out.pairs.append(meta)

    if listed_urls and not out.pairs:
        out.follow_urls = list(dict.fromkeys(listed_urls))[:_MAX_FOLLOW_PER_PAGE]
    if not out.pairs and not out.follow_urls:
        out.container_error = ("__structured_data__", _AUTO_LABEL,
                               "no product data found (not a product page, or this site needs manual selectors)")
    return out


# --------------------------------------------------------------------------- #
# Shared page-level logic
# --------------------------------------------------------------------------- #
def parse_page(html: str, page_url: str, site: SiteConfig) -> ParseResult:
    """Parse one page into products + aggregated field errors + next-page URL. Synchronous."""
    soup = BeautifulSoup(html, _PARSER)
    result = ParseResult()

    extraction = (_extract_auto(soup, site, page_url) if site.mode == "auto"
                  else _extract_with_selectors(soup, site, page_url))

    if extraction.container_error:
        fname, selector, reason = extraction.container_error
        logger.error("[%s] %s on %s", site.name, reason, page_url)
        result.errors.append(FieldError(site.name, page_url, fname, selector, reason, 1, 0))

    aggregated: dict[str, FieldError] = {}
    total = len(extraction.pairs)
    scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for record, failures in extraction.pairs:
        for fname, selector, reason in failures:
            existing = aggregated.get(fname)
            if existing is None:
                aggregated[fname] = FieldError(site.name, page_url, fname, selector, reason, 1, total)
            else:
                existing.failed_count += 1

        if all(record.get(f) is None for f in REQUIRED_FIELDS):
            continue                                  # nothing usable extracted from this node
        record.update(site=site.name, source_url=page_url, scraped_at=scraped_at,
                      dedupe_key=_dedupe_key(record))
        result.products.append(record)

    for err in aggregated.values():
        level = logging.ERROR if err.failed_count == err.total_items else logging.WARNING
        logger.log(
            level, "[%s] field '%s' failed for %d/%d items on %s (selector: %s; e.g. %s)",
            err.site, err.field, err.failed_count, err.total_items, err.url, err.selector, err.reason,
        )
    result.errors.extend(aggregated.values())
    result.next_page_url = _find_next_page(soup, site, page_url)
    result.follow_urls = extraction.follow_urls
    return result


def _find_next_page(soup: BeautifulSoup, site: SiteConfig, page_url: str) -> str | None:
    selector = site.next_page_selector
    if not selector and site.mode == "auto":
        selector = "a[rel~='next'], link[rel='next']"   # standard "next page" markers
    if not selector:
        return None
    try:
        element = soup.select_one(selector)
    except Exception:
        logger.error("[%s] invalid next_page_selector %r", site.name, selector)
        return None
    href = element.get("href") if element else None
    return urljoin(page_url, href) if href else None


async def parse_page_async(html: str, page_url: str, site: SiteConfig) -> ParseResult:
    """Parse in a worker thread so large pages never block the event loop."""
    return await asyncio.to_thread(parse_page, html, page_url, site)
