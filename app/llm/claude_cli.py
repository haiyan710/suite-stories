"""Claude provider that runs the locally installed Claude Code CLI (`claude -p`).

Each call is one non-interactive `claude -p` run under the Claude account the
machine is logged in with, so no API key is involved. The run is stripped down
to what a completion needs: no MCP servers, skills, settings, or session
history, and no tools except WebSearch for search calls. ANTHROPIC_API_KEY /
ANTHROPIC_AUTH_TOKEN are removed from the child environment so the CLI never
silently switches to API billing.

Search grounding comes from the WebSearch tool_result blocks in the
stream-json output ("Links: [{title, url}, ...]"), not from URLs the model
writes in its answer.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Sequence

from .base import LLMError, LLMMessage, LLMResponse, SearchResult

logger = logging.getLogger(__name__)

_LINKS_RE = re.compile(r"Links:\s*(\[.*?\])\s*(?:\n|$)", re.DOTALL)
_STRIPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")


class ClaudeCliProvider:
    """LLMProvider backed by `claude -p` subprocess calls."""

    def __init__(
        self,
        *,
        claude_path: str = "claude",
        model: str = "",
        timeout_seconds: float = 600.0,
        workdir: Path | None = None,
    ) -> None:
        self._claude_path = claude_path
        self._model = model
        self._timeout = timeout_seconds
        # A neutral cwd keeps the project's CLAUDE.md and git context out of every call.
        self._workdir = workdir or Path(tempfile.gettempdir()) / "suite-stories-claude-cli"
        self._workdir.mkdir(parents=True, exist_ok=True)

    def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        system: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        thinking: bool = False,
    ) -> LLMResponse:
        # max_tokens / temperature / thinking have no CLI equivalent; the CLI picks them.
        return self._run(messages, system=system, tools="")

    def complete_with_search(
        self,
        messages: Sequence[LLMMessage],
        *,
        system: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.4,
        max_searches: int = 4,
        thinking: bool = False,
    ) -> LLMResponse:
        return self._run(messages, system=system, tools="WebSearch")

    def transcribe_pdf(self, path: Path, *, page_count: int) -> str:
        """Have Claude read an image-heavy PDF page by page and transcribe it.

        pypdf gets nothing from slide decks exported as images; Claude's Read
        tool renders the pages, so it can recover the text and what the
        visuals show.
        """
        ranges = [f"{start}-{min(start + 19, page_count)}" for start in range(1, page_count + 1, 20)]
        prompt = (
            f"Read the PDF at {path} with the Read tool, in page ranges {', '.join(ranges)} "
            "(at most 20 pages per Read call). For every page, write '[page N]' and then transcribe "
            "all visible text verbatim, followed by one line starting 'Visual:' describing what the "
            "images show (spaces, rooms, views, food, design details). Do not summarize across pages, "
            "do not add facts that are not on the page, and output only the transcription."
        )
        response = self._run(
            [LLMMessage(role="user", content=prompt)],
            system="You transcribe documents faithfully.",
            tools="Read", extra_args=["--add-dir", str(path.parent)],
        )
        return response.text

    def describe_images(self, paths: Sequence[Path]) -> list[str]:
        """One factual line per image (what is visibly in it), in input order."""
        if not paths:
            return []
        listing = "\n".join(f"{i}. {p}" for i, p in enumerate(paths, 1))
        prompt = (
            "Open each image below with the Read tool and describe what is visibly in it in one "
            "factual sentence of at most 25 words: the space or subject, notable objects, light and "
            "colour. No guesses about names or locations. Answer ONLY a JSON array of strings, one per "
            f"image, in the same order.\n\n{listing}"
        )
        response = self._run(
            [LLMMessage(role="user", content=prompt)], system="You describe photos accurately.",
            tools="Read", extra_args=["--add-dir", str(Path(paths[0]).parent)],
        )
        match = re.search(r"\[.*\]", response.text, re.S)
        try:
            lines = json.loads(match.group(0)) if match else []
        except json.JSONDecodeError:
            lines = []
        lines = [str(line) for line in lines] if isinstance(lines, list) else []
        return (lines + [""] * len(paths))[: len(paths)]

    # ------------------------------------------------------------------

    def _command(self, *, system: str | None, tools: str) -> list[str]:
        cmd = [
            self._claude_path, "-p",
            "--output-format", "stream-json", "--verbose",
            "--no-session-persistence",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--disable-slash-commands",
            "--setting-sources", "",
            "--tools", tools,
            "--system-prompt", system or "You are a careful assistant. Follow the user's output format exactly.",
        ]
        if tools:
            cmd += ["--allowedTools", tools]
        if self._model:
            cmd += ["--model", self._model]
        return cmd

    def _run(
        self, messages: Sequence[LLMMessage], *, system: str | None, tools: str,
        extra_args: Sequence[str] = (),
    ) -> LLMResponse:
        env = {k: v for k, v in os.environ.items() if k not in _STRIPPED_ENV}
        try:
            proc = subprocess.run(
                self._command(system=system, tools=tools) + list(extra_args),
                input=render_messages(messages), capture_output=True, text=True,
                timeout=self._timeout, cwd=self._workdir, env=env,
            )
        except FileNotFoundError as exc:
            raise LLMError(f"claude CLI not found at {self._claude_path!r}") from exc
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"claude CLI timed out after {self._timeout:.0f}s") from exc
        response = parse_stream(proc.stdout)
        if response is None:
            detail = (proc.stderr or proc.stdout or "").strip()[:300]
            raise LLMError(f"claude CLI failed (exit {proc.returncode}): {detail}")
        return response


def render_messages(messages: Sequence[LLMMessage]) -> str:
    """Flatten a message list into one prompt; the CLI takes a single user turn."""
    if len(messages) == 1 and messages[0].role == "user":
        return messages[0].content
    return "\n\n".join(f"[{m.role.upper()}]\n{m.content}" for m in messages)


def parse_stream(stdout: str) -> LLMResponse | None:
    """Turn `claude -p --output-format stream-json` output into an LLMResponse.

    Returns None when no result event is present. Raises LLMError when the
    run ended in an error (usage limit, auth, etc.).
    """
    results: list[SearchResult] = []
    seen: set[str] = set()
    final: dict[str, Any] | None = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            final = event
            continue
        message = event.get("message")
        if event.get("type") != "user" or not isinstance(message, dict):
            continue
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                for result in _search_links(block.get("content")):
                    if result.url not in seen:
                        seen.add(result.url)
                        results.append(result)
    if final is None:
        return None
    if final.get("is_error") or final.get("subtype") != "success":
        raise LLMError(f"claude CLI run failed: {str(final.get('result') or final.get('subtype'))[:300]}")
    usage = final.get("usage") or {}
    return LLMResponse(
        text=str(final.get("result") or "").strip(),
        model=next(iter(final.get("modelUsage") or {}), ""),
        input_tokens=int(usage.get("input_tokens", 0)) + int(usage.get("cache_read_input_tokens", 0))
        + int(usage.get("cache_creation_input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
        stop_reason=str(final.get("stop_reason") or ""),
        search_results=tuple(results),
    )


def _search_links(content: Any) -> list[SearchResult]:
    if isinstance(content, list):
        text = "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    else:
        text = str(content or "")
    out: list[SearchResult] = []
    for match in _LINKS_RE.finditer(text):
        try:
            links = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        for link in links if isinstance(links, list) else []:
            url = str(link.get("url", "")).strip() if isinstance(link, dict) else ""
            if url.startswith(("http://", "https://")):
                out.append(SearchResult(url=url, title=str(link.get("title", ""))[:200]))
    return out
