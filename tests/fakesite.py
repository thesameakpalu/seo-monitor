"""An in-memory website reproducing the problems found in the GTDC audit.

Served through httpx.MockTransport, so tests never touch the network.
"""

from __future__ import annotations

import copy

import httpx

from seo_monitor.urls import normalize

SITE = "https://example.org"
WWW = "https://www.example.org"
PRESIGNED = (
    "https://bucket.s3.amazonaws.com/img/hero.jpg?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Expires=86400&X-Amz-Signature=abc123"
)

FOOTER = """<footer class="site-footer"><a href="/about/">About</a>
<a href="#">Privacy Policy</a><a href="javascript:void(0)">Terms</a></footer>"""
HEADER = '<header><nav><a href="/">Home</a><a href="/about/">About</a></nav></header>'
WORDS = " ".join(f"word{i}" for i in range(200))


def page(
    title,
    body,
    *,
    desc="A useful description of this page.",
    canonical=None,
    robots=None,
    og=True,
    extra_head="",
):
    head = f"<title>{title}</title>" if title is not None else ""
    if desc:
        head += f'<meta name="description" content="{desc}">'
    if canonical:
        head += f'<link rel="canonical" href="{canonical}">'
    if robots:
        head += f'<meta name="robots" content="{robots}">'
    if og:
        head += (
            '<meta property="og:title" content="t"><meta property="og:description" '
            f'content="d"><meta property="og:image" content="{SITE}/og.jpg">'
        )
    html = (
        f"<!doctype html><html><head>{head}{extra_head}</head><body>{HEADER}"
        f"<main>{body}</main>{FOOTER}</body></html>"
    )
    return 200, {"content-type": "text/html; charset=utf-8"}, html


def redirect(to, code=301):
    return code, {"location": to}, ""


def build_routes() -> dict[str, tuple[int, dict, str]]:
    org_ld = (
        '<script type="application/ld+json">{"@context":"https://schema.org",'
        '"@type":"Organization","name":"Example","url":"https://example.org/"}</script>'
    )
    bad_ld = '<script type="application/ld+json">{"@type": "Event", "name": }</script>'
    routes = {
        f"{SITE}/": page(
            "Example Tourism",
            f"<h1>Welcome</h1><p>{WORDS}</p>"
            f'<img src="{PRESIGNED}" alt="hero"><img src="/photo.png">'
            '<a href="/old/">old</a><a href="/services/">services</a>'
            '<a href="/location/-1">loc</a><a href="/event/">event</a>',
            canonical=f"{SITE}/",
            extra_head=org_ld,
        ),
        f"{SITE}/robots.txt": (
            200,
            {"content-type": "text/plain"},
            "User-agent: *\nDisallow: /wp-admin/\n" f"Sitemap: {SITE}/sitemap_index.xml\n",
        ),
        f"{SITE}/sitemap_index.xml": (
            200,
            {"content-type": "application/xml"},
            '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"<sitemap><loc>{SITE}/page-sitemap.xml</loc></sitemap></sitemapindex>",
        ),
        f"{SITE}/page-sitemap.xml": (
            200,
            {"content-type": "application/xml"},
            '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(
                f"<url><loc>{u}</loc></url>"
                for u in [
                    f"{SITE}/",
                    f"{WWW}/about/",
                    f"{SITE}/header/demo-header/",
                    f"{SITE}/sample-page/",
                    f"{SITE}/location/-1",
                    f"{SITE}/gone/",
                    f"{SITE}/event/",
                    f"{SITE}/members/",
                    f"{WWW}/services/",
                ]
            )
            + "</urlset>",
        ),
        # Served on both hosts; canonical points to www (host mismatch).
        f"{SITE}/about/": page(
            "About us",
            f"<h1>About</h1><p>{WORDS} about</p>" '<a href="/broken-link/">broken</a>',
            canonical=f"{WWW}/about/",
            desc=None,
        ),
        f"{WWW}/about/": page(
            "About us", f"<h1>About</h1><p>{WORDS} about</p>", canonical=f"{WWW}/about/", desc=None
        ),
        # Canonical uses the www host, which redirects back: canonical target redirects.
        f"{SITE}/services/": page(
            "Services", f"<h1>Services</h1><p>{WORDS} services</p>", canonical=f"{WWW}/services/"
        ),
        f"{WWW}/services/": redirect(f"{SITE}/services/"),
        f"{SITE}/header/demo-header/": page(
            "Demo header", f"<h1>Raise Your Hands</h1><p>{WORDS} demo</p>"
        ),
        f"{SITE}/sample-page/": page(
            "Sample Page", f"<h1>Sample Page</h1><p>This is an example page. {WORDS} sample</p>"
        ),
        f"{SITE}/location/-1": page("Location", "<h1></h1>", og=False, desc=None),
        f"{SITE}/event/": page(
            "Event",
            f"<h1>Festival</h1><p>Starts {{{{ start_date_html }}}} {WORDS} event</p>",
            extra_head=bad_ld,
        ),
        f"{SITE}/members/": page(
            "Members", f"<h1>Members</h1><p>{WORDS} members</p>", robots="noindex, follow"
        ),
        f"{SITE}/old/": redirect(f"{SITE}/older/"),
        f"{SITE}/older/": redirect(f"{SITE}/about/"),
        f"{SITE}/og.jpg": (200, {"content-type": "image/jpeg", "content-length": "2048"}, ""),
        f"{SITE}/photo.png": (200, {"content-type": "image/png", "content-length": "512000"}, ""),
        # Host / scheme variants
        f"{WWW}/": page("Example Tourism", f"<h1>Welcome</h1><p>{WORDS}</p>", canonical=f"{WWW}/"),
        "http://example.org/": redirect("http://www.example.org/"),  # two hops to https
        "http://www.example.org/": redirect(f"{SITE}/"),
        "http://www.example.org/about/": redirect(f"{WWW}/about/"),
    }
    return routes


