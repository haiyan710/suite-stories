"""The hotel-journey page: the side flow's denser format (story intro, season tips, 4-8 moments a day)."""

from __future__ import annotations

import json
from pathlib import Path

from app.artifacts.guide_ko import hotel_tip_quotes_ok, render_journey_html, validate_guide, validate_journey
from app.orchestration.handoffs import CritiqueResult
from app.side_flows import korean_guide
from app.side_flows.guide_eval import evaluate_guide
from test_guide_ko import CheckProvider, GuideProvider
from test_orchestrator import _draft_setup

INTRO = ("바르셀로나는 바다와 언덕 사이에 층층이 쌓인 도시예요. " * 25).strip()


def journey(days: int = 4, **extra):
    def moments(n, last):
        times = ["아침", "오전", "점심", "오후", "늦은 오후", "이른 저녁", "저녁", "밤"]
        out = [{"time": times[i], "title": f"순간 {i}", "text": "천천히 즐기세요.", "tip": "", "place": "",
                "photo": "", "style": ""} for i in range(n)]
        if last:
            out[-1].update(title="체크아웃", text="체크아웃은 [확인 필요: 체크아웃 시간]입니다. 짐 보관을 문의하세요.")
        return out
    base = {
        "hotel_ko": "호텔 마르", "hotel_en": "Hotel Mar", "city_ko": "바르셀로나", "area_ko": "에이샴플레",
        "title": "호텔 마르에서 시작하는 3박 4일 바르셀로나 여행", "intro": INTRO,
        "season_tips": ["겹겹이 입으세요.", "작은 우산을 챙기세요.", "점심은 늦게 먹어요."],
        "season_tips_from_hotel": False, "hotel_tip_quotes": [],
        "criteria_line": "2월 설 연휴를 바르셀로나에서 3박 4일", "highlights": ["사그라다 파밀리아", "고딕 지구", "보른", "람블라"],
        "hero_photo": "p1", "days_intro": "호텔에서 쉬며 다닙니다.",
        "days": [
            {"day": i, "title": f"{i}일차", "summary": "하루를 잇는 긴 문장으로 이어지는 하루.", "photo": "",
             "moments": moments(4 if 1 < i < days else 3, i == days)}
            for i in range(1, days + 1)
        ],
        "info": {"location": "에이샴플레", "duration": "3박 4일", "highlights": "사그라다 파밀리아 · 고딕 지구"},
        "move_tip": "택시가 편합니다. 해가 약 18시에 집니다.",
    }
    base["days"][0]["moments"][0].update(time="오후", title="체크인 및 짐 풀기", text="체크인은 [확인 필요: 체크인 시간]부터입니다.")
    base["days"][0]["moments"][1].update(time="저녁")
    base["days"][0]["moments"][2].update(time="밤")
    base.update(extra)
    return base


def test_a_well_formed_journey_passes_its_own_checks():
    assert validate_journey(journey(), nights=3) == []


def test_the_journey_shape_is_checked_in_python():
    short = journey(intro="짧아요.")
    assert any("intro" in issue for issue in validate_journey(short, nights=3))
    thin = journey()
    thin["days"][1]["moments"] = thin["days"][1]["moments"][:2]
    assert any("Day 2: give 4-8 moments" in issue for issue in validate_journey(thin, nights=3))
    unordered = journey()
    unordered["days"][1]["moments"][0]["time"] = "밤"
    assert any("time order" in issue for issue in validate_journey(unordered, nights=3))
    no_checkout = journey()
    no_checkout["days"][-1]["moments"][-1].update(title="공항", text="공항으로 갑니다.")
    assert any("check-out" in issue for issue in validate_journey(no_checkout, nights=3))
    lone_style = journey()
    lone_style["days"][3]["moments"][1]["style"] = "탐험가 스타일"
    assert any("style option" in issue for issue in validate_journey(lone_style, nights=3))


