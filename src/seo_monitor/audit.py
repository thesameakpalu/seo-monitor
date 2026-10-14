"""Turn a finished (or cancelled) crawl into checks, comparison, stored history and reports."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape

from .checks import CHECKS, SEVERITIES, Context, Finding, run_checks
from .compare import Change, compare, health_score, make_rechecked, severity_counts
from .crawl import CrawlResult
from .store import Store

ITEM_LIMIT = 200
_env = Environment(
    loader=PackageLoader("seo_monitor", "templates"), autoescape=select_autoescape(["html", "j2"])
)


@dataclass
class Outcome:
    run_id: int
    site: str
    score: int
    prev_score: int | None
    findings: list[Finding]
    changes: dict[str, Change]
    has_baseline: bool
    complete: bool
    report_path: Path
    email_path: Path

    @property
    def subject(self) -> str:
        crit = sum(1 for f in self.findings if f.severity == "critical")
        flag = f" — {crit} CRITICAL" if crit else ""
        return f"SEO health {self.site}: {self.score}/100{flag}"


def _local(dt: datetime | str | None) -> str:
    if dt is None:
        return ""
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return dt.astimezone().strftime("%d %b %Y %H:%M")


def urls_to_recheck(store: Store, site: str, mode: str) -> list[str]:
    """Page URLs flagged in the previous comparable run (images and host variants are re-tested
    every run anyway, and must not be fetched as pages)."""
    prev = store.previous_run(site, mode)
    if not prev:
        return []
    return [
        i["url"]
        for check_id, f in store.findings(prev["id"]).items()
        if check_id in CHECKS and CHECKS[check_id].scope in ("page", "sitemap")
        for i in f["items"]
        if i["url"]
    ]


def audit(crawl: CrawlResult, store: Store, reports_dir: Path, note: str | None = None) -> Outcome:
    prev_run = store.previous_run(crawl.site, crawl.mode)
    previous, prev_findings = store.load_previous(prev_run["id"]) if prev_run else (None, None)

    findings, errors = run_checks(Context(crawl=crawl, prev=previous))
    changes = compare(findings, prev_findings, make_rechecked(crawl))
    score = health_score(findings, max(len(crawl.pages), 1))
    counts = severity_counts(findings)
    run_id = store.save_run(
        crawl, findings, {k: c.status for k, c in changes.items()}, score, counts, note
    )

    rank = {s: i for i, s in enumerate(SEVERITIES)}
    worse = sorted(
        (c for c in changes.values() if c.status in ("new", "worsened")),
        key=lambda c: (rank[c.severity], -len(c.new_items)),
    )
    better = [c for c in changes.values() if c.status in ("improved", "resolved")]
    duration = (crawl.finished_at or crawl.started_at) - crawl.started_at
    home = crawl.home
    robots = crawl.robots
    data = {
        "run_id": run_id,
        "site": crawl.site,
        "start_url": crawl.start_url,
        "mode": crawl.mode,
        "mode_label": "Quick check" if crawl.mode == "quick" else "Full audit",
        "started": _local(crawl.started_at),
        "duration": f"{int(duration.total_seconds() // 60)}m {int(duration.total_seconds() % 60)}s",
        "pages": len(crawl.pages),
        "requests": crawl.requests,
        "note": note,
        "complete": crawl.complete,
        "score": score,
        "prev_score": prev_run["score"] if prev_run else None,
        "prev_started": _local(prev_run["started_at"]) if prev_run else None,
        "severities": SEVERITIES,
        "counts": counts,
        "findings": findings,
        "criticals": [f for f in findings if f.severity == "critical"],
        "changes": changes,
        "worse": worse,
        "better": better,
        "resolved": [c for c in better if c.status == "resolved"],
        "item_limit": ITEM_LIMIT,
        "home_status": (
            f"{home.url} → {home.fetch.describe()}"
            + (f" at {home.final_url}" if home.fetch.redirected else "")
            if home
            else "not fetched"
        ),
        "preferred_host": crawl.preferred_host
        + ("" if crawl.profile.preferred_host else " (where the homepage lands)"),
        "robots_status": (
            (f"{robots.url} — " + ("found" if robots.found else robots.error or "missing"))
            if robots
            else "not fetched"
        ),
        "sitemaps": crawl.sitemaps,
        "sitemap_url_count": len(crawl.sitemap_urls),
        "status_counts": sorted(
            Counter(str(p.status or "error") for p in crawl.pages.values()).items()
        ),
        "over_limit": len(crawl.over_limit),
        "blocked": len(crawl.blocked_by_robots),
        "skipped": len(crawl.skipped_unsafe),
        "errors": errors,
    }

    stamp = crawl.started_at.astimezone().strftime("%Y-%m-%d_%H%M")
    out = reports_dir / crawl.site
    out.mkdir(parents=True, exist_ok=True)
    report_path = out / f"{stamp}_{crawl.mode}_run{run_id}.html"
    email_path = out / f"{stamp}_{crawl.mode}_run{run_id}_email.html"
    report_path.write_text(_env.get_template("report.html.j2").render(**data), encoding="utf-8")
    email_path.write_text(_env.get_template("email.html.j2").render(**data), encoding="utf-8")
    store.set_paths(run_id, report_path, email_path)

    return Outcome(
        run_id,
        crawl.site,
        score,
        data["prev_score"],
        findings,
        changes,
        prev_run is not None,
        crawl.complete,
        report_path,
        email_path,
    )
