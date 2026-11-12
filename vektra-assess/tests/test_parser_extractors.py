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
    # 0x81 0x8D 0x8F 0x90 0x9D are undefined in cp1252: legacy text holding one is still text
    assert decode(b"caff\xe8 \x81\x8d\x8f\x90\x9d") == "caff\xe8 \x81\x8d\x8f\x90\x9d"
    legacy = extract_text(b"perch\xe9 \x81 ok", 100)
    assert legacy.status is FileStatus.READ and legacy.text.startswith("perch\xe9")
    assert decode(b"\xff\xfe\x61") is None  # only a broken UTF-16 text is undecodable
    assert extract_text(b"\xff\xfe\x61", 100).status is FileStatus.NO_TEXT
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


# --- review fixes -----------------------------------------------------------------


@pytest.mark.parametrize(
    "head",
    [
        b"ID3 tags are metadata",
        b"BZh is how a bzip2 stream starts",
        b"RIFF files are containers",
        b"OggS pages",
        b"fLaC frames",
        b"GIF89a is a format",
    ],
)
def test_prose_starting_like_a_weak_magic_is_text(head: bytes) -> None:
    for ext in ("txt", "md", ""):
        assert sniff(head + b"\nand some more text\n", ext).kind is Kind.TEXT, (
            head,
            ext,
        )
        res = run("parse", head + b"\nmore\n", ext, max_pages=50, max_chars=1000)
        assert res.status is FileStatus.READ


def test_weak_magic_still_refuses_real_binaries_and_non_text_extensions() -> None:
    assert (
        sniff(b"ID3\x03\x00\x00\x00\x00\x00\x21" + bytes(100), "mp3").kind
        is Kind.REFUSED
    )
    assert (
        sniff(b"BZh91AY&SY\xd4\x1f" + bytes(50) + b"\x00", "txt").kind is Kind.REFUSED
    )  # NULs
    assert (
        sniff(b"RIFF\x24\x08\x00\x00WAVEfmt " + bytes(40), "txt").kind is Kind.REFUSED
    )
    assert (
        sniff(b"ID3 looks like text", "mp3").kind is Kind.REFUSED
    )  # extension says audio
    assert sniff(b"BZh text", "bz2").kind is Kind.REFUSED
    assert (
        sniff(b"\x89PNG\r\n\x1a\nreadable?", "txt").kind is Kind.REFUSED
    )  # strong: always
    assert sniff(b"\x00\x00\x00\x18ftypmp42" + bytes(20), "mp4").kind is Kind.REFUSED


def test_docx_text_box_does_not_lose_the_outer_paragraph() -> None:
    from parser_corpus_support import module

    m = module()
    body = (
        '<w:p><w:r><w:t xml:space="preserve">before </w:t></w:r>'
        "<w:r><w:pict><w:txbxContent><w:p><w:r><w:t>inside the box</w:t></w:r></w:p></w:txbxContent></w:pict></w:r>"
        '<w:r><w:t xml:space="preserve">after</w:t></w:r></w:p>'
    )
    res = run("parse", m.make_docx(body), "docx", max_pages=50, max_chars=10**6)
    assert res.status is FileStatus.READ
    assert "before after" in res.text and "inside the box" in res.text


def _hex_text(n_chars: int) -> str:
    out, i = [], 0
    while sum(map(len, out)) < n_chars:
        out.append(hashlib.sha256(str(i).encode()).hexdigest())
        i += 1
    return "".join(out)[:n_chars]


def _peak_bytes(fn):  # type: ignore[no-untyped-def]
    import tracemalloc

    tracemalloc.start()
    try:
        res = fn()
        return res, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_one_giant_paragraph_ends_partial_at_the_cap_not_in_memory() -> None:
    from parser_corpus_support import module

    m = module()
    giant = _hex_text(2_500_000)  # one 2.5 MB paragraph
    docx = m.make_docx(m._p(giant))
    odt = m.make_odt(f"<text:p>{giant}</text:p>")
    for data, ext in ((docx, "docx"), (odt, "odt")):
        res, peak = _peak_bytes(
            lambda d=data, e=ext: run("parse", d, e, max_pages=50, max_chars=5000)
        )
        assert res.status is FileStatus.READ_PARTIAL, (ext, res.status, res.code)
        assert len(res.text) == 5000 and res.text == giant[:5000]
        # the paragraph was never buffered whole (it would cost 2.5 MB here, twice with the join)
        assert peak < 1_200_000, (ext, peak)


def test_giant_table_cell_and_math_are_capped_too() -> None:
    from parser_corpus_support import module

    m = module()
    giant = _hex_text(1_500_000)
    cell = m.make_docx(m._tbl([[giant, "x"]]))
    math = m.make_docx(f"<w:p><m:oMath><m:r><m:t>{giant}</m:t></m:r></m:oMath></w:p>")
    for data in (cell, math):
        res, peak = _peak_bytes(
            lambda d=data: run("parse", d, "docx", max_pages=50, max_chars=2000)
        )
        assert res.status is FileStatus.READ_PARTIAL and len(res.text) == 2000
        assert peak < 900_000


