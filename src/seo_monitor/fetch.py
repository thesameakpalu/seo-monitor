"""Polite HTTP fetching: GET only, per-host rate limit, manual redirects so chains are recorded."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin

import httpx

from .urls import host_of, normalize

REDIRECT_CODES = {301, 302, 303, 307, 308}
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
    def redirected(self) -> bool:
        return bool(self.chain)

    def describe(self) -> str:
        if self.loop:
            return "redirect loop"
        if self.error and self.status is None:
            return self.error
        return f"HTTP {self.status}" + (f" ({self.error})" if self.error else "")

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
        return f"could not connect (DNS or connection refused): {msg[:120]}"
    return f"{type(exc).__name__}: {msg[:160]}" if msg else type(exc).__name__


class Fetcher:
    """One shared client. Every request waits its turn per host, so concurrency never
    exceeds ``1 / delay`` requests per second on any host."""

    def __init__(
        self,
        user_agent: str,
        delay: float = 1.0,
        timeout: float = 20.0,
        max_body: int = 5_000_000,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.delay = delay
        self.timeout = timeout
        self.max_body = max_body
        self.requests = 0
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
            self._next[host] = time.monotonic() + self.delay

    async def fetch(self, url: str, body: str = "page", max_redirects: int = 10) -> FetchResult:
        """GET ``url`` following redirects manually.

        body: "page" reads text/XML bodies only; "full" reads any body (sitemaps, maybe gzipped);
        "size" streams the body only to measure it when Content-Length is missing.
        """
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
