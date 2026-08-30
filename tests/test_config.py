"""Config tests: real config file validation, error paths, path resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from plp.kernel.config import ConfigError, load_config, resolve

REPO = Path(__file__).resolve().parent.parent


def test_loads_real_config():
    cfg = load_config(REPO / "config" / "plp.yaml")
    assert cfg.llm.model == "qwen3.8-27b"
    assert cfg.llm.base_url == "http://127.0.0.1:11434/v1"
    assert cfg.llm.max_tool_steps == 8
    assert cfg.delivery.terminal is True
    assert cfg.delivery.email.enabled is False
    assert cfg.features.email_summarization is False
    assert cfg.schedules == {}


def test_root_resolves_to_repo_root():
    cfg = load_config(REPO / "config" / "plp.yaml")
    assert cfg.root == REPO
    assert resolve(cfg, "data/plp.db") == REPO / "data" / "plp.db"
    assert resolve(cfg, "plp-vault") == REPO / "plp-vault"
    assert resolve(cfg, "/abs/path") == Path("/abs/path")


def test_tzinfo_default_is_none():
    cfg = load_config(REPO / "config" / "plp.yaml")
    assert cfg.tzinfo() is None  # "" = system local


def test_tzinfo_explicit():
    cfg = load_config(REPO / "config" / "plp.yaml")
    cfg.timezone = "UTC"
    from zoneinfo import ZoneInfo

    assert cfg.tzinfo() == ZoneInfo("UTC")


def test_missing_file_raises():
    with pytest.raises(ConfigError, match="not found"):
        load_config("/nonexistent/plp.yaml")


def test_invalid_yaml(tmp_path):
    p = tmp_path / "plp.yaml"
    p.write_text("a: [unclosed")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(p)


def test_invalid_type(tmp_path):
    d = tmp_path / "config"
    d.mkdir()
    f = d / "plp.yaml"
    f.write_text("schedules: [1, 2]\n")
    with pytest.raises(ConfigError, match="invalid config"):
        load_config(f)


def test_schedule_overrides_loaded(tmp_path):
    d = tmp_path / "config"
    d.mkdir()
    f = d / "plp.yaml"
    f.write_text("schedules:\n  news.collect: '10 7 * * *'\n")
    cfg = load_config(f)
    assert cfg.schedules == {"news.collect": "10 7 * * *"}
    assert cfg.root == tmp_path
