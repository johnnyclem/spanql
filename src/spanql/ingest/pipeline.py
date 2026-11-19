"""Discover files under configured roots and ingest them into the span store."""

from __future__ import annotations

import glob as globmod
import hashlib
import os
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pathspec

from spanql.blocks import Converted
from spanql.config import SourceConfig
from spanql.ingest import code as code_conv
from spanql.ingest import converters as cv
from spanql.ingest.logs import convert_log
from spanql.store.db import Store

MAX_FILE_BYTES = 20 * 1024 * 1024

DOC_EXT = {".md", ".markdown", ".mdx", ".rst", ".txt", ".adoc", ".html", ".htm", ".pdf", ".text"}
FILE_EXT = {
    ".csv",
    ".tsv",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".xml",
    ".env.example",
    ".conf",
    ".properties",
}
LOG_EXT = {".log", ".jsonl", ".ndjson", ".out", ".err"}
CODE_EXT = set(code_conv.LANG_BY_EXT) | {
    ".rs",
    ".java",
    ".kt",
    ".rb",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".hpp",
    ".cs",
    ".swift",
    ".m",
    ".php",
    ".scala",
    ".sh",
    ".bash",
    ".zsh",
    ".sql",
    ".lua",
    ".ex",
    ".exs",
    ".erl",
    ".hs",
    ".clj",
    ".dart",
    ".r",
    ".jl",
    ".vue",
    ".svelte",
    ".tf",
    ".proto",
    ".gradle",
    ".mk",
}
ALWAYS_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".venv",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "dist",
    "build",
    ".idea",
    ".DS_Store",
}
SPECIAL_NAMES = {"Dockerfile": "code", "Makefile": "code", "Containerfile": "code"}


def ext_of(path: str) -> str:
    name = os.path.basename(path)
    lower = name.lower()
    # rotated logs: app.log.1, app.log.2.gz is binary and skipped later
    parts = lower.split(".")
    if len(parts) >= 3 and parts[-2] == "log" and parts[-1].isdigit():
        return ".log"
    return os.path.splitext(lower)[1]


def detect_kind(path: str) -> str | None:
    name = os.path.basename(path)
    if name in SPECIAL_NAMES:
        return SPECIAL_NAMES[name]
    ext = ext_of(path)
    if ext in LOG_EXT:
        return "log"
    if ext in DOC_EXT:
        return "doc"
    if ext in FILE_EXT:
        return "file"
    if ext in CODE_EXT:
        return "code"
    return None


def is_binary(raw: bytes) -> bool:
    return b"\x00" in raw[:8192]


def convert(raw: bytes, path: str, uri: str, kind: str, parser: str = "auto") -> Converted:
    ext = ext_of(path)
    if kind == "log" or ext in LOG_EXT:
        return convert_log(raw, parser)
    if ext in (".md", ".markdown", ".mdx"):
        return cv.convert_markdown(raw)
    if ext in (".html", ".htm"):
        return cv.convert_html(raw)
    if ext == ".pdf":
        return cv.convert_pdf(raw)
    if ext == ".csv":
        return cv.convert_delimited(raw, ",")
    if ext == ".tsv":
        return cv.convert_delimited(raw, "\t")
    if ext in CODE_EXT or os.path.basename(path) in SPECIAL_NAMES:
        return code_conv.convert_code(raw, uri, ext)
    if ext in FILE_EXT:
        return cv.convert_lines(raw)
    return cv.convert_plain(raw)


# --------------------------------------------------------------------- discovery
@dataclass
class Discovered:
    path: str
    uri: str
    kind: str


@dataclass
class RootInfo:
    name: str
    base: str
    git_commit: str | None = None
    files: list[Discovered] = field(default_factory=list)


