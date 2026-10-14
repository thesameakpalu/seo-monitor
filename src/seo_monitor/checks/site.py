"""Site-wide checks: homepage, robots.txt, sitemap files."""

from __future__ import annotations

import difflib

from .base import Context, Item, check


@check(
    "homepage_not_200",
    "critical",
    "Homepage does not load",
    "If the homepage fails, visitors and Google cannot reach the site.",
    "Check hosting, DNS, SSL and firewall/CDN rules. If a firewall blocks bots, allow this "
    "crawler.",
)
def homepage_not_200(ctx: Context):
    home = ctx.crawl.home
    if home and home.fetch.ok:
        return
    detail = home.fetch.describe() if home else "not fetched"
    if (
        home
        and home.status in (403, 429, 503)
        and "cloudflare" in home.fetch.headers.get("server", "").lower()
    ):
        detail += " — Cloudflare may be blocking this crawler"
    yield Item(ctx.crawl.start_url, detail)


@check(
    "homepage_noindex",
    "critical",
    "Homepage tells Google not to index it",
    "A noindex on the homepage removes the site's most important page from search.",
    "Remove 'noindex' from the meta robots tag / X-Robots-Tag header "
    "(WordPress: Settings → Reading → uncheck 'Discourage search engines').",
)
def homepage_noindex(ctx: Context):
    home = ctx.crawl.home
    if home and home.is_ok_html and home.noindex:
        yield Item(home.final_url, f"robots directives: {home.robots_directives}")


@check(
    "robots_blocks_site",
    "critical",
    "robots.txt blocks Google from the whole site",
    "'Disallow: /' for Googlebot or all user agents stops search engines crawling every page.",
    "Remove 'Disallow: /' from robots.txt (often left over from a staging site).",
)
def robots_blocks_site(ctx: Context):
    robots = ctx.crawl.robots
    if robots and robots.found and not robots.allowed(ctx.crawl.start_url, "googlebot"):
        yield Item(robots.url, "Googlebot is disallowed from the homepage")


@check(
    "sitemap_unreachable",
    "critical",
    "A declared sitemap cannot be read",
    "Search engines use sitemaps to find pages. A broken sitemap hides new pages.",
    "Make sure every sitemap listed in robots.txt (and any sitemap index) returns 200 and "
    "valid XML.",
)
def sitemap_unreachable(ctx: Context):
    for sm in ctx.crawl.sitemaps:
        if not sm.ok:
            yield Item(sm.url, f"{sm.error or f'HTTP {sm.status}'} (listed via {sm.source})")


@check(
    "sitemap_missing",
    "medium",
    "No XML sitemap found",
    "Without a sitemap, search engines rely only on links to discover pages.",
    "Publish a sitemap (e.g. Yoast/RankMath on WordPress) and reference it in robots.txt.",
)
def sitemap_missing(ctx: Context):
    if not ctx.crawl.sitemaps:
        yield Item("", "No sitemap in robots.txt and none at /sitemap.xml, /sitemap_index.xml")


@check(
    "sitemap_file_redirects",
    "medium",
    "Sitemap file redirects",
    "Sitemap URLs submitted to Google should resolve directly.",
    "Reference the final sitemap URL in robots.txt and Search Console.",
)
def sitemap_file_redirects(ctx: Context):
    for sm in ctx.crawl.sitemaps:
        if sm.redirected:
            yield Item(sm.url, f"redirects to {sm.final_url}")


@check(
    "robots_missing",
    "low",
    "robots.txt is missing or unreadable",
    "robots.txt tells crawlers what to skip and where the sitemap is.",
    "Serve a robots.txt at the site root with a Sitemap: line.",
)
def robots_missing(ctx: Context):
    robots = ctx.crawl.robots
    if robots and not robots.found:
        yield Item(robots.url, robots.error or f"HTTP {robots.status}")


@check(
    "robots_no_sitemap",
    "low",
    "robots.txt does not list a sitemap",
    "A Sitemap: line lets every search engine find the sitemap without manual submission.",
    "Add 'Sitemap: https://<host>/sitemap_index.xml' (the real sitemap URL) to robots.txt.",
)
def robots_no_sitemap(ctx: Context):
    robots = ctx.crawl.robots
    if robots and robots.found and not robots.sitemaps:
        yield Item(robots.url, "no Sitemap: line")


@check(
    "robots_changed",
    "medium",
    "robots.txt changed since the last run",
    "An unnoticed robots.txt edit can block important sections of the site.",
    "Review the diff below and confirm the change was intended.",
)
def robots_changed(ctx: Context):
    robots = ctx.crawl.robots
    if not ctx.prev or ctx.prev.robots_txt is None or not robots:
        return
    old, new = ctx.prev.robots_txt.strip(), robots.text.strip()
    if old != new:
        diff = difflib.unified_diff(
            old.splitlines(), new.splitlines(), "previous", "now", lineterm=""
        )
        yield Item(robots.url, "\n".join(diff), key="robots.txt")
