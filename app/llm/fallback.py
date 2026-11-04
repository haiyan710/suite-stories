"""Provider wrapper: try a primary provider, fall back to another on failure."""

from __future__ import annotations

import logging
from typing import Sequence

from .base import LLMError, LLMMessage, LLMProvider, LLMResponse

logger = logging.getLogger(__name__)


class FallbackProvider:
    """Use `primary` (e.g. GLM) and switch to `fallback` (e.g. Claude) when it fails.

    Failures include transport errors, quota errors and empty answers, so a
    secondary model being down never stalls the pipeline.
    """

    def __init__(self, primary: LLMProvider, fallback: LLMProvider, *, name: str = "") -> None:
        self._primary = primary
        self._fallback = fallback
        self._name = name or type(primary).__name__
        self.last_used = ""

    def complete(self, messages: Sequence[LLMMessage], **kwargs) -> LLMResponse:
        return self._call("complete", messages, kwargs)

    def complete_with_search(self, messages: Sequence[LLMMessage], **kwargs) -> LLMResponse:
        return self._call("complete_with_search", messages, kwargs)

    def _call(self, method: str, messages: Sequence[LLMMessage], kwargs: dict) -> LLMResponse:
        try:
            response = getattr(self._primary, method)(messages, **kwargs)
            if response.text.strip():
                self.last_used = "primary"
                return response
            logger.warning("%s returned an empty answer; using fallback", self._name)
        except LLMError as exc:
            logger.warning("%s failed (%s); using fallback", self._name, exc)
        self.last_used = "fallback"
        return getattr(self._fallback, method)(messages, **kwargs)
