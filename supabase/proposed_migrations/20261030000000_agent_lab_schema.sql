-- PROPOSAL ONLY - DO NOT APPLY YET.
-- Lives in supabase/proposed_migrations/ (not migrations/) so `supabase db push`
-- cannot pick it up by accident. Review docs/supabase-schema.md first.
--
-- Creates a new, isolated `agent_lab` schema. It does not reference, alter, or
-- grant anything on existing schemas (public, kilo, anime, auth, storage).
-- Access is server-side only: RLS is enabled with no policies and anon /
-- authenticated get no privileges, so only the service_role (used by a backend
-- job, never a browser) can read or write. The schema is not added to the
-- Data API's exposed schemas.

begin;

create schema if not exists agent_lab;
revoke all on schema agent_lab from public, anon, authenticated;
grant usage on schema agent_lab to service_role;

-- Agent configuration: one row per agent version.
create table agent_lab.agents (
    id            uuid primary key default gen_random_uuid(),
    name          text not null check (name ~ '^[a-z][a-z0-9_-]{1,62}$'),
    version       integer not null default 1 check (version >= 1),
    provider      text not null check (provider in ('mock', 'gemini')),
    model         text not null,
    instructions  text not null,
    max_requests  integer not null default 6 check (max_requests between 1 and 50),
    max_tool_calls integer not null default 8 check (max_tool_calls between 0 and 100),
    enabled       boolean not null default false,
    config        jsonb not null default '{}'::jsonb,
    created_at    timestamptz not null default now(),
    unique (name, version)
);

-- Run history: mirrors agent_lab.schemas.RunRecord.
create table agent_lab.runs (
    id            uuid primary key,
    agent_id      uuid references agent_lab.agents (id) on delete set null,
    agent_name    text not null,
    provider      text not null,
    model         text not null,
    trigger       text not null default 'manual' check (trigger in ('manual', 'schedule', 'cli', 'k8s')),
    status        text not null check (status in ('succeeded', 'failed')),
    prompt        text not null check (length(prompt) <= 4000),
    output        jsonb,
    error         text,
    tool_calls    jsonb not null default '[]'::jsonb,
    requests      integer not null default 0,
    input_tokens  integer not null default 0,
    output_tokens integer not null default 0,
    started_at    timestamptz not null,
    finished_at   timestamptz not null,
    check (finished_at >= started_at)
);
create index runs_agent_started_idx on agent_lab.runs (agent_name, started_at desc);

-- Decisions: advisory outputs (e.g. Fantasy GM AdvisoryDecision) and whether a
-- human accepted them. Nothing in Agent Lab acts on a decision automatically.
create table agent_lab.decisions (
    id            uuid primary key default gen_random_uuid(),
    run_id        uuid not null references agent_lab.runs (id) on delete cascade,
    domain        text not null check (domain ~ '^[a-z][a-z0-9_]{1,40}$'),  -- e.g. 'fantasy_gm'
    contract_version text not null,
    payload       jsonb not null,
    human_status  text not null default 'pending' check (human_status in ('pending', 'accepted', 'rejected')),
    human_note    text,
    created_at    timestamptz not null default now(),
    reviewed_at   timestamptz
);
create index decisions_domain_created_idx on agent_lab.decisions (domain, created_at desc);

alter table agent_lab.agents    enable row level security;
alter table agent_lab.runs      enable row level security;
alter table agent_lab.decisions enable row level security;

revoke all on all tables in schema agent_lab from public, anon, authenticated;
grant select, insert, update, delete on all tables in schema agent_lab to service_role;

commit;
