"""Code converter: segment source files into symbols (module/class/function/method) with
names, kinds, line ranges and called names.

- Python uses the stdlib ``ast`` module (exact, no extra dependency).
- TypeScript/JavaScript/Go use tree-sitter when the ``code`` extra is installed, and a
  conservative regex fallback otherwise.
- Other languages are split into line chunks.
Lines outside any top-level symbol become ``chunk`` blocks so nothing is unsearchable.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any

from spanql.blocks import BlockSpec, Converted
from spanql.ingest.text import ByteIndex, line_spans, normalize

LANG_BY_EXT = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".mts": "typescript",
    ".cts": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
}
CHUNK_LINES = 40


@dataclass
class Sym:
    kind: str
    name: str
    qualified: str
    line_start: int  # 1-based inclusive
    line_end: int
    calls: set[str] = field(default_factory=set)
    parent: int | None = None  # index into symbol list
    col_start: int = 0


def module_name(uri: str) -> str:
    path = uri.split("/", 1)[1] if "/" in uri else uri
    for ext in LANG_BY_EXT:
        if path.endswith(ext):
            path = path[: -len(ext)]
            break
    parts = [p for p in path.split("/") if p]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or "__root__"


# ------------------------------------------------------------------------ python
class _PyCalls(ast.NodeVisitor):
    def __init__(self) -> None:
        self.calls: set[str] = set()

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        if isinstance(f, ast.Name):
            self.calls.add(f.id)
        elif isinstance(f, ast.Attribute):
            self.calls.add(f.attr)
        self.generic_visit(node)


def python_symbols(text: str, mod: str) -> list[Sym] | None:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    out: list[Sym] = []

    def walk(body: list[ast.stmt], prefix: str, parent: int | None, in_class: bool) -> None:
        for node in body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                is_class = isinstance(node, ast.ClassDef)
                kind = "class" if is_class else ("method" if in_class else "function")
                cv = _PyCalls()
                if not is_class:
                    for stmt in node.body:
                        cv.visit(stmt)
                qn = f"{prefix}.{node.name}"
                out.append(
                    Sym(
                        kind, node.name, qn, start, node.end_lineno or node.lineno, cv.calls, parent
                    )
                )
                walk(node.body, qn, len(out) - 1, is_class)

    walk(tree.body, mod, None, False)
    return out


# ------------------------------------------------------------------- tree-sitter
_TS_DEF_NODES = {
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "method_definition": "method",
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "interface_declaration": "interface",
    "method_declaration": "method",  # go
    "type_spec": "type",  # go
}


def _ts_language(lang: str) -> Any | None:
    try:
        from tree_sitter import Language
    except ImportError:
        return None
    try:
        if lang in ("typescript", "tsx"):
            import tree_sitter_typescript as tst

            return Language(tst.language_tsx() if lang == "tsx" else tst.language_typescript())
        if lang == "javascript":
            import tree_sitter_javascript as tsj

            return Language(tsj.language())
        if lang == "go":
            import tree_sitter_go as tsg

            return Language(tsg.language())
    except (ImportError, AttributeError, ValueError):
        return None
    return None


def treesitter_available(lang: str) -> bool:
    return _ts_language(lang) is not None


def _ts_name(node: Any, src: bytes) -> str | None:
    n = node.child_by_field_name("name")
    if n is None:
        return None
    return src[n.start_byte : n.end_byte].decode("utf-8", errors="replace")


def _ts_calls(node: Any, src: bytes) -> set[str]:
    calls: set[str] = set()
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.type == "call_expression":
            fn = cur.child_by_field_name("function")
            if fn is not None:
                if fn.type in ("member_expression", "selector_expression"):
                    prop = fn.child_by_field_name("property") or fn.child_by_field_name("field")
                    fn = prop if prop is not None else fn
                if fn.type in ("identifier", "property_identifier", "field_identifier"):
                    calls.add(src[fn.start_byte : fn.end_byte].decode("utf-8", "replace"))
        stack.extend(cur.children)
    return calls


def treesitter_symbols(text: str, lang: str, mod: str) -> list[Sym] | None:
    language = _ts_language(lang)
    if language is None:
        return None
    from tree_sitter import Parser

    parser = Parser(language)
    src = text.encode("utf-8")
    tree = parser.parse(src)
    out: list[Sym] = []

    def visit(node: Any, prefix: str, parent: int | None, in_class: bool) -> None:
        for child in node.children:
            kind = _TS_DEF_NODES.get(child.type)
            name = _ts_name(child, src) if kind else None
            target = child
            if kind is None and child.type in ("lexical_declaration", "variable_declaration"):
                # const foo = (...) => {...}
                for decl in child.children:
                    val = (
                        decl.child_by_field_name("value")
                        if decl.type == "variable_declarator"
                        else None
                    )
                    if val is not None and val.type in (
                        "arrow_function",
                        "function_expression",
                        "function",
                    ):
                        kind, name, target = "function", _ts_name(decl, src), child
                        break
            if kind == "method" and lang == "go":
                recv = child.child_by_field_name("receiver")
                rtype = (
                    re.sub(r"[^\w]", " ", src[recv.start_byte : recv.end_byte].decode()).split()[
                        -1:
                    ]
                    if recv
                    else []
                )
                prefix_here = f"{prefix}.{rtype[0]}" if rtype else prefix
            else:
                prefix_here = prefix
            if kind and name:
                if kind == "function" and in_class:
                    kind = "method"
                qn = f"{prefix_here}.{name}"
                calls = _ts_calls(target, src) if kind in ("function", "method") else set()
                out.append(
                    Sym(
                        kind,
                        name,
                        qn,
                        target.start_point[0] + 1,
                        target.end_point[0] + 1,
                        calls,
                        parent,
                    )
                )
                visit(target, qn, len(out) - 1, kind in ("class", "interface"))
            elif child.type in (
                "export_statement",
                "class_body",
                "program",
                "source_file",
                "type_declaration",
                "statement_block",
                "declaration_list",
            ):
                visit(child, prefix, parent, in_class)

    visit(tree.root_node, mod, None, False)
    return out


# ----------------------------------------------------------------- regex fallback
_FALLBACK = {
    "typescript": [
        (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(\w+)"), "class"),
        (
            re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(\w+)"),
            "function",
        ),
        (
            re.compile(
                r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*(?::[^=]+)?=>"
            ),
            "function",
        ),
        (re.compile(r"^\s*(?:export\s+)?interface\s+(\w+)"), "interface"),
    ],
    "go": [
        (re.compile(r"^func\s+\([^)]*\)\s*(\w+)\s*\("), "method"),
        (re.compile(r"^func\s+(\w+)\s*[\[(]"), "function"),
        (re.compile(r"^type\s+(\w+)\s+"), "type"),
    ],
}
_FALLBACK["tsx"] = _FALLBACK["javascript"] = _FALLBACK["typescript"]
_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
_KEYWORDS = {
    "if",
    "for",
    "while",
    "switch",
    "return",
    "function",
    "catch",
    "func",
    "typeof",
    "new",
    "await",
    "super",
    "import",
    "require",
}


def regex_symbols(text: str, lang: str, mod: str) -> list[Sym]:
    pats = _FALLBACK.get(lang, [])
    lines = text.split("\n")
    out: list[Sym] = []
    for i, line in enumerate(lines):
        for pat, kind in pats:
            m = pat.match(line)
            if not m:
                continue
            # end = matching brace balance from this line
            depth, end, seen = 0, i, False
            for j in range(i, len(lines)):
                depth += lines[j].count("{") - lines[j].count("}")
                seen = seen or "{" in lines[j]
                end = j
                if seen and depth <= 0:
                    break
            body = "\n".join(lines[i + 1 : end + 1])
            calls = {c for c in _CALL_RE.findall(body) if c not in _KEYWORDS}
            out.append(Sym(kind, m.group(1), f"{mod}.{m.group(1)}", i + 1, end + 1, calls))
            break
    return out


# ------------------------------------------------------------------------ convert
def convert_code(raw: bytes, uri: str, ext: str) -> Converted:
    text, omap = normalize(raw)
    lang = LANG_BY_EXT.get(ext)
    mod = module_name(uri)
    syms: list[Sym] | None = None
    engine = "chunks"
    if lang == "python":
        syms = python_symbols(text, mod)
        engine = "ast" if syms is not None else "chunks"
    elif lang is not None:
        syms = treesitter_symbols(text, lang, mod)
        engine = "tree-sitter"
        if syms is None:
            syms = regex_symbols(text, lang, mod)
            engine = "regex"
    return _build(text, omap, syms or [], lang or ext.lstrip(".") or "text", engine)


def _build(
    text: str, omap: list[tuple[int, int]] | None, syms: list[Sym], lang: str, engine: str
) -> Converted:
    bi = ByteIndex(text)
    lines = line_spans(text)
    blocks: list[BlockSpec] = []
    covered = [False] * len(lines)
    index_of: dict[int, int] = {}
    for si, sym in enumerate(syms):
        a = lines[sym.line_start - 1][0]
        b = lines[min(sym.line_end, len(lines)) - 1][1]
        if sym.parent is None:
            for k in range(sym.line_start - 1, min(sym.line_end, len(lines))):
                covered[k] = True
        index_of[si] = len(blocks)
        blocks.append(
            BlockSpec(
                "symbol",
                bi(a),
                bi(b),
                path=sym.qualified,
                symbol={
                    "lang": lang,
                    "kind": sym.kind,
                    "name": sym.name,
                    "qualified_name": sym.qualified,
                    "parent_index": index_of.get(sym.parent) if sym.parent is not None else None,
                    "calls": sorted(sym.calls),
                },
            )
        )
    # chunk uncovered lines
    run_start: int | None = None
    count = 0
    for k, (s, e) in enumerate([*lines, (len(text), len(text))]):
        blank = k == len(lines) or covered[k] or not text[s:e].strip()
        if run_start is not None and (blank or count >= CHUNK_LINES):
            a = lines[run_start][0]
            b = lines[k - 1][1]
            if text[a:b].strip():
                blocks.append(BlockSpec("chunk", bi(a), bi(b)))
            run_start, count = None, 0
        if not blank:
            if run_start is None:
                run_start = k
            count += 1
    return Converted(
        text,
        blocks,
        f"code-{engine}/1",
        omap,
        meta={"lang": lang, "symbols": len(syms), "engine": engine},
    )
