

def test_compact_research_keeps_facts_for_places_the_focus_text_names():
    import json as _json

    from app.orchestration.planning import compact_research_json as compact

    filler = [{"fact": f"Bangkok fact number {i} about the city and its many districts.", "source_ids": ["S1"]}
              for i in range(300)]
    research = {"verified_facts": [{"fact": "Nusara holds one Michelin star.", "source_ids": ["S9"]}] + filler}
    plain = compact(research, budget_chars=3000)
    focused = compact(research, budget_chars=3000, focus_text="Day 2: dinner at Nusara in Bangkok.")
    assert "Nusara" not in plain
    assert "Nusara" in focused
    assert _json.loads(focused)["verified_facts"][0]["fact"].startswith("Nusara")
