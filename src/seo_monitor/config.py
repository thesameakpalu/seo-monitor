"""Site profiles and defaults.

Any URL can be audited with the built-in defaults. ``sites.yaml`` is optional and only
adds named shortcuts (``monitor run gtm``) and per-site tuning.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

from .urls import bare_host, host_of, normalize

VERSION = "0.1.0"

# Generic theme/CMS leftovers. Site-specific terms belong in sites.yaml (extra_placeholders).
DEFAULT_PLACEHOLDERS = [
    "{{",
    "{%",
    "lorem ipsum",
    "sample page",
    "this is an example page",
    "hello world!",
    "just another wordpress site",
]

# URL fragments that should never appear in a sitemap (theme builder templates, archives).
DEFAULT_JUNK_SITEMAP_PATTERNS = [
    "/author/",
    "/elementor-hf/",
    "/elementskit-content/",
    "/elementskit-template/",
    "/e-landing-page/",
    "/header/",
    "/footer/",
    "/slider/",
    "/sliders/",
    "/attachment/",
    "/hello-world/",
    "/sample-page/",
    "/wp-content/",
]

# Never request URLs matching these: they can change state even over GET.
DEFAULT_SKIP_PATTERNS = [
    "add-to-cart=",
    "add_to_wishlist",
    "action=",
    "/wp-admin",
    "wp-login.php",
    "/logout",
    "/login",
    "/register",
    "/cart",
    "/checkout",
    "/my-account",
    "replytocom=",
]


@dataclass
class Profile:
    name: str
    url: str
    preferred_host: str | None = None  # "www" | "non-www"; default: wherever the homepage lands
    sitemaps: list[str] = field(default_factory=list)
    key_urls: list[str] = field(default_factory=list)
    max_pages: int = 500
    quick_max_pages: int = 40
    delay: float = 1.0
    timeout: float = 20.0
    concurrency: int = 3
    extra_placeholders: list[str] = field(default_factory=list)
    placeholder_whitelist: list[str] = field(default_factory=list)
    junk_sitemap_patterns: list[str] = field(default_factory=list)
    skip_patterns: list[str] = field(default_factory=list)
    thin_content_patterns: list[str] = field(default_factory=list)
    thin_words: int = 150
    empty_words: int = 30
    title_max: int = 65
    description_max: int = 160
    image_max_kb: int = 300
    image_checks: int = 150

    @property
    def site(self) -> str:
        return bare_host(host_of(self.url))

    @property
    def placeholders(self) -> list[str]:
        allowed = {w.lower() for w in self.placeholder_whitelist}
        terms = DEFAULT_PLACEHOLDERS + self.extra_placeholders
        return [t for t in dict.fromkeys(terms) if t.lower() not in allowed]

    @property
    def junk_patterns(self) -> list[str]:
        return list(dict.fromkeys(DEFAULT_JUNK_SITEMAP_PATTERNS + self.junk_sitemap_patterns))

    @property
    def all_skip_patterns(self) -> list[str]:
        return list(dict.fromkeys(DEFAULT_SKIP_PATTERNS + self.skip_patterns))


_FIELDS = {f.name for f in fields(Profile)}


def user_agent() -> str:
    # The standard crawler form (as Googlebot/Bingbot use): firewalls that refuse anything not
    # starting with "Mozilla/5.0" let it through, and it still names the bot honestly.
    contact = os.getenv("MONITOR_CONTACT", "").strip()
    ua = f"SEO-Monitor/{VERSION}"
    return (
        f"Mozilla/5.0 (compatible; {ua}; +{contact})"
        if contact
        else f"Mozilla/5.0 (compatible; {ua})"
    )


def _as_url(target: str) -> str:
    return normalize(target if "://" in target else f"https://{target}")


def _build(name: str, data: dict, defaults: dict) -> Profile:
    merged = {**defaults, **data}
    unknown = set(merged) - _FIELDS
    if unknown:
        raise ValueError(
            f"sites.yaml: unknown setting(s) for '{name}': {', '.join(sorted(unknown))}"
        )
    if "url" not in merged:
        raise ValueError(f"sites.yaml: site '{name}' needs a url")
    merged["url"] = _as_url(merged["url"])
    return Profile(name=name, **{k: v for k, v in merged.items() if k != "name"})


def load_config(path: Path) -> tuple[dict, dict[str, Profile]]:
    if not path.exists():
        return {}, {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    defaults = raw.get("defaults") or {}
    sites = {
        name: _build(name, data or {}, defaults) for name, data in (raw.get("sites") or {}).items()
    }
    return defaults, sites


def resolve(target: str, path: Path) -> Profile:
    """Turn a profile name or any URL/domain into a Profile."""
    defaults, sites = load_config(path)
    if target in sites:
        return sites[target]
    if "." not in target and "://" not in target:
        known = ", ".join(sites) or "none"
        raise ValueError(f"'{target}' is not a URL or a profile in {path} (profiles: {known})")
    url = _as_url(target)
    site = bare_host(host_of(url))
    for profile in sites.values():
        if profile.site == site:
            # Same site as a saved profile: keep its tuning but start where asked.
            profile.url = url
            return profile
    return _build(site, {"url": url}, {k: v for k, v in defaults.items() if k != "url"})
