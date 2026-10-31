"""Command line entry point: `agent-lab run "prompt"` or `python -m agent_lab`."""

from __future__ import annotations

import argparse
import json
import sys

from agent_lab.agents import REGISTRY
from agent_lab.config import Settings
from agent_lab.runtime import run_agent, save_record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-lab")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Run an agent once and print the JSON run record.")
    run.add_argument("prompt")
    run.add_argument("--agent", default="demo", choices=sorted(REGISTRY))
    run.add_argument("--no-save", action="store_true", help="Do not append to the local run log.")
    calm = sub.add_parser("calm-down", help="Research a fear, verify citations, render a one-off email (never sent).")
    calm.add_argument("fear")
    calm.add_argument("--out", default="calm-down-out", help="Directory for email.html, email.txt and the dossier.")
    calm.add_argument("--no-save", action="store_true", help="Do not append to the local run log.")
    sub.add_parser("healthcheck", help="Validate settings without calling any model.")
    args = parser.parse_args(argv)

    try:
        settings = Settings.from_env()
    except ValueError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.command == "healthcheck":
        print(json.dumps({"status": "ok", "provider": settings.provider, "agents": sorted(REGISTRY)}))
        return 0

    if args.command == "calm-down":
        return _calm_down(args, settings)

    try:
        record = run_agent(args.prompt, settings, args.agent)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not args.no_save:
        save_record(record, settings.run_log_dir)
    print(record.model_dump_json(indent=2))
    return exit_code(record)


def _calm_down(args, settings: Settings) -> int:
    from pathlib import Path

    from agent_lab.agents.calm_down.pipeline import research_settings, run_calm_down
    from agent_lab.agents.calm_down.retrieval import mode_from_env

    try:
        mode = mode_from_env()
        result = run_calm_down(args.fear, research_settings(settings), mode, Path(args.out))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not args.no_save:
        save_record(result.record, settings.run_log_dir)
    summary = {"run_id": result.record.run_id, "run_status": result.record.status, "retrieval": mode,
               "research_status": result.report.status if result.report else None,
               "problems": result.report.problems if result.report else [],
               "files": {k: str(v) for k, v in result.files.items()}}
    print(json.dumps(summary, indent=2))
    if result.record.status != "succeeded":
        return exit_code(result.record)
    return 0 if result.ok else 4


EXIT_CODES = {"config": 2, "transient": 3}


def exit_code(record) -> int:
    """0 success, 2 configuration, 3 transient (safe to retry later), 1 anything else."""
    if record.status == "succeeded":
        return 0
    return EXIT_CODES.get(record.error_class, 1)


if __name__ == "__main__":
    raise SystemExit(main())
