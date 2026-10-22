from __future__ import annotations

import gzip
from datetime import UTC, datetime, timedelta

from seo_monitor.checks import Item
from seo_monitor.checks.content import _snippet, _term_rx
from seo_monitor.compare import compare, health_score
from seo_monitor.config import Profile, resolve
from seo_monitor.parse import parse_html
from seo_monitor.robots import parse_robots
from seo_monitor.sitemap import parse_sitemap
from seo_monitor.urls import is_presigned, normalize, same_page


def test_normalize_and_same_page():
    assert normalize("HTTPS://Example.ORG:443/a?b=1#frag") == "https://example.org/a?b=1"
    assert normalize("https://example.org") == "https://example.org/"
    assert same_page("https://www.example.org/a/", "http://example.org/a/")
    assert not same_page("https://example.org/a", "https://example.org/a/")


def test_presigned_detection():
    assert is_presigned("https://b.s3.amazonaws.com/x.jpg?X-Amz-Expires=60&X-Amz-Signature=a")
    assert is_presigned("https://storage.googleapis.com/b/x?X-Goog-Signature=a")
    assert not is_presigned("https://cdn.example.org/x.jpg?w=300")


def test_robots_wildcards_and_precedence():
    r = parse_robots(
        "u",
        "User-agent: *\nDisallow: /*?s=\nDisallow: /private/\nAllow: /private/ok$\n"
        "Crawl-delay: 2\n\nUser-agent: Googlebot\nDisallow: /\nSitemap: https://x/s.xml",
    )
    assert not r.allowed("https://x/search?s=a")
    assert r.allowed("https://x/private/ok")
    assert not r.allowed("https://x/private/ok/more")
    assert r.allowed("https://x/page")
    assert not r.allowed("https://x/page", "googlebot")
    assert r.crawl_delay() == 2
    assert r.sitemaps == ["https://x/s.xml"]


def test_robots_empty_disallow_allows_everything():
    assert parse_robots("u", "User-agent: *\nDisallow:\n").allowed("https://x/anything")


def test_sitemap_parsing_index_gzip_and_broken_xml():
    idx = (
        b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<sitemap><loc>https://x/a.xml</loc></sitemap></sitemapindex>"
    )
    assert parse_sitemap(idx) == ("index", ["https://x/a.xml"])
    urlset = (
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc> https://x/p?a=1&amp;b=2 </loc></url></urlset>"
    )
    assert parse_sitemap(gzip.compress(urlset)) == ("urlset", ["https://x/p?a=1&b=2"])
    broken = b"<urlset><url><loc>https://x/a</loc></url><url><loc>https://x/b &amp; c</loc>"
    assert parse_sitemap(broken) == ("urlset", ["https://x/a", "https://x/b & c"])


HTML = """<html><head><title> Hello  </title><meta name="Description" content="desc">
<meta name="robots" content="NOINDEX"><link rel="canonical" href="/c">
<meta property="og:image" content="/og.png">
<script type="application/ld+json">{"@graph":[{"@type":"Organization","name":"x"},
  {"@type":["Hotel","LocalBusiness"]}]}</script>
</head><body>
<div data-elementor-type="header"><a href="#">Menu</a></div>
<div class="content"><h1>One</h1><h1>Two</h1><p>Main text here</p>
<img src="a.png" alt="A"><img src="data:image/gif;base64,x" data-src="/lazy.webp">
<img src="c.png" alt>
<script>var hidden = "{{ not visible }}";</script></div>
<footer><a href="javascript:void(0)">Terms</a><a href="/about">About</a></footer>
</body></html>"""


def test_parse_html():
    doc = parse_html(HTML, "https://x.org/p/")
    assert doc.title == "Hello"
    assert doc.meta_description == "desc"
    assert doc.meta_robots == "noindex"
    assert doc.canonical == "https://x.org/c"
    assert doc.og["og:image"] == "/og.png"
    assert len(doc.h1s) == 2
    assert [(lk.href, lk.region) for lk in doc.links] == [
        ("#", "header"),
        ("javascript:void(0)", "footer"),
        ("/about", "footer"),
    ]
    assert [(i.src, i.alt) for i in doc.images] == [
        ("https://x.org/p/a.png", "A"),
        ("https://x.org/lazy.webp", None),
        ("https://x.org/p/c.png", ""),
    ]
    assert doc.jsonld[0].types == ["Hotel", "LocalBusiness", "Organization"]
    assert "Main text here" in doc.main_text and "Menu" not in doc.main_text
    assert "Terms" in doc.visible_text and "not visible" not in doc.visible_text


