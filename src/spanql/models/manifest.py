"""Known model aliases with pinned Hugging Face revisions and SHA-256 checksums.

Facts checked against the Hugging Face API on 2026-11-18 (repo sha, file size, LFS sha256).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx


@dataclass(frozen=True)
class ModelSpec:
    alias: str
    repo: str
    revision: str
    filename: str
    sha256: str
    size: int
    license: str
    params: str
    notes: str = ""

    @property
    def url(self) -> str:
        return f"https://huggingface.co/{self.repo}/resolve/{self.revision}/{self.filename}"


MANIFEST: dict[str, ModelSpec] = {
    m.alias: m
    for m in [
        ModelSpec(
            "qwen3.5-4b-instruct-q4_k_m",
            "unsloth/Qwen3.5-4B-GGUF",
            "e87f176479d0855a907a41277aca2f8ee7a09523",
            "Qwen3.5-4B-Q4_K_M.gguf",
            "00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4",
            2740937888,
            "apache-2.0",
            "4B",
            "default (MODEL.md); non-thinking mode",
        ),
        ModelSpec(
            "qwen3.5-4b-instruct-q8_0",
            "unsloth/Qwen3.5-4B-GGUF",
            "e87f176479d0855a907a41277aca2f8ee7a09523",
            "Qwen3.5-4B-Q8_0.gguf",
            "10cc391b403021dd11c614679d2fd92f611c3681d29e29651b717316965d61e1",
            4482403488,
            "apache-2.0",
            "4B",
        ),
        ModelSpec(
            "qwen3.5-2b-instruct-q4_k_m",
            "unsloth/Qwen3.5-2B-GGUF",
            "f6d5376be1edb4d416d56da11e5397a961aca8ae",
            "Qwen3.5-2B-Q4_K_M.gguf",
            "aaf42c8b7c3cab2bf3d69c355048d4a0ee9973d48f16c731c0520ee914699223",
            1280835840,
            "apache-2.0",
            "2B",
            "low-memory tier",
        ),
        ModelSpec(
            "qwen3.5-0.8b-instruct-q4_k_m",
            "unsloth/Qwen3.5-0.8B-GGUF",
            "6ab461498e2023f6e3c1baea90a8f0fe38ab64d0",
            "Qwen3.5-0.8B-Q4_K_M.gguf",
            "bd258782e35f7f458f8aced1adc053e6e92e89bc735ba3be89d38a06121dc517",
            532517120,
            "apache-2.0",
            "0.8B",
            "smoke tests / relevance classifier",
        ),
    ]
}


def model_path(models_dir: Path, alias: str) -> Path:
    return models_dir / f"{alias}.gguf"


def installed(models_dir: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not models_dir.exists():
        return out
    for f in sorted(models_dir.glob("*.gguf")):
        meta_f = f.with_suffix(".json")
        meta = json.loads(meta_f.read_text()) if meta_f.exists() else {}
        out.append({"alias": f.stem, "path": str(f), "size": f.stat().st_size, **meta})
    return out


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while data := fh.read(chunk):
            h.update(data)
    return h.hexdigest()


def pull(
    alias: str,
    models_dir: Path,
    *,
    progress: Callable[[int, int], None] | None = None,
    client: httpx.Client | None = None,
) -> Path:
    spec = MANIFEST.get(alias)
    if spec is None:
        raise KeyError(f"unknown model alias {alias!r}; see `spanql model list`")
    models_dir.mkdir(parents=True, exist_ok=True)
    dest = model_path(models_dir, alias)
    if dest.exists() and dest.stat().st_size == spec.size and sha256_file(dest) == spec.sha256:
        return dest
    tmp = dest.with_suffix(".part")
    headers = {}
    done = tmp.stat().st_size if tmp.exists() else 0
    if done:
        headers["Range"] = f"bytes={done}-"
    if token := os.environ.get("HF_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    own = client is None
    cl = client or httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60, read=300))
    try:
        with cl.stream("GET", spec.url, headers=headers) as r:
            if r.status_code == 200:
                done = 0
                mode = "wb"
            elif r.status_code == 206:
                mode = "ab"
            else:
                raise RuntimeError(f"download failed: HTTP {r.status_code}")
            with tmp.open(mode) as fh:
                for chunk in r.iter_bytes(1 << 20):
                    fh.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, spec.size)
    finally:
        if own:
            cl.close()
    digest = sha256_file(tmp)
    if digest != spec.sha256:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"checksum mismatch for {alias}: got {digest}, want {spec.sha256}")
    tmp.replace(dest)
    dest.with_suffix(".json").write_text(json.dumps(asdict(spec), indent=2))
    return dest


def import_file(src: Path, alias: str, models_dir: Path, license: str = "unknown") -> Path:
    if not src.exists():
        raise FileNotFoundError(src)
    models_dir.mkdir(parents=True, exist_ok=True)
    dest = model_path(models_dir, alias)
    shutil.copyfile(src, dest)
    dest.with_suffix(".json").write_text(
        json.dumps(
            {
                "alias": alias,
                "imported_from": str(src),
                "sha256": sha256_file(dest),
                "license": license,
            },
            indent=2,
        )
    )
    return dest
