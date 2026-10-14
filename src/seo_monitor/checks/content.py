"""On-page content: titles, descriptions, headings, sharing tags, thin/empty/duplicate pages,
and template placeholder text."""

from __future__ import annotations

import re
from functools import cache

from .base import Context, Item, check, group_by, on_pages


def _needs_js(ctx: Context, page) -> bool:
    """Server HTML is an app shell; text-based checks would give false answers."""
    return bool(page.doc.js_hint) and page.doc.word_count < ctx.profile.thin_words


def _server_rendered(ctx: Context):
    return [p for p in ctx.indexable if not _needs_js(ctx, p)]


@check(
    "client_side_rendered",
    "medium",
    "Page content only appears after JavaScript runs",
    "The server sends a near-empty page and the browser builds the content with JavaScript. "
    "You see a full page, but Google indexes it later and less reliably, and WhatsApp/Facebook "
    "previews, Bing and AI crawlers may see nothing. Content checks are skipped for these pages; "
    "they would need a rendering browser to measure.",
    "Render the content on the server. Next.js: keep pages as server components, wrap "
    "useSearchParams()/browser-only code in <Suspense>, avoid dynamic(..., { ssr: false }) for "
    "main content. React SPA: move to SSR/SSG (Next.js, Remix, Astro) or pre-render.",
)
def client_side_rendered(ctx: Context):
    for page in ctx.indexable:
        if _needs_js(ctx, page):
            yield Item(
                page.final_url,
                f"{page.doc.js_hint}; server HTML has "
                f"{page.doc.word_count} words of main content",
            )


def _dupes(ctx: Context, value) -> dict[str, list[str]]:
    pairs = [(value(p), p.final_url) for p in ctx.indexable if value(p)]
    return {k: v for k, v in group_by(pairs).items() if len(v) > 1}


@check(
    "title_missing",
    "high",
    "Pages without a title",
    "The title is the headline Google shows in results.",
    "Give every page a unique, descriptive <title>.",
)
def title_missing(ctx: Context):
    for page in ctx.indexable:
        if not page.doc.title:
            yield Item(page.final_url)


@check(
    "title_too_long",
    "low",
    "Titles longer than recommended",
    "Long titles are cut off in search results.",
    "Keep titles to about 50–65 characters with the key words first.",
)
def title_too_long(ctx: Context):
    limit = ctx.profile.title_max
    for page in ctx.indexable:
        title = page.doc.title or ""
        if len(title) > limit:
            yield Item(page.final_url, f"{len(title)} chars: {title}")


@check(
    "title_duplicate",
    "medium",
    "Several pages share the same title",
    "Duplicate titles make pages look interchangeable to Google and users.",
    "Write a unique title for each page (usually a template that ignores the page name).",
)
def title_duplicate(ctx: Context):
    for title, pages in _dupes(ctx, lambda p: p.doc.title).items():
        yield Item(pages[0], f'"{title}" {on_pages(pages)}', key=title)


@check(
    "meta_description_missing",
    "medium",
    "Pages without a meta description",
    "Without one Google improvises a snippet, which is often less compelling.",
    "Write a meta description (about 120–160 characters) per page or per template.",
)
def meta_description_missing(ctx: Context):
    for page in ctx.indexable:
        if not page.doc.meta_description:
            yield Item(page.final_url)


@check(
    "meta_description_too_long",
    "low",
    "Meta descriptions longer than recommended",
    "Long descriptions are truncated in search results.",
    "Keep descriptions to about 120–160 characters.",
)
def meta_description_too_long(ctx: Context):
    limit = ctx.profile.description_max
    for page in ctx.indexable:
        desc = page.doc.meta_description or ""
        if len(desc) > limit:
            yield Item(page.final_url, f"{len(desc)} chars")


@check(
    "meta_description_duplicate",
    "low",
    "Several pages share the same meta description",
    "Duplicate descriptions waste the chance to sell each page.",
    "Write unique descriptions, or generate them from each page's own content.",
)
def meta_description_duplicate(ctx: Context):
    for desc, pages in _dupes(ctx, lambda p: p.doc.meta_description).items():
        yield Item(pages[0], f'"{desc[:90]}" {on_pages(pages)}', key=desc)


@check(
    "h1_missing",
    "medium",
    "Pages without an H1 heading",
    "The H1 tells visitors and search engines what the page is about.",
    "Add one visible H1 that describes the page.",
)
def h1_missing(ctx: Context):
    for page in _server_rendered(ctx):
        if not any(page.doc.h1s):
            yield Item(page.final_url)


