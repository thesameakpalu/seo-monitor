"""Command line: `monitor run <url-or-profile>` and friends."""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import webbrowser
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from .audit import Outcome, audit, urls_to_recheck
from .checks import CHECKS, SEVERITIES
from .config import load_config, resolve, user_agent
from .crawl import CrawlResult, crawl
from .emailer import EmailNotConfigured, send_report
from .fetch import Fetcher
from .store import Store

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="On-demand SEO health monitor. Crawls any site, reports issues and what "
    "changed since your last run.",
)
console = Console()
SEV_STYLE = {"critical": "bold red", "high": "dark_orange", "medium": "yellow", "low": "grey58"}
MIN_DELAY = 0.2


def _home() -> Path:
    return Path(os.getenv("MONITOR_HOME", ".")).resolve()


def _setup() -> tuple[Path, Store]:
    home = _home()
    load_dotenv(home / ".env")
    logging.basicConfig(
        level=logging.WARNING, handlers=[RichHandler(console=console)], format="%(message)s"
    )
    return home, Store(home / "data" / "history.db")


async def _crawl_all(results: list[CrawlResult], progress: Progress) -> None:
    async def one(result: CrawlResult) -> None:
        task = progress.add_task("", site=result.site, pages=0, current="starting")

        def update(r: CrawlResult, current: str) -> None:
            progress.update(task, pages=len(r.pages), current=current[-80:])

        p = result.profile
        fetcher = Fetcher(user_agent(), delay=p.delay, timeout=p.timeout)
        try:
            await crawl(result, fetcher, update)
        finally:
            await fetcher.close()
            progress.update(
                task, pages=len(result.pages), current="done" if result.complete else "stopped"
            )

    outcomes = await asyncio.gather(*(one(r) for r in results), return_exceptions=True)
    for r, exc in zip(results, outcomes, strict=True):
        if isinstance(exc, Exception):
            logging.getLogger("seo_monitor").error(
                "Crawl of %s failed: %s", r.site, exc, exc_info=exc
            )


def _print_outcome(o: Outcome) -> None:
    score_style = "green" if o.score >= 80 else "yellow" if o.score >= 50 else "red"
    was = f" (was {o.prev_score})" if o.prev_score is not None else ""
    status = "" if o.complete else " [yellow](incomplete)[/]"
    console.rule(f"[bold]{o.site}[/] — health [{score_style}]{o.score}[/]{was}{status}")

    crit = [f for f in o.findings if f.severity == "critical"]
    for f in crit:
        console.print(
            f"[bold red]CRITICAL[/] {f.check.title}: "
            + "; ".join(f"{i.url} {i.detail}".strip() for i in f.items[:3])
        )

    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("Severity")
    table.add_column("Issue")
    table.add_column("Count", justify="right")
    if o.has_baseline:
        table.add_column("Change")
    for f in o.findings:
        row = [f"[{SEV_STYLE[f.severity]}]{f.severity}[/]", f.check.title, str(f.count)]
        if o.has_baseline:
            ch = o.changes.get(f.id)
            label = ch.status if ch else ""
            if ch and ch.status == "worsened":
                label += f" (+{len(ch.new_items)})"
            style = (
                "red"
                if label.startswith(("new", "worsened"))
                else "green" if label.startswith("improved") else "dim"
            )
            row.append(f"[{style}]{label}[/]")
        table.add_row(*row)
    if o.findings:
        console.print(table)
    else:
        console.print("[green]No issues found.[/]")

    resolved = [c for c in o.changes.values() if c.status == "resolved"]
    if resolved:
        console.print("[green]Resolved since last run:[/] " + ", ".join(c.title for c in resolved))
    if not o.has_baseline:
        console.print("[dim]First run of this mode for this site — saved as the baseline.[/]")
    console.print(f"Report: [link={o.report_path.as_uri()}]{o.report_path}[/link]")


def _email(subject: str, email_path: Path, report_path: Path) -> None:
    try:
        to = send_report(subject, email_path, report_path)
        console.print(f"[green]Emailed to {', '.join(to)}[/]")
    except EmailNotConfigured as exc:
        console.print(f"[yellow]Email not sent: {exc}[/]")
    except Exception as exc:  # SMTP errors should never lose the report
        console.print(f"[red]Email failed: {exc}[/]")


