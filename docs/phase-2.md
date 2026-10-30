# Phase 2 proposal (revised)

This replaces the first Phase 2 draft. That draft was reviewed against Agent Lab
`main` (2b1af43) and against `bpronin90/fantasy-gm` at b822b57, whose Phase 2
local path is described in that repository's `docs/phase-2-usage.md`. The review
found that the draft was mostly a list of infrastructure topics. It did not say
what new capability Phase 2 would deliver, and several parts of it would not work.

Nothing here is implemented yet. Constraints that still hold:

- Zero additional recurring cost.
- No laptop has to stay running.
- No paid LLM APIs and no Oracle Cloud.
- Existing Supabase schemas and applications stay untouched.
- Fantasy GM changes need separate approval in that repository.
- Roster transactions are never automated.

**Second review (applied).** Five corrections were made after the second review:

1. The cloud ledger is now durable: a hydrate/sync mirror in Supabase that keeps Fantasy GM's idempotency rules (P2.4, A13).
2. Private inputs move from `workflow_dispatch` to a protected Supabase input store, and real mode uploads no artifacts (P2.4, A14).
3. The order is now: a synthetic-only cloud dry run (P2.3), then persistence and input handling (P2.4), then the first real execution (P2.5).
4. Isolation is measured on the live project, not assumed from grants, because the role still inherits `PUBLIC` privileges (P2.4, A15).
5. A Gemini quota error no longer counts as an integration pass. A8 is split into a failure-handling test (A8a) and a required successful tool-calling run (A8b).

---

## 1. What was wrong with the first draft

### 1.1 The Fantasy GM agent was the wrong agent

