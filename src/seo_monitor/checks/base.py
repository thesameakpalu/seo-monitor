"""Check registry. Adding a check = writing one decorated function that yields Items."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from ..crawl import CrawlResult, Page

log = logging.getLogger(__name__)

SEVERITIES = ("critical", "high", "medium", "low")


@dataclass
class Item:
    url: str  # the URL the problem is on ("" for site-wide issues)
    detail: str = ""
    key: str | None = None  # identity across runs; defaults to url

    @property
    def ident(self) -> str:
        return self.key if self.key is not None else self.url


@dataclass
class Previous:
    """What change-detection checks need from the last comparable run."""

    robots_txt: str | None = None
    pages: dict[str, dict] = field(default_factory=dict)  # url -> {status, noindex, indexable}


@dataclass
class Context:
    crawl: CrawlResult
    prev: Previous | None = None

    @property
    def profile(self):
        return self.crawl.profile

    @property
    def pages(self) -> list[Page]:
        return list(self.crawl.pages.values())

    @property
    def html_pages(self) -> list[Page]:
        """Pages that returned 200 HTML on the site's own hosts."""
        return [p for p in self.pages if p.is_ok_html and self.crawl.is_internal(p.final_url)]

    @property
    def indexable(self) -> list[Page]:
        return [p for p in self.html_pages if p.indexable]


@dataclass(frozen=True)
class Check:
    id: str
    severity: str
    title: str
    why: str
    fix: str
    func: Callable[[Context], Iterable[Item]]
    quick: bool = True  # also runs in quick mode
    scope: str = "page"  # "sitemap": items are about sitemap entries (re-checked by re-reading it)


CHECKS: dict[str, Check] = {}


def check(
    id: str, severity: str, title: str, why: str, fix: str, quick: bool = True, scope: str = "page"
):
    assert severity in SEVERITIES, severity

    def register(func):
        if id in CHECKS:
            raise ValueError(f"duplicate check id {id}")
        CHECKS[id] = Check(id, severity, title, why, fix, func, quick, scope)
        return func

    return register


@dataclass
class Finding:
    check: Check
    items: list[Item]

    @property
    def id(self) -> str:
        return self.check.id

    @property
    def severity(self) -> str:
        return self.check.severity

    @property
    def count(self) -> int:
        return len(self.items)


def run_checks(ctx: Context) -> tuple[list[Finding], list[str]]:
    findings, errors = [], []
    for c in CHECKS.values():
        if ctx.crawl.mode == "quick" and not c.quick:
            continue
        try:
            items = list(c.func(ctx))
        except Exception as exc:  # one broken check must not sink the report
            log.exception("check %s failed", c.id)
            errors.append(f"Check '{c.id}' could not run: {exc}")
            continue
        if items:
            seen: dict[str, Item] = {}
            for item in items:
                seen.setdefault(item.ident, item)
            findings.append(Finding(c, list(seen.values())))
    findings.sort(key=lambda f: (SEVERITIES.index(f.severity), -f.count))
    return findings, errors


def group_by(pairs: Iterable[tuple[str, str]]) -> dict[str, list[str]]:
    """{key: [pages...]} preserving first-seen order."""
    out: dict[str, list[str]] = {}
    for key, page in pairs:
        pages = out.setdefault(key, [])
        if page not in pages:
            pages.append(page)
    return out


def on_pages(pages: list[str], limit: int = 3) -> str:
    shown = ", ".join(pages[:limit])
    more = f" and {len(pages) - limit} more" if len(pages) > limit else ""
    return f"on {len(pages)} page{'s' if len(pages) != 1 else ''}: {shown}{more}"
