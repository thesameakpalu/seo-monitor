"""Run history in a local SQLite file (data/history.db, git-ignored)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .checks import Finding, Previous
from .crawl import CrawlResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    site        TEXT NOT NULL,
    start_url   TEXT NOT NULL,
    mode        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    complete    INTEGER NOT NULL,
    pages       INTEGER NOT NULL,
    requests    INTEGER NOT NULL,
    score       INTEGER,
    counts      TEXT,
    note        TEXT,
    robots_txt  TEXT,
    report_path TEXT,
    email_path  TEXT
);
CREATE INDEX IF NOT EXISTS runs_by_site ON runs (site, mode, id);
CREATE TABLE IF NOT EXISTS findings (
    run_id   INTEGER NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    check_id TEXT NOT NULL,
    severity TEXT NOT NULL,
    title    TEXT NOT NULL,
    status   TEXT,
    items    TEXT NOT NULL,
    PRIMARY KEY (run_id, check_id)
);
CREATE TABLE IF NOT EXISTS pages (
    run_id    INTEGER NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    url       TEXT NOT NULL,
    final_url TEXT NOT NULL,
    status    INTEGER,
    noindex   INTEGER NOT NULL,
    indexable INTEGER NOT NULL,
    title     TEXT,
    PRIMARY KEY (run_id, url)
);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def previous_run(self, site: str, mode: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM runs WHERE site = ? AND mode = ? AND complete = 1 "
            "ORDER BY id DESC LIMIT 1",
            (site, mode),
        ).fetchone()

    def load_previous(self, run_id: int) -> tuple[Previous, dict[str, dict]]:
        run = self.get_run(run_id)
        pages = {
            r["url"]: {
                "status": r["status"],
                "noindex": bool(r["noindex"]),
                "indexable": bool(r["indexable"]),
            }
            for r in self.db.execute("SELECT * FROM pages WHERE run_id = ?", (run_id,))
        }
        return Previous(robots_txt=run["robots_txt"], pages=pages), self.findings(run_id)

    def findings(self, run_id: int) -> dict[str, dict]:
        return {
            r["check_id"]: {
                "severity": r["severity"],
                "title": r["title"],
                "status": r["status"],
                "items": json.loads(r["items"]),
            }
            for r in self.db.execute("SELECT * FROM findings WHERE run_id = ?", (run_id,))
        }

    def save_run(
        self,
        crawl: CrawlResult,
        findings: list[Finding],
        statuses: dict[str, str],
        score: int,
        counts: dict[str, int],
        note: str | None,
    ) -> int:
        with self.db:
            cur = self.db.execute(
                """INSERT INTO runs (site, start_url, mode, started_at, finished_at, complete,
                   pages, requests, score, counts, note, robots_txt)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    crawl.site,
                    crawl.start_url,
                    crawl.mode,
                    crawl.started_at.isoformat(),
                    crawl.finished_at.isoformat() if crawl.finished_at else None,
                    int(crawl.complete),
                    len(crawl.pages),
                    crawl.requests,
                    score,
                    json.dumps(counts),
                    note,
                    crawl.robots.text if crawl.robots and crawl.robots.found else None,
                ),
            )
            run_id = cur.lastrowid
            self.db.executemany(
                "INSERT INTO findings VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id,
                        f.id,
                        f.severity,
                        f.check.title,
                        statuses.get(f.id),
                        json.dumps(
                            [{"url": i.url, "detail": i.detail, "key": i.key} for i in f.items]
                        ),
                    )
                    for f in findings
                ],
            )
            self.db.executemany(
                "INSERT OR IGNORE INTO pages VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id,
                        p.url,
                        p.final_url,
                        p.status,
                        int(p.noindex),
                        int(p.indexable),
                        p.doc.title if p.doc else None,
                    )
                    for p in crawl.pages.values()
                ],
            )
        return run_id

    def set_paths(self, run_id: int, report_path: Path, email_path: Path) -> None:
        with self.db:
            self.db.execute(
                "UPDATE runs SET report_path = ?, email_path = ? WHERE id = ?",
                (str(report_path), str(email_path), run_id),
            )

    def get_run(self, run_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()

    def history(self, site: str | None = None, limit: int = 20) -> list[sqlite3.Row]:
        if site:
            return self.db.execute(
                "SELECT * FROM runs WHERE site = ? ORDER BY id DESC LIMIT ?", (site, limit)
            ).fetchall()
        return self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
