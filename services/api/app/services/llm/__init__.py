"""LLM provider factory: LLM_PROVIDER=auto|anthropic|openai_compatible|none."""

from __future__ import annotations

from fastapi import HTTPException

from app.config import get_settings
from app.services.llm.base import LLMError, LLMNotConfigured, LLMProvider, LLMRefused, NoneProvider, nullable, strict_object

__all__ = ["LLMError", "LLMNotConfigured", "LLMProvider", "LLMRefused", "get_llm", "get_vision_llm", "require_llm",
           "nullable", "strict_object"]


def get_llm() -> LLMProvider:
    s = get_settings()
    provider = s.effective_llm_provider
    if provider == "anthropic":
        from app.services.llm.anthropic_provider import from_settings

        return from_settings()
    if provider == "openai_compatible":
        from app.services.llm.openai_compatible import from_settings as oai

        return oai()
    if s.llm_provider == "auto":
        return NoneProvider("LLM_PROVIDER=auto, but neither OPENAI_API_KEY nor ANTHROPIC_API_KEY is set")
    return NoneProvider()


def get_vision_llm() -> LLMProvider:
    """The model that reads page images: LLM_VISION_PROVIDER when set (e.g. Claude beside a text-only Groq model)."""
    s = get_settings()
    model = s.llm_vision_model
    if s.llm_vision_provider == "anthropic":
        from app.services.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(api_key=s.anthropic_api_key, model=model or s.anthropic_model, effort=s.llm_effort,
                                 max_tokens=s.llm_max_tokens, timeout=s.llm_timeout_seconds)
    if s.llm_vision_provider == "openai_compatible":
        from app.services.llm.openai_compatible import OpenAICompatibleProvider

        return OpenAICompatibleProvider(api_key=s.llm_vision_api_key or s.openai_api_key,
                                        base_url=(s.llm_vision_base_url or s.openai_base_url).rstrip("/"),
                                        model=model or s.openai_model, max_tokens=s.llm_max_tokens,
                                        timeout=s.llm_timeout_seconds)
    return get_llm()


def require_llm() -> LLMProvider:
    """For API routes: 503 with a helpful message when AI is not configured (never fake output)."""
    llm = get_llm()
    ok, detail = llm.configured()
    if not ok:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "llm_not_configured",
                "message": "AI features are unavailable because no LLM provider is configured.",
                "detail": detail,
                "hint": "Set OPENAI_API_KEY or ANTHROPIC_API_KEY in services/api/.env, then restart the API.",
            },
        )
    return llm
