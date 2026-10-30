import pytest

from agent_lab.config import Settings
from agent_lab.models import ProviderConfigError, build_model


def test_defaults_are_offline():
    s = Settings.from_env({})
    assert s.provider == "mock"
    assert s.gemini_api_key is None


def test_rejects_unknown_provider():
    with pytest.raises(ValueError):
        Settings.from_env({"AGENT_LAB_PROVIDER": "openai"})


def test_rejects_non_positive_limits():
    with pytest.raises(ValueError):
        Settings.from_env({"AGENT_LAB_MAX_REQUESTS": "0"})


def test_gemini_requires_key():
    with pytest.raises(ProviderConfigError):
        build_model(Settings.from_env({"AGENT_LAB_PROVIDER": "gemini"}))


def test_gemini_model_builds_without_network():
    model = build_model(Settings.from_env({"AGENT_LAB_PROVIDER": "gemini", "GEMINI_API_KEY": "fake"}))
    assert model.model_name == "gemini-2.5-flash"


def test_thinking_budget_must_fit_output_cap():
    from agent_lab.models import build_model_settings

    s = Settings.from_env({"AGENT_LAB_PROVIDER": "gemini", "AGENT_LAB_MAX_OUTPUT_TOKENS": "512"})
    with pytest.raises(ProviderConfigError):
        build_model_settings(s, 0.2, 512)
    ms = build_model_settings(Settings.from_env({"AGENT_LAB_PROVIDER": "gemini"}), 0.2, 512)
    assert ms["google_thinking_config"] == {"thinking_budget": 512} and ms["max_tokens"] == 2048


def test_fallback_model_listed_after_primary():
    from agent_lab.models import model_names

    s = Settings.from_env({"AGENT_LAB_PROVIDER": "gemini", "AGENT_LAB_GEMINI_FALLBACK_MODEL": "gemini-2.5-flash-lite"})
    assert model_names(s) == ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
