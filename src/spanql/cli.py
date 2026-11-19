"""The ``spanql`` command."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from spanql import __version__
from spanql.config import Config, ConfigError, SourceConfig, load_config, render_default_config


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def _load(args: argparse.Namespace) -> Config:
    cfg = load_config(args.config)
    if getattr(args, "db", None):
        cfg.store.path = args.db
    if getattr(args, "backend", None):
        cfg.model.backend = args.backend
    if getattr(args, "endpoint", None):
        cfg.model.endpoint = args.endpoint
    if getattr(args, "model", None):
        cfg.model.model = args.model
    return cfg


def _services(args: argparse.Namespace) -> Any:
    from spanql.services import Services

    return Services.create(_load(args))


# ------------------------------------------------------------------- commands
def cmd_init(args: argparse.Namespace) -> int:
    from spanql.config import config_home, data_home

    target = Path(args.config or os.environ.get("SPANQL_CONFIG") or config_home() / "config.toml")
    target = target.expanduser()
    if target.exists() and not args.force:
        print(f"config already exists: {target} (use --force to overwrite)")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    runtime = "managed" if shutil.which("llama-server") else "external"
    store = Path(args.store).expanduser() if args.store else data_home() / "spanql.db"
    models = (
        Path(args.models_dir).expanduser()
        if args.models_dir
        else (
            Path(os.environ["SPANQL_MODELS"])
            if os.environ.get("SPANQL_MODELS")
            else data_home() / "models"
        )
    )
    target.write_text(render_default_config(store, models, runtime))
    print(f"wrote {target}")
    print(f"  store:   {store}\n  models:  {models}\n  runtime: {runtime}")
    print(
        "next: add [[sources]] to the config (or `spanql ingest <path>`), then "
        "`spanql model pull qwen3.5-4b-instruct-q4_k_m`"
    )
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    svc = _services(args)
    sources = [
        SourceConfig(path=p, kind=args.kind, name=args.name, parser=args.parser) for p in args.paths
    ] or None
    if sources is None and not svc.config.sources:
        print("no sources: pass paths, or add [[sources]] to the config", file=sys.stderr)
        return 2
    rep = svc.ingest(sources)
    if args.json:
        _print_json(rep.to_dict())
    else:
        print(
            f"ingested {', '.join(rep.roots)}: {rep.added} added, {rep.updated} updated, "
            f"{rep.unchanged} unchanged, {rep.removed} removed, {len(rep.skipped)} skipped, "
            f"{len(rep.errors)} errors"
        )
        for p, e in rep.errors[:20]:
            print(f"  error {p}: {e}")
    return 1 if rep.errors and not (rep.added or rep.updated or rep.unchanged) else 0


def cmd_sources(args: argparse.Namespace) -> int:
    svc = _services(args)
    rows = [dict(r) for r in svc.store.list_sources()]
    if args.json:
        _print_json(rows)
    else:
        for r in rows:
            print(f"{r['kind']:5} {r['uri']}  ({(r['current_snapshot'] or '')[:8]})")
        print(f"{len(rows)} sources")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    _print_json(_services(args).status())
    return 0


def cmd_read_span(args: argparse.Namespace) -> int:
    svc = _services(args)
    out = svc.store.read_with_context(args.span, args.context)
    if args.json:
        _print_json(out)
    else:
        cur = "" if out["current"] else "  [source changed since this snapshot]"
        print(f"{out['source']}:L{out['line_start']}-L{out['line_end']}  {out['id']}{cur}")
        print(out["text"])
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    svc = _services(args)
    r = svc.search(args.query, scope=args.scope, k=args.k)
    if args.json:
        _print_json(r.to_dict())
        return 0
    for h in r.handles.values():
        print(f"${h.n}  {h.source} · {h.location()}  {h.span.id}")
        print("    " + h.text[:300].replace("\n", "\n    "))
    for hint in r.hints:
        print(f"  hint: {hint}")
    return 0


def _print_cells(cells: list[dict[str, Any]]) -> None:
    for i, c in enumerate(cells, start=1):
        print(f"  [{i}] {c['value']['display']}  = {c['formula']}   ({c['id']})")
        for inp in c["inputs"][:5]:
            if inp["kind"] == "span":
                where = f"{inp['source']}:L{inp['line']}"
                shown = inp.get("consumed") or inp["text"]
                print(
                    f"        ← {where} · {inp.get('consumed_span') or inp['id']} · {shown[:80]!r}"
                )
            elif inp["kind"] == "query":
                first = f" · first: {inp['first_source']}" if inp.get("first_source") else ""
                print(f"        ← {inp['count']} matches of {inp['query']}{first}")
            else:
                print(f"        ← cell [{inp['id']}]")


def cmd_evaluate(args: argparse.Namespace) -> int:
    svc = _services(args)
    handles = {}
    for h in args.handle or []:
        k, _, v = h.partition("=")
        handles[k.lstrip("$")] = v
    out = svc.evaluate(args.formula, handles, question=args.question)
    if args.json:
        _print_json(out.to_dict())
    elif out.status == "grounded":
        print(out.answer)
        _print_cells(out.cells)
    elif out.status in ("not_found", "ambiguous"):
        print(f"{out.status}: {out.reason}")
    else:
        print("rejected by the verifier:")
        for p in out.problems:
            print(f"  {p}")
    return 0 if out.ok else 1


def cmd_trace(args: argparse.Namespace) -> int:
    svc = _services(args)
    cell = svc.store.get_cell(args.cell)
    if cell is None:
        print(f"unknown cell {args.cell}", file=sys.stderr)
        return 1
    if args.json:
        _print_json({**cell, **svc.staleness(cell)})
        return 0
    print(f"{cell['id']} = {cell['value']['display']}  ({cell['status']})")
    print(f"formula: {cell['formula']}")
    for h, sid in cell.get("handles", {}).items():
        print(f"  {h} = {sid}")
    print("steps:")
    for s in cell["steps"]:
        print(f"  {s['op']:<10} → {s['out']}")
    print("inputs:")
    _print_cells([cell])
    st = svc.staleness(cell)
    if st["stale"]:
        print(
            f"source changed for {len(st['stale_inputs'])} input(s); "
            f"run `spanql recompute {cell['id']}`"
        )
    return 0


def _print_answer(res: Any, as_json: bool) -> None:
    if as_json:
        _print_json(res.to_dict())
        return
    print(res.answer)
    if res.status == "grounded":
        _print_cells(res.cells)
    else:
        print(f"  ({res.status}; {len(res.attempts)} attempt(s); refused by {res.refusal_by})")
        for nm in res.search_report.get("near_misses", [])[:3]:
            print(f"   near miss: {nm['source']} {nm['location']}: {nm['text'][:100]!r}")


def cmd_ask(args: argparse.Namespace) -> int:
    svc = _services(args)
    res = svc.ask(args.question, scope=args.scope)
    _print_answer(res, args.json)
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    svc = _services(args)
    print("spanql chat — ask questions, `:trace <cell>` for provenance, Ctrl-D to quit")
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line.startswith(":trace "):
            args.cell, args.json = line.split(maxsplit=1)[1], False
            cmd_trace(args)
            continue
        res = svc.ask(line, scope=args.scope, conversation_id="cli")
        print("spanql> ", end="")
        _print_answer(res, False)


def cmd_recompute(args: argparse.Namespace) -> int:
    _print_json(_services(args).recompute(args.cell))
    return 0


def cmd_reanchor(args: argparse.Namespace) -> int:
    from spanql.store.reanchor import reanchor

    _print_json(reanchor(_services(args).store, args.span).to_dict())
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    svc = _services(args)
    if args.run_id:
        _print_json(svc.store.get_run(args.run_id))
        return 0
    for r in svc.store.list_runs(args.limit):
        print(f"{r['created_at']}  {r['id']}  {r['status']:<10} {r['question']}")
    return 0


def cmd_grammar(args: argparse.Namespace) -> int:
    from spanql.formula.gbnf import build_gbnf

    print(build_gbnf(range(1, args.handles + 1)), end="")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from spanql.api.app import create_app
    from spanql.runtime import ManagedLlamaServer
    from spanql.services import Services

    cfg = _load(args)
    if args.host or args.port:
        cfg.server.bind = f"{args.host or cfg.server.host}:{args.port or cfg.server.port}"
        if not cfg.server.is_loopback and not cfg.server.api_token:
            print(
                "refusing to bind a non-loopback address without SPANQL_API_TOKEN", file=sys.stderr
            )
            return 2
    runtime = None
    if cfg.model.runtime == "managed" and cfg.model.backend in ("llama.cpp", "llamacpp"):
        runtime = ManagedLlamaServer(cfg)
        ok, why = runtime.available()
        if ok:
            runtime.start()
            cfg.model.endpoint = runtime.endpoint
            print(f"started llama-server for {cfg.model.model} on {runtime.endpoint}")
        else:
            print(
                f"model runtime not started: {why}\nserving in 'no model' state "
                "(ingestion, search and /v1/evaluate still work)"
            )
            runtime = None
    svc = Services.create(cfg)
    if cfg.sources and not args.no_ingest:
        rep = svc.ingest()
        print(f"ingested: {rep.added} added, {rep.updated} updated, {rep.unchanged} unchanged")
    app = create_app(svc)
    print(f"spanql {__version__} listening on http://{cfg.server.bind}")
    try:
        uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, log_level="warning")
    finally:
        if runtime:
            runtime.stop()
        svc.close()
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from spanql.mcp.server import MCPServer

    MCPServer(_services(args)).serve()
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    import httpx

    cfg = _load(args)
    url = args.url or f"http://{cfg.server.bind.replace('0.0.0.0', '127.0.0.1')}/v1/health"
    try:
        r = httpx.get(url, timeout=3)
        ok = r.status_code == 200 and r.json().get("ok")
    except (httpx.HTTPError, ValueError):
        ok = False
    print("ok" if ok else "unhealthy")
    return 0 if ok else 1


def cmd_model(args: argparse.Namespace) -> int:
    from spanql.models import manifest as mf

    cfg = _load(args)
    if args.model_cmd == "list":
        have = {m["alias"] for m in mf.installed(cfg.models_dir)}
        for spec in mf.MANIFEST.values():
            mark = "*" if spec.alias in have else " "
            print(
                f"{mark} {spec.alias:<32} {spec.size / 1e9:5.2f} GB  {spec.license:<11} "
                f"{spec.repo}  {spec.notes}"
            )
        extra = have - set(mf.MANIFEST)
        for a in sorted(extra):
            print(f"* {a:<32} (imported)")
        print(f"models dir: {cfg.models_dir}")
        return 0
    if args.model_cmd == "pull":
        pspec = mf.MANIFEST.get(args.alias)
        if pspec is None:
            print(f"unknown alias {args.alias}; see `spanql model list`", file=sys.stderr)
            return 2
        print(
            f"pulling {pspec.alias} ({pspec.size / 1e9:.2f} GB, license {pspec.license}) from "
            f"{pspec.repo}@{pspec.revision[:10]}"
        )
        last = [-1]

        def progress(done: int, total: int) -> None:
            pct = int(done * 100 / total) if total else 0
            if pct != last[0] and pct % 5 == 0:
                last[0] = pct
                print(f"  {pct}%", flush=True)

        path = mf.pull(args.alias, cfg.models_dir, progress=progress)
        print(f"ok: {path}")
        return 0
    if args.model_cmd == "import":
        path = mf.import_file(
            Path(args.file).expanduser(), args.alias, cfg.models_dir, args.license
        )
        print(f"imported {path}")
        return 0
    return 2


def cmd_gc(args: argparse.Namespace) -> int:
    svc = _services(args)
    n = svc.store.gc_snapshots(
        args.retention_days
        if args.retention_days is not None
        else svc.config.store.snapshot_retention_days
    )
    print(f"removed {n} unreferenced old snapshot(s)")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    _services(args).store.backup(args.dest)
    print(f"backed up to {args.dest}")
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    """Ingest a tiny built-in corpus into a temp store and check every layer offline."""
    from spanql.config import Config
    from spanql.harness.backends import FakeBackend, make_backend
    from spanql.services import Services

    corpus = {
        "report.md": "# Q3\n\nTotal revenue for the quarter was $4.82 million.\n\n"
        "Q2 revenue: $4.31M\n",
        "app.log": "2026-11-10T08:00:05Z ERROR api: PaymentTimeout status=504\n"
        "2026-11-10T08:01:00Z INFO api: ok status=200\n"
        "2026-11-10T09:00:05Z ERROR api: PaymentTimeout status=504\n",
        "data.csv": 'region,amount\nWest,1200.50\nEast,"3,400.00"\n',
        "lib.py": "def retry(fn):\n    return fn()\n\n\ndef charge():\n    return retry(lambda: 1)\n",
    }
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "selftest"
        root.mkdir()
        for name, text in corpus.items():
            (root / name).write_text(text)
        cfg = Config()
        cfg.store.path = str(Path(tmp) / "st.db")
        backend = FakeBackend() if args.no_model else make_backend(_load(args).model)
        svc = Services.create(cfg, backend=backend)
        rep = svc.ingest([SourceConfig(path=str(root))])
        checks: list[tuple[str, bool, str]] = [
            (
                "ingest",
                rep.added == 4 and not rep.errors,
                f"{rep.added} added, {len(rep.errors)} errors",
            )
        ]
        again = svc.ingest([SourceConfig(path=str(root))])
        checks.append(("re-ingest is a no-op", again.unchanged == 4 and again.added == 0, ""))
        r = svc.search("revenue quarter")
        handles = {str(h.n): h.span.id for h in r.handles.values()}
        q3 = next((str(h.n) for h in r.handles.values() if "4.82" in h.text), None)
        q2 = next((str(h.n) for h in r.handles.values() if "4.31" in h.text), None)
        checks.append(("retrieval", bool(q3 and q2), f"{len(handles)} candidates"))
        if q3 and q2:
            out = svc.evaluate(
                f'ANSWER(DIFF(MONEY(${q3}."$4.82 million"), MONEY(${q2}."$4.31M")))', handles
            )
            checks.append(("evaluate", out.answer == "$510,000", str(out.answer)))
            bad = svc.evaluate(f'ANSWER(MONEY(${q3}."$9.99 million"))', handles)
            checks.append(
                (
                    "verifier rejects missing quote",
                    bad.status == "error",
                    bad.problems[0].code if bad.problems else "",
                )
            )
        cnt = svc.evaluate('ANSWER(COUNT(FIND(kind:"log", FIELD("status") >= 500)))')
        checks.append(("log query", cnt.answer == "2", str(cnt.answer)))
        tot = svc.evaluate('ANSWER(SUM(COLUMN(src:"data.csv", "amount")))')
        checks.append(("table query", tot.answer == "4,600.5", str(tot.answer)))
        sym = svc.evaluate('ANSWER(COUNT(SYMBOLS(calls:"retry")))')
        checks.append(("code query", sym.answer == "1", str(sym.answer)))
        res = svc.ask("What is the capital of France?")
        checks.append(("refusal", res.status == "not_found", res.status))
        if not args.no_model:
            h = backend.health()
            checks.append(("model backend", bool(h.get("ok")), json.dumps(h)[:120]))
            res = svc.ask("How many PaymentTimeout errors were logged on 2026-11-10?")
            checks.append(("model ask (informational)", True, f"{res.status}: {res.answer}"))
        svc.close()
    for name, ok, detail in checks:
        failures += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f"  — {detail}" if detail else ""))
    print("selftest passed" if not failures else f"selftest: {failures} failure(s)")
    return 0 if not failures else 1


# ----------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="spanql", description=__doc__)
    p.add_argument("--version", action="version", version=f"spanql {__version__}")
    p.add_argument(
        "--config",
        help="path to config.toml (default: $SPANQL_CONFIG, ./spanql.toml,"
        " ~/.config/spanql/config.toml)",
    )
    p.add_argument("--db", help="override store.path")
    p.add_argument(
        "--backend", help="override model.backend (llama.cpp|ollama|openai-compatible|fake)"
    )
    p.add_argument("--endpoint", help="override model.endpoint")
    p.add_argument("--model", help="override model.model")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="write a default config file")
    s.add_argument("--force", action="store_true")
    s.add_argument("--store")
    s.add_argument("--models-dir")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("ingest", help="ingest paths (or the configured sources)")
    s.add_argument("paths", nargs="*")
    s.add_argument("--kind", default="auto", choices=["auto", "doc", "log", "file", "code"])
    s.add_argument("--name", help="root name used in source URIs")
    s.add_argument("--parser", default="auto", help="log parser: auto|jsonl|access|syslog|generic")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("sources", help="list ingested sources")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_sources)

    sub.add_parser("status", help="store and model status").set_defaults(fn=cmd_status)

    s = sub.add_parser("read-span", help="print the exact text of a span id")
    s.add_argument("span")
    s.add_argument("--context", type=int, default=0)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_read_span)

    s = sub.add_parser("search", help="full-text search; shows candidate handles")
    s.add_argument("query")
    s.add_argument("-k", type=int, default=10)
    s.add_argument("--scope", action="append")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("evaluate", help="evaluate a formula (no model)")
    s.add_argument("formula")
    s.add_argument("--handle", action="append", help="N=sp:… (repeatable)")
    s.add_argument("--question")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_evaluate)

    s = sub.add_parser("trace", help="show a cell's provenance")
    s.add_argument("cell")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_trace)

    s = sub.add_parser("ask", help="ask a question")
    s.add_argument("question")
    s.add_argument("--scope", action="append", help="limit to source globs")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_ask)

    s = sub.add_parser("chat", help="interactive REPL")
    s.add_argument("--scope", action="append")
    s.set_defaults(fn=cmd_chat)

    s = sub.add_parser("recompute", help="re-evaluate a cell against current sources")
    s.add_argument("cell")
    s.set_defaults(fn=cmd_recompute)

    s = sub.add_parser("reanchor", help="find a span in its source's current snapshot")
    s.add_argument("span")
    s.set_defaults(fn=cmd_reanchor)

    s = sub.add_parser("runs", help="list run records (or show one)")
    s.add_argument("run_id", nargs="?")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_runs)

    s = sub.add_parser("grammar", help="print the GBNF grammar for N handles")
    s.add_argument("--handles", type=int, default=5)
    s.set_defaults(fn=cmd_grammar)

    s = sub.add_parser("serve", help="run the HTTP API + web UI")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--no-ingest", action="store_true", help="skip ingesting sources at startup")
    s.set_defaults(fn=cmd_serve)

    sub.add_parser("mcp", help="run as an MCP server on stdio").set_defaults(fn=cmd_mcp)

    s = sub.add_parser("health", help="check a running server (Docker HEALTHCHECK)")
    s.add_argument("--url")
    s.set_defaults(fn=cmd_health)

    s = sub.add_parser("model", help="manage local model weights")
    msub = s.add_subparsers(dest="model_cmd", required=True)
    msub.add_parser("list")
    mp = msub.add_parser("pull")
    mp.add_argument("alias")
    mi = msub.add_parser("import")
    mi.add_argument("file")
    mi.add_argument("--alias", required=True)
    mi.add_argument("--license", default="unknown")
    s.set_defaults(fn=cmd_model)

    s = sub.add_parser("gc", help="delete old unreferenced snapshots")
    s.add_argument("--retention-days", type=int)
    s.set_defaults(fn=cmd_gc)

    s = sub.add_parser("backup", help="online backup of the store")
    s.add_argument("dest")
    s.set_defaults(fn=cmd_backup)

    s = sub.add_parser("selftest", help="check every layer on a built-in corpus")
    s.add_argument("--no-model", action="store_true", help="use the offline fake backend")
    s.set_defaults(fn=cmd_selftest)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return int(args.fn(args) or 0)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        if args.verbose:
            raise
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
