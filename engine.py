"""
engine.py - Core scraping engine (site-agnostic).

Architecture: a bounded pool of worker tasks pulls CrawlTasks from one asyncio.Queue.
Each worker: fetch (RequestHandler) -> parse (extractor) -> persist (SQLiteSink) -> enqueue
the next page. Concurrency is capped at three levels (see RequestHandler): per domain,
global, and headless-browser pages. A worker that hits an error logs it and moves on to
the next task, so one failing page or site can never crash the run.
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from itertools import zip_longest

from extractor import parse_page_async
from request_handler import FetchError, RequestHandler
from site_config import SiteConfig
from storage import SQLiteSink

logger = logging.getLogger("scraper.engine")


@dataclass(frozen=True)
class CrawlTask:
    site: SiteConfig
    url: str
    page_no: int = 1


class ScraperEngine:
    def __init__(
        self,
        sites: list[SiteConfig],
        handler: RequestHandler,
        sink: SQLiteSink,
        num_workers: int = 20,
    ):
        self.sites = sites
        self.handler = handler
        self.sink = sink
        self.num_workers = num_workers
        self.stats: Counter[str] = Counter()
        self._queue: asyncio.Queue[CrawlTask] = asyncio.Queue()
        self._seen: set[tuple[str, str]] = set()      # (site name, url) already queued

    # -- queue management --------------------------------------------------
    def _enqueue(self, task: CrawlTask) -> bool:
        key = (task.site.name, task.url)
        if key in self._seen:
            return False
        self._seen.add(key)
        self._queue.put_nowait(task)
        return True

    def _seed(self) -> None:
        """
        Interleave start URLs round-robin across sites. A plain site-by-site order would let
        workers pile up waiting on one domain's semaphore while other domains sit idle.
        """
        per_site = [[CrawlTask(site, url) for url in site.start_urls] for site in self.sites]
        for group in zip_longest(*per_site):
            for task in group:
                if task is not None:
                    self._enqueue(task)

    # -- run ----------------------------------------------------------------
    async def run(self) -> Counter[str]:
        self._seed()
        logger.info("Starting: %d site(s), %d seed URL(s), %d worker(s)",
                    len(self.sites), self._queue.qsize(), self.num_workers)

        workers = [asyncio.create_task(self._worker(i), name=f"worker-{i}")
                   for i in range(self.num_workers)]
        try:
            await self._queue.join()                  # finishes when every queued page is processed
        finally:                                      # also runs on Ctrl-C / cancellation
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            logger.info("Finished. Stats: %s", dict(self.stats))
        return self.stats

    async def _worker(self, worker_id: int) -> None:
        while True:
            task = await self._queue.get()
            try:
                await self._process(task)
            except asyncio.CancelledError:
                raise
            except Exception:                         # last line of defence: log and keep going
                self.stats["tasks_crashed"] += 1
                logger.exception("[%s] unexpected error processing %s", task.site.name, task.url)
            finally:
                self._queue.task_done()

    async def _process(self, task: CrawlTask) -> None:
        site = task.site
        try:
            fetched = await self.handler.fetch(task.url, site)
        except FetchError as exc:
            self.stats["pages_failed"] += 1
            logger.error("[%s] %s", site.name, exc)
            return

        parsed = await parse_page_async(fetched.html, task.url, site)
        await self.sink.save_page(parsed.products, parsed.errors)   # committed immediately

        self.stats["pages_ok"] += 1
        self.stats["products_saved"] += len(parsed.products)
        self.stats["field_error_groups"] += len(parsed.errors)
        logger.info("[%s] page %d (%s): %d product(s), %d field issue(s) - %s",
                    site.name, task.page_no, fetched.engine,
                    len(parsed.products), len(parsed.errors), task.url)

        nxt = parsed.next_page_url
        if nxt and task.page_no < site.max_pages and site.owns_url(nxt):
            self._enqueue(CrawlTask(site, nxt, task.page_no + 1))
