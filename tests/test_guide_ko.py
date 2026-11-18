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


def test_outdoor_photos_never_fill_a_gap():
    from app.artifacts.guide_ko import fill_empty_days, is_outdoor
    assert is_outdoor({"evidence": "Rooftop terrace with set tables"})
    assert is_outdoor({"evidence": "Pink-walled courtyard with lanterns"})
    assert not is_outdoor({"evidence": "Warmly lit restaurant dining room"})
    g = guide(hero_photo="p1")
    fill_empty_days(g, ["p1", "p2", "p3", "p4"], outdoor={"p2", "p4"})
    assert [d["photo"] for d in g["days"]] == ["p1", "p3", "", ""]  # Day 1 keeps its own photo


def test_closed_and_outdoor_photos_are_handled_in_the_flow(tmp_path):
    from PIL import Image
    o, db, pid, glm, _claude, critic = _draft_setup(tmp_path, [CritiqueResult(verdict="PASS", reasons=[])])
    research = dict(db.project_state(pid)["research"])
    assets = []
    for name, shows in (("a", "Corner of the hotel at dusk"), ("b", "Rooftop terrace with loungers"),
                        ("c", "Sky Deck plunge pool"), ("d", "Lounge with curved sofas")):
        path = tmp_path / f"{name}.jpg"
        Image.new("RGB", (1600, 900)).save(path)
        assets.append({"local_path": str(path), "rights": "hotel_supplied", "evidence": shows, "width": 1600, "height": 900})
    research["available_assets"] = assets
    db.update_project_state(pid, "research", research)
    concept = dict(db.project_state(pid)["concept"])
    concept["format"] = {"type": "itinerary", "nights": 3}
    concept["venues"] = {"hotel_spaces": [{"name": "Sky Deck", "open_on_trip": "no"}]}
    db.update_project_state(pid, "concept", concept)
    o._provider = writer = GuideProvider([guide(hero_photo="p1")])
    o._fast_provider = CheckProvider({"verdict": "PASS", "fixes": []})
    o.handle_message("hotel guide", user_id="U1", thread_key="C:1")
    sent = json.loads(writer.prompts[0][1])["photos"]
    assert [p["id"] for p in sent] == ["p1", "p2", "p4"]  # the closed Sky Deck is gone
    assert [p["outdoor"] for p in sent] == [False, True, False]
    days = db.project_state(pid)["concept"]["guide_ko"]["guide"]["days"]
    assert [d["photo"] for d in days] == ["p4", "", "", ""]  # only the indoor lounge fills a gap


def _commons(title="Fountain Square, Harbourtown.jpg", licence="CC BY-SA 4.0", width=4000, height=2600, **extra):
    from app.tools.commons import CommonsPhoto
    base = dict(title=f"File:{title}", page_url="https://commons.wikimedia.org/wiki/File:x", thumb_url="https://x/t.jpg",
                width=width, height=height, licence=licence, licence_url="https://creativecommons.org/licenses/by-sa/4.0",
                artist="A. Photographer", description="")
    base.update(extra)
    return CommonsPhoto(**base)


def test_only_free_landscape_photos_of_the_place_are_usable():
    from app.tools.commons import CommonsPhotos
    usable = CommonsPhotos.usable
    assert usable(_commons(), "Fountain Square, Harbourtown")
    assert usable(_commons(licence="CC0"), "Fountain Square, Harbourtown")
    assert not usable(_commons(licence="CC BY-NC 2.0"), "Fountain Square, Harbourtown")
    assert not usable(_commons(width=900, height=600), "Fountain Square, Harbourtown")
    assert not usable(_commons(width=2600, height=4000), "Fountain Square, Harbourtown")  # portrait
    assert not usable(_commons(title="Fountain Square map.jpg"), "Fountain Square, Harbourtown")
    assert not usable(_commons(title="Old Bridge at night.jpg"), "Fountain Square, Harbourtown")
    assert usable(_commons(title="IMG_2041.jpg", description="The fountain square at noon"), "Fountain Square, Harbourtown")


def test_destination_photos_go_on_their_moment_or_an_empty_day_card():
    from app.artifacts.guide_ko import apply_place_photos, guide_places
    g = guide(hero_photo="p1")
    g["days"][1]["moments"][0].update(place="Fountain Square, Harbourtown")
    g["days"][2]["photo"] = "p2"
    g["days"][2]["moments"][0].update(place="Hill Museum, Harbourtown")
    g["days"][3]["moments"][1].update(place="Fountain Square, Harbourtown")  # same sight again
    assert guide_places(g) == ["Fountain Square, Harbourtown", "Hill Museum, Harbourtown"]
    apply_place_photos(g, {"Fountain Square, Harbourtown": "w1", "Hill Museum, Harbourtown": "w2"})
    assert g["days"][1]["photo"] == "w1"                               # empty card shows the day's sight
    assert g["days"][2]["photo"] == "p2" and g["days"][2]["moments"][0]["photo"] == "w2"  # hotel photo kept
    assert g["days"][3]["photo"] == "" and g["days"][3]["moments"][1]["photo"] == ""      # once per page


