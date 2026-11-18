"""Golden evaluation of the Korean guide side flow (offline: hand-built stored guides)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from app.side_flows.guide_eval import evaluate_guide

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "golden_guide.json").read_text(encoding="utf-8"))


def _stored() -> dict:
    days = []
    for n in range(1, 4):
        days.append({
            "day": n, "title": f"{n}일차", "summary": "호텔에서 시작합니다.", "photo": f"p{n}",
            "moments": [
                {"time": "오전", "title": "조식", "text": "호텔에서 조식을 즐깁니다.", "photo": ""},
                {"time": "오후", "title": "시립 공원 산책", "text": "공원을 걷습니다.", "place": "City Park",
                 "photo": "w1" if n == 2 else ""},
                {"time": "저녁", "title": "레스토랑 저녁", "text": "호텔 레스토랑에서 저녁을 즐깁니다.", "photo": ""},
            ],
        })
    return {
        "nights": 2,
        "guide": {
            "hotel_ko": "호텔", "title": "호텔에서 시작하는 2박 3일 도시 여행", "summary": "호텔 중심의 여유로운 일정입니다.",
            "theme": "도시를 즐기는 2박 3일", "travel_tip": "택시가 편합니다. 2월은 춥습니다.",
            "hero_photo": "p9", "days": days,
        },
        "season": {
            "month": "February", "season": "winter", "dates": ["2027-02-06", "2027-02-07", "2027-02-08"],
            "daylight": [{"day": i, "sunset": "17:31"} for i in range(1, 4)],
            "typical": {"high_c": 13, "low_c": 4},
        },
        "photo_credits": {"w1": {"place": "City Park", "taken": "2019-02-11", "licence": "CC BY-SA 4.0",
                                 "page_url": "https://commons.wikimedia.org/wiki/File:Park.jpg"}},
    }


def _failures(stored: dict) -> list[str]:
    return evaluate_guide(stored, FIXTURE).to_dict()["failures"]


def test_a_good_guide_passes_every_check():
    result = evaluate_guide(_stored(), FIXTURE)
    assert result.passed and result.score == result.total, result.to_dict()


def test_a_repeated_photo_fails():
    stored = _stored()
    stored["guide"]["days"][2]["photo"] = "p1"
    assert "photos_once" in _failures(stored)
    assert not evaluate_guide(stored, FIXTURE).passed


def test_a_destination_photo_from_another_season_fails():
    stored = _stored()
    stored["photo_credits"]["w1"]["taken"] = "16 April 2015"
    assert "season_photos" in _failures(stored)


def test_a_park_after_an_early_sunset_and_a_winter_rooftop_fail():
    stored = _stored()
    evening = stored["guide"]["days"][0]["moments"][2]
    evening.update(title="정원 산책", text="정원을 걷습니다.")
    assert "daylight" in _failures(stored)
    stored = _stored()
    stored["guide"]["days"][1]["moments"][2].update(title="루프탑 디너", text="루프탑에서 저녁을 즐깁니다.")
    assert "daylight" in _failures(stored)


def test_shape_credits_and_title_are_checked():
    stored = _stored()
    broken = copy.deepcopy(stored)
    broken["guide"]["days"].pop()
    assert "shape" in _failures(broken)
    uncredited = copy.deepcopy(stored)
    uncredited["photo_credits"]["w1"]["licence"] = ""
    assert "credits" in _failures(uncredited)
    untitled = copy.deepcopy(stored)
    untitled["guide"]["title"] = "호텔 여행"
    assert "title_length" in _failures(untitled)


def test_the_fixture_holds_no_expected_answers():
    assert set(FIXTURE) >= {"required", "min_score", "max_check_items"}
    assert not {"expected", "answer", "title", "places"} & set(FIXTURE)
