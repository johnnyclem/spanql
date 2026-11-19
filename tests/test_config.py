from __future__ import annotations

from pathlib import Path

import pytest

from spanql.config import ConfigError, load_config, render_default_config


def test_defaults_without_file(tmp_path: Path) -> None:
    cfg = load_config(env={})
    assert cfg.server.bind == "127.0.0.1:7717"
    assert cfg.server.is_loopback
    assert cfg.model.backend == "llama.cpp"
    assert cfg.harness.max_attempts == 3
    assert cfg.store_path.name == "spanql.db"


def test_load_file_and_sources(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text(
        '[store]\npath = "/x/s.db"\n[[sources]]\npath = "~/notes"\nkind = "doc"\n'
        '[[sources]]\npath = "/var/log/*.log"\nparser = "jsonl"\n[model]\nbackend = "ollama"\n'
    )
    cfg = load_config(p, env={})
    assert str(cfg.store_path) == "/x/s.db"
    assert [s.kind for s in cfg.sources] == ["doc", "auto"]
    assert cfg.sources[1].parser == "jsonl"
    assert cfg.model.backend == "ollama"


def test_env_overrides_coerce_types(tmp_path: Path) -> None:
    cfg = load_config(
        env={
            "SPANQL_HARNESS__MAX_ATTEMPTS": "5",
            "SPANQL_HARNESS__RELEVANCE_CHECK": "true",
            "SPANQL_MODEL__ENDPOINT": "http://h:1/v1",
            "SPANQL_NOPE__X": "ignored",
        }
    )
    assert cfg.harness.max_attempts == 5
    assert cfg.harness.relevance_check is True
    assert cfg.model.endpoint == "http://h:1/v1"


def test_non_loopback_requires_token(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="SPANQL_API_TOKEN"):
        load_config(env={"SPANQL_SERVER__BIND": "0.0.0.0:7717"})
    cfg = load_config(env={"SPANQL_SERVER__BIND": "0.0.0.0:7717", "SPANQL_API_TOKEN": "t"})
    assert cfg.server.api_token == "t"
    assert not cfg.server.is_loopback
    assert cfg.server.port == 7717


@pytest.mark.parametrize(
    "body,msg",
    [
        ("[bogus]\nx=1\n", "unknown section"),
        ("[[sources]]\nkind='doc'\n", "missing 'path'"),
        ("server = 3\n", "must be a table"),
    ],
)
def test_bad_config(tmp_path: Path, body: str, msg: str) -> None:
    p = tmp_path / "c.toml"
    p.write_text(body)
    with pytest.raises(ConfigError, match=msg):
        load_config(p, env={})


def test_missing_file_is_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml", env={})


def test_rendered_default_config_loads(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text(render_default_config(tmp_path / "s.db", tmp_path / "m", runtime="managed"))
    cfg = load_config(p, env={})
    assert cfg.model.runtime == "managed"
    assert cfg.models_dir == tmp_path / "m"
