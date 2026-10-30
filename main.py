"""
main.py - Entry point: wires config -> request handler -> storage -> engine.

Usage:
    export SCRAPER_API_KEY="your-key"                      # managed proxy key (optional for the demo site)
    export PROXY_URL_TEMPLATE="http://api.scraperapi.com/?api_key={api_key}&url={url}"
    python main.py                                         # runs SAMPLE_SITE_CONFIGS + every URL in sites.txt
    python main.py --config sites.json --db products.db --workers 20 --max-concurrency 20

Optional proxy-mode settings for JS sites (Playwright):
    PROXY_SERVER, PROXY_USERNAME, PROXY_PASSWORD
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os

from engine import ScraperEngine
from request_handler import ProxySettings, RequestHandler, RetryPolicy
from site_config import SAMPLE_SITE_CONFIGS, load_site_configs, load_sites_txt
from storage import SQLiteSink

logger = logging.getLogger("scraper.main")


def proxy_settings_from_env() -> ProxySettings:
    api_key = os.getenv("SCRAPER_API_KEY", "")
    template = os.getenv("PROXY_URL_TEMPLATE", "http://api.scraperapi.com/?api_key={api_key}&url={url}")
    if not api_key:
        logger.warning("SCRAPER_API_KEY not set: proxied sites will be requested directly")
        template = ""                                  # empty template => no API-mode proxying

    browser_proxy = None
    if os.getenv("PROXY_SERVER"):
        browser_proxy = {
            "server": os.environ["PROXY_SERVER"],
            "username": os.getenv("PROXY_USERNAME", ""),
            "password": os.getenv("PROXY_PASSWORD", ""),
        }
    return ProxySettings(api_url_template=template, api_key=api_key, browser_proxy=browser_proxy)


async def run(args: argparse.Namespace) -> None:
    sites = load_site_configs(args.config if args.config else SAMPLE_SITE_CONFIGS)

    # Sites listed as plain URLs in sites.txt are read automatically (no selectors needed).
    # A site configured by hand above always wins over a sites.txt entry for the same domain.
    manual_domains = {site.domain for site in sites}
    for auto_site in load_sites_txt(args.sites_file):
        if auto_site.domain in manual_domains:
            logger.info("%s is configured by hand; ignoring its sites.txt entries", auto_site.domain)
        else:
            sites.append(auto_site)

    if not sites:
        logger.error("No valid, enabled site configs to run")
        return

    # `async with` guarantees HTTP client, browser and DB are closed (and committed)
    # even when the run is interrupted.
    async with RequestHandler(
        proxy=proxy_settings_from_env(),
        retry=RetryPolicy(max_attempts=args.max_attempts),
        max_concurrency=args.max_concurrency,
        max_browser_pages=args.max_browser_pages,
    ) as handler, SQLiteSink(args.db) as sink:
        engine = ScraperEngine(sites, handler, sink, num_workers=args.workers)
        await engine.run()


def main() -> None:
    parser = argparse.ArgumentParser(description="Config-driven product scraper")
    parser.add_argument("--config", help="Path to a JSON file of site configs (default: built-in samples)")
    parser.add_argument("--sites-file", default="sites.txt", help="Plain list of URLs to read automatically")
    parser.add_argument("--db", default="products.db", help="SQLite output path")
    parser.add_argument("--workers", type=int, default=20, help="Worker tasks pulling from the queue")
    parser.add_argument("--max-concurrency", type=int, default=20, help="Global in-flight request cap")
    parser.add_argument("--max-browser-pages", type=int, default=3, help="Concurrent headless pages")
    parser.add_argument("--max-attempts", type=int, default=6, help="Attempts per URL before giving up")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)   # silence per-request INFO lines

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        logger.warning("Interrupted: pages processed so far are already saved in %s", args.db)


if __name__ == "__main__":
    main()
