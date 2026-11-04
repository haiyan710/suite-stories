"""Research tools: search, page parsing, asset discovery."""

from .assets import AssetCandidate, classify_image, find_candidate_assets, summarize_asset_inventory
from .pages import FetchedPage, PageFetcher, condense_page_text, host_of, normalize_url, parse_html
from .web import (
    BRAND,
    DESTINATION,
    EDITORIAL_PRESS,
    PUBLICATION,
    OFFICIAL,
    OTHER,
    REVIEW_SIGNAL,
    SearchHit,
    WebSearchTool,
    build_search_query,
    classify_source,
    confidence_for,
)

__all__ = [
    "AssetCandidate", "classify_image", "find_candidate_assets", "summarize_asset_inventory",
    "FetchedPage", "PageFetcher", "condense_page_text", "host_of", "normalize_url", "parse_html",
    "BRAND", "DESTINATION", "EDITORIAL_PRESS", "PUBLICATION", "OFFICIAL", "OTHER",
    "REVIEW_SIGNAL", "SearchHit", "WebSearchTool", "build_search_query", "classify_source",
    "confidence_for",
]
