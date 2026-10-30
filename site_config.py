"""
site_config.py - Site configuration schema, validation, loader and SAMPLE configs.

The core engine knows nothing about individual shops. Everything site-specific
lives in a plain dict / JSON document that is validated once at start-up into
immutable ``SiteConfig`` objects. One broken site definition is logged and
skipped; it never prevents the other 99+ sites from running.

Selector spec (per field) can be written as:
    "p.price"                                      # CSS string, element text
    {"selector": "img.main", "attr": "src"}        # read an attribute instead of text
    {"selector": ["h1.title", "h1"],               # primary + fallback selectors,
     "attr": ["data-src", "src"],                  # attributes tried in order,
     "regex": r"([^/]+)/index\\.html$"}             # optional regex (group 1 wins)
Use ``null`` for a field the site genuinely does not expose.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("scraper.config")

# The five fields every site config must declare (value may be null to opt out).
REQUIRED_FIELDS = ("product_name", "price", "sku", "availability", "image_url")
# Extra fields that may be declared but are not required.
OPTIONAL_FIELDS = ("product_url",)


class ConfigError(ValueError):
    """Raised when a single site definition is invalid."""


@dataclass(frozen=True)
class SelectorSpec:
    """How to pull one field out of one product node."""

    selectors: tuple[str, ...]          # primary selector first, then fallbacks
    attrs: tuple[str, ...] = ()         # empty => use element text
    regex: str | None = None            # optional post-extraction regex

    @classmethod
    def from_raw(cls, field_name: str, raw: Any) -> "SelectorSpec":
        if isinstance(raw, str):
            raw = {"selector": raw}
        if not isinstance(raw, dict) or "selector" not in raw:
            raise ConfigError(
                f"field '{field_name}': expected a CSS string or a dict with a 'selector' key"
            )

        sel = raw["selector"]
        selectors = (sel,) if isinstance(sel, str) else tuple(sel)
        if not selectors or not all(isinstance(s, str) and s.strip() for s in selectors):
            raise ConfigError(f"field '{field_name}': 'selector' must be non-empty string(s)")

        attr = raw.get("attr")
        attrs = () if attr is None else ((attr,) if isinstance(attr, str) else tuple(attr))

        regex = raw.get("regex")
        if regex is not None:
            try:
                re.compile(regex)
            except re.error as exc:
                raise ConfigError(f"field '{field_name}': invalid regex: {exc}") from exc

        return cls(selectors=selectors, attrs=attrs, regex=regex)


@dataclass(frozen=True, eq=False)
class SiteConfig:
    """Validated, immutable description of one e-commerce site."""

    name: str
    domain: str                                   # e.g. "books.toscrape.com" (no scheme / www)
    start_urls: tuple[str, ...]
    fields: dict[str, SelectorSpec | None]        # field name -> spec (None = opted out)

    # --- page structure ---------------------------------------------------
    item_selector: str | None = None              # one match per product card; None = whole page is one product
    next_page_selector: str | None = None         # <a> to follow for pagination
    max_pages: int = 1                            # pagination depth per start URL
    currency: str | None = None                   # force currency code; otherwise inferred from symbol

    # --- engine routing ---------------------------------------------------
    render_javascript: bool = False               # False => httpx+bs4, True => Playwright
    use_proxy: bool = True                        # route through the managed proxy service

    # --- JS-engine options (ignored when render_javascript is False) -------
    wait_for_selector: str | None = None          # wait until this exists in the DOM (hidden elements OK)
    scroll_passes: int = 0                        # infinite scroll: max scroll-to-bottom iterations
    scroll_pause_ms: int = 1000                   # pause after each scroll
    click_selectors: tuple[str, ...] = ()         # e.g. cookie banners / "load more" buttons (best effort)
    block_resources: bool = True                  # skip images/fonts/media to save proxy bandwidth

    # --- politeness -------------------------------------------------------
    max_concurrent_requests: int = 2              # per-domain concurrency cap
    min_delay_seconds: float = 1.0                # min average gap between requests to this domain

    enabled: bool = True

    def owns_url(self, url: str) -> bool:
        """True if ``url`` belongs to this site's domain (used to keep pagination on-site)."""
        host = (urlparse(url).hostname or "").lower()
        return host == self.domain or host.endswith("." + self.domain)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SiteConfig":
        if not isinstance(raw, dict):
            raise ConfigError("site definition must be an object/dict")

        name = str(raw.get("name") or "").strip()
        domain = str(raw.get("domain") or "").strip().lower().removeprefix("www.")
        if not name or not domain:
            raise ConfigError("'name' and 'domain' are required")

        start_urls = tuple(raw.get("start_urls") or ())
        if not start_urls:
            raise ConfigError("'start_urls' must contain at least one URL")

        raw_selectors = raw.get("selectors")
        if not isinstance(raw_selectors, dict):
            raise ConfigError("missing 'selectors' block")
        missing = [f for f in REQUIRED_FIELDS if f not in raw_selectors]
        if missing:
            raise ConfigError(f"'selectors' is missing keys {missing} (use null to opt out of a field)")

        fields: dict[str, SelectorSpec | None] = {}
        for key in REQUIRED_FIELDS + OPTIONAL_FIELDS:
            spec_raw = raw_selectors.get(key)
            fields[key] = None if spec_raw is None else SelectorSpec.from_raw(key, spec_raw)

        site = cls(
            name=name,
            domain=domain,
            start_urls=start_urls,
            fields=fields,
            item_selector=raw.get("item_selector"),
            next_page_selector=raw.get("next_page_selector"),
            max_pages=max(1, int(raw.get("max_pages", 1))),
            currency=raw.get("currency"),
            render_javascript=bool(raw.get("render_javascript", False)),
            use_proxy=bool(raw.get("use_proxy", True)),
            wait_for_selector=raw.get("wait_for_selector"),
            scroll_passes=max(0, int(raw.get("scroll_passes", 0))),
            scroll_pause_ms=max(0, int(raw.get("scroll_pause_ms", 1000))),
            click_selectors=tuple(raw.get("click_selectors") or ()),
            block_resources=bool(raw.get("block_resources", True)),
            max_concurrent_requests=max(1, int(raw.get("max_concurrent_requests", 2))),
            min_delay_seconds=max(0.0, float(raw.get("min_delay_seconds", 1.0))),
            enabled=bool(raw.get("enabled", True)),
        )

        for url in site.start_urls:
            if not site.owns_url(url):
                raise ConfigError(f"start URL {url!r} is not on domain {site.domain!r}")
        return site


