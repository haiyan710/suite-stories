"""Asset research: inventory and classify real, citable imagery.

We never claim an image shows something the source does not support.
Classification relies on filename/alt-text evidence from pages we
actually fetched; anything uncertain stays "unclassified".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .pages import FetchedPage

logger = logging.getLogger(__name__)

# Ordered (bucket, keyword-pattern) rules applied to path + alt text.
_BUCKET_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("room", ("room", "suite", "bedroom", "deluxe", "club-", "penthouse", "twin", "king", "guest")),
    ("dining", ("restaurant", "dining", "bar", "lounge", "kitchen", "gourmet", "chef", "breakfast", "afternoon")),
    ("spa", ("spa", "treatment", "wellness", "massage", "onsen", "sauna", "fitness", "gym", "pool", "swimming")),
    ("architecture", ("architecture", "facade", "exterior", "building", "lobby", "atrium", "entrance", "interior", "design")),
    ("view", ("view", "skyline", "panorama", "city", "river", "ocean", "mountain", "garden", "park")),
    ("detail", ("detail", "texture", "material", "lighting", "art", "craft", "artwork", "pattern")),
    ("event", ("wedding", "banquet", "meeting", "conference", "ballroom", "event")),
    ("people", ("staff", "concierge", "butler", "service", "chef-portrait")),
)

_ICON_PATTERNS = re.compile(r"(logo|icon|sprite|favicon|badge|button|arrow|social|emoji|avatar|placeholder|loading)", re.I)
_RASTER_EXT = (".jpg", ".jpeg", ".png", ".webp", ".avif")

ASSET_BUCKETS = ("hero", "room", "dining", "spa", "architecture", "view", "detail", "event", "people", "unclassified")


@dataclass(frozen=True)
class AssetCandidate:
    url: str
    bucket: str
    evidence: str  # why we think it belongs to the bucket (alt text / filename)
    page_url: str
    source_type: str  # provenance: which page it came from (official etc.)


def classify_image(url: str, alt: str) -> tuple[str, str]:
    """Return (bucket, evidence) for one image based on path and alt text."""
    path = urlsplit(url).path.lower()
    stem = path.rsplit("/", 1)[-1]
    hay = f"{stem} {' '.join(re.split(r'[-_.]', stem))} {alt.lower()}"
    for bucket, keywords in _BUCKET_RULES:
        for kw in keywords:
            if kw in hay:
                evidence = f"alt: {alt!r}" if kw in alt.lower() else f"filename: {stem!r}"
                return bucket, evidence
    return "unclassified", (f"alt: {alt!r}" if alt else "filename only")


def find_candidate_assets(
    page: FetchedPage, *, page_source_type: str = "other", max_items: int = 40
) -> list[AssetCandidate]:
    """Inventory usable images from one fetched page, junk filtered."""
    assets: list[AssetCandidate] = []
    for img in page.images:
        src = img["src"]
        path = urlsplit(src).path.lower()
        if not path.endswith(_RASTER_EXT):
            continue
        if _ICON_PATTERNS.search(src) or _ICON_PATTERNS.search(img["alt"]):
            continue
        bucket, evidence = classify_image(src, img["alt"])
        assets.append(
            AssetCandidate(
                url=src, bucket=bucket, evidence=evidence,
                page_url=page.url, source_type=page_source_type,
            )
        )
        if len(assets) >= max_items:
            break
    return assets


def summarize_asset_inventory(assets: list[AssetCandidate]) -> str:
    """Compact text summary of what imagery exists, for the LLM to reason over."""
    if not assets:
        return "No hotel imagery could be inventoried from fetched pages."
    by_bucket: dict[str, list[AssetCandidate]] = {}
    for a in assets:
        by_bucket.setdefault(a.bucket, []).append(a)
    lines = []
    for bucket in ASSET_BUCKETS:
        items = by_bucket.get(bucket)
        if not items:
            continue
        lines.append(f"{bucket} ({len(items)}):")
        for a in items[:5]:
            label = a.evidence if len(a.evidence) < 90 else a.evidence[:87] + "..."
            lines.append(f"  - {label} [{a.source_type}] {a.url[:110]}")
        if len(items) > 5:
            lines.append(f"  - ...and {len(items) - 5} more {bucket} images")
    return "\n".join(lines)
