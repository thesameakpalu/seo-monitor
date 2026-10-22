"""Polite HTTP fetching: GET only, per-host rate limit, manual redirects so chains are recorded."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

import httpx

from .urls import host_of, normalize

REDIRECT_CODES = {301, 302, 303, 307, 308}
# 4xx answers that mean "busy, try later"; every 5xx is treated the same way (shared hosting
# answers 508 "resource limit reached" when a crawl uses its CPU quota).
BUSY_CODES = {408, 425, 429}
# "Access denied" is usually a firewall reacting to the crawl, not a dead page. It only counts
# as broken once confirmed: still refused after a cool-down while the rest of the site answers.
BLOCKED_CODES = {401, 403, 406, 451}
REASONS = {
    401: "login required",
    403: "access denied",
    406: "refused by server rules",
    408: "request timeout",
    429: "too many requests",
    451: "blocked for legal reasons",
    500: "server error",
    502: "bad gateway",
    503: "service unavailable",
    504: "gateway timeout",
    508: "hosting resource limit reached",
    509: "bandwidth limit exceeded",
}
MAX_HOST_DELAY = 10.0
PAUSE_AFTER = 3  # failures in a row on one host before it gets a pause
MAX_PAUSE = 300.0
TEXT_TYPES = ("text/", "application/xhtml+xml", "application/xml", "application/rss+xml")


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int | None = None
    chain: list[tuple[str, int]] = field(default_factory=list)  # (url, status) of each redirect hop
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None
    size: int | None = None
    elapsed_ms: int = 0
    error: str | None = None
    loop: bool = False
    network_error: bool = False  # timeout, dropped or refused connection (not SSL)
    attempts: int = 1
    # Failed again after a cool-down while a page that worked earlier still loaded, so the
    # failure belongs to this URL rather than to a struggling or defensive server.
    confirmed: bool = False

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    @property
    def is_html(self) -> bool:
        return self.content_type in ("text/html", "application/xhtml+xml")

    @property
    def ok(self) -> bool:
        return self.status == 200 and not self.error

    @property
    def server_trouble(self) -> bool:
        """No answer, "busy" or a 5xx: the server failed, whatever the URL."""
        return self.network_error or self.status in BUSY_CODES or (self.status or 0) >= 500

    @property
    def blocked(self) -> bool:
        return self.status in BLOCKED_CODES

    @property
    def transient(self) -> bool:
        """Failed in a way that says more about the server's load (or firewall) than the URL."""
        return self.server_trouble or (self.blocked and not self.confirmed)

    @property
    def broken(self) -> bool:
        """Definitely broken: a 404/410-type answer, a confirmed refusal, a redirect loop or a
        permanent error such as a bad certificate."""
        return (
            not self.ok
            and not self.transient
            and (self.loop or self.status is None or self.status >= 400)
        )

    @property
    def redirected(self) -> bool:
        return bool(self.chain)

    def describe(self) -> str:
        if self.loop:
            text = "redirect loop"
        elif self.error and self.status is None:
            text = self.error
        else:
            reason = self.error or REASONS.get(self.status or 0)
            text = f"HTTP {self.status}" + (f" ({reason})" if reason else "")
        if self.confirmed and self.server_trouble:
            text += ", while the rest of the site loaded"
        return text + (f", {self.attempts} attempts" if self.attempts > 1 else "")

    def text(self) -> str:
        if not self.body:
            return ""
        charset = "utf-8"
        for part in self.headers.get("content-type", "").split(";")[1:]:
            key, _, value = part.partition("=")
            if key.strip().lower() == "charset" and value.strip():
                charset = value.strip().strip("\"'")
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


def _describe_error(exc: Exception, timeout: float) -> str:
    msg = str(exc)
    if isinstance(exc, httpx.TimeoutException):
        return f"timed out after {timeout:.0f}s"
    if "CERTIFICATE_VERIFY_FAILED" in msg or "SSL" in msg:
        return f"SSL/TLS problem: {msg[:160]}"
    if isinstance(exc, httpx.ConnectError):
        return "could not connect (refused, dropped or DNS failure)" + (
            f": {msg[:120]}" if msg else ""
        )
    return f"{type(exc).__name__}: {msg[:160]}" if msg else type(exc).__name__


