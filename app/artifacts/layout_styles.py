"""Four magazine layouts for a sample draft, beyond the classic template in layout.py.

- zigzag:      photo and text side by side, alternating sides (photo stays in view while reading)
- image_first: each chapter opens with a full-width photo, then the text in one column
- inline:      one column of text with photos set inside it, some breaking into the margin
- hybrid:      a dark masthead and full-width hero, chapter-opening photos and inline photos

All four are one self-contained HTML file (photos as data URIs), with [TK] notes in
yellow and [ASK ...] notes for the editor in blue, and an optional closing strip of
photos for spaces that are closed on the trip dates ("when you come back").
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from typing import Sequence

from .layout import Section, _inline, split_sections

STYLES = ("zigzag", "image_first", "inline", "hybrid")
_NOTES = ("insider notes", "before you go")


@dataclass
class Photo:
    src: str  # data URI
    caption: str = ""
    orientation: str = "landscape"


@dataclass
class Page:
    hotel: str
    headline: str
    standfirst: str
    body: str
    kicker: str = ""
    hero: Photo | None = None
    photos: dict[int, Photo] = field(default_factory=dict)  # section index -> photo
    pull_quote: str = ""
    coda: Sequence[Photo] = ()
    coda_label: str = "When you come back in the warmer months"
    gallery: Sequence[Photo] = ()  # supplied photos the sections did not use
    gallery_label: str = "More of the hotel"
    footer: str = "Sample draft for internal review, not for publication."


BASE_CSS = """
*{box-sizing:border-box} html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font-family:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif}
img{display:block;width:100%;height:auto}
.kicker{font:600 11px/1.2 "Helvetica Neue",Arial,sans-serif;letter-spacing:.24em;text-transform:uppercase}
h1{font-weight:500;margin:0}
h2{font:600 12px/1.3 "Helvetica Neue",Arial,sans-serif;letter-spacing:.2em;text-transform:uppercase;color:var(--accent);margin:0 0 14px}
p,li{font-size:17px;line-height:1.7;margin:0 0 14px;overflow-wrap:break-word}
figcaption{font:12px/1.45 "Helvetica Neue",Arial,sans-serif;color:var(--muted);margin-top:8px}
mark.tk{background:#fff0b8;color:#4a3b00;padding:0 3px;font:12px/1.4 "Helvetica Neue",Arial,sans-serif}
mark.ask{display:inline-block;background:#e6f0ff;color:#0b3d91;border:1px solid #9bbcf0;border-radius:4px;padding:2px 8px;margin:2px 0;font:12px/1.45 "Helvetica Neue",Arial,sans-serif}
mark.ask b{font-weight:700;margin-right:4px}
mark.ask b::before{content:"→ "}
blockquote{margin:36px auto;max-width:760px;text-align:center;font-size:26px;line-height:1.35;font-style:italic;color:var(--accent);padding:0 24px}
footer{max-width:760px;margin:40px auto 60px;padding:16px 16px 0;border-top:1px solid var(--rule);font:12px/1.6 "Helvetica Neue",Arial,sans-serif;color:var(--muted)}
.coda{max-width:1180px;margin:56px auto 0;padding:0 24px}
.coda .lbl{font:600 11px/1.3 "Helvetica Neue",Arial,sans-serif;letter-spacing:.22em;text-transform:uppercase;color:var(--accent);margin:0 0 12px}
.coda .grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
.coda figure{margin:0}.coda img{aspect-ratio:1/1;object-fit:cover}
@media (max-width:700px){.coda .grid{grid-template-columns:1fr}.coda{padding:0 16px}}
@media print{figure,.row{break-inside:avoid}}
"""

CSS = {
    "zigzag": """
:root{--bg:#f7f1e8;--ink:#231c17;--accent:#b0512b;--muted:#7a6d61;--rule:#dccbb6}
.hero{position:relative;min-height:92vh;display:flex;align-items:flex-end;color:#fff;background:#231c17 center/cover no-repeat}
.hero.plain{min-height:0;padding-top:72px}
.hero::after{content:"";position:absolute;inset:0;background:linear-gradient(180deg,rgba(0,0,0,0) 30%,rgba(0,0,0,.55) 62%,rgba(0,0,0,.85))}
.hero.plain::after{display:none}
.hero .t{position:relative;z-index:1;max-width:1100px;margin:0 auto;width:100%;padding:0 32px 64px}
.hero h1{font-size:clamp(34px,5.4vw,64px);line-height:1.05;margin:16px 0 18px;max-width:900px}
.hero .sf{font-size:clamp(17px,1.6vw,21px);line-height:1.5;font-style:italic;max-width:680px;opacity:.95}
.hero .hc{position:absolute;right:20px;bottom:14px;z-index:1;font:11px "Helvetica Neue",Arial,sans-serif;opacity:.75}
.intro{max-width:720px;margin:72px auto 40px;padding:0 24px}
.intro p:first-child::first-letter{float:left;font-size:64px;line-height:.85;padding:6px 10px 0 0;color:var(--accent)}
.row{display:grid;grid-template-columns:1fr 1fr;gap:56px;align-items:start;max-width:1180px;margin:0 auto 88px;padding:0 32px}
.row.flip figure{order:2}
.row figure{margin:0;position:sticky;top:28px}
.row figure img{aspect-ratio:4/5;object-fit:cover}
.row .txt{max-width:520px}
.row.text-only{grid-template-columns:1fr;max-width:760px}
.notes{background:#efe3d3;padding:56px 32px;margin-top:20px}
.notes>div{max-width:760px;margin:0 auto}
@media (max-width:820px){.row figure{position:static}.row{grid-template-columns:1fr;gap:20px;padding:0 16px;margin-bottom:56px}.row.flip figure{order:0}
.row figure img{aspect-ratio:4/3}.hero .t{padding:0 16px 40px}.intro{padding:0 16px;margin-top:40px}}
""",
    "image_first": """
:root{--bg:#fbfaf7;--ink:#17201c;--accent:#9a7a3c;--muted:#6c716c;--rule:#d9d3c4}
.mast{background:#1f3b33;color:#efe6cf;padding:72px 24px 56px;text-align:center}
.mast h1{font-size:clamp(34px,5vw,60px);line-height:1.08;margin:18px auto 14px;max-width:920px}
.mast .hotel{font:600 13px "Helvetica Neue",Arial,sans-serif;letter-spacing:.3em;text-transform:uppercase;color:#c9a95f}
.mast .sf{font-size:19px;line-height:1.55;font-style:italic;max-width:640px;margin:22px auto 0;opacity:.92}
.bleed{margin:0}
.bleed img{width:100%;height:78vh;min-height:360px;object-fit:cover}
.bleed figcaption{max-width:720px;margin:10px auto 0;padding:0 24px}
.col{max-width:680px;margin:0 auto;padding:44px 24px 72px}
.col.first p:first-child{font-size:21px;line-height:1.6}
.num{font:500 44px/1 "Iowan Old Style",Georgia,serif;color:var(--accent);margin-bottom:8px}
.notes{background:#1f3b33;color:#efe6cf}
.notes .col{padding-top:56px}
.notes h2{color:#c9a95f}
@media (max-width:700px){.bleed img{height:56vh}.col{padding:28px 16px 48px}.mast{padding:48px 16px 40px}}
""",
    "inline": """
:root{--bg:#f6f7f9;--ink:#1b1d24;--accent:#7d1f2c;--muted:#6b7080;--rule:#cfd5df}
.cover{max-width:1080px;margin:0 auto;padding:64px 32px 0}
.cover .kicker{color:var(--accent)}
.cover h1{font-size:clamp(36px,6vw,76px);line-height:1;letter-spacing:-.01em;margin:18px 0 0;max-width:980px}
.cover .meta{display:grid;grid-template-columns:2fr 1fr;gap:40px;align-items:end;margin:28px 0 30px;border-top:2px solid var(--ink);padding-top:18px}
.cover .sf{font-size:20px;line-height:1.5;font-style:italic;margin:0}
.cover .hotel{font:600 12px "Helvetica Neue",Arial,sans-serif;letter-spacing:.24em;text-transform:uppercase;text-align:right;color:var(--muted)}
.cover figure{margin:0}
.cover figure img{aspect-ratio:16/9;object-fit:cover}
""",
    "hybrid": """
:root{--bg:#fbfaf7;--ink:#17201c;--accent:#7a5a1f;--muted:#6c716c;--rule:#d9d3c4}
.mast{background:#1f3b33;color:#efe6cf;padding:72px 24px 56px;text-align:center}
.mast .kicker{color:#c9a95f}
.mast h1{font-size:clamp(34px,5vw,60px);line-height:1.08;margin:18px auto 14px;max-width:960px}
.mast .hotel{font:600 13px "Helvetica Neue",Arial,sans-serif;letter-spacing:.3em;text-transform:uppercase;color:#c9a95f}
.mast .sf{font-size:19px;line-height:1.55;font-style:italic;max-width:660px;margin:22px auto 0;opacity:.92}
figure.bleed{margin:0}
figure.bleed img{width:100%;height:76vh;min-height:340px;object-fit:cover}
figure.bleed figcaption{max-width:660px;margin:10px auto 0;padding:0 24px}
figure.opener{margin-top:56px}
figure.opener img{aspect-ratio:16/7}
@media (max-width:700px){figure.bleed img{height:56vh}.mast{padding:48px 16px 40px}figure.opener img{aspect-ratio:4/3}}
""",
}
# The one-column article body shared by inline and hybrid.
ARTICLE_CSS = """
article{padding:48px 24px 0;overflow-x:clip}
.sec{max-width:660px;margin:0 auto 30px}
.sec h2{border-top:1px solid var(--rule);padding-top:22px;margin-top:10px}
figure.wide{width:min(1040px,calc(100vw - 32px));margin:30px 0 34px 50%;transform:translateX(-50%)}
figure.wide img{aspect-ratio:21/9;object-fit:cover}
figure.wide figcaption{max-width:660px;margin-left:auto;margin-right:auto}
figure.inset{float:right;width:340px;margin:8px -190px 18px 30px}
figure.inset.l{float:left;margin:8px 30px 18px -190px}
figure.inset img{aspect-ratio:4/5;object-fit:cover}
.sec::after{content:"";display:block;clear:both}
.lede p:first-child{font-size:21px;line-height:1.6}
.lede p:first-child::first-letter{float:left;font-size:70px;line-height:.82;padding:6px 10px 0 0;color:var(--accent)}
.notes{max-width:660px;border:1px solid var(--ink);padding:28px 32px;margin:40px auto 0;background:#fff}
@media (max-width:1120px){figure.inset{width:42%;margin-right:0}figure.inset.l{margin-left:0}}
@media (max-width:820px){figure.inset,figure.inset.l{float:none;width:100%;margin:18px 0}
.cover,article{padding-left:16px;padding-right:16px}.cover .meta{grid-template-columns:1fr;gap:10px}.cover .hotel{text-align:left}
figure.wide img{aspect-ratio:4/3}}
"""


def _figure(photo: Photo, css: str = "") -> str:
    cap = f"<figcaption>{html.escape(photo.caption)}</figcaption>" if photo.caption else ""
    return f'<figure class="{css}"><img src="{photo.src}" alt="{html.escape(photo.caption)}">{cap}</figure>'


def _paragraphs(section: Section) -> list[str]:
    out = []
    for block in section.paragraphs:
        lines = [line for line in block.split("\n") if line.strip()]
        if lines and all(line.lstrip().startswith(("- ", "* ")) for line in lines):
            out.append("<ul>" + "".join(f"<li>{_inline(line.lstrip()[2:])}</li>" for line in lines) + "</ul>")
        else:
            out.append(f"<p>{_inline(' '.join(line.strip() for line in lines))}</p>")
    return out


def _is_notes(section: Section) -> bool:
    return section.heading.lower().startswith(_NOTES)


def _split_headline(headline: str, hotel: str) -> tuple[str, str]:
    line, _, name = headline.partition("|")
    return line.strip(), name.strip() or hotel


def _head(title: str, css: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>'
        f"<style>{BASE_CSS}{css}</style></head><body>"
    )


def _zigzag(page: Page, sections: list[Section]) -> list[str]:
    line, _hotel = _split_headline(page.headline, page.hotel)
    style = f' style="background-image:url(\'{page.hero.src}\')"' if page.hero else ""
    out = [_head(page.headline, CSS["zigzag"]), (
        f'<header class="hero{"" if page.hero else " plain"}"{style}><div class="t">'
        f'<div class="kicker">{html.escape(page.kicker)}</div><h1>{_inline(line)}</h1>'
        f'<div class="sf">{_inline(page.standfirst)}</div></div>'
        + (f'<span class="hc">{html.escape(page.hero.caption)}</span>' if page.hero and page.hero.caption else "")
        + "</header>"
    )]
    flip = False
    for index, section in enumerate(sections):
        paragraphs = "".join(_paragraphs(section))
        heading = f"<h2>{_inline(section.heading)}</h2>" if section.heading else ""
        if index == 0 and not section.heading:
            out.append(f'<section class="intro">{paragraphs}</section>')
            if page.pull_quote:
                out.append(f"<blockquote>{_inline(page.pull_quote)}</blockquote>")
        elif _is_notes(section):
            out.append(f'<section class="notes"><div>{heading}{paragraphs}</div></section>')
        elif index in page.photos:
            out.append(
                f'<section class="row{" flip" if flip else ""}">{_figure(page.photos[index])}'
                f'<div class="txt">{heading}{paragraphs}</div></section>'
            )
            flip = not flip
        else:
            out.append(f'<section class="row text-only"><div class="txt">{heading}{paragraphs}</div></section>')
    return out


def _image_first(page: Page, sections: list[Section]) -> list[str]:
    line, hotel = _split_headline(page.headline, page.hotel)
    out = [_head(page.headline, CSS["image_first"]), (
        f'<header class="mast"><div class="kicker">{html.escape(page.kicker)}</div><h1>{_inline(line)}</h1>'
        f'<div class="hotel">{html.escape(hotel)}</div><div class="sf">{_inline(page.standfirst)}</div></header>'
    )]
    if page.hero:
        out.append(_figure(page.hero, "bleed"))
    number = 0
    for index, section in enumerate(sections):
        paragraphs = "".join(_paragraphs(section))
        if index == 0 and not section.heading:
            out.append(f'<section class="col first">{paragraphs}</section>')
            if page.pull_quote:
                out.append(f"<blockquote>{_inline(page.pull_quote)}</blockquote>")
            continue
        if _is_notes(section):
            out.append(f'<section class="notes"><div class="col"><h2>{_inline(section.heading)}</h2>{paragraphs}</div></section>')
            continue
        if index in page.photos:
            out.append(_figure(page.photos[index], "bleed"))
        number += 1
        out.append(
            f'<section class="col"><div class="num">{number:02d}</div>'
            f'<h2>{_inline(section.heading)}</h2>{paragraphs}</section>'
        )
    return out


def _article(page: Page, sections: list[Section], *, openers: bool) -> list[str]:
    """The one-column body: photos inside the text; with openers, landscape photos open the chapter."""
    out = ["<article>"]
    left = False
    for index, section in enumerate(sections):
        paragraphs = _paragraphs(section)
        heading = f"<h2>{_inline(section.heading)}</h2>" if section.heading else ""
        if _is_notes(section):
            out.append(f'<section class="notes">{heading}{"".join(paragraphs)}</section>')
            continue
        photo = page.photos.get(index)
        opener = ""
        if photo is not None:
            if openers and section.heading and photo.orientation == "landscape":
                opener = _figure(photo, "wide opener")
            else:
                wide = photo.orientation == "landscape" and not openers and index % 3 == 2
                css = "wide" if wide else ("inset l" if left else "inset")
                if not wide:
                    left = not left
                paragraphs.insert(min(1, len(paragraphs)), _figure(photo, css))
        css = "sec lede" if index == 0 and not section.heading else "sec"
        out.append(f'{opener}<section class="{css}">{heading}{"".join(paragraphs)}</section>')
        if index == 0 and page.pull_quote:
            out.append(f"<blockquote>{_inline(page.pull_quote)}</blockquote>")
    out.append("</article>")
    return out


def _inline_page(page: Page, sections: list[Section]) -> list[str]:
    line, hotel = _split_headline(page.headline, page.hotel)
    return [_head(page.headline, CSS["inline"] + ARTICLE_CSS), (
        f'<header class="cover"><div class="kicker">{html.escape(page.kicker)}</div><h1>{_inline(line)}</h1>'
        f'<div class="meta"><p class="sf">{_inline(page.standfirst)}</p><div class="hotel">{html.escape(hotel)}</div></div>'
        + (_figure(page.hero) if page.hero else "") + "</header>"
    ), *_article(page, sections, openers=False)]


def _hybrid(page: Page, sections: list[Section]) -> list[str]:
    line, hotel = _split_headline(page.headline, page.hotel)
    return [_head(page.headline, CSS["inline"] + ARTICLE_CSS + CSS["hybrid"]), (
        f'<header class="mast"><div class="kicker">{html.escape(page.kicker)}</div><h1>{_inline(line)}</h1>'
        f'<div class="hotel">{html.escape(hotel)}</div><div class="sf">{_inline(page.standfirst)}</div></header>'
        + (_figure(page.hero, "bleed") if page.hero else "")
    ), *_article(page, sections, openers=True)]


def render_styled_html(page: Page, style: str) -> str:
    if style not in STYLES:
        raise ValueError(f"unknown layout style {style!r}")
    sections = split_sections(page.body)
    builder = {"zigzag": _zigzag, "image_first": _image_first, "inline": _inline_page, "hybrid": _hybrid}[style]
    parts = builder(page, sections)
    if page.gallery:
        parts.append(
            f'<section class="coda"><p class="lbl">{html.escape(page.gallery_label)}</p>'
            f'<div class="grid">{"".join(_figure(photo) for photo in page.gallery)}</div></section>'
        )
    if page.coda:
        parts.append(
            f'<section class="coda"><p class="lbl">{html.escape(page.coda_label)}</p>'
            f'<div class="grid">{"".join(_figure(photo) for photo in page.coda)}</div></section>'
        )
    parts.append(f"<footer>{html.escape(page.footer)}</footer></body></html>")
    return "".join(parts)
