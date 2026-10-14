"""robots.txt parsing with Google-style matching (wildcards, `$`, longest match wins)."""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field

from .urls import path_and_query

AGENT_TOKEN = "seo-monitor"


@dataclass
class Group:
    agents: list[str] = field(default_factory=list)
    rules: list[tuple[bool, str]] = field(default_factory=list)  # (allow, pattern)
    crawl_delay: float | None = None


@dataclass
class Robots:
    url: str
    status: int | None = None
    text: str = ""
    error: str | None = None
    groups: list[Group] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.status == 200

    def _group(self, agent: str) -> Group | None:
        agent = agent.lower()
        best, best_len = None, -1
        for g in self.groups:
            for a in g.agents:
                if a != "*" and a in agent and len(a) > best_len:
                    best, best_len = g, len(a)
        if best:
            return best
        return next((g for g in self.groups if "*" in g.agents), None)

    def allowed(self, url: str, agent: str = AGENT_TOKEN) -> bool:
        # A robots.txt that cannot be read at all (5xx/network) is treated as "allow" for this
        # on-demand audit tool, but it is reported as an issue.
        group = self._group(agent)
        if not group:
            return True
        path = path_and_query(url)
        best_allow, best_len = True, -1
        for allow, pattern in group.rules:
            longer = len(pattern) > best_len or (len(pattern) == best_len and allow)
            if longer and _match(pattern, path):
                best_allow, best_len = allow, len(pattern)
        return best_allow

    def crawl_delay(self, agent: str = AGENT_TOKEN) -> float | None:
        group = self._group(agent)
        return group.crawl_delay if group else None


_pattern_cache: dict[str, re.Pattern] = {}


def _match(pattern: str, path: str) -> bool:
    rx = _pattern_cache.get(pattern)
    if rx is None:
        anchored = pattern.endswith("$")
        body = re.escape(pattern.rstrip("$")).replace(r"\*", ".*")
        rx = _pattern_cache[pattern] = re.compile(body + ("$" if anchored else ""))
    return rx.match(path) is not None


def parse_robots(url: str, text: str, status: int | None = 200) -> Robots:
    robots = Robots(url=url, status=status, text=text)
    current: Group | None = None
    last_was_agent = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (s.strip() for s in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if not last_was_agent or current is None:
                current = Group()
                robots.groups.append(current)
            current.agents.append(value.lower())
            last_was_agent = True
            continue
        last_was_agent = False
        if key == "sitemap" and value:
            robots.sitemaps.append(value)
        elif current is None:
            continue
        elif key in ("allow", "disallow") and value:
            current.rules.append((key == "allow", value))
        elif key == "crawl-delay":
            with contextlib.suppress(ValueError):
                current.crawl_delay = float(value)
    return robots
