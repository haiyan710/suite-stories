"""Builder output parsing tests."""

from app.agents.builder import _extract_build_output


def test_builder_accepts_standalone_fenced_html():
    html, css, js, notes = _extract_build_output(
        "```html\n<!doctype html><html><head><style>body{margin:0}</style></head>"
        "<body><main>Story</main></body></html>\n```"
    )
    assert html.startswith("<!doctype html>")
    assert html.endswith("</html>")
    assert css == "" and js == ""
    assert notes


def test_builder_keeps_legacy_json_contract_compatible():
    html, css, js, notes = _extract_build_output(
        '{"html":"<!doctype html><html><body>x</body></html>",'
        '"css":"body{}","js":"","notes":["ok"]}'
    )
    assert "<body>x</body>" in html
    assert css == "body{}" and js == ""
    assert notes == ["ok"]


def test_builder_rejects_truncated_html():
    assert _extract_build_output("```html\n<!doctype html><html><body>") == ("", "", "", [])
