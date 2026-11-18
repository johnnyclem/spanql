"""Ingestion: discover -> convert -> normalize -> snapshot -> segment -> index."""

from __future__ import annotations

import glob as _glob
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..blocks import Document
from ..spans import sha256_hex
from ..store import Store, commit_version, mark_deleted
from .discover import discover, git_commit
from .kinds import classify, jsonl_parser_hint
from .logs import segment_log
from .normalize import normalize
from .pycode import segment_python
from .tables import segment_csv, segment_json
from .textseg import segment_chunks, segment_lines, segment_prose

SEGMENTER_VERSION = 1
DEFAULT_MAX_FILE_BYTES = 64 * 1024 * 1024


@dataclass(slots=True)
class IngestReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: int = 0
    removed: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)

    def to_json(self) -> dict:
        return {
            "added": self.added, "updated": self.updated, "unchanged": self.unchanged, "removed": self.removed,
            "skipped": [{"path": p, "reason": r} for p, r in self.skipped],
            "errors": [{"path": p, "error": e} for p, e in self.errors],
        }


def module_name(rel_path: str) -> str:
    p = re.sub(r"\.py$", "", rel_path).replace("/", ".")
    return p[: -len(".__init__")] if p.endswith(".__init__") else p


def convert(
    raw: bytes, kind: str, converter: str, rel_path: str, *, parser: str = "auto", year: int | None = None
) -> Document:
    """Raw bytes -> canonical text + blocks."""
    norm = normalize(raw)
    data = norm.data
    tag = f"{kind}:{converter}"
    if kind == "doc":
        blocks = segment_prose(data, markdown=converter == "markdown")
    elif kind == "log":
        parser = jsonl_parser_hint(rel_path) or parser
        blocks, used = segment_log(data, parser, year=year)
        tag = f"log:{used}"
    elif kind == "file" and converter in ("csv", "tsv"):
        blocks = segment_csv(data, delim=b"\t" if converter == "tsv" else b",")
        if not blocks:
            blocks, tag = segment_lines(data), "file:lines"
    elif kind == "file" and converter == "json":
        blocks = segment_json(data)
        if blocks is None:
            blocks, tag = segment_lines(data), "file:lines"
    elif kind == "code":
        blocks = segment_python(data, module_name(rel_path)) if converter == "python" else None
        if blocks is None:
            blocks, tag = segment_chunks(data), "code:chunks"
        blocks = blocks + segment_lines(data, fts=False)  # grep-style unit, not FTS-indexed
    else:
        blocks = segment_lines(data)
    return Document(data=data, blocks=blocks, segmentation=f"{tag}@{SEGMENTER_VERSION}", offset_map=norm.offset_map)


def _expand(path: str) -> tuple[Path, list[Path] | None, bool]:
    """-> (root, explicit file list for glob inputs or None, is_single_file)."""
    expanded = str(Path(path).expanduser())
    if any(c in expanded for c in "*?["):
        parts = Path(expanded).parts
        fixed: list[str] = []
        for part in parts:
            if any(c in part for c in "*?["):
                break
            fixed.append(part)
        root = Path(*fixed) if fixed else Path(".")
        files = sorted(Path(m) for m in _glob.glob(expanded, recursive=True) if Path(m).is_file())
        return root.resolve(), [f.resolve() for f in files], False
    p = Path(expanded).resolve()
    if not p.exists():
        raise FileNotFoundError(f"no such file or directory: {path}")
    return (p.parent if p.is_file() else p), None, p.is_file()


def ingest_path(
    store: Store,
    path: str,
    *,
    kind: str | None = None,
    name: str | None = None,
    parser: str = "auto",
    force: bool = False,
    respect_gitignore: bool = True,
    remove_missing: bool = True,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    year: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> IngestReport:
    """Ingest a file, directory or glob. Re-ingesting unchanged files is a no-op."""
    root, explicit, single = _expand(path)
    report = IngestReport()
    if explicit is not None:
        files = explicit
        prefix = name or root.name
    elif single:
        files = [Path(path).expanduser().resolve()]
        prefix = None
    else:
        files = discover(root, respect_gitignore=respect_gitignore)
        prefix = name or root.name
    commit = git_commit(root) if explicit is None and not single else None
    seen: set[str] = set()

    for f in files:
        try:
            rel = f.relative_to(root).as_posix()
        except ValueError:
            rel = f.name
        uri = (name or f.name) if single else f"{prefix}/{rel}"
        try:
            cls = classify(rel, kind)
            if cls is None:
                report.skipped.append((uri, f"unsupported type {f.suffix or '(none)'} (needs the Docling extra)"))
                continue
            if f.stat().st_size > max_file_bytes:
                report.skipped.append((uri, f"larger than {max_file_bytes // (1024 * 1024)} MB"))
                continue
            raw = f.read_bytes()
            if b"\0" in raw[:8192]:
                report.skipped.append((uri, "binary file"))
                continue
            seen.add(uri)
            raw_hash = sha256_hex(raw)
            existing = store.get_source(uri)
            if existing and not force and existing.kind == cls[0] and existing.current_snapshot:
                cur = store.db.execute(
                    "SELECT raw_sha256 FROM source_snapshot WHERE source_id=? AND superseded_at IS NULL",
                    (existing.id,),
                ).fetchone()
                if cur and cur["raw_sha256"] == raw_hash:
                    report.unchanged += 1
                    continue
            doc = convert(raw, cls[0], cls[1], rel, parser=parser, year=year)
            res = commit_version(
                store, uri=uri, kind=cls[0], root=str(root), rel_path=rel, raw_sha256=raw_hash,
                raw_size=len(raw), doc=doc, git_commit=commit,
            )
            (report.updated if existing and existing.current_snapshot else report.added).append(uri)
            if progress:
                progress(uri)
        except Exception as e:  # one bad file must not abort the run
            report.errors.append((uri, f"{type(e).__name__}: {e}"))

    # Only a whole-directory ingest can tell that a file vanished (a glob or single file says nothing
    # about its siblings).
    if remove_missing and explicit is None and not single:
        for s in store.list_sources():
            if s.root == str(root) and s.uri.startswith(prefix + "/") and s.uri not in seen:
                mark_deleted(store, s.uri)
                report.removed.append(s.uri)
    return report
