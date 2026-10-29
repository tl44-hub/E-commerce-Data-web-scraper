"""
request_handler.py - Centralised request management.

Responsibilities
  * Build proxied URLs from a placeholder template (ScraperAPI / Bright Data / Crawlbase style)
  * Rotate User-Agents and send realistic Accept / Accept-Language headers
  * Adaptive retries: exponential backoff + jitter for 429 / 503 / 403 (and soft CAPTCHA pages)
  * Route to the right engine: httpx (static) or Playwright (JS-heavy, stealth)
  * Enforce concurrency: per-domain semaphore, global semaphore, and a smaller browser-page semaphore

Nothing in here knows how to parse a product page; it only returns HTML.
Requires Python 3.10+.
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from site_config import SiteConfig

logger = logging.getLogger("scraper.request")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
# Statuses worth retrying. 429/503/403 are the block signals you asked for;
# 500/502/504 are common transient errors returned by proxy gateways.
RETRYABLE_STATUS_CODES = frozenset({429, 503, 403, 500, 502, 504})

# Refresh these periodically so they track current browser releases.
CHROME_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
]
OTHER_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:143.0) Gecko/20100101 Firefox/143.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:143.0) Gecko/20100101 Firefox/143.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
]
# httpx can use any of them; Playwright launches Chromium, so it only uses Chrome UAs
# (a Firefox UA on a Chromium engine is an easy fingerprint mismatch to spot).
ALL_USER_AGENTS = CHROME_USER_AGENTS + OTHER_USER_AGENTS

ACCEPT_LANGUAGES = ["en-US,en;q=0.9", "en-GB,en;q=0.9", "en-US,en;q=0.8,es;q=0.5", "en-CA,en;q=0.9,fr;q=0.6"]

# Phrases that indicate a "soft block" (HTTP 200 serving a challenge page).
BLOCK_MARKERS = (
    "captcha", "access denied", "unusual traffic", "are you a robot",
    "verify you are human", "cf-chl", "px-captcha", "request blocked",
)

# Injected before any page script runs; hides the most common headless tells.
# For tougher targets consider a maintained stealth layer (e.g. patchright / playwright-stealth).
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
const _q = window.navigator.permissions && window.navigator.permissions.query;
if (_q) {
  window.navigator.permissions.query = (p) =>
    p && p.name === 'notifications'
      ? Promise.resolve({state: Notification.permission})
      : _q.call(window.navigator.permissions, p);
}
"""


# --------------------------------------------------------------------------- #
# Errors and small value objects
# --------------------------------------------------------------------------- #
class FetchError(Exception):
    """A page could not be fetched (permanent failure, or retries exhausted)."""


