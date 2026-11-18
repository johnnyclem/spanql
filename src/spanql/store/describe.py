"""Source schemas for the model prompt and the ``describe_source`` tool."""

from __future__ import annotations

import json

from .db import Store


def describe_sources(store: Store, glob: str | None = None, *, limit: int = 50) -> list[dict]:
    """Per-source summary: log fields, CSV columns, symbol kinds, headings, time range."""
    out = []
    for src in store.list_sources(glob)[:limit]:
        sid = src.current_snapshot
        d: dict = {"uri": src.uri, "kind": src.kind, "blocks": store.block_count(sid)}
        q = store.db.execute
        if src.kind == "log":
            r = q(
                """SELECT COUNT(*) n, MIN(l.ts) first_ts, MAX(l.ts) last_ts, MIN(l.day) first_day, MAX(l.day) last_day
                   FROM log_record l JOIN block b ON b.id=l.block_id WHERE b.snapshot_id=?""",
                (sid,),
            ).fetchone()
            d["records"] = r["n"]
            if r["first_day"]:
                d["time_range"] = [r["first_day"], r["last_day"]]
            d["levels"] = {
                x["level"]: x["n"]
                for x in q(
                    """SELECT l.level, COUNT(*) n FROM log_record l JOIN block b ON b.id=l.block_id
                       WHERE b.snapshot_id=? AND l.level IS NOT NULL GROUP BY l.level ORDER BY n DESC""",
                    (sid,),
                )
            }
            d["fields"] = _field_samples(
                store,
                q(
                    """SELECT l.fields FROM log_record l JOIN block b ON b.id=l.block_id
                       WHERE b.snapshot_id=? ORDER BY b.start LIMIT 300""",
                    (sid,),
                ),
            )
        elif src.kind == "file":
            rows = q(
                """SELECT t.fields FROM table_row t JOIN block b ON b.id=t.block_id
                   WHERE b.snapshot_id=? ORDER BY b.start LIMIT 300""",
                (sid,),
            ).fetchall()
            if rows:
                d["rows"] = q(
                    "SELECT COUNT(*) FROM table_row t JOIN block b ON b.id=t.block_id WHERE b.snapshot_id=?", (sid,)
                ).fetchone()[0]
                d["columns"] = _field_samples(store, rows)
        elif src.kind == "code":
            d["symbols"] = {
                x["kind"]: x["n"]
                for x in q(
                    """SELECT c.kind, COUNT(*) n FROM code_symbol c JOIN block b ON b.id=c.block_id
                       WHERE b.snapshot_id=? GROUP BY c.kind""",
                    (sid,),
                )
            }
        else:
            heads = [
                r["path"]
                for r in q(
                    "SELECT path FROM block WHERE snapshot_id=? AND kind='heading' ORDER BY start LIMIT 8", (sid,)
                )
            ]
            if heads:
                d["headings"] = heads
        out.append(d)
    return out


def _field_samples(store: Store, rows, max_fields: int = 25) -> dict[str, str]:
    """field name -> short example value, in first-seen order."""
    samples: dict[str, str] = {}
    for r in rows:
        try:
            obj = json.loads(r[0])
        except (TypeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        for k, v in obj.items():
            if k not in samples and len(samples) < max_fields:
                s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                samples[k] = s if len(s) <= 40 else s[:37] + "..."
    return samples
