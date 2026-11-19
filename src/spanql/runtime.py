"""Managed model runtime: spawn and supervise ``llama-server`` (runtime = "managed")."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import httpx

from spanql.config import Config
from spanql.models.manifest import model_path

log = logging.getLogger("spanql.runtime")


class ManagedLlamaServer:
    def __init__(self, cfg: Config, host: str = "127.0.0.1", port: int = 7718):
        self.cfg = cfg
        self.host = host
        self.port = port
        self.proc: subprocess.Popen[bytes] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.restarts = 0

    @property
    def endpoint(self) -> str:
        return f"http://{self.host}:{self.port}/v1"

    def model_file(self) -> Path:
        p = Path(self.cfg.model.model).expanduser()
        if p.suffix == ".gguf" and p.exists():
            return p
        return model_path(self.cfg.models_dir, self.cfg.model.model)

    def command(self) -> list[str]:
        binary = shutil.which(self.cfg.model.llama_server_bin) or self.cfg.model.llama_server_bin
        cmd = [
            binary,
            "-m",
            str(self.model_file()),
            "--host",
            self.host,
            "--port",
            str(self.port),
            "-c",
            str(self.cfg.model.context),
            "--jinja",
        ]
        if self.cfg.model.threads != "auto":
            cmd += ["-t", str(self.cfg.model.threads)]
        if self.cfg.model.gpu_layers != "auto":
            cmd += ["-ngl", str(self.cfg.model.gpu_layers)]
        return cmd

    def available(self) -> tuple[bool, str]:
        if not (
            shutil.which(self.cfg.model.llama_server_bin)
            or os.path.exists(self.cfg.model.llama_server_bin)
        ):
            return False, (
                f"{self.cfg.model.llama_server_bin} not found; install llama.cpp "
                '(brew install llama.cpp) or set model.runtime = "external"'
            )
        if not self.model_file().exists():
            return False, (
                f"model {self.cfg.model.model} is not downloaded; run "
                f"`spanql model pull {self.cfg.model.model}`"
            )
        return True, "ok"

    def start(self) -> None:
        self._stop.clear()
        self._spawn()
        self._thread = threading.Thread(target=self._supervise, daemon=True)
        self._thread.start()

    def _spawn(self) -> None:
        log.info("starting %s", " ".join(self.command()))
        self.proc = subprocess.Popen(
            self.command(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self._spawned_at = time.monotonic()

    def _supervise(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            if self.proc and self.proc.poll() is not None:
                if time.monotonic() - getattr(self, "_spawned_at", 0.0) > 60:
                    backoff = 1.0  # it ran fine for a while; this is a fresh failure
                log.warning(
                    "llama-server exited with %s; restarting in %.0fs",
                    self.proc.returncode,
                    backoff,
                )
                if self._stop.wait(backoff):
                    break
                self.restarts += 1
                backoff = min(backoff * 2, 60)
                self._spawn()
            self._stop.wait(1.0)

    def wait_ready(self, timeout: float = 120) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                r = httpx.get(f"http://{self.host}:{self.port}/health", timeout=2)
                if r.status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            if self.proc and self.proc.poll() is not None and self.restarts > 3:
                return False
            time.sleep(0.5)
        return False

    def stop(self) -> None:
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
