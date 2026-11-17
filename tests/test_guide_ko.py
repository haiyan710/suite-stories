"""Korean hotel guide: a side output that leaves the English flow untouched."""

from __future__ import annotations

import json
from pathlib import Path

from app.artifacts.guide_ko import guide_check_items, render_guide_html, validate_guide
from app.llm.base import LLMResponse
from app.orchestration.handoffs import CritiqueResult
from test_orchestrator import _draft_setup


def guide(days: int = 4, **extra):
    base = {
        "hotel_ko": "하버 그랜드 고베", "hotel_en": "Harbour Grand Kobe", "city_ko": "고베", "area_ko": "하버랜드",
        "title": "하버 그랜드 고베에서 시작하는 3박 4일 고베 여행",
        "summary": "하버랜드를 중심으로 바다 산책과 미식을 즐깁니다.",
        "theme": "바다와 미식을 즐기는 3박 4일", "highlights": ["하버랜드", "기타노"], "intro": "호텔에서 쉬며 다닙니다.",
        "hero_photo": "p1",
        "days": [
            {"day": i, "title": f"{i}일차", "summary": "하루 요약입니다.", "photo": "p1" if i == 1 else "",
             "moments": [{"time": "오후", "title": "체크인 · 객실 휴식", "text": "체크인하고 쉽니다.", "photo": ""},
                         {"time": "저녁", "title": "호텔 레스토랑", "text": "[확인 필요: 대표 메뉴] 저녁을 즐깁니다.", "photo": ""}]}
            for i in range(1, days + 1)
        ],
        "info": {"location": "하버랜드", "duration": "3박 4일", "highlights": "하버랜드 · 기타노"},
        "travel_tip": "이동 시간을 넉넉히 잡으세요.",
    }
    base.update(extra)
    return base


def test_the_shape_is_checked_in_python():
    assert validate_guide(guide(), nights=3) == []
    assert any("exactly 4 days" in i for i in validate_guide(guide(days=3), nights=3))
    bad = guide()
    bad["days"][0]["moments"] = list(reversed(bad["days"][0]["moments"]))
    assert any("time order" in i for i in validate_guide(bad, nights=3))
    assert len(guide_check_items(guide())) == 4


def test_the_page_follows_the_site_layout_without_branding(tmp_path: Path):
    from PIL import Image
    photo = tmp_path / "p.jpg"
    Image.new("RGB", (1600, 900), (90, 90, 90)).save(photo)
    page = render_guide_html(guide(), photos={"p1": {"local_path": str(photo), "evidence": "lobby"}}, hotel="Harbour Grand Kobe")
    for part in ("HOTEL-CENTERED JOURNEY", "일정 구성 기준", "일차별 추천 일정", "여행 정보", "이동 팁",
                 '"@type": "TouristTrip"', "사진: Harbour Grand Kobe · 호텔 제공 이미지", 'class="check"'):
        assert part in page, part
    assert page.count('class="day"') == 4 and "사진 추가" in page


class GuideProvider:
    def __init__(self, guides):
        self.guides, self.prompts = list(guides), []

    def complete(self, messages, *, system=None, **kwargs):
        self.prompts.append((system, messages[0].content))
        return LLMResponse(text=json.dumps(self.guides.pop(0), ensure_ascii=False))


class CheckProvider:
    def __init__(self, verdict):
        self.verdict, self.calls = verdict, 0

    def complete(self, messages, *, system=None, **kwargs):
        self.calls += 1
        return LLMResponse(text=json.dumps(self.verdict))


def test_the_guide_is_a_side_output_that_keeps_the_english_draft(tmp_path: Path):
    o, db, pid, glm, _claude, critic = _draft_setup(tmp_path, [CritiqueResult(verdict="PASS", reasons=[])])
    o.handle_message("Write a sample draft", user_id="U1", thread_key="C:1")
    before = db.project_state(pid)["concept"]
    o._provider = writer = GuideProvider([guide(days=3), guide()])
    o._fast_provider = check = CheckProvider({"verdict": "FIX", "fixes": ["Day 2: shorten the summary."]})
    concept = dict(before)
    concept["format"] = {"type": "itinerary", "nights": 3}
    db.update_project_state(pid, "concept", concept)

    outcome = o.handle_message("make the Korean hotel guide", user_id="U1", thread_key="C:1")

    assert len(outcome.attachments) == 2 and outcome.attachments[0].endswith("-hotel-guide-ko.html")
    assert all(Path(p).is_file() for p in outcome.attachments)
    first_payload = json.loads(writer.prompts[0][1])
    assert first_payload["draft"]["body"] and first_payload["format"]["nights"] == 3
    second_payload = json.loads(writer.prompts[1][1])
    assert "Day 2: shorten the summary." in second_payload["fixes"]
    assert any("exactly 4 days" in f for f in second_payload["fixes"])
    saved = db.project_state(pid)["concept"]
    assert saved["guide_ko"]["fixes_applied"] == 2 and saved["guide_ko"]["open_issues"] == []
    for key in ("sample_draft", "outline", "editorial", "status"):
        assert saved[key] == before[key], key
    assert "English draft and layouts in this thread are unchanged" in outcome.markdown
    assert check.calls == 1 and len(glm.tasks) == 1


def test_a_guide_request_without_a_concept_goes_the_normal_way(tmp_path: Path):
    from test_orchestrator import setup_orchestrator
    o, db, *_ = setup_orchestrator(tmp_path, ['{"intent":"GENERAL_CONVERSATION"}'])
    outcome = o.handle_message("what is a hotel guide?", user_id="U1", thread_key="C:none")
    assert not outcome.attachments


def test_a_photo_is_used_once_on_the_whole_page():
    from app.artifacts.guide_ko import dedupe_photos
    g = guide(hero_photo="p1")
    placements = [("p1", ["", "p3"]), ("p3", ["p3", ""]), ("p1", ["", ""]), ("p3", ["", "p3"])]
    for day, (photo, moments) in zip(g["days"], placements):
        day["photo"] = photo
        for moment, m in zip(day["moments"], moments):
            moment["photo"] = m
    dedupe_photos(g)
    shown = [g["hero_photo"]] + [d["photo"] for d in g["days"]] + [m["photo"] for d in g["days"] for m in d["moments"]]
    filled = [x for x in shown if x]
    assert filled == ["p1", "p3"] and len(filled) == len(set(filled))
    assert g["days"][0]["photo"] == "" and g["days"][1]["photo"] == "p3"


def test_unused_hotel_photos_fill_empty_day_cards_without_repeats():
    from app.artifacts.guide_ko import dedupe_photos, fill_empty_days
    g = guide(hero_photo="p1")
    g["days"][0]["photo"] = "p1"
    g["days"][1]["photo"] = "p3"
    fill_empty_days(dedupe_photos(g), ["p1", "p2", "p3", "p4"])
    assert [d["photo"] for d in g["days"]] == ["p2", "p3", "p4", ""]
