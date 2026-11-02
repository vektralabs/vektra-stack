"""Sniffing, zip guards and every extractor, in-process (plan assess-parser, task B).

No fork here: the hostile-resource cases (rlimit, timeout) are the corpus test's job.
Fixtures come from the generated corpus; each test asserts the generator produced the
file before asserting a status.
"""

from __future__ import annotations

import hashlib
import io
import zipfile

import pytest
from parser_corpus_support import HEAVY, corpus

from vektra_assess.parser_protocol import FileStatus
from vektra_assess.parser_sidecar import zipguard
from vektra_assess.parser_sidecar.child import run, serialise
from vektra_assess.parser_sidecar.html_review import sanitize_review_html
from vektra_assess.parser_sidecar.pdf import extract_pdf
from vektra_assess.parser_sidecar.sniff import Kind, sniff
from vektra_assess.parser_sidecar.text import clean, decode, extract_text

ITEMS = [n for n in corpus() if n not in HEAVY]


@pytest.mark.parametrize("name", ITEMS)
def test_corpus_item_in_process(name: str) -> None:
    it = corpus()[name]
    assert it.data or it.name in ("empty_file", "rv_empty")
    if it.magic:
        assert it.data.startswith(it.magic)
    res = run(it.op, it.data, it.ext, max_pages=50, max_chars=200_000)
    assert res.status is it.expected, (res.status, res.code)
    if it.upto_page is not None:
        assert res.upto_page == it.upto_page
    for s in it.contains:
        assert s in res.text, f"{s!r} missing"
    for s in it.forbidden:
        assert s not in res.text


# --- sniff ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "ext", "kind"),
    [
        (b"%PDF-1.7 x", "txt", Kind.PDF),
        (b"\x89PNG\r\n\x1a\nxx", "txt", Kind.REFUSED),
        (b"\xff\xd8\xff\xe0 jfif", "jpg", Kind.REFUSED),
        (b"GIF89a....", "gif", Kind.REFUSED),
        (b"\x1f\x8b\x08 gz", "gz", Kind.REFUSED),
        (b"\x00\x00\x00\x18ftypmp42", "mp4", Kind.REFUSED),
        (b"\x7fELF\x02", "", Kind.REFUSED),
        (b"hello world\n", "txt", Kind.TEXT),
        (b"hello world\n", "", Kind.TEXT),
        (b"hello world\n", "exe", Kind.REFUSED),
        (b"hello\x00world", "txt", Kind.REFUSED),
        (b"\x01\x02\x03\x04" * 100, "txt", Kind.REFUSED),
    ],
)
def test_sniff_signatures(data: bytes, ext: str, kind: Kind) -> None:
    assert sniff(data, ext).kind is kind


def test_sniff_zip_flavours() -> None:
    c = corpus()
    assert sniff(c["docx_valid"].data, "zip").kind is Kind.DOCX  # magic beats the hint
    assert sniff(c["odt_valid"].data, "docx").kind is Kind.ODT
    for name, code in [
        ("docm_macro", "macro_enabled"),
        ("pptx", "ooxml_other"),
        ("xlsx", "ooxml_other"),
        ("zip_archive", "zip_archive"),
        ("odf_spreadsheet", "odf_other"),
    ]:
        s = sniff(c[name].data, c[name].ext)
        assert (s.kind, s.status, s.code) == (
            Kind.REFUSED,
            FileStatus.UNSUPPORTED_FORMAT,
            code,
        ), name
    s = sniff(c["ole_encrypted_docx"].data, "docx")
    assert s.status is FileStatus.PROTECTED
    assert (
        sniff(c["legacy_doc_ole"].data, "doc").status is FileStatus.UNSUPPORTED_FORMAT
    )
    assert sniff(c["zip_bomb_total"].data, "docx").status is FileStatus.CORRUPT


def test_polyglots_and_misleading_extensions() -> None:
    c = corpus()
    assert sniff(c["pdf_zip_polyglot"].data, "docx").kind is Kind.PDF
    assert sniff(c["misleading_ext_pdf_as_txt"].data, "txt").kind is Kind.PDF
    assert sniff(c["misleading_ext_text_as_pdf"].data, "pdf").kind is Kind.REFUSED


