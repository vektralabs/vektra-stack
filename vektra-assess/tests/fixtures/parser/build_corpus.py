"""Deterministic synthetic corpus for the parser sidecar (plan assess-parser, task F).

Everything is built at test time, from code, so no zip bomb or hostile file is ever
committed. Each item embeds a unique canary token; the corpus tests assert the canary
never reaches the sidecar's stdout, stderr or logs. No real student data, no network.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
import zlib
from dataclasses import dataclass, field

from vektra_assess.parser_protocol import FileStatus

R, RP, U = FileStatus.READ, FileStatus.READ_PARTIAL, FileStatus.UNSUPPORTED_FORMAT
TL, NT, PR, CO = (
    FileStatus.TOO_LARGE,
    FileStatus.NO_TEXT,
    FileStatus.PROTECTED,
    FileStatus.CORRUPT,
)


@dataclass
class Item:
    name: str
    data: bytes
    ext: str
    expected: FileStatus
    canary: str
    op: str = "parse"
    upto_page: int | None = None
    timeout_ms: int | None = None  # per-item client budget, for the slow items
    magic: bytes = b""  # the generator asserts the file really starts like this
    contains: tuple[str, ...] = ()  # exact substrings expected in the READ text
    forbidden: tuple[str, ...] = ()  # substrings that must not reach the output
    notes: str = ""
    extra: dict[str, object] = field(default_factory=dict)


def canary(name: str) -> str:
    return "CANARY" + hashlib.sha256(name.encode()).hexdigest()[:16].upper()


# --- PDF ------------------------------------------------------------------------


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(
    pages: list[str],
    *,
    encrypt: bool = False,
    extra_objects: list[bytes] | None = None,
    page_chain_depth: int = 0,
    raw_contents: list[bytes] | None = None,
) -> bytes:
    """A small valid text PDF. ``pages`` are page texts (split on newlines)."""
    objs: list[bytes] = []  # object i+1
    n = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    if page_chain_depth:
        base = 3 + 2 * n + 1  # first chain node id
        objs.append(f"<< /Type /Pages /Kids [{base} 0 R] /Count {n} >>".encode())
    else:
        objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    font_id = 3 + 2 * n
    for i, text in enumerate(pages):
        cid = 4 + 2 * i
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {cid} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()
        )
        if raw_contents is not None:
            body = raw_contents[i]
        else:
            lines = text.split("\n") if text else []
            ops = [b"BT /F1 11 Tf 14 TL 40 750 Td"]
            for ln in lines:
                ops.append(f"({_esc(ln)}) Tj T*".encode("latin-1", "replace"))
            ops.append(b"ET")
            body = b"\n".join(ops)
        objs.append(
            b"<< /Length "
            + str(len(body)).encode()
            + b" >>\nstream\n"
            + body
            + b"\nendstream"
        )
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    if page_chain_depth:
        # n pages are reachable only through a chain of nested /Pages nodes
        first = 3 + 2 * n + 1
        for d in range(page_chain_depth):
            nid = first + d
            if d == page_chain_depth - 1:
                objs.append(
                    f"<< /Type /Pages /Parent {nid - 1 if d else 2} 0 R /Kids [{kids}] /Count {n} >>".encode()
                )
            else:
                objs.append(
                    f"<< /Type /Pages /Parent {nid - 1 if d else 2} 0 R /Kids [{nid + 1} 0 R] /Count {n} >>".encode()
                )
    objs.extend(extra_objects or [])
    if encrypt:
        objs.append(
            b"<< /Filter /Standard /V 1 /R 2 /P -4 /O <"
            + b"AB" * 32
            + b"> /U <"
            + b"CD" * 32
            + b"> >>"
        )
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + o + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    trailer = f"<< /Size {len(objs) + 1} /Root 1 0 R"
    if encrypt:
        trailer += f" /Encrypt {len(objs)} 0 R /ID [<{'11' * 16}> <{'11' * 16}>]"
    out.write(f"trailer\n{trailer} >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def _page_text(tag: str, marker: str, lines: int = 4) -> str:
    return "\n".join(
        f"{marker} line {i}: the quick brown fox jumps over the lazy dog {tag}"
        for i in range(lines)
    )


# --- OOXML / ODF ------------------------------------------------------------------

_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_CT_DOCX = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
)


def _zip(entries: list[tuple[str, bytes]], stored_first: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, (name, data) in enumerate(entries):
            ct = (
                zipfile.ZIP_STORED
                if (stored_first and i == 0)
                else zipfile.ZIP_DEFLATED
            )
            z.writestr(
                zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0)), data, compress_type=ct
            )
    return buf.getvalue()


def _p(text: str, style: str | None = None) -> str:
    st = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    t = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f'<w:p>{st}<w:r><w:t xml:space="preserve">{t}</w:t></w:r></w:p>'


def _tbl(rows: list[list[str]]) -> str:
    return (
        "<w:tbl>"
        + "".join(
            "<w:tr>" + "".join(f"<w:tc>{_p(c)}</w:tc>" for c in row) + "</w:tr>"
            for row in rows
        )
        + "</w:tbl>"
    )


def make_docx(
    body_xml: str,
    *,
    content_type: str = _CT_DOCX,
    extra: list[tuple[str, bytes]] | None = None,
    document_xml: bytes | None = None,
) -> bytes:
    doc = (
        document_xml
        or (
            f'<?xml version="1.0" encoding="UTF-8"?><w:document {_W} '
            f'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" '
            f'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">'
            f"<w:body>{body_xml}</w:body></w:document>"
        ).encode()
    )
    ct = (
        '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/word/document.xml" ContentType="{content_type}"/></Types>'
    ).encode()
    return _zip(
        [("[Content_Types].xml", ct), ("word/document.xml", doc), *(extra or [])]
    )


def make_odt(
    content_xml: str, *, encrypted: bool = False, objects: dict[str, str] | None = None
) -> bytes:
    enc = (
        '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml">'
        '<manifest:encryption-data manifest:checksum-type="SHA1/1K" manifest:checksum="AAAA"/></manifest:file-entry>'
        if encrypted
        else ""
    )
    manifest = (
        '<?xml version="1.0" encoding="UTF-8"?><manifest:manifest '
        'xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">'
        f"{enc}</manifest:manifest>"
    ).encode()
    content = (
        '<?xml version="1.0" encoding="UTF-8"?><office:document-content '
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
        'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
        'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
        'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
        'xmlns:xlink="http://www.w3.org/1999/xlink">'
        f"<office:body><office:text>{content_xml}</office:text></office:body></office:document-content>"
    ).encode()
    entries = [
        ("mimetype", b"application/vnd.oasis.opendocument.text"),
        ("META-INF/manifest.xml", manifest),
        ("content.xml", content),
    ]
    for name, xml in (objects or {}).items():
        entries.append((f"{name}/content.xml", xml.encode()))
    return _zip(entries, stored_first=True)


def _zeros_zip(name_sizes: list[tuple[str, int]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, size in name_sizes:
            with z.open(name, "w", force_zip64=size > (1 << 30)) as w:
                chunk = bytes(1 << 20)
                left = size
                while left > 0:
                    w.write(chunk[: min(len(chunk), left)])
                    left -= len(chunk)
    return buf.getvalue()


def _huge_stream_pdf(total: int = 700 << 20) -> bytes:
    """A small file whose one content stream inflates to ``total`` bytes."""
    comp = zlib.compressobj(9)
    chunks, chunk = [], bytes(1 << 20)
    for _ in range(total >> 20):
        chunks.append(comp.compress(chunk))
    chunks.append(comp.flush())
    body = b"".join(chunks)
    stream = (
        b"<< /Length "
        + str(len(body)).encode()
        + b" /Filter /FlateDecode >>\nstream\n"
        + body
        + b"\nendstream"
    )
    return make_pdf(["x"], raw_contents=[b""]).replace(
        b"<< /Length 0 >>\nstream\n\nendstream", stream
    )


def _slow_pdf() -> bytes:
    ops = b"BT /F1 9 Tf 10 10 Td (a) Tj ET\n" * 250_000  # ~8 MB of content operators
    return make_pdf(["x"], raw_contents=[ops])


# --- review HTML (Moodle-shaped) ------------------------------------------------------


def _review(name: str, body: str) -> bytes:
    return (
        '<!DOCTYPE html><html><head><title>Review</title><script>var tok="'
        + canary(name + "-script")
        + '";</script><style>.x{}</style></head><body><div id="page"><div class="que">'
        + body
        + "</div></div></body></html>"
    ).encode()


def build_corpus() -> list[Item]:
    items: list[Item] = []

    def add(
        name: str, data: bytes, ext: str, expected: FileStatus, **kw: object
    ) -> Item:
        it = Item(name, data, ext, expected, canary(name), **kw)  # type: ignore[arg-type]
        items.append(it)
        return it

    def c(name: str) -> str:
        return canary(name)

    # valid documents -----------------------------------------------------------------
    n = "pdf_valid"
    add(
        n,
        make_pdf([_page_text(c(n), "P1"), _page_text(c(n), "P2")]),
        "pdf",
        R,
        magic=b"%PDF-",
        contains=(c(n), "P2 line 3"),
    )
    n = "docx_valid"
    add(
        n,
        make_docx(
            _p("Relazione finale", "Heading1")
            + _p(f"Corpo del testo {c(n)}")
            + _tbl([["cella A1", "cella B1"], ["cella A2", "cella B2"]])
            + _p("def f(x):\n    return x ** 2  # codice")
            + "<w:p><m:oMath><m:r><m:t>E=mc2</m:t></m:r></m:oMath></w:p>"
            + '<w:p><w:r><w:drawing><wp:inline><wp:docPr id="1" name="Img" descr="schema a blocchi"/></wp:inline></w:drawing></w:r></w:p>'
        ),
        "docx",
        R,
        magic=b"PK\x03\x04",
        contains=(
            "# Relazione finale",
            c(n),
            "| cella A1 | cella B1 |",
            "| cella A2 | cella B2 |",
            "def f(x):\n    return x ** 2  # codice",
            "E=mc2",
            "[immagine: schema a blocchi]",
        ),
    )
    n = "odt_valid"
    add(
        n,
        make_odt(
            '<text:h text:outline-level="1">Titolo ODT</text:h>'
            f'<text:p>Paragrafo {c(n)} con <text:span>span</text:span> e<text:s text:c="2"/>spazi</text:p>'
            "<text:list><text:list-item><text:p>voce uno</text:p></text:list-item></text:list>"
            "<table:table><table:table-row><table:table-cell><text:p>a</text:p></table:table-cell>"
            "<table:table-cell><text:p>b</text:p></table:table-cell></table:table-row></table:table>"
            '<text:p><draw:frame><draw:image xlink:href="Pictures/a.png"/><svg:title>logo</svg:title></draw:frame></text:p>'
            '<text:p><draw:frame><draw:object xlink:href="./Object 1"/></draw:frame></text:p>'
            '<text:p><draw:frame><draw:object xlink:href="./Object 2"/></draw:frame></text:p>',
            objects={
                "Object 1": '<math xmlns="http://www.w3.org/1998/Math/MathML"><semantics><mi>x</mi><annotation encoding="application/x-tex">\\frac{a}{b}</annotation></semantics></math>',
                "Object 2": '<math xmlns="http://www.w3.org/1998/Math/MathML"><mi>y</mi></math>',
            },
        ),
        "odt",
        R,
        magic=b"PK\x03\x04",
        contains=(
            "# Titolo ODT",
            c(n),
            "con span e  spazi",
            "- voce uno",
            "| a | b |",
            "[immagine: logo]",
            "\\frac{a}{b}",
            "<math",
        ),
    )
    n = "txt_valid"
    add(
        n,
        f"Appunti {c(n)}\nsecondo rigo con è e ü\n".encode(),
        "txt",
        R,
        contains=(c(n), "è e ü"),
    )
    n = "md_valid"
    add(
        n,
        f"# Titolo\n\n- punto\n\n```py\nprint('{c(n)}')\n```\n".encode(),
        "md",
        R,
        contains=("# Titolo", "```py\nprint('" + c(n) + "')\n```"),
    )
    n = "code_python"
    add(
        n,
        f"import os\nos.system('rm -rf /')  # {c(n)}\nprint('{{}}'.format(1))\n".encode(),
        "py",
        R,
        contains=("os.system('rm -rf /')", c(n)),
    )
    n = "code_java"
    add(
        n,
        f'public class A {{ public static void main(String[] a) {{ Runtime.getRuntime().exec("x"); }} }} // {c(n)}\n'.encode(),
        "java",
        R,
        contains=('Runtime.getRuntime().exec("x")',),
    )
    n = "svg_as_text"
    svg = f'<svg xmlns="http://www.w3.org/2000/svg"><script>alert("{c(n)}")</script><image href="http://127.0.0.1:9/x"/></svg>'
    add(n, svg.encode(), "svg", R, contains=(svg,))
    n = "html_as_text"
    html = f'<html><body onload="fetch(\'http://127.0.0.1:9/\')"><img src="http://127.0.0.1:9/x.png"><script>x="{c(n)}"</script></body></html>'
    add(n, html.encode(), "html", R, contains=(html,))
    n = "latin1_text"
    add(
        n,
        f"caff\xe8 perch\xe9 {c(n)}\n".encode("cp1252"),
        "txt",
        R,
        contains=("caffè perché",),
    )
    n = "text_utf16_bom"
    add(
        n,
        "﻿".encode("utf-16") + f"utf16 {c(n)}".encode("utf-16-le"),
        "txt",
        R,
        contains=("utf16",),
    )
    n = "long_text"
    add(
        n,
        (f"riga {c(n)} " + "x" * 80 + "\n").encode() * 4000,
        "txt",
        RP,
        notes="char cap 200000 reached",
    )
    n = "extensionless_text"
    add(n, f"all:\n\techo {c(n)}\n".encode(), "", R, contains=("echo",))

    # not supported / empty / protected -----------------------------------------------------
    n = "pptx"
    add(
        n,
        _zip(
            [
                (
                    "[Content_Types].xml",
                    b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/></Types>',
                ),
                ("ppt/presentation.xml", b"<p/>"),
                ("c.txt", c(n).encode()),
            ]
        ),
        "pptx",
        U,
        magic=b"PK",
    )
    n = "xlsx"
    add(
        n,
        _zip(
            [
                (
                    "[Content_Types].xml",
                    b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/></Types>',
                ),
                ("xl/workbook.xml", b"<w/>"),
                ("c.txt", c(n).encode()),
            ]
        ),
        "xlsx",
        U,
        magic=b"PK",
    )
    n = "docm_macro"
    add(
        n,
        make_docx(
            _p(c(n)),
            content_type="application/vnd.ms-word.document.macroEnabled.main+xml",
            extra=[("word/vbaProject.bin", b"\xd0\xcf\x11\xe0 macro")],
        ),
        "docm",
        U,
        magic=b"PK",
    )
    n = "png_image"
    add(
        n,
        b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + c(n).encode() * 3,
        "png",
        U,
        magic=b"\x89PNG",
    )
    n = "zip_archive"
    add(n, _zip([("a.txt", c(n).encode()), ("b.txt", b"b")]), "zip", U, magic=b"PK")
    n = "nested_zip"
    inner = _zip([("deep.txt", c(n).encode())])
    add(n, _zip([("inner.zip", inner), ("c.txt", b"x")]), "zip", U, magic=b"PK")
    n = "odf_spreadsheet"
    buf = _zip(
        [
            ("mimetype", b"application/vnd.oasis.opendocument.spreadsheet"),
            ("content.xml", b"<a/>"),
        ],
        stored_first=True,
    )
    add(n, buf, "ods", U, magic=b"PK")
    n = "legacy_doc_ole"
    add(
        n,
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + c(n).encode() * 10 + bytes(600),
        "doc",
        U,
        magic=b"\xd0\xcf\x11\xe0",
    )
    n = "ole_encrypted_docx"
    add(
        n,
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
        + bytes(64)
        + "EncryptionInfo".encode("utf-16-le")
        + c(n).encode()
        + bytes(600),
        "docx",
        PR,
        magic=b"\xd0\xcf\x11\xe0",
    )
    n = "pdf_encrypted"
    add(n, make_pdf([_page_text(c(n), "S")], encrypt=True), "pdf", PR, magic=b"%PDF-")
    n = "odt_encrypted"
    add(n, make_odt(f"<text:p>{c(n)}</text:p>", encrypted=True), "odt", PR, magic=b"PK")
    n = "pdf_scanned"
    add(
        n,
        make_pdf(["", "", ""]),
        "pdf",
        NT,
        magic=b"%PDF-",
        notes="pages with no text layer",
    )
    n = "empty_file"
    add(n, b"", "txt", NT)
    n = "docx_empty_body"
    add(n, make_docx(""), "docx", NT)
    n = "nul_laden_text"
    add(n, b"hello\x00\x00\x00world " + c(n).encode() + b"\x00" * 50, "txt", U)
    n = "binary_unknown"
    add(n, bytes(range(256)) * 8, "bin", U)
    n = "misleading_ext_text_as_pdf"
    add(
        n,
        f"just text {c(n)}".encode(),
        "pdf",
        U,
        notes="text content named .pdf: not a PDF, ext not text",
    )
    n = "misleading_ext_pdf_as_txt"
    add(
        n,
        make_pdf([_page_text(c(n), "M")]),
        "txt",
        R,
        magic=b"%PDF-",
        contains=("M line 0",),
        notes="magic wins over extension",
    )
    n = "pdf_zip_polyglot"
    add(
        n,
        make_pdf([_page_text(c(n), "PZ")]) + make_docx(_p("DOCX-SIDE-" + c(n))),
        "pdf",
        R,
        magic=b"%PDF-",
        contains=("PZ line 0",),
        forbidden=("DOCX-SIDE-",),
        notes="parsed as PDF only",
    )

    # too large / pages ------------------------------------------------------------------
    n = "pdf_51_pages"
    add(
        n,
        make_pdf([_page_text(c(n), f"PAGE{i:03d}") for i in range(1, 52)]),
        "pdf",
        RP,
        upto_page=50,
        magic=b"%PDF-",
        contains=("PAGE050 line 0",),
        forbidden=("PAGE051",),
    )
    n = "file_11mb"
    add(
        n,
        make_pdf(
            [_page_text(c(n), "BIG")],
            extra_objects=[
                b"<< /Length 11534336 >>\nstream\n"
                + bytes(11 * 1024 * 1024)
                + b"\nendstream"
            ],
        ),
        "pdf",
        TL,
        magic=b"%PDF-",
    )

    # hostile containers ------------------------------------------------------------------
    n = "zip_bomb_total"
    add(
        n,
        _zeros_zip([(f"w/p{i}.xml", 30 << 20) for i in range(3)]),
        "docx",
        CO,
        magic=b"PK",
    )
    n = "zip_ratio_bomb"
    add(
        n,
        make_docx("", extra=[("word/media/zeros.bin", bytes(20 << 20))]),
        "docx",
        CO,
        magic=b"PK",
    )
    n = "zip_many_entries"
    add(n, _zip([(f"e{i}.txt", b"x") for i in range(2500)]), "docx", CO, magic=b"PK")
    n = "docx_truncated"
    full = make_docx(_p(c(n) * 50))
    add(n, full[: len(full) // 2], "docx", CO, magic=b"PK")
    n = "docx_xxe_file"
    xxe = (
        f'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///run/secrets/x">]>'
        f"<w:document {_W}><w:body><w:p><w:r><w:t>&x; {c(n)}</w:t></w:r></w:p></w:body></w:document>"
    ).encode()
    add(n, make_docx("", document_xml=xxe), "docx", CO, magic=b"PK")
    n = "docx_xxe_http"
    xxe = (
        f'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "http://127.0.0.1:9/x">]>'
        f"<w:document {_W}><w:body><w:p><w:r><w:t>&x; {c(n)}</w:t></w:r></w:p></w:body></w:document>"
    ).encode()
    add(n, make_docx("", document_xml=xxe), "docx", CO, magic=b"PK")
    n = "docx_billion_laughs"
    ents = '<!ENTITY a0 "lol">' + "".join(
        f'<!ENTITY a{i} "&a{i - 1};&a{i - 1};&a{i - 1};&a{i - 1};&a{i - 1};&a{i - 1};&a{i - 1};&a{i - 1};&a{i - 1};&a{i - 1};">'
        for i in range(1, 10)
    )
    bl = (
        f'<?xml version="1.0"?><!DOCTYPE d [{ents}]><w:document {_W}><w:body><w:p><w:r><w:t>&a9; {c(n)}</w:t></w:r></w:p></w:body></w:document>'
    ).encode()
    add(n, make_docx("", document_xml=bl), "docx", CO, magic=b"PK")
    n = "docx_remote_template"
    add(
        n,
        make_docx(
            _p(f"remote template {c(n)}"),
            extra=[
                (
                    "word/settings.xml",
                    f'<w:settings {_W} xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:attachedTemplate r:id="rId1"/></w:settings>'.encode(),
                ),
                (
                    "word/_rels/settings.xml.rels",
                    b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate" Target="http://127.0.0.1:9/evil.dotm" TargetMode="External"/></Relationships>',
                ),
            ],
        ),
        "docx",
        R,
        contains=("remote template",),
    )
    n = "odt_xxe"
    odt_x = make_odt("<text:p>x</text:p>")
    # rebuild with a DOCTYPE in content.xml
    zin = zipfile.ZipFile(io.BytesIO(odt_x))
    ent = [(i.filename, zin.read(i.filename)) for i in zin.infolist()]
    ent = [
        (
            nm,
            (
                b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
                + d.split(b"?>", 1)[1]
            )
            if nm == "content.xml"
            else d,
        )
        for nm, d in ent
    ]
    add(n, _zip(ent, stored_first=True), "odt", CO, magic=b"PK")
    n = "pdf_deep_pagetree"
    add(
        n,
        make_pdf([_page_text(c(n), "D")], page_chain_depth=3000),
        "pdf",
        CO,
        magic=b"%PDF-",
    )
    n = "pdf_slow_content"
    add(
        n,
        _slow_pdf(),
        "pdf",
        CO,
        magic=b"%PDF-",
        timeout_ms=2500,
        notes="killed at the wall-clock timeout",
    )
    n = "pdf_huge_stream"
    add(
        n,
        _huge_stream_pdf(),
        "pdf",
        CO,
        magic=b"%PDF-",
        notes="inflates to 700 MiB: RLIMIT_AS",
    )

    # review HTML ------------------------------------------------------------------------
    n = "rv_multichoice"
    add(
        n,
        _review(
            n,
            f'<div class="qtext"><p>Domanda {c(n)}: quanto fa 2+2?</p></div><div class="answer">'
            '<div class="r0"><input type="radio" disabled="disabled" checked="checked"><label>a. 4</label></div>'
            '<div class="r1"><input type="radio" disabled="disabled"><label>b. 5</label></div></div>',
        ),
        "html",
        R,
        op="sanitize_html",
        contains=("[x] a. 4", "[ ] b. 5", c(n)),
        forbidden=(c(n + "-script"), "tok="),
    )
    n = "rv_shortanswer"
    add(
        n,
        _review(
            n,
            f'<p>Capitale della Francia? {c(n)}</p><input type="text" value="Parigi" disabled>',
        ),
        "html",
        R,
        op="sanitize_html",
        contains=("[risposta: Parigi]",),
    )
    n = "rv_essay"
    add(
        n,
        _review(
            n,
            f'<p>Spiega.</p><textarea>La mia risposta {c(n)}\n  con rientro</textarea><div class="answer"><p>Testo <b>HTML</b> libero</p></div>',
        ),
        "html",
        R,
        op="sanitize_html",
        contains=(f"La mia risposta {c(n)}\n  con rientro", "Testo HTML libero"),
    )
    n = "rv_match_gapselect"
    add(
        n,
        _review(
            n,
            "<table><tr><td>gatto</td><td><select disabled><option>--</option><option selected>animale</option></select></td></tr></table>"
            f"<p>Il {c(n)} <select><option selected>cane</option><option>auto</option></select> abbaia</p>",
        ),
        "html",
        R,
        op="sanitize_html",
        contains=("| gatto | [selezione: animale] |", "[selezione: cane]"),
        forbidden=("auto",),
    )
    n = "rv_multianswer"
    add(
        n,
        _review(
            n,
            f'<p>{c(n)} <input type="text" value="12"> e <input type="checkbox" checked> ok <input type="hidden" name="sesskey" value="SECRETSESS"></p>',
        ),
        "html",
        R,
        op="sanitize_html",
        contains=("[risposta: 12]", "[x]"),
        forbidden=("SECRETSESS",),
    )
    n = "rv_tex_mathml_code_table"
    tex = r"\( \frac{a}{b} \) e \[ x^{2}+y^{2} \] e $$ \sum_{i=0}^{n} i $$"
    mml = '<math display="block"><mi>x</mi><mo>=</mo><mn>2</mn></math>'
    code = 'for i in range(3):\n    print(i)  # <ok> & "q"'
    add(
        n,
        _review(
            n,
            f"<p>{tex}</p><p>{mml}</p><pre><code>{code.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')}</code></pre>"
            "<table><tr><th>x</th><th>y</th></tr><tr><td>1</td><td>2</td></tr></table>"
            f"<p>{c(n)}</p>",
        ),
        "html",
        R,
        op="sanitize_html",
        contains=(tex, mml, "```\n" + code + "\n```", "| x | y |", "| 1 | 2 |"),
    )
    n = "rv_external_img"
    add(
        n,
        _review(
            n,
            f'<p>{c(n)}</p><img src="http://127.0.0.1:9/track.png" alt="figura 1"><svg><title>diagramma</title><circle r="1"/></svg>'
            '<video src="http://127.0.0.1:9/v.mp4"></video><a href="http://127.0.0.1:9/l">collegamento</a>'
            '<iframe src="http://127.0.0.1:9/f"></iframe><link rel="stylesheet" href="http://127.0.0.1:9/c.css">',
        ),
        "html",
        R,
        op="sanitize_html",
        contains=(
            "[immagine non visibile all'assistente] figura 1",
            "[immagine non visibile all'assistente] diagramma",
            "[contenuto multimediale non visibile all'assistente]",
            "collegamento",
        ),
        forbidden=("127.0.0.1", "http://"),
    )
    n = "rv_deep_nesting"
    add(
        n,
        _review(n, "<div>" * 5000 + f"deep {c(n)}" + "</div>" * 5000),
        "html",
        R,
        op="sanitize_html",
        contains=("deep",),
    )
    n = "rv_empty"
    add(
        n,
        b"<html><body><script>1</script></body></html>",
        "html",
        NT,
        op="sanitize_html",
    )

    for it in items:
        if it.magic:
            assert it.data.startswith(it.magic), (
                f"{it.name}: generator produced the wrong magic"
            )
        if it.name not in ("empty_file", "rv_empty"):
            assert len(it.data) > 0, it.name
    return items
