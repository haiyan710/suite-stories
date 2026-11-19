"""Korean hotel-guide page: the web itinerary format the booking site publishes.

A separate output from the English magazine layouts: a hotel-centred N-night
plan as a header with a theme card, a hero photo, one card per day with timed
moments (오전 / 오후 / 저녁), a trip-info box and a getting-around tip, plus
schema.org TouristTrip data. It carries no site branding: the team pastes the
text into the site's own template, so the page only has to match its shape.

`validate_guide` is the Python check of the structure; `render_guide_html`
builds one self-contained file with the hotel's photos embedded.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any, Mapping

from .layout import embed_image

TIME_LABELS = ("오전", "오후", "저녁")
# The journey page's finer day (the booking site's own hotel journeys use these).
JOURNEY_TIMES = ("아침", "오전", "점심", "오후", "늦은 오후", "이른 저녁", "저녁", "밤")
MAX_JOURNEY_MOMENT_CHARS = 600
# Facts the journey intro must leave to the season tips: temperatures, sunset times, rain counts.
INTRO_FACTS = re.compile(
    r"\d+\s*(?:도|°)|(?:해가?|해는|일몰)[^.]{0,25}\d|(?:\d+\s*일|하루|이틀|사흘|나흘)\s*(?:중|쯤|정도)?은?[^.]{0,8}비"
)
# Booking-platform benefits (credits, upgrades, breakfast for two) are the platform's
# policy, not the hotel's and not the bot's to state: never written on a page.
PLATFORM_BENEFIT = re.compile(
    r"크레딧|credit|[$€₩]\s?\d[\d,.]*\s*(?:상당|어치)?\s*(?:의\s*)?혜택|\d\s?(?:달러|유로)\s*(?:상당|혜택)|업그레이드\s*혜택|"
    r"2인\s*조식|조식\s*2인|"
    r"투숙객\s*(?:특별\s*)?혜택|예약\s*혜택|멤버십\s*혜택",
    re.I,
)
CHECK_MARK = re.compile(r"\[확인 필요[^\]]*\]")
# A moment line of more than this many characters no longer reads as a card.
MAX_MOMENT_CHARS = 160


def validate_guide(guide: Mapping[str, Any], *, nights: int) -> list[str]:
    """Structure problems Python can see, as fix instructions (empty when the shape is right)."""
    issues: list[str] = []
    for key in ("hotel_ko", "title", "summary", "theme", "travel_tip"):
        if not str(guide.get(key) or "").strip():
            issues.append(f"Fill in '{key}'.")
    days = [d for d in guide.get("days") or [] if isinstance(d, dict)]
    if nights and len(days) != nights + 1:
        issues.append(f"Write exactly {nights + 1} days ({nights}박 {nights + 1}일); there are {len(days)}.")
    for day in days:
        number = day.get("day")
        moments = [m for m in day.get("moments") or [] if isinstance(m, dict)]
        if not 1 <= len(moments) <= 3:
            issues.append(f"Day {number}: give 2-3 moments (there are {len(moments)}).")
        order = [TIME_LABELS.index(m.get("time")) for m in moments if m.get("time") in TIME_LABELS]
        if len(order) != len(moments):
            issues.append(f"Day {number}: every moment's time must be 오전, 오후 or 저녁.")
        elif order != sorted(order):
            issues.append(f"Day {number}: put the moments in time order (오전, 오후, 저녁).")
        for moment in moments:
            if len(str(moment.get("text") or "")) > MAX_MOMENT_CHARS:
                issues.append(f"Day {number} {moment.get('time')}: shorten '{moment.get('title')}' to one or two short sentences.")
    if re.search(r"[A-Za-z]{4,}\s+[A-Za-z]{4,}\s+[A-Za-z]{4,}", str(guide.get("summary") or "")):
        issues.append("The summary reads as English: write it in Korean.")
    return issues + _benefit_issues(guide)


def _benefit_issues(guide: Mapping[str, Any]) -> list[str]:
    found = sorted(set(PLATFORM_BENEFIT.findall(json.dumps(guide, ensure_ascii=False))))
    return [
        f"Remove the booking-platform benefit ({', '.join(found[:3])}): credits, upgrades and breakfast offers "
        "are the platform's policy and are never written on the page."
    ] if found else []


def validate_journey(guide: Mapping[str, Any], *, nights: int) -> list[str]:
    """Structure of the hotel-journey page (long intro, 3-8 timed moments a day), as fix instructions."""
    issues: list[str] = []
    for key in ("hotel_ko", "title", "intro", "criteria_line", "move_tip"):
        if not str(guide.get(key) or "").strip():
            issues.append(f"Fill in '{key}'.")
    intro = str(guide.get("intro") or "")
    if intro and not 350 <= len(intro) <= 900:
        issues.append(f"Write the intro as a mood story of about 500-750 characters (it has {len(intro)}).")
    if INTRO_FACTS.search(intro):
        issues.append(
            "Take the weather numbers and sunset time out of the intro: the season tips carry them; "
            "write the season as mood (slower days, long lunches, warm evenings)."
        )
    tips = [t for t in guide.get("season_tips") or [] if str(t).strip()]
    if not 3 <= len(tips) <= 4:
        issues.append(f"Give 3-4 season tips (there are {len(tips)}).")
    if not 4 <= len(guide.get("highlights") or []) <= 6:
        issues.append("Give 4-6 highlights for the criteria card.")
    days = [d for d in guide.get("days") or [] if isinstance(d, dict)]
    if nights and len(days) != nights + 1:
        issues.append(f"Write exactly {nights + 1} days ({nights}박 {nights + 1}일); there are {len(days)}.")
    for index, day in enumerate(days):
        number = day.get("day")
        moments = [m for m in day.get("moments") or [] if isinstance(m, dict)]
        least = 3 if index in (0, len(days) - 1) else 4
        if not least <= len(moments) <= 8:
            issues.append(f"Day {number}: give {least}-8 moments (there are {len(moments)}).")
        order = [JOURNEY_TIMES.index(m.get("time")) for m in moments if m.get("time") in JOURNEY_TIMES]
        if len(order) != len(moments):
            issues.append(f"Day {number}: every moment's time must be one of {', '.join(JOURNEY_TIMES)}.")
        elif order != sorted(order):
            issues.append(f"Day {number}: put the moments in time order ({' → '.join(JOURNEY_TIMES)}).")
        styled = [m for m in moments if str(m.get("style") or "").strip()]
        if len(styled) == 1:
            issues.append(f"Day {number}: a style option needs at least one alternative at the same time, or none.")
        for moment in moments:
            if len(str(moment.get("text") or "")) > MAX_JOURNEY_MOMENT_CHARS:
                issues.append(f"Day {number} {moment.get('time')}: shorten '{moment.get('title')}' to at most five sentences.")
    if days:
        first = json.dumps(days[0], ensure_ascii=False)
        last = json.dumps(days[-1], ensure_ascii=False)
        if "체크인" not in first:
            issues.append("Day 1: include check-in at the hotel (its time, or [확인 필요: 체크인 시간]).")
        if "체크아웃" not in last:
            issues.append(f"Day {days[-1].get('day')}: include check-out (its time and luggage storage, or [확인 필요]).")
    return issues + _benefit_issues(guide)


def hotel_tip_quotes_ok(quotes: list[str], hotel_facts: list[str]) -> bool:
    """Tips may be credited to the hotel only when each quote comes from what the hotel itself sent."""
    plain = lambda text: re.sub(r"\W+", "", str(text).lower())  # noqa: E731
    sources = [plain(f) for f in hotel_facts]
    quotes = [plain(q) for q in quotes if len(plain(q)) >= 12]
    return bool(quotes) and bool(sources) and all(any(q in s for s in sources) for q in quotes)


def dedupe_photos(guide: dict[str, Any]) -> dict[str, Any]:
    """Each photo appears once on the whole page: hero first, then day photos in order, then moments.

    A repeated photo reads as filler (an editor saw the same facade three times), so a slot
    that would repeat one is left empty and renders as a "사진 추가" box for the editor.
    """
    used: set[str] = set()

    def keep(key: Any) -> str:
        key = str(key or "")
        if not key or key in used:
            return ""
        used.add(key)
        return key

    guide["hero_photo"] = keep(guide.get("hero_photo"))
    days = [d for d in guide.get("days") or [] if isinstance(d, dict)]
    for day in days:
        day["photo"] = keep(day.get("photo"))
    for day in days:
        for moment in day.get("moments") or []:
            if isinstance(moment, dict):
                moment["photo"] = keep(moment.get("photo"))
    return guide


# Captions of outdoor or seasonal spaces: a rooftop or courtyard photo on a winter
# day card shows a space the guest may not be able to use.
_OUTDOOR = re.compile(
    r"rooftop|roof terrace|terrace|pool|plunge|courtyard|garden|deck|sun ?lounger|beach|cabana|"
    r"open-air|outdoor|al fresco|patio|루프탑|테라스|수영장|정원|안뜰|야외",
    re.I,
)


def is_outdoor(asset: Mapping[str, Any]) -> bool:
    shows = f"{asset.get('evidence', '')} {asset.get('page_context', '')} {asset.get('document', '')}"
    return bool(_OUTDOOR.search(shows))


def fill_empty_days(
    guide: dict[str, Any], photo_ids: list[str], *, outdoor: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Give days without a photo the indoor hotel photos not used anywhere yet, in day order.

    Every day includes the hotel, so an unused room, lounge or restaurant photo suits a
    day card better than an empty box. Outdoor and seasonal spaces are never used to fill
    a gap: they appear only where the writer chose them for that moment.
    """
    used = {str(guide.get("hero_photo") or "")} | {
        str(x.get("photo") or "") for d in guide.get("days") or [] if isinstance(d, dict)
        for x in [d, *[m for m in d.get("moments") or [] if isinstance(m, dict)]]
    }
    spare = [pid for pid in photo_ids if pid not in used and pid not in outdoor]
    for day in guide.get("days") or []:
        if spare and isinstance(day, dict) and not day.get("photo"):
            day["photo"] = spare.pop(0)
    return guide