def test_tex_scan_is_linear_on_a_wall_of_unmatched_openers() -> None:
    import time

    from vektra_assess.parser_sidecar.html_review import _collapse

    wall = "\\(" * 1_000_000  # 2 MB, a million unmatched openers
    t0 = time.monotonic()
    out = _collapse(wall + " x  y")
    took = time.monotonic() - t0
    assert took < 5, f"{took:.1f}s"
    assert out.endswith("x y")
    # the whole path, as a review page node
    html = b"<p>$$ a$$</p><p>" + (b"\\[ " * 600_000) + b"</p>"
    t0 = time.monotonic()
    res = sanitize_review_html(html, 10**6)
    assert time.monotonic() - t0 < 20 and "$$ a$$" in res.text


def test_tex_spans_keep_verbatim_and_a_too_long_span_is_prose() -> None:
    from vektra_assess.parser_sidecar.html_review import MAX_TEX_SPAN, _collapse

    assert _collapse(r"a  \(  x   y  \)  b") == r"a \(  x   y  \) b"
    assert _collapse("$$ a  b $$ and  \\[ c \\]") == "$$ a  b $$ and \\[ c \\]"
    long_span = "\\(" + "x  " * MAX_TEX_SPAN + "\\)"
    assert "x x" in _collapse(long_span)  # not verbatim: collapsed like prose
    assert _collapse("\\( never closed   here") == "\\( never closed here"


# --- independent review: PlainText contract and invisible characters ---------------------


def test_markup_like_review_output_is_plain_text_that_an_escaper_neutralises() -> None:
    import html5lib

    from vektra_assess.parser_protocol import PlainText, html_escape

    html = (
        b"<p>&lt;img src=x onerror=alert(1)&gt; and &lt;script&gt;steal()&lt;/script&gt;</p>"
        b'<math><mi onclick="evil()">x</mi><mo>&lt;</mo></math>'
    )
    res = sanitize_review_html(html, 10_000)
    text = PlainText(res.text)
    # the sanitiser does NOT escape: decoded entities and rebuilt MathML are literal markup
    assert "<img src=x onerror=alert(1)>" in text and "<script>steal()</script>" in text
    assert (
        '<mi onclick="evil()">' in text
    )  # MathML without a TeX annotation, via serialize_xml
    # a renderer that follows the contract (html_escape) leaves nothing that parses as an element
    escaped = html_escape(text)
    assert "<" not in escaped and ">" not in escaped
    doc = html5lib.parse(
        f"<div>{escaped}</div>", treebuilder="etree", namespaceHTMLElements=False
    )
    tags = {e.tag for e in doc.iter()} - {"html", "head", "body", "div"}
    assert tags == set(), tags
    # ... while the same text dropped into HTML unescaped DOES create elements (why the type exists)
    raw = html5lib.parse(
        f"<div>{text}</div>", treebuilder="etree", namespaceHTMLElements=False
    )
    assert {"img", "script"} <= {e.tag for e in raw.iter()}
    assert html_escape(PlainText("\"q\" & 'a'")) == "&quot;q&quot; &amp; &#x27;a&#x27;"


async def test_the_client_returns_plain_text_for_review_html(tmp_path) -> None:
    import shutil
    import tempfile
    from pathlib import Path

    from vektra_assess.parser_sidecar.server import ParserServer, ServerConfig
    from vektra_assess.worker.parser_client import ParserClient, SanitizeResult

    class Fake(ParserServer):
        def _supervise(
            self, op, data, ext, max_pages, max_chars, timeout_s, profile=None
        ):  # type: ignore[no-untyped-def]
            from vektra_assess.parser_sidecar.child import run
            from vektra_assess.parser_sidecar.server import ChildOutcome

            return ChildOutcome(
                run(op, data, ext, max_pages=max_pages, max_chars=max_chars),
                1,
                0,
                0,
                False,
            )

    d = Path(tempfile.mkdtemp(prefix="vp", dir="/tmp"))
    d.chmod(0o700)
    srv = Fake(ServerConfig(socket_path=d / "p.sock"))
    await srv.start()
    try:
        res = await ParserClient(d / "p.sock").sanitize_html(
            b"<p>&lt;b&gt;x&lt;/b&gt;</p>"
        )
        assert isinstance(res, SanitizeResult) and res.text == "<b>x</b>"
        assert res.label == "letto"
    finally:
        await srv.close()
        shutil.rmtree(d, ignore_errors=True)