@check(
    "h1_multiple",
    "low",
    "Pages with more than one H1",
    "Several H1s blur the page's main topic (often the logo or a slider is an H1).",
    "Keep one H1 for the page title; use H2/H3 for the rest.",
)
def h1_multiple(ctx: Context):
    for page in ctx.indexable:
        if len(page.doc.h1s) > 1:
            yield Item(page.final_url, " | ".join(h[:50] for h in page.doc.h1s[:5]))


@check(
    "og_tags_missing",
    "low",
    "Pages missing Open Graph tags",
    "og:title, og:description and og:image control how a link looks when shared on WhatsApp, "
    "Facebook and LinkedIn.",
    "Output Open Graph tags on every page (most SEO plugins do this once enabled).",
)
def og_tags_missing(ctx: Context):
    for page in ctx.indexable:
        missing = [t for t in ("og:title", "og:description", "og:image") if not page.doc.og.get(t)]
        if missing:
            yield Item(page.final_url, "missing " + ", ".join(missing))


@check(
    "empty_page",
    "high",
    "Indexable pages with almost no content",
    "Empty pages (for example /location/-1 or an unfilled template) look like junk to Google "
    "and to visitors.",
    "Return 404/410 for pages that should not exist, or noindex them; fix the code that "
    "generates the URLs.",
)
def empty_page(ctx: Context):
    limit = ctx.profile.empty_words
    for page in _server_rendered(ctx):
        if page.url != ctx.crawl.start_url and page.doc.word_count < limit:
            yield Item(page.final_url, f"{page.doc.word_count} words of main content")


@check(
    "thin_content",
    "low",
    "Pages with thin content",
    "Pages with very little unique text rarely rank and can drag down overall site quality.",
    "Add useful content, merge thin pages, or noindex them. Limit this check to specific page "
    "types with thin_content_patterns in sites.yaml.",
)
def thin_content(ctx: Context):
    p = ctx.profile
    for page in _server_rendered(ctx):
        words = page.doc.word_count
        if not p.empty_words <= words < p.thin_words:
            continue
        if p.thin_content_patterns and not any(
            x in page.final_url for x in p.thin_content_patterns
        ):
            continue
        yield Item(page.final_url, f"{words} words")


@check(
    "duplicate_content",
    "medium",
    "Pages with identical main content",
    "Identical pages compete with each other; Google picks one and may pick the wrong one.",
    "Consolidate the pages, or canonicalise / redirect duplicates to the main version.",
    quick=False,
)
def duplicate_content(ctx: Context):
    min_words = ctx.profile.empty_words
    pairs = [
        (p.doc.content_hash, p.final_url)
        for p in _server_rendered(ctx)
        if p.doc.word_count >= min_words
    ]
    for pages in group_by(pairs).values():
        if len(pages) > 1:
            yield Item(pages[0], f"same text {on_pages(pages, 5)}", key=",".join(sorted(pages)))


@cache
def _term_rx(term: str) -> re.Pattern:
    body = re.escape(term)
    if term[0].isalnum():
        body = r"\b" + body
    if term[-1].isalnum():
        body += r"\b"
    return re.compile(body, re.IGNORECASE)


_TEMPLATE_RX = re.compile(r"\{\{.{0,60}?\}\}|\{%.{0,60}?%\}", re.S)


def _snippet(text: str, start: int, end: int) -> str:
    tmpl = _TEMPLATE_RX.search(text, max(start - 60, 0), end + 60)
    if tmpl and tmpl.start() <= start < tmpl.end():
        return tmpl.group(0)
    left, right = max(start - 40, 0), min(end + 40, len(text))
    return ("…" if left else "") + text[left:right].strip() + ("…" if right < len(text) else "")


@check(
    "placeholder_text",
    "high",
    "Template placeholders or demo text visible on pages",
    "Text like '{{ start_date_html }}', 'Lorem ipsum' or 'Sample Page' tells visitors the site "
    "is unfinished and can be indexed by Google.",
    "Fix the template variable / remove the demo content. If a match is legitimate, add the "
    "term to placeholder_whitelist in sites.yaml.",
)
def placeholder_text(ctx: Context):
    terms = ctx.profile.placeholders
    for page in ctx.html_pages:
        doc = page.doc
        fields = {
            "title": doc.title or "",
            "meta description": doc.meta_description or "",
            "Open Graph": " ".join(doc.og.values()),
            "page text": doc.visible_text,
            "link/image URL": " ".join([lk.href for lk in doc.links] + [i.src for i in doc.images]),
        }
        for term in terms:
            rx = _term_rx(term)
            for where, text in fields.items():
                m = rx.search(text)
                if m:
                    found = _snippet(text, m.start(), m.end())
                    yield Item(
                        page.final_url,
                        f'"{term}" in {where}: {found}',
                        key=f"{page.final_url}|{term}",
                    )
                    break