def guide_places(guide: Mapping[str, Any], limit: int = 8) -> list[str]:
    """The outside sights the writer marked for a destination photo, once each, in page order."""
    places: list[str] = []
    for day in guide.get("days") or []:
        for moment in (day.get("moments") or []) if isinstance(day, dict) else []:
            place = str(moment.get("place") or "").strip() if isinstance(moment, dict) else ""
            if place and place.lower() not in {p.lower() for p in places}:
                places.append(place)
    return places[:limit]


def apply_place_photos(guide: dict[str, Any], place_photos: Mapping[str, str]) -> dict[str, Any]:
    """Put each destination photo once: on its moment, or on the day card when that is empty.

    A day spent at a sight (a day trip, a museum morning) shows that sight on its card
    rather than an empty box; otherwise the photo sits beside the moment. Run after
    `dedupe_photos` and before `fill_empty_days`, so a hotel photo the writer chose is
    never replaced and spare hotel photos only fill what is still empty.
    """
    used: set[str] = set()
    for day in guide.get("days") or []:
        if not isinstance(day, dict):
            continue
        moments = [m for m in day.get("moments") or [] if isinstance(m, dict)]
        placed = [
            (m, place_photos[str(m.get("place") or "").strip()]) for m in moments
            if place_photos.get(str(m.get("place") or "").strip())
        ]
        for moment, photo_id in placed:
            if photo_id in used:
                continue
            if not day.get("photo"):
                day["photo"] = photo_id
            elif not moment.get("photo"):
                moment["photo"] = photo_id
            else:
                continue
            used.add(photo_id)
    return guide


