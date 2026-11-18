"""HTTP API (HARNESS.md section 4), stdlib only.

Implemented: sources, ingest jobs, ask, evaluate, cells (get / trace / recompute), runs, spans,
health, status. Not yet: SSE streaming, the OpenAI-compatible shim, the web UI (roadmap M5).
Each request opens its own SQLite connection (WAL), so the server is thread-safe by construction.
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import __version__
from ..config import Config, ConfigError
from ..engine import Engine
from ..engine.recompute import recompute
from ..errors import SpanqlError, StoreError
from ..harness import Harness, HarnessError, evaluate_formula, list_runs
from ..ingest import ingest_path
from ..store import Store, describe_sources

MAX_BODY = 1_000_000
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class _HttpError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


class Jobs:
    """In-memory registry for background ingest jobs."""

    def __init__(self):
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def submit(self, fn) -> str:
        job_id = "j_" + secrets.token_hex(4)
        with self._lock:
            self._jobs[job_id] = {"id": job_id, "status": "running"}

        def run():
            try:
                result = fn()
                update = {"status": "done", "result": result}
            except Exception as e:  # report, never crash the worker silently
                update = {"status": "failed", "error": f"{type(e).__name__}: {e}"}
            with self._lock:
                self._jobs[job_id].update(update)

        threading.Thread(target=run, daemon=True).start()
        return job_id

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            j = self._jobs.get(job_id)
            return dict(j) if j else None


def check_bind_security(host: str, token: str | None) -> None:
    if host not in LOOPBACK and not token:
        raise ConfigError(f"binding to {host} exposes the API beyond this machine; set SPANQL_API_TOKEN "
                          "(or bind to 127.0.0.1)")


def make_server(cfg: Config, *, client=None, token: str | None = None, host: str | None = None,
                port: int | None = None) -> ThreadingHTTPServer:
    """Build (but do not start) the server. ``client`` overrides the configured model client."""
    bind_host, bind_port = cfg.bind()
    host = bind_host if host is None else host
    port = bind_port if port is None else port
    check_bind_security(host, token)
    if cfg.store_path == ":memory:" or cfg.data["store"]["path"] == ":memory:":
        raise ConfigError("the API needs a file-backed store (store.path), not :memory:")
    model_client = client if client is not None else cfg.make_client()
    jobs = Jobs()
    store_path = cfg.store_path
    Store(store_path).close()  # create/migrate once up front

    routes: list[tuple[str, re.Pattern[str], str]] = []

    def route(method: str, pattern: str):
        def deco(fn):
            routes.append((method, re.compile("^" + pattern + "$"), fn.__name__))
            return fn
        return deco

    class Handler(BaseHTTPRequestHandler):
        server_version = f"spanql/{__version__}"
        protocol_version = "HTTP/1.1"

        # -------------------------------------------------------------- plumbing
        def log_message(self, fmt, *args):  # quiet; no request logging of question text
            pass

        def _send(self, status: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise _HttpError(413, f"request body larger than {MAX_BODY} bytes")
            raw = self.rfile.read(n) if n else b""
            if not raw:
                return {}
            try:
                obj = json.loads(raw)
            except ValueError as e:
                raise _HttpError(400, f"invalid JSON body: {e}") from e
            if not isinstance(obj, dict):
                raise _HttpError(400, "JSON body must be an object")
            return obj

        def _authorized(self, path: str) -> bool:
            if token is None or path == "/v1/health":
                return True
            got = self.headers.get("Authorization", "")
            return got.startswith("Bearer ") and hmac.compare_digest(got[7:].encode(), token.encode())

        def _dispatch(self, method: str) -> None:
            url = urlparse(self.path)
            try:
                if not self._authorized(url.path):
                    raise _HttpError(401, "missing or invalid API token (Authorization: Bearer <token>)")
                for m, rx, name in routes:
                    match = rx.match(url.path)
                    if m == method and match:
                        body = self._body() if method == "POST" else {}
                        status, payload = getattr(self, name)(body, parse_qs(url.query), *match.groups())
                        return self._send(status, payload)
                if any(rx.match(url.path) for _, rx, _ in routes):
                    raise _HttpError(405, f"{method} not allowed here")
                raise _HttpError(404, f"no such endpoint: {url.path}")
            except _HttpError as e:
                self.close_connection = True  # the request body may be unread
                self._send(e.status, {"error": {"message": e.message, **e.extra}})
            except SpanqlError as e:
                self._send(422, {"error": e.to_json()})
            except HarnessError as e:
                self._send(503, {"error": {"message": str(e)}})
            except (StoreError, ConfigError, ValueError, FileNotFoundError) as e:
                self._send(400, {"error": {"message": str(e)}})
            except Exception as e:  # pragma: no cover - last resort, never leak a traceback to the client
                self.close_connection = True
                traceback.print_exc()
                self._send(500, {"error": {"message": f"internal error: {type(e).__name__}"}})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        # ---------------------------------------------------------------- routes
        @route("GET", "/v1/health")
        def health(self, body, qs):
            return 200, {"status": "ok", "version": __version__}

        @route("GET", "/v1/status")
        def status(self, body, qs):
            with Store(store_path) as s:
                st = s.status()
            return 200, {**st, "model": {"configured": model_client is not None,
                                         "endpoint": cfg.data["model"].get("endpoint"),
                                         "backend": cfg.data["model"].get("backend")}}

        @route("GET", "/v1/sources")
        def list_sources(self, body, qs):
            glob = (qs.get("glob") or [None])[0]
            with Store(store_path) as s:
                rows = s.db.execute("SELECT id, uri, kind, current_snapshot FROM source "
                                    "WHERE current_snapshot IS NOT NULL ORDER BY uri").fetchall()
                from ..globs import match_uri

                return 200, {"sources": [dict(r) for r in rows if not glob or match_uri(glob, r["uri"])],
                             "schemas": describe_sources(s, glob) if (qs.get("describe") or ["0"])[0] == "1" else None}

        @route("POST", "/v1/sources")
        def add_source(self, body, qs):
            path = body.get("path")
            if not isinstance(path, str) or not path:
                raise _HttpError(400, '"path" is required')
            if body.get("watch"):
                raise _HttpError(400, "watch mode is not implemented yet; re-ingest with POST /v1/sources")

            def run():
                with Store(store_path) as s:
                    return ingest_path(s, path, kind=body.get("kind"), name=body.get("name"),
                                       parser=body.get("parser", "auto"),
                                       respect_gitignore=bool(body.get("respect_gitignore", True))).to_json()

            return 202, {"job_id": jobs.submit(run)}

        @route("POST", r"/v1/ingest/(\d+)")
        def reingest(self, body, qs, source_id):
            with Store(store_path) as s:
                src = s.source_by_id(int(source_id))
            if src is None or not src.root:
                raise _HttpError(404, f"no source with id {source_id}")
            target = str(Path(src.root) / src.rel_path) if "/" not in src.uri else src.root
            name = src.uri if "/" not in src.uri else src.uri.split("/", 1)[0]

            def run():
                with Store(store_path) as s:
                    return ingest_path(s, target, name=name, force=bool(body.get("force"))).to_json()

            return 202, {"job_id": jobs.submit(run)}

        @route("GET", "/v1/jobs/([A-Za-z0-9_]+)")
        def job(self, body, qs, job_id):
            j = jobs.get(job_id)
            if j is None:
                raise _HttpError(404, f"no job {job_id}")
            return 200, j

        @route("POST", "/v1/ask")
        def ask(self, body, qs):
            q = body.get("question")
            if not isinstance(q, str) or not q.strip():
                raise _HttpError(400, '"question" is required')
            scope = body.get("scope")
            if scope is not None and not (isinstance(scope, list) and all(isinstance(x, str) for x in scope)):
                raise _HttpError(400, '"scope" must be a list of globs')
            with Store(store_path) as s:
                h = Harness(s, model_client, cfg.harness_config())
                return 200, h.ask(q, scope=scope, conversation_id=body.get("conversation_id")).to_json()

        @route("POST", "/v1/evaluate")
        def evaluate(self, body, qs):
            f = body.get("formula")
            if not isinstance(f, str) or not f.strip():
                raise _HttpError(400, '"formula" is required')
            handles = body.get("handles") or {}
            if not isinstance(handles, dict):
                raise _HttpError(400, '"handles" must map "$1" to a span id')
            with Store(store_path) as s:
                ev = evaluate_formula(s, f, handles, question=body.get("question"),
                                      verify=bool(body.get("verify", True)))
                return 200, {"kind": ev.kind, "text": ev.render(), "message": ev.message,
                             "cells": [c.to_json() for c in ev.cells]}

        @route("GET", r"/v1/cells/([A-Za-z0-9_]+)")
        def cell(self, body, qs, cell_id):
            with Store(store_path) as s:
                c = s.get_cell(cell_id)
                if c is None:
                    raise _HttpError(404, f"no cell {cell_id}")
                return 200, {**c, "stale": s.staleness(c)}

        @route("GET", r"/v1/cells/([A-Za-z0-9_]+)/trace")
        def trace(self, body, qs, cell_id):
            with Store(store_path) as s:
                c = s.get_cell(cell_id)
                if c is None:
                    raise _HttpError(404, f"no cell {cell_id}")
                return 200, {"id": c["id"], "formula": c["formula"], "answer_formula": c["answer_formula"],
                             "value": c["value"], "display": c["display"], "steps": c["steps"], "inputs": c["inputs"],
                             "evaluated_at": c["evaluated_at"], "engine_version": c["engine_version"],
                             "model": c.get("model"), "stale": s.staleness(c)}

        @route("POST", r"/v1/cells/([A-Za-z0-9_]+)/recompute")
        def recompute_cell(self, body, qs, cell_id):
            with Store(store_path) as s:
                try:
                    r = recompute(Engine(s), cell_id)
                except KeyError:
                    raise _HttpError(404, f"no cell {cell_id}") from None
                if r.evaluation and r.evaluation.cells:
                    s.save_cells([c.to_json() for c in r.evaluation.cells])
                return 200, r.to_json()

        @route("GET", "/v1/runs")
        def runs(self, body, qs):
            limit = int((qs.get("limit") or ["20"])[0])
            with Store(store_path) as s:
                return 200, {"runs": list_runs(s, max(1, min(limit, 200)))}

        @route("GET", r"/v1/spans/(sp:[0-9a-f]+:\d+:\d+)")
        def span(self, body, qs, span_id):
            ctx = int((qs.get("context") or ["0"])[0])
            with Store(store_path) as s:
                sp = s.span_from_id(span_id)
                loc = s.locate(sp)
                return 200, {**s.read_span_context(sp, max(0, min(ctx, 50))), "sources": list(loc.sources),
                             "current": loc.current, "path": loc.path}

    return ThreadingHTTPServer((host, port), Handler)
