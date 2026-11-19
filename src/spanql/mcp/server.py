"""Minimal MCP server over stdio (newline-delimited JSON-RPC 2.0), dependency-free.

Exposes ``search``, ``ask``, ``evaluate``, ``explain_cell``, ``read_span`` and ``status`` as
tools so coding agents can ask grounded questions and get values with traces.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from spanql import __version__
from spanql.services import Services

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

TOOLS: list[dict[str, Any]] = [
    {
        "name": "ask",
        "description": "Ask a question of the ingested docs/logs/files/code. Returns an answer "
        "whose every value is a traceable cell, or an explicit not_found.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "scope": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "source globs, e.g. ['payments/**']",
                },
            },
            "required": ["question"],
        },
    },
    {
        "name": "search",
        "description": "Full-text search over current sources; returns candidate spans.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "default": 10},
                "scope": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["query"],
        },
    },
    {
        "name": "evaluate",
        "description": "Evaluate a spanql formula. handles maps '1' -> span id (sp:…).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "formula": {"type": "string"},
                "handles": {"type": "object", "additionalProperties": {"type": "string"}},
            },
            "required": ["formula"],
        },
    },
    {
        "name": "explain_cell",
        "description": "Full trace for a cell id: formula, steps, input spans, staleness.",
        "inputSchema": {
            "type": "object",
            "properties": {"cell_id": {"type": "string"}},
            "required": ["cell_id"],
        },
    },
    {
        "name": "read_span",
        "description": "Exact text and location for a span id, with surrounding lines.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "span_id": {"type": "string"},
                "context": {"type": "integer", "default": 2},
            },
            "required": ["span_id"],
        },
    },
    {
        "name": "status",
        "description": "Sources, snapshot counts and model status.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class MCPServer:
    def __init__(self, svc: Services):
        self.svc = svc

    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if mid is None:  # notification
            return None
        try:
            if method == "initialize":
                want = params.get("protocolVersion")
                version = want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
                result: Any = {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "spanql", "version": __version__},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = self.call(params.get("name", ""), params.get("arguments") or {})
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": mid,
                    "error": {"code": -32601, "message": f"method not found: {method}"},
                }
        except Exception as exc:  # protocol-level failure
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(exc)}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        svc = self.svc
        try:
            if name == "ask":
                data: Any = svc.ask(str(args["question"]), scope=args.get("scope")).to_dict()
                text = data["answer"]["text"]
            elif name == "search":
                data = svc.search(
                    str(args["query"]), scope=args.get("scope"), k=int(args.get("k", 10))
                ).to_dict()
                text = (
                    "\n".join(
                        f"{c['handle']} {c['source']} {c['location']} {c['span']}: "
                        f"{c['text'][:200]}"
                        for c in data["candidates"]
                    )
                    or "no matches"
                )
            elif name == "evaluate":
                out = svc.evaluate(str(args["formula"]), args.get("handles") or {})
                data = out.to_dict()
                text = (
                    out.answer
                    if out.ok and out.answer
                    else (out.reason or "; ".join(str(p) for p in out.problems) or out.status)
                )
            elif name == "explain_cell":
                cell = svc.store.get_cell(str(args["cell_id"]))
                if cell is None:
                    raise KeyError(f"unknown cell {args['cell_id']}")
                data = {**cell, **svc.staleness(cell)}
                text = f"{cell['value']['display']} = {cell['formula']}"
            elif name == "read_span":
                data = svc.store.read_with_context(
                    str(args["span_id"]), int(args.get("context", 2))
                )
                text = f"{data['source']}:L{data['line_start']}: {data['text']}"
            elif name == "status":
                data = svc.status()
                text = f"{data['store']['sources']} sources, model {data['model']['backend']}"
            else:
                raise KeyError(f"unknown tool {name}")
        except Exception as exc:
            return {"content": [{"type": "text", "text": f"error: {exc}"}], "isError": True}
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": data,
            "isError": False,
        }

    def serve(self, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                resp: dict[str, Any] | None = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "parse error"},
                }
            else:
                resp = (
                    self.handle(msg)
                    if isinstance(msg, dict)
                    else {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32600, "message": "invalid request"},
                    }
                )
            if resp is not None:
                stdout.write(json.dumps(resp, default=str) + "\n")
                stdout.flush()
