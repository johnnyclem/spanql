"""Configuration: one TOML file plus ``SPANQL_*`` environment overrides.

Env overrides use ``SPANQL_<SECTION>__<KEY>`` (double underscore), e.g.
``SPANQL_MODEL__RUNTIME=external``. A few top-level shortcuts are also honoured:
``SPANQL_CONFIG`` (config path), ``SPANQL_HOME`` (data dir), ``SPANQL_MODELS``
(model cache), ``SPANQL_API_TOKEN``.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

DEFAULT_PORT = 7717
DEFAULT_MODEL_PORT = 7718


def data_home() -> Path:
    if env := os.environ.get("SPANQL_HOME"):
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "spanql"


def homebrew_prefix() -> Path | None:
    """The Homebrew prefix when spanql runs from a Homebrew keg (…/Cellar/spanql/<ver>/…)."""
    parts = Path(sys.prefix).resolve().parts
    if "Cellar" in parts:
        i = len(parts) - 1 - parts[::-1].index("Cellar")
        if i + 1 < len(parts) and parts[i + 1] == "spanql":
            return Path(*parts[:i])
    return None


def config_home() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "spanql"


@dataclass
class ServerConfig:
    bind: str = f"127.0.0.1:{DEFAULT_PORT}"
    api_token: str | None = None
    # Explicit opt-in to serve a non-loopback bind without a token. Only for setups where
    # the network boundary is enforced elsewhere, e.g. a container whose port is published
    # on the host's 127.0.0.1 only (see compose.yaml).
    allow_unauthenticated: bool = False

    @property
    def host(self) -> str:
        return self.bind.rsplit(":", 1)[0]

    @property
    def port(self) -> int:
        return int(self.bind.rsplit(":", 1)[1])

    @property
    def is_loopback(self) -> bool:
        return self.host in {"127.0.0.1", "localhost", "::1", "[::1]"}


@dataclass
class StoreConfig:
    path: str = ""
    snapshot_retention_days: int = 90


@dataclass
class SourceConfig:
    path: str
    kind: str = "auto"  # auto | doc | log | file | code
    name: str | None = None
    watch: bool = False
    respect_gitignore: bool = True
    parser: str = "auto"  # log parser: auto | jsonl | access | syslog | generic


@dataclass
class ModelConfig:
    runtime: str = "external"  # managed | external
    backend: str = "llama.cpp"  # llama.cpp | ollama | openai-compatible | fake
    model: str = "qwen3.5-4b-instruct-q4_k_m"
    endpoint: str = f"http://127.0.0.1:{DEFAULT_MODEL_PORT}/v1"
    api_key: str | None = None
    context: int = 32768
    threads: str = "auto"
    gpu_layers: str = "auto"
    timeout_s: float = 60.0
    temperature: float = 0.0
    llama_server_bin: str = "llama-server"
    models_dir: str = ""


@dataclass
class HarnessConfig:
    max_attempts: int = 3
    candidate_token_budget: int = 8192
    max_candidates: int = 12
    relevance_check: bool = False
    wall_clock_s: float = 30.0


@dataclass
class RetrievalConfig:
    vectors: bool = False


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    store: StoreConfig = field(default_factory=StoreConfig)
    sources: list[SourceConfig] = field(default_factory=list)
    model: ModelConfig = field(default_factory=ModelConfig)
    harness: HarnessConfig = field(default_factory=HarnessConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    path: str | None = None  # where this config was loaded from

    @property
    def store_path(self) -> Path:
        if self.store.path:
            return Path(self.store.path).expanduser()
        return data_home() / "spanql.db"

    @property
    def models_dir(self) -> Path:
        if self.model.models_dir:
            return Path(self.model.models_dir).expanduser()
        if env := os.environ.get("SPANQL_MODELS"):
            return Path(env).expanduser()
        return data_home() / "models"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("path", None)
        if d["server"].get("api_token"):
            d["server"]["api_token"] = "***"
        if d["model"].get("api_key"):
            d["model"]["api_key"] = "***"
        return d


_SECTIONS: dict[str, type] = {
    "server": ServerConfig,
    "store": StoreConfig,
    "model": ModelConfig,
    "harness": HarnessConfig,
    "retrieval": RetrievalConfig,
}


class ConfigError(ValueError):
    pass


def _coerce(value: str, target: Any) -> Any:
    if isinstance(target, bool):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(target, int):
        return int(value)
    if isinstance(target, float):
        return float(value)
    return value


def _build_section(cls: type, raw: dict[str, Any], section: str) -> Any:
    names = {f.name for f in fields(cls)}
    unknown = set(raw) - names
    if unknown:
        raise ConfigError(f"unknown key(s) in [{section}]: {', '.join(sorted(unknown))}")
    return cls(**raw)


def default_config_path() -> Path | None:
    if env := os.environ.get("SPANQL_CONFIG"):
        return Path(env).expanduser()
    brew = homebrew_prefix()
    cands = [Path("spanql.toml"), config_home() / "config.toml"]
    if brew is not None:
        cands.append(brew / "etc" / "spanql" / "config.toml")
    for cand in cands:
        if cand.exists():
            return cand
    return None


def load_config(path: str | Path | None = None, *, env: dict[str, str] | None = None) -> Config:
    """Load config from ``path`` (or the default location) and apply env overrides."""
    env = dict(os.environ if env is None else env)
    p = Path(path).expanduser() if path else default_config_path()
    raw: dict[str, Any] = {}
    if p is not None:
        if not p.exists():
            raise ConfigError(f"config file not found: {p}")
        with p.open("rb") as fh:
            raw = tomllib.load(fh)

    cfg = Config(path=str(p) if p else None)
    for name, cls in _SECTIONS.items():
        if name in raw:
            if not isinstance(raw[name], dict):
                raise ConfigError(f"[{name}] must be a table")
            setattr(cfg, name, _build_section(cls, raw[name], name))
    for i, src in enumerate(raw.get("sources", [])):
        if "path" not in src:
            raise ConfigError(f"[[sources]] #{i + 1} is missing 'path'")
        cfg.sources.append(_build_section(SourceConfig, src, "sources"))
    unknown_top = set(raw) - set(_SECTIONS) - {"sources"}
    if unknown_top:
        raise ConfigError(f"unknown section(s): {', '.join(sorted(unknown_top))}")

    # env overrides: SPANQL_SECTION__KEY
    for key, value in env.items():
        if not key.startswith("SPANQL_") or "__" not in key:
            continue
        section, _, attr = key[len("SPANQL_") :].lower().partition("__")
        obj = getattr(cfg, section, None) if section in _SECTIONS else None
        if obj is None or not hasattr(obj, attr):
            continue
        setattr(
            obj, attr, _coerce(value, getattr(obj, attr) if getattr(obj, attr) is not None else "")
        )
    if token := env.get("SPANQL_API_TOKEN"):
        cfg.server.api_token = token
    if (
        not cfg.server.is_loopback
        and not cfg.server.api_token
        and not cfg.server.allow_unauthenticated
    ):
        raise ConfigError(
            f"server.bind={cfg.server.bind} is not loopback; set SPANQL_API_TOKEN to enable auth"
        )
    return cfg


DEFAULT_CONFIG_TOML = """\
# spanql configuration. Every key can be overridden with SPANQL_<SECTION>__<KEY>.

[server]
bind = "127.0.0.1:7717"
# api_token comes from env SPANQL_API_TOKEN; required if bind is not loopback

[store]
path = "{store_path}"
snapshot_retention_days = 90

# Add one [[sources]] table per folder, repo, file or log glob.
# [[sources]]
# path = "~/src/payments"
# kind = "code"            # auto | doc | log | file | code
# respect_gitignore = true
#
# [[sources]]
# path = "/var/log/myapp/*.log"
# kind = "log"
# parser = "auto"          # auto | jsonl | access | syslog | generic

[model]
runtime = "{runtime}"     # managed (spanql starts llama-server) | external
backend = "llama.cpp"      # llama.cpp | ollama | openai-compatible | fake
model = "qwen3.5-4b-instruct-q4_k_m"
endpoint = "http://127.0.0.1:7718/v1"
models_dir = "{models_dir}"
context = 32768

[harness]
max_attempts = 3
candidate_token_budget = 8192
max_candidates = 12
relevance_check = false

[retrieval]
vectors = false
"""


def render_default_config(store_path: Path, models_dir: Path, runtime: str = "external") -> str:
    return DEFAULT_CONFIG_TOML.format(
        store_path=str(store_path), models_dir=str(models_dir), runtime=runtime
    )