def guide_check_items(guide: Mapping[str, Any]) -> list[str]:
    """Every [확인 필요: …] note left for the editor."""
    return CHECK_MARK.findall(json.dumps(guide, ensure_ascii=False))


def guide_jsonld(guide: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "@context": "https://schema.org", "@type": "TouristTrip",
        "name": str(guide.get("title") or ""), "description": str(guide.get("summary") or ""),
        "touristType": "럭셔리 호텔 여행객",
        "itinerary": {"@type": "ItemList", "itemListElement": [
            {"@type": "ListItem", "position": int(day.get("day") or i), "name": str(day.get("title") or ""),
             "description": str(day.get("summary") or "")}
            for i, day in enumerate(guide.get("days") or [], 1) if isinstance(day, dict)
        ]},
    }


CSS = """
:root{--ink:#1a1d24;--muted:#6b7280;--line:#e5e7eb;--soft:#f5f6f8;--primary:#1f5fa8;--primary-soft:#eef4fb}
*{box-sizing:border-box}
body{margin:0;background:#fff;color:var(--ink);font-family:"Pretendard","Apple SD Gothic Neo","Noto Sans KR","Malgun Gothic",system-ui,sans-serif;-webkit-font-smoothing:antialiased}
img{display:block;width:100%;height:100%;object-fit:cover}
.wrap{max-width:1040px;margin:0 auto;padding:40px 20px 64px}
.kicker{font-size:14px;font-weight:700;letter-spacing:.06em;color:var(--primary);margin:0}
.head{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:32px;align-items:start}
h1{font-size:clamp(28px,4.4vw,46px);line-height:1.25;font-weight:700;margin:12px 0 0;word-break:keep-all}
.sub{font-size:18px;color:var(--muted);margin:12px 0 0}
.summary{font-size:18px;line-height:1.75;color:#3b3f48;margin:22px 0 0;word-break:keep-all}
.theme{border:1px solid #cfdcef;background:var(--primary-soft);border-radius:16px;padding:20px}
.theme .lbl{font-size:14px;font-weight:600;color:var(--primary);margin:0}
.theme .t{font-size:18px;font-weight:700;margin:8px 0 0;line-height:1.5;word-break:keep-all}
.theme ul{margin:14px 0 0;padding:0;list-style:none;font-size:14px;color:#454a55}
.theme li{margin:8px 0;padding-left:18px;position:relative}
.theme li::before{content:"";position:absolute;left:2px;top:7px;width:7px;height:7px;border-radius:50%;background:var(--primary)}
.photo{position:relative;background:var(--soft);overflow:hidden}
.photo .credit{position:absolute;left:8px;bottom:8px;font-size:10px;line-height:1.3;color:#fff;background:rgba(0,0,0,.6);padding:3px 6px;border-radius:4px}
.photo.empty{display:flex;align-items:center;justify-content:center;color:#9aa1ad;font-size:13px;text-align:center;padding:12px;border:1.5px dashed #d6dae1}
.hero{margin-top:36px;border-radius:20px;aspect-ratio:16/8}
.hero .cap{position:absolute;inset:auto 0 0 0;padding:18px 22px;color:#fff;font-size:15px;background:linear-gradient(transparent,rgba(0,0,0,.55))}
.body{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:40px;margin-top:48px}
h2{font-size:26px;font-weight:700;margin:0}
.intro{font-size:16px;line-height:1.75;color:var(--muted);margin:10px 0 0}
.days{list-style:none;margin:28px 0 0;padding:0;display:grid;gap:28px}
.day{border:1px solid var(--line);border-radius:16px;overflow:hidden;background:#fff}
.day header{display:flex;gap:16px;align-items:flex-start;padding:20px;background:rgba(245,246,248,.6);border-bottom:1px solid var(--line)}
.badge{flex:none;width:56px;height:56px;border-radius:16px;background:var(--primary);color:#fff;display:flex;flex-direction:column;align-items:center;justify-content:center}
.badge small{font-size:10px;font-weight:700;letter-spacing:.08em;text-transform:uppercase}
.badge b{font-size:24px;line-height:1}
.day h3{font-size:22px;font-weight:700;margin:0;word-break:keep-all}
.day header p{font-size:15px;line-height:1.6;color:var(--muted);margin:6px 0 0;word-break:keep-all}
.day .photo.wide{aspect-ratio:16/8}
.moments{list-style:none;margin:0;padding:0 22px}
.moment{display:grid;grid-template-columns:5.5rem minmax(0,1fr) 10.5rem;gap:16px;padding:20px 0;border-bottom:1px solid var(--line)}
.moment:last-child{border-bottom:0}
.moment.nophoto{grid-template-columns:5.5rem minmax(0,1fr)}
.time{font-size:14px;font-weight:600;color:var(--primary);margin:0;display:flex;gap:6px;align-items:center}
.time::before{content:"";width:12px;height:12px;border:2px solid currentColor;border-radius:50%}
.moment h4{font-size:16px;font-weight:700;margin:0;word-break:keep-all}
.moment p.txt{font-size:14px;line-height:1.7;color:var(--muted);margin:6px 0 0;word-break:keep-all}
.moment .photo{border-radius:12px;border:1px solid var(--line);aspect-ratio:4/3}
aside .box{border:1px solid var(--line);border-radius:16px;padding:20px;position:sticky;top:24px}
aside h2{font-size:18px}
dl{margin:14px 0 0;font-size:14px}
dl div{display:grid;grid-template-columns:4.5rem 1fr;gap:12px;padding:12px 0;border-top:1px solid var(--line)}
dl div:first-child{border-top:0}
dt{color:var(--muted)} dd{margin:0;font-weight:600;word-break:keep-all}
.tip{margin-top:16px;border-radius:16px;background:var(--soft);padding:20px}
.tip h2{font-size:16px} .tip p{font-size:14px;line-height:1.7;color:#454a55;margin:8px 0 0;word-break:keep-all}
mark.check{background:#fff0b8;color:#4a3b00;padding:0 3px;border-radius:3px;font-size:.92em}
.note{max-width:1040px;margin:0 auto;padding:0 20px 40px;font-size:12px;color:var(--muted)}
.note ul{margin:6px 0 0;padding-left:18px} .note a{color:inherit}
@media (max-width:860px){.head,.body{grid-template-columns:1fr}.moment,.moment.nophoto{grid-template-columns:1fr}
.moment .photo{max-width:none}aside .box{position:static}}
"""


