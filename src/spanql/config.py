"""Configuration: one TOML file plus ``SPANQL_*`` environment overrides (DEPLOYMENT.md section 1).

Environment form: ``SPANQL_<SECTION>__<KEY>`` (e.g. ``SPANQL_MODEL__ENDPOINT``).
"""

from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .harness import HarnessConfig, OpenAICompatClient

DEFAULTS: dict = {
    "server": {"bind": "127.0.0.1:7717"},
    "store": {"path": "~/.local/share/spanql/spanql.db", "snapshot_retention_days": 90},
    "sources": [],
    "model": {
        "runtime": "external",  # "managed" (spanql supervises llama-server) is not implemented yet
        "backend": "llama.cpp",  # llama.cpp gets grammar-constrained decoding; others rely on the verifier
        "model": "spanql",
        "endpoint": "http://127.0.0.1:7718/v1",
        "api_key": "",
        "timeout": 60,
        "thinking": False,
    },
    "harness": {"max_attempts": 3, "candidate_token_budget": 8192, "relevance_check": True, "max_seconds": 30,
                "k": 40},
    "retrieval": {"vectors": False},
}


class ConfigError(Exception):
    pass


def _coerce(value: str):
    low = value.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _merge(base: dict, extra: dict) -> dict:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def find_config_path(explicit: str | None, env: dict) -> Path | None:
    for cand in (explicit, env.get("SPANQL_CONFIG")):
        if cand:
            p = Path(cand).expanduser()
            if not p.is_file():
                raise ConfigError(f"config file not found: {p}")
            return p
    for p in (Path("config.toml"), Path("~/.config/spanql/config.toml").expanduser()):
        if p.is_file():
            return p
    return None


@dataclass
class Config:
    data: dict = field(default_factory=lambda: copy.deepcopy(DEFAULTS))
    path: Path | None = None

    @property
    def store_path(self) -> str:
        return str(Path(self.data["store"]["path"]).expanduser())

    @property
    def sources(self) -> list[dict]:
        return list(self.data.get("sources", []))

    def harness_config(self) -> HarnessConfig:
        h = self.data["harness"]
        return HarnessConfig(max_attempts=int(h["max_attempts"]), candidate_token_budget=int(h["candidate_token_budget"]),
                             relevance_check=bool(h["relevance_check"]), k=int(h.get("k", 40)),
                             max_seconds=float(h["max_seconds"]))

    def make_client(self):
        """The configured model client, or None when no endpoint is set."""
        m = self.data["model"]
        if m.get("runtime") == "managed":
            raise ConfigError("model.runtime = \"managed\" is not implemented yet; run llama-server yourself and set "
                              "runtime = \"external\" with model.endpoint")
        if not m.get("endpoint"):
            return None
        return OpenAICompatClient(m["endpoint"], m.get("model") or "spanql", backend=m.get("backend", "llama.cpp"),
                                  api_key=m.get("api_key") or os.environ.get("SPANQL_MODEL_API_KEY") or None,
                                  timeout=float(m.get("timeout", 60)), thinking=bool(m.get("thinking", False)))

    def bind(self) -> tuple[str, int]:
        host, _, port = self.data["server"]["bind"].rpartition(":")
        if not host or not port.isdigit():
            raise ConfigError(f"server.bind must look like 127.0.0.1:7717, got {self.data['server']['bind']!r}")
        return host, int(port)


def load_config(path: str | None = None, env: dict | None = None) -> Config:
    env = dict(os.environ) if env is None else env
    data = copy.deepcopy(DEFAULTS)
    p = find_config_path(path, env)
    if p is not None:
        try:
            _merge(data, tomllib.loads(p.read_text("utf-8")))
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{p}: {e}") from e
    for k, v in env.items():
        if not k.startswith("SPANQL_") or "__" not in k:
            continue
        section, _, key = k[len("SPANQL_"):].lower().partition("__")
        if section in data and isinstance(data[section], dict) and key:
            data[section][key] = _coerce(v)
    return Config(data, p)
