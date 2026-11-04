"""Transport-agnostic LLM interface.

Everything downstream (agent pipeline, tools, critic) talks to
``LLMProvider`` only, so the model/transport can be swapped without
touching application logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence


@dataclass(frozen=True)
class LLMMessage:
    """A single conversation message."""

    role: str  # "user" | "assistant"
    content: str


@dataclass(frozen=True)
class SearchResult:
    """One web result returned by the provider's server-side search."""

    url: str
    title: str = ""
    snippet: str = ""


@dataclass(frozen=True)
class LLMResponse:
    """A completed (non-streaming) model response."""

    text: str
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""
    # Present when the call used server-side web search.
    search_results: tuple[SearchResult, ...] = field(default_factory=tuple)
    # Non-empty when the server-side search tool ran but failed (quota, outage).
    search_error: str = ""


class LLMError(RuntimeError):
    """Raised when the provider fails after all retries."""


class LLMProvider(Protocol):
    """The single seam through which the app reaches the model."""

    def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        system: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        thinking: bool = False,
    ) -> LLMResponse:
        """Run a plain completion.

        ``thinking`` opts into extended reasoning where the transport
        supports it; structured-output stages keep it off.
        """
        ...

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
        """Run a completion with server-side web search enabled.

        The model may issue live web searches before answering; any
        results it cites are surfaced in ``LLMResponse.search_results``
        when the transport exposes them.
        """
        ...


def message_history(*pairs: tuple[str, str]) -> list[LLMMessage]:
    """Convenience builder: message_history(("user", "hi"), ...)."""
    return [LLMMessage(role=r, content=c) for r, c in pairs]
