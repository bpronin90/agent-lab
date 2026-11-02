"""Model provider factory: Gemini free tier, or the offline mock."""

from __future__ import annotations

import asyncio
import contextvars
import time
from typing import Callable

from pydantic_ai.models import Model
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

from agent_lab.config import Settings
from agent_lab.mock_provider import MOCK_MODEL_NAME, build_mock_model


class ProviderConfigError(RuntimeError):
    """Raised when a provider is selected but not configured."""


_last_request_at: float | None = None
# Set by the runtime for the duration of an attempt: seconds left before the run deadline.
remaining_seconds: contextvars.ContextVar[Callable[[], float] | None] = contextvars.ContextVar(
    "remaining_seconds", default=None)


class PacingDeadlineError(RuntimeError):
    """The wait needed to respect the request pacing would outlast the run deadline."""


class PacedModel(WrapperModel):
    """Spaces model requests at least `interval` seconds apart so a multi-step research run
    stays under the Gemini free tier's per-minute request limit. It only delays; it never retries."""

    def __init__(self, wrapped: Model, interval: float):
        super().__init__(wrapped)
        self.interval = interval

    async def request(self, messages, model_settings, model_request_parameters):
        global _last_request_at
        if _last_request_at is not None:
            wait = _last_request_at + self.interval - time.monotonic()
            if wait > 0:
                remaining = remaining_seconds.get()
                if remaining is not None and wait > remaining():
                    raise PacingDeadlineError(f"request pacing wait {wait:g}s exceeds remaining deadline")
                await asyncio.sleep(wait)
        _last_request_at = time.monotonic()
        return await super().request(messages, model_settings, model_request_parameters)


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

        model = GoogleModel(model_name or settings.gemini_model, provider=GoogleProvider(api_key=settings.gemini_api_key))
        if settings.min_request_interval_seconds > 0:
            return PacedModel(model, settings.min_request_interval_seconds)
        return model
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
