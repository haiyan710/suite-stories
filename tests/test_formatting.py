"""Unit tests: Slack formatting, splitting, sources block, citations."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.formatting import (
    SourceLine,
    build_compact_sources,
    build_sources_block,
    markdown_to_slack,
    split_for_slack,
    strip_citations,
    validate_citations,
)
from app.tools.web import EDITORIAL_PRESS, PUBLICATION, OFFICIAL


class TestMarkdownToSlack:
    def test_headings_bold(self):
        assert markdown_to_slack("## Harbour Grand Kobe") == "*Harbour Grand Kobe*"
        assert markdown_to_slack("### 1. Research snapshot") == "*1. Research snapshot*"

    def test_table_to_bullets(self):
        src = "| Concept | Rank |\n| --- | --- |\n| A | 1 |"
        out = markdown_to_slack(src)
        assert "• Concept — Rank" in out
        assert "• A — 1" in out
        assert "|" not in out

    def test_commonmark_bold_and_link_become_slack_mrkdwn(self):
        text = "Some **bold** and a [link](https://x.com)."
        assert markdown_to_slack(text) == "Some *bold* and a <https://x.com|link>."

    def test_inline_code_is_not_reformatted(self):
        text = "Use `**literal**` but show **real bold**."
        assert markdown_to_slack(text) == "Use `**literal**` but show *real bold*."


class TestSplitForSlack:
    def test_short_message_single_chunk(self):
        assert split_for_slack("hello") == ["hello"]

    def test_long_message_splits_under_limit(self):
        section = "### Section\n" + ("word " * 700) + "\n\n"
        text = section * 3
        for chunk in split_for_slack(text):
            assert len(chunk) <= 2800

    def test_splits_prefer_heading_boundaries(self):
        part_a = "A" * 2500
        part_b = "B" * 600
        text = f"{part_a}\n\n### Heading\n{part_b}"
        chunks = split_for_slack(text)
        assert len(chunks) >= 2
        assert any("Heading" in c for c in chunks)

    def test_no_content_lost(self):
        text = "\n\n".join(f"### S{i}\n{'x' * 900}" for i in range(9))
        chunks = split_for_slack(text)
        rejoined = "".join(chunks)
        for i in range(9):
            assert f"S{i}" in rejoined


class TestSourcesBlock:
    def test_groups_by_category(self):
        sources = [
            SourceLine("S1", "Official site", "https://harbourgrandkobe.com", OFFICIAL),
            SourceLine("S2", "Our article", "https://example-magazine.com/x", PUBLICATION),
            SourceLine("S3", "Wallpaper review", "https://wallpaper.com/x", EDITORIAL_PRESS),
        ]
        block = build_sources_block(sources)
        assert "*official*" in block
        assert "*Our publication*" in block
        assert "*editorial / press*" in block
        assert block.index("*official*") < block.index("*editorial / press*")
        assert "<https://harbourgrandkobe.com|Official site>" in block
        assert "[S1]" in block and "[SS1]" not in block

    def test_empty(self):
        assert "No usable sources" in build_sources_block([])


class TestValidateCitations:
    def test_keeps_valid_removes_invalid(self):
        text = "Claim one [S1]. Claim two [S2]. Claim three [S9]."
        cleaned, removed = validate_citations(text, {"S1", "S2"})
        assert "[S1]" in cleaned and "[S2]" in cleaned
        assert "[S9]" not in cleaned
        assert removed == ["S9"]


class TestStripCitations:
    def test_strips_all_ids(self):
        assert strip_citations("Claim [S1] and [S12] hold.") == "Claim and hold."

    def test_no_ids_untouched(self):
        assert strip_citations("plain text") == "plain text"


class TestCompactSources:
    def test_prefers_official_and_caps_links(self):
        sources = [
            SourceLine("S1", "Blog", "https://blog.com/x", "other"),
            SourceLine("S2", "Official site", "https://h.com", "official"),
            SourceLine("S3", "Tourism", "https://kobe-info.jp", "destination"),
        ]
        text = build_compact_sources(sources)
        assert "*Sources*" in text
        assert "Official hotel — <https://h.com" in text
        assert "Destination — <https://kobe-info.jp" in text
        assert "blog.com" not in text  # low priority dropped
        assert "(+1 more stored internally)" in text

    def test_max_links(self):
        sources = [
            SourceLine(f"S{i}", f"P{i}", f"https://h.com/{i}", "official") for i in range(9)
        ]
        text = build_compact_sources(sources)
        assert text.count("https://h.com/") == 6
        assert "(+3 more stored internally)" in text

    def test_empty(self):
        assert "no usable sources" in build_compact_sources([])
