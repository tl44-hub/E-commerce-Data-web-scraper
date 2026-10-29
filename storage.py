"""
storage.py - Incremental, crash-safe persistence with aiosqlite.

Every parsed page is committed immediately, so an interrupted run (Ctrl-C, OOM kill,
power loss) keeps everything scraped so far. WAL mode makes commits cheap.

Re-scrapes upsert on (site, dedupe_key). COALESCE keeps the previously stored value when a
field comes back NULL, so a broken selector never wipes good historical data.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Iterable

import aiosqlite

from extractor import FieldError

logger = logging.getLogger("scraper.storage")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    site          TEXT NOT NULL,
    dedupe_key    TEXT NOT NULL,
    product_name  TEXT,
    price         REAL,
    currency      TEXT,
    sku           TEXT,
    availability  INTEGER,          -- 1 in stock, 0 out of stock, NULL unknown
    image_url     TEXT,
    product_url   TEXT,
    source_url    TEXT,
    scraped_at    TEXT,
    UNIQUE (site, dedupe_key)
);
CREATE TABLE IF NOT EXISTS field_errors (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    site          TEXT NOT NULL,
    url           TEXT NOT NULL,
    field         TEXT NOT NULL,
    selector      TEXT,
    reason        TEXT,
    failed_count  INTEGER,
    total_items   INTEGER,
    logged_at     TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_errors_site_field ON field_errors (site, field);
"""

_COLUMNS = ("site", "dedupe_key", "product_name", "price", "currency", "sku",
            "availability", "image_url", "product_url", "source_url", "scraped_at")
_PRESERVE_ON_NULL = ("product_name", "price", "currency", "sku", "availability",
                     "image_url", "product_url")

_UPSERT_SQL = (
    f"INSERT INTO products ({', '.join(_COLUMNS)}) "
    f"VALUES ({', '.join(':' + c for c in _COLUMNS)}) "
    "ON CONFLICT(site, dedupe_key) DO UPDATE SET "
    + ", ".join(f"{c} = COALESCE(excluded.{c}, products.{c})" for c in _PRESERVE_ON_NULL)
    + ", source_url = excluded.source_url, scraped_at = excluded.scraped_at"
)

_ERROR_SQL = (
    "INSERT INTO field_errors (site, url, field, selector, reason, failed_count, total_items) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)


class SQLiteSink:
    """Async context manager wrapping one SQLite connection."""

    def __init__(self, path: str = "products.db"):
        self.path = path
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()           # keeps each page's batch + commit atomic

    async def __aenter__(self) -> "SQLiteSink":
        self._db = await aiosqlite.connect(self.path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._db:
            await self._db.commit()
            await self._db.close()

    async def save_page(self, products: Iterable[dict], errors: Iterable[FieldError]) -> None:
        """Persist one page's products and field errors in a single committed transaction."""
        assert self._db is not None, "use SQLiteSink as an async context manager"
        rows = [{c: p.get(c) for c in _COLUMNS} for p in products if p.get("dedupe_key")]
        error_rows = [(e.site, e.url, e.field, e.selector, e.reason, e.failed_count, e.total_items)
                      for e in errors]
        if not rows and not error_rows:
            return
        async with self._lock:
            if rows:
                await self._db.executemany(_UPSERT_SQL, rows)
            if error_rows:
                await self._db.executemany(_ERROR_SQL, error_rows)
            await self._db.commit()           # committed per page => safe on interrupt
