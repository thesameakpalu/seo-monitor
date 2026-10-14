"""Internal links, redirects, dead navigation links, development URLs."""

from __future__ import annotations

import re

from ..urls import host_of
from .base import Context, Item, check, group_by, on_pages

DEV_HOST_RX = re.compile(
    r"(^localhost$|^127\.|^0\.0\.0\.0$|\.(test|local|localhost|invalid|example)$|"
    r"^(staging|stage|dev|test)\.)"
)


@check(
    "broken_internal_links",
    "high",
    "Internal links to broken pages",
    "Links to missing pages frustrate visitors and leak link value.",
    "Update or remove each link, or 301-redirect the dead URL to its replacement.",
    quick=False,
)
def broken_internal_links(ctx: Context):
    pairs = []
    for page in ctx.html_pages:
        for link in page.doc.links:
            if not ctx.crawl.is_internal(link.url):
                continue
            res = ctx.crawl.lookup(link.url)
            if res and (res.loop or res.status is None or res.status >= 400):
                pairs.append((link.url, page.final_url))
    for target, pages in group_by(pairs).items():
        yield Item(target, f"{ctx.crawl.lookup(target).describe()} — linked {on_pages(pages)}")


@check(
    "internal_links_to_redirects",
    "low",
    "Internal links that go through a redirect",
    "Linking straight to the final URL is faster and avoids redirect chains.",
    "Update links to point at the final URL.",
    quick=False,
)
def internal_links_to_redirects(ctx: Context):
    pairs = []
    for page in ctx.html_pages:
        for link in page.doc.links:
            if ctx.crawl.is_internal(link.url):
                res = ctx.crawl.lookup(link.url)
                if res and res.redirected and res.ok:
                    pairs.append((link.url, page.final_url))
    for target, pages in group_by(pairs).items():
        yield Item(target, f"→ {ctx.crawl.lookup(target).final_url} — linked {on_pages(pages)}")


@check(
    "redirect_chain",
    "medium",
    "Redirect chains (more than one hop)",
    "Each hop adds latency and Google may stop following long chains.",
    "Redirect the first URL straight to the final destination.",
)
def redirect_chain(ctx: Context):
    for page in ctx.pages:
        if len(page.fetch.chain) > 1 and not page.fetch.loop:
            hops = " → ".join(u for u, _ in page.fetch.chain)
            yield Item(page.url, f"{hops} → {page.final_url}")


@check(
    "redirect_loop",
    "high",
    "Redirect loops",
    "The URL never resolves; browsers show an error.",
    "Fix the conflicting redirect rules (often www/https rules in the CDN and the server "
    "fighting).",
)
def redirect_loop(ctx: Context):
    for page in ctx.pages:
        if page.fetch.loop:
            yield Item(page.url, " → ".join(u for u, _ in page.fetch.chain))


@check(
    "dead_nav_links",
    "medium",
    "Header/footer links that go nowhere ('#' or javascript:)",
    "Navigation links that lead nowhere look broken to visitors and pass no value to any page.",
    "Point each link at a real page, or remove it. Menu parents that only open a sub-menu "
    "are fine to ignore.",
)
def dead_nav_links(ctx: Context):
    pairs = []
    for page in ctx.html_pages:
        for link in page.doc.links:
            href = link.href.strip()
            if link.region != "body" and (href == "#" or href.lower().startswith("javascript:")):
                pairs.append((f"{link.region}|{link.text}|{href}", page.final_url))
    for key, pages in group_by(pairs).items():
        region, text, href = key.split("|", 2)
        yield Item(
            pages[0], f'{region} link "{text or "(no text)"}" → {href} — {on_pages(pages)}', key=key
        )


@check(
    "dev_urls",
    "high",
    "Links or images pointing to development/staging hosts",
    "Links to localhost, *.test or staging hosts are broken for visitors and leak the dev setup.",
    "Search-and-replace the development host in the database/content and rebuild caches.",
)
def dev_urls(ctx: Context):
    pairs = []
    for page in ctx.html_pages:
        urls = [link.url for link in page.doc.links] + [img.src for img in page.doc.images]
        urls += [page.canonical, page.doc.og.get("og:image")]
        for url in urls:
            host = host_of(url or "")
            if host and DEV_HOST_RX.search(host):
                pairs.append((host, page.final_url))
    for host, pages in group_by(pairs).items():
        yield Item(pages[0], f"{host} referenced {on_pages(pages)}", key=host)