def _text(value: Any) -> str:
    escaped = html.escape(str(value or ""))
    return CHECK_MARK.sub(lambda m: f'<mark class="check">{m.group(0)}</mark>', escaped)


def _photo_maker(photos: Mapping[str, Mapping[str, Any]], credit_name: str):
    def photo(photo_id: Any, css: str, edge: int = 1400, hint: str = "") -> str:
        asset = photos.get(str(photo_id or ""))
        if not asset:
            label = f"사진 추가: {html.escape(hint)}" if hint else "사진 추가"
            return f'<div class="photo empty {css}">{label}</div>'
        src = embed_image(str(asset.get("local_path") or asset.get("url")), edge)
        credit = str(asset.get("credit") or f"{credit_name} · 호텔 제공 이미지")
        return (
            f'<div class="photo {css}"><img src="{src}" alt="{html.escape(str(asset.get("evidence") or ""))}">'
            f'<span class="credit">사진: {html.escape(credit)}</span></div>'
        )
    return photo


def _used_photos(guide: Mapping[str, Any]) -> set[str]:
    return {str(guide.get("hero_photo") or "")} | {
        str(x.get("photo") or "") for d in guide.get("days") or [] if isinstance(d, dict)
        for x in [d, *[m for m in d.get("moments") or [] if isinstance(m, dict)]]
    }


