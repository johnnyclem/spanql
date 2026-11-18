"""The ``spanql`` command line."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, load_config
from .engine import Engine
from .engine.recompute import recompute
from .errors import SpanqlError, StoreError
from .formula.gbnf import build_gbnf
from .harness import Harness, HarnessError, evaluate_formula, list_runs
from .ingest import ingest_path
from .store import Store, collect_garbage, describe_sources, reanchor


def _out(text: str = "") -> None:
    print(text)


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def _clip(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


# ----------------------------------------------------------- presentation


def _cell_sources(c: dict, limit: int = 3) -> list[str]:
    lines: list[str] = []
    for inp in c.get("inputs", [])[:limit]:
        if inp["kind"] == "span":
            what = f"«{inp['consumed']}»" if inp.get("consumed") else f'"{_clip(inp["text"], 60)}"'
            lines.append(f"{inp.get('source')}:L{inp.get('line')}  {what}")
        elif inp["kind"] == "query":
            q = inp["query"]
            where = f" in {inp['matched_sources']} source(s)" if inp.get("matched_sources") else ""
            first = inp["sample"][0] if inp.get("sample") else None
            lines.append(f"{inp['count']} match(es){where} · {q.get('op')} {q.get('src', '')}"
                         + (f" · first: {first}" if first else ""))
        else:
            lines.append(f"from cell {inp['id']}")
    extra = len(c.get("inputs", [])) - limit
    if extra > 0:
        lines.append(f"… and {extra} more input(s); see `spanql trace {c['id']}`")
    return lines


def format_cells(cells: list[dict]) -> str:
    out = []
    for c in cells:
        out.append(f"  [{c['index']}] {_clip(c['display'], 60)}   = {_clip(c['formula'], 110)}")
        out += [f"        → {line}" for line in _cell_sources(c)]
        out.append(f"        · {c['id']}")
    return "\n".join(out)


def format_ask(res) -> str:
    parts = [res.text]
    if res.cells:
        parts += ["", format_cells(res.cells)]
    foot = f"\n[{res.status} · {res.attempts} attempt(s) · {res.elapsed_ms} ms · run {res.run_id}]"
    return "\n".join(parts) + foot


def format_trace(c: dict, stale: list[dict]) -> str:
    out = [f"{c['id']}  =  {c['formula']}", f"value     {c['display']}   ({c['type']})",
           f"computed  {c['evaluated_at']} · engine {c['engine_version']}"
           + (f" · model {c['model']['id']} attempt {c['model']['formula_attempt']}" if c.get("model") else "")]
    out.append("status    " + ("fresh" if not stale else "STALE: " + "; ".join(f"{s['reason']} ({s.get('source') or s.get('op') or s.get('id')})" for s in stale)))
    out += ["", "steps:"]
    for i, s in enumerate(c["steps"], 1):
        extra = f"   [consumed: {s['in']}]" if s.get("in") else ""
        out.append(f"  {i}. {_clip(s['expr'], 90)}  →  {s['out']}{extra}")
    out += ["", "inputs:"]
    for inp in c["inputs"]:
        if inp["kind"] == "span":
            loc = f"{inp.get('source')} L{inp.get('line')}" + (f" · {inp['path']}" if inp.get("path") else "")
            out.append(f"  span {inp['id']}  {loc}  snapshot {inp['snapshot'][:12]}")
            text = inp["text"]
            if inp.get("consumed") and inp["consumed"] in text:
                text = text.replace(inp["consumed"], f"«{inp['consumed']}»", 1)
            out.append(f"      {text}")
            for u in inp.get("uses", []):
                out.append(f"      {u['op']} read «{u['consumed']}» → {json.dumps(u['parsed'])}")
        elif inp["kind"] == "query":
            out.append(f"  query {json.dumps(inp['query'])}")
            out.append(f"      {inp['count']} match(es) of {inp['blocks_searched']} blocks in {inp['matched_sources']} source(s)")
            for uri, snap in sorted(inp["snapshots"].items())[:8]:
                out.append(f"      {uri}  snapshot {snap[:12]}")
            if inp.get("sample"):
                out.append("      first contributing spans: " + ", ".join(inp["sample"][:5])
                           + (" …" if inp.get("truncated") else ""))
        else:
            out.append(f"  cell {inp['id']}")
    return "\n".join(out)


# --------------------------------------------------------------- commands


def cmd_ingest(args, cfg: Config) -> int:
    targets: list[dict] = [{"path": p, "kind": args.kind, "name": args.name} for p in args.paths]
    if not targets:
        targets = [dict(s) for s in cfg.sources]
        if not targets:
            _err("nothing to ingest: pass a path or configure [[sources]]")
            return 2
    rc = 0
    with Store(args.db or cfg.store_path) as store:
        for t in targets:
            if t.get("watch"):
                _err(f"note: watch = true is not implemented yet; {t['path']} was ingested once")
            try:
                rep = ingest_path(store, t["path"], kind=t.get("kind"), name=t.get("name"),
                                  parser=args.parser or t.get("parser", "auto"), force=args.force,
                                  respect_gitignore=not args.no_gitignore and t.get("respect_gitignore", True),
                                  year=args.year)
            except (FileNotFoundError, ValueError) as e:
                _err(f"error: {e}")
                rc = 1
                continue
            if args.json:
                _out(json.dumps({"path": t["path"], **rep.to_json()}))
                continue
            _out(f"{t['path']}: {len(rep.added)} added, {len(rep.updated)} updated, {rep.unchanged} unchanged, "
                 f"{len(rep.removed)} removed, {len(rep.skipped)} skipped, {len(rep.errors)} errors")
            for p, why in rep.skipped[:20]:
                _out(f"  skipped {p}: {why}")
            for p, why in rep.errors[:20]:
                _out(f"  ERROR   {p}: {why}")
            rc = rc or (1 if rep.errors else 0)
    return rc


def cmd_sources(args, cfg: Config) -> int:
    with Store(args.db or cfg.store_path) as store:
        rows = store.list_sources(args.glob)
        if args.json:
            _out(json.dumps([{"uri": s.uri, "kind": s.kind, "snapshot": s.current_snapshot} for s in rows]))
        else:
            for s in rows:
                _out(f"{s.kind:5} {s.current_snapshot[:8]}  {s.uri}")
            _out(f"{len(rows)} source(s)")
    return 0


def cmd_status(args, cfg: Config) -> int:
    with Store(args.db or cfg.store_path) as store:
        st = store.status()
    m = cfg.data["model"]
    st["model"] = {"endpoint": m.get("endpoint"), "backend": m.get("backend"), "runtime": m.get("runtime")}
    if args.json:
        _out(json.dumps(st, indent=2))
    else:
        for k, v in st.items():
            _out(f"{k:18} {v}")
    return 0


def cmd_describe(args, cfg: Config) -> int:
    with Store(args.db or cfg.store_path) as store:
        _out(json.dumps(describe_sources(store, args.glob), indent=2))
    return 0


def cmd_read_span(args, cfg: Config) -> int:
    with Store(args.db or cfg.store_path) as store:
        span = store.span_from_id(args.span)
        info = store.read_span_context(span, args.context)
        loc = store.locate(span)
        if args.json:
            _out(json.dumps({**info, "sources": list(loc.sources), "current": loc.current, "path": loc.path}))
            return 0
        _out(f"{info['span']}  {', '.join(loc.sources) or '(unknown source)'}  L{info['line_start']}-L{info['line_end']}"
             + ("" if loc.current else "  [source has changed since]"))
        _out(info["context"] if args.context else info["text"])
    return 0


def cmd_reanchor(args, cfg: Config) -> int:
    with Store(args.db or cfg.store_path) as store:
        results = reanchor(store, store.span_from_id(args.span))
        for r in results:
            _out(json.dumps(r.to_json()) if args.json else
                 f"{r.status:9} {r.source}  {r.span.id if r.span else '-'}  {r.note}")
    return 0


def _handles(items: list[str]) -> dict[str, str]:
    out = {}
    for it in items:
        k, sep, v = it.partition("=")
        if not sep:
            raise ValueError(f"--handle must look like '$1=sp:abcd1234:10:50', got {it!r}")
        out[k.strip()] = v.strip()
    return out


def cmd_evaluate(args, cfg: Config) -> int:
    with Store(args.db or cfg.store_path) as store:
        try:
            ev = evaluate_formula(store, args.formula, _handles(args.handle), question=args.question,
                                  verify=not args.no_verify)
        except SpanqlError as e:
            _err(e.feedback())
            return 1
        if args.json:
            _out(json.dumps({"kind": ev.kind, "text": ev.render(), "cells": [c.to_json() for c in ev.cells]}, indent=2))
        else:
            _out(ev.render())
            if ev.cells:
                _out("")
                _out(format_cells([c.to_json() for c in ev.cells]))
    return 0


def _harness(args, cfg: Config, store: Store) -> Harness:
    return Harness(store, cfg.make_client(), cfg.harness_config())


def cmd_ask(args, cfg: Config) -> int:
    with Store(args.db or cfg.store_path) as store:
        res = _harness(args, cfg, store).ask(args.question, scope=args.scope or None)
        _out(json.dumps(res.to_json(), indent=2, ensure_ascii=False) if args.json else format_ask(res))
    return 0


def cmd_chat(args, cfg: Config) -> int:
    _out("spanql chat. Ask about your sources; /quit to leave.")
    with Store(args.db or cfg.store_path) as store:
        h = _harness(args, cfg, store)
        while True:
            try:
                q = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                _out()
                return 0
            if q in ("/quit", "/exit"):
                return 0
            if not q:
                continue
            try:
                _out("\nspanql> " + format_ask(h.ask(q, scope=args.scope or None)).replace("\n", "\n        ") + "\n")
            except HarnessError as e:
                _err(f"error: {e}")


def cmd_trace(args, cfg: Config) -> int:
    with Store(args.db or cfg.store_path) as store:
        c = store.get_cell(args.cell)
        if c is None:
            _err(f"no cell {args.cell}")
            return 1
        stale = store.staleness(c)
        _out(json.dumps({**c, "stale": stale}, indent=2) if args.json else format_trace(c, stale))
    return 0


def cmd_recompute(args, cfg: Config) -> int:
    with Store(args.db or cfg.store_path) as store:
        try:
            r = recompute(Engine(store), args.cell)
        except KeyError:
            _err(f"no cell {args.cell}")
            return 1
        if r.evaluation and r.evaluation.cells:
            store.save_cells([c.to_json() for c in r.evaluation.cells])
        if args.json:
            _out(json.dumps(r.to_json(), indent=2))
        elif r.status == "ok":
            _out(r.evaluation.render() + ("   (changed)" if r.changed else "   (unchanged)"))
            _out(format_cells([c.to_json() for c in r.evaluation.cells]))
        else:
            _err("recompute failed: " + (r.error or {}).get("message", "unknown"))
            return 1
    return 0


def cmd_runs(args, cfg: Config) -> int:
    with Store(args.db or cfg.store_path) as store:
        rows = list_runs(store, args.limit)
    if args.json:
        _out(json.dumps(rows))
    else:
        for r in rows:
            _out(f"{r['id']}  {r['created_at']}  {r['status']:9} {r['attempts']} attempt(s) {r['elapsed_ms']:>6} ms  "
                 f"{_clip(r['question'], 70)}")
    return 0


def cmd_gc(args, cfg: Config) -> int:
    with Store(args.db or cfg.store_path) as store:
        days = args.days if args.days is not None else int(cfg.data["store"]["snapshot_retention_days"])
        _out(json.dumps(collect_garbage(store, days)))
    return 0


def cmd_grammar(args, cfg: Config) -> int:
    sys.stdout.write(build_gbnf(args.handles))
    return 0


def cmd_serve(args, cfg: Config) -> int:
    from .api import make_server

    token = os.environ.get("SPANQL_API_TOKEN") or None
    host, port = cfg.bind()
    server = make_server(cfg, token=token, host=args.host or host, port=args.port if args.port is not None else port)
    h, p = server.server_address[:2]
    _out(f"spanql {__version__} listening on http://{h}:{p}  (store: {cfg.store_path})")
    if cfg.make_client() is None:
        _out("note: no model endpoint configured; /v1/ask is unavailable (ingestion and /v1/evaluate still work)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _out("\nshutting down")
    finally:
        server.server_close()
    return 0


def cmd_health(args, cfg: Config) -> int:
    host, port = cfg.bind()
    host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/v1/health", timeout=3) as r:  # noqa: S310
            ok = json.loads(r.read()).get("status") == "ok"
    except Exception as e:
        _err(f"unhealthy: {e}")
        return 1
    _out("ok" if ok else "unhealthy")
    return 0 if ok else 1


SELFTEST_FILES = {
    "docs/q3.md": "# Q3 Report\n\nTotal revenue for the quarter was $4.82 million, up from $4.31 million in Q2.\n",
    "logs/app.log": ("2026-11-10 09:00:01,100 ERROR app - PaymentTimeout user=a\n"
                     "2026-11-10 09:00:02,200 INFO app - ok user=b\n"
                     "2026-11-10 09:00:03,300 ERROR app - PaymentTimeout user=c\n"),
}


def cmd_selftest(args, cfg: Config) -> int:
    """Ingest a tiny corpus into a throwaway store and evaluate formulas end to end (no model needed)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "corpus"
        for rel, text in SELFTEST_FILES.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        store = Store(":memory:")
        try:
            ingest_path(store, str(root / "docs"))
            ingest_path(store, str(root / "logs"))
            src = store.get_source("docs/q3.md")
            data = store.snapshot_bytes(src.current_snapshot)
            start = data.index(b"Total revenue")
            span = store.make_span(src.current_snapshot, start, data.index(b"\n", start))
            ev = evaluate_formula(store, 'ANSWER(MONEY($1."$4.82 million"), COUNT(FIND(src:"logs/*.log", '
                                         're:"PaymentTimeout")), text:"{0} and {1}")', {"$1": span.id}, save=False)
            expected = "$4,820,000 and 2"
            if ev.render() != expected:
                _err(f"selftest FAILED: expected {expected!r}, got {ev.render()!r}")
                return 1
        finally:
            store.close()
    _out("ok")
    return 0