def _git(base: str, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", base, *args], capture_output=True, text=True, timeout=60, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout


def _ignore_spec(base: Path) -> pathspec.PathSpec | None:
    patterns: list[str] = []
    for name in (".gitignore", ".spanqlignore"):
        f = base / name
        if f.exists():
            patterns.extend(f.read_text(errors="replace").splitlines())
    return pathspec.GitIgnoreSpec.from_lines(patterns) if patterns else None


def discover(src: SourceConfig) -> RootInfo:
    raw = os.path.expanduser(src.path)
    has_glob = any(ch in raw for ch in "*?[")
    if has_glob:
        base = raw
        while any(ch in base for ch in "*?["):
            base = os.path.dirname(base)
        paths = sorted(p for p in globmod.glob(raw, recursive=True) if os.path.isfile(p))
        base = os.path.abspath(base or ".")
        info = RootInfo(src.name or os.path.basename(base) or "root", base)
        rels = [os.path.relpath(os.path.abspath(p), base) for p in paths]
    elif os.path.isfile(raw):
        base = os.path.dirname(os.path.abspath(raw))
        info = RootInfo(src.name or os.path.basename(base) or "root", base)
        rels = [os.path.basename(raw)]
    elif os.path.isdir(raw):
        base = os.path.abspath(raw)
        info = RootInfo(src.name or os.path.basename(base.rstrip("/")) or "root", base)
        rels = list(_walk(base, src.respect_gitignore, info))
    else:
        raise FileNotFoundError(f"source path not found: {src.path}")

    for rel in rels:
        full = os.path.join(base, rel)
        kind = src.kind if src.kind != "auto" else detect_kind(full)
        if kind is None:
            kind = "doc"  # unknown text files are documents; binaries are dropped at read time
        info.files.append(Discovered(full, f"{info.name}/{rel.replace(os.sep, '/')}", kind))
    return info


def _walk(base: str, respect_gitignore: bool, info: RootInfo) -> Iterator[str]:
    if respect_gitignore and os.path.isdir(os.path.join(base, ".git")):
        out = _git(base, "ls-files", "-co", "--exclude-standard", "-z")
        if out is not None:
            info.git_commit = (_git(base, "rev-parse", "HEAD") or "").strip() or None
            for rel in sorted(p for p in out.split("\0") if p):
                if os.path.isfile(os.path.join(base, rel)) and not (
                    set(Path(rel).parts) & ALWAYS_SKIP_DIRS
                ):
                    yield rel
            return
    spec = _ignore_spec(Path(base)) if respect_gitignore else None
    for dirpath, dirnames, filenames in os.walk(base):
        rel_dir = os.path.relpath(dirpath, base)
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in ALWAYS_SKIP_DIRS
            and not (spec and spec.match_file(os.path.normpath(os.path.join(rel_dir, d)) + "/"))
        )
        for fn in sorted(filenames):
            rel = os.path.normpath(os.path.join(rel_dir, fn))
            if fn in (".gitignore", ".spanqlignore", ".DS_Store"):
                continue
            if spec and spec.match_file(rel):
                continue
            yield rel


# ------------------------------------------------------------------------ ingest
@dataclass
class IngestReport:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    roots: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "roots": self.roots,
            "added": self.added,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "removed": self.removed,
            "skipped": [{"path": p, "reason": r} for p, r in self.skipped],
            "errors": [{"path": p, "error": e} for p, e in self.errors],
        }


def ingest_source(
    store: Store,
    src: SourceConfig,
    report: IngestReport | None = None,
    progress: Callable[[str], None] | None = None,
) -> IngestReport:
    report = report or IngestReport()
    info = discover(src)
    report.roots.append(f"{info.name} ({info.base})")
    seen: set[str] = set()
    for f in info.files:
        seen.add(f.uri)
        try:
            size = os.path.getsize(f.path)
            if size > MAX_FILE_BYTES:
                report.skipped.append((f.uri, f"larger than {MAX_FILE_BYTES // 1_048_576} MB"))
                continue
            with open(f.path, "rb") as fh:
                raw = fh.read()
            if is_binary(raw) and ext_of(f.path) != ".pdf":
                report.skipped.append((f.uri, "binary"))
                continue
            raw_sha = hashlib.sha256(raw).hexdigest()
            existing = store.get_source(f.uri)
            if existing is not None and existing["current_snapshot"] and not existing["removed"]:
                snap = store.one(
                    "SELECT raw_sha256, converter FROM snapshot WHERE id = ?",
                    (existing["current_snapshot"],),
                )
                if snap is not None and snap["raw_sha256"] == raw_sha:
                    report.unchanged += 1
                    continue
            conv = convert(raw, f.path, f.uri, f.kind, src.parser)
            snap_id, _created = store.add_snapshot(
                uri=f.uri,
                path=f.path,
                kind=f.kind,
                root=info.base,
                raw_sha256=raw_sha,
                converted=conv,
                git_commit=info.git_commit,
            )
            if existing is None:
                report.added += 1
            elif existing["current_snapshot"] != snap_id:
                report.updated += 1
            else:
                report.unchanged += 1
            if progress:
                progress(f.uri)
        except Exception as exc:  # one bad file must not stop an ingest
            report.errors.append((f.uri, f"{type(exc).__name__}: {exc}"))
    report.removed += store.mark_removed(info.base, seen) if not _is_partial(src) else 0
    return report


def _is_partial(src: SourceConfig) -> bool:
    """Single files and globs share a base dir with siblings; only whole directories may mark
    files that disappeared as removed."""
    return not os.path.isdir(os.path.expanduser(src.path))


def ingest_all(
    store: Store, sources: list[SourceConfig], progress: Callable[[str], None] | None = None
) -> IngestReport:
    report = IngestReport()
    for src in sources:
        try:
            ingest_source(store, src, report, progress)
        except FileNotFoundError as exc:
            report.errors.append((src.path, str(exc)))
    return report
