---
name: implement
description: Use when the owner says "implement #N". Implements issue N within its Allowed Files through independent Codex review of the exact head. Does not merge.
---

# implement #N

The owner's own `implement #N` message is START for issue #N. "finish this up" and
"keep going" are not. `AGENTS.md` and `CONTRIBUTING.md` stay binding.

1. Read the issue contract (Goal, Allowed Files, Acceptance Criteria, Verification).
   Stop and ask if it is missing or unclear.
2. Record START on the issue: a short comment quoting the owner's words, marked
   `Relayed at owner request`.
3. Branch `issue/<N>-<scope>` off `origin/main` (separate worktree if the checkout
   is dirty). Edit only Allowed Files; work outside them needs an owner `APPROVED`
   comment naming the paths.
4. Run targeted offline tests (and schema export after contract changes). No live
   model calls without an OWNER AUTHORIZATION RECORD.
5. Stage explicit paths, commit, run `bash scripts/check-scope.sh --allowed <path> ...`,
   push, and open a PR against `main` with exactly one `Issue: #N` line, the contract
   headings, and `Closes #N`.
6. Post `UPDATE=IMPLEMENTED` on the issue with the full head SHA and exact checks run.
7. Run `scripts/codex-review.sh <PR> <head-sha>` and relay the review verbatim per
   `CONTRIBUTING.md` (`Relayed review from: <reviewer>`, head SHA confirmed unchanged).
   Never self-review or use a Claude subagent as reviewer.
8. On `VERDICT=FEEDBACK`, check each finding against code and contract, fix in scope
   with new commits, post `UPDATE=FEEDBACK_ADDRESSED`, and review the new head.
9. Stop at `VERDICT=APPROVED` (or a blocker) and report. Do not merge without the
   owner's separate go-ahead.