# ----------------------------------------------------------------- parser


def _add_common(p: argparse.ArgumentParser, *, sub: bool) -> None:
    """--config/--db/--json work before or after the subcommand (the subparser copy must not clobber)."""
    d = argparse.SUPPRESS if sub else None
    p.add_argument("--config", default=d, help="path to config.toml (default: $SPANQL_CONFIG, ./config.toml, ~/.config/spanql/)")
    p.add_argument("--db", default=d, help="override the store path")
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS if sub else False,
                   help="machine-readable output")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="spanql", description="Grounded Q&A over docs, logs, files and code.")
    p.add_argument("--version", action="version", version=f"spanql {__version__}")
    _add_common(p, sub=False)
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    def add(name, fn, help_, **kw):
        sp = sub.add_parser(name, help=help_, description=help_, **kw)
        _add_common(sp, sub=True)
        sp.set_defaults(fn=fn)
        return sp

    s = add("ingest", cmd_ingest, "ingest files, directories, git repos or globs (no-op if unchanged)")
    s.add_argument("paths", nargs="*", help="paths to ingest (default: the [[sources]] in config)")
    s.add_argument("--kind", choices=["doc", "log", "file", "code"], help="override the detected kind")
    s.add_argument("--name", help="URI prefix (default: the directory name)")
    s.add_argument("--parser", help="log parser: auto, jsonl, nginx, iso, syslog, plain, or re:<regex>")
    s.add_argument("--year", type=int, help="year for syslog-style timestamps that have none")
    s.add_argument("--force", action="store_true", help="re-ingest even if the file is unchanged")
    s.add_argument("--no-gitignore", action="store_true", help="do not use git to decide which files to ingest")

    s = add("sources", cmd_sources, "list ingested sources")
    s.add_argument("glob", nargs="?")
    add("status", cmd_status, "store and model status")
    s = add("describe", cmd_describe, "show source schemas (log fields, columns, symbols)")
    s.add_argument("glob", nargs="?")
    s = add("read-span", cmd_read_span, "print the exact text of a span, with optional context lines")
    s.add_argument("span", help="sp:<snapshot>:<start>:<end>")
    s.add_argument("--context", type=int, default=0, help="lines of context on each side")
    s = add("reanchor", cmd_reanchor, "find where a span's text lives now after its source changed")
    s.add_argument("span")
    s = add("evaluate", cmd_evaluate, "evaluate a hand-written formula (no model)")
    s.add_argument("formula")
    s.add_argument("--handle", action="append", default=[], metavar="'$1=SPAN_ID'", help="bind a handle to a span")
    s.add_argument("--question", help="the question (enables the 'numbers from the question' allowance)")
    s.add_argument("--no-verify", action="store_true", help="skip the V4 literal policy")
    s = add("ask", cmd_ask, "ask a question (needs a model endpoint)")
    s.add_argument("question")
    s.add_argument("--scope", action="append", help="limit to sources matching this glob (repeatable)")
    s = add("chat", cmd_chat, "interactive questions (needs a model endpoint)")
    s.add_argument("--scope", action="append")
    s = add("trace", cmd_trace, "show a cell's formula, steps and exact source inputs")
    s.add_argument("cell")
    s = add("recompute", cmd_recompute, "re-evaluate a cell against the current sources")
    s.add_argument("cell")
    s = add("runs", cmd_runs, "recent questions")
    s.add_argument("--limit", type=int, default=20)
    s = add("gc", cmd_gc, "delete old snapshots that no source or cell uses")
    s.add_argument("--days", type=int, help="retention in days (default: store.snapshot_retention_days)")
    s = add("grammar", cmd_grammar, "print the GBNF grammar for N candidate handles")
    s.add_argument("--handles", type=int, default=8)
    s = add("serve", cmd_serve, "run the HTTP API")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    add("health", cmd_health, "check that a running server is healthy")
    s = add("selftest", cmd_selftest, "end-to-end check on a throwaway corpus")
    s.add_argument("--no-model", action="store_true", help="accepted for packaging tests; no model is ever needed")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
        return args.fn(args, cfg)
    except HarnessError as e:
        _err(f"error: {e}")
        return 2
    except (ConfigError, StoreError, ValueError) as e:
        _err(f"error: {e}")
        return 2
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
