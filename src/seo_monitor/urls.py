"""URL helpers."""

from __future__ import annotations

from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize(url: str) -> str:
    """Canonical form for comparison: lower-case scheme/host, no default port, no fragment."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    netloc = host
    if parts.port and parts.port != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"
    path = (parts.path or "/").replace(" ", "%20")
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def resolve(base: str, href: str) -> str | None:
    """Absolute, normalized http(s) URL for an href, or None for other schemes."""
    href = (href or "").strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
        return None
    try:
        url = urljoin(base, href)
    except ValueError:
        return None
    if urlsplit(url).scheme not in ("http", "https"):
        return None
    return normalize(url)


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def bare_host(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def path_and_query(url: str) -> str:
    parts = urlsplit(url)
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def same_page(a: str | None, b: str | None) -> bool:
    """Same document ignoring scheme and www/non-www (host mismatches are reported separately)."""
    if not a or not b:
        return False
    pa, pb = urlsplit(normalize(a)), urlsplit(normalize(b))
    return (bare_host(pa.hostname or ""), pa.path, pa.query) == (
        bare_host(pb.hostname or ""),
        pb.path,
        pb.query,
    )


def is_presigned(url: str) -> bool:
    """Signed, expiring object-storage URLs (S3, GCS, Azure SAS)."""
    try:
        keys = {k.lower() for k in parse_qs(urlsplit(url).query, keep_blank_values=True)}
    except ValueError:
        return False
    return bool(
        keys & {"x-amz-signature", "x-amz-expires", "x-goog-signature", "x-goog-expires"}
        or {"sig", "se"} <= keys
    )
