from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from spanql.config import Config, ModelConfig
from spanql.models import manifest as mf
from spanql.runtime import ManagedLlamaServer


@pytest.fixture
def tiny(monkeypatch: pytest.MonkeyPatch) -> tuple[str, bytes]:
    data = b"GGUF" + b"\x00" * 1000
    spec = mf.ModelSpec(
        alias="tiny",
        repo="org/tiny-GGUF",
        revision="abc123",
        filename="tiny.gguf",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        license="apache-2.0",
        params="1M",
    )
    monkeypatch.setitem(mf.MANIFEST, "tiny", spec)
    return "tiny", data


def test_manifest_entries_are_pinned() -> None:
    for alias, s in mf.MANIFEST.items():
        assert len(s.sha256) == 64 and len(s.revision) == 40, alias
        assert s.url.startswith("https://huggingface.co/") and s.revision in s.url
        assert s.license == "apache-2.0"


def test_pull_verifies_checksum(tmp_path: Path, tiny) -> None:
    alias, data = tiny
    seen = {}

    def h(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        return httpx.Response(200, content=data)

    p = mf.pull(alias, tmp_path, client=httpx.Client(transport=httpx.MockTransport(h)))
    assert p.read_bytes() == data
    assert "abc123" in seen["url"]
    assert {i["alias"] for i in mf.installed(tmp_path)} >= {alias}
    # second pull is a no-op (no request)
    seen.clear()
    mf.pull(alias, tmp_path, client=httpx.Client(transport=httpx.MockTransport(h)))
    assert not seen


def test_pull_rejects_bad_checksum(tmp_path: Path, tiny) -> None:
    alias, _data = tiny
    cl = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"evil")))
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        mf.pull(alias, tmp_path, client=cl)
    assert not list(tmp_path.glob("*.part"))


def test_pull_resumes_with_range(tmp_path: Path, tiny) -> None:
    alias, data = tiny
    part = mf.model_path(tmp_path, alias).with_suffix(".part")
    part.write_bytes(data[:100])
    seen = {}

    def h(req: httpx.Request) -> httpx.Response:
        seen["range"] = req.headers.get("range")
        return httpx.Response(206, content=data[100:])

    mf.pull(alias, tmp_path, client=httpx.Client(transport=httpx.MockTransport(h)))
    assert seen["range"] == "bytes=100-"
    assert mf.model_path(tmp_path, alias).read_bytes() == data


def test_import_file(tmp_path: Path) -> None:
    src = tmp_path / "x.gguf"
    src.write_bytes(b"GGUF123")
    dest = mf.import_file(src, "mine", tmp_path / "models", license="mit")
    assert dest.read_bytes() == b"GGUF123"


def test_managed_runtime_command(tmp_path: Path) -> None:
    cfg = Config(model=ModelConfig(models_dir=str(tmp_path), llama_server_bin="/nope/llama-server"))
    rt = ManagedLlamaServer(cfg, port=7799)
    cmd = rt.command()
    assert cmd[0].endswith("llama-server")
    assert cmd[cmd.index("--port") + 1] == "7799" and "--jinja" in cmd
    assert rt.endpoint == "http://127.0.0.1:7799/v1"
    ok, why = rt.available()
    assert not ok and "not found" in why


FAKE_SERVER = """#!/usr/bin/env python3
import sys, http.server
port = int(sys.argv[sys.argv.index("--port") + 1])
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"status":"ok"}')
    def log_message(self, *a):
        pass
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
"""


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_managed_runtime_spawns_supervises_and_stops(tmp_path: Path) -> None:
    import sys

    # a /bin/sh wrapper (not a python shebang, which macOS handles differently for venv
    # interpreters) that runs the fake server and keeps its stderr for diagnostics
    script = tmp_path / "fake_server.py"
    script.write_text(FAKE_SERVER)
    log = tmp_path / "fake_server.log"
    binary = tmp_path / "llama-server"
    binary.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@" 2>>"{log}"\n')
    binary.chmod(0o755)

    def why() -> str:
        code = rt.proc.poll() if rt.proc else None
        return f"exit={code} restarts={rt.restarts} log={log.read_text() if log.exists() else ''!r}"

    (tmp_path / "m.gguf").write_bytes(b"GGUF")
    cfg = Config(model=ModelConfig(model=str(tmp_path / "m.gguf"), llama_server_bin=str(binary)))
    rt = ManagedLlamaServer(cfg, port=_free_port())
    assert rt.available() == (True, "ok")
    rt.start()
    try:
        assert rt.wait_ready(timeout=15), why()
        assert rt.proc is not None
        rt.proc.kill()  # crash -> supervisor restarts it
        rt.proc.wait()
        assert rt.wait_ready(timeout=15), why()
        import time

        deadline = time.monotonic() + 10
        while rt.restarts < 1 and time.monotonic() < deadline:
            time.sleep(0.1)
        assert rt.restarts >= 1
    finally:
        rt.stop()
    assert rt.proc is not None and rt.proc.poll() is not None
