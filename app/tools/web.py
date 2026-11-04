"""Composable web research tools.

Search is powered by the LLM provider's server-side web search (no
separate search API key). Query building and source classification are
pure functions so they can be unit-tested without network access.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import Sequence
from urllib.parse import quote_plus

import httpx

from ..llm.base import LLMMessage, LLMProvider
from ..llm.zai_glm import extract_json_block
from .pages import host_of, normalize_url

logger = logging.getLogger(__name__)

# Parent-brand / booking-platform domains that are NOT the property's own site.
BRAND_DOMAINS = {
    "ihg.com", "marriott.com", "hyatt.com", "accor.com", "hilton.com",
    "intercontinental.com",
    "fourseasons.com", "rosewoodhotels.com", "mandarinoriental.com",
    "capellahotels.com", "aman.com", "kempinski.com", "shangri-la.com",
    "peninsula.com", "ritzcarlton.com", "stregis.com",
    "luxurycollection.com", "w-hotels.com", "whotels.com", "jumeirah.com",
    "langhamhotels.com", "fairmont.com", "swissotel.com", "sofitel.com",
    "pullmanhotels.com", "movenpick.com", "raffles.com", "banyantree.com",
}
BOOKING_DOMAINS = {
    "booking.com", "expedia.com", "agoda.com", "hotels.com", "tripadvisor.com",
    "kayak.com", "trivago.com", "priceline.com", "trip.com", "ctrip.com",
}
EDITORIAL_DOMAINS = {
    "wallpaper.com", "monocle.com", "dezeen.com", "designboom.com",
    "architecturaldigest.com", "ad-magazine.com", "travelandleisure.com",
    "cntraveler.com", "condenasttraveler.com", "afar.com", "robbreport.com",
    "ft.com", "nytimes.com", "theguardian.com", "bbc.com", "cnn.com",
    "forbes.com", "forbestravelguide.com", "bloomberg.com",
    "nationalgeographic.com", "surface.com", "interiordesign.net",
    "hospitalitydesign.com", "hoteldesigns.net", "designhotels.com",
    "leadinghotels.org", "timeout.com", "lonelyplanet.com",
    "theworlds50best.com", "eater.com", "michelin.com",
}
REVIEW_DOMAINS = {
    "tripadvisor.com", "google.com", "g.co", "yelp.com", "trustpilot.com",
}
DESTINATION_HINTS = (
    "tourism", "tourist", "visits", "travel-", "-travel", "convention", "cvb",
    "-info", ".go.jp", "go.jp", "japan.travel", "-metro", "city.",
)
LOW_QUALITY_HINTS = ("blogspot.", "medium.com", "wordpress.com", "wixsite.com", "blog.")

OFFICIAL = "official"
BRAND = "brand"
PUBLICATION = "publication"
DESTINATION = "destination"
EDITORIAL_PRESS = "editorial_press"
REVIEW_SIGNAL = "review_signal"
OTHER = "other"


@dataclass(frozen=True)
class SearchHit:
    url: str
    title: str
    snippet: str
    source_type: str = OTHER
    confidence: str = "medium"
    # True only when the URL came back from the live search tool (or the user).
    # Model-written candidates are unverified until a page fetch succeeds.
    grounded: bool = False

    @property
    def normalized_url(self) -> str:
        return normalize_url(self.url)


def build_search_query(*parts: str) -> str:
    return " ".join(p.strip() for p in parts if p and p.strip())


def classify_source(url: str, *, publication_domain: str = "", hotel_domain_hint: str = "") -> str:
    """Classify a URL into the source hierarchy using domain rules."""
    host = host_of(url)
    if not host:
        return OTHER
    if publication_domain and host.endswith(host_of(publication_domain) or publication_domain):
        return PUBLICATION
    if hotel_domain_hint and host == host_of(hotel_domain_hint):
        return OFFICIAL
    if any(host == d or host.endswith("." + d) for d in BRAND_DOMAINS):
        return BRAND
    if any(host == d or host.endswith("." + d) for d in BOOKING_DOMAINS):
        return REVIEW_SIGNAL if "tripadvisor" in host or "review" in url else OTHER
    if any(host == d or host.endswith("." + d) for d in REVIEW_DOMAINS):
        return REVIEW_SIGNAL
    if any(host == d or host.endswith("." + d) for d in EDITORIAL_DOMAINS):
        return EDITORIAL_PRESS
    if any(h in host for h in DESTINATION_HINTS):
        return DESTINATION
    if any(h in host for h in LOW_QUALITY_HINTS):
        return OTHER
    return OTHER


def confidence_for(source_type: str, *, fetched_ok: bool) -> str:
    if source_type in (OFFICIAL, BRAND, PUBLICATION) and fetched_ok:
        return "high"
    if source_type in (OFFICIAL, BRAND, PUBLICATION, DESTINATION, EDITORIAL_PRESS):
        return "medium"
    return "low"


class WebSearchTool:
    """Runs structured searches through the provider's server-side search."""

    def __init__(self, provider: LLMProvider, *, publication_domain: str = "") -> None:
        self._provider = provider
        self._ls_domain = publication_domain
        self.last_search_error = ""

    @property
    def publication_domain(self) -> str:
        return self._ls_domain

    def search(self, query: str, *, purpose: str = "research", max_results: int = 8) -> list[SearchHit]:
        """Search the web and return structured hits.

        The model performs live searches server-side and must answer
        with a strict JSON array; URLs found in the response but missing
        from the JSON are appended as fallback hits.
        """
        prompt = (
            f"Find web pages for: {query}\n\n"
            f"Purpose: {purpose}.\n"
            "Use web search when available, then answer with ONLY a JSON array "
            f"(no prose, no markdown fence) of up to {max_results} candidate pages, "
            'each as {"url": "...", "title": "...", "snippet": "one-sentence what this page says"}.\n'
            "Rules: return complete real URLs, never placeholder URLs; drop aggregator/booking SEO "
            "clones; prefer authoritative and official sources. Page fetching will verify candidates."
        )
        response = self._provider.complete_with_search(
            [LLMMessage(role="user", content=prompt)],
            system="You are a meticulous research assistant. You always answer with the requested JSON only.",
            max_tokens=2000,
            temperature=0.2,
        )
        if response.search_error:
            self.last_search_error = response.search_error
        grounded = {normalize_url(result.url) for result in response.search_results if result.url}
        hits = [
            replace(hit, grounded=normalize_url(hit.url) in grounded)
            for hit in _hits_from_model_text(response.text, query)
        ]
        for result in response.search_results:
            if result.url and not any(h.url == result.url for h in hits):
                hits.append(SearchHit(url=result.url, title=result.title, snippet=result.snippet, grounded=True))
        return hits

    # -- higher-level composable queries -------------------------------

    def search_official_hotel(self, hotel_name: str) -> list[SearchHit]:
        # Property-owned sites and parent-brand pages are separate source paths.
        # Asking for both in one broad query tended to return an obsolete brand
        # URL while hiding a newer, independently hosted property site.
        own_hits = self.search(
            build_search_query(
                f'"{hotel_name}"',
                "current official website property rooms dining contact -booking -expedia -tripadvisor",
            ),
            purpose="find the hotel's current OWN official property website, excluding booking platforms and stale mirrors",
            max_results=6,
        )
        brand_hits = self.search(
            build_search_query(f'"{hotel_name}"', "official hotel brand page"),
            purpose="find the current parent-brand hotel-detail page as a separate authoritative source",
            max_results=4,
        )
        out: list[SearchHit] = []
        seen: set[str] = set()
        for hit in own_hits + brand_hits:
            normalized = normalize_url(hit.url)
            dedupe_key = normalized.rstrip("/")
            if not normalized or dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            source_type = (
                OFFICIAL if _looks_like_hotel_site(hit.url, hotel_name)
                else classify_source(hit.url, publication_domain=self._ls_domain)
            )
            out.append(replace(hit, source_type=source_type, confidence="medium"))
        out.sort(key=lambda hit: 0 if hit.source_type == OFFICIAL else 1 if hit.source_type == BRAND else 2)
        return out

    def discover_public_official(self, hotel_name: str) -> list[SearchHit]:
        """Independent candidates used only after initial official fetches fail."""
        return self._public_index_search(
            build_search_query(f'"{hotel_name}"', "official website"), hotel_name
        )

    def _public_index_search(self, query: str, hotel_name: str) -> list[SearchHit]:
        """Discover candidate URLs from Yahoo Japan's public HTML result page.

        This is deliberately a narrow fallback, not an evidence source. Search
        result text is discarded and only successfully fetched destination
        pages can contribute facts downstream.
        """
        try:
            response = httpx.get(
                f"https://search.yahoo.co.jp/search?p={quote_plus(query)}",
                headers={"User-Agent": "Mozilla/5.0"},
                follow_redirects=True,
                timeout=8.0,
            )
            response.raise_for_status()
        except Exception as exc:
            logger.info("public index discovery unavailable: %s", exc)
            return []
        candidates: list[SearchHit] = []
        seen: set[str] = set()
        for raw in re.findall(r'https://[^"\'<>\\ ]+', response.text):
            url = raw.replace("&amp;", "&").rstrip(".,);]")
            normalized = normalize_url(url)
            dedupe_key = normalized.rstrip("/")
            host = host_of(url)
            if (
                not normalized or dedupe_key in seen
                or host.endswith("yahoo.co.jp")
                or host.endswith("yahoo.com")
                or re.search(r"\.(?:css|js|png|jpe?g|gif|svg|webp|woff2?)(?:\?|$)", url, re.I)
            ):
                continue
            source_type = classify_source(url, publication_domain=self._ls_domain)
            if source_type != BRAND and not _looks_like_hotel_site(url, hotel_name):
                continue
            seen.add(dedupe_key)
            # Real URLs from a public result page, so grounded; its text is still discarded.
            candidates.append(SearchHit(url, host, "", source_type, "medium", grounded=True))
            if len(candidates) >= 8:
                break
        return candidates

    def search_publication(self, hotel_name: str) -> list[SearchHit]:
        if not self._ls_domain:
            return []  # without the publication's domain there is nothing to search for
        query = build_search_query(f"site:{self._ls_domain}", hotel_name)
        hits = self.search(query, purpose="find existing editorial coverage in our publication of this hotel")
        # Only trust the publication label when it matches the configured
        # publication domain; without a domain we cannot confirm ownership,
        # so hits keep their domain-based classification.
        out = []
        for h in hits[:5]:
            if self._ls_domain and host_of(h.url) == host_of(self._ls_domain):
                out.append(replace(h, source_type=PUBLICATION, confidence="medium"))
            else:
                out.append(replace(
                    h, source_type=classify_source(h.url, publication_domain=self._ls_domain),
                    confidence="medium",
                ))
        return out

    def search_destination_context(self, hotel_name: str, city_hint: str = "") -> list[SearchHit]:
        subject = build_search_query(hotel_name, city_hint)
        hits = self.search(
            build_search_query(subject, "neighborhood district what is nearby official tourism information"),
            purpose="understand the hotel's neighborhood, city context and official destination information",
        )
        out: list[SearchHit] = []
        gov_hints = ("-metro", "city.", "go.jp", ".go.", "japan.travel")
        for hit in hits:
            stype = classify_source(hit.url, publication_domain=self._ls_domain)
            if stype == OTHER and any(g in host_of(hit.url) for g in gov_hints):
                stype = DESTINATION
            out.append(replace(hit, source_type=stype, confidence="medium"))
        return out

    def search_itinerary_context(self, hotel_name: str, city_hint: str = "") -> list[SearchHit]:
        """Venues and day plans around the hotel, for multi-day itinerary pieces."""
        hits = self.search(
            build_search_query(
                f'"{hotel_name}"', city_hint,
                "neighbourhood restaurants cafes museums day trips things to do nearby official",
            ),
            purpose="find specific restaurants, sights, museums and day trips near the hotel for a multi-day itinerary",
        )
        return [replace(hit, source_type=DESTINATION, confidence="medium") for hit in hits[:6]]

    def search_editorial_coverage(self, hotel_name: str) -> list[SearchHit]:
        hits = self.search(
            build_search_query(hotel_name, "architecture interior design review hospitality press"),
            purpose="find reputable design/travel press coverage and the architects/designers involved",
        )
        return [
            replace(
                h, confidence="medium",
                source_type=(
                    EDITORIAL_PRESS
                    if classify_source(h.url, publication_domain=self._ls_domain) != OTHER else OTHER
                ),
            )
            for h in hits[:8]
        ]


