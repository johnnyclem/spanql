"""HTTP API (docs/HARNESS.md §4): REST + SSE, OpenAI-compatible shim, and the web UI."""

from __future__ import annotations

import asyncio
import hmac
import json
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route

from spanql import __version__
from spanql.config import SourceConfig
from spanql.services import Services
from spanql.store.db import SpanError

UI_FILE = Path(__file__).with_name("ui.html")
OPEN_PATHS = {"/v1/health", "/", "/favicon.ico"}


class TokenAuth(BaseHTTPMiddleware):
    def __init__(self, app: Any, token: str | None):
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if self.token and request.url.path not in OPEN_PATHS:
            got = request.headers.get("authorization", "")
            if not hmac.compare_digest(got.encode(), f"Bearer {self.token}".encode()):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


class Metrics:
    def __init__(self) -> None:
        self.asks = Counter[str]()
        self.attempts = 0
        self.latency_sum = 0.0
        self.problems = Counter[str]()

    def observe(self, result: dict[str, Any]) -> None:
        self.asks[result["status"]] += 1
        self.attempts += int(result.get("attempts", 0))
        self.latency_sum += float(result.get("seconds", 0))
        for a in result.get("attempt_log", []):
            for p in a.get("problems", []):
                self.problems[p["code"]] += 1

    def render(self) -> str:
        lines = ["# TYPE spanql_asks_total counter"]
        for k, v in sorted(self.asks.items()):
            lines.append(f'spanql_asks_total{{status="{k}"}} {v}')
        lines += [
            "# TYPE spanql_attempts_total counter",
            f"spanql_attempts_total {self.attempts}",
            "# TYPE spanql_ask_seconds_sum counter",
            f"spanql_ask_seconds_sum {self.latency_sum:.3f}",
            "# TYPE spanql_verifier_failures_total counter",
        ]
        for k, v in sorted(self.problems.items()):
            lines.append(f'spanql_verifier_failures_total{{code="{k}"}} {v}')
        return "\n".join(lines) + "\n"


