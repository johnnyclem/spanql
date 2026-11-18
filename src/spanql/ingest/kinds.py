"""Which converter handles which file."""

from __future__ import annotations

from pathlib import PurePosixPath

KINDS = ("doc", "log", "file", "code")

_DOC = {".md": "markdown", ".markdown": "markdown", ".txt": "text", ".rst": "text", ".text": "text"}
_LOG = {".log": "log", ".jsonl": "log", ".ndjson": "log"}
_FILE = {".csv": "csv", ".tsv": "tsv", ".json": "json", ".yaml": "lines", ".yml": "lines", ".toml": "lines",
         ".ini": "lines", ".cfg": "lines", ".conf": "lines", ".properties": "lines", ".env": "lines"}
_CODE = {".py": "python"}
_CODE_GENERIC = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".swift", ".c", ".h",
                 ".cc", ".cpp", ".hpp", ".cs", ".rb", ".php", ".sh", ".bash", ".zsh", ".sql", ".scala", ".lua",
                 ".m", ".mm", ".r", ".pl", ".dart"}
_CODE_NAMES = {"dockerfile", "makefile", "rakefile", "gemfile"}

# Recognised but not convertible without the Docling extra (roadmap M8).
UNSUPPORTED = {".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm", ".epub", ".odt", ".rtf"}

_DEFAULT_CONVERTER = {"doc": "text", "log": "log", "file": "lines", "code": "generic"}


def classify(rel_path: str, override: str | None = None) -> tuple[str, str] | None:
    """Return (source kind, converter) or None if the file type is unsupported."""
    p = PurePosixPath(rel_path)
    ext = p.suffix.lower()
    if ext in UNSUPPORTED:
        return None
    detected: tuple[str, str] | None = None
    if ext in _DOC:
        detected = ("doc", _DOC[ext])
    elif ext in _LOG:
        detected = ("log", "log")
    elif ext in _FILE:
        detected = ("file", _FILE[ext])
    elif ext in _CODE:
        detected = ("code", _CODE[ext])
    elif ext in _CODE_GENERIC or p.name.lower() in _CODE_NAMES:
        detected = ("code", "generic")
    if override:
        if override not in KINDS:
            raise ValueError(f"unknown kind {override!r}; expected one of {', '.join(KINDS)}")
        if detected and detected[0] == override:
            return detected
        if override == "log" and ext == ".jsonl":
            return ("log", "log")
        return (override, _DEFAULT_CONVERTER[override])
    return detected or ("file", "lines")  # unknown text files are searchable line by line


def jsonl_parser_hint(rel_path: str) -> str | None:
    return "jsonl" if PurePosixPath(rel_path).suffix.lower() in (".jsonl", ".ndjson") else None
