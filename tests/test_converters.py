from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from spanql.ingest.converters import (
    convert_delimited,
    convert_html,
    convert_lines,
    convert_markdown,
    convert_plain,
)


def _slice(c, b) -> str:
    return c.text.encode()[b.start : b.end].decode()


def test_plain_paragraphs() -> None:
    c = convert_plain(b"para one\nstill one\n\npara two\n")
    paras = [_slice(c, b) for b in c.blocks]
    assert paras == ["para one\nstill one", "para two"]


def test_markdown_headings_paths_and_tables() -> None:
    md = (
        b"# Report\n\nIntro text.\n\n## Revenue\n\nRevenue was $4.82 million.\n\n"
        b"| region | amount |\n|---|---|\n| West | $10 |\n| East | $20 |\n\n"
        b"```python\nx = 1\n```\n- item one\n"
    )
    c = convert_markdown(md)
    kinds = [b.kind for b in c.blocks]
    assert "heading" in kinds and "table_row" in kinds and "table_cell" in kinds
    para = next(b for b in c.blocks if _slice(c, b).startswith("Revenue was"))
    assert para.path and "Revenue" in para.path
    cells = [b for b in c.blocks if b.kind == "table_cell"]
    assert {b.cell["header"] for b in cells} == {"region", "amount"}
    assert [_slice(c, b) for b in cells] == ["West", "$10", "East", "$20"]
    assert any(_slice(c, b).startswith("```") or "x = 1" in _slice(c, b) for b in c.blocks)


def test_csv_quoted_fields_have_exact_offsets() -> None:
    raw = b'a,b,c\n1,"x, y",3\n"q""uote",,9\n'
    c = convert_delimited(raw)
    cells = [b for b in c.blocks if b.kind == "table_cell"]
    vals = [_slice(c, b) for b in cells]
    assert vals[:3] == ["1", '"x, y"', "3"] or vals[:3] == ["1", "x, y", "3"]
    rows = [b for b in c.blocks if b.kind == "table_row"]
    assert rows[0].row["fields"] == {"a": "1", "b": "x, y", "c": "3"}
    assert rows[1].row["fields"]["a"] == 'q"uote'


def test_tsv() -> None:
    c = convert_delimited(b"k\tv\nalpha\t1\n", "\t")
    row = next(b for b in c.blocks if b.kind == "table_row")
    assert row.row["fields"] == {"k": "alpha", "v": "1"}


cell_text = st.text(alphabet='abcXYZ 019,."$-', max_size=6)


@settings(max_examples=60)
@given(st.lists(st.lists(cell_text, min_size=3, max_size=3), min_size=1, max_size=6))
def test_csv_roundtrip_property(rows: list[list[str]]) -> None:
    def q(v: str) -> str:
        return '"' + v.replace('"', '""') + '"' if any(ch in v for ch in ',"') else v

    raw = ("h1,h2,h3\n" + "\n".join(",".join(q(v) for v in r) for r in rows) + "\n").encode()
    c = convert_delimited(raw)
    got = [b.row["fields"] for b in c.blocks if b.kind == "table_row"]
    want = [dict(zip(["h1", "h2", "h3"], [v.strip() for v in r], strict=True)) for r in rows]
    # whitespace-only rows are skipped and values are trimmed
    norm = [{k: v.strip() for k, v in g.items()} for g in got]
    assert [g for g in norm if any(g.values())] == [w for w in want if any(w.values())]
    for b in c.blocks:
        assert 0 <= b.start <= b.end <= len(c.text.encode())


def test_lines_and_html() -> None:
    c = convert_lines(b"a: 1\nb: 2\n")
    assert c.blocks and all(b.end <= len(c.text.encode()) for b in c.blocks)
    h = convert_html(
        b"<html><body><h1>Title</h1><p>Hello <b>world</b></p><script>x</script></body></html>"
    )
    assert "Hello world" in h.text
    assert "x" not in h.text.replace("Hello world", "")