@app.command()
def run(
    targets: Annotated[
        list[str] | None,
        typer.Argument(help="URLs/domains (e.g. example.com) or profile names from sites.yaml"),
    ] = None,
    all_sites: Annotated[bool, typer.Option("--all", help="Every profile in sites.yaml")] = False,
    quick: Annotated[
        bool,
        typer.Option(
            "--quick", "-q", help="Homepage, robots.txt, sitemaps, key URLs and a sample only"
        ),
    ] = False,
    max_pages: Annotated[int | None, typer.Option("--max-pages", "-n")] = None,
    delay: Annotated[float | None, typer.Option(help="Seconds between requests per host")] = None,
    note: Annotated[
        str | None, typer.Option(help="Saved with the run, e.g. 'after sitemap fix'")
    ] = None,
    email: Annotated[bool, typer.Option("--email", help="Email the report (SMTP in .env)")] = False,
    open_report: Annotated[bool, typer.Option("--open/--no-open")] = True,
    config: Annotated[Path, typer.Option(help="Profiles file")] = Path("sites.yaml"),
):
    """Crawl one or more sites and report issues."""
    home, store = _setup()
    config = config if config.is_absolute() else home / config
    names = list(targets or [])
    if all_sites:
        names += [n for n in load_config(config)[1] if n not in names]
    if not names:
        raise typer.BadParameter("give a URL or profile name, or --all")

    results = []
    for name in names:
        try:
            profile = resolve(name, config)
        except ValueError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(2) from exc
        if max_pages:
            profile.max_pages = profile.quick_max_pages = max_pages
        if delay is not None:
            profile.delay = max(delay, MIN_DELAY)
        mode = "quick" if quick else "full"
        results.append(
            CrawlResult(
                profile=profile, mode=mode, recheck=urls_to_recheck(store, profile.site, mode)
            )
        )

    console.print(f"User-Agent: [dim]{user_agent()}[/]")
    if not os.getenv("MONITOR_CONTACT"):
        console.print("[dim]Tip: set MONITOR_CONTACT in .env so site owners can identify you.[/]")
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.fields[site]}"),
        TextColumn("{task.fields[pages]:>4} pages"),
        TimeElapsedColumn(),
        TextColumn("[dim]{task.fields[current]}"),
        console=console,
    )
    try:
        with progress:
            asyncio.run(_crawl_all(results, progress))
    except KeyboardInterrupt:
        console.print("[yellow]Stopped — saving partial results (marked incomplete).[/]")

    for result in results:
        if result.home is None:
            console.print(f"[red]{result.site}: nothing was crawled.[/]")
            continue
        outcome = audit(result, store, home / "reports", note)
        _print_outcome(outcome)
        if open_report:
            webbrowser.open(outcome.report_path.as_uri())
        if email:
            _email(outcome.subject, outcome.email_path, outcome.report_path)
    store.close()


def _pick_run(store: Store, ref: str | None):
    if ref and ref.isdigit():
        row = store.get_run(int(ref))
    else:
        rows = store.history(ref, limit=1) if ref else store.history(limit=1)
        row = rows[0] if rows else None
    if row is None:
        console.print("[red]No matching run.[/]")
        raise typer.Exit(1)
    return row


@app.command()
def history(
    site: Annotated[str | None, typer.Argument(help="Site, e.g. gtdc.gov.gh")] = None,
    limit: int = 20,
):
    """List past runs."""
    _, store = _setup()
    table = Table("Run", "Site", "Mode", "Started", "Pages", "Score", "Issues (C/H/M/L)", "Note")
    for r in store.history(site, limit):
        counts = json.loads(r["counts"] or "{}")
        issues = "/".join(str(counts.get(s, 0)) for s in SEVERITIES)
        mode = r["mode"] + ("" if r["complete"] else " (incomplete)")
        table.add_row(
            str(r["id"]),
            r["site"],
            mode,
            r["started_at"][:16].replace("T", " "),
            str(r["pages"]),
            str(r["score"]),
            issues,
            r["note"] or "",
        )
    console.print(table)


@app.command("open")
def open_cmd(
    ref: Annotated[str | None, typer.Argument(help="Run id or site; default latest")] = None,
):
    """Open a saved report in the browser."""
    _, store = _setup()
    row = _pick_run(store, ref)
    webbrowser.open(Path(row["report_path"]).as_uri())
    console.print(row["report_path"])


@app.command("email")
def email_cmd(
    ref: Annotated[str | None, typer.Argument(help="Run id or site; default latest")] = None,
):
    """Email a saved report."""
    _, store = _setup()
    row = _pick_run(store, ref)
    subject = f"SEO health {row['site']}: {row['score']}/100"
    _email(subject, Path(row["email_path"]), Path(row["report_path"]))


@app.command()
def export(
    ref: Annotated[str | None, typer.Argument(help="Run id or site; default latest")] = None,
):
    """Export every issue and affected URL of a run to CSV."""
    home, store = _setup()
    row = _pick_run(store, ref)
    path = home / "reports" / row["site"] / f"run{row['id']}_issues.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["severity", "check_id", "issue", "status", "url", "detail"])
        for check_id, f in store.findings(row["id"]).items():
            for item in f["items"]:
                w.writerow(
                    [
                        f["severity"],
                        check_id,
                        f["title"],
                        f["status"] or "",
                        item["url"],
                        item["detail"],
                    ]
                )
    console.print(str(path))


@app.command()
def checks():
    """List every check."""
    table = Table("Id", "Severity", "Issue", "Quick")
    for c in CHECKS.values():
        table.add_row(
            c.id, f"[{SEV_STYLE[c.severity]}]{c.severity}[/]", c.title, "yes" if c.quick else ""
        )
    console.print(table)


if __name__ == "__main__":
    app()
