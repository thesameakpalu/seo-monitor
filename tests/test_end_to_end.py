"""Crawl the fake site, check every audit issue is caught, then verify fixes show as resolved."""

from __future__ import annotations

from seo_monitor.audit import audit, urls_to_recheck
from seo_monitor.checks import Context, run_checks
from seo_monitor.config import Profile
from seo_monitor.crawl import CrawlResult, crawl
from seo_monitor.fetch import Fetcher
from seo_monitor.store import Store

from .fakesite import PRESIGNED, SITE, WWW, build_routes, fixed_routes, transport


def profile(**kw) -> Profile:
    return Profile(name="example", url=f"{SITE}/", delay=0, **kw)


async def do_crawl(routes, mode="full", log=None, recheck=(), **kw) -> CrawlResult:
    result = CrawlResult(profile=profile(**kw), mode=mode, recheck=list(recheck))
    fetcher = Fetcher("SEO-Monitor/test", delay=0, transport=transport(routes, log))
    try:
        await crawl(result, fetcher)
    finally:
        await fetcher.close()
    return result


def by_id(findings):
    return {f.id: f for f in findings}


def urls(finding):
    return {i.url for i in finding.items}


async def test_catches_every_audit_issue():
    result = await do_crawl(build_routes())
    assert result.complete
    found = by_id(run_checks(Context(result))[0])

    # Leftover demo pages listed in the sitemap
    assert f"{SITE}/header/demo-header/" in urls(found["sitemap_junk_urls"])
    assert f"{SITE}/sample-page/" in urls(found["sitemap_junk_urls"])
    # Sitemap host differs from canonical/preferred host; those URLs redirect
    assert any("www.example.org" in i.detail for i in found["sitemap_host_mismatch"].items)
    assert f"{WWW}/services/" in urls(found["sitemap_url_redirects"])
    # Canonical points to another host, and to a host that redirects
    assert f"{SITE}/about/" in urls(found["canonical_host_mismatch"])
    assert f"{SITE}/services/" in urls(found["canonical_target_error"])
    # Both hosts serve 200 with self-canonicals
    dup = {i.url: i.detail for i in found["host_duplicate"].items}
    assert f"{WWW}/" in dup and "self-canonical" in dup[f"{WWW}/"]
    # http redirect goes through two hops
    assert "http://example.org/" in urls(found["host_redirect_chain"])
    # Empty junk page
    assert f"{SITE}/location/-1" in urls(found["empty_page"])
    # Template placeholder and demo text
    details = " ".join(i.detail for i in found["placeholder_text"].items)
    assert "{{ start_date_html }}" in details
    assert f"{SITE}/sample-page/" in urls(found["placeholder_text"])
    # Footer links to "#" and javascript:
    nav = " ".join(i.detail for i in found["dead_nav_links"].items)
    assert '"Privacy Policy" → #' in nav and "javascript:" in nav
    # Missing meta descriptions / OG tags
    assert f"{SITE}/about/" in urls(found["meta_description_missing"])
    assert f"{SITE}/location/-1" in urls(found["og_tags_missing"])
    # Presigned S3 image URLs
    assert f"{SITE}/" in urls(found["presigned_urls"])
    # And the rest
    assert f"{SITE}/gone/" in urls(found["sitemap_url_error"])
    assert f"{SITE}/members/" in urls(found["sitemap_url_noindex"])
    assert f"{SITE}/broken-link/" in urls(found["broken_internal_links"])
    assert f"{SITE}/old/" in urls(found["redirect_chain"])
    assert f"{SITE}/event/" in urls(found["jsonld_invalid"])
    assert any("Organization: logo" in i.detail for i in found["jsonld_missing_properties"].items)
    assert f"{SITE}/photo.png" in urls(found["image_too_large"])
    assert f"{SITE}/photo.png" in urls(found["image_missing_alt"])
    # Nothing critical on this site
    assert not [f for f in found.values() if f.severity == "critical"]


