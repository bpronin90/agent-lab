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

    try:
        record = run_agent(args.prompt, settings, args.agent)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not args.no_save:
        save_record(record, settings.run_log_dir)
    print(record.model_dump_json(indent=2))
    return exit_code(record)


EXIT_CODES = {"config": 2, "transient": 3}


def exit_code(record) -> int:
    """0 success, 2 configuration, 3 transient (safe to retry later), 1 anything else."""
    if record.status == "succeeded":
        return 0
    return EXIT_CODES.get(record.error_class, 1)


if __name__ == "__main__":
    raise SystemExit(main())
