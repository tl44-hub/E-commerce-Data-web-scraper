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
"""
from __future__ import annotations

import asyncio
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


def parse_page(html: str, page_url: str, site: SiteConfig) -> ParseResult:
    """Parse one page into products + aggregated field errors + next-page URL. Synchronous."""
    soup = BeautifulSoup(html, _PARSER)
    result = ParseResult()

    if site.item_selector:
        try:
            nodes = soup.select(site.item_selector)
            container_reason = "no product containers found (layout change, empty page, or soft block?)"
        except Exception as exc:
            nodes, container_reason = [], f"invalid item_selector: {exc}"
        if not nodes:
            logger.error("[%s] %s on %s", site.name, container_reason, page_url)
            result.errors.append(FieldError(site.name, page_url, "__item_container__",
                                            site.item_selector, container_reason, 1, 0))
    else:
        nodes = [soup]                                # whole page is a single product page

    aggregated: dict[str, FieldError] = {}
    scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for node in nodes:
        try:
            record, failures = _extract_record(node, site, page_url)
        except Exception:                             # one bad card must not kill the page
            logger.exception("[%s] unexpected error parsing an item on %s", site.name, page_url)
            continue

        for fname, selector, reason in failures:
            existing = aggregated.get(fname)
            if existing is None:
                aggregated[fname] = FieldError(site.name, page_url, fname, selector, reason, 1, len(nodes))
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
    return result


def _find_next_page(soup: BeautifulSoup, site: SiteConfig, page_url: str) -> str | None:
    if not site.next_page_selector:
        return None
    try:
        element = soup.select_one(site.next_page_selector)
    except Exception:
        logger.error("[%s] invalid next_page_selector %r", site.name, site.next_page_selector)
        return None
    href = element.get("href") if element else None
    return urljoin(page_url, href) if href else None


async def parse_page_async(html: str, page_url: str, site: SiteConfig) -> ParseResult:
    """Parse in a worker thread so large pages never block the event loop."""
    return await asyncio.to_thread(parse_page, html, page_url, site)