def fixed_routes() -> dict[str, tuple[int, dict, str]]:
    """The same site after the developers fixed most issues."""
    routes = build_routes()
    routes = copy.deepcopy(routes)
    for path in ["/sample-page/", "/header/demo-header/", "/location/-1"]:
        routes[f"{SITE}{path}"] = (404, {"content-type": "text/html"}, "<h1>Not found</h1>")
    routes[f"{SITE}/page-sitemap.xml"] = (
        200,
        {"content-type": "application/xml"},
        '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(
            f"<url><loc>{SITE}{p}</loc></url>" for p in ["/", "/about/", "/event/", "/services/"]
        )
        + "</urlset>",
    )
    routes[f"{SITE}/event/"] = page(
        "Event", f"<h1>Festival</h1><p>Starts 1 March {WORDS} event</p>"
    )
    routes[f"{SITE}/about/"] = page(
        "About us", f"<h1>About</h1><p>{WORDS} about</p>", canonical=f"{SITE}/about/"
    )
    routes[f"{SITE}/services/"] = page(
        "Services", f"<h1>Services</h1><p>{WORDS} services</p>", canonical=f"{SITE}/services/"
    )
    routes[f"{SITE}/"] = page(
        "Example Tourism",
        f"<h1>Welcome</h1><p>{WORDS}</p>"
        '<a href="/about/">about</a><a href="/services/">services</a>'
        '<a href="/event/">event</a>',
        canonical=f"{SITE}/",
    )
    for host_path in ["/", "/about/", "/services/", "/event/"]:
        routes[f"{WWW}{host_path}"] = redirect(f"{SITE}{host_path}")
        routes[f"http://example.org{host_path}"] = redirect(f"{SITE}{host_path}")
        routes[f"http://www.example.org{host_path}"] = redirect(f"{SITE}{host_path}")
    return routes


def transport(routes: dict[str, tuple[int, dict, str]], log: list[str] | None = None):
    table = {normalize(k): v for k, v in routes.items()}

    def handler(request: httpx.Request) -> httpx.Response:
        url = normalize(str(request.url))
        if log is not None:
            log.append(f"{request.method} {url}")
        entry = table.get(url, (404, {"content-type": "text/html"}, "nope"))
        if callable(entry):  # simulate slow/flaky servers: may raise or answer differently
            entry = entry(request)
        status, headers, body = entry
        return httpx.Response(status, headers=headers, content=body.encode())

    return httpx.MockTransport(handler)
