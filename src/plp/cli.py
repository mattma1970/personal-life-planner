"""``plp`` command-line interface (Phase 0 stub).

Subcommands are declared here so the surface is visible from day one; their
implementations land in later build phases (see PRD.md §7 and §8).
"""

from __future__ import annotations

import argparse
import sys

from plp import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plp",
        description="PersonalLifePlanner — a local-first, plugin-based personal assistant.",
    )
    parser.add_argument("--version", action="version", version=f"plp {__version__}")
    sub = parser.add_subparsers(dest="command")
    for name, help_text in (
        ("daemon", "Run the PLP daemon (scheduler + agent runtime)."),
        ("run", "Run a job on demand: plp run <job> [json-args]."),
        ("runs", "Show recent job runs and their outcomes."),
        ("plugins", "List discovered plugins, their jobs, and their tools."),
        ("chat", "Conversational turn with the assistant."),
        ("approve", "Approve or reject a pending proposal: plp approve <id> [reject]."),
        ("calendar", "View calendar entries: plp calendar week|today|upcoming."),
    ):
        sub.add_parser(name, help=help_text)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command is None:
        build_parser().print_help()
        return 0
    print(f"plp {args.command}: implemented in a later build phase (see PRD.md §8)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
