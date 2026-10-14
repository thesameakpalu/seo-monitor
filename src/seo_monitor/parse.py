"""Extract SEO-relevant data from an HTML document."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from selectolax.lexbor import LexborHTMLParser, LexborNode

from .urls import resolve

INVISIBLE = {"script", "style", "noscript", "template", "svg", "iframe", "head", "object"}
CHROME_TAGS = {"header", "footer", "nav"}
CHROME_ROLES = {"banner", "navigation", "contentinfo"}
FOOTER_HINTS = {"footer", "site-footer", "elementor-location-footer", "colophon"}
HEADER_HINTS = {
    "header",
    "site-header",
    "elementor-location-header",
    "navbar",
    "main-navigation",
    "masthead",
}
_WORD_RX = re.compile(r"\w+", re.UNICODE)
_SPACE_RX = re.compile(r"\s+")


@dataclass
class Link:
    href: str  # as written
    url: str | None  # absolute & normalized, None for #, javascript:, mailto: ...
    text: str
    region: str  # header | footer | body


@dataclass
class Image:
    src: str  # absolute
    alt: str | None  # None = attribute missing


@dataclass
class JsonLd:
    raw: str
    data: object | None
    error: str | None

    @property
    def types(self) -> list[str]:
        return sorted({t for node in iter_nodes(self.data) for t in node_types(node)})


@dataclass
class Document:
    title: str | None = None
    title_count: int = 0
    meta_description: str | None = None
    meta_robots: str = ""
    canonicals: list[str] = field(default_factory=list)
    h1s: list[str] = field(default_factory=list)
    og: dict[str, str] = field(default_factory=dict)
    hreflang: list[tuple[str, str]] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    images: list[Image] = field(default_factory=list)
    jsonld: list[JsonLd] = field(default_factory=list)
    visible_text: str = ""  # everything a visitor can read (for placeholder detection)
    main_text: str = ""  # body minus header/footer/nav (for thin/duplicate content)

    @property
    def canonical(self) -> str | None:
        return self.canonicals[0] if self.canonicals else None

    @property
    def word_count(self) -> int:
        return len(_WORD_RX.findall(self.main_text))

    @property
    def content_hash(self) -> str:
        return hashlib.sha1(self.main_text.lower().encode()).hexdigest()


def iter_nodes(data: object):
    """Yield every dict in a JSON-LD structure (handles lists and @graph)."""
    stack = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            yield item
            stack.extend(v for v in item.values() if isinstance(v, (dict, list)))
        elif isinstance(item, list):
            stack.extend(item)


def node_types(node: dict) -> list[str]:
    t = node.get("@type")
    if isinstance(t, str):
        return [t]
    if isinstance(t, list):
        return [x for x in t if isinstance(x, str)]
    return []


def _clean(text: str | None) -> str:
    return _SPACE_RX.sub(" ", text or "").strip()


def _tokens(node: LexborNode) -> set[str]:
    attrs = node.attributes
    return set((attrs.get("class") or "").lower().split()) | {(attrs.get("id") or "").lower()}


def _chrome_kind(node: LexborNode) -> str | None:
    """'header'/'footer' if this element is site chrome, else None."""
    tag = node.tag
    attrs = node.attributes
    role = (attrs.get("role") or "").lower()
    elementor = (attrs.get("data-elementor-type") or "").lower()
    toks = _tokens(node)
    if tag == "footer" or role == "contentinfo" or elementor == "footer" or toks & FOOTER_HINTS:
        return "footer"
    if (
        tag in ("header", "nav")
        or role in ("banner", "navigation")
        or elementor == "header"
        or toks & HEADER_HINTS
    ):
        return "header"
    return None


def _region(node: LexborNode) -> str:
    n = node.parent
    while n is not None and n.tag not in ("body", "html"):
        kind = _chrome_kind(n)
        if kind:
            return kind
        n = n.parent
    return "body"


def _collect_text(root: LexborNode, skip_chrome: bool) -> str:
    parts: list[str] = []
    stack = [root]
    while stack:
        node = stack.pop()
        tag = node.tag
        if tag == "-text":
            parts.append(node.text_content or "")
            continue
        if tag in INVISIBLE or tag.startswith("-"):
            continue
        if skip_chrome and node is not root and _chrome_kind(node):
            continue
        children = list(node.iter(include_text=True))
        stack.extend(reversed(children))
    return _clean(" ".join(parts))


def parse_html(html: str, base_url: str) -> Document:
    tree = LexborHTMLParser(html)
    doc = Document()
    base = base_url
    base_el = tree.css_first("base[href]")
    if base_el:
        base = resolve(base_url, base_el.attributes.get("href") or "") or base_url

    titles = [t for t in tree.css("title") if not _inside(t, "svg")]
    doc.title_count = len(titles)
    if titles:
        doc.title = _clean(titles[0].text())

    for meta in tree.css("meta"):
        attrs = meta.attributes
        name = (attrs.get("name") or attrs.get("property") or "").strip().lower()
        content = _clean(attrs.get("content"))
        if name == "description" and doc.meta_description is None:
            doc.meta_description = content
        elif name in ("robots", "googlebot"):
            doc.meta_robots = f"{doc.meta_robots}, {content.lower()}".strip(", ")
        elif name.startswith("og:") and name not in doc.og:
            doc.og[name] = content

    for link in tree.css("link[rel]"):
        attrs = link.attributes
        rels = (attrs.get("rel") or "").lower().split()
        href = attrs.get("href") or ""
        if "canonical" in rels:
            doc.canonicals.append(resolve(base, href) or href.strip())
        if "alternate" in rels and attrs.get("hreflang"):
            doc.hreflang.append((attrs["hreflang"], resolve(base, href) or href))

    doc.h1s = [_clean(h.text()) for h in tree.css("h1")]

    for a in tree.css("a[href]"):
        href = a.attributes.get("href") or ""
        doc.links.append(
            Link(href=href, url=resolve(base, href), text=_clean(a.text())[:80], region=_region(a))
        )

    for img in tree.css("img"):
        attrs = img.attributes
        src = attrs.get("src") or ""
        lazy = attrs.get("data-src") or attrs.get("data-lazy-src")
        if lazy and (not src or src.startswith("data:")):
            src = lazy
        url = resolve(base, src)
        if url:
            alt = (attrs["alt"] or "") if "alt" in attrs else None
            doc.images.append(Image(src=url, alt=alt))

    for script in tree.css("script"):
        if (script.attributes.get("type") or "").strip().lower() != "application/ld+json":
            continue
        raw = (script.text() or "").strip()
        try:
            doc.jsonld.append(JsonLd(raw=raw, data=json.loads(raw), error=None))
        except ValueError as exc:
            doc.jsonld.append(JsonLd(raw=raw, data=None, error=str(exc)[:160]))

    body = tree.body
    if body is not None:
        doc.visible_text = _collect_text(body, skip_chrome=False)
        main = tree.css_first("main") or tree.css_first("[role=main]")
        doc.main_text = _collect_text(main, False) if main else _collect_text(body, True)
    return doc


def _inside(node: LexborNode, tag: str) -> bool:
    n = node.parent
    while n is not None:
        if n.tag == tag:
            return True
        n = n.parent
    return False
