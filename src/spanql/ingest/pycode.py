"""Python symbol segmentation using the stdlib ``ast`` (tree-sitter is the planned
upgrade for other languages; see docs/IMPLEMENTATION.md)."""

from __future__ import annotations

import ast

from ..blocks import Block
from .logs import split_identifiers

_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return parts[0] if parts else None  # call on an expression: keep the final attribute only


def direct_calls(nodes: list[ast.AST]) -> list[str]:
    """Dotted names of calls in ``nodes``, not descending into nested defs/classes."""
    found: set[str] = set()
    stack = [n for n in nodes if not isinstance(n, _DEFS)]
    while stack:
        n = stack.pop()
        if isinstance(n, ast.Call):
            name = _dotted(n.func)
            if name:
                found.add(name)
        for child in ast.iter_child_nodes(n):
            if not isinstance(child, _DEFS):
                stack.append(child)
    return sorted(found)


def segment_python(data: bytes, module_name: str) -> list[Block] | None:
    """Module/class/function/method blocks with call lists, or None if the file does not parse."""
    try:
        tree = ast.parse(data.decode("utf-8"))
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    starts = [0]
    pos = data.find(b"\n")
    while pos != -1:
        starts.append(pos + 1)
        pos = data.find(b"\n", pos + 1)

    def off(lineno: int, col: int) -> int:
        return min(len(data), starts[lineno - 1] + col)

    blocks: list[Block] = []
    children: dict[int, list[tuple[int, int]]] = {}  # block index -> child (start, end)

    def own_text(start: int, end: int, idx: int) -> str:
        pieces, cur = [], start
        for cs, ce in sorted(children.get(idx, [])):
            pieces.append(data[cur:cs])
            cur = max(cur, ce)
        pieces.append(data[cur:end])
        return b"".join(pieces).decode("utf-8", "replace")

    # module block first so children can reference index 0
    mod_start, mod_end = 0, len(data)
    while mod_start < mod_end and data[mod_start] in b" \t\r\n":
        mod_start += 1
    while mod_end > mod_start and data[mod_end - 1] in b" \t\r\n":
        mod_end -= 1
    if mod_end <= mod_start:
        return []
    blocks.append(
        Block("module", mod_start, mod_end,
              symbol={"lang": "python", "kind": "module", "name": module_name.rsplit(".", 1)[-1],
                      "qualified_name": module_name, "parent": None,
                      "calls": direct_calls(list(ast.iter_child_nodes(tree)))})
    )

    def walk(node: ast.AST, parent: int, path: list[str], in_class: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, _DEFS):
                walk(child, parent, path, in_class)
                continue
            first_line = min([child.lineno] + [d.lineno for d in child.decorator_list])
            s = starts[first_line - 1]
            while s < len(data) and data[s] in b" \t":
                s += 1
            e = off(child.end_lineno, child.end_col_offset)
            kind = "class" if isinstance(child, ast.ClassDef) else ("method" if in_class else "function")
            new_path = path + [child.name]
            idx = len(blocks)
            blocks.append(
                Block(kind, s, e, path=".".join(new_path),
                      symbol={"lang": "python", "kind": kind, "name": child.name,
                              "qualified_name": f"{module_name}.{'.'.join(new_path)}", "parent": parent,
                              "calls": direct_calls(child.body)})
            )
            children.setdefault(parent, []).append((s, e))
            walk(child, idx, new_path, isinstance(child, ast.ClassDef))

    try:
        walk(tree, 0, [], False)
    except RecursionError:
        return None
    for i, b in enumerate(blocks):
        b.fts = (own_text(b.start, b.end, i) + "\n" + b.symbol["qualified_name"].replace(".", " ")
                 + "\n" + split_identifiers(b.symbol["name"] + " " + " ".join(b.symbol["calls"])))
    return blocks
