"""Regenerate contracts/*.schema.json from the Pydantic contract models."""

import json
from pathlib import Path

from agent_lab.contracts.fantasy_gm import AdvisoryDecision, ResearchPackEnvelope

OUT = Path(__file__).resolve().parents[1] / "contracts"
for model, name in ((ResearchPackEnvelope, "research_pack_envelope"), (AdvisoryDecision, "advisory_decision")):
    (OUT / f"{name}.schema.json").write_text(json.dumps(model.model_json_schema(), indent=2) + "\n")
    print(f"wrote contracts/{name}.schema.json")