**What the draft proposed:** an agent that reads `ResearchPackEnvelope` (Agent Lab's v1.0 contract) and returns
`AdvisoryDecision`, using "roster slot math" and "bye-week check" tools.

**What Fantasy GM already does better** (its Phase 2 local path):

| Capability | Fantasy GM today | First-draft agent |
| --- | --- | --- |
| Snapshot validation | Snapshot v2 with stable `player_id`, verified `gsis_id`, verified `eligible_slots`, IDP positions, `WSH→WAS` normalization, and a 24-hour freshness rule | Weaker re-implementation: offensive positions only, regex-checked team codes, no eligibility |
| Evidence | Fresh nflverse fetches with retries, timeouts, size limits and content hashes. Official injury/news items carry `source_url`, `fetched_at` and `evidence_id`. The pack is frozen with a `pack_id` hash | None. The envelope has no evidence at all |
| Lineup | Exact search over eligibility, IR, bye/out and kickoff locks, plus up to five alternatives | "Slot math": a duplicate of this, done worse |
| Decisions | Idempotent, immutable SQLite ledger. Writes are refused after the cutoff, after kickoff, or if the pack is older than 6 hours | A second, conflicting `decisions` table |

The first-draft agent would have produced unsourced recommendations from roster names alone.
That is less than Fantasy GM already delivers, and it would contradict Fantasy GM's rules:
cite only the frozen pack, and treat unknowns as blocking.

**The step Fantasy GM still leaves to a person is step 7 of its usage guide.** A person has
Codex write `assessments.json`: one entry per player, with these fields:

- `rank_score`
- `thesis`
- `scoring_fit`
- `confidence` and `confidence_reason`
- `counterargument`
- `risk`
- `contingency`
- `recheck_at`
- `evidence_ids`, drawn only from that player's frozen pack

This is the only step that needs an LLM, and the only step that stops the pipeline from running unattended.

**What the agent should add, precisely:**

1. **Automating step 7 in the cloud.** The agent writes `assessments.json` from a frozen pack, so a
   reviewable report exists before you sit down. Today nothing happens until you open Codex.
2. **Citations checked in code, not just requested in a prompt.** An output validator rejects
   (`ModelRetry`) any of the following:
   - an `evidence_id` that is not in that player's pack evidence;
   - a rostered, non-IR player with no assessment;
   - a `recheck_at` that is not before that player's kickoff;
   - a `rank_score` outside the rubric range.

   The model cannot cite anything outside the pack, because it has no tool that fetches anything.
3. **An explicit gap list.** For each player, the agent lists the evidence that would change the call
   but is missing or stale: injury status, a practice report, usage trend. Each item says *who must verify it, and where*.
   That gives you a short checklist instead of a raw pack.
4. **Structured disagreement.** For every close call (adjacent `rank_score`s competing for one slot), the
   agent must fill `counterargument` and `contingency` with reasoning that cites evidence. This is the part
   where an LLM actually helps.

**What the agent explicitly does *not* do:**

- collect evidence;
- verify facts;
- choose the lineup;
- write the ledger.

All four stay in Fantasy GM. The agent's output goes into the unchanged Fantasy GM `report` command,
and Fantasy GM's own evaluator decides whether the result is blocked.

**How the agent works with fresh, verifiable evidence in the cloud:**

- Freshness comes from Fantasy GM, not from the agent.
  - `pack` refuses an `as_of` more than 5 minutes from now, fetches nflverse live, and freezes the cutoff.
  - The ledger refuses packs older than 6 hours and any write after kickoff.
- So the cloud job must run **pack, then assess, then report, inside one GitHub Actions job**, within minutes.
  It must not reuse an old pack.
- Verification does not become automatic.
  - Fantasy GM requires manually verified eligibility, scoring and lock attestations.
  - Official injury and news items must be supplied by a person (team-site items additionally need `manually_verified_official: true`).
  - The cloud run therefore reads your weekly snapshot and official-evidence JSON from a protected input store (P2.4), never from workflow inputs. Where something is missing, the report comes out **blocked**, and that is the correct outcome.
- Yahoo integration is still pending approval, so a person still supplies the weekly snapshot. Phase 2
  removes the *always-on laptop* and the *manual Codex step*. It does not remove the human.

**Two decisions only you can make, needed before any real Fantasy GM data reaches Gemini (P2.5):**

- **Policy conflict.** Fantasy GM uses user-initiated Codex analysis rather than autonomous AI execution. Gemini's free tier is not a paid API, but it is a different policy. You decide.
  If you approve Gemini, Fantasy GM's docs should be updated in that repository with your approval. If you don't,
  the agent runs in `mock`/offline evaluation only, and Codex remains the production path.
- **Data exposure.** Prompts sent to Gemini's free tier may be used by Google to improve its products. NFL
  player names are public. Your league and team identifiers, and which players make up your roster, are not.
  The agent must drop `league_id` and `team_id` and send only pack evidence. Even then, the roster composition is
  still sent. You need to explicitly accept that.

### 1.2 Supabase persistence would not have worked as designed

| Problem | Why it fails | Fix |
| --- | --- | --- |
| Writes through PostgREST, with `agent_lab` deliberately **not** in the Data API's exposed schemas | PostgREST serves only exposed schemas, so every write would return an error | Don't use the Data API. Connect with Postgres directly through Supabase's **connection pooler (Supavisor)**, which accepts IPv4. That matters because GitHub-hosted runners don't support the IPv6-only direct connection, and the IPv4 add-on costs money. No API configuration changes are proposed |
| `service_role` as the writer | `service_role` bypasses RLS and may have access beyond `agent_lab`. In a database hosting multiple applications, a leaked credential could expose unrelated data | A dedicated `agent_lab_writer` role with `USAGE` on `agent_lab` only, `INSERT`/`SELECT` on its tables, and no `UPDATE`/`DELETE`. The migration creates it as `NOLOGIN` with no password; you enable login and set the password manually in the dashboard, so no credential lives in Git |
| `decisions.run_id ... on delete cascade`, combined with proposed retention deletes | Pruning old runs would silently delete decisions, which are the most valuable records | No cascade (`on delete restrict`). No retention deletes in Phase 2: about 2–4 runs a week is a few MB a year |
| `agent_lab.decisions` as a decision store | It duplicates Fantasy GM's ledger and would let the two disagree | Store immutable **artifacts** (pack, assessments, evaluation, report, ledger record) by SHA-256, linked to the run. Fantasy GM's ledger semantics (decision key = digest of pack and context, `adoption: unknown`) stay authoritative |
| `agent_lab.agents` configuration table | Agent configuration in a database bypasses code review, tests and git history, and nothing reads it | Drop the table. Record `git_sha`, `model`, the model settings and a `config_hash` on each run instead |

Phase 1's `supabase/proposed_migrations/` file will be **replaced** by a revised proposal, not amended in
place. It has never been applied, so nothing needs migrating.

### 1.3 The infrastructure items, challenged

| Item | Verdict | Reasoning |
| --- | --- | --- |
| Agent configuration in a database | **Drop** | See 1.2. Revisit only if a future UI edits configuration. |
| GHCR image publishing | **Defer** | Nothing pulls an image. GitHub Actions installs from source in seconds, and k3d imports locally. It becomes useful only when a K3s node you own runs the job (Lab L1). |
| Kustomize overlays | **Defer** | There is only one environment. An overlay with nothing to differ is ceremony. |
| SOPS / Sealed Secrets | **Defer** | No cluster holds a real secret. `kubectl create secret` already keeps the key out of Git. |
| NetworkPolicy "Gemini-only egress" | **Correct the claim** | Standard NetworkPolicy matches IPs and ports, not domain names. K3s's built-in controller cannot express "only `generativelanguage.googleapis.com`". Realistic options: default-deny egress plus DNS plus 443 to anywhere, or Cilium's `toFQDNs` (free, but it replaces K3s's default networking). Kept as an optional lab. |
| Helm | **Drop for now** | There is one chart consumer: you. Kustomize already covers the need. |
| Dependabot and SHA-pinned Actions | **Do (cheap)** | Free, and it fixes a real supply-chain gap. See D10. |

**Learning without losing useful functionality:** each technology becomes an optional **lab** that runs
the real Phase 2 job, never a toy. Labs come *after* the critical path (P2.0–P2.5) works. See §4.

### 1.4 Phase 1 design defects (verified)

These are actual defects, not preferences. "Verified" means I reproduced the problem against
current `main`.

| ID | Defect | Evidence | Severity |
| --- | --- | --- | --- |
| D1 | `run_agent` catches only `AgentRunError`. A tool raising anything except `ModelRetry` (for example an `OSError`), or a transport error (`httpx.ConnectError`), crashes the CLI **without a RunRecord**, so failures leave no history | Verified: both raise out of `run_agent` | High |
| D2 | No failure taxonomy or retry. A Gemini `429`/`503` (transient), a missing key (config), a usage cap and a schema failure all become `failed`, or an exit code, with no class and no retry. Free-tier `429`s are expected | `runtime.py`, `cli.py` | High |
| D3 | Supabase access design does not work (see 1.2) | Migration and draft | High |
| D4 | `decisions` cascade (see 1.2) | Migration line 60 | High |
| D5 | Wrong ownership of the integration contract. Agent Lab defines its own `ResearchPackEnvelope`, which diverges from Fantasy GM snapshot v2: no IDP, no eligibility, no evidence, different team validation. The consumer must not own the producer's schema | `contracts/fantasy_gm.py` | High |
| D6 | Cloud run history is ephemeral. In Actions, `runs.jsonl` lives on a throwaway runner, and the 14-day artifact is the only record. The "run history" in Phase 1 is effectively local only | `agent-run.yml` | Medium |
| D7 | Model configuration is risky (needs a live check). Gemini 2.5 models "think" by default, and thinking tokens may count against `max_output_tokens=2048`, which could truncate structured output into `UnexpectedModelBehavior`. There is no thinking budget, temperature or fallback model. The served model version is not recorded, and the hard-coded `gemini-2.5-flash` default will eventually be retired | `config.py`, `runtime.py` | Medium |
| D8 | Tool-call history drops `final_result` by hard-coded name. A custom output tool name or a library change would leak the output tool into `tool_calls` | `runtime.py` | Low |
| D9 | `AgentAnswer.tools_used` is self-reported by the model and can be wrong. The runtime already knows the real calls | `schemas.py` | Low |
| D10 | Supply chain. `k3d-smoke.yml` pipes `install.sh` from k3d's **`main` branch** into `bash`. Actions are pinned by tag, and Python dependencies are unpinned (`pydantic-ai-slim>=2.60,<3`, a fast-moving library) with no lockfile, so builds are not reproducible | Workflows, `pyproject.toml` | Medium |
| D11 | The schedule doesn't match the domain. A Monday 14:17 UTC cron is unrelated to NFL lock times. GitHub schedules can be delayed or dropped, and are disabled after 60 days without repository activity | `agent-run.yml` | Medium (for Fantasy GM) |
| D12 | Policy and data exposure for Fantasy GM data on the Gemini free tier is unresolved (see 1.1) | — | Blocker for P2.5 (P2.3 uses synthetic data only) |

Optional improvements (not defects):

- base image pinned by digest;
- image scanning;
- the mock provider is coupled to demo tool names;
- the `PYDANTIC_AI_NO_BANNER` side effect at import time;
- `docs/security.md` and `docs/supabase-schema.md` will need updating when P2.0 and P2.4 land.

---

## 2. Revised, prioritized Phase 2

Ordering rule: make the runtime trustworthy, then make the agent correct offline, then run it in the cloud, then persist.

### P2.0 — Runtime hardening (Agent Lab only)

**Status:** implemented, and A1–A3 and A8b pass. A8b was met on 2026-10-30 by Actions run 40998037788 (`agent-run.yml`, `provider=gemini`, commit 3d78670), on its first attempt:

- `status=succeeded`, with `served_model=gemini-2.5-flash`;
- 2 real tool calls: `calculate((17*23)+4)` and `lookup_note(k3s)`;
- output validated against `AgentAnswer`;
- 2 requests, 824 input tokens and 244 output tokens.

With a thinking budget of 512, the output was not truncated, which addresses D7 for this agent.

Fixes D1, D2, D7–D10.

- **Every run returns a `RunRecord`.** Catch everything at the boundary and classify the error as one of
  `config`, `transient`, `limit`, `validation`, `tool` or `internal`. Exit codes by class:
  - 2: config
  - 3: transient
  - 1: everything else
- **Bounded retry.** Retry `transient` failures (429, 5xx, transport errors) with exponential backoff and jitter:
  at most 3 attempts, and only before the first tool call has run. Tools are read-only, but retrying after one
  has run would hide repeated work. Honor `Retry-After`. Retries count toward a wall-clock deadline
  (`AGENT_LAB_DEADLINE_SECONDS`).
- **Per-agent model settings in code.** Model name, temperature and a thinking budget, with the thinking
  budget set explicitly and kept within the output token cap. Add an optional `AGENT_LAB_GEMINI_FALLBACK_MODEL`
  that is used only on `transient` failures or a retired-model error.
- **Accurate run records.**
  - Record the served model name from the response.
  - Record `git_sha` and `config_hash`.
  - Derive `tools_used` from actual calls.
  - Identify output tools from the agent's own output tool definitions, not a hard-coded name.
- **Supply chain.**
  - A lockfile with hashes (`uv lock` or `pip-compile --generate-hashes`), used by Docker and CI.
  - Actions pinned by SHA.
  - The k3d binary downloaded by version and verified by checksum.
  - Dependabot for pip and Actions.

### P2.1 — Correct the contract boundary

Fixes D5. Needs one small approved change in Fantasy GM.

- **Fantasy GM owns the schemas.** It would add a JSON Schema export for `pack.json` (v2) and
  `assessments.json`, generated from the code that already validates them. Agent Lab vendors a copy pinned
  to a Fantasy GM commit SHA.
- **Delete Agent Lab's `ResearchPackEnvelope` and `AdvisoryDecision`.**
- **Use Fantasy GM's real evaluator in CI.** Agent Lab CI checks out Fantasy GM at the pinned SHA using a
  read-only, fine-grained PAT (free) scoped to that one repository with *Contents: read*. It then runs
  `python3 -m src.fantasy_gm report` on the agent's output against a **synthetic** v2 pack. Fantasy GM's
  real evaluator, not a copy, decides whether the output is valid.
- **Fallback if you don't approve the Fantasy GM change:** Agent Lab hand-writes the schemas from
  `docs/phase-2-usage.md` and the CI evaluator run stays. The evaluator run is the real safety net.

### P2.2 — Assessment agent (offline first)

- `agents/fantasy_assessor.py` takes `pack.json` (v2) and produces two things:
  1. `assessments.json` in exactly Fantasy GM's format;
  2. `gaps.json`, a list of what a person must verify, with source hints.
- **Tools.** All read-only, over the frozen pack only, with no network access:
  - `list_players()`
  - `get_evidence(player_id, kind)`
  - `compare_usage(player_a, player_b)`: recent versus season usage, from the pack's stats evidence
  - `slot_candidates(slot)`: eligible players from verified `eligible_slots`; this is a lookup, not an optimizer
- **Output validator** as described in 1.1(2), plus: `counterargument` and `contingency` must be non-empty for close calls.
- **Prompt-injection posture.** Evidence `value` text is passed as data, never as instructions. With no write
  or fetch tools, injected text cannot cause side effects; the most it can do is distort an assessment, and the
  human review plus citation checks are the controls for that.
- **Offline evaluation set.** At least 5 synthetic v2 packs covering:
  - bye week;
  - out/inactive;
  - unverified eligibility;
  - a close FLEX call;
  - DEF/K with team stats.

  Each pack has expected properties, not expected prose: which players must be assessed, which IDs may be
  cited, and whether the report is expected to be blocked. A mock model or a `FunctionModel` runs the set on every PR.

### P2.3 — Synthetic cloud dry run (GitHub Actions, no private data)

Addresses D11. This stage is deliberately **disposable**: it proves the cloud pipeline before any real data
or persistence exists.

- **A new workflow, `fantasy-assess.yml`, with `workflow_dispatch` only.** The only inputs are
  `fixture` (the name of a committed synthetic v2 fixture) and `provider` (`mock`/`gemini`). No roster or
  evidence JSON is ever accepted as an input.
- **Hard guard.** In P2.3 the job refuses to start unless the selected snapshot has `is_sample: true` and
  the request has `mode: test`. That holds until P2.5 replaces the guard.
- **One job, run in this order:**
  1. Check out Fantasy GM at the pinned SHA (with the read-only PAT).
  2. Run `pack` with fresh nflverse data and `as_of = now`.
  3. Run the agent.
  4. Run `report`.

  Outputs come from synthetic fixtures only, so uploading them as a 14-day artifact is acceptable *here*.
  The ledger is thrown away with the runner, which is the intended behavior for a dry run.
- **Timing guard.** If the pack's earliest kickoff is under 15 minutes away, fail before calling the model.
- **Workflow security:**
  - `permissions: contents: read`
  - `concurrency` (one run at a time)
  - `timeout-minutes: 15`
  - secrets exposed only to the steps that need them

### P2.4 — Supabase: protected input store, durable ledger, run history (revised proposal; you apply it)

Fixes D3, D4, D6, and closes the three gaps found in the second review: ledger continuity, private inputs, and
effective permissions.

**Tables** (all in `agent_lab`, all insert-only for the job role):

| Table | Purpose |
| --- | --- |
| `inputs` | Your private weekly inputs. Columns: `id uuid`, `kind` (`rows`/`request`/`official`), `bundle_id uuid`, `content jsonb`, `sha256`, `created_at` |
| `runs` | One row per run, including the P2.0 columns: `error_class`, `served_model`, `git_sha`, `config_hash` |
| `artifacts` | One row per output file. Columns: `run_id` (`on delete restrict`), `kind`, `sha256` (unique), `content jsonb` |
| `ledger_decisions` | A mirror of Fantasy GM's ledger. Columns: `decision_id text primary key`, `payload jsonb`, `payload_sha256`, `run_id`, `created_at` |

There are no `agents` or `decisions` tables, and no deletes or cascades anywhere.

**Private-input transfer (fixes review issue 2).**

- You add an input bundle yourself while signed in to the Supabase dashboard, in the Table Editor or SQL editor.
  That is your own authenticated session; no new UI is needed, and it works from a phone browser.
- The workflow's only inputs are the non-secret `bundle_id` and `provider`.
- The job reads the bundle through the restricted role. The roster and evidence never appear in workflow-run
  metadata, `env`, logs or artifacts.
- In real mode the job uploads **no private artifacts**. It writes the outputs to `agent_lab.artifacts`. The run log
  prints only `run_id`, the status, the blocker *categories* and the decision ID.
- The pack fetch must still happen within 5 minutes of the run (Fantasy GM's rule), so the bundle holds inputs,
  never a pack.

**Durable ledger with Fantasy GM's guarantees (fixes review issue 1).** Fantasy GM's `persist()` is left
untouched. Agent Lab wraps the SQLite file in four steps:

1. **Hydrate.** Before `report`, create a fresh `decisions.sqlite` with Fantasy GM's table definition and load
   every `agent_lab.ledger_decisions` row into it. Fantasy GM's immutability check, "identical retry is
   idempotent, conflicting rewrite fails", then runs against the full history, exactly as it does locally.
2. **Report.** Run Fantasy GM's `report` unchanged. Its own guards apply: blocked results, cutoff, six-hour pack
   age, kickoff.
3. **Sync.** For each SQLite row not yet in Supabase:
   - insert it with `INSERT ... ON CONFLICT (decision_id) DO NOTHING`;
   - read it back and compare it to the local payload, ignoring `created_at`, the same rule Fantasy GM uses;
   - on a mismatch, fail with `ledger_conflict`.

   Postgres's primary key and `concurrency: agent-run` together prevent two runs from writing different payloads under one key.
4. **Recover.**
   - If sync fails after `report` succeeded, the run is marked `persist_failed`, the job exits nonzero, and
     **the decision is not reported as recorded**. That matches Fantasy GM's rule "print a decision ID only after commit".
   - `agent-lab ledger sync --from <sqlite>` retries the upload idempotently. The SQLite file is
     kept in the runner only until the job ends, so automatic recovery is a retry of the whole job. That is safe,
     because the same pack yields the same `decision_id`. A changed pack yields a new decision, which is correct.
   - `agent-lab ledger export --to <sqlite>` rebuilds a complete local ledger from Supabase, so you can run Fantasy GM
     locally against the same history at any time.

**Effective-permission isolation (fixes review issue 4).**

- **The role is restricted at creation.** `agent_lab_writer` is created `NOLOGIN NOSUPERUSER NOCREATEDB
  NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT` with `CONNECTION LIMIT 2`. It has role-level
  `statement_timeout` and `idle_in_transaction_session_timeout` limits, and you set its password and turn on login yourself.
- **Explicit grants don't make it isolated.** The role still inherits whatever Postgres's `PUBLIC` pseudo-role can
  do: default `EXECUTE` on functions, possibly `USAGE` on `public`, and any RLS policy written `TO public`. Changing
  `PUBLIC` grants on other schemas would affect unrelated applications and are out of scope.
- **The fix is to measure, not assume.** A read-only `agent-lab db audit-privileges` command connects *as the
  role* to the **live** project. It lists `has_schema_privilege`, `has_table_privilege`,
  `has_sequence_privilege` and `has_function_privilege` for every object outside `agent_lab`, and runs a
  `SELECT count(*)` on every table it can reach (Postgres applies RLS, so this returns the rows the role could actually read).
- **Pass rule:**
  - no table outside `agent_lab` returns any rows;
  - the role has no write privilege outside `agent_lab`;
  - every executable function outside `agent_lab` is on a reviewed allowlist (for example, Supabase's built-in extension functions).
- **The credential stays unused until the audit passes.** The job runs the audit before every real-mode run and
  refuses to continue if it fails. The audit's report is shown to you before the secret is first stored.

**Testing before any target deployment.** The migration is tested in CI against `postgres:16` with stub
Supabase roles. That includes a fixture schema with `PUBLIC` grants and a `TO public` RLS policy, to prove the
audit catches inheritance. The live audit is a separate step that needs your authorization.

### P2.5 — First real execution

Runs only after P2.4's acceptance criteria pass and you have made the D12 decision.

- Replace the P2.3 guard with: real mode requires `bundle_id`, a passing privilege audit, and a successful hydrate.
- **Run order:**
  1. Audit.
  2. Read the bundle.
  3. Run `pack`.
  4. Strip `league_id` and `team_id` from the model context.
  5. Run the agent.
  6. Hydrate the ledger.
  7. Run `report`.
  8. Sync the ledger and artifacts.
  9. Print only a summary.

### P2.6 — Shadow evaluation and go/no-go

- For two real weeks, run the cloud assessor *and* your normal Codex step on the same pack. Record both
  assessment hashes and your judgment of which is more useful.
- **Go** (the agent becomes the default first draft) only if both hold:
  - every real-week output passes Fantasy GM's evaluator, apart from blockers caused by missing human verification;
  - you judge it useful in at least one of the two weeks.

  Otherwise, keep Codex as the path and keep the agent as lab infrastructure. Outcome-based scoring needs
  Fantasy GM's P2.4 outcome records, which don't exist yet.

---

## 3. End-to-end acceptance criteria

Each criterion can be checked with a command, a CI job or an artifact.

| # | Criterion | How it is verified |
| --- | --- | --- |
| A1 | Any exception inside a run yields a `RunRecord` with `status=failed` and an `error_class` | Tests inject `OSError` in a tool, `httpx.ConnectError` in the model, a fake 429, a missing key and a usage cap. Each one produces a record and the documented exit code |
| A2 | A transient 429 is retried at most 3 times, honoring `Retry-After`, within the deadline, and never after a tool has run | `FunctionModel` test with a scripted 429, 429, then success, checking attempt count and timing with a fake clock |
| A3 | Builds are reproducible | CI installs from the lockfile with `--require-hashes`. Actions are pinned by SHA (checked by a lint step). The k3d checksum is verified |
| A4 | Agent output is valid in Fantasy GM's format, as judged by Fantasy GM's own code | CI job: check out Fantasy GM at the pinned SHA, run the agent (mock) on each synthetic v2 pack, then run `python3 -m src.fantasy_gm report`. Exit 0, and `evaluation.json` blockers equal the expected set for every pack |
| A5 | The agent cannot cite evidence outside a player's pack | Test: a scripted model cites a foreign `evidence_id`, and the validator forces a retry. On persistent violation the run fails with `error_class=validation` |
| A6 | Unverified facts block, never pass | A synthetic pack with `eligibility.verified=false` produces `BLOCKED` from Fantasy GM's report, a `gaps.json` entry naming eligibility, and no ledger write |
| A7 | The synthetic cloud dry run works end to end with fresh data and no laptop | Manual `fantasy-assess` dispatch with `fixture=<name>`. The artifact contains a pack with `as_of` within 5 minutes of the run, nflverse `fetched_at` inside the run window, assessments, and a report. A dispatch whose fixture is not `is_sample: true` is refused before any fetch |
| A8a | Gemini failures are handled | A recorded or injected 429 and a missing key each produce a classified `RunRecord` (`transient`/`config`), with retries logged. **This is a failure-handling test, not an integration pass** |
| A8b | The real Gemini integration works (the demo agent in P2.0; the assessor in P2.3 on a synthetic fixture) | A live `provider=gemini` run with `status=succeeded`, at least one **actual tool call** in `tool_calls`, output that validated against the agent's schema, and the served model recorded. A quota error doesn't count: run again later. The criterion is unmet until a success exists |
| A9 | League and team identifiers never reach the model | Test that captures every model request: `league_id` and `team_id` appear in none of them |
| A10 | Supabase migration is safe in CI | CI applies the migration to `postgres:16` with stub roles and asserts: `anon` and `authenticated` are denied; `agent_lab_writer` can insert into and select from `inputs`, `runs`, `artifacts` and `ledger_decisions` but cannot update or delete them; deleting a run that has artifacts fails; down then up succeeds; a catalog diff before and after shows changes only inside `agent_lab` plus the new role. A fixture schema with `PUBLIC` grants and a `TO public` RLS policy must make `audit-privileges` **fail** |
| A11 | Persistence failure is visible and never claims a decision | Test with an unreachable database: the run is recorded `persist_failed` in JSONL, the process exits nonzero, and no "Decision saved" line is printed |
| A13 | The ledger keeps its continuity and idempotency across runs | CI with `postgres:16`. Run 1 records decision K. Run 2, on a fresh runner directory with the same pack, hydrates, `report` reports the same K, and the row count stays at 1. Run 3, with a conflicting payload under K, fails with Fantasy GM's `immutable decision conflict`. `ledger export` followed by local `fantasy_gm report` reproduces the same K. A sync that is killed and then rerun leaves exactly one row |
| A14 | Private data never lands in workflow metadata, logs or artifacts | A real-mode CI run with a synthetic bundle stored in Postgres and marked by unique canary strings. Using the GitHub API, the job's dispatch inputs, log archive and artifact list contain none of the canaries, and there are no artifacts in real mode |
| A15 | Effective permissions on the live project are verified before first use | `agent-lab db audit-privileges` passes against the live project as `agent_lab_writer`: zero readable rows and no write privileges outside `agent_lab`, and every executable function is allowlisted. You review the report before storing `AGENT_LAB_DATABASE_URL`. Real-mode runs repeat the audit and abort if it fails |
| A12 | Boundaries are respected | `git diff` across the Phase 2 PRs touches Fantasy GM only in the single approved schema-export PR. The Supabase dashboard shows no changes outside `agent_lab` and `agent_lab_writer`. The Actions bill shows no paid usage |

---

## 4. Optional learning labs (after P2.5, each on the real job)

| Lab | What you learn | Prerequisite | Cost |
| --- | --- | --- | --- |
| L1: K3s on a device you already own | Running the `fantasy-assess` job as a K8s Job; GHCR pulls with an `imagePullSecret` (this is when GHCR earns its place) | An always-on machine. Without one, use k3d on demand | Free |
| L2: NetworkPolicy | Default-deny egress plus DNS plus 443, and observing what breaks. Optionally Cilium `toFQDNs` to really pin the egress to Gemini, nflverse and the Supabase pooler | L1 or k3d | Free |
| L3: Secrets | Sealed Secrets or SOPS for `GEMINI_API_KEY` and `AGENT_LAB_DATABASE_URL` in Git | L1 | Free |
| L4: Kustomize overlays | `k3d-ci` versus a `home` overlay (image source, schedule, resources). Only once L1 creates a second environment | L1 | Free |

Not planned: Helm, database-backed configuration, a dashboard, and additional agents.

---

## 5. Decisions needed from you

1. **D12:** approve or decline Gemini for Fantasy GM assessments, given Fantasy GM's Codex policy and the free-tier data-use terms. If you decline, P2.3 runs with mock only, and P2.5–P2.6 don't happen.
2. **P2.1:** approve the small Fantasy GM schema-export change, or choose the hand-written-schema fallback.
3. **PAT:** create a fine-grained, read-only PAT for `bpronin90/fantasy-gm` for CI and the cloud job. It is free; expiry and rotation are your choice.
4. **P2.4:** whether, and when, to apply the revised `agent_lab` migration to an owner-selected Supabase project, and to authorize the live privilege audit (A15).
