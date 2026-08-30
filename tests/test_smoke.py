"""Smoke tests for the Phase 0 scaffolding."""

import pytest

from plp import __version__
from plp.cli import build_parser, main


def test_version_string():
    assert __version__ == "0.1.0"


def test_cli_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert f"plp {__version__}" in capsys.readouterr().out


def test_cli_no_command_prints_help(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "plp" in out and "daemon" in out


def test_cli_stubbed_subcommands():
    for cmd in ("daemon", "run", "runs", "plugins", "chat", "approve", "calendar"):
        assert main([cmd]) == 0


def test_parser_exposes_all_subcommands():
    parser = build_parser()
    # The subparsers action holds the registered choices.
    for action in parser._actions:
        if action.dest == "command":
            assert set(action.choices) == {
                "daemon", "run", "runs", "plugins", "chat", "approve", "calendar"
            }
            return
    raise AssertionError("no subparser action found")
