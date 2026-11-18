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

# Steps whose model can be chosen per step (STEP_MODELS), and the role each one uses by default.
STEP_ROLES = {
    "guide_write": "default",   # Korean guide: writing and the fix pass (main model)
    "guide_check": "research",  # Korean guide: the check against the plan (fast model)
    "outline": "research",      # shortlisted concept -> writing outline (fast model)
    "option_judge": "default",  # the panel's three judges (main model)
}


def build_step_providers(settings) -> dict[str, LLMProvider]:
    """Providers for the steps STEP_MODELS overrides (Claude CLI only); other steps keep their role's model."""
    if settings.llm_provider != "claude_cli":
        return {}
    unknown = [step for step, _ in settings.step_models if step not in STEP_ROLES]
    if unknown:
        raise ValueError(f"STEP_MODELS names unknown steps {unknown}; known: {sorted(STEP_ROLES)}")
    return {
        step: ClaudeCliProvider(claude_path=settings.claude_cli_path, model=model)
        for step, model in settings.step_models
    }


__all__ = [
    "LLMError",
    "LLMMessage",
    "LLMProvider",
    "LLMResponse",
    "SearchResult",
    "message_history",
    "build_provider",
    "build_step_providers",
    "STEP_ROLES",
    "ClaudeCliProvider",
    "FallbackProvider",
    "ZaiGlmProvider",
    "extract_json_block",
    "extract_urls",
]
