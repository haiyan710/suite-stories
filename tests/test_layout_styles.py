"""The four styled layouts render a draft with photos, notes and a closing strip."""

from __future__ import annotations

from app.artifacts.layout_styles import STYLES, Page, Photo, render_styled_html

BODY = (
    "The intro paragraph.\n\n## Day 1 · Saturday 6 February · Arrival\nCheck in. Dinner [TK: hours].\n\n"
    "## Day 2 · Sunday 7 February · The New Year\nBreakfast. [ASK ANNA: a dish]\n\n## Insider notes\nBest time: early."
)
PIXEL = "data:image/jpeg;base64,AAAA"


def page(**extra):
    return Page(
        hotel="Hotel X", headline="A winter line | Hotel X", standfirst="One sentence.", body=BODY,
        kicker="Suite Stories · Hotel X", hero=Photo(PIXEL, "The door at dusk"),
        photos={1: Photo(PIXEL, "A room"), 2: Photo(PIXEL, "The restaurant", "portrait")},
        coda=[Photo(PIXEL, "Rooftop pool")], **extra,
    )


def test_every_style_renders_text_photos_notes_and_the_closing_strip():
    for style in STYLES:
        html = render_styled_html(page(pull_quote="A quiet line."), style)
        assert html.startswith("<!doctype html>") and html.endswith("</html>"), style
        assert "A winter line" in html and "Hotel X" in html
        assert html.count('data:image/jpeg') == 4, style  # hero (img or background), two sections, coda
        assert '<mark class="tk">[TK: hours]</mark>' in html
        assert '<mark class="ask"><b>Ask Anna</b> a dish</mark>' in html
        assert "When you come back in the warmer months" in html and "A quiet line." in html
        assert "Insider notes" in html


def test_styles_differ_in_how_photos_meet_text():
    assert 'class="row' in render_styled_html(page(), "zigzag")
    assert 'class="bleed"' in render_styled_html(page(), "image_first")
    inline = render_styled_html(page(), "inline")
    assert 'class="inset' in inline and "opener" not in inline
    assert 'class="wide opener"' in render_styled_html(page(), "hybrid")


def test_a_page_without_photos_still_renders():
    bare = Page(hotel="Hotel X", headline="Line | Hotel X", standfirst="S", body=BODY)
    for style in STYLES:
        assert "<img" not in render_styled_html(bare, style)


def test_unused_photos_close_the_page_as_a_gallery():
    html = render_styled_html(page(gallery=[Photo(PIXEL, "The lobby")]), "hybrid")
    assert "More of the hotel" in html and "The lobby" in html