def _looks_like_hotel_site(url: str, hotel_name: str) -> bool:
    """Heuristic: does this URL plausibly belong to the hotel itself?"""
    host = host_of(url)
    if any(host == d or host.endswith("." + d) for d in BRAND_DOMAINS | BOOKING_DOMAINS):
        return False
    if any(h in host for h in DESTINATION_HINTS):
        return False
    tokens = [t for t in hotel_name.lower().replace("-", " ").split() if len(t) > 2]
    if not tokens:
        return False
    joined = host.replace("-", "").replace("_", "")
    recognizable = sum(1 for t in tokens if t.replace(" ", "") in joined)
    brandish = any(b in joined for b in ("hotel", "resort"))
    return recognizable >= 1 or (brandish and recognizable >= 1)


def _strong_hotel_domain_match(url: str, hotel_name: str) -> bool:
    """Require more than a shared city token before skipping fallback discovery."""
    host = host_of(url)
    if any(host == d or host.endswith("." + d) for d in BRAND_DOMAINS | BOOKING_DOMAINS):
        return False
    tokens = [
        re.sub(r"[^a-z0-9]", "", token)
        for token in hotel_name.lower().replace("-", " ").split()
        if len(token) > 2
    ]
    tokens = [token for token in tokens if token]
    if not tokens:
        return False
    joined = re.sub(r"[^a-z0-9]", "", host.split(".")[0])
    full_name = "".join(tokens)
    if full_name and full_name in joined:
        return True
    required = 1 if len(tokens) == 1 else 2
    return sum(token in joined for token in tokens) >= required


def _hits_from_model_text(text: str, query: str) -> list[SearchHit]:
    data = extract_json_block(text)
    hits: list[SearchHit] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url", "")).strip()
            if not url.startswith(("http://", "https://")):
                continue
            hits.append(
                SearchHit(
                    url=url,
                    title=str(item.get("title", ""))[:200],
                    snippet=str(item.get("snippet", ""))[:400],
                )
            )
    return hits
