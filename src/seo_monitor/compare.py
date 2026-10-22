"""Change detection against the previous run, and the health score."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .checks import CHECKS, SEVERITIES, Finding, Item
from .crawl import CrawlResult

# Health score: start at 100, subtract per open issue
#   weight[severity] × (0.5 + 0.5 × share of crawled pages affected)
# so an issue costs between half and all of its weight. Any critical issue caps the score at 49.
WEIGHTS = {"critical": 25, "high": 8, "medium": 3, "low": 1}


@dataclass
class Change:
    check_id: str
    title: str
    severity: str
    status: str  # new | worsened | improved | persisting | resolved
    new_items: list[Item] = field(default_factory=list)
    fixed_items: list[Item] = field(default_factory=list)
    count: int = 0
    previous_count: int = 0


def _item(d: dict) -> Item:
    return Item(url=d["url"], detail=d.get("detail", ""), key=d.get("key"))


Rechecked = Callable[[str, Item], bool]


def make_rechecked(crawl: CrawlResult) -> Rechecked:
    """Was a previously reported item looked at again in this run?

    Page items: only if the URL was fetched again and answered (a timeout proves nothing).
    Sitemap items: also if the sitemaps were read and the URL is no longer listed. Site-wide
    items (no URL): always.
    """
    unanswered = {u for u, p in crawl.pages.items() if p.fetch.transient}
    unanswered |= {u for u, r in crawl.resources.items() if r.transient}
    checked = crawl.checked_urls - unanswered
    sitemaps_read = bool(crawl.sitemaps) and all(sm.ok for sm in crawl.sitemaps)

    def rechecked(check_id: str, item: Item) -> bool:
        if not item.url or item.url in checked:
            return True
        check = CHECKS.get(check_id)
        return bool(
            check
            and check.scope == "sitemap"
            and sitemaps_read
            and item.url not in crawl.sitemap_urls
        )

    return rechecked


def compare(
    findings: list[Finding], previous: dict[str, dict] | None, rechecked: Rechecked
) -> dict[str, Change]:
    """Status per check id. Without a previous run every finding is 'new'.

    A previously reported item only counts as fixed if it was re-checked this run, so a
    smaller crawl does not produce false 'resolved' results.
    """
    previous = previous or {}

    changes: dict[str, Change] = {}
    for f in findings:
        before = previous.get(f.id)
        ch = Change(f.id, f.check.title, f.severity, "new", count=f.count)
        if before is None:
            ch.new_items = list(f.items)
        else:
            old = [_item(d) for d in before["items"]]
            old_ids = {i.ident for i in old}
            now_ids = {i.ident for i in f.items}
            ch.previous_count = len(old)
            ch.new_items = [i for i in f.items if i.ident not in old_ids]
            ch.fixed_items = [i for i in old if i.ident not in now_ids and rechecked(f.id, i)]
            ch.status = (
                "worsened" if ch.new_items else "improved" if ch.fixed_items else "persisting"
            )
        changes[f.id] = ch

    for check_id, before in previous.items():
        if check_id in changes:
            continue
        old = [_item(d) for d in before["items"]]
        fixed = [i for i in old if rechecked(check_id, i)]
        if fixed:
            check = CHECKS.get(check_id)
            changes[check_id] = Change(
                check_id,
                check.title if check else before["title"],
                before["severity"],
                "resolved",
                fixed_items=fixed,
                previous_count=len(old),
            )
    return changes


def health_score(findings: list[Finding], pages: int) -> int:
    total = 0.0
    for f in findings:
        share = min(1.0, f.count / max(pages, 1))
        total += WEIGHTS[f.severity] * (0.5 + 0.5 * share)
    score = max(0, round(100 - total))
    if any(f.severity == "critical" for f in findings):
        score = min(score, 49)
    return score


def severity_counts(findings: list[Finding]) -> dict[str, int]:
    return {s: sum(1 for f in findings if f.severity == s) for s in SEVERITIES}