class FakeCommons:
    def __init__(self, image_path):
        self.image_path, self.searched = image_path, []

    def candidates(self, place, months=frozenset(), month_names=()):
        self.searched.append(place)
        return [_commons(title="Other city fountain.jpg"), _commons()]

    def download(self, photo):
        from app.tools.commons import CommonsPhoto
        return CommonsPhoto(**{**photo.__dict__, "local_path": str(self.image_path)})


class PickProvider(CheckProvider):
    def complete(self, messages, *, system=None, **kwargs):
        if system and "destination photos" in system:
            places = json.loads(messages[0].content)["places"]
            return LLMResponse(text=json.dumps({"picks": [{"place": p["place"], "pick": 1} for p in places]}))
        return super().complete(messages, system=system, **kwargs)


def test_the_flow_adds_credited_destination_photos(tmp_path):
    from PIL import Image
    o, db, pid, *_ = _draft_setup(tmp_path, [CritiqueResult(verdict="PASS", reasons=[])])
    image = tmp_path / "sight.jpg"
    Image.new("RGB", (1600, 1000)).save(image)
    concept = dict(db.project_state(pid)["concept"])
    concept["format"] = {"type": "itinerary", "nights": 3}
    db.update_project_state(pid, "concept", concept)
    written = guide(hero_photo="")
    written["days"][1]["moments"][0].update(title="분수 광장", place="Fountain Square, Harbourtown")
    o._provider = GuideProvider([written])
    o._fast_provider = PickProvider({"verdict": "PASS", "fixes": []})
    o._place_photos = commons = FakeCommons(image)
    outcome = o.handle_message("hotel guide", user_id="U1", thread_key="C:1")
    assert commons.searched == ["Fountain Square, Harbourtown"]
    saved = db.project_state(pid)["concept"]["guide_ko"]
    assert saved["guide"]["days"][1]["photo"] == "w1"
    assert saved["photo_credits"]["w1"]["licence"] == "CC BY-SA 4.0"
    page = Path(saved["html"]).read_text()
    assert "사진: A. Photographer · CC BY-SA 4.0 · Wikimedia Commons" in page and "사진 출처" in page
    assert "1 destination photos from Wikimedia Commons" in outcome.markdown


def test_the_season_comes_from_past_years_on_the_same_dates(monkeypatch):
    from app.tools.season import SeasonClient, month_names, season_months, season_name
    calls = []

    def daily(self, lat, lon, start, end):
        calls.append((start, end))
        return {"sunrise": ["2026-02-06T07:17", "2026-02-07T07:16"], "sunset": ["2026-02-06T17:31", "2026-02-07T17:32"],
                "temperature_2m_max": [16, 12], "temperature_2m_min": [8, 6], "precipitation_sum": [5.0, 0.0]}

    monkeypatch.setattr(SeasonClient, "_daily", daily)
    season = SeasonClient(years=2).trip_season(["2030-02-06", "2030-02-07"], lat=41.9, lon=12.5)
    assert season["month"] == "February" and season["season"] == "winter"
    assert [c[0][:4] for c in calls] != ["2030", "2030"] and len(calls) == 2
    assert season["daylight"][0] == {"day": 1, "date": "2030-02-06", "sunrise": "07:17", "sunset": "17:31"}
    assert season["typical"]["high_c"] == 14 and season["typical"]["low_c"] == 7
    assert season_name(2, -33.9) == "summer" and season_months(["2027-02-06"]) == {12, 1, 2}
    assert season_months(["2027-02-27", "2027-03-02"]) == {12, 1, 2, 3, 4, 5}
    assert month_names(["2027-02-27", "2027-03-02"]) == ["February", "March"]
    assert SeasonClient().trip_season(["2027-02-06"], lat=None, lon=None) == {
        "month": "February", "season": "winter", "dates": ["2027-02-06"]}


def test_commons_photos_from_another_season_are_never_offered(monkeypatch):
    from app.tools.commons import CommonsPhotos
    found = [_commons(title="Fountain Square spring.jpg", taken="2009-04-24"),
             _commons(title="Fountain Square undated.jpg"),
             _commons(title="Fountain Square march.jpg", taken="2022-03-25"),
             _commons(title="Fountain Square winter.jpg", taken="2021-02-03")]
    queries = []
    monkeypatch.setattr(CommonsPhotos, "_search", lambda self, query, limit=30: queries.append(query) or found)
    titles = [p.title for p in CommonsPhotos(Path("/tmp")).candidates(
        "Fountain Square, Harbourtown", months={12, 1, 2}, month_names=["February"])]
    assert titles == ["File:Fountain Square winter.jpg", "File:Fountain Square undated.jpg"]
    assert queries[0] == '"Fountain Square" February'


class FakeSeason:
    def trip_season(self, dates, *, lat, lon):
        return {"month": "February", "season": "winter", "dates": dates,
                "daylight": [{"day": 1, "date": dates[0], "sunrise": "07:17", "sunset": "17:31"}],
                "typical": {"high_c": 14, "low_c": 7}}