class Fetcher:
    """One shared client. Every request waits its turn per host, so concurrency never
    exceeds ``1 / delay`` requests per second on any host.

    Timeouts, dropped connections, "busy" answers (429/5xx) and refusals (403) are retried
    after a pause and that host is slowed down; after several failures in a row the host gets a
    growing pause (circuit breaker), so a struggling server is not pushed into blocking us."""

    def __init__(
        self,
        user_agent: str,
        delay: float = 1.0,
        timeout: float = 20.0,
        max_body: int = 5_000_000,
        transport: httpx.AsyncBaseTransport | None = None,
        retries: int = 2,
        retry_wait: float = 5.0,
    ):
        self.delay = delay
        self.timeout = timeout
        self.max_body = max_body
        self.retries = retries
        self.retry_wait = retry_wait
        self.requests = 0
        self.slowed: dict[str, float] = {}  # host -> extra delay added after transient failures
        self.failures: dict[str, int] = {}  # host -> transient failures in a row
        self._next: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en",
            },
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _wait_turn(self, host: str) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = self._next.get(host, 0.0) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next[host] = time.monotonic() + self.delay + self.slowed.get(host, 0.0)

    def _slow_down(self, host: str) -> None:
        current = self.slowed.get(host, 0.0)
        self.slowed[host] = min(max(current * 2, self.delay), MAX_HOST_DELAY)
        failures = self.failures[host] = self.failures.get(host, 0) + 1
        if failures >= PAUSE_AFTER:
            pause = min(self.retry_wait * 6 * 2 ** (failures - PAUSE_AFTER), MAX_PAUSE)
            self._next[host] = max(self._next.get(host, 0.0), time.monotonic() + pause)

    def _recover(self, host: str) -> None:
        self.failures.pop(host, None)
        if host in self.slowed:
            self.slowed[host] /= 2
            if self.slowed[host] < 0.1:
                del self.slowed[host]

    async def fetch(
        self, url: str, body: str = "page", max_redirects: int = 10, retries: int | None = None
    ) -> FetchResult:
        """GET ``url`` following redirects manually, retrying transient failures.

        body: "page" reads text/XML bodies only; "full" reads any body (sitemaps, maybe gzipped);
        "size" streams the body only to measure it when Content-Length is missing.
        """
        retries = self.retries if retries is None else retries
        for attempt in range(retries + 1):
            res = await self._fetch_once(url, body, max_redirects)
            res.attempts = attempt + 1
            if not res.transient:
                self._recover(host_of(res.final_url))
                return res
            self._slow_down(host_of(res.final_url))
            if attempt == retries:
                return res
            await asyncio.sleep(self._retry_after(res) or self.retry_wait * 3**attempt)
        return res

    @staticmethod
    def _retry_after(res: FetchResult) -> float | None:
        """Seconds asked for by a Retry-After header (seconds or HTTP date), capped at 60."""
        value = res.headers.get("retry-after", "").strip()
        if value.isdigit():
            return min(float(value), 60.0)
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return min(max((when - datetime.now(UTC)).total_seconds(), 0.0), 60.0) or None

    async def _fetch_once(self, url: str, body: str, max_redirects: int) -> FetchResult:
        res = FetchResult(url=url, final_url=url)
        current, seen = url, {url}
        started = time.monotonic()
        try:
            for _ in range(max_redirects + 1):
                await self._wait_turn(host_of(current))
                self.requests += 1
                async with self._client.stream("GET", current) as r:
                    location = r.headers.get("location")
                    if r.status_code in REDIRECT_CODES and location:
                        nxt = normalize(urljoin(current, location))
                        res.chain.append((current, r.status_code))
                        if nxt in seen:
                            res.loop, res.status, res.final_url = True, r.status_code, nxt
                            return res
                        seen.add(nxt)
                        current = nxt
                        continue
                    res.status, res.final_url = r.status_code, current
                    res.headers = {k.lower(): v for k, v in r.headers.items()}
                    length = r.headers.get("content-length", "")
                    res.size = int(length) if length.isdigit() else None
                    await self._read(r, res, body)
                    return res
            res.status = res.chain[-1][1] if res.chain else None
            res.error = f"more than {max_redirects} redirects"
        except httpx.HTTPError as exc:
            res.error = _describe_error(exc, self.timeout)
            res.network_error = isinstance(exc, httpx.TransportError) and not res.error.startswith(
                "SSL"
            )
        finally:
            res.elapsed_ms = int((time.monotonic() - started) * 1000)
        return res

    async def _read(self, r: httpx.Response, res: FetchResult, mode: str) -> None:
        ctype = res.content_type
        wanted = mode == "full" or (mode == "page" and ctype.startswith(TEXT_TYPES))
        if mode == "size" and res.size is not None:
            return
        if not wanted and mode != "size":
            return
        buf, total = bytearray(), 0
        async for chunk in r.aiter_bytes():
            total += len(chunk)
            if wanted:
                buf += chunk
            if total > self.max_body:
                break
        if wanted:
            res.body = bytes(buf)
        if res.size is None and total <= self.max_body:
            res.size = total