def _commons_sources(photos: Mapping[str, Mapping[str, Any]], used: set[str]) -> str:
    """Free licences (CC BY, CC BY-SA) ask for the author, the licence and the source."""
    return "".join(
        f'<li><a href="{html.escape(str(a.get("page_url")))}">{html.escape(str(a.get("title") or ""))}</a> · '
        f'{html.escape(str(a.get("artist") or ""))} · '
        f'<a href="{html.escape(str(a.get("licence_url") or ""))}">{html.escape(str(a.get("licence") or ""))}</a></li>'
        for key, a in photos.items() if key in used and a.get("page_url")
    )


def render_guide_html(
    guide: Mapping[str, Any], *, photos: Mapping[str, Mapping[str, Any]], hotel: str,
    note: str = "",
) -> str:
    """One self-contained page in the site's itinerary shape; `photos` maps id -> asset."""
    credit_name = str(guide.get("hotel_en") or hotel)
    photo = _photo_maker(photos, credit_name)

    days = []
    for day in guide.get("days") or []:
        if not isinstance(day, dict):
            continue
        moments = []
        for moment in day.get("moments") or []:
            if not isinstance(moment, dict):
                continue
            has_photo = str(moment.get("photo") or "") in photos
            moments.append(
                f'<li class="moment{"" if has_photo else " nophoto"}"><p class="time">{_text(moment.get("time"))}</p>'
                f'<div><h4>{_text(moment.get("title"))}</h4><p class="txt">{_text(moment.get("text"))}</p></div>'
                + (photo(moment.get("photo"), "", 700) if has_photo else "") + "</li>"
            )
        days.append(
            f'<li class="day"><header><span class="badge"><small>Day</small><b>{int(day.get("day") or 0)}</b></span>'
            f'<div><h3>{_text(day.get("title"))}</h3><p>{_text(day.get("summary"))}</p></div></header>'
            + photo(day.get("photo"), "wide", 1200, str(day.get("title") or ""))
            + f'<ol class="moments">{"".join(moments)}</ol></li>'
        )
    info = guide.get("info") or {}
    highlights = "".join(f"<li>{_text(h)}</li>" for h in guide.get("highlights") or [])
    hero = photo(guide.get("hero_photo"), "hero", 1600, str(guide.get("theme") or ""))
    if "empty" not in hero:
        hero = hero.replace("</div>", f'<div class="cap">{_text(guide.get("theme"))}</div></div>', 1)
    title = str(guide.get("title") or "")
    sources = _commons_sources(photos, _used_photos(guide))
    return (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{CSS}</style>"
        f'<script type="application/ld+json">{json.dumps(guide_jsonld(guide), ensure_ascii=False)}</script>'
        '</head><body><main class="wrap"><article>'
        '<div class="head"><div>'
        '<p class="kicker">HOTEL-CENTERED JOURNEY</p>'
        f"<h1>{_text(title)}</h1>"
        f'<p class="sub">{_text(guide.get("hotel_en"))}{" · " + _text(guide.get("area_ko")) if guide.get("area_ko") else ""}</p>'
        f'<p class="summary">{_text(guide.get("summary"))}</p></div>'
        f'<div class="theme"><p class="lbl">일정 구성 기준</p><p class="t">{_text(guide.get("theme"))}</p>'
        f"<ul>{highlights}</ul></div></div>"
        f"{hero}"
        '<div class="body"><section><h2>일차별 추천 일정</h2>'
        f'<p class="intro">{_text(guide.get("intro"))}</p><ol class="days">{"".join(days)}</ol></section>'
        f'<aside><div class="box"><h2>{_text(guide.get("hotel_ko"))} 여행 정보</h2><dl>'
        f'<div><dt>위치</dt><dd>{_text(info.get("location"))}</dd></div>'
        f'<div><dt>기간</dt><dd>{_text(info.get("duration"))}</dd></div>'
        f'<div><dt>핵심</dt><dd>{_text(info.get("highlights"))}</dd></div></dl>'
        f'<div class="tip"><h2>이동 팁</h2><p>{_text(guide.get("travel_tip"))}</p></div></div></aside>'
        "</div></article></main>"
        + (f'<div class="note"><p>사진 출처 (Wikimedia Commons)</p><ul>{sources}</ul></div>' if sources else "")
        + (f'<p class="note">{html.escape(note)}</p>' if note else "")
        + "</body></html>"
    )