class RetryableFetchError(FetchError):
    """A transient / block-like failure that should be retried with backoff."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class FetchResult:
    url: str          # the original target URL (not the proxy URL)
    status: int
    html: str
    engine: str       # "httpx" or "playwright"


@dataclass
class ProxySettings:
    """
    Managed-proxy configuration.

    api_url_template: placeholder string, formatted with {api_key} and {url}, e.g.
        ScraperAPI : "http://api.scraperapi.com/?api_key={api_key}&url={url}"
        Crawlbase  : "https://api.crawlbase.com/?token={api_key}&url={url}"
        (Bright Data's Web Unlocker / API products use their own URL or proxy-mode format.)
    Leave it empty to disable API-mode proxying (direct requests).

    browser_proxy: Playwright proxy dict ({"server", "username", "password"}) for
        proxy-mode access. Browsers should use the provider's *proxy endpoint*, not the
        API URL, otherwise the provider would return pre-rendered HTML and bypass Playwright.
    """
    api_url_template: str = ""
    api_key: str = ""
    browser_proxy: dict[str, str] | None = None
    browser_ignore_https_errors: bool = False   # some providers require True in proxy mode

    def build_proxied_url(self, target_url: str) -> str:
        if not self.api_url_template:
            return target_url
        return self.api_url_template.format(
            api_key=quote(self.api_key, safe=""),
            url=quote(target_url, safe=""),
        )


@dataclass
class RetryPolicy:
    """Exponential backoff with 'equal jitter': half deterministic, half random."""
    max_attempts: int = 6
    base_delay: float = 1.5
    max_delay: float = 60.0

    def delay_for(self, attempt: int, retry_after: float | None = None) -> float:
        exp = min(self.max_delay, self.base_delay * (2 ** attempt))
        delay = exp / 2 + random.uniform(0, exp / 2)
        if retry_after:                                   # server told us how long to wait
            delay = max(delay, min(retry_after, 120.0))
        return delay


def build_headers(user_agent: str | None = None) -> dict[str, str]:
    """Browser-like request headers with a rotated UA and Accept-Language."""
    return {
        "User-Agent": user_agent or random.choice(ALL_USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": random.choice(ACCEPT_LANGUAGES),
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }


def looks_blocked(html: str) -> bool:
    """Heuristic for soft blocks: a short page that mentions a CAPTCHA / denial phrase."""
    if len(html) > 30_000:
        return False
    head = html[:20_000].lower()
    return any(marker in head for marker in BLOCK_MARKERS)


def _parse_retry_after(value: str | None) -> float | None:
    if value and value.strip().isdigit():
        return float(value.strip())
    return None


async def _block_heavy_resources(route) -> None:
    """Playwright route handler: we only need image *URLs*, not the image bytes."""
    if route.request.resource_type in {"image", "media", "font"}:
        await route.abort()
    else:
        await route.continue_()


class _Throttle:
    """Spaces out request starts to one domain (randomised around min_delay)."""

    def __init__(self, min_delay: float):
        self._min_delay = min_delay
        self._next_allowed = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            now = loop.time()
            if self._next_allowed > now:
                await asyncio.sleep(self._next_allowed - now)
                now = loop.time()
            self._next_allowed = now + self._min_delay * random.uniform(0.75, 1.5)


@dataclass
class _DomainState:
    sem: asyncio.BoundedSemaphore
    throttle: _Throttle


# --------------------------------------------------------------------------- #
# The request handler
# --------------------------------------------------------------------------- #
class RequestHandler:
    """Use as an async context manager: ``async with RequestHandler(...) as handler``."""

    def __init__(
        self,
        proxy: ProxySettings | None = None,
        retry: RetryPolicy | None = None,
        max_concurrency: int = 20,       # global cap on in-flight requests (memory + proxy bandwidth)
        max_browser_pages: int = 3,      # headless pages are RAM-hungry, so cap them separately
        request_timeout: float = 45.0,
        headless: bool = True,
    ):
        self.proxy = proxy or ProxySettings()
        self.retry = retry or RetryPolicy()
        self.request_timeout = request_timeout
        self.headless = headless
        self._max_concurrency = max_concurrency

        self._global_sem = asyncio.BoundedSemaphore(max_concurrency)
        self._browser_sem = asyncio.BoundedSemaphore(max_browser_pages)
        self._domains: dict[str, _DomainState] = {}

        self._client: httpx.AsyncClient | None = None
        self._playwright = None
        self._browser = None
        self._browser_lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------
    async def __aenter__(self) -> "RequestHandler":
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.request_timeout, connect=15.0),
            limits=httpx.Limits(
                max_connections=self._max_concurrency,
                max_keepalive_connections=self._max_concurrency,
            ),
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._client:
            await self._client.aclose()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    # -- public API --------------------------------------------------------
    async def fetch(self, url: str, site: SiteConfig) -> FetchResult:
        """Fetch ``url`` with adaptive retries. Raises FetchError when giving up."""
        state = self._domain_state(site)
        last_error: Exception | None = None

        for attempt in range(self.retry.max_attempts):
            await state.throttle.wait()                       # politeness delay (not holding any slot)
            try:
                # Slots are held only while a request is in flight, never while backing off.
                async with state.sem, self._global_sem:
                    if site.render_javascript:
                        async with self._browser_sem:
                            return await self._fetch_browser(url, site)
                    return await self._fetch_http(url, site)
            except RetryableFetchError as exc:
                last_error = exc
                if attempt + 1 >= self.retry.max_attempts:
                    break
                delay = self.retry.delay_for(attempt, exc.retry_after)
                logger.warning(
                    "[%s] retry %d/%d for %s in %.1fs (%s)",
                    site.name, attempt + 1, self.retry.max_attempts - 1, url, delay, exc,
                )
                await asyncio.sleep(delay)

        raise FetchError(
            f"gave up on {url} after {self.retry.max_attempts} attempts: {last_error}"
        ) from last_error

    # -- internals ---------------------------------------------------------
    def _domain_state(self, site: SiteConfig) -> _DomainState:
        state = self._domains.get(site.domain)
        if state is None:
            state = _DomainState(
                sem=asyncio.BoundedSemaphore(site.max_concurrent_requests),
                throttle=_Throttle(site.min_delay_seconds),
            )
            self._domains[site.domain] = state
        return state

    @staticmethod
    def _check_status(status: int, retry_after_header: str | None, url: str) -> None:
        if 200 <= status < 300:
            return
        if status in RETRYABLE_STATUS_CODES:
            raise RetryableFetchError(f"HTTP {status}", _parse_retry_after(retry_after_header))
        raise FetchError(f"HTTP {status} for {url} (not retryable)")

    # -- engine 1: httpx (static HTML) --------------------------------------
    async def _fetch_http(self, url: str, site: SiteConfig) -> FetchResult:
        assert self._client is not None, "use RequestHandler as an async context manager"
        target = self.proxy.build_proxied_url(url) if site.use_proxy else url
        try:
            resp = await self._client.get(target, headers=build_headers())
        except httpx.TransportError as exc:                   # timeouts, resets, DNS, proxy errors
            raise RetryableFetchError(f"transport error: {exc!r}") from exc

        self._check_status(resp.status_code, resp.headers.get("Retry-After"), url)
        html = resp.text
        if looks_blocked(html):
            raise RetryableFetchError("soft block (CAPTCHA / challenge page)")
        return FetchResult(url=url, status=resp.status_code, html=html, engine="httpx")

    # -- engine 2: Playwright (JS-heavy) ------------------------------------
    async def _ensure_browser(self):
        """Lazily start one shared Chromium; each request gets its own isolated context."""
        if self._browser is None:
            async with self._browser_lock:
                if self._browser is None:
                    from playwright.async_api import async_playwright   # lazy: only needed for JS sites
                    self._playwright = await async_playwright().start()
                    self._browser = await self._playwright.chromium.launch(
                        headless=self.headless,
                        # Add "--no-sandbox" only inside containers that require it.
                        args=["--disable-blink-features=AutomationControlled"],
                    )
        return self._browser

    async def _fetch_browser(self, url: str, site: SiteConfig) -> FetchResult:
        browser = await self._ensure_browser()
        context = None
        try:
            use_proxy = site.use_proxy and self.proxy.browser_proxy
            context = await browser.new_context(
                user_agent=random.choice(CHROME_USER_AGENTS),
                locale="en-US",
                viewport={"width": random.randint(1280, 1920), "height": random.randint(720, 1080)},
                extra_http_headers={"Accept-Language": random.choice(ACCEPT_LANGUAGES)},
                proxy=self.proxy.browser_proxy if use_proxy else None,
                ignore_https_errors=bool(use_proxy and self.proxy.browser_ignore_https_errors),
            )
            await context.add_init_script(STEALTH_JS)
            if site.block_resources:
                await context.route("**/*", _block_heavy_resources)

            page = await context.new_page()
            resp = await page.goto(
                url, wait_until="domcontentloaded", timeout=int(self.request_timeout * 1000)
            )
            if resp is not None:
                self._check_status(resp.status, resp.headers.get("retry-after"), url)

            # Best-effort prep steps: failures here must not discard a page that loaded.
            if site.wait_for_selector:
                try:
                    # state="attached" => hidden / off-screen elements count too
                    await page.wait_for_selector(site.wait_for_selector, state="attached", timeout=15_000)
                except Exception:
                    logger.warning("[%s] wait_for_selector %r timed out on %s",
                                   site.name, site.wait_for_selector, url)
            for selector in site.click_selectors:
                try:
                    await page.click(selector, timeout=2_000)
                    await page.wait_for_timeout(500)
                except Exception:
                    logger.debug("[%s] optional click %r not available", site.name, selector)
            await self._auto_scroll(page, site)

            html = await page.content()
            if looks_blocked(html):
                raise RetryableFetchError("soft block (CAPTCHA / challenge page)")
            return FetchResult(url=url, status=resp.status if resp else 200, html=html, engine="playwright")

        except FetchError:
            raise
        except Exception as exc:                              # Playwright timeouts, navigation errors, ...
            raise RetryableFetchError(f"browser error: {exc!r}") from exc
        finally:
            if context is not None:
                await context.close()

    @staticmethod
    async def _auto_scroll(page, site: SiteConfig) -> None:
        """Scroll to the bottom repeatedly until the page height stops growing."""
        if site.scroll_passes <= 0:
            return
        last_height = await page.evaluate("document.body.scrollHeight")
        for _ in range(site.scroll_passes):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(site.scroll_pause_ms + random.randint(0, 400))
            new_height = await page.evaluate("document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height
