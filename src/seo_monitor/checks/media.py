"""Images, social sharing images and structured data."""

from __future__ import annotations

from importlib import resources

import yaml

from ..parse import iter_nodes, node_types
from ..urls import is_presigned, normalize
from .base import Context, Item, check, group_by, on_pages

LEGACY_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/gif", "image/bmp", "image/tiff"}


def _image_pages(ctx: Context) -> dict[str, list[str]]:
    return group_by((img.src, p.final_url) for p in ctx.html_pages for img in p.doc.images)


@check(
    "presigned_urls",
    "high",
    "Images use expiring (signed) storage URLs",
    "Signed S3/GCS links stop working after a few hours or days. Google, social previews and "
    "cached pages then show broken images.",
    "Serve images from a public bucket path or CDN (e.g. CloudFront) instead of generating "
    "temporary signed URLs in page HTML.",
)
def presigned_urls(ctx: Context):
    for page in ctx.html_pages:
        urls = [img.src for img in page.doc.images] + [page.doc.og.get("og:image") or ""]
        signed = [u for u in urls if u and is_presigned(u)]
        if signed:
            yield Item(page.final_url, f"{len(signed)} signed URL(s), e.g. {signed[0][:120]}…")


@check(
    "og_image_broken",
    "medium",
    "Social sharing image (og:image) is broken",
    "When the og:image fails, links shared on WhatsApp/Facebook/LinkedIn show no picture.",
    "Point og:image at a permanent, public image URL that returns 200.",
    scope="resource",
)
def og_image_broken(ctx: Context):
    pairs = []
    for page in ctx.html_pages:
        og = page.doc.og.get("og:image")
        if og and og.startswith("http"):
            res = ctx.crawl.lookup(og)
            if res and (not res.ok or not res.content_type.startswith("image/")):
                why = res.describe() if not res.ok else f"not an image ({res.content_type})"
                pairs.append((f"{normalize(og)}|{why}", page.final_url))
    for key, pages in group_by(pairs).items():
        src, why = key.rsplit("|", 1)
        yield Item(src, f"{why} — used {on_pages(pages)}", key=src)


@check(
    "image_broken",
    "medium",
    "Images that fail to load",
    "Broken images look unprofessional and lose image-search traffic.",
    "Re-upload the image or fix its URL.",
    quick=False,
    scope="resource",
)
def image_broken(ctx: Context):
    for src, pages in _image_pages(ctx).items():
        res = ctx.crawl.resources.get(src)
        if res and not res.ok:
            yield Item(src, f"{res.describe()} — {on_pages(pages)}", key=src)


@check(
    "image_missing_alt",
    "low",
    "Images without alt text",
    "Alt text describes images to screen readers and image search.",
    'Add a short description in the alt attribute (use alt="" only for decorative images).',
    scope="resource",
)
def image_missing_alt(ctx: Context):
    pairs = [
        (img.src, p.final_url) for p in ctx.html_pages for img in p.doc.images if img.alt is None
    ]
    for src, pages in group_by(pairs).items():
        yield Item(src, on_pages(pages), key=src)


@check(
    "image_too_large",
    "medium",
    "Large image files",
    "Heavy images slow pages down, especially on mobile data in Ghana.",
    "Resize to the displayed size and compress (WebP/AVIF, ~80% quality).",
    quick=False,
    scope="resource",
)
def image_too_large(ctx: Context):
    limit = ctx.profile.image_max_kb * 1024
    for src, pages in _image_pages(ctx).items():
        res = ctx.crawl.resources.get(src)
        if res and res.ok and res.size and res.size > limit:
            yield Item(src, f"{res.size // 1024} KB — {on_pages(pages)}", key=src)


@check(
    "image_legacy_format",
    "low",
    "Images not in a modern format",
    "WebP/AVIF are typically 25–50% smaller than JPEG/PNG at the same quality.",
    "Serve WebP or AVIF (WordPress: an image optimisation plugin; or a CDN that converts).",
    quick=False,
    scope="resource",
)
def image_legacy_format(ctx: Context):
    for src, pages in _image_pages(ctx).items():
        res = ctx.crawl.resources.get(src)
        if res and res.ok and res.content_type in LEGACY_TYPES:
            yield Item(src, f"{res.content_type} — {on_pages(pages)}", key=src)


@check(
    "jsonld_invalid",
    "medium",
    "Structured data (JSON-LD) that is not valid JSON",
    "Search engines ignore structured data they cannot parse.",
    "Fix the JSON syntax (check with https://validator.schema.org).",
)
def jsonld_invalid(ctx: Context):
    for page in ctx.html_pages:
        for i, block in enumerate(page.doc.jsonld):
            if block.error:
                yield Item(page.final_url, block.error, key=f"{page.final_url}|{i}")


def _rules() -> dict[str, list[str]]:
    text = resources.files("seo_monitor").joinpath("structured_data_rules.yaml").read_text()
    return yaml.safe_load(text) or {}


@check(
    "jsonld_missing_properties",
    "low",
    "Structured data missing commonly expected properties",
    "Incomplete schema.org markup carries less information. This is a vocabulary completeness "
    "check only; it does not test rich-result eligibility.",
    "Add the listed properties (rules live in structured_data_rules.yaml).",
)
def jsonld_missing_properties(ctx: Context):
    rules = _rules()
    pairs = []
    for page in ctx.html_pages:
        for block in page.doc.jsonld:
            for node in iter_nodes(block.data):
                for t in node_types(node):
                    missing = [
                        prop for prop in rules.get(t, []) if node.get(prop) in (None, "", [])
                    ]
                    if missing:
                        pairs.append((f"{t}: {', '.join(missing)}", page.final_url))
    for key, pages in group_by(pairs).items():
        yield Item(pages[0], f"{key} — {on_pages(pages)}", key=key)
