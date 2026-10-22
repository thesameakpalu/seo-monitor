"""Crawl the fake site, check every audit issue is caught, then verify fixes show as resolved."""

from __future__ import annotations

import httpx

from seo_monitor.audit import audit, urls_to_recheck
from seo_monitor.checks import Context, run_checks
from seo_monitor.config import Profile
from seo_monitor.crawl import CrawlResult, crawl
from seo_monitor.fetch import Fetcher
from seo_monitor.store import Store

from .fakesite import (
    PRESIGNED,
    SITE,
    WORDS,
    WWW,
    build_routes,
    fixed_routes,
    page,
    transport,
)


def profile(**kw) -> Profile:
    return Profile(name="example", url=f"{SITE}/", delay=0, **kw)


async def do_crawl(routes, mode="full", log=None, recheck=(), **kw) -> CrawlResult:
    result = CrawlResult(profile=profile(**kw), mode=mode, recheck=list(recheck))
    fetcher = Fetcher("SEO-Monitor/test", delay=0, retry_wait=0, transport=transport(routes, log))
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
            '<a href="/shop/?add-to-cart=12">buy</a><a href="/wp-admin/">admin</a></main>',
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


async def test_js_rendered_pages_are_not_called_empty():
    routes = build_routes()
    routes[f"{SITE}/app/"] = (
        200,
        {"content-type": "text/html"},
        '<html><head><title>App</title><meta name="description" content="d">'
        '</head><body><div id="root"></div><script src="/a.js"></script>'
        "</body></html>",
    )
    routes[f"{SITE}/"] = (
        routes[f"{SITE}/"][0],
        routes[f"{SITE}/"][1],
        routes[f"{SITE}/"][2].replace("</main>", '<a href="/app/">app</a></main>'),
    )
    found = by_id(run_checks(Context(await do_crawl(routes)))[0])
    assert f"{SITE}/app/" in urls(found["client_side_rendered"])
    for check_id in ["empty_page", "h1_missing", "thin_content"]:
        assert f"{SITE}/app/" not in urls(found.get(check_id) or type("F", (), {"items": []}))


async def test_recheck_does_not_hide_image_or_host_issues(tmp_path):
    store = Store(tmp_path / "h.db")
    audit(await do_crawl(build_routes()), store, tmp_path / "r")
    recheck = urls_to_recheck(store, "example.org", "full")
    assert f"{SITE}/photo.png" not in recheck and "http://example.org/" not in recheck
    second = audit(await do_crawl(build_routes(), recheck=recheck), store, tmp_path / "r")
    status = {k: c.status for k, c in second.changes.items()}
    for check_id in ["image_too_large", "image_legacy_format", "host_redirect_chain"]:
        assert status[check_id] == "persisting", (check_id, status[check_id])
    assert not [c for c in second.changes.values() if c.status in ("new", "resolved")]
    store.close()


async def test_timeouts_are_not_reported_as_broken_links():

    calls = {"flaky": 0}
    html = (
        "<html><head><title>T</title></head><body><main><h1>T</h1><p>words</p></main></body></html>"
    )

    def flaky(request):  # times out twice, then answers: a busy server, not a dead page
        calls["flaky"] += 1
        if calls["flaky"] <= 2:
            raise httpx.ReadTimeout("slow", request=request)
        return 200, {"content-type": "text/html"}, html

    def down(request):
        raise httpx.ReadTimeout("slow", request=request)

    routes = build_routes()
    routes[f"{SITE}/flaky/"] = flaky
    routes[f"{SITE}/down/"] = down
    routes[f"{SITE}/"] = (
        routes[f"{SITE}/"][0],
        routes[f"{SITE}/"][1],
        routes[f"{SITE}/"][2].replace(
            "</main>",
            '<a href="/flaky/">a</a><a href="/down/">b</a><a href="/gone/">c</a></main>',
        ),
    )
    result = await do_crawl(routes)
    found = by_id(run_checks(Context(result))[0])

    assert result.pages[f"{SITE}/flaky/"].fetch.ok
    broken = urls(found["broken_internal_links"])
    assert f"{SITE}/gone/" in broken  # a real 404 is still reported
    assert f"{SITE}/flaky/" not in broken and f"{SITE}/down/" not in broken
    unreachable = urls(found["unreachable_urls"])
    assert unreachable == {f"{SITE}/down/"}
    assert result.pages[f"{SITE}/down/"].fetch.attempts == 5  # 3 tries + 2 when confirming