async def test_never_fetches_presigned_or_unsafe_urls():
    routes = build_routes()
    routes[f"{SITE}/"] = (
        routes[f"{SITE}/"][0],
        routes[f"{SITE}/"][1],
        routes[f"{SITE}/"][2].replace(
            "</main>",
            '<a href="/shop/?add-to-cart=12">buy</a>' '<a href="/wp-admin/">admin</a></main>',
        ),
    )
    log: list[str] = []
    result = await do_crawl(routes, log=log)
    assert all(line.startswith("GET ") for line in log)
    assert not any("add-to-cart" in line or "wp-admin" in line for line in log)
    assert not any(PRESIGNED.split("?")[0] in line for line in log)
    assert f"{SITE}/shop/?add-to-cart=12" in result.skipped_unsafe


async def test_respects_robots_and_page_limit():
    routes = build_routes()
    routes[f"{SITE}/robots.txt"] = (
        200,
        {"content-type": "text/plain"},
        f"User-agent: *\nDisallow: /*-1$\nSitemap: {SITE}/sitemap_index.xml",
    )
    result = await do_crawl(routes, max_pages=5)
    assert f"{SITE}/location/-1" in result.blocked_by_robots
    assert f"{SITE}/location/-1" not in result.pages
    assert len(result.pages) <= 5 + 2  # limit, plus redirect finals already counted


async def test_quick_mode_is_small():
    log: list[str] = []
    result = await do_crawl(build_routes(), mode="quick", log=log, quick_max_pages=4)
    assert len(result.pages) <= 4
    findings, errors = run_checks(Context(result))
    assert not errors
    assert "broken_internal_links" not in by_id(findings)  # full-only check


async def test_second_run_marks_fixes_resolved(tmp_path):
    store = Store(tmp_path / "h.db")
    first = audit(await do_crawl(build_routes()), store, tmp_path / "reports")
    assert not first.has_baseline
    assert first.report_path.exists() and first.email_path.exists()

    recheck = urls_to_recheck(store, "example.org", "full")
    assert f"{SITE}/location/-1" in recheck
    second = audit(
        await do_crawl(fixed_routes(), recheck=recheck),
        store,
        tmp_path / "reports",
        note="after fix",
    )
    assert second.has_baseline
    status = {k: c.status for k, c in second.changes.items()}
    for fixed in [
        "sitemap_junk_urls",
        "placeholder_text",
        "empty_page",
        "canonical_host_mismatch",
        "canonical_target_error",
        "sitemap_host_mismatch",
        "jsonld_invalid",
        "sitemap_url_noindex",
        "host_duplicate",
    ]:
        assert status.get(fixed) == "resolved", (fixed, status.get(fixed))
    # Footer "#" links were not fixed
    assert status["dead_nav_links"] == "persisting"
    assert second.score > first.score

    html = second.report_path.read_text()
    assert "Resolved since last run" in html and "after fix" in html
    store.close()


async def test_critical_issues():
    routes = build_routes()
    routes[f"{SITE}/robots.txt"] = (
        200,
        {"content-type": "text/plain"},
        "User-agent: *\nDisallow: /\n",
    )
    routes[f"{SITE}/"] = (
        200,
        {"content-type": "text/html"},
        '<html><head><title>x</title><meta name="robots" content="noindex">'
        "</head><body><h1>x</h1></body></html>",
    )
    routes[f"{SITE}/sitemap.xml"] = (500, {}, "")
    result = await do_crawl(routes, sitemaps=[f"{SITE}/sitemap.xml"])
    found = by_id(run_checks(Context(result))[0])
    for check_id in ["robots_blocks_site", "homepage_noindex", "sitemap_unreachable"]:
        assert check_id in found
    assert result.pages.keys() == {f"{SITE}/"}  # Disallow: / stops the crawl politely


async def test_homepage_down():
    result = await do_crawl({})
    found = by_id(run_checks(Context(result))[0])
    assert "homepage_not_200" in found
