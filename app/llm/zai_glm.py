"""GLM provider using Z.ai's Anthropic-compatible Messages API.

This is the same documented transport the local ZCode install uses for
the "Z.ai Coding Plan" provider (see ~/.zcode runtime provider config:
api.type = anthropic-messages, baseUrl = https://api.z.ai/api/anthropic,
builtin models GLM-5.3 / GLM-5.3-Flash). It also supports the
Anthropic-style server-side ``web_search`` tool, which gives the agent
grounded live search without any additional search API key.

Responses may contain interleaved ``thinking`` blocks (GLM reasoning);
only ``text`` blocks are returned to callers.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from typing import Any, Sequence
from urllib.parse import urlparse

import httpx

from .base import LLMError, LLMMessage, LLMResponse, SearchResult

logger = logging.getLogger(__name__)

_ANTHROPIC_VERSION = "2023-06-01"
_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_BARE_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


class ZaiGlmProvider:
    """Talks to ``{base_url}/v1/messages`` using the Anthropic wire format."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "GLM-5.3-Flash",
        base_url: str = "https://api.z.ai/api/anthropic",
        timeout_seconds: float = 180.0,
        max_attempts: int = 4,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts
        self._client = httpx.Client(
            timeout=timeout_seconds,
            headers={
                "x-api-key": api_key,
                "anthropic-version": _ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
        )

    @property
    def model(self) -> str:
        return self._model

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    # LLMProvider interface
    # ------------------------------------------------------------------

    def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        system: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        thinking: bool = False,
    ) -> LLMResponse:
        return self._request(
            messages, system=system, max_tokens=max_tokens, temperature=temperature, thinking=thinking
        )

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
        return self._request(
            messages,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": max_searches}],
            thinking=thinking,
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _request(
        self,
        messages: Sequence[LLMMessage],
        *,
        system: str | None,
        max_tokens: int,
        temperature: float,
        tools: list[dict[str, Any]] | None = None,
        thinking: bool = False,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            # GLM emits `thinking` blocks by default; on long inputs the
            # reasoning can consume the entire token budget before any
            # text. Opt-in only where the caller wants it.
            "thinking": {"type": "enabled" if thinking else "disabled"},
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = tools

        last_error: Exception | None = None
        budget = max_tokens
        for attempt in range(1, self._max_attempts + 1):
            try:
                payload["max_tokens"] = budget
                response = self._client.post(f"{self._base_url}/v1/messages", json=payload)
                if response.status_code in _RETRYABLE_STATUS:
                    raise httpx.HTTPStatusError(
                        f"retryable status {response.status_code}: {response.text[:200]}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                parsed = self._parse_response(response.json())
                if not parsed.text and parsed.stop_reason == "max_tokens" and budget < 16000:
                    logger.warning("empty response at max_tokens=%d; retrying with doubled budget", budget)
                    budget = min(budget * 2, 16000)
                    continue
                return parsed
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_error = exc
                if attempt == self._max_attempts:
                    break
                backoff = min(2.0 ** (attempt - 1), 8.0) + random.uniform(0, 0.5)
                logger.warning(
                    "GLM request attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt, self._max_attempts, exc.__class__.__name__, backoff,
                )
                time.sleep(backoff)

        raise LLMError(f"GLM request failed after {self._max_attempts} attempts: {last_error}") from last_error

    def _parse_response(self, body: dict[str, Any]) -> LLMResponse:
        text_parts: list[str] = []
        search_results: list[SearchResult] = []
        search_errors: list[str] = []
        for block in body.get("content", []):
            block_type = block.get("type")
            if block_type == "text":
                text_parts.append(block.get("text", ""))
            elif block_type == "web_search_tool_result":
                search_results.extend(_extract_search_results(block))
            elif block_type == "tool_result":
                # Z.ai reports its server-side search as server_tool_use + tool_result
                # rather than Anthropic's web_search_tool_result.
                results, error = _extract_zai_tool_result(block)
                search_results.extend(results)
                if error:
                    search_errors.append(error)
        if search_errors and not search_results:
            logger.warning("server-side web search failed: %s", search_errors[0])
        usage = body.get("usage", {}) or {}
        return LLMResponse(
            text="\n".join(text_parts).strip(),
            model=body.get("model", self._model),
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            stop_reason=body.get("stop_reason", ""),
            search_results=tuple(search_results),
            search_error=(search_errors[0] if search_errors and not search_results else ""),
        )


def _extract_search_results(block: dict[str, Any]) -> list[SearchResult]:
    """Pull url/title pairs out of a web_search_tool_result content block."""
    results: list[SearchResult] = []
    content = block.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "web_search_result":
                results.append(
                    SearchResult(
                        url=item.get("url", ""),
                        title=item.get("title", ""),
                        snippet=item.get("page_subtitle", "") or "",
                    )
                )
    return results


_SEARCH_ERROR_RE = re.compile(r"MCP error|\"error\"\s*:|rate limit|limit exhausted", re.IGNORECASE)


def _extract_zai_tool_result(block: dict[str, Any]) -> tuple[list[SearchResult], str]:
    """Parse Z.ai's search tool_result block into results, or an error message.

    The content arrives as a list of text parts or as a stringified list; each
    text part holds either an error or a JSON list of result objects.
    """
    content = block.get("content")
    texts: list[str] = []
    if isinstance(content, list):
        texts = [str(item.get("text", "")) for item in content if isinstance(item, dict)]
    elif isinstance(content, str):
        texts = [content]
    results: list[SearchResult] = []
    for text in texts:
        data = extract_json_block(text)
        items = data if isinstance(data, list) else (data.get("results") if isinstance(data, dict) else None)
        for item in items or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or item.get("link") or "").strip()
            if url.startswith(("http://", "https://")):
                results.append(SearchResult(
                    url=url,
                    title=str(item.get("title", ""))[:200],
                    snippet=str(item.get("content") or item.get("snippet") or item.get("page_subtitle") or "")[:400],
                ))
    if results:
        return results, ""
    joined = " ".join(texts)
    if _SEARCH_ERROR_RE.search(joined):
        match = re.search(r'"message"\s*:\s*"([^"]+)"', joined)
        return [], (match.group(1) if match else joined[:200])
    return [], ""


def extract_json_block(text: str) -> dict[str, Any] | list[Any] | None:
    """Best-effort extraction of a JSON object/array from model output.

    Handles fenced ```json blocks and bare JSON embedded in prose.
    Scans for the outermost balanced bracket run (string-aware), trying
    each candidate from earliest opening bracket until one parses.
    Returns None when nothing parseable is found.
    """
    candidates: list[str] = [f.strip() for f in _JSON_FENCE_RE.findall(text)]
    candidates.extend(_scan_balanced_json(text))
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # GLM occasionally emits JavaScript-style escaped apostrophes
            # (\') inside otherwise valid JSON. JSON does not define that
            # escape, so accept this narrow, lossless repair while keeping all
            # other malformed structured output rejected.
            try:
                return json.loads(candidate.replace("\\'", "'"))
            except json.JSONDecodeError:
                continue
    return None


def _scan_balanced_json(text: str) -> list[str]:
    """Return balanced {...}/[...] substrings, outermost-first per position."""
    found: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] in "[{":
            depth = 0
            in_string = False
            escaped = False
            for j in range(i, n):
                ch = text[j]
                if in_string:
                    if escaped:
                        escaped = False
                    elif ch == "\\":
                        escaped = True
                    elif ch == '"':
                        in_string = False
                    continue
                if ch == '"':
                    in_string = True
                elif ch in "[{":
                    depth += 1
                elif ch in "]}":
                    depth -= 1
                    if depth == 0:
                        found.append(text[i : j + 1])
                        i = j
                        break
            else:
                break  # unbalanced; nothing more to find
        i += 1
    return found


def extract_urls(text: str) -> list[str]:
    """All distinct http(s) URLs mentioned in a text, in order of appearance."""
    seen: set[str] = set()
    urls: list[str] = []
    for match in _BARE_URL_RE.findall(text):
        cleaned = match.rstrip(".,;:!")
        key = _url_key(cleaned)
        if key not in seen:
            seen.add(key)
            urls.append(cleaned)
    return urls


def _url_key(url: str) -> str:
    parsed = urlparse(url)
    return f"{(parsed.netloc or '').lower()}{parsed.path.rstrip('/')}"
