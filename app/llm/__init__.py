"""LLM access layer. The rest of the app imports from here only."""

from .base import LLMError, LLMMessage, LLMProvider, LLMResponse, SearchResult, message_history
from .claude_cli import ClaudeCliProvider
from .fallback import FallbackProvider
from .zai_glm import ZaiGlmProvider, extract_json_block, extract_urls


def build_provider(settings, *, role: str = "default") -> LLMProvider:
    """The configured provider (LLM_PROVIDER); the only place one is constructed.

    role="research" selects the faster model for the researcher's extraction calls
    when running on the Claude CLI.
    """
    if role == "writer":
        claude = build_provider(settings)
        if settings.writer_provider == "zai" and settings.zai_api_key:
            glm = ZaiGlmProvider(
                settings.zai_api_key, model=settings.glm_model, base_url=settings.zai_anthropic_base_url,
            )
            return FallbackProvider(glm, claude, name="GLM writer")
        return claude
    if settings.llm_provider == "claude_cli":
        model = settings.claude_cli_research_model if role == "research" else settings.claude_cli_model
        return ClaudeCliProvider(claude_path=settings.claude_cli_path, model=model)
    return ZaiGlmProvider(
        settings.zai_api_key, model=settings.glm_model, base_url=settings.zai_anthropic_base_url,
    )

__all__ = [
    "LLMError",
    "LLMMessage",
    "LLMProvider",
    "LLMResponse",
    "SearchResult",
    "message_history",
    "build_provider",
    "ClaudeCliProvider",
    "FallbackProvider",
    "ZaiGlmProvider",
    "extract_json_block",
    "extract_urls",
]