def _with_links(routes, *paths):
    status, headers, body = routes[f"{SITE}/"]
    links = "".join(f'<a href="{p}">x</a>' for p in paths)
    routes[f"{SITE}/"] = (status, headers, body.replace("</main>", links + "</main>"))
    return routes


async def test_overloaded_hosting_is_not_reported_as_broken_links():
    """What GTDC's shared hosting does under a crawl: 508 'resource limit reached' and firewall
    403s for pages that load fine moments later."""
    html = page("Post", f"<h1>Post</h1><p>{WORDS}</p>")
    calls: dict[str, int] = {}

    def busy_then_fine(code, fails):
        def answer(request):
            key = str(request.url)
            calls[key] = calls.get(key, 0) + 1
            return (code, {"content-type": "text/html"}, "busy") if calls[key] <= fails else html

        return answer

    routes = _with_links(build_routes(), "/limited/", "/firewalled/", "/projects")
    routes[f"{SITE}/limited/"] = busy_then_fine(508, 3)  # outlasts the crawl's own retries
    routes[f"{SITE}/firewalled/"] = busy_then_fine(403, 3)
    result = await do_crawl(routes)
    found = by_id(run_checks(Context(result))[0])

    broken = urls(found["broken_internal_links"])
    assert f"{SITE}/projects" in broken  # a real 404 still is
    assert f"{SITE}/limited/" not in broken and f"{SITE}/firewalled/" not in broken
    assert result.pages[f"{SITE}/limited/"].fetch.ok
    assert result.pages[f"{SITE}/firewalled/"].fetch.ok
    assert "unreachable_urls" not in found


async def test_refusal_counts_as_broken_only_while_the_rest_of_the_site_loads():
    forbidden = (403, {"content-type": "text/html"}, "Forbidden")
    routes = _with_links(build_routes(), "/private/")
    routes[f"{SITE}/private/"] = forbidden
    result = await do_crawl(routes)
    found = by_id(run_checks(Context(result))[0])
    assert f"{SITE}/private/" in urls(found["broken_internal_links"])
    assert result.pages[f"{SITE}/private/"].fetch.confirmed

    # A firewall that starts refusing every page part-way: nothing can be confirmed.
    seen = {"n": 0}
    base = transport(_with_links(build_routes(), "/private/"))

    def firewall(request):
        seen["n"] += 1
        # Only the (cached) homepage, robots.txt and sitemaps keep loading.
        url = str(request.url)
        if seen["n"] > 6 and url != f"{SITE}/" and not url.endswith((".txt", ".xml")):
            return httpx.Response(403, content=b"Forbidden")
        return base.handler(request)

    result = CrawlResult(profile=profile(), mode="full")
    fetcher = Fetcher("t", delay=0, retry_wait=0, transport=httpx.MockTransport(firewall))
    await crawl(result, fetcher)
    await fetcher.close()
    found = by_id(run_checks(Context(result))[0])
    refused = {u for u, p in result.pages.items() if p.status == 403}
    assert refused
    assert not refused & {i.url for f in [found.get("broken_internal_links")] if f for i in f.items}
    assert refused <= urls(found["unreachable_urls"])


async def test_connection_blip_at_start_does_not_report_missing_robots_or_sitemap():
    calls = {"n": 0}
    base = transport(build_routes())

    def blip(request):  # our own DNS/connection fails for the first few requests
        calls["n"] += 1
        if calls["n"] <= 4:
            raise httpx.ConnectError("nodename nor servname provided", request=request)
        return base.handler(request)

    result = CrawlResult(profile=profile(), mode="full")
    fetcher = Fetcher("t", delay=0, retry_wait=0, transport=httpx.MockTransport(blip))
    await crawl(result, fetcher)
    await fetcher.close()
    found = by_id(run_checks(Context(result))[0])
    assert result.complete and result.home.fetch.ok
    for check_id in ("homepage_not_200", "robots_missing", "sitemap_missing"):
        assert check_id not in found, check_id
    assert result.sitemap_urls


async def test_unreachable_site_reports_only_the_homepage():
    def down(request):
        raise httpx.ConnectError("nodename nor servname provided", request=request)

    result = CrawlResult(profile=profile(), mode="full")
    fetcher = Fetcher("t", delay=0, retry_wait=0, transport=httpx.MockTransport(down))
    await crawl(result, fetcher)
    await fetcher.close()
    found = by_id(run_checks(Context(result))[0])
    assert not result.complete
    assert set(found) == {"homepage_not_200"}
