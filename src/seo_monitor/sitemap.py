"""XML sitemap discovery and parsing (urlset, sitemap index, gzip)."""

from __future__ import annotations

import gzip
import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .fetch import Fetcher
from .urls import normalize

GUESSES = ["/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml"]
MAX_FILES = 200
_LOC_RX = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.I | re.S)


@dataclass
class SitemapFile:
    url: str
    source: str  # robots | profile | guess | index
    status: int | None = None
    final_url: str = ""
    redirected: bool = False
    error: str | None = None
    kind: str = ""  # urlset | index
    locs: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == 200 and not self.error


def parse_sitemap(body: bytes) -> tuple[str, list[str]]:
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        text = body.decode("utf-8", errors="replace")
        kind = "index" if "<sitemapindex" in text else "urlset"
        locs = [html.unescape(m) for m in _LOC_RX.findall(text)]
        if not locs:
            raise
        return kind, locs
    kind = "index" if root.tag.rsplit("}", 1)[-1] == "sitemapindex" else "urlset"
    locs = [
        el.text.strip()
        for el in root.iter()
        if el.tag.rsplit("}", 1)[-1] == "loc" and el.text and el.text.strip()
    ]
    return kind, locs


async def load_sitemaps(
    fetcher: Fetcher, seeds: list[tuple[str, str]], origin: str
) -> list[SitemapFile]:
    """Fetch declared sitemaps (following indexes). If none are declared, try common locations."""
    files: list[SitemapFile] = []
    queue = list(seeds)
    seen: set[str] = set()
    guessing = not seeds
    if guessing:
        queue = [(origin + g, "guess") for g in GUESSES]

    while queue and len(files) < MAX_FILES:
        url, source = queue.pop(0)
        url = normalize(url)
        if url in seen:
            continue
        seen.add(url)
        res = await fetcher.fetch(url, body="full")
        sm = SitemapFile(
            url=url,
            source=source,
            status=res.status,
            final_url=res.final_url,
            redirected=res.redirected,
            error=res.error,
        )
        if res.ok and res.body:
            try:
                sm.kind, sm.locs = parse_sitemap(res.body)
            except (ET.ParseError, OSError, EOFError) as exc:
                sm.error = f"not a valid XML sitemap ({exc.__class__.__name__})"
        elif res.status and res.status != 200:
            sm.error = f"HTTP {res.status}"
        if source == "guess" and not sm.ok:
            continue  # failed guesses are not errors
        files.append(sm)
        if sm.kind == "index":
            queue.extend((loc, "index") for loc in sm.locs)
        if source == "guess" and sm.ok:
            # First working guess is enough; it is usually the index.
            queue = [q for q in queue if q[1] != "guess"]
    return files
