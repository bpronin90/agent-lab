#!/usr/bin/env python3
"""PreToolUse guard for Claude Code Bash calls. Exit 2 blocks the call and shows
stderr to Claude. Enforces AGENTS.md rules that are otherwise instruction-only:
  - no broad staging (git add -A/--all/./-u, git commit -a)
  - no commits on, or pushes to, the default branch
  - no force push, hard reset, git clean, or whole-tree checkout/restore
    (protects existing uncommitted work)
  - no `gh pr merge --admin` (bypasses checks; owner-only)
This is a guardrail against agent mistakes, not a security boundary: it does not
see commands run by other tools, scripts, or other agents.
"""
import json, os, re, shlex, subprocess, sys

data = json.load(sys.stdin)
cmd = (data.get("tool_input") or {}).get("command", "") or ""
cwd = data.get("cwd") or os.getcwd()


def block(msg):
    print(f"BLOCKED by .claude/hooks/guard-bash.py: {msg} (AGENTS.md)", file=sys.stderr)
    sys.exit(2)


def git(workdir, *args):
    r = subprocess.run(["git", "-C", workdir, *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def default_branch(workdir):
    ref = git(workdir, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if ref:
        return ref.split("/", 1)[1]
    for name in ("main", "master"):
        if git(workdir, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{name}"):
            return name
    return "main"


def tokens(segment):
    try:
        toks = shlex.split(segment)
    except ValueError:
        toks = segment.split()
    while toks and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]):
        toks.pop(0)  # leading VAR=value assignments
    return toks


def short_flags(args):
    return "".join(a[1:] for a in args if re.fullmatch(r"-[A-Za-z]+", a))


def check_git(args, workdir):
    # Global options before the subcommand: -C <dir>, -C<dir>, -c <k=v>, -c<k=v>,
    # and other flag-style options (e.g. --no-pager, --git-dir=<path>).
    while args and args[0].startswith("-"):
        opt = args[0]
        if opt in ("-C", "-c"):
            if opt == "-C" and len(args) > 1:
                workdir = os.path.join(workdir, os.path.expanduser(args[1]))
            args = args[2:]
            continue
        if opt.startswith("-C"):
            workdir = os.path.join(workdir, os.path.expanduser(opt[2:]))
        args = args[1:]
    if not args:
        return
    sub, rest = args[0], args[1:]
    main = default_branch(workdir)
    current = git(workdir, "rev-parse", "--abbrev-ref", "HEAD")

    if sub == "add":
        broad = {"-A", "--all", ".", "./", ":/", "*", "-u", "--update", "--no-ignore-removal"}
        if broad & set(rest) or re.search(r"[Au]", short_flags(rest)):
            block("broad staging is forbidden; stage explicit paths only")
    elif sub == "commit":
        if "--all" in rest or "a" in short_flags(rest):
            block("`git commit -a` is broad staging; stage explicit paths only")
        if current == main:
            block(f"commits on the default branch `{main}` are forbidden; create a branch first")
    elif sub == "push":
        if "f" in short_flags(rest) or any(a == "--force" or a.startswith("--force=") for a in rest):
            block("force push is forbidden (use --force-with-lease on your own branch only)")
        if "--all" in rest or "--mirror" in rest:
            block("pushing all branches is forbidden; push your branch explicitly")
        positional = [a for a in rest if not a.startswith("-")]
        refspecs = positional[1:]
        if any(r.startswith("+") for r in refspecs):
            block("forced refspecs are forbidden")
        dests = [r.split(":")[-1].removeprefix("refs/heads/") for r in refspecs] or [current]
        if main in (current if d == "HEAD" else d for d in dests):
            block(f"pushing to the default branch `{main}` is forbidden; open a PR")
    elif sub == "reset" and "--hard" in rest:
        block("`git reset --hard` can discard uncommitted work")
    elif sub == "clean" and ("f" in short_flags(rest) or "--force" in rest):
        block("`git clean -f` can delete untracked work")
    elif sub in ("checkout", "restore") and ("." in rest or ":/" in rest):
        block(f"`git {sub} .` discards uncommitted work across the tree; name explicit paths")


def is_admin_merge(toks):
    """`gh ... pr merge ... --admin`, allowing `env`, global flags, and a path to gh."""
    if toks and toks[0] == "env":
        toks = toks[1:]
        while toks and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=|^-", toks[0]):
            toks = toks[1:]  # env's own options and VAR=value arguments
    if not toks or os.path.basename(toks[0]) != "gh":
        return False
    rest = toks[1:]
    if "pr" not in rest or "merge" not in rest[rest.index("pr") + 1:]:
        return False
    return any(t == "--admin" or t.startswith("--admin=") for t in rest)


workdir = cwd
for seg in re.split(r"&&|\|\||;|\||\n", cmd):
    toks = tokens(seg.strip())
    if not toks:
        continue
    if toks[0] == "cd" and len(toks) > 1:
        workdir = os.path.join(workdir, os.path.expanduser(toks[1]))
    elif toks[0] == "git":
        check_git(toks[1:], workdir)
    elif is_admin_merge(toks):
        block("`gh pr merge --admin` bypasses checks and is owner-only; merge without it once the merge conditions hold")

sys.exit(0)