def test_text_decoding_fallbacks() -> None:
    assert decode("è".encode()) == "è"  # utf-8
    assert decode(b"\xef\xbb\xbfabc") == "abc"  # BOM tolerated
    assert decode("è".encode("cp1252")) == "è"  # cp1252 fallback
    assert decode(b"\x81\x8d\x8f") is None  # undefined in cp1252 and not UTF-8
    assert extract_text(b"\x81\x8d\x8f abc", 100).status is FileStatus.NO_TEXT
    assert extract_text(b"  \n\t ", 100).status is FileStatus.NO_TEXT
    assert extract_text(b"a" * 50, 10).status is FileStatus.READ_PARTIAL
    assert clean("a\x00b\x07c\r\nd\te") == "abc\nd\te"


# --- zip guards ------------------------------------------------------------------


def _zip(entries: dict[str, bytes], method: int = zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", method) as z:
        for k, v in entries.items():
            z.writestr(k, v)
    return buf.getvalue()


def test_zipguard_refusals() -> None:
    with pytest.raises(zipguard.ZipRefused, match="zip_unreadable"):
        zipguard.open_checked(b"PK\x03\x04 not a zip")
    with pytest.raises(zipguard.ZipRefused, match="too_many"):
        zipguard.open_checked(_zip({f"e{i}": b"x" for i in range(12)}), max_entries=10)
    with pytest.raises(zipguard.ZipRefused, match="declared"):
        zipguard.open_checked(
            _zip({"a": b"x" * 5000, "b": b"y" * 5000}), max_total=9000
        )
    with pytest.raises(zipguard.ZipRefused, match="ratio"):
        zipguard.open_checked(_zip({"z": bytes(1 << 20)}))
    ok = zipguard.open_checked(_zip({"a": b"hello"}))
    assert ok.read("a", 100) == b"hello"
    with pytest.raises(zipguard.ZipRefused, match="too_large"):
        ok.read("a", 3)  # streamed cap, whatever the header says
    with pytest.raises(zipguard.ZipRefused, match="unreadable"):
        ok.read("missing", 10)


def test_zipguard_duplicate_entries_and_crc_and_budget() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a", b"one")
        with pytest.warns(UserWarning):
            z.writestr("a", b"two")
    with pytest.raises(zipguard.ZipRefused, match="duplicate"):
        zipguard.open_checked(buf.getvalue())
    good = _zip({"a": b"hello world" * 10}, zipfile.ZIP_STORED)
    bad = bytearray(good)
    bad[good.index(b"hello world") + 3] ^= (
        0xFF  # corrupt the stored data, CRC mismatches
    )
    with pytest.raises(zipguard.ZipRefused, match="zip_crc"):
        zipguard.open_checked(bytes(bad)).read("a", 10_000)
    g = zipguard.open_checked(_zip({"a": b"x" * 100, "b": b"y" * 100}))
    g.budget.remaining = 150  # the shared budget spans entries
    g.read("a", 1000)
    with pytest.raises(zipguard.ZipRefused, match="too_large"):
        g.read("b", 1000)


def test_zipguard_overlapping_entries() -> None:
    raw = bytearray(_zip({"a": b"A" * 200, "b": b"B" * 200}, zipfile.ZIP_STORED))
    # point entry b's central-directory header offset into the middle of entry a
    cd = raw.index(b"PK\x01\x02", raw.index(b"PK\x01\x02") + 1)
    raw[cd + 42 : cd + 46] = (10).to_bytes(4, "little")
    with pytest.raises(zipguard.ZipRefused, match="overlap"):
        zipguard.open_checked(bytes(raw))


def test_encrypted_zip_flag_is_protected() -> None:
    raw = bytearray(corpus()["docx_valid"].data)
    for sig, off in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        i = 0
        while (i := raw.find(sig, i)) != -1:
            raw[i + off] |= 1
            i += 4
    res = run("parse", bytes(raw), "docx", max_pages=50, max_chars=1000)
    assert res.status is FileStatus.PROTECTED


# --- PDF ---------------------------------------------------------------------------


def test_pdf_page_cap_and_char_cap() -> None:
    c = corpus()
    res = extract_pdf(c["pdf_51_pages"].data, 50, 10**7)
    assert (res.status, res.upto_page) == (FileStatus.READ_PARTIAL, 50)
    assert "PAGE050" in res.text and "PAGE051" not in res.text
    res = extract_pdf(c["pdf_valid"].data, 50, 400)  # page 1 alone is ~300 chars
    assert res.status is FileStatus.READ_PARTIAL and res.upto_page == 1
    assert "P2 line" not in res.text
    res = extract_pdf(c["pdf_valid"].data, 50, 100)  # one page above the cap: its head
    assert res.status is FileStatus.READ_PARTIAL and len(res.text) == 100
    res = extract_pdf(c["pdf_valid"].data, 1, 10**6)
    assert (res.status, res.upto_page) == (FileStatus.READ_PARTIAL, 1)


def test_pdf_garbage_and_scanned() -> None:
    assert extract_pdf(b"%PDF-1.4 garbage", 50, 1000).status is FileStatus.CORRUPT
    assert (
        extract_pdf(corpus()["pdf_scanned"].data, 50, 1000).status is FileStatus.NO_TEXT
    )


# --- DOCX / ODT --------------------------------------------------------------------


def test_docx_body_order_and_remote_template_never_resolved() -> None:
    res = run(
        "parse", corpus()["docx_valid"].data, "docx", max_pages=50, max_chars=10**6
    )
    t = res.text
    assert (
        t.index("# Relazione finale")
        < t.index("cella A1")
        < t.index("def f(x)")
        < t.index("E=mc2")
    )
    rt = run(
        "parse",
        corpus()["docx_remote_template"].data,
        "docx",
        max_pages=50,
        max_chars=10**6,
    )
    assert rt.status is FileStatus.READ and "attachedTemplate" not in rt.text


def test_docx_nested_table_and_char_cap() -> None:
    from parser_corpus_support import module

    m = module()
    inner = m._tbl([["i1", "i2"]])
    body = f"<w:tbl><w:tr><w:tc>{m._p('outer')}{inner}</w:tc><w:tc>{m._p('x')}</w:tc></w:tr></w:tbl>"
    res = run("parse", m.make_docx(body), "docx", max_pages=50, max_chars=10**6)
    assert "outer" in res.text and "i1" in res.text and "| x |" in res.text
    long = m.make_docx(
        "".join(
            m._p(f"paragrafo {i} " + hashlib.sha256(str(i).encode()).hexdigest() * 3)
            for i in range(300)
        )
    )
    capped = run("parse", long, "docx", max_pages=50, max_chars=1000)
    assert capped.status is FileStatus.READ_PARTIAL and len(capped.text) == 1000
    assert capped.upto_page is None


def test_odt_formula_variants_and_char_cap() -> None:
    res = run("parse", corpus()["odt_valid"].data, "odt", max_pages=50, max_chars=10**6)
    assert "\\frac{a}{b}" in res.text
    assert (
        '<math xmlns="http://www.w3.org/1998/Math/MathML">' not in res.text
    )  # no ns noise
    assert "<math><mi>y</mi></math>" in res.text
    capped = run("parse", corpus()["odt_valid"].data, "odt", max_pages=50, max_chars=20)
    assert capped.status is FileStatus.READ_PARTIAL


def test_odt_object_reference_cannot_escape_the_archive() -> None:
    from parser_corpus_support import module

    m = module()
    for href in ("../x", "/etc/passwd", "http://127.0.0.1:9/x", "a/b"):
        data = m.make_odt(
            f'<text:p>t<draw:frame><draw:object xlink:href="{href}"/></draw:frame></text:p>'
        )
        res = run("parse", data, "odt", max_pages=50, max_chars=1000)
        assert res.status is FileStatus.READ and "127.0.0.1" not in res.text


# --- review HTML ------------------------------------------------------------------


def test_review_html_preserves_source_forms_byte_for_byte() -> None:
    it = corpus()["rv_tex_mathml_code_table"]
    res = sanitize_review_html(it.data, 10**6)
    for s in it.contains:
        assert s in res.text


def test_review_html_drops_executable_and_remote_content() -> None:
    res = sanitize_review_html(corpus()["rv_external_img"].data, 10**6)
    assert "http" not in res.text and "<" not in res.text
    res = sanitize_review_html(
        b"<p>a</p><object data='x'>OBJ</object><embed src='y'><noscript>NS</noscript><style>p{}</style><meta x><base href='http://z'>b",
        1000,
    )
    assert res.text == "a\nb"


def test_review_html_char_cap_and_status() -> None:
    res = sanitize_review_html(b"<p>" + b"x" * 500 + b"</p>", 100)
    assert res.status is FileStatus.READ_PARTIAL and len(res.text) == 100


def test_serialise_roundtrip_shape() -> None:
    res = run("parse", b"hello", "txt", max_pages=50, max_chars=100)
    head, _, body = serialise(res, 100).partition(b"\n")
    assert b'"status": "READ"' in head and body == b"hello"
