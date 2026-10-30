# `agent_lab` Supabase schema — proposal (not applied)

Files:
- `supabase/proposed_migrations/20261030000000_agent_lab_schema.sql`
- its `.down.sql` rollback

They live in `proposed_migrations/`, not `migrations/`, so no Supabase CLI command will apply them.

## Constraints honored
- A new `agent_lab` schema only. No existing application or platform schema is
  altered, referenced or granted on. Deployment-specific database relationships
  belong in private configuration.
- The schema is not added to the Data API's exposed schemas. RLS is enabled with **no policies**,
  and `anon`/`authenticated` have no privileges, so only `service_role` can access it.

## Tables
| Table | Purpose | Key columns |
| --- | --- | --- |
| `agents` | Versioned agent configuration | `name`, `version` (unique together), `provider`, `model`, `instructions`, `max_requests`, `max_tool_calls`, `enabled` (default **false**), `config jsonb` |
| `runs` | Run history; mirrors `RunRecord` | `id` (the run_id), `agent_id` → agents, `trigger`, `status`, `prompt`, `output jsonb`, `error`, `tool_calls jsonb`, token and request counts, timestamps |
| `decisions` | Advisory outputs and human review | `run_id` → runs, `domain` (e.g. `fantasy_gm`), `contract_version`, `payload jsonb`, `human_status` (pending/accepted/rejected), `human_note`, `reviewed_at` |

Design choices:
- `jsonb` for outputs and payloads, so new agents need no migrations.
- `CHECK` constraints instead of enums, because they are easier to evolve.
- Indexes only for "latest runs or decisions per agent or domain".

## Validation performed
Checked against a throwaway local PostgreSQL 16, with stub `anon`, `authenticated` and `service_role` roles:
- The migration applies cleanly.
- `anon` gets `permission denied for schema agent_lab`.
- down + re-apply succeeds.

It has **not** been run against Supabase.

## How you would apply it later (Phase 2, your decision)
1. Review the SQL, then test it on a Supabase branch or a local `supabase start`, not production.
2. Move the file to that project's `supabase/migrations/` and apply it through your normal process.
3. Store the service-role key only as a GitHub Actions secret.
