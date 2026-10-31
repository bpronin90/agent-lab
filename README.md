# Agent Lab — Phase 1

A small, reusable foundation for running AI agents at zero recurring cost. It is
also a hands-on way to learn Docker and Kubernetes.

- **Runtime:** Python + [PydanticAI](https://ai.pydantic.dev). It does real LLM tool calling,
  keeps the reasoning loop bounded (request, tool-call, token and time caps) and validates
  the structured output.
- **Models:** Gemini free tier (`AGENT_LAB_PROVIDER=gemini`), or a deterministic **mock**
  provider (the default) that works offline with no credentials.
- **Demo agent:** three safe tools (`calculate`, `current_time`, `lookup_note`). None of them
  touches the network or the filesystem.
- **Packaging:** Docker image and Docker Compose for local development.
- **Orchestration:** Kubernetes manifests for a single-node K3s cluster. CI runs a k3d smoke test.
- **Cloud schedule:** a GitHub Actions workflow you start by hand. Its schedule is gated off until you enable it.
- **Persistence:** a local `runs.jsonl` file today. A proposed `agent_lab` Supabase schema is included but **not applied**.
- **Calm Down (Experiment 3):** a research agent that investigates a scary AI prediction on both sides, checks its citations against retrieved text, and renders a one-off email. See [docs/experiments/calm-down.md](docs/experiments/calm-down.md).
- **Fantasy GM:** a versioned integration contract (JSON Schema + Pydantic). The Fantasy GM repository is untouched.

## Architecture

```
            ┌──────────── trigger ─────────────┐
            │ CLI · docker compose · K8s CronJob · GitHub Actions (manual / gated schedule)
            ▼
   agent_lab.cli ──► runtime.run_agent()  ── UsageLimits(requests, tool calls) + max_tokens
                          │
                          ├─► agents/REGISTRY["demo"] ── PydanticAI Agent(output_type=AgentAnswer)
                          │        └─ tools: calculate · current_time · lookup_note
                          ├─► models.build_model() ── mock (FunctionModel)  |  Gemini (GoogleModel)
                          └─► RunRecord ──► runs/runs.jsonl   (Phase 2: agent_lab.runs in Supabase)

   contracts/fantasy_gm.py   ResearchPackEnvelope ──► agent ──► AdvisoryDecision (advisory only)
```

| Path | Purpose |
| --- | --- |
| `src/agent_lab/config.py` | Settings from environment variables. No secret has a default. |
| `src/agent_lab/models.py` | Provider factory (`mock` / `gemini`). |
| `src/agent_lab/mock_provider.py` | Offline tool-calling model for tests and demos. |
| `src/agent_lab/runtime.py` | Bounded run, `RunRecord` capture, JSONL log. |
| `src/agent_lab/agents/` | Agent registry, the demo agent, and the `calm_down` research agent. |
| `src/agent_lab/contracts/fantasy_gm.py` | Fantasy GM integration contract. |
| `contracts/*.schema.json` | Generated JSON Schemas of the contract. |
| `Dockerfile`, `compose.yaml` | Container image and local dev setup. |
| `k8s/` | Namespace, ConfigMap, PVC and CronJob, applied with Kustomize. |
| `.github/workflows/` | `ci.yml`, `k3d-smoke.yml`, `agent-run.yml`. |
| `supabase/proposed_migrations/` | Schema proposal (do not apply yet). |
| `docs/` | Learning guides, exercises, security, contract, Phase 2. |

### How the bounded loop works

PydanticAI runs a loop: model → tool calls → model → … → structured output.
Agent Lab caps each run in four ways:

- `request_limit`: `AGENT_LAB_MAX_REQUESTS`, default 6.
- `tool_calls_limit`: `AGENT_LAB_MAX_TOOL_CALLS`, default 8.
- `max_tokens`: caps the length of each model response.
- In Kubernetes, `activeDeadlineSeconds` also caps wall-clock time.

When a cap is hit, the run stops and is recorded as `failed`. Tools report bad
input with `ModelRetry`, so the model can correct itself, up to 2 retries. The output
must validate against `AgentAnswer`; if it doesn't, PydanticAI asks the model to try again.

### Failure handling

Every run produces a `RunRecord`, including runs that crash. A failed run's
`error_class` says what went wrong:

| `error_class` | Meaning | Retried? | CLI exit code |
| --- | --- | --- | --- |
| `config` | Missing key, bad request, auth error (400/401/403), retired model (404) | No. A 404 falls back to the fallback model | 2 |
| `transient` | 429, 408, 5xx, network error | Yes, up to `AGENT_LAB_MAX_ATTEMPTS` (default 3), then the fallback model | 3 |
| `limit` | A usage cap or `AGENT_LAB_DEADLINE_SECONDS` (default 120) was hit | No | 1 |
| `validation` | The model's output never matched the schema | No | 1 |
| `tool` | A tool raised an unexpected exception | No | 1 |
| `internal` | Anything else | No | 1 |

Retries details:

- **Backoff.** Exponential with jitter, capped at 30 s. A provider `Retry-After` is honored
  in full, never shortened.
- **Deadline.** If the required wait would run past the deadline, the run stops at once
  as `transient` (exit 3) instead of retrying early. A request already in flight is
  bounded by the provider's own timeout and the job timeout.
- **After a tool runs.** Once a tool has executed, a run is never retried and never
  falls back to another model, so neither can hide repeated work.
- **Fallback model.** `AGENT_LAB_GEMINI_FALLBACK_MODEL` (optional) is tried only when
  the primary model is unavailable.
- **One budget per run.** The request, tool-call and token totals are shared across every
  retry and the fallback; they don't reset per attempt. A provider call that fails still
  counts against `AGENT_LAB_MAX_REQUESTS` and appears in the recorded totals, because it
  spent API quota.

Each record lists every attempt, plus:

- `tools_used`: derived from the tools that actually executed, not from what the model claims;
- `served_model`: the model name the provider reported;
- `git_sha` and `config_hash`: a hash of the non-secret settings, instructions and tools.

Per-agent model settings (temperature and Gemini's thinking budget) live in code,
in `src/agent_lab/agents/__init__.py`. The thinking budget must stay below
`AGENT_LAB_MAX_OUTPUT_TOKENS`, so thinking cannot use up the space for the answer.

## Quick start

Python 3.11+ and Git are required for the local path. Docker and Kubernetes are
optional. Start from a fresh checkout; private agent configuration is not needed.

```sh
git clone https://github.com/bpronin90/agent-lab.git
cd agent-lab
python3 -m venv .venv && . .venv/bin/activate
pip install --require-hashes -r requirements-dev.lock -r requirements-build.lock
pip install --no-deps --no-build-isolation -e .
pytest                                          # fully offline
agent-lab run "What is (12+30)*2 and what is docker?"
cat runs/runs.jsonl
```

To use Gemini (free tier), create a key at https://aistudio.google.com/apikey, then:

```sh
export AGENT_LAB_PROVIDER=gemini GEMINI_API_KEY=...   # never commit it
agent-lab run "What time is it in Tokyo, and what is 17*23?"
```

Docker / Compose:

```sh
docker compose build
docker compose run --rm agent run "What is 6*7?"
docker compose --profile test run --rm test
```

Kubernetes (local, with k3d) is covered step by step in
[docs/learning/exercises.md](docs/learning/exercises.md).

## GitHub Actions

| Workflow | Trigger | What it does |
| --- | --- | --- |
| `ci.yml` | PR, push to main | Runs the tests and checks that the contract schemas are current. Builds the image and runs it with `--network none`. |
| `k3d-smoke.yml` | PR touching runtime/k8s, manual | Creates a throwaway K3s cluster with k3d, applies `k8s/`, runs one Job and checks its JSON output, then deletes the cluster. |
| `agent-run.yml` | Manual (`workflow_dispatch`), weekly schedule | Runs the demo agent and uploads `runs/` as an artifact. |

**Gating for `agent-run.yml`.** Scheduled runs are skipped unless the repository
variable `AGENT_LAB_SCHEDULE_ENABLED` is set to `true`. The provider is `mock`
unless you choose `gemini` for a manual run, or set the variable
`AGENT_LAB_SCHEDULE_PROVIDER=gemini` for scheduled runs. If Gemini is selected
but the `GEMINI_API_KEY` secret is missing, the job fails fast. Nothing is written to Supabase.

## Learning material

- [docs/learning/docker.md](docs/learning/docker.md) explains every Docker concept used here.
- [docs/learning/kubernetes.md](docs/learning/kubernetes.md) does the same for Kubernetes, K3s and k3d.
- [docs/learning/exercises.md](docs/learning/exercises.md) has hands-on exercises with expected results.
- [docs/learning/n8n-kubernetes.md](docs/learning/n8n-kubernetes.md) has n8n submit, monitor, and collect agent Kubernetes Jobs (Experiment 2).

## Other docs

- [docs/experiments/calm-down.md](docs/experiments/calm-down.md): Calm Down research agent (`agent-lab calm-down`; exit 4 = research insufficient, 5 = waiting for human support review).
- [docs/security.md](docs/security.md): security model and free-tier limits.
- [docs/supabase-schema.md](docs/supabase-schema.md): the `agent_lab` schema proposal.
- [docs/integration/fantasy-gm-contract.md](docs/integration/fantasy-gm-contract.md): the Fantasy GM contract.
- [docs/phase-2.md](docs/phase-2.md): the Phase 2 proposal.

## Adding an agent (pattern for later phases)

1. Create `src/agent_lab/agents/<name>.py` with a `build_<name>_agent(model)` function that
   returns `Agent(..., output_type=<PydanticModel>)`.
2. Register it in `src/agent_lab/agents/__init__.py`.
3. Add offline tests that use the mock provider or a `FunctionModel`.

## Development rules

Read [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md) before changing
code. Keep changes scoped, add failure-path tests for behavior changes, and run
the targeted checks required by the contract. CI runs the complete suite.

Dependencies are locked with hashes. Docker and CI install only from the `*.lock` files.
After changing dependencies in `pyproject.toml`, regenerate the locks with
[uv](https://docs.astral.sh/uv/):

```sh
uv pip compile pyproject.toml --universal --python-version 3.11 --generate-hashes -o requirements.lock
uv pip compile pyproject.toml --extra dev --universal --python-version 3.11 --generate-hashes -o requirements-dev.lock
```

Locked local setup: `pip install --require-hashes -r requirements-dev.lock -r requirements-build.lock && pip install --no-deps --no-build-isolation -e .`
After changing `contracts/fantasy_gm.py`, regenerate the schemas with
`python scripts/export_contract_schemas.py`. Never commit `.env`, keys, or real personal data.
