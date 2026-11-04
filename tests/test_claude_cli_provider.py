"""ClaudeCliProvider: stream parsing, environment isolation, config selection."""

import json
import subprocess
from pathlib import Path

import pytest

from app.config import load_settings
from app.llm import ClaudeCliProvider, LLMError, LLMMessage, ZaiGlmProvider, build_provider
from app.llm.claude_cli import parse_stream


def _stream(*events) -> str:
    return "\n".join(json.dumps(event) for event in events)


SEARCH_RESULT = {
    "type": "user",
    "message": {"content": [{
        "type": "tool_result", "tool_use_id": "t1",
        "content": 'Web search results for query: "W Rome"\n\nLinks: [{"title":"W Rome","url":"https://whotels.com/rome/"},'
                   '{"title":"Review","url":"https://www.cntraveler.com/hotels/rome/w-rome"}]\n\nSummary text',
    }]},
}
FINAL = {
    "type": "result", "subtype": "success", "is_error": False, "result": '[{"url":"https://whotels.com/rome/"}]',
    "stop_reason": "end_turn", "usage": {"input_tokens": 5, "cache_read_input_tokens": 100, "output_tokens": 40},
    "modelUsage": {"claude-opus-5-5": {}},
}


def test_parse_stream_extracts_text_and_grounded_links():
    response = parse_stream(_stream({"type": "system", "subtype": "init"}, SEARCH_RESULT, FINAL))
    assert response.text == '[{"url":"https://whotels.com/rome/"}]'
    assert [r.url for r in response.search_results] == [
        "https://whotels.com/rome/", "https://www.cntraveler.com/hotels/rome/w-rome",
    ]
    assert response.model == "claude-opus-5-5"
    assert response.input_tokens == 105


def test_parse_stream_raises_on_error_result():
    error = {"type": "result", "subtype": "success", "is_error": True, "result": "Claude usage limit reached"}
    with pytest.raises(LLMError, match="usage limit"):
        parse_stream(_stream(error))
    assert parse_stream("not json\n") is None


def test_cli_call_strips_api_credentials_and_disables_extras(monkeypatch, tmp_path: Path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"], captured["kwargs"] = cmd, kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout=_stream(SEARCH_RESULT, FINAL), stderr="")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    monkeypatch.setattr(subprocess, "run", fake_run)
    provider = ClaudeCliProvider(claude_path="/bin/claude", model="claude-sonnet-5-5", workdir=tmp_path)

    response = provider.complete_with_search([LLMMessage("user", "find W Rome")], system="SYS")

    cmd = captured["cmd"]
    assert "ANTHROPIC_API_KEY" not in captured["kwargs"]["env"]
    assert captured["kwargs"]["input"] == "find W Rome"
    assert cmd[cmd.index("--tools") + 1] == "WebSearch"
    assert cmd[cmd.index("--system-prompt") + 1] == "SYS"
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-5-5"
    assert "--strict-mcp-config" in cmd and "--no-session-persistence" in cmd
    assert response.search_results

    provider.complete([LLMMessage("user", "hi")])
    assert captured["cmd"][captured["cmd"].index("--tools") + 1] == ""


def test_provider_selection_and_zai_key_only_required_for_zai(tmp_path: Path):
    base = {"SLACK_BOT_TOKEN": "x", "SLACK_APP_TOKEN": "x"}
    missing_env = tmp_path / "none.env"
    cli = load_settings(missing_env, {**base, "LLM_PROVIDER": "claude_cli"})
    assert isinstance(build_provider(cli), ClaudeCliProvider)
    zai = load_settings(missing_env, {**base, "ZAI_API_KEY": "k"})
    assert isinstance(build_provider(zai), ZaiGlmProvider)
    with pytest.raises(RuntimeError, match="ZAI_API_KEY"):
        load_settings(missing_env, base)
    with pytest.raises(RuntimeError, match="LLM_PROVIDER"):
        load_settings(missing_env, {**base, "LLM_PROVIDER": "openai"})
