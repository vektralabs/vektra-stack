"""Type detection from content (no libmagic): REQ-101, COMP-008.

The magic bytes decide; the extension is only a hint, and only for plain text. A file
whose extension and magic disagree is judged by its magic (a PDF named ``.txt`` is a PDF,
a zip-with-PDF-header polyglot is parsed as a PDF only).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError, fromstring

from vektra_assess.parser_protocol import FileStatus
from vektra_assess.parser_sidecar.zipguard import ZipRefused, open_checked


class Kind(str, Enum):
    PDF = "pdf"
    DOCX = "docx"
    ODT = "odt"
    TEXT = "text"
    REFUSED = "refused"  # carries the status


@dataclass(frozen=True)
class Sniffed:
    kind: Kind
    status: FileStatus | None = None  # set when kind is REFUSED
    code: str = ""


TEXT_EXTENSIONS = frozenset(
    "txt md markdown rst csv tsv log json jsonl yaml yml toml ini cfg conf env xml "
    "html htm xhtml svg css scss js mjs cjs ts tsx jsx vue py pyi ipynb java kt kts "
    "scala groovy gradle c h cc cpp cxx hpp hh cs go rs rb php swift m mm sh bash "
    "zsh fish ps1 bat cmd sql r jl lua pl pm tex bib dart hs ml fs clj ex exs erl "
    "asm s properties dockerfile makefile cmake proto graphql tf".split()
)

# Binary signatures: no text file starts with these.
_STRONG_SIGNATURES: tuple[tuple[bytes, int], ...] = (
    (b"\x89PNG\r\n\x1a\n", 0),
    (b"\xff\xd8\xff", 0),
    (b"\x1f\x8b", 0),
    (b"7z\xbc\xaf\x27\x1c", 0),
    (b"Rar!\x1a\x07", 0),
    (b"\xfd7zXZ\x00", 0),
    (b"\x7fELF", 0),
    (b"II*\x00", 0),
    (b"MM\x00*", 0),
    (b"\x00\x00\x01\x00", 0),
    (b"\x1a\x45\xdf\xa3", 0),  # matroska / webm
)
# Short ASCII magics that a text file can legitimately start with ("ID3 tags are...",
# "BZh..."): they only count when the content is not plain text of a text extension.
_WEAK_SIGNATURES: tuple[tuple[bytes, int], ...] = (
    (b"GIF87a", 0),
    (b"GIF89a", 0),
    (b"RIFF", 0),
    (b"BZh", 0),
    (b"ID3", 0),
    (b"OggS", 0),
    (b"fLaC", 0),
    (b"ftyp", 4),
)
_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_OLE_ENCRYPTED = "EncryptionInfo".encode("utf-16-le")
_ODT_MIME = b"application/vnd.oasis.opendocument.text"
_DOCX_MAIN = "wordprocessingml.document.main+xml"
_PK = b"PK\x03\x04"


def _refuse(code: str, status: FileStatus = FileStatus.UNSUPPORTED_FORMAT) -> Sniffed:
    return Sniffed(Kind.REFUSED, status, code)


def _looks_like_text(data: bytes) -> bool:
    head = data[:8192]
    if b"\x00" in head:
        return False
    if not head:
        return True
    printable = sum(
        1 for b in head if b >= 0x80 or b in (9, 10, 12, 13) or 32 <= b < 127
    )
    return printable / len(head) >= 0.95


def _sniff_zip(data: bytes) -> Sniffed:
    try:
        gz = open_checked(data)
        if gz.is_encrypted():
            return _refuse("zip_encrypted", FileStatus.PROTECTED)
        first = gz.zf.infolist()[0].filename if gz.names else ""
        if first == "mimetype":
            mime = gz.read("mimetype", 256).strip()
            if mime == _ODT_MIME:
                return Sniffed(Kind.ODT)
            return _refuse("odf_other")
        if "[Content_Types].xml" not in gz.names:
            return _refuse("zip_archive")
        if any(n.lower().endswith("vbaproject.bin") for n in gz.names):
            return _refuse("macro_enabled")
        types = fromstring(gz.read("[Content_Types].xml", 1 << 20), forbid_dtd=True)
        ctypes = {
            (el.get("PartName") or ""): (el.get("ContentType") or "")
            for el in types.iter()
            if el.tag.endswith("Override")
        }
        main = ctypes.get("/word/document.xml", "")
        if "macroEnabled" in main or "macroEnabled" in "".join(ctypes.values()):
            return _refuse("macro_enabled")
        if main.endswith(_DOCX_MAIN):
            return Sniffed(Kind.DOCX)
        return _refuse("ooxml_other")
    except ZipRefused as exc:
        return _refuse(exc.code, FileStatus.CORRUPT)
    except (ParseError, DefusedXmlException, ValueError):
        return _refuse("content_types_unreadable", FileStatus.CORRUPT)


def sniff(data: bytes, ext_hint: str) -> Sniffed:
    if data.startswith(b"%PDF-"):
        return Sniffed(Kind.PDF)
    if data.startswith(_PK):
        return _sniff_zip(data)
    if data.startswith(_OLE):
        if _OLE_ENCRYPTED in data:
            return _refuse("ole_encrypted", FileStatus.PROTECTED)
        return _refuse("ole_legacy")
    for sig, off in _STRONG_SIGNATURES:
        if data[off : off + len(sig)] == sig:
            return _refuse("binary_signature")
    ext = ext_hint.lower().lstrip(".")
    plain_text = _looks_like_text(data) and (not ext or ext in TEXT_EXTENSIONS)
    if not plain_text:
        for sig, off in _WEAK_SIGNATURES:
            if data[off : off + len(sig)] == sig:
                return _refuse("binary_signature")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")) and ext in TEXT_EXTENSIONS:
        return Sniffed(Kind.TEXT)  # UTF-16 with BOM
    if not _looks_like_text(data):
        return _refuse("binary_content")
    if ext and ext not in TEXT_EXTENSIONS:
        return _refuse("extension_not_text")
    return Sniffed(Kind.TEXT)
