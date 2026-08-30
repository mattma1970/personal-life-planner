"""CLI end-to-end tests against a temporary project: plugins, run, runs,
approve, daemon --once (the Phase-1 demo path)."""

from __future__ import annotations

import json

import pytest

from plp.cli import main

DEMO_PLUGIN = '''
from plp.kernel.plugin import Command, Job, Plugin, tool

class DemoPlugin(Plugin):
    name = "demo"

    def setup(self, ctx):
        self._ctx = ctx

    def tools(self):
        @tool("echo a message back")
        def echo(message: str) -> str:
            return message
        return [echo]

    def jobs(self):
        def heartbeat(ctx, args):
            return {"beat": True}

        def hello(ctx, args):
            result = {"hello": True}
            if args.get("propose"):
                result["proposal"] = ctx.approvals.propose(
                    "calendar_block",
                    {"title": "Focus time", "when": args.get("when", "soon")},
                    note="test proposal",
                )
            return result
        return [
            Job(name="demo.heartbeat", cron="* * * * *", handler=heartbeat,
                staleness_h=1, timeout_s=10),
            Job(name="demo.hello", cron="0 9 * * 1", handler=hello),
        ]

    def commands(self):
        def demo_cmd(a, ctx):
            print("demo command works")
            return 0
        c = Command(name="demo", help="demo of plugin-provided commands",
                    handler=demo_cmd)
        c.add_arguments = lambda p: p.add_argument("--flag", action="store_true")
        return [c]
'''

FAILING_PLUGIN = '''
from plp.kernel.plugin import Plugin

class FailingPlugin(Plugin):
    name = "broken"

    def setup(self, ctx):
        raise ValueError("intentional failure for tests")
'''


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "plp.yaml").write_text(
        "timezone: UTC\n"
        "state_db:\n"
        "  path: data/t.db\n"
        "plugins:\n"
        "  dir: plugins\n"
        "delivery:\n"
        "  terminal: true\n"
    )
    (tmp_path / "plugins" / "demo").mkdir(parents=True)
    (tmp_path / "plugins" / "demo" / "plugin.py").write_text(DEMO_PLUGIN)
    (tmp_path / "plugins" / "broken").mkdir(parents=True)
    (tmp_path / "plugins" / "broken" / "plugin.py").write_text(FAILING_PLUGIN)
    monkeypatch.chdir(tmp_path)  # config discovery is CWD-relative by default
    return tmp_path / "config" / "plp.yaml"


def test_cli_plugins_shows_ok_and_failed(cfg_path, capsys):
    rc = main(["--config", str(cfg_path), "plugins"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "demo" in out and "ok" in out
    assert "demo.heartbeat" in out and "demo.echo" in out
    assert "broken" in out and "FAILED" in out
    assert "intentional failure" in out


def test_cli_run_logs_and_returns_detail(cfg_path, capsys):
    rc = main(
        [
            "--config",
            str(cfg_path),
            "run",
            "demo.hello",
            '{"propose": true, "when": "tomorrow 09:00"}',
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "demo.hello → ok" in out
    detail = json.loads(out.split("detail: ", 1)[1].strip())
    assert detail["hello"] is True
    assert isinstance(detail["proposal"], int)


def test_cli_runs_shows_audit(cfg_path, capsys):
    main(["--config", str(cfg_path), "run", "demo.hello"])
    rc = main(["--config", str(cfg_path), "runs"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "demo.hello" in out
    assert "ok" in out


def test_cli_plugin_command(cfg_path, capsys):
    rc = main(["--config", str(cfg_path), "demo", "--flag"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "demo command works" in out


def test_cli_unknown_command_rejected(cfg_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--config", str(cfg_path), "frobnicate"])
    assert excinfo.value.code == 2  # argparse "invalid choice"


def test_cli_run_unknown_job(cfg_path, capsys):
    rc = main(["--config", str(cfg_path), "run", "nope"])
    assert rc == 2
    assert "unknown job" in capsys.readouterr().err


def test_cli_approve_resolves_proposal(cfg_path, capsys):
    out = capsys.readouterr(
    )  # flush
    main(
        [
            "--config",
            str(cfg_path),
            "run",
            "demo.hello",
            '{"propose": true, "when": "soon"}',
        ]
    )
    out = capsys.readouterr().out
    aid = json.loads(out.split("detail: ", 1)[1].strip())["proposal"]
    rc = main(["--config", str(cfg_path), "approve", str(aid)])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"approval #{aid} (calendar_block) → APPROVED" in out
    assert '"title": "Focus time"' in out


def test_cli_approve_reject(cfg_path, capsys):
    main(
        [
            "--config",
            str(cfg_path),
            "run",
            "demo.hello",
            '{"propose": true}',
        ]
    )
    out = capsys.readouterr().out
    aid = json.loads(out.split("detail: ", 1)[1].strip())["proposal"]
    rc = main(
        [
            "--config",
            str(cfg_path),
            "approve",
            str(aid),
            "--reject",
            "--note",
            "not now",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "→ REJECTED" in out


def test_cli_approve_missing(cfg_path, capsys):
    rc = main(["--config", str(cfg_path), "approve", "999"])
    assert rc == 1
    assert "not found or not pending" in capsys.readouterr().err


def test_daemon_once_fires_and_waits(cfg_path, capsys):
    rc = main(["--config", str(cfg_path), "daemon", "--once"])
    err = capsys.readouterr().err
    assert rc == 0
    assert "--once: fired 1 job(s)" in err  # heartbeat is due (never fired, 1-min cron)
    # the run was audited
    rc = main(["--config", str(cfg_path), "runs"])
    out = capsys.readouterr().out
    assert "demo.heartbeat" in out
    assert "ok" in out
