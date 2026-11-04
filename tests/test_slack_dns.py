"""Process-local Slack DNS fallback tests."""

from app import slack_dns


def test_doh_parser_keeps_only_a_records(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"Answer": [
                {"type": 5, "data": "alias.example"},
                {"type": 1, "data": "203.0.113.10"},
            ]}

    monkeypatch.setattr(slack_dns.httpx, "get", lambda *a, **k: Response())
    slack_dns._cache.clear()
    assert slack_dns._resolve_a_records("wss-primary.slack.com") == ["203.0.113.10"]


def test_public_hostname_filter_excludes_local_names_and_ip_literals():
    assert slack_dns._is_public_hostname("kobe.intercontinental.com") is True
    assert slack_dns._is_public_hostname("printer.local") is False
    assert slack_dns._is_public_hostname("database.internal") is False
    assert slack_dns._is_public_hostname("localhost") is False
    assert slack_dns._is_public_hostname("192.0.2.1") is False
