"""www / non-www, http / https and canonical-host consistency."""

from __future__ import annotations

from collections import Counter

from ..urls import host_of, same_page
from .base import Context, Item, check


def _variants_by_sample(ctx: Context):
    out: dict[str, list] = {}
    for v in ctx.crawl.variants:
        out.setdefault(v.sample, []).append(v)
    return out


def _chain_text(v) -> str:
    hops = " → ".join(f"{u} ({s})" for u, s in v.fetch.chain)
    end = f"{v.fetch.final_url} ({v.fetch.describe()})"
    return f"{hops} → {end}" if hops else end


@check(
    "host_duplicate",
    "high",
    "Site is served on both www and non-www (or http and https)",
    "Two hosts serving the same pages, each claiming to be canonical, split ranking signals "
    "and let Google index duplicates.",
    "301-redirect every non-preferred variant (http, and www or non-www) to the preferred "
    "https host in a single hop.",
    scope="resource",
)
def host_duplicate(ctx: Context):
    pref = ctx.crawl.preferred_host
    for v in ctx.crawl.variants:
        if host_of(v.url) == pref and v.url.startswith("https://"):
            continue
        if v.fetch.ok and not v.fetch.redirected:
            note = (
                "self-canonical"
                if v.canonical and host_of(v.canonical) == host_of(v.url)
                else (f"canonical → {v.canonical}" if v.canonical else "no canonical")
            )
            yield Item(v.url, f"serves 200 directly instead of redirecting to {pref} ({note})")


@check(
    "host_redirect_chain",
    "medium",
    "Host/HTTPS redirect takes more than one hop",
    "Each extra redirect hop slows users and wastes crawl budget.",
    "Redirect http:// and the non-preferred host straight to the final https:// URL.",
    scope="resource",
)
def host_redirect_chain(ctx: Context):
    for v in ctx.crawl.variants:
        if len(v.fetch.chain) > 1:
            yield Item(v.url, _chain_text(v))


@check(
    "host_redirect_wrong_target",
    "high",
    "Host variant redirects to the wrong place",
    "Variants of a page should redirect to the same page on the preferred host.",
    "Make the www/non-www and http redirect preserve the path and land on the preferred host.",
    scope="resource",
)
def host_redirect_wrong_target(ctx: Context):
    pref = ctx.crawl.preferred_host
    for v in ctx.crawl.variants:
        if not v.fetch.redirected or not v.fetch.ok:
            continue
        final = v.fetch.final_url
        if host_of(final) != pref or not same_page(final, v.sample):
            yield Item(v.url, _chain_text(v))


@check(
    "host_variant_unreachable",
    "low",
    "A www/non-www or http variant does not respond",
    "People type and link to both forms; they should land on the site rather than an error.",
    "Add DNS for the missing host and redirect it to the preferred host.",
    scope="resource",
)
def host_variant_unreachable(ctx: Context):
    for sample, variants in _variants_by_sample(ctx).items():
        if sample != ctx.crawl.start_url:
            continue  # one report per host is enough
        for v in variants:
            if v.fetch.status is None or (v.fetch.status >= 400):
                yield Item(v.url, v.fetch.describe())


@check(
    "canonical_host_mismatch",
    "high",
    "Canonical tags point to a different host than the page is served on",
    "When pages say 'my real address is on another host', Google may index the other host, "
    "or ignore the canonical entirely if that host redirects back.",
    "Generate canonicals from the preferred host (WordPress: Settings → General → Site Address; "
    "Laravel: APP_URL; Next.js: metadataBase).",
)
def canonical_host_mismatch(ctx: Context):
    for page in ctx.html_pages:
        c = page.canonical
        if c and ctx.crawl.is_internal(c) and host_of(c) != host_of(page.final_url):
            yield Item(page.final_url, f"canonical → {c}")


@check(
    "sitemap_host_mismatch",
    "high",
    "Sitemap lists URLs on a different host than the preferred one",
    "Sitemap URLs should be exactly the canonical URLs. Mixed hosts send Google conflicting "
    "signals and every listed URL becomes a redirect.",
    "Make the sitemap generator use the preferred host (same setting as the canonical URL).",
)
def sitemap_host_mismatch(ctx: Context):
    pref = ctx.crawl.preferred_host
    hosts = Counter(host_of(u) for u in ctx.crawl.sitemap_urls)
    for host, n in hosts.items():
        if host != pref:
            yield Item(
                "",
                f"{n} of {len(ctx.crawl.sitemap_urls)} sitemap URLs use {host} "
                f"(preferred host is {pref})",
                key=host,
            )
