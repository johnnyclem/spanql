from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from spanql.ingest.text import ByteIndex, decode, normalize, raw_offset


def test_identity_when_canonical() -> None:
    text, omap = normalize(b"hello\nworld\n")
    assert text == "hello\nworld\n"
    assert omap is None


def test_crlf_and_bom_mapped() -> None:
    raw = b"\xef\xbb\xbfab\r\ncd\r\n"
    text, omap = normalize(raw)
    assert text == "ab\ncd\n"
    assert omap is not None
    # "c" is canonical byte 3; raw: BOM(3) + "ab\r\n"(4) = 7
    assert raw[raw_offset(omap, 3) : raw_offset(omap, 3) + 2] == b"cd"


def test_nfc_normalization() -> None:
    text, _ = normalize("cafe\u0301".encode())
    assert text == "caf\u00e9"


def test_decode_fallback_cp1252() -> None:
    assert decode(b"caf\xe9") == "café"


def test_byte_index_non_ascii() -> None:
    s = "aé€😀b"
    bi = ByteIndex(s)
    for i in range(len(s) + 1):
        assert bi(i) == len(s[:i].encode())


@given(
    st.lists(
        st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=20), max_size=8
    ),
    st.sampled_from(["\n", "\r\n", "\r"]),
)
def test_every_line_maps_back_to_raw(lines: list[str], eol: str) -> None:
    lines = [ln.replace("\r", "").replace("\n", "") for ln in lines]
    raw = eol.join(lines).encode()
    text, omap = normalize(raw)
    assert "\r" not in text
    pos = 0
    for ln in text.split("\n"):
        r = raw_offset(omap, pos)
        if ln and ln.isascii():
            assert raw[r : r + len(ln)] == ln.encode()
        pos += len(ln.encode()) + 1
