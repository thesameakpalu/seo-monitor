"""Sitemap contents, canonicals, indexability and changes since the last run."""

from __future__ import annotations

from ..urls import normalize
from .base import Context, Item, check


def _sitemap_pages(ctx: Context):
    for url in ctx.crawl.sitemap_urls:
        page = ctx.crawl.pages.get(url)
        if page:
            yield url, page


@check(
    "sitemap_url_error",
    "high",
    "Sitemap lists pages that are broken",
    "Every URL in a sitemap should return 200. Errors waste crawl budget and erode trust in "
    "the sitemap.",
    "Remove or fix these URLs; make sure deleted content drops out of the sitemap automatically.",
    scope="sitemap",
)
def sitemap_url_error(ctx: Context):
    for url, page in _sitemap_pages(ctx):
        if not page.fetch.ok:
            yield Item(url, page.fetch.describe())


@check(
    "sitemap_url_redirects",
    "medium",
    "Sitemap lists URLs that redirect",
    "Sitemaps should list final URLs only; redirects tell Google the sitemap is stale or on the "
    "wrong host.",
    "List the redirect targets instead (often fixed by correcting the site/host URL setting).",
    scope="sitemap",
)
def sitemap_url_redirects(ctx: Context):
    for url, page in _sitemap_pages(ctx):
        if page.fetch.redirected:
            yield Item(url, f"redirects to {page.final_url}")


@check(
    "sitemap_url_noindex",
    "high",
    "Sitemap lists pages marked noindex",
    "Asking Google to index a page and telling it not to at the same time is a contradiction.",
    "Either remove noindex or exclude these pages from the sitemap.",
    scope="sitemap",
)
def sitemap_url_noindex(ctx: Context):
    for url, page in _sitemap_pages(ctx):
        if page.is_ok_html and page.noindex:
            yield Item(url, page.robots_directives)


@check(
    "sitemap_url_canonicalised",
    "medium",
    "Sitemap lists pages whose canonical points elsewhere",
    "Only canonical URLs belong in a sitemap.",
    "List the canonical URL instead, or fix the canonical tag.",
    scope="sitemap",
)
def sitemap_url_canonicalised(ctx: Context):
    for url, page in _sitemap_pages(ctx):
        if page.is_ok_html and page.canonicalised_elsewhere:
            yield Item(url, f"canonical → {page.canonical}")


@check(
    "sitemap_junk_urls",
    "high",
    "Sitemap lists template, demo or archive URLs",
    "Theme-builder templates (headers, footers, sliders), author archives and demo pages are not "
    "content. Listing them invites Google to index junk.",
    "Exclude these post types/taxonomies from the sitemap (Yoast/RankMath: Search Appearance → "
    "Content types) and delete leftover demo content.",
    scope="sitemap",
)
def sitemap_junk_urls(ctx: Context):
    patterns = ctx.profile.junk_patterns
    for url in ctx.crawl.sitemap_urls:
        hit = next((p for p in patterns if p in url.lower()), None)
        if hit:
            yield Item(url, f"matches '{hit}'")


@check(
    "canonical_missing",
    "low",
    "Pages without a canonical tag",
    "A self-referencing canonical protects against duplicates from URL parameters and host "
    "variants.",
    'Output <link rel="canonical" href="…"> on every indexable page.',
)
def canonical_missing(ctx: Context):
    for page in ctx.html_pages:
        if not page.noindex and not page.doc.canonicals:
            yield Item(page.final_url)


@check(
    "canonical_multiple",
    "medium",
    "Pages with more than one canonical tag",
    "With conflicting canonicals Google may ignore all of them.",
    "Output exactly one canonical (often two SEO plugins or a theme + plugin both add one).",
)
def canonical_multiple(ctx: Context):
    for page in ctx.html_pages:
        if len(set(page.doc.canonicals)) > 1:
            yield Item(page.final_url, " | ".join(dict.fromkeys(page.doc.canonicals)))


@check(
    "canonical_target_error",
    "high",
    "Canonical points to a URL that redirects or is broken",
    "A canonical must point at a live 200 page. If it redirects or errors, Google ignores it — "
    "typical when the canonical uses a host the site does not actually serve.",
    "Point canonicals at the final, live URL on the preferred host.",
)
def canonical_target_error(ctx: Context):
    for page in ctx.html_pages:
        c = page.canonical
        if not c or normalize(c) == page.final_url:
            continue
        res = ctx.crawl.lookup(c)
        if res is None:
            continue
        if res.redirected or not res.ok:
            what = f"redirects to {res.final_url}" if res.ok else res.describe()
            yield Item(page.final_url, f"canonical {c} {what}")


@check(
    "became_noindex",
    "high",
    "Pages that became noindex since the last run",
    "A page that was indexable last time and now says noindex will drop out of Google.",
    "Confirm the change was intended; otherwise remove the noindex.",
)
def became_noindex(ctx: Context):
    if not ctx.prev:
        return
    for page in ctx.html_pages:
        before = ctx.prev.pages.get(page.url)
        if before and before.get("indexable") and page.noindex:
            yield Item(page.url, page.robots_directives)


@check(
    "became_broken",
    "high",
    "Pages that worked last run and are broken now",
    "These URLs returned 200 in the previous run.",
    "Restore the page or 301-redirect it to the closest replacement.",
)
def became_broken(ctx: Context):
    if not ctx.prev:
        return
    for page in ctx.pages:
        before = ctx.prev.pages.get(page.url)
        if before and before.get("status") == 200 and not page.fetch.ok:
            yield Item(page.url, f"was 200, now {page.fetch.describe()}")