def load_site_configs(source: str | Path | list[dict[str, Any]]) -> list[SiteConfig]:
    """
    Load and validate site configs from a JSON file (a list, or {"sites": [...]})
    or directly from a list of dicts. Invalid / disabled sites are skipped with a log line.
    """
    if isinstance(source, (str, Path)):
        data = json.loads(Path(source).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("sites", [])
    else:
        data = source

    sites: list[SiteConfig] = []
    seen: set[str] = set()
    for raw in data:
        label = raw.get("name", "<unnamed>") if isinstance(raw, dict) else "<invalid>"
        try:
            site = SiteConfig.from_dict(raw)
        except (ConfigError, KeyError, TypeError, ValueError) as exc:
            logger.error("Skipping invalid site config %s: %s", label, exc)
            continue
        if not site.enabled:
            logger.info("Site %s is disabled; skipping", site.name)
            continue
        if site.name in seen:
            logger.error("Duplicate site name %s; skipping the second definition", site.name)
            continue
        seen.add(site.name)
        sites.append(site)

    logger.info("Loaded %d site config(s)", len(sites))
    return sites


# ---------------------------------------------------------------------------
# SAMPLE E-COMMERCE CONFIGURATION
# JSON-compatible (True/False/None map to true/false/null), so this list can be
# dumped to sites.json unchanged and loaded with load_site_configs("sites.json").
# ---------------------------------------------------------------------------
SAMPLE_SITE_CONFIGS: list[dict[str, Any]] = [
    # --- 1. Static site (httpx + BeautifulSoup) ---------------------------------
    # books.toscrape.com is a public scraping sandbox, so it needs no proxy.
    {
        "name": "books_toscrape",
        "domain": "books.toscrape.com",
        "start_urls": ["https://books.toscrape.com/catalogue/page-1.html"],
        "render_javascript": False,
        "use_proxy": False,
        "item_selector": "article.product_pod",       # one match per product card
        "next_page_selector": "li.next > a",
        "max_pages": 3,
        "currency": "GBP",
        "max_concurrent_requests": 2,
        "min_delay_seconds": 0.5,
        "selectors": {                                # all selectors are relative to the card
            "product_name": {"selector": "h3 > a", "attr": "title"},
            # primary selector + fallback: if the layout changes the engine tries the next one
            "price": {"selector": ["p.price_color", ".price"]},
            # no SKU on listing cards, so derive one from the product URL slug via regex
            "sku": {"selector": "h3 > a", "attr": "href", "regex": r"([^/]+)/index\.html$"},
            "availability": "p.instock.availability",
            "image_url": {"selector": "img.thumbnail", "attr": ["data-src", "src"]},
            "product_url": {"selector": "h3 > a", "attr": "href"},
        },
    },
        # --- Practice laptop store (static) -------------------------------------
    {
        "name": "webscraper_test_store",
        "domain": "webscraper.io",
        "start_urls": [
            "https://webscraper.io/test-sites/e-commerce/static/computers/laptops"
        ],
        "render_javascript": False,
        "use_proxy": False,
        "item_selector": "div.thumbnail",
        "next_page_selector": "ul.pagination a[rel='next']",
        "max_pages": 3,
        "currency": "USD",
        "min_delay_seconds": 1.0,
        "selectors": {
            "product_name": {"selector": "a.title", "attr": "title"},
            "price": {"selector": ["h4.price", ".price"]},
            "sku": {"selector": "a.title", "attr": "href", "regex": "/product/(\\d+)"},
            "availability": None,
            "image_url": {"selector": "img.img-responsive", "attr": "src"},
            "product_url": {"selector": "a.title", "attr": "href"},
        },
    },
    # --- 2. JS-heavy site template (Playwright) ----------------------------------
    # Disabled placeholder showing the JS-engine options; copy and adapt it.
    {
        "name": "example_js_shop",
        "domain": "shop.example.com",
        "enabled": False,
        "start_urls": ["https://shop.example.com/collections/all"],
        "render_javascript": True,
        "use_proxy": True,
        "wait_for_selector": "div.product-card",      # wait for client-side render
        "scroll_passes": 10,                          # infinite scroll depth
        "scroll_pause_ms": 1200,
        "click_selectors": ["button#accept-cookies"], # best-effort clicks before scraping
        "item_selector": "div.product-card",
        "max_concurrent_requests": 1,
        "min_delay_seconds": 2.0,
        "selectors": {
            "product_name": "h2.product-title",
            "price": {"selector": ["span.price--sale", "span.price"]},
            "sku": {"selector": "div.product-card", "attr": "data-sku"},
            "availability": {"selector": "button.add-to-cart", "attr": ["data-availability", "aria-label"]},
            "image_url": {"selector": "img.product-image", "attr": ["data-src", "src", "srcset"]},
            "product_url": {"selector": "a.product-link", "attr": "href"},
        },
    },
]
