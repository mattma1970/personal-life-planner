"""Smoke tests: version, help, parser surface."""

from __future__ import annotations

import argparse

import pytest

from plp import __version__
from plp.cli import build_parser, main

# Static parser commands. Since Phase 4, `calendar` is NOT here: the calendar
# plugin registers its own `calendar` subcommand during discovery.
ALL_COMMANDS = [
    "daemon",
    "run",
    "runs",
    "plugins",
    "search",
    "approve",
    "chat",
]


def test_version():
    assert __version__ == "0.1.0"


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "plp 0.1.0" in capsys.readouterr().out


def test_no_command_prints_help(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    for cmd in ALL_COMMANDS:
        assert cmd in out


def test_parser_choices():
    p = build_parser()
    subs = [
        a
        for a in p._subparsers._actions
        if isinstance(a, argparse._SubParsersAction)
    ][0]
    assert set(subs.choices) == set(ALL_COMMANDS)


def test_stub_commands(capsys):
    assert main(["chat"]) == 0
    assert "phase 5" in capsys.readouterr().out.lower()


def test_calendar_not_in_static_parser():
    """Phase 4: `calendar` is plugin-registered, so it must NOT be a static
    choice (static names always win in the wire-up — a static `calendar`
    would permanently shadow the plugin's)."""
    p = build_parser()
    subs = [
        a
        for a in p._subparsers._actions
        if isinstance(a, argparse._SubParsersAction)
    ][0]
    assert "calendar" not in subs.choices