def test_the_writer_and_the_check_get_the_season(tmp_path):
    o, db, pid, *_ = _draft_setup(tmp_path, [CritiqueResult(verdict="PASS", reasons=[])])
    concept = dict(db.project_state(pid)["concept"])
    concept["format"] = {"type": "itinerary", "nights": 3, "calendar": {"days": [
        {"day": i, "date": f"2027-02-0{5 + i}"} for i in range(1, 5)]}}
    db.update_project_state(pid, "concept", concept)
    o._provider = writer = GuideProvider([guide()])
    o._fast_provider = check = PickProvider({"verdict": "PASS", "fixes": []})
    seen = []
    check.complete = lambda messages, system=None, **kw: (seen.append(messages[0].content),
                                                          LLMResponse(text='{"verdict":"PASS","fixes":[]}'))[1]
    o._season, o._geo = FakeSeason(), None
    outcome = o.handle_message("hotel guide", user_id="U1", thread_key="C:1")
    assert json.loads(writer.prompts[0][1])["season"]["daylight"][0]["sunset"] == "17:31"
    assert json.loads(seen[0])["season"]["month"] == "February"
    assert "Timed for February (winter): sunset about 17:31–17:31, typically 7–14°C" in outcome.markdown


def test_photo_dates_written_in_words_are_read():
    from app.tools.commons import _month
    assert [_month(t) for t in ("2010-02-03", "2010:02:03 10:00", "16 August 2013", "Sept 2011", "Marching band", "")] \
        == [2, 2, 8, 9, 0, 0]


class LookProvider(CheckProvider):
    """A Claude-like fast model that can open images; rejects the first place's photos."""

    def __init__(self):
        super().__init__({"verdict": "PASS", "fixes": []})
        self.looked = []

    def look_at_images(self, paths, *, system, task):
        self.looked.append((list(paths), json.loads(task)))
        places = json.loads(task)["places"]
        return json.dumps({"picks": [{"place": places[0]["place"], "pick": -1, "why": "blossom"},
                                     *[{"place": p["place"], "pick": 1, "why": "bare trees"} for p in places[1:]]]})


def test_destination_photos_are_judged_by_looking_at_them(tmp_path):
    from PIL import Image
    o, db, pid, *_ = _draft_setup(tmp_path, [CritiqueResult(verdict="PASS", reasons=[])])
    image = tmp_path / "sight.jpg"
    Image.new("RGB", (1600, 1000)).save(image)
    concept = dict(db.project_state(pid)["concept"])
    concept["format"] = {"type": "itinerary", "nights": 3}
    db.update_project_state(pid, "concept", concept)
    written = guide(hero_photo="")
    written["days"][1]["moments"][0].update(place="Fountain Square, Harbourtown")
    written["days"][2]["moments"][0].update(place="Hill Museum, Harbourtown")
    o._provider = GuideProvider([written])
    o._fast_provider = look = LookProvider()
    o._place_photos = FakeCommons(image)
    o._season = FakeSeason()
    o.handle_message("hotel guide", user_id="U1", thread_key="C:1")
    paths, task = look.looked[0]
    assert len(paths) == 4 and task["places"][0]["photos"][1]["path"] == str(image)
    saved = db.project_state(pid)["concept"]["guide_ko"]
    assert list(saved["photo_credits"].values())[0]["place"] == "Hill Museum, Harbourtown"  # the blossom one is dropped
    assert saved["guide"]["days"][1]["photo"] == ""


class CheckAndVerify:
    def __init__(self):
        self.verified = None

    def complete(self, messages, *, system=None, **kwargs):
        if system and system.startswith("# Check: Korean hotel guide"):
            return LLMResponse(text=json.dumps({"verdict": "FIX", "fixes": [
                "Day 2: remove the restaurant (unsupported).", "Day 3: shorten the summary."]}))
        if system and "Verify suggested fixes" in system:
            self.verified = json.loads(messages[0].content)["fixes"]
            return LLMResponse(text=json.dumps({"verdicts": [
                {"i": 0, "status": "refuted", "reason": "the evidence names it"}]}))
        return LLMResponse(text="{}")


def test_the_guide_checks_its_fixes_from_the_other_side(tmp_path):
    from dataclasses import replace
    o, db, pid, *_ = _draft_setup(tmp_path, [CritiqueResult(verdict="PASS", reasons=[])])
    concept = dict(db.project_state(pid)["concept"])
    concept["format"] = {"type": "itinerary", "nights": 3}
    db.update_project_state(pid, "concept", concept)
    o._settings = replace(o._settings, verify_fixes=True)
    o._provider = writer = GuideProvider([guide(), guide()])
    o._fast_provider = fast = CheckAndVerify()
    outcome = o.handle_message("hotel guide", user_id="U1", thread_key="C:1")
    assert [f["fix"] for f in fast.verified] == ["Day 2: remove the restaurant (unsupported).", "Day 3: shorten the summary."]
    assert json.loads(writer.prompts[1][1])["fixes"] == ["Day 3: shorten the summary."]
    saved = db.project_state(pid)["concept"]["guide_ko"]
    assert saved["refuted_fixes"][0]["reason"] == "the evidence names it"
    assert "1 suggested fix dropped after a second check" in outcome.markdown