def test_placeholder_word_boundaries():
    assert _term_rx("test").search("This is a test page")
    assert not _term_rx("test").search("the latest contest")
    assert _term_rx("{{").search("Starts {{ start_date_html }}")
    text = "Festival starts {{ start_date_html }} at the arena"
    m = _term_rx("{{").search(text)
    assert _snippet(text, m.start(), m.end()) == "{{ start_date_html }}"


def test_profile_whitelist():
    p = Profile(
        name="x",
        url="https://x/",
        extra_placeholders=["test", "donation"],
        placeholder_whitelist=["donation", "Sample Page"],
    )
    assert "test" in p.placeholders
    assert "donation" not in p.placeholders and "sample page" not in p.placeholders


def test_resolve_any_url_and_profiles(tmp_path):
    cfg = tmp_path / "sites.yaml"
    cfg.write_text(
        "defaults:\n  max_pages: 50\nsites:\n  gtm:\n    url: www.gtm.com.gh\n"
        "    preferred_host: www\n"
    )
    assert resolve("gtm", cfg).url == "https://www.gtm.com.gh/"
    other = resolve("example.com/blog", cfg)
    assert other.url == "https://example.com/blog" and other.max_pages == 50
    same_site = resolve("https://gtm.com.gh/listing/1", cfg)
    assert same_site.name == "gtm" and same_site.preferred_host == "www"


def _finding(check_id, items):
    from seo_monitor.checks import CHECKS, Finding

    return Finding(CHECKS[check_id], items)


def test_compare_statuses():
    prev = {
        "title_missing": {"severity": "high", "title": "t", "items": [{"url": "a"}, {"url": "b"}]},
        "h1_missing": {"severity": "medium", "title": "t", "items": [{"url": "c"}]},
        "h1_multiple": {"severity": "low", "title": "t", "items": [{"url": "not-crawled"}]},
        "og_tags_missing": {"severity": "low", "title": "t", "items": [{"url": "d"}]},
    }
    now = [
        _finding("title_missing", [Item("a")]),
        _finding("og_tags_missing", [Item("d"), Item("e")]),
        _finding("empty_page", [Item("f")]),
    ]
    checked = {"a", "b", "c", "d", "e", "f"}
    ch = compare(now, prev, lambda check_id, item: item.url in checked)
    assert ch["title_missing"].status == "improved"
    assert ch["og_tags_missing"].status == "worsened"
    assert ch["empty_page"].status == "new"
    assert ch["h1_missing"].status == "resolved"
    assert "h1_multiple" not in ch  # its page was not re-checked, so not claimed as fixed


def test_health_score():
    assert health_score([], 10) == 100
    assert health_score([_finding("homepage_not_200", [Item("x")])], 1) == 49
    assert health_score([_finding("title_missing", [Item("a")] * 10)], 10) == 92


def test_detects_client_side_rendering():
    shell = (
        '<html><head><title>Hi</title></head><body><header><a href="/">Home</a></header>'
        '<template data-dgst="BAILOUT_TO_CLIENT_SIDE_RENDERING"></template>'
        '<script src="/_next/static/chunks/main.js"></script></body></html>'
    )
    assert "client-side" in parse_html(shell, "https://x/").js_hint
    spa = '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'
    assert "#root" in parse_html(spa, "https://x/").js_hint
    ssr = '<html><body><div id="__next"><main><h1>Hello</h1></main></div></body></html>'
    assert parse_html(ssr, "https://x/").js_hint is None


def test_status_classification():
    from seo_monitor.fetch import FetchResult

    def res(status, **kw):
        return FetchResult(url="u", final_url="u", status=status, **kw)

    assert res(404).broken and res(410).broken and not res(404).transient
    for busy in (429, 500, 503, 508, 509, 530):  # 508: shared hosting "resource limit reached"
        assert res(busy).transient and not res(busy).broken, busy
    assert res(403).transient and not res(403).broken  # unconfirmed refusal: maybe a firewall
    assert res(403, confirmed=True).broken
    assert not res(500, confirmed=True).broken  # a server error is never a "broken link"
    assert "resource limit" in res(508).describe()
    assert res(None, network_error=True, error="timed out").transient
    assert res(None, error="SSL/TLS problem").broken


def test_retry_after_header():
    from email.utils import format_datetime

    from seo_monitor.fetch import Fetcher, FetchResult

    def wait(value):
        return Fetcher._retry_after(
            FetchResult(url="u", final_url="u", headers={"retry-after": value})
        )

    assert wait("7") == 7.0 and wait("999") == 60.0
    soon = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    assert 25 <= wait(soon) <= 30
    assert wait("garbage") is None
