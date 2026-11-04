"""Editorial layout: a sample draft plus cleared photos -> one self-contained HTML file.

The page is a fixed magazine-style template (cover with hero photo, then
sections with one photo each) so the layout quality does not depend on a
model writing HTML. Photos are embedded as data URIs, so the single file
opens with its images straight from Slack or a browser.
"""

from __future__ import annotations

import base64
import html
import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger(__name__)

# Embedded photos are re-encoded to this size to keep the file light.
EMBED_MAX_EDGE = 1400


@dataclass
class Section:
    heading: str
    paragraphs: list[str] = field(default_factory=list)


# Art-direction notes a writer may still slip in; photos are placed by the layout step.
_DIRECTION_NOTE = re.compile(r"\[(?:IMAGE|PHOTO|LAYOUT|GRAPHIC|VISUAL)\b[^\[\]]*(?:\[[^\[\]]*\][^\[\]]*)*\]", re.I)


def strip_direction_notes(body: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", _DIRECTION_NOTE.sub("", body)).strip()


def split_sections(body: str) -> list[Section]:
    """Intro (heading '') followed by one section per '## ' subhead."""
    sections = [Section(heading="")]
    for block in re.split(r"\n\s*\n", strip_direction_notes(body)):
        block = block.strip()
        if not block:
            continue
        heading = re.match(r"^##\s*(.+?)\s*(?:\n|$)", block)
        if heading:
            sections.append(Section(heading=heading.group(1).strip()))
            rest = block[heading.end():].strip()
            if rest:
                sections[-1].paragraphs.append(rest)
        else:
            sections[-1].paragraphs.append(block)
    return [s for s in sections if s.heading or s.paragraphs]


def _inline(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<em>\1</em>", escaped)
    return re.sub(r"\[TK:?([^\]]*)\]", r'<mark class="tk">[TK:\1]</mark>', escaped)


def embed_image(path: str) -> str:
    """A JPEG data URI for a local photo, downscaled for a light single-file page."""
    from PIL import Image

    with Image.open(path) as picture:
        picture = picture.convert("RGB")
        picture.thumbnail((EMBED_MAX_EDGE, EMBED_MAX_EDGE))
        buffer = io.BytesIO()
        picture.save(buffer, "JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


def _figure(asset: dict[str, Any] | None, caption: str, css: str) -> str:
    if not asset:
        return ""
    src = embed_image(str(asset.get("local_path") or asset.get("url")))
    cap = f"<figcaption>{html.escape(caption)}</figcaption>" if caption else ""
    return f'<figure class="{css} {asset.get("orientation", "landscape")}"><img src="{src}" alt="">{cap}</figure>'


# List-style closing sections read better without a photo slot.
_NO_SLOT = re.compile(r"insider notes|before you go", re.I)


def _slot(shot: str, css: str) -> str:
    """An empty photo frame the editor fills in the browser (click or drop a file)."""
    hint = html.escape(shot)
    suggestion = f"<small>Suggested: {hint}</small>" if hint else ""
    return (
        f'<figure class="slot {css}" data-slot>'
        f'<div class="slot-box"><span>+ Add photo</span>{suggestion}</div>'
        f'<figcaption contenteditable="true">{hint}</figcaption></figure>'
    )


def render_editorial_html(
    *, hotel: str, headline: str, standfirst: str, body: str,
    images: dict[str, dict[str, Any]], plan: dict[str, Any], footer_notes: Sequence[str] = (),
) -> str:
    sections = split_sections(body)
    hero = images.get(str(plan.get("hero") or ""))
    by_section = {
        int(item.get("section", -1)): item for item in plan.get("sections") or []
        if isinstance(item, dict) and str(item.get("image")) in images
    }
    shots = {
        int(item.get("section", -1)): str(item.get("shot") or "").strip()
        for item in plan.get("shots") or [] if isinstance(item, dict)
    }
    pull_quote = str(plan.get("pull_quote") or "").strip()
    quote_after = min(2, max(len(sections) - 1, 0))
    parts: list[str] = []
    for index, section in enumerate(sections):
        figure = ""
        choice = by_section.get(index)
        if choice:
            figure = _figure(images[str(choice["image"])], str(choice.get("caption") or ""),
                             "inline" if index % 2 else "inline alt")
        elif section.heading and not _NO_SLOT.search(section.heading):
            figure = _slot(shots.get(index, ""), "inline")
        heading = f"<h2>{_inline(section.heading)}</h2>" if section.heading else ""
        paragraphs = [f"<p>{_inline(p)}</p>" for p in section.paragraphs]
        # Keep the heading, its photo and the first paragraph together across page breaks.
        lead = f'<div class="lead">{heading}{figure}{paragraphs[0] if paragraphs else ""}</div>'
        css = "intro" if not section.heading else "section"
        parts.append(f'<section class="{css}">{lead}{"".join(paragraphs[1:])}</section>')
        if pull_quote and index == quote_after:
            parts.append(f'<blockquote>{_inline(pull_quote)}</blockquote>')
    hero_style, cover_class = "", "cover plain"
    cover_slot = (
        '<button class="cover-slot" type="button" data-cover>+ Add cover photo'
        f'<small>Suggested: {html.escape(str(plan.get("hero_shot") or "the hotel at its most inviting"))}</small></button>'
    )
    if hero:
        cover_class = "cover"
        cover_slot = ""
        hero_style = f' style="background-image:url(\'{embed_image(str(hero.get("local_path")))}\')"'
    notes = "".join(f"<li>{_inline(n)}</li>" for n in footer_notes if n)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(headline)}</title>
<style>
@page {{ size: A4; margin: 0; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; font-family: "Iowan Old Style", "Palatino", Georgia, serif; color: #1d1b19; background: #fbf8f3; }}
.cover {{ height: 297mm; position: relative; background: #1d1b19 center/cover no-repeat; page-break-after: always; }}
.cover::after {{ content: ""; position: absolute; inset: 0; background: linear-gradient(180deg, rgba(0,0,0,0) 30%, rgba(0,0,0,.55) 58%, rgba(0,0,0,.86)); }}
/* Without a hero photo a full-page dark cover is an empty screen: use a compact title band. */
.cover.plain {{ height: auto; padding: 30mm 22mm 18mm; page-break-after: auto; }}
.cover.plain::after {{ display: none; }}
.cover.plain .cover-text {{ position: static; }}
.cover-text {{ position: absolute; left: 22mm; right: 22mm; bottom: 26mm; z-index: 1; color: #fff; }}
.kicker {{ font: 600 9pt/1 "Helvetica Neue", Arial, sans-serif; letter-spacing: .22em; text-transform: uppercase; opacity: .9; }}
h1 {{ font-size: 40pt; line-height: 1.05; margin: 6mm 0 5mm; font-weight: 500; }}
.standfirst {{ font-size: 14pt; line-height: 1.45; max-width: 140mm; opacity: .95; font-style: italic; }}
main {{ padding: 20mm 24mm 16mm; }}
section {{ margin: 0 0 6mm; }}
.lead {{ break-inside: avoid; page-break-inside: avoid; }}
h2 {{ break-after: avoid; page-break-after: avoid; font: 600 10pt/1.2 "Helvetica Neue", Arial, sans-serif; letter-spacing: .18em; text-transform: uppercase; color: #8a5a2b; margin: 9mm 0 4mm; }}
p {{ font-size: 11.5pt; line-height: 1.65; margin: 0 0 4mm; }}
.intro .lead p:first-of-type::first-letter {{ float: left; font-size: 46pt; line-height: .9; padding: 2mm 3mm 0 0; color: #8a5a2b; }}
figure {{ margin: 2mm 0 6mm; page-break-inside: avoid; }}
figure img {{ width: 100%; max-height: 120mm; object-fit: cover; display: block; }}
figure.portrait {{ float: right; width: 46%; margin: 1mm 0 4mm 7mm; }}
figure.alt.portrait {{ float: left; margin: 1mm 7mm 4mm 0; }}
figcaption {{ font: 8.5pt/1.4 "Helvetica Neue", Arial, sans-serif; color: #6d655c; margin-top: 2mm; }}
section::after {{ content: ""; display: block; clear: both; }}
.slot-box {{ aspect-ratio: 3 / 2; border: 1.5px dashed #c9b79f; background: #f3ece2; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 2mm; cursor: pointer; text-align: center; padding: 6mm; font: 600 10pt/1.3 "Helvetica Neue", Arial, sans-serif; color: #8a5a2b; }}
.slot-box small {{ font-weight: 400; color: #6d655c; max-width: 110mm; }}
.slot.drag .slot-box, .cover.drag {{ outline: 3px solid #8a5a2b; }}
.slot figcaption:empty::before {{ content: "Caption"; color: #aaa; }}
.cover-slot {{ margin-top: 7mm; display: flex; flex-direction: column; gap: 1mm; align-items: flex-start; border: 1.5px dashed currentColor; background: transparent; color: inherit; padding: 3mm 5mm; cursor: pointer; font: 600 10pt/1.3 "Helvetica Neue", Arial, sans-serif; text-align: left; }}
.cover-slot small {{ font-weight: 400; opacity: .75; }}
.cover:not(.plain) .cover-slot {{ display: none; }}
.tools {{ position: fixed; right: 16px; bottom: 16px; z-index: 5; display: flex; gap: 8px; }}
.tools button {{ font: 600 10pt "Helvetica Neue", Arial, sans-serif; padding: 9px 14px; border: 0; border-radius: 4px; background: #1d1b19; color: #fff; cursor: pointer; }}
@media print {{ .tools, .cover-slot, figure.slot:not(.filled) {{ display: none !important; }} }}
blockquote {{ margin: 8mm 6mm; padding: 5mm 0; border-top: 1px solid #c9b79f; border-bottom: 1px solid #c9b79f; font-size: 18pt; line-height: 1.35; font-style: italic; text-align: center; color: #3b2f25; page-break-inside: avoid; }}
mark.tk {{ background: #fff0b8; padding: 0 1mm; font-family: "Helvetica Neue", Arial, sans-serif; font-size: 9pt; }}
footer {{ margin: 10mm 24mm 18mm; padding-top: 4mm; border-top: 1px solid #d8cbb9; font: 8pt/1.5 "Helvetica Neue", Arial, sans-serif; color: #6d655c; page-break-inside: avoid; }}
footer ul {{ margin: 2mm 0 0; padding-left: 4mm; }}
h1, h2, p {{ overflow-wrap: break-word; }}
/* Phones (Slack mobile opens the file in a narrow view): smaller type and gutters, no floats. */
@media screen and (max-width: 700px) {{
  .cover {{ height: 78vh; }}
  .cover.plain {{ height: auto; padding: 18mm 16px 10mm; }}
  .cover-text {{ left: 16px; right: 16px; bottom: 12mm; }}
  h1 {{ font-size: 28pt; }}
  .standfirst {{ font-size: 12.5pt; max-width: none; }}
  main {{ padding: 10mm 16px 8mm; }}
  footer {{ margin: 8mm 16px 12mm; }}
  figure.portrait, figure.alt.portrait {{ float: none; width: 100%; margin: 2mm 0 6mm; }}
  blockquote {{ margin: 6mm 0; font-size: 15pt; }}
}}
</style></head>
<body>
<div class="{cover_class}"{hero_style}><div class="cover-text">
<div class="kicker">Suite Stories · {html.escape(hotel)}</div>
<h1>{_inline(headline)}</h1>
<div class="standfirst">{_inline(standfirst)}</div>
{cover_slot}
</div></div>
<main>{''.join(parts)}</main>
<footer><strong>Sample draft for internal review, not for publication.</strong>
{"Photos supplied by the hotel." if hero or by_section else ""}<ul>{notes}</ul></footer>
<div class="tools"><button type="button" data-save>Save with photos</button></div>
<input type="file" accept="image/*" id="photo-input" hidden>
<script>{_PHOTO_SCRIPT}</script>
</body></html>"""


# Fills photo slots in the browser and saves the page with the photos embedded.
_PHOTO_SCRIPT = """
(function () {
  var input = document.getElementById('photo-input'), target = null;
  function place(el, file) {
    if (!file || !/^image\\//.test(file.type)) return;
    var reader = new FileReader();
    reader.onload = function () {
      if (el.classList.contains('cover')) {
        el.style.backgroundImage = "url('" + reader.result + "')";
        el.classList.remove('plain');
        return;
      }
      var box = el.querySelector('.slot-box'), img = el.querySelector('img');
      if (!img) { img = document.createElement('img'); img.alt = ''; el.insertBefore(img, box); }
      img.src = reader.result;
      if (box) box.remove();
      el.classList.add('filled');
    };
    reader.readAsDataURL(file);
  }
  function pick(el) { target = el; input.value = ''; input.click(); }
  input.addEventListener('change', function () { if (target) place(target, input.files[0]); });
  document.addEventListener('click', function (e) {
    var box = e.target.closest('.slot-box, figure.slot img');
    if (box) return pick(box.closest('figure'));
    if (e.target.closest('[data-cover]')) return pick(document.querySelector('.cover'));
    if (e.target.closest('[data-save]')) {
      var html = '<!doctype html>\\n' + document.documentElement.outerHTML;
      var link = document.createElement('a');
      link.href = URL.createObjectURL(new Blob([html], {type: 'text/html'}));
      link.download = (document.title || 'editorial').replace(/[^\\w-]+/g, '-') + '.html';
      link.click();
    }
  });
  document.querySelectorAll('figure.slot, .cover').forEach(function (el) {
    el.addEventListener('dragover', function (e) { e.preventDefault(); el.classList.add('drag'); });
    el.addEventListener('dragleave', function () { el.classList.remove('drag'); });
    el.addEventListener('drop', function (e) {
      e.preventDefault(); el.classList.remove('drag');
      place(el, e.dataTransfer.files[0]);
    });
  });
})();
"""
