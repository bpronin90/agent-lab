# Contributing

Thanks for contributing to `bpronin90/agent-lab`. This guide covers the contributor-facing process:
how work is tracked, how changes are scoped and reviewed, and what must be verified
before a change lands.

## Issue And PR Workflow

| Work | Contract | Branch |
| --- | --- | --- |
| Substantive change: behavior, interfaces, data, security, CI, dependencies | GitHub issue with **Goal, Allowed Files, Acceptance Criteria, Verification** | `issue/<n>-<scope>` |
| Doc correction or small maintenance with no behavior change | PR body with `Issue: none` and the same four headings (one line each is fine) | `chore/<scope>` or `docs/<scope>` |

If you are unsure whether work is substantive, treat it as substantive.

- Before any implementation branch, edit, or worktree, require an owner issue
  comment `START` naming the task. Issue existence, "finish this up", and
  "keep going" are not START. For issue-free maintenance, require an explicit
  owner START naming that task. Read-only analysis requires no START.
- Branch off `main` and open the pull request against `main`. Never commit or
  push to `main` directly, and never force-push a shared branch.
- One issue has one PR. Every non-Dependabot PR body contains exactly one standalone
  line `Issue: #<n>` or `Issue: none`.
- Change only the contract's Allowed Files, including necessary doc changes.
  Before work outside them, require an owner issue comment `APPROVED` naming the
  added paths (or owner approval on the contract for issue-free work).
  An implementer's amendment or edited issue body is not owner approval.
  Run `bash scripts/check-scope.sh --allowed <path> ...` using the approved scope;
  the script checks paths, not authorization.
- No adjacent refactors, cleanup, renames, dependency upgrades, or redesigns. Never
  silently change requirements; raise the conflict in the issue instead.
- Stage explicit paths only (no `git add -A`, `git add .`, or `git commit -a`), and keep
  commits focused.
- In the PR, summarize what changed, why, and exactly how it was verified.

## Testing And Verification

- Install from the hash-locked files as described in `README.md`; never install unhashed.
- Targeted tests while working: `pytest tests/<file>.py` (offline, mock provider, no credentials).
- After changing `src/agent_lab/contracts/`:
  `python scripts/export_contract_schemas.py && git diff --exit-code contracts/`.
- After changing dependencies, regenerate the hash locks as described in `README.md`.
- CI runs `ci.yml` on every PR (tests, schema drift, SHA-pinned actions, offline
  container) and `k3d-smoke.yml` on runtime/k8s paths.
- Live model calls consume quota, including free-tier and subscription quota.
  They are never routine verification and require the authorization record below.
- Test failure modes, not only happy paths.
- Report exactly which checks ran and which did not. If you cannot run a check that
  would normally apply, say so in the PR and explain why. Never describe unexecuted
  integration, deployment, or live/native validation as proven.

## Review And Merge

- Substantive changes need an independent review of the exact PR head before merge;
  the author of a head cannot approve it. For issue-free doc or maintenance PRs, review
  is at the owner's discretion.
- Review findings cite `file:line` and a concrete failing input or state, and separate
  blocking defects from optional suggestions. A new commit makes earlier reviews stale.
- A reviewer that cannot post (for example a read-only, network-isolated Codex run) has its
  complete review relayed verbatim, where the handoff was posted, by the owner or an
  implementer the owner authorized. Prepend `Relayed review from: <reviewer>`, keep the
  reviewer's findings, stated limits, and exactly one `VERDICT`/`PR`/`Commit` (full SHA)/
  `Findings` record, and confirm the PR head still equals the reviewed SHA before posting
  and before merging. A missing, incomplete, or stale review does not satisfy the merge
  condition. Never edit or delete review records; supersede them. A relay is transport
  only: it does not make the poster the reviewer or authorize merge. Run
  `scripts/codex-review.sh <PR> <head-sha>` to produce a Codex review of an exact head;
  it never posts or merges.
- An agent may merge a PR only when CI passes on the exact head; for substantive changes,
  an independent reviewer who is not the head's author has issued `VERDICT=APPROVED` for
  that exact head, posted directly or relayed as described below, with no unresolved
  blocking findings; and the PR does not deploy,
  release, migrate data, touch credentials, secrets, or repository settings, or cause
  external side effects. Green CI alone is not permission to merge.
- Only the owner merges Dependabot PRs, merges with `--admin`, or closes issues other
  than through a merged PR's `Closes #n`.
- Only the owner authorizes deploys and releases; production data, migrations, and
  schedules; credentials, secrets, and access; repository settings and branch
  protection; new paid services; and external side effects, including live model calls, any Supabase write, enabling the `agent-run.yml` schedule, and any write to another repository. Ambiguous
  approval such as "looks good" is not authorization.

