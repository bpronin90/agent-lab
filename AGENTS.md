# AGENTS.md

Instructions for every coding agent working in this repository: Codex, Claude Code,
Copilot, Gemini, Cursor, and local, cloud, or remote sessions alike.

`CONTRIBUTING.md` is binding for agents. Read it before changing anything.

The owner keeps a fuller agent protocol outside this repository. On the owner's
machine it is restored as the Git-ignored `AGENTS.override.md`, which Codex loads in
place of this file and Claude Code imports through `CLAUDE.md`. It only adds detail
and never relaxes these rules. If `AGENTS.override.md` is absent, as in a fresh clone
or a cloud checkout, then `CONTRIBUTING.md` and this file are the whole protocol, and
anything they do not permit is not permitted.

Rules that hold in every environment:

- Before any implementation branch, edit, or worktree, require owner START for
  the issue: the owner's own message `implement #<n>` (implement through
  independent review; no merge) or `carry #<n>` (implement, then merge and close
  under the merge conditions). The agent records it on the issue as a comment
  relayed at owner request. An existing issue, "finish this up", and
  "keep going" are not START. Issue-free maintenance requires an explicit owner
  START naming that task; read-only analysis requires no START.
- Work on a branch, never on the default branch. Never commit or push to the default
  branch. Never force-push a shared branch unless the owner explicitly directs it.
- Never reset, clean, stash away, or overwrite uncommitted work you did not create. If
  the checkout is dirty, work in a separate worktree.
- Stage explicit paths only (no `git add -A`, `git add .`, or `git commit -a`).
- Every PR body contains exactly one standalone `Issue: #<n>` or `Issue: none` line
  and the contract headings from `CONTRIBUTING.md`. Change only the Allowed Files.
  Before work outside them, require an owner issue comment `APPROVED` naming the
  added paths; an agent's proposed amendment is not approval. For issue-free work,
  require the same owner approval on its contract. Include necessary doc changes
  in the approved scope. Run `bash scripts/check-scope.sh --allowed <path> ...`
  before handoff using that scope.
- Merge a PR only under the merge conditions in `CONTRIBUTING.md`. Never close issues
  (other than through a merged PR's `Closes #n`), deploy, release, change repository
  settings or visibility, touch credentials or secrets, or cause external side effects
  without the applicable owner authorization in `CONTRIBUTING.md`.
  An ambiguous "looks good" is not authorization.
- Live model calls, and any external side effect outside an owner-started task, require
  the OWNER AUTHORIZATION RECORD in `CONTRIBUTING.md`. Announce each live batch before
  running it and stay within its cap, including failed calls and retries. START alone
  is not this record.
- Post Codex-drafted text only when the owner asked for that text to be posted;
  review relays additionally follow the review rules below.
- Send email only with per-send written owner authorization naming the recipient
  and exact files. Never put the owner's private contact details in issues, PRs,
  commits, or tracked files.
- A new model provider or paid-quota source needs an issue contract reconciling it
  with "Zero incremental cost"; an existing subscription is not authorization.
- Do not review or approve a head you produced.
- If an independent reviewer cannot post to GitHub, the owner, or an implementer the
  owner authorized, relays its complete review verbatim as described in `CONTRIBUTING.md`.
  A relay is transport only: it does not make the poster the reviewer or authorize merge.
- Report exactly which checks ran. Never present simulated, skipped, or unexecuted
  validation as proven.
- When a rule is unclear or a needed step needs owner authority, stop and ask.