def test_clean_drops_invisible_and_reordering_characters_but_keeps_joiners() -> None:
    dropped = "\u0080\u0085\u009f‪‫‬‭‮⁦⁧⁨⁩​⁠﻿"
    assert clean(f"a{dropped}b") == "ab"
    for keep in ("‍", "‌", "‎", "‏"):
        assert clean(f"a{keep}b") == f"a{keep}b", hex(ord(keep))
    # real scripts that need ZWJ/ZWNJ survive; a bidi-override "trojan source" does not
    assert clean("क्‍ष") == "क्‍ष"
    assert clean("access = 'user‮ ⁦// admin⁩ ⁦'") == "access = 'user // admin '"
    # legacy text decoded through latin-1 sheds its C1 controls too
    res = extract_text(b"perch\xe9 \x81\x8d ok", 100)
    assert res.text == "perch\xe9  ok"


def test_overlap_check_counts_the_local_filename_and_extra_field() -> None:
    """Entry b is pointed into the tail of entry a's real span (local header = 30 bytes +
    a 200-byte name + data). The old check (30 + compressed size) saw no overlap."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("n" * 200, b"A" * 100)
        z.writestr("b", b"B" * 100)
    raw = bytearray(buf.getvalue())
    a_off = 0
    old_end = a_off + 30 + 100  # what the previous check believed
    new_b = old_end + 20  # beyond the old end, inside a's name+data
    assert new_b < a_off + 30 + 200 + 100
    cds = [i for i in range(len(raw)) if raw[i : i + 4] == b"PK\x01\x02"]
    raw[cds[1] + 42 : cds[1] + 46] = new_b.to_bytes(4, "little")
    with pytest.raises(zipguard.ZipRefused, match="overlap"):
        zipguard.open_checked(bytes(raw))
    assert zipguard.open_checked(buf.getvalue()).names  # the untouched archive is fine


def _pdf(content: bytes, *, image: bool, form: bool = False) -> bytes:
    """A minimal one-page PDF: optional 8x8 image XObject, and ``content`` as its page stream.

    ``form``: the page resources also list ``/Fm0``, a Form XObject that paints ``/Im0`` across
    the page, so a scan can be drawn one level down (``/Fm0 Do``)."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    ]
    xobjs = (b"/Im0 6 0 R" if image else b"") + (b" /Fm0 7 0 R" if form else b"")
    res = b"/Font << /F1 5 0 R >>" + (
        b" /XObject << " + xobjs + b" >>" if xobjs else b""
    )
    objs.append(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << "
        + res
        + b" >> >>"
    )
    objs.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    pix = b"00" * 192
    objs.append(
        b"<< /Type /XObject /Subtype /Image /Width 8 /Height 8 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /ASCIIHexDecode /Length %d >>\nstream\n"
        % (len(pix) + 1)
        + pix
        + b">\nendstream"
    )
    if form:
        draw = b"q 600 0 0 780 6 6 cm /Im0 Do Q"
        objs.append(
            b"<< /Type /XObject /Subtype /Form /BBox [0 0 612 792] /Resources << /XObject << /Im0 6 0 R >> >> /Length %d >>\nstream\n"
            % len(draw)
            + draw
            + b"\nendstream"
        )
    out, offsets = b"%PDF-1.4\n", []
    for n, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1,
        xref,
    )


@pytest.mark.parametrize(
    ("content", "image", "status"),
    [
        (
            b"BT /F1 18 Tf 72 720 Td (Una riga sola) Tj ET",
            False,
            FileStatus.READ,
        ),  # short text PDF
        (
            b"q 600 0 0 780 6 6 cm /Im0 Do Q BT /F1 8 Tf 500 20 Td (Pag. 1/20) Tj ET",
            True,
            FileStatus.NO_TEXT,
        ),  # scan + footer
        (b"q 600 0 0 780 6 6 cm /Im0 Do Q", True, FileStatus.NO_TEXT),  # bare scan
        (
            b"q 40 0 0 40 20 740 cm /Im0 Do Q BT /F1 18 Tf 72 700 Td (Una riga sola) Tj ET",
            True,
            FileStatus.READ,
        ),  # short text page with a small logo
        (
            b"BT /F1 18 Tf 72 720 Td (Una riga sola) Tj ET",
            True,
            FileStatus.READ,
        ),  # image listed in the resources, never drawn
    ],
)
def test_scanned_detection_uses_page_images_not_a_character_count(
    content: bytes, image: bool, status: FileStatus
) -> None:
    assert extract_pdf(_pdf(content, image=image), 50, 1000).status is status


def test_a_scan_drawn_inside_a_form_xobject_is_detected() -> None:
    footer = b"/Fm0 Do BT /F1 8 Tf 500 20 Td (Pag. 1/20) Tj ET"
    assert (
        extract_pdf(_pdf(footer, image=True, form=True), 50, 1000).status
        is FileStatus.NO_TEXT
    )
