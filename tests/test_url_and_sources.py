"""Unit tests: URL normalization, source classification, confidence."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.tools.pages import normalize_url, host_of, parse_html
from app.tools.web import (
    BRAND,
    DESTINATION,
    EDITORIAL_PRESS,
    PUBLICATION,
    OFFICIAL,
    OTHER,
    REVIEW_SIGNAL,
    build_search_query,
    classify_source,
    confidence_for,
    WebSearchTool,
    _strong_hotel_domain_match,
    SearchHit,
)
from app.llm.base import LLMResponse


class TestNormalizeUrl:
    def test_strips_tracking_params(self):
        url = "https://Example.com/path/?utm_source=x&id=2&gclid=abc"
        assert normalize_url(url) == "https://example.com/path?id=2"

    def test_strips_fragment_and_trailing_slash(self):
        assert normalize_url("https://a.com/page/#section") == "https://a.com/page"

    def test_root_keeps_slash(self):
        assert normalize_url("https://a.com/") == "https://a.com/"

    def test_default_ports_removed(self):
        assert normalize_url("http://a.com:80/x") == "http://a.com/x"
        assert normalize_url("https://a.com:443/x") == "https://a.com/x"

    def test_nondefault_port_kept(self):
        assert normalize_url("https://a.com:8443/x") == "https://a.com:8443/x"

    def test_missing_scheme_tolerated(self):
        assert normalize_url("a.com/x") == "http://a.com/x"

    def test_protocol_relative(self):
        assert normalize_url("//a.com/x") == "https://a.com/x"

    def test_empty(self):
        assert normalize_url("") == ""
        assert normalize_url("   ") == ""

    def test_dedup_equivalence(self):
        a = normalize_url("https://hotel.com/rooms/?utm_campaign=launch")
        b = normalize_url("https://hotel.com/rooms")
        assert a == b


class TestHostOf:
    def test_www_stripped(self):
        assert host_of("https://www.harbourgrandkobe.com/en/") == "harbourgrandkobe.com"

    def test_no_double_strip_bug(self):
        # lstrip("www.") would wrongly strip the 'w' in "web..."
        assert host_of("https://web.example.com") == "web.example.com"


def test_soft_404_page_is_not_fetch_usable():
    page = parse_html(
        "https://example.com/en/404.html",
        "<html><head><title>404 notfound</title></head><body>Page missing</body></html>",
    )
    assert page.fetched_ok is False
    assert page.note == "soft 404"


class TestClassifySource:
    def test_publication_domain(self):
        assert classify_source("https://example-magazine.com/x", publication_domain="example-magazine.com") == PUBLICATION

    def test_hotel_domain_hint(self):
        assert classify_source("https://harbourgrandkobe.com/en/", hotel_domain_hint="harbourgrandkobe.com") == OFFICIAL

    def test_brand(self):
        assert classify_source("https://www.ihg.com/intercontinental/x") == BRAND

    def test_editorial(self):
        assert classify_source("https://www.wallpaper.com/hotels/x") == EDITORIAL_PRESS

    def test_tripadvisor_is_review_signal(self):
        assert classify_source("https://www.tripadvisor.com/Hotel_Review-x") == REVIEW_SIGNAL

    def test_tourism_board(self):
        assert classify_source("https://kobe-info.jp/en/") == DESTINATION

    def test_blog_is_other(self):
        assert classify_source("https://randomtravelthoughts.blogspot.com/x") == OTHER

    def test_unknown_is_other(self):
        assert classify_source("https://example.net/article") == OTHER


class TestConfidence:
    def test_official_fetched_is_high(self):
        assert confidence_for(OFFICIAL, fetched_ok=True) == "high"

    def test_official_unfetched_is_medium(self):
        assert confidence_for(OFFICIAL, fetched_ok=False) == "medium"

    def test_blog_fetched_is_low(self):
        assert confidence_for(OTHER, fetched_ok=True) == "low"


class TestBuildSearchQuery:
    def test_joins_nonempty(self):
        assert build_search_query("Aman Tokyo", "", "official website") == "Aman Tokyo official website"

    def test_all_empty(self):
        assert build_search_query("", " ") == ""


def test_official_search_keeps_property_and_brand_as_separate_paths():
    class Provider:
        def __init__(self):
            self.prompts = []

        def complete_with_search(self, messages, **kwargs):
            prompt = messages[-1].content
            self.prompts.append(prompt)
            if "OWN official" in prompt:
                return LLMResponse(text='[{"url":"https://hotelx.com/","title":"Hotel X","snippet":"Own site"}]')
            return LLMResponse(text='[{"url":"https://www.ihg.com/hotelx","title":"Hotel X","snippet":"Brand page"}]')

    provider = Provider()
    hits = WebSearchTool(provider).search_official_hotel("Hotel X")

    assert len(provider.prompts) == 2
    assert [hit.source_type for hit in hits] == [OFFICIAL, BRAND]


def test_official_search_uses_independent_discovery_only_without_property_site():
    class BrandOnlyProvider:
        def complete_with_search(self, messages, **kwargs):
            return LLMResponse(text='[{"url":"https://www.ihg.com/intercontinental/hotels/us/en/hotelx","title":"Hotel X","snippet":"Brand"}]')

    tool = WebSearchTool(BrandOnlyProvider())
    calls = []
    tool._public_index_search = lambda query, hotel_name: (
        calls.append(query)
        or [SearchHit("https://hotelx.com/", "Hotel X", "", OFFICIAL)]
    )

    hits = tool.discover_public_official("Hotel X")

    assert len(calls) == 1
    assert any(hit.url == "https://hotelx.com/" and hit.source_type == OFFICIAL for hit in hits)


def test_city_only_domain_is_not_a_strong_hotel_identity_match():
    assert not _strong_hotel_domain_match(
        "https://www.hg-kobe.com/", "Harbour Grand Kobe"
    )
    assert _strong_hotel_domain_match("https://hotelx.com/", "Hotel X")
