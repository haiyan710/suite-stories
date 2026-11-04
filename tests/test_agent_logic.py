"""Unit tests: prompt loading, JSON extraction, asset classification,
agent anti-repetition heuristics — all offline (fake LLM where needed)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent import _keyword_overlap_warnings
from app.llm.zai_glm import extract_json_block, extract_urls
from app.prompts import load_prompt
from app.tools.assets import (
    AssetCandidate,
    classify_image,
    find_candidate_assets,
    summarize_asset_inventory,
)
from app.tools.pages import FetchedPage, parse_html


class TestPromptLoading:
    def test_all_prompts_loadable_and_substantive(self):
        for name in ("triage", "methodology", "critic", "final"):
            text = load_prompt(name)
            assert len(text) > 500, name

    def test_methodology_contains_core_rules(self):
        methodology = load_prompt("methodology")
        assert "unit of ideation is an EXPERIENCE" in methodology
        assert "sales test" in methodology.lower()
        assert "swap test" in methodology.lower()
        assert "STAGE: EVIDENCE" in methodology
        assert "STAGE: CLASSIFY" in methodology
        assert "STAGE: TERRITORIES" in methodology
        # three layers: fact / product benefit / experience
        assert "PRODUCT BENEFIT" in methodology

    def test_critic_contains_gates(self):
        critic = load_prompt("critic")
        assert "swap test" in critic.lower()
        assert "sales test" in critic.lower()
        assert "desire" in critic
        assert "STAGE: REVIEW" in critic
        assert "STAGE: DIAGNOSE" in critic

    def test_final_prompt_defines_modes(self):
        final = load_prompt("final")
        assert "MODE: NORMAL" in final
        assert "MODE: DEEP" in final
        # default register is conversational, not a report
        assert "30–60 seconds" in final or "30-60 seconds" in final

    def test_triage_knows_new_intents(self):
        triage = load_prompt("triage")
        assert "prototype_review" in triage
        assert "`deep`" in triage


class TestJsonExtraction:
    def test_fenced_json(self):
        text = 'Here you go:\n```json\n{"intent": "research", "hotel": "Aman Tokyo"}\n```'
        assert extract_json_block(text) == {"intent": "research", "hotel": "Aman Tokyo"}

    def test_bare_json_with_prose(self):
        text = 'Sure!\n{"a": 1}\nHope that helps.'
        assert extract_json_block(text) == {"a": 1}

    def test_json_array(self):
        assert extract_json_block('[{"fact": "x"}, {"fact": "y"}]') == [{"fact": "x"}, {"fact": "y"}]

    def test_nested_braces(self):
        text = '{"outer": {"inner": "}"}}'
        assert extract_json_block(text) == {"outer": {"inner": "}"}}

    def test_no_json(self):
        assert extract_json_block("no json here") is None

    def test_glm_javascript_style_apostrophe_escape(self):
        text = r'''```json
[{"url":"https://example.com","snippet":"Umeda\'s Grand Front"}]
```'''
        assert extract_json_block(text) == [
            {"url": "https://example.com", "snippet": "Umeda's Grand Front"}
        ]


class TestExtractUrls:
    def test_ordered_dedup(self):
        text = "See https://a.com/x and https://a.com/x again plus https://b.com/y."
        assert extract_urls(text) == ["https://a.com/x", "https://b.com/y"]

    def test_trailing_punctuation_stripped(self):
        assert extract_urls("link: https://a.com/x.") == ["https://a.com/x"]


class TestAssetClassification:
    def test_room_by_filename(self):
        bucket, evidence = classify_image("https://h.com/img/deluxe-suite-king.jpg", "")
        assert bucket == "room"
        assert "filename" in evidence

    def test_dining_by_alt(self):
        bucket, _ = classify_image("https://h.com/img/photo-12.jpg", "Afternoon tea lounge")
        assert bucket == "dining"

    def test_unclassified_when_no_signal(self):
        bucket, _ = classify_image("https://h.com/img/12345.jpg", "")
        assert bucket == "unclassified"

    def test_find_assets_filters_icons(self):
        html = """
        <html><body>
        <img src="/logo.png" alt="logo">
        <img src="/images/club-suite.jpg" alt="Club Suite">
        <img src="/images/pool.jpg">
        <img src="data:image/png;base64,xxx">
        </body></html>
        """
        page = parse_html("https://h.com/", html)
        assets = find_candidate_assets(page, page_source_type="official")
        buckets = {a.bucket for a in assets}
        assert "room" in buckets and "spa" in buckets
        assert not any("logo" in a.url for a in assets)

    def test_summary_text(self):
        assets = [
            AssetCandidate("https://h.com/a.jpg", "room", "alt: 'Suite'", "https://h.com", "official"),
            AssetCandidate("https://h.com/b.jpg", "dining", "filename: 'bar'", "https://h.com", "official"),
        ]
        summary = summarize_asset_inventory(assets)
        assert "room (1):" in summary and "dining (1):" in summary


class TestParseHtml:
    def test_extracts_title_meta_headings_text(self):
        html = """
        <html><head><title>Kobe Hotel</title>
        <meta name="description" content="A hotel in Kita-ku"></head>
        <body><h1>Welcome</h1><script>var x=1;</script>
        <p>The hotel stands above Kobe Station.</p></body></html>
        """
        page = parse_html("https://h.com/", html)
        assert page.title == "Kobe Hotel"
        assert page.meta_description == "A hotel in Kita-ku"
        assert page.headings == ("Welcome",)
        assert "Kobe Station" in page.text
        assert "var x=1" not in page.text


class TestKeywordOverlapWarnings:
    def test_flags_repeated_keywords(self):
        class FakeConcept:
            keywords = ("verticality", "base", "quiet")
            hotel = "X"
        warnings = _keyword_overlap_warnings([FakeConcept(), FakeConcept()], "X")
        assert len(warnings) == 1
        assert "verticality" in warnings[0]

    def test_no_warning_when_fresh(self):
        class FakeConcept:
            keywords = ("verticality",)
            hotel = "X"
        class OtherConcept:
            keywords = ("market",)
            hotel = "X"
        assert _keyword_overlap_warnings([FakeConcept(), OtherConcept()], "X") == []