def test_platform_benefits_are_never_written_on_either_page():
    page = journey()
    page["days"][3]["moments"][-1]["text"] += " 투숙객에게 제공되는 혜택인 $100크레딧 사용을 잊지 마세요!"
    assert any("booking-platform benefit" in issue for issue in validate_journey(page, nights=3))
    guide = {"days": [], "summary": "2인 조식이 포함됩니다."}
    assert any("booking-platform benefit" in issue for issue in validate_guide(guide, nights=0))


def test_tips_are_credited_to_the_hotel_only_from_its_own_documents():
    facts = ["Winter in the city: bring layers and a small umbrella, February can be chilly and rainy."]
    assert hotel_tip_quotes_ok(["bring layers and a small umbrella"], facts)
    assert not hotel_tip_quotes_ok(["bring layers and a small umbrella"], [])
    assert not hotel_tip_quotes_ok(["pack sunscreen for the beach days"], facts)
    assert not hotel_tip_quotes_ok([], facts)


def test_the_journey_page_renders_story_tips_styles_and_hint():
    page = journey(season_tips_from_hotel=True)
    page["days"][3]["moments"][1].update(style="예술 애호가 스타일", tip="팁: 미리 예약하세요.")
    page["days"][3]["moments"][2].update(time="오전", style="쇼핑 애호가 스타일")
    html = render_journey_html(page, photos={}, hotel="Hotel Mar")
    assert "호텔 마르에서 시작하는<span class=\"l2\">3박 4일 바르셀로나 여행</span>" in html
    assert "호텔 마르이 전하는 시즌별 팁" in html and "일정 구성 기준" in html
    assert "예술 애호가 스타일" in html and "팁: 미리 예약하세요." in html and "팁: 팁:" not in html
    assert '<mark class="check">[확인 필요: 체크인 시간]</mark>' in html
    plain = render_journey_html(journey(), photos={}, hotel="Hotel Mar")
    assert "<h2>시즌별 팁</h2>" in plain


def test_a_journey_request_is_a_side_output_next_to_the_guide(tmp_path: Path):
    o, db, pid, *_ = _draft_setup(tmp_path, [CritiqueResult(verdict="PASS", reasons=[])])
    concept = dict(db.project_state(pid)["concept"])
    concept["format"] = {"type": "itinerary", "nights": 3}
    concept["guide_ko"] = {"guide": {"title": "earlier guide"}}
    db.update_project_state(pid, "concept", concept)
    researched = []
    o._journey_research = lambda project_id, state, research: researched.append(project_id) or research
    # The writer claims the hotel sent the tips; no hotel document says so.
    written = journey(season_tips_from_hotel=True, hotel_tip_quotes=["bring layers and a small umbrella"])
    o._provider = GuideProvider([written])
    o._fast_provider = CheckProvider({"verdict": "PASS", "fixes": []})
    outcome = o.handle_message("make the hotel journey page", user_id="U1", thread_key="C:1")
    assert researched == [pid]
    assert "guide_journey" in o._provider.prompts[0][0] or "journey" in o._provider.prompts[0][0].lower()
    payload = json.loads(o._provider.prompts[0][1])
    assert "hotel_document_facts" in payload
    state = db.project_state(pid)["concept"]
    saved = state["guide_journey"]
    assert saved["style"] == "journey" and saved["guide"]["season_tips_from_hotel"] is False
    assert state["guide_ko"] == {"guide": {"title": "earlier guide"}}  # the short guide is left alone
    assert "hotel-journey-ko.html" in outcome.attachments[0]
    assert "Season tips not credited to the hotel (the hotel sent no such tips)" in outcome.markdown
    assert "moments" in outcome.markdown
    assert evaluate_guide(saved).to_dict()["failures"] == [], evaluate_guide(saved).to_dict()


def test_journey_triggers():
    assert korean_guide.wants_journey("hotel journey please")
    assert korean_guide.wants_journey("호텔 저니 만들어 주세요")
    assert korean_guide.wants_journey("làm bản journey cho khách sạn này")
    assert not korean_guide.wants_journey("hotel guide")
