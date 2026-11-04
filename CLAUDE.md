@AGENTS.md
@CONTRIBUTING.md
@AGENTS.override.md

# CLAUDE.md

`AGENTS.md` and `CONTRIBUTING.md` bind Claude here. `AGENTS.override.md` and
`CLAUDE.local.md` hold the owner's fuller private protocol. They are Git-ignored,
present only on the owner's machine, and loaded automatically when present.

- Require owner START (`implement #<n>` or `carry #<n>`, see the
  `.claude/skills/` of the same names) before an implementation branch, edit, or worktree, and
  owner `APPROVED` before expanding Allowed Files. Follow `CONTRIBUTING.md` for
  authorization records, capped live batches, Codex-draft posting, new providers,
  and per-send email permission. Private notes cannot relax these requirements.
- `.claude/settings.json` runs `.claude/hooks/guard-bash.py` before every Bash command.
  It blocks broad staging, commits and pushes to the default branch, force pushes,
  `reset --hard`, `clean -f`, and `gh pr merge --admin`. If it blocks a command, fix the
  command. Never route around the hook or disable it.
- Do not review or approve a head you produced, and do not use a Claude subagent as
  the independent reviewer.
