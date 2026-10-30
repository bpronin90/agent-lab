"""Model provider factory: Gemini free tier, or the offline mock."""

from __future__ import annotations

from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from agent_lab.config import Settings
from agent_lab.mock_provider import MOCK_MODEL_NAME, build_mock_model


class ProviderConfigError(RuntimeError):
    """Raised when a provider is selected but not configured."""


def model_names(settings: Settings) -> list[str]:
    """Primary model first, then the optional fallback."""
    if settings.provider == "mock":
        return [MOCK_MODEL_NAME]
    names = [settings.gemini_model]
    if settings.gemini_fallback_model and settings.gemini_fallback_model != settings.gemini_model:
        names.append(settings.gemini_fallback_model)
    return names


def build_model(settings: Settings, model_name: str | None = None) -> Model:
    if settings.provider == "mock":
        return build_mock_model()
    if settings.provider == "gemini":
        if not settings.gemini_api_key:
            raise ProviderConfigError("AGENT_LAB_PROVIDER=gemini requires GEMINI_API_KEY to be set.")
        # Imported lazily so the mock path never touches the Google SDK.
        from pydantic_ai.models.google import GoogleModel
        from pydantic_ai.providers.google import GoogleProvider

        return GoogleModel(model_name or settings.gemini_model, provider=GoogleProvider(api_key=settings.gemini_api_key))
    raise ProviderConfigError(f"Unknown provider {settings.provider!r}")


def build_model_settings(settings: Settings, temperature: float, thinking_budget: int) -> ModelSettings:
    """Explicit per-agent settings. Gemini 2.5 thinks by default; cap thinking so it cannot
    consume the whole output budget and truncate the structured answer."""
    if thinking_budget >= settings.max_output_tokens:
        raise ProviderConfigError("thinking_budget must be smaller than AGENT_LAB_MAX_OUTPUT_TOKENS")
    model_settings: dict = {"max_tokens": settings.max_output_tokens, "temperature": temperature}
    if settings.provider == "gemini":
        model_settings["google_thinking_config"] = {"thinking_budget": thinking_budget}
    return ModelSettings(**model_settings)