def _err(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


async def _json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        return {}
    return body if isinstance(body, dict) else {}


def create_app(svc: Services) -> Starlette:
    metrics = Metrics()
    jobs: dict[str, dict[str, Any]] = {}
    lock = threading.Lock()  # the harness and store are used from worker threads

    def run_sync(fn: Any, *a: Any, **kw: Any) -> Any:
        def call() -> Any:
            with lock:
                return fn(*a, **kw)

        return asyncio.to_thread(call)

    async def health(_: Request) -> Response:
        return JSONResponse({"ok": True, "version": __version__})

    async def status(_: Request) -> Response:
        return JSONResponse(await run_sync(svc.status))

    async def sources(request: Request) -> Response:
        if request.method == "POST":
            body = await _json(request)
            if "path" not in body:
                return _err("path is required")
            src = SourceConfig(
                path=str(body["path"]),
                kind=str(body.get("kind", "auto")),
                name=body.get("name"),
                watch=bool(body.get("watch", False)),
                parser=str(body.get("parser", "auto")),
            )
            svc.config.sources.append(src)
            job = _start_job([src])
            return JSONResponse({"source": src.__dict__, "job_id": job}, status_code=202)
        rows = await run_sync(svc.store.list_sources)
        return JSONResponse(
            {
                "sources": [dict(r) for r in rows],
                "configured": [s.__dict__ for s in svc.config.sources],
            }
        )

    def _start_job(srcs: list[SourceConfig] | None) -> str:
        jid = "j_" + uuid.uuid4().hex[:10]
        jobs[jid] = {"id": jid, "status": "running", "started": time.time()}

        def work() -> None:
            try:
                with lock:
                    rep = svc.ingest(srcs)
                jobs[jid].update(status="done", report=rep.to_dict())
            except Exception as exc:  # report, don't crash the server
                jobs[jid].update(status="error", error=str(exc))

        threading.Thread(target=work, daemon=True).start()
        return jid

    async def ingest(request: Request) -> Response:
        body = await _json(request)
        srcs = None
        if body.get("path"):
            srcs = [SourceConfig(path=str(body["path"]), kind=str(body.get("kind", "auto")))]
        return JSONResponse({"job_id": _start_job(srcs)}, status_code=202)

    async def job(request: Request) -> Response:
        j = jobs.get(request.path_params["job_id"])
        return JSONResponse(j) if j else _err("unknown job", 404)

    async def ask(request: Request) -> Response:
        body = await _json(request)
        q = str(body.get("question", "")).strip()
        if not q:
            return _err("question is required")
        scope = body.get("scope") or None
        conv = body.get("conversation_id")
        if request.query_params.get("stream") in ("1", "true"):
            return _ask_stream(q, scope, conv)
        res = await run_sync(svc.ask, q, scope=scope, conversation_id=conv)
        out = res.to_dict()
        metrics.observe(out)
        # a failed model backend is a 503, never a 200 "not found"
        return JSONResponse(out, status_code=503 if out["status"] == "error" else 200)

    def _ask_stream(q: str, scope: list[str] | None, conv: str | None) -> StreamingResponse:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

        def emit(kind: str, data: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, (kind, data))

        def work() -> None:
            try:
                with lock:
                    res = svc.ask(q, scope=scope, conversation_id=conv, on_event=emit)
                metrics.observe(res.to_dict())
            except Exception as exc:
                emit("error", {"message": str(exc)})
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=work, daemon=True).start()

        async def gen() -> Any:
            while True:
                item = await queue.get()
                if item is None:
                    break
                kind, data = item
                yield f"event: {kind}\ndata: {json.dumps(data, default=str)}\n\n"

        return StreamingResponse(
            gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    async def search(request: Request) -> Response:
        body = await _json(request)
        q = str(body.get("query", "")).strip()
        if not q:
            return _err("query is required")
        r = await run_sync(svc.search, q, scope=body.get("scope"), k=int(body.get("k", 10)))
        return JSONResponse(r.to_dict())

    async def evaluate(request: Request) -> Response:
        body = await _json(request)
        f = str(body.get("formula", "")).strip()
        if not f:
            return _err("formula is required")
        try:
            out = await run_sync(
                svc.evaluate, f, body.get("handles") or {}, question=body.get("question")
            )
        except SpanError as exc:
            return _err(str(exc))
        return JSONResponse(out.to_dict(), status_code=200 if out.ok else 422)

    async def cell(request: Request) -> Response:
        c = await run_sync(svc.store.get_cell, request.path_params["cell_id"])
        if c is None:
            return _err("unknown cell", 404)
        summary = {k: c[k] for k in ("id", "formula", "value", "status", "evaluated_at")}
        summary.update(await run_sync(svc.staleness, c))
        return JSONResponse(summary)

    async def trace(request: Request) -> Response:
        c = await run_sync(svc.store.get_cell, request.path_params["cell_id"])
        if c is None:
            return _err("unknown cell", 404)
        c = dict(c)
        c.update(await run_sync(svc.staleness, c))
        return JSONResponse(c)

    async def recompute(request: Request) -> Response:
        try:
            return JSONResponse(await run_sync(svc.recompute, request.path_params["cell_id"]))
        except KeyError:
            return _err("unknown cell", 404)

    async def read_span(request: Request) -> Response:
        try:
            ctx = int(request.query_params.get("context", "2"))
            return JSONResponse(
                await run_sync(svc.store.read_with_context, request.path_params["span_id"], ctx)
            )
        except (SpanError, ValueError) as exc:
            return _err(str(exc), 404)

    async def runs(_: Request) -> Response:
        return JSONResponse({"runs": await run_sync(svc.store.list_runs, 50)})

    async def run_detail(request: Request) -> Response:
        r = await run_sync(svc.store.get_run, request.path_params["run_id"])
        return JSONResponse(r) if r else _err("unknown run", 404)

    async def metrics_ep(_: Request) -> Response:
        return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")

    # ------------------------------------------------- OpenAI-compatible shim
    async def models(_: Request) -> Response:
        return JSONResponse(
            {
                "object": "list",
                "data": [{"id": "spanql", "object": "model", "owned_by": "spanql", "created": 0}],
            }
        )

    async def chat_completions(request: Request) -> Response:
        body = await _json(request)
        msgs = body.get("messages") or []
        users = [m for m in msgs if m.get("role") == "user"]
        if not users:
            return _err("messages must include a user message")
        content = users[-1].get("content")
        if isinstance(content, list):  # content parts
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        q = str(content or "").strip()
        res = await run_sync(svc.ask, q, conversation_id=body.get("user"))
        out = res.to_dict()
        metrics.observe(out)
        if out["status"] == "error":
            return JSONResponse(
                {"error": {"message": out["answer"]["text"], "type": "backend_error"}},
                status_code=503,
            )
        text = _chat_text(out)
        cid = "chatcmpl-" + uuid.uuid4().hex[:16]
        created = int(time.time())
        ext = {k: out[k] for k in ("status", "cells", "formula", "search_report", "run_id")}
        if body.get("stream"):

            async def gen() -> Any:
                chunk = {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": "spanql",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": text},
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(chunk)}\n\n"
                done = dict(
                    chunk, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}], spanql=ext
                )
                yield f"data: {json.dumps(done, default=str)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(gen(), media_type="text/event-stream")
        return JSONResponse(
            {
                "id": cid,
                "object": "chat.completion",
                "created": created,
                "model": "spanql",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                "spanql": ext,
            }
        )

    async def ui(_: Request) -> Response:
        return HTMLResponse(UI_FILE.read_text(encoding="utf-8"))

    routes = [
        Route("/", ui),
        Route("/v1/health", health),
        Route("/v1/status", status),
        Route("/v1/sources", sources, methods=["GET", "POST"]),
        Route("/v1/ingest", ingest, methods=["POST"]),
        Route("/v1/jobs/{job_id}", job),
        Route("/v1/ask", ask, methods=["POST"]),
        Route("/v1/search", search, methods=["POST"]),
        Route("/v1/evaluate", evaluate, methods=["POST"]),
        Route("/v1/cells/{cell_id}", cell),
        Route("/v1/cells/{cell_id}/trace", trace),
        Route("/v1/cells/{cell_id}/recompute", recompute, methods=["POST"]),
        Route("/v1/spans/{span_id}", read_span),
        Route("/v1/runs", runs),
        Route("/v1/runs/{run_id}", run_detail),
        Route("/v1/models", models),
        Route("/v1/chat/completions", chat_completions, methods=["POST"]),
        Route("/metrics", metrics_ep),
    ]
    return Starlette(
        routes=routes, middleware=[Middleware(TokenAuth, token=svc.config.server.api_token)]
    )


def _chat_text(out: dict[str, Any]) -> str:
    text = out["answer"]["text"]
    if out["status"] != "grounded":
        return text
    notes = []
    for i, c in enumerate(out["cells"], start=1):
        srcs = [f"{x['source']}:L{x['line']}" for x in c["inputs"] if x.get("kind") == "span"]
        qs = [
            f"{x['count']} matches of {x['query']}" for x in c["inputs"] if x.get("kind") == "query"
        ]
        notes.append(
            f"[{i}] {c['value']['display']} = {c['formula']} → "
            + "; ".join(srcs[:3] + qs[:2])
            + f" (cell {c['id']})"
        )
    return text + "\n\n" + "\n".join(notes)
