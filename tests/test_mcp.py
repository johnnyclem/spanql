from __future__ import annotations

import io
import json

from spanql.mcp.server import MCPServer
from spanql.services import Services


def rpc(srv: MCPServer, method: str, params=None, mid=1):
    return srv.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})


def test_initialize_and_list(services: Services) -> None:
    srv = MCPServer(services)
    init = rpc(srv, "initialize", {"protocolVersion": "2025-06-18"})
    assert init["result"]["serverInfo"]["name"] == "spanql"
    tools = {t["name"] for t in rpc(srv, "tools/list")["result"]["tools"]}
    assert tools == {"ask", "search", "evaluate", "explain_cell", "read_span", "status"}
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert rpc(srv, "nope")["error"]["code"] == -32601


def test_tools(services: Services) -> None:
    srv = MCPServer(services)
    ask = rpc(
        srv,
        "tools/call",
        {"name": "ask", "arguments": {"question": "When was the Q3 report published?"}},
    )
    res = ask["result"]
    assert not res["isError"] and "2026-11-03" in res["content"][0]["text"]
    cid = res["structuredContent"]["answer"]["cells"][0]
    ex = rpc(srv, "tools/call", {"name": "explain_cell", "arguments": {"cell_id": cid}})["result"]
    assert "DATE(" in ex["content"][0]["text"]
    span = next(i for i in ex["structuredContent"]["inputs"] if i["kind"] == "span")["id"]
    rs = rpc(srv, "tools/call", {"name": "read_span", "arguments": {"span_id": span}})["result"]
    assert "November 3, 2026" in rs["content"][0]["text"]
    bad = rpc(srv, "tools/call", {"name": "evaluate", "arguments": {"formula": "ANSWER(1000)"}})
    assert "V4" in bad["result"]["content"][0]["text"]
    err = rpc(srv, "tools/call", {"name": "nope", "arguments": {}})["result"]
    assert err["isError"]


def test_stdio_loop(services: Services) -> None:
    srv = MCPServer(services)
    inp = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        + "\n\nnot json\n"
        + json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "status", "arguments": {}},
            }
        )
        + "\n"
    )
    out = io.StringIO()
    srv.serve(inp, out)
    msgs = [json.loads(ln) for ln in out.getvalue().splitlines()]
    assert msgs[0] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert any("error" in m and m["error"]["code"] == -32700 for m in msgs)
    assert "7 sources" in msgs[-1]["result"]["content"][0]["text"]
