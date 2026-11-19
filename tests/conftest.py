from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from spanql.config import Config, HarnessConfig, ModelConfig, SourceConfig, StoreConfig
from spanql.harness.backends import FakeBackend
from spanql.ingest.pipeline import ingest_all
from spanql.services import Services
from spanql.store.db import Store

REPO = Path(__file__).resolve().parents[1]
DEMO = REPO / "examples" / "demo"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Never touch the developer's real config/data dirs."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    for k in list(__import__("os").environ):
        if k.startswith("SPANQL_"):
            monkeypatch.delenv(k, raising=False)


@pytest.fixture
def demo_dir(tmp_path: Path) -> Path:
    dst = tmp_path / "demo"
    shutil.copytree(DEMO, dst)
    return dst


@pytest.fixture
def store(tmp_path: Path) -> Store:
    st = Store(tmp_path / "spanql.db")
    yield st
    st.close()


@pytest.fixture
def demo_store(store: Store, demo_dir: Path) -> Store:
    rep = ingest_all(store, [SourceConfig(path=str(demo_dir))])
    assert not rep.errors, rep.errors
    return store


@pytest.fixture
def config(tmp_path: Path, demo_dir: Path) -> Config:
    return Config(
        store=StoreConfig(path=str(tmp_path / "svc.db")),
        sources=[SourceConfig(path=str(demo_dir))],
        model=ModelConfig(backend="fake", models_dir=str(tmp_path / "models")),
        harness=HarnessConfig(),
    )


@pytest.fixture
def services(config: Config) -> Services:
    svc = Services.create(config, backend=FakeBackend())
    svc.ingest()
    yield svc
    svc.close()


def write(p: Path, text: str | bytes) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        p.write_bytes(text)
    else:
        p.write_text(text, encoding="utf-8")
    return p
