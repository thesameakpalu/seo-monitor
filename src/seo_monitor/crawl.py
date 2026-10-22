"""Crawl one site: homepage, robots.txt, sitemaps, internal links, host variants, resources."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .config import Profile
from .fetch import Fetcher, FetchResult
from .parse import Document, parse_html
from .robots import Robots, parse_robots
from .sitemap import SitemapFile, load_sitemaps
from .urls import host_of, is_presigned, normalize, origin, path_and_query, same_page

HOST_SAMPLES = 5  # pages (besides the homepage) whose http/https/www/non-www variants are tested
CANONICAL_TARGET_CHECKS = 100
RECHECK_LIMIT = 200  # URLs flagged in the previous run that are re-visited to confirm fixes
OG_IMAGE_CHECKS = 50
CONFIRM_LIMIT = 300  # failed URLs re-tested at the end before being reported
STRUGGLING_STOP = 3  # control requests failing in a row: the server is down, stop confirming
FOUNDATION_ROUNDS = 3  # tries, a little apart, at the homepage, robots.txt and sitemaps


@dataclass
class Page:
    url: str
    fetch: FetchResult
    source: str  # start | key | sitemap | link
    doc: Document | None = None

    @property
    def status(self) -> int | None:
        return self.fetch.status

    @property
    def final_url(self) -> str:
        return self.fetch.final_url

    @property
    def is_ok_html(self) -> bool:
        return self.fetch.ok and self.doc is not None

    @property
    def robots_directives(self) -> str:
        meta = self.doc.meta_robots if self.doc else ""
        return f"{meta}, {self.fetch.headers.get('x-robots-tag', '').lower()}".strip(", ")

    @property
    def noindex(self) -> bool:
        directives = self.robots_directives
        return "noindex" in directives or " none" in f" {directives}"

    @property
    def canonical(self) -> str | None:
        return self.doc.canonical if self.doc else None

    @property
    def canonicalised_elsewhere(self) -> bool:
        return bool(self.canonical) and not same_page(self.canonical, self.final_url)

    @property
    def indexable(self) -> bool:
        return self.is_ok_html and not self.noindex and not self.canonicalised_elsewhere


@dataclass
class Variant:
    sample: str  # the page whose variant this is
    url: str
    fetch: FetchResult
    canonical: str | None = None


@dataclass
class CrawlResult:
    profile: Profile
    mode: str  # full | quick
    recheck: list[str] = field(default_factory=list)  # URLs flagged last run
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    start_url: str = ""
    home: Page | None = None
    robots: Robots | None = None
    sitemaps: list[SitemapFile] = field(default_factory=list)
    sitemap_urls: dict[str, str] = field(default_factory=dict)  # loc -> sitemap file
    pages: dict[str, Page] = field(default_factory=dict)
    variants: list[Variant] = field(default_factory=list)
    resources: dict[str, FetchResult] = field(default_factory=dict)
    blocked_by_robots: set[str] = field(default_factory=set)
    skipped_unsafe: set[str] = field(default_factory=set)
    over_limit: set[str] = field(default_factory=set)
    requests: int = 0
    complete: bool = False  # False if cancelled or crashed part-way
    _by_final: dict[str, Page] = field(default_factory=dict)

    @property
    def site(self) -> str:
        return self.profile.site

    @property
    def hosts(self) -> set[str]:
        return {self.site, f"www.{self.site}"}

    def is_internal(self, url: str | None) -> bool:
        return bool(url) and host_of(url) in self.hosts

    def add_page(self, page: Page) -> None:
        self.pages[page.url] = page
        known = self._by_final.get(page.final_url)
        if known is None or known.url == page.url or not known.fetch.ok:
            self._by_final[page.final_url] = page

    def lookup(self, url: str) -> FetchResult | None:
        """Fetch result for a URL that was requested (page or resource) or reached by redirect."""
        url = normalize(url)
        if url in self.pages:
            return self.pages[url].fetch
        if url in self.resources:
            return self.resources[url]
        if url in self._by_final:
            return self._by_final[url].fetch
        return None

    @property
    def checked_urls(self) -> set[str]:
        urls = set(self.pages) | set(self.resources) | {v.url for v in self.variants}
        return urls | {p.final_url for p in self.pages.values()}

    @property
    def preferred_host(self) -> str:
        pref = self.profile.preferred_host
        if pref == "www":
            return f"www.{self.site}"
        if pref == "non-www":
            return self.site
        if self.home and self.is_internal(self.home.final_url):
            return host_of(self.home.final_url)
        return host_of(self.start_url)


ProgressFn = Callable[[CrawlResult, str], None]


def _make_page(url: str, res: FetchResult, source: str) -> Page:
    doc = None
    if res.ok and res.is_html and res.body:
        doc = parse_html(res.text(), res.final_url)
    return Page(url=url, fetch=res, source=source, doc=doc)


async def crawl(result: CrawlResult, fetcher: Fetcher, progress: ProgressFn | None = None) -> None:
    """Fill ``result`` in place so a cancelled crawl still leaves usable partial data."""
    p = result.profile
    quick = result.mode == "quick"
    notify = progress or (lambda r, msg: None)
    try:
        result.start_url = normalize(p.url)
        if not await _fetch_foundation(result, fetcher, notify):
            return  # the homepage never answered: report that alone, not a crawl of nothing
        delay = result.robots.crawl_delay()
        if delay and delay > fetcher.delay:
            fetcher.delay = min(delay, 10.0)
        for sm in result.sitemaps:
            if sm.kind == "urlset":
                for loc in sm.locs:
                    result.sitemap_urls.setdefault(normalize(loc), sm.url)

        await _crawl_pages(result, fetcher, notify, quick)
        await _recheck_previous(result, fetcher, notify)
        await _check_host_variants(result, fetcher, notify)
        await _check_resources(result, fetcher, notify, quick)
        await _confirm_failures(result, fetcher, notify)
        result.complete = True
    finally:
        result.finished_at = datetime.now(UTC)
        result.requests = fetcher.requests


async def _fetch_foundation(result: CrawlResult, fetcher: Fetcher, notify) -> bool:
    """Homepage, robots.txt and sitemaps. Whatever failed for a temporary reason (our own
    connection, a busy server) is tried again in later rounds, so a blip at the start does not
    become "robots.txt missing" or "no sitemap". False if the homepage never answered."""
    p = result.profile
    start = result.start_url
    robots_res: FetchResult | None = None
    sitemaps_done = False
    for round_ in range(FOUNDATION_ROUNDS):
        if round_:
            await asyncio.sleep(fetcher.retry_wait * 6 * round_)
        last = round_ == FOUNDATION_ROUNDS - 1
        if result.home is None or result.home.fetch.transient:
            notify(result, start)
            result.add_page(_make_page(start, await fetcher.fetch(start), "start"))
            result.home = result.pages[start]
            if result.home.fetch.transient:
                continue
        home_final = result.home.final_url if result.is_internal(result.home.final_url) else start

        if robots_res is None or robots_res.transient:
            robots_url = origin(home_final) + "/robots.txt"
            notify(result, robots_url)
            robots_res = await fetcher.fetch(robots_url)
            if robots_res.ok:
                result.robots = parse_robots(robots_url, robots_res.text(), robots_res.status)
            else:
                result.robots = parse_robots(robots_url, "", robots_res.status)
                result.robots.error = robots_res.describe()
            if robots_res.transient and not last:
                continue  # its Sitemap: lines decide which sitemaps to read

        if not sitemaps_done:
            seeds = [(u, "robots") for u in result.robots.sitemaps]
            seeds += [(u, "profile") for u in p.sitemaps]
            notify(result, "sitemaps")
            result.sitemaps = await load_sitemaps(fetcher, seeds, origin(home_final))
            sitemaps_done = not any(sm.transient for sm in result.sitemaps)
        if sitemaps_done and not robots_res.transient:
            break
    return not result.home.fetch.transient


async def _crawl_pages(result: CrawlResult, fetcher: Fetcher, notify, quick: bool) -> None:
    p = result.profile
    limit = p.quick_max_pages if quick else p.max_pages
    queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
    scheduled: set[str] = set(result.pages) | {result.home.final_url}

    def schedule(url: str | None, source: str) -> None:
        if not url or url in scheduled or not result.is_internal(url):
            return
        if any(pat in url for pat in p.all_skip_patterns):
            result.skipped_unsafe.add(url)
            return
        if result.robots and not result.robots.allowed(url):
            result.blocked_by_robots.add(url)
            return
        if len(scheduled) >= limit:
            result.over_limit.add(url)
            return
        scheduled.add(url)
        queue.put_nowait((url, source))

    for u in p.key_urls:
        schedule(normalize(u if "://" in u else origin(result.start_url) + u), "key")
    sitemap_locs = list(result.sitemap_urls)
    if quick and sitemap_locs:
        room = max(limit - len(scheduled), 0)
        step = max(len(sitemap_locs) // room, 1) if room else len(sitemap_locs) + 1
        sitemap_locs = sitemap_locs[::step][:room]
    for loc in sitemap_locs:
        schedule(loc, "sitemap")
    if not quick and result.home.doc:
        for link in result.home.doc.links:
            schedule(link.url, "link")

    async def worker() -> None:
        while True:
            url, source = await queue.get()
            try:
                notify(result, url)
                page = _make_page(url, await fetcher.fetch(url), source)
                result.add_page(page)
                scheduled.add(page.final_url)
                if not quick and page.doc and result.is_internal(page.final_url):
                    for link in page.doc.links:
                        schedule(link.url, "link")
            finally:
                queue.task_done()

    workers = [asyncio.create_task(worker()) for _ in range(p.concurrency)]
    try:
        await queue.join()
    finally:
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


async def _recheck_previous(result: CrawlResult, fetcher: Fetcher, notify) -> None:
    """Visit URLs flagged last time that this crawl did not reach, so fixes can be confirmed."""
    skip = result.profile.all_skip_patterns
    todo = [
        u
        for u in dict.fromkeys(normalize(u) for u in result.recheck)
        if result.is_internal(u)
        and result.lookup(u) is None
        and not any(pat in u for pat in skip)
        and (result.robots is None or result.robots.allowed(u))
    ]
    for url in todo[:RECHECK_LIMIT]:
        notify(result, url)
        result.add_page(_make_page(url, await fetcher.fetch(url), "recheck"))


async def _check_host_variants(result: CrawlResult, fetcher: Fetcher, notify) -> None:
    samples = [result.start_url]
    for page in result.pages.values():
        if len(samples) > HOST_SAMPLES:
            break
        if page.source == "sitemap" and page.is_ok_html and page.url not in samples:
            samples.append(page.url)
    for page in result.pages.values():
        if len(samples) > HOST_SAMPLES:
            break
        if page.is_ok_html and page.url not in samples:
            samples.append(page.url)

    site = result.site
    for sample in samples:
        tail = path_and_query(sample)
        for scheme in ("https", "http"):
            for host in (site, f"www.{site}"):
                url = normalize(f"{scheme}://{host}{tail}")
                known = result.pages.get(url)
                if known:
                    res, canonical = known.fetch, known.canonical
                else:
                    notify(result, url)
                    res = await fetcher.fetch(url)
                    canonical = None
                    if res.ok and res.is_html and res.body:
                        canonical = parse_html(res.text(), res.final_url).canonical
                result.variants.append(
                    Variant(sample=sample, url=url, fetch=res, canonical=canonical)
                )


async def _check_resources(result: CrawlResult, fetcher: Fetcher, notify, quick: bool) -> None:
    """Status of canonical targets and og:images, size/type of images (capped)."""
    p = result.profile
    canonical_targets, og_images, images = [], [], []
    for page in result.pages.values():
        if not page.doc:
            continue
        c = page.canonical
        if c and c.startswith("http") and normalize(c) != page.final_url:
            canonical_targets.append(normalize(c))
        og = page.doc.og.get("og:image")
        if og and og.startswith("http"):
            og_images.append(normalize(og))
        images.extend(img.src for img in page.doc.images)

    async def check(urls: list[str], cap: int, mode: str) -> None:
        todo = [u for u in dict.fromkeys(urls) if result.lookup(u) is None][:cap]
        for url in todo:
            notify(result, url)
            result.resources[url] = await fetcher.fetch(url, body=mode)

    await check(canonical_targets, CANONICAL_TARGET_CHECKS, "page")
    await check(og_images, OG_IMAGE_CHECKS, "size")
    if not quick:
        # Presigned URLs are reported regardless; fetching them adds nothing.
        await check([u for u in images if not is_presigned(u)], p.image_checks, "size")


async def _confirm_failures(result: CrawlResult, fetcher: Fetcher, notify) -> None:
    """Re-test every failed URL after a cool-down before anything is reported.

    A 404 must happen again. A refusal (403), 5xx or timeout is pinned on the URL only if, right
    then, a control request to a page that worked earlier still loads; otherwise the server (or
    its firewall) is struggling and the URL is reported as unreachable, never as broken. Pages
    that recover are stored with their new answer, so they drop out of every check.
    """
    failed: dict[str, FetchResult] = {}
    for url, page in result.pages.items():
        if page.fetch.broken or page.fetch.transient:
            failed[url] = page.fetch
    for url, res in result.resources.items():
        if res.broken or res.transient:
            failed[url] = res
    if not failed:
        return
    # Refusals and 404s first: those are the ones that would be reported as broken.
    todo = sorted(failed, key=lambda u: failed[u].server_trouble)[:CONFIRM_LIMIT]
    controls = _control_pages(result, set(failed))
    await asyncio.sleep(fetcher.retry_wait * 6)
    struggling = used = 0
    for url in todo:
        notify(result, url)
        before = failed[url]
        mode = "page" if url in result.pages or before.body is not None else "size"
        res = await fetcher.fetch(url, body=mode, retries=1)
        res.attempts += before.attempts
        if res.broken:
            res.confirmed = True
        elif res.transient and controls:
            control = await fetcher.fetch(controls[used % len(controls)], retries=0)
            used += 1
            if control.ok:
                res.confirmed, struggling = True, 0
            else:
                struggling += 1
        if url in result.pages:
            result.add_page(_make_page(url, res, result.pages[url].source))
        else:
            result.resources[url] = res
        if struggling >= STRUGGLING_STOP:
            break  # the whole site is failing; what is left stays "unreachable"
        if struggling:
            await asyncio.sleep(fetcher.retry_wait * 6)


def _control_pages(result: CrawlResult, failed: set[str]) -> list[str]:
    """Pages that loaded earlier, for checking the server is healthy. Not the homepage first:
    it is often served from a cache while uncached pages hit the hosting limits."""
    pages = [
        u
        for u, page in result.pages.items()
        if page.is_ok_html and u not in failed and result.is_internal(page.final_url)
    ]
    others = [u for u in pages if u != result.start_url]
    return others[:20] or pages[:1]