## Owner Authorization

Before live model calls, sending messages, or any other external side effect outside an
owner-started task (issue, PR, handoff, and review comments inside a started task need
no record), require this comment on the issue or PR. The owner posts it, or explicitly
asks an agent to relay their authorization. Recording permission does not grant it.

```text
OWNER AUTHORIZATION RECORD
Owner words + date: <authorization and date; omit private contact details>
Provider/model: <provider and model, or service and N/A>
Task: <issue/task and exact permitted operation>
Cap on calls/runs: <numeric limits; include external operations where applicable>
Quota/spend limit: <quota ceiling and maximum incremental spend>
Expiry/stop: <expiry and stop conditions, whichever occurs first>
Local-manual-only: yes
No sending/schedules: yes
```

Missing or ambiguous fields mean stop and ask. "Keep running" does not supply
a cap. Announce each live batch before it runs, naming the record, provider/model,
planned maximum calls/runs, and remaining allowance. Count attempted calls,
including retries and failed runs; failure does not extend a cap. Bound internal
retries and fallback within the remaining allowance or do not run the batch.
Stop at any cap, quota/spend limit, expiry, or stop condition. Further activity
requires fresh owner authorization and a new record.

The record permits only its named operation; it does not waive START, scope,
review, merge, or other owner-only restrictions. Its local manual authorization
does not enable schedules or authorize sending messages. Those require separate
explicit owner authorization. For each email send, require written authorization
naming the recipient and exact files to send, plus the record; for that send only,
replace `No sending/schedules: yes` with `No schedules; email send authorized
separately: <private authorization reference>`. Keep the recipient and private
contact details out of public records.

An implementer may post Codex-drafted text only when the owner asked for that
text to be posted. Drafting is not posting authority. Independent review relays
must also satisfy the review rules above.

## Project Rules

- **Zero incremental cost.** Gemini free tier, the offline `mock` provider by default,
  and free GitHub Actions usage. No new paid service. Free-tier prompts may be used by
  Google, so never send private data to Gemini.
- A new model provider or paid-quota source needs an issue contract explaining
  its quota source, incremental cost, limits, and reconciliation with "Zero
  incremental cost" before implementation or use. This includes subscription-backed
  CLIs such as `claude -p`; an existing subscription does not establish permission
  or zero incremental cost. Any policy change needs explicit owner approval.
- **Runtime guarantees** (`README.md`, "Failure handling") are contracts: one usage
  budget per run across retries and fallback; no retry or fallback after a tool has
  executed; `Retry-After` honored in full; bounded deadlines; every run, including
  crashes, recorded with `error_class`, `git_sha`, and `config_hash`. Changing any of
  them is substantive and needs failure-path tests.
- **Tools.** A tool with network, filesystem, shell, or write side effects needs an
  issue that names its trust boundary and its failure tests. Treat tool output and
  external text as untrusted.
- **Supabase.** Agent Lab may own only the `agent_lab` schema. Its SQL stays in
  `supabase/proposed_migrations/` until the owner applies it.
- **Credentials.** `GEMINI_API_KEY` comes from the environment or a GitHub secret only.
  It is never logged, stored in run records, or committed.
- **Supply chain.** Install from hash-locked files; pin actions to full commit SHAs.
  Dependabot PRs use the PR itself as their contract and still need green CI and an
  owner merge.
- Agent Lab does not own any other repository. Work in another repository follows that
  repository's own contributing rules and needs its own authorization.

## Public source and private configuration

The tracked `AGENTS.md`, `CLAUDE.md`, guard hook, and this guide are the complete
contributor baseline for a fresh checkout. Private agent overrides are optional;
contributors do not need the maintainer's environment or operating protocols.
Keep personal settings, service identifiers, infrastructure relationships, real
data, and full private protocols in ignored local files or a separate private
backup. Review staged content: ignore rules do not remove sensitive text inside
tracked files or earlier commits.

Use a GitHub noreply email for commits intended for publication. Keep review and
handoff examples free of personal paths and instance identifiers.

For a solo maintainer, branch protection should require PRs and passing CI with
zero required GitHub approving reviews; an author cannot approve their own PR.
This does not waive the independent agent-review and exact-head handoff rules
above. The owner may review and merge their own PR after the applicable checks;
agents must still follow the agent merge conditions. Repository protection and
visibility changes require separate owner approval.

Never put the owner's private contact details in issues, PRs, commits, or tracked
files. Public authorization records must omit them and reference private written
authorization when needed.
