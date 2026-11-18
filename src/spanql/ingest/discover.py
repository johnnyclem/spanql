"""Find the files to ingest. Git repos use ``git ls-files`` (respects .gitignore);
everything else is walked, honouring ``.spanqlignore`` and a short default ignore list."""

from __future__ import annotations

import fnmatch
import os
import subprocess
from pathlib import Path

DEFAULT_IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache",
                        ".pytest_cache", ".idea", ".ruff_cache"}
DEFAULT_IGNORED_FILES = {".DS_Store", "Thumbs.db", ".spanqlignore"}


def load_ignore(root: Path) -> list[str]:
    f = root / ".spanqlignore"
    if not f.is_file():
        return []
    return [ln.strip() for ln in f.read_text("utf-8", "replace").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def is_ignored(rel: str, patterns: list[str]) -> bool:
    parts = rel.split("/")
    for pat in patterns:
        if pat.startswith("!"):
            continue  # negation is not supported
        dir_only = pat.endswith("/")
        pat = pat.rstrip("/")
        anchored = pat.startswith("/")
        pat = pat.lstrip("/")
        if dir_only:
            if any(fnmatch.fnmatch("/".join(parts[: i + 1]), pat) or (not anchored and fnmatch.fnmatch(parts[i], pat))
                   for i in range(len(parts) - 1)):
                return True
        elif fnmatch.fnmatch(rel, pat) or (not anchored and ("/" not in pat) and any(fnmatch.fnmatch(p, pat) for p in parts)):
            return True
    return False


def _git(root: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None


def git_commit(root: Path) -> str | None:
    out = _git(root, "rev-parse", "HEAD")
    return out.strip() if out else None


def discover(root: Path, *, respect_gitignore: bool = True) -> list[Path]:
    """Files under ``root`` (relative paths resolved), sorted. ``root`` may itself be a file."""
    if root.is_file():
        return [root]
    patterns = load_ignore(root)
    files: list[Path] = []
    top = _git(root, "rev-parse", "--show-toplevel") if respect_gitignore else None
    if top:
        out = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        if out is not None:
            for rel in filter(None, out.split("\0")):
                p = root / rel
                if p.is_file() and not p.is_symlink() and p.name not in DEFAULT_IGNORED_FILES \
                        and not is_ignored(rel, patterns):
                    files.append(p)
            return sorted(set(files))
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in DEFAULT_IGNORED_DIRS)
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            rel = p.relative_to(root).as_posix()
            if fn in DEFAULT_IGNORED_FILES or p.is_symlink() or is_ignored(rel, patterns):
                continue
            files.append(p)
    return files
