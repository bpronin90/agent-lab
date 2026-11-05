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


def test_claude_cli_settings_explicit_and_outside_general_hash():
    base = Settings.from_env({})
    s = Settings.from_env({"AGENT_LAB_PROVIDER": "claude-cli", "AGENT_LAB_CLAUDE_MODEL": " claude-x ",
                           "AGENT_LAB_CLAUDE_PLAN_OUTPUT_TOKENS": "800"})
    assert base.claude_model is None and s.claude_model == "claude-x" and s.claude_plan_output_tokens == 800
    assert s.claude_synthesis_output_tokens == 20000
    assert not any(k.startswith("claude_") for k in base.public_dict())
    with pytest.raises(ValueError):
        Settings.from_env({"AGENT_LAB_CLAUDE_SYNTHESIS_OUTPUT_TOKENS": "0"})


# --- #35: effort and the dotenv grammar -------------------------------------------------

def test_effort_values_and_defaults():
    from agent_lab.config import CLAUDE_EFFORTS
    assert Settings.from_env({}).claude_effort is None
    assert Settings.from_env({"AGENT_LAB_CLAUDE_EFFORT": " "}).claude_effort is None
    for level in CLAUDE_EFFORTS:
        assert Settings.from_env({"AGENT_LAB_CLAUDE_EFFORT": level.upper()}).claude_effort == level
    with pytest.raises(ValueError, match="AGENT_LAB_CLAUDE_EFFORT"):
        Settings.from_env({"AGENT_LAB_CLAUDE_EFFORT": "turbo"})


def test_claude_defaults_and_general_defaults():
    s = Settings.from_env({})
    assert (s.provider, s.claude_model) == ("mock", None)
    assert (s.claude_plan_output_tokens, s.claude_synthesis_output_tokens) == (1000, 20000)


def test_dotenv_grammar():
    from agent_lab.config import parse_dotenv
    text = "# c\n\nA=1\n B = two words \nC='q  x'\nD=\"y\"\nE=\nF=$(touch /tmp/x) `id` ${A} ~ ; rm -rf /\nG='it\"s'\n"
    assert parse_dotenv(text) == {
        "A": "1", "B": "two words", "C": "q  x", "D": "y", "E": "",
        "F": "$(touch /tmp/x) `id` ${A} ~ ; rm -rf /", "G": "it\"s"}


@pytest.mark.parametrize("text", ["justtext", "=v", "1A=x", "export A=1", "A B=1", "A='x", "A=\"x'", "A=1\nA=2", "A='"])
def test_dotenv_rejects_malformed_and_duplicates_without_values(text):
    from agent_lab.config import parse_dotenv
    text = text.replace("=1", "=sekret").replace("='x", "='sekret")
    with pytest.raises(ValueError) as e:
        parse_dotenv(text)
    assert "sekret" not in str(e.value)


def test_load_env_file_missing_is_harmless(tmp_path):
    from agent_lab.config import load_env_file
    assert load_env_file(tmp_path / "nope") == {} and load_env_file(None) == {}
    (tmp_path / "bad").write_bytes(b"\xff\xfe=")
    with pytest.raises(ValueError):
        load_env_file(tmp_path / "bad")
    (tmp_path / "ok").write_text("A=1\n")
    assert load_env_file(tmp_path / "ok") == {"A": "1"}