JOURNEY_CSS = """
h1 .l2{display:block}
.story{font-size:17px;line-height:1.85;color:#3b3f48;margin:22px 0 0;word-break:keep-all;white-space:pre-line}
.tips{margin:22px 0 0;border-left:3px solid var(--primary);padding:4px 0 4px 16px}
.tips h2{font-size:15px;color:var(--primary);margin:0}
.tips ul{margin:8px 0 0;padding-left:18px;font-size:15px;line-height:1.7;color:#454a55}
.moment p.hint{font-size:13px;line-height:1.6;color:#454a55;background:var(--soft);border-radius:8px;padding:6px 10px;margin:8px 0 0}
.moment .style{display:inline-block;font-size:12px;font-weight:700;color:var(--primary);background:var(--primary-soft);border-radius:999px;padding:2px 8px;margin-bottom:6px}
"""


def render_journey_html(
    guide: Mapping[str, Any], *, photos: Mapping[str, Mapping[str, Any]], hotel: str, note: str = "",
) -> str:
    """The hotel-journey page: a story intro, season tips, a criteria card and dense timed days."""
    credit_name = str(guide.get("hotel_en") or hotel)
    photo = _photo_maker(photos, credit_name)
    days = []
    for day in guide.get("days") or []:
        if not isinstance(day, dict):
            continue
        moments = []
        for moment in day.get("moments") or []:
            if not isinstance(moment, dict):
                continue
            has_photo = str(moment.get("photo") or "") in photos
            style = str(moment.get("style") or "").strip()
            tip = str(moment.get("tip") or "").strip()
            moments.append(
                f'<li class="moment{"" if has_photo else " nophoto"}"><p class="time">{_text(moment.get("time"))}</p><div>'
                + (f'<span class="style">{_text(style)}</span>' if style else "")
                + f'<h4>{_text(moment.get("title"))}</h4><p class="txt">{_text(moment.get("text"))}</p>'
                + (f'<p class="hint">팁: {_text(tip.removeprefix("팁:").removeprefix("팁 :").strip())}</p>' if tip else "")
                + "</div>" + (photo(moment.get("photo"), "", 700) if has_photo else "") + "</li>"
            )
        days.append(
            f'<li class="day"><header><span class="badge"><small>Day</small><b>{int(day.get("day") or 0)}</b></span>'
            f'<div><h3>{_text(day.get("title"))}</h3><p>{_text(day.get("summary"))}</p></div></header>'
            + photo(day.get("photo"), "wide", 1200, str(day.get("title") or ""))
            + f'<ol class="moments">{"".join(moments)}</ol></li>'
        )
    info = guide.get("info") or {}
    title = str(guide.get("title") or "")
    head, _, tail = title.partition("에서 시작하는")
    heading = (f"{_text(head)}에서 시작하는<span class=\"l2\">{_text(tail.strip())}</span>" if tail else _text(title))
    tips = "".join(f"<li>{_text(t)}</li>" for t in guide.get("season_tips") or [] if str(t).strip())
    tips_title = (f"{_text(guide.get('hotel_ko'))}이 전하는 시즌별 팁" if guide.get("season_tips_from_hotel")
                  else "시즌별 팁")
    highlights = "".join(f"<li>{_text(h)}</li>" for h in guide.get("highlights") or [])
    hero = photo(guide.get("hero_photo"), "hero", 1600, str(guide.get("criteria_line") or ""))
    if "empty" not in hero:
        hero = hero.replace("</div>", f'<div class="cap">{_text(guide.get("criteria_line"))}</div></div>', 1)
    sources = _commons_sources(photos, _used_photos(guide))
    return (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{CSS}{JOURNEY_CSS}</style>"
        f'<script type="application/ld+json">{json.dumps(guide_jsonld(guide), ensure_ascii=False)}</script>'
        '</head><body><main class="wrap"><article>'
        '<div class="head"><div>'
        '<p class="kicker">HOTEL-CENTERED JOURNEY</p>'
        f"<h1>{heading}</h1>"
        f'<p class="sub">{_text(guide.get("hotel_en"))}{" · " + _text(guide.get("city_ko")) if guide.get("city_ko") else ""}</p>'
        f'<p class="story">{_text(guide.get("intro"))}</p>'
        + (f'<div class="tips"><h2>{tips_title}</h2><ul>{tips}</ul></div>' if tips else "")
        + '</div>'
        f'<div class="theme"><p class="lbl">일정 구성 기준</p><p class="t">{_text(guide.get("criteria_line"))}</p>'
        f"<ul>{highlights}</ul></div></div>"
        f"{hero}"
        '<div class="body"><section><h2>일차별 추천 일정</h2>'
        f'<p class="intro">{_text(guide.get("days_intro"))}</p><ol class="days">{"".join(days)}</ol></section>'
        f'<aside><div class="box"><h2>{_text(guide.get("hotel_ko"))} 여행 정보</h2><dl>'
        f'<div><dt>위치</dt><dd>{_text(info.get("location"))}</dd></div>'
        f'<div><dt>기간</dt><dd>{_text(info.get("duration"))}</dd></div>'
        f'<div><dt>핵심</dt><dd>{_text(info.get("highlights"))}</dd></div></dl>'
        f'<div class="tip"><h2>이동 팁</h2><p>{_text(guide.get("move_tip"))}</p></div></div></aside>'
        "</div></article></main>"
        + (f'<div class="note"><p>사진 출처 (Wikimedia Commons)</p><ul>{sources}</ul></div>' if sources else "")
        + (f'<p class="note">{html.escape(note)}</p>' if note else "")
        + "</body></html>"
    )


