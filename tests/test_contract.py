import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_lab.contracts.fantasy_gm import AdvisoryDecision, ResearchPackEnvelope

FIXTURES = Path(__file__).parent / "fixtures"


def test_sample_envelope_validates():
    env = ResearchPackEnvelope.model_validate_json((FIXTURES / "fantasy_gm_envelope.sample.json").read_text())
    assert env.is_sample is True
    assert env.roster[0].position == "QB"


def test_envelope_rejects_unknown_fields_and_bad_team():
    data = json.loads((FIXTURES / "fantasy_gm_envelope.sample.json").read_text())
    with pytest.raises(ValidationError):
        ResearchPackEnvelope.model_validate({**data, "yahoo_token": "x"})
    data["roster"][0]["team"] = "kc"
    with pytest.raises(ValidationError):
        ResearchPackEnvelope.model_validate(data)


def test_decision_is_always_advisory():
    with pytest.raises(ValidationError):
        AdvisoryDecision(advisory_only=False, based_on_sample_data=True, summary="s", recommendations=[], confidence="low")
    ok = AdvisoryDecision(based_on_sample_data=True, summary="s", recommendations=[], confidence="low")
    assert ok.advisory_only is True


def test_published_json_schemas_are_current():
    for model, name in ((ResearchPackEnvelope, "research_pack_envelope"), (AdvisoryDecision, "advisory_decision")):
        published = json.loads((Path(__file__).parents[1] / "contracts" / f"{name}.schema.json").read_text())
        assert published == model.model_json_schema(), f"run: python scripts/export_contract_schemas.py ({name})"