# The hotel is shown only with the photos it sent: a moment held in the hotel (its rooms,
# restaurant, bar, spa, courtyard) never gets a web photo, and a web photo of an outside
# sight that shows the hotel is not used. These words name a kind of space, not a place.
_SPACE_WORDS = re.compile(
    r"\b(?:restaurant|ristorante|bar|lounge|lobby|suite|room|rooms|spa|pool|gym|terrace|rooftop|courtyard|"
    r"garden|deck|cafe|café|bistro|jpe?g|png|tiff?|webp|photo|image|img|dsc|exterior|interior|at|dusk|night|day|the)\b",
    re.I,
)


def _plain_name(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", str(text or "").lower())).strip()


def hotel_terms(hotel: str, *, names: list[str] = (), photo_files: list[str] = ()) -> list[str]:
    """Names that mean "the hotel": its own name and its outlets (from stop notes and photo files)."""
    terms: list[str] = []
    for raw in [hotel, *names]:
        name = _plain_name(raw)
        if len(name) >= 4 and name not in terms:
            terms.append(name)
    for file in photo_files:
        stem = _plain_name(_SPACE_WORDS.sub(" ", re.sub(r"\.\w+$", "", str(file))))
        if len(stem) >= 4 and stem not in terms:
            terms.append(stem)
    return terms


def is_hotel_place(place: str, terms: list[str]) -> bool:
    """A moment "place" that is the hotel or one of its outlets ("Grill Mar, Lisbon" for the hotel's restaurant)."""
    core = _plain_name(str(place).split(",")[0])
    if len(core) < 3:
        return False
    # The hotel's name inside the place, never the other way round: "Lisbon" is in "Hotel Lisbon".
    return any(re.search(rf"\b{re.escape(term)}\b", core) for term in terms)


def strip_hotel_places(guide: dict[str, Any], terms: list[str]) -> list[str]:
    """Clear the "place" of moments held in the hotel, so they are shown only with the hotel's photos."""
    cleared: list[str] = []
    for day in guide.get("days") or []:
        for moment in (day.get("moments") or []) if isinstance(day, dict) else []:
            if isinstance(moment, dict) and moment.get("place") and is_hotel_place(moment["place"], terms):
                cleared.append(str(moment["place"]))
                moment["place"] = ""
    return cleared
