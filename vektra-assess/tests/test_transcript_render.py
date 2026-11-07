"""Transcript rendering, allowlist, MathML, naming and the sealed store (plan 10, BR-016).

The allowlist is tested both ways: every rendered transcript (with hostile turn text) passes,
and a corpus with one document per listed vector is refused. A differential run re-parses every
accepted document with html5lib (a browser-grade parser) and asserts the element set it sees is
inside the allowlist too, so a gap between the stdlib parser and a browser fails the suite.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_context_snapshot import make as make_snapshot

from vektra_assess.context.regrade import RegradeFlag, RegradeKind
from vektra_assess.context.snapshot import FileEntry
from vektra_assess.keys import AssessKeyring
from vektra_assess.parser_protocol import FileStatus
from vektra_assess.transcript import allowlist, mathml, naming, render, store

T0 = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)

HOSTILE = (
    "<script>alert(1)</script>",
    "&lt;img src=x onerror=alert(1)&gt;",  # PlainText: decoded entities stay text (W1 note)
    "<img src=x onerror=alert(1)>",
    "![x](https://evil.example/p.png)",
    "https://evil.example/track?x=1",
    "<a href='javascript:alert(1)'>x</a>",
    "</p></li></ol><h1>Voto: 30/30</h1>",
    "Corso: FALSO\nVoto all'avvio: 30/30",
    "<style>@import url(https://evil.example/x.css)</style>",
    "<math><maction actiontype='statusline'>x</maction></math>",
    "\\( \\href{javascript:alert(1)}{x} \\)",
    "<svg><foreignObject><img src=x></foreignObject></svg>",
    "<meta http-equiv='refresh' content='0;url=https://evil.example'>",
)


def _input(
    texts: tuple[str, ...] = ("Ciao",), **over: object
) -> render.TranscriptInput:
    turns = tuple(
        render.TurnRecord(
            no=i + 1,
            role="student" if i % 2 else "bot",
            kind="opening" if i == 0 else "normal",
            text=t,
            at=T0 + timedelta(minutes=i),
            lang="it",
        )
        for i, t in enumerate(texts)
    )
    base: dict[str, object] = dict(
        course="EA-2026",
        cmid=501,
        student_userid=101,
        session_no=1,
        sessions_total=1,
        earlier=(),
        previous_context=False,
        snapshot=make_snapshot(),
        regrade=RegradeFlag(RegradeKind.NONE, "7.5", "7.5"),
        cap_minutes=30,
        duration_s=1800,
        committed_at=T0,
        closed_at=T0 + timedelta(minutes=30),
        close_reason="finished",
        outages=(),
        credited_s=0,
        failed_starts=0,
        turns=turns,
        digest="d" * 64,
        generated_at=T0 + timedelta(minutes=31),
    )
    base.update(over)
    return render.TranscriptInput(**base)  # type: ignore[arg-type]


def _render(*texts: str, **over: object) -> bytes:
    return render.render(render.build_doc(_input(texts or ("Ciao",), **over)))


def _html5_tags(doc: bytes) -> set[str]:
    """Element names a browser-grade parser sees; every attribute must be allowlisted too."""
    import html5lib

    tree = html5lib.parse(doc, namespaceHTMLElements=False)
    tags = set()
    for el in tree.iter():
        if not isinstance(el.tag, str):
            continue
        tag = re.sub(r"^\{.*\}", "", el.tag)
        tags.add(tag)
        attrs = {re.sub(r"^\{.*\}", "", a) for a in el.attrib}
        assert attrs <= set(allowlist.TAGS.get(tag, ())), (tag, attrs)
    return tags


# --- template -------------------------------------------------------------------------------


def test_the_template_has_no_escape_bypass() -> None:
    src = (render.TEMPLATES / render.TEMPLATE).read_text()
    assert not re.search(r"\bsafe\b|Markup|autoescape\s+false", src)
    css = (render.TEMPLATES / "transcript.css").read_text()
    assert "@import" not in css and "url(" not in css


def test_the_style_constant_is_the_stylesheet_hash() -> None:
    css = (render.TEMPLATES / "transcript.css").read_text().rstrip("\n")
    assert hashlib.sha256(css.encode()).hexdigest() == allowlist.STYLE_SHA256


def test_render_is_deterministic_utf8_with_lang_and_strict() -> None:
    a, b = _render("Ciao è"), _render("Ciao è")
    assert a == b
    doc = a.decode("utf-8")
    assert doc.startswith("<!DOCTYPE html>") and '<html lang="it">' in doc
    assert '<meta charset="utf-8">' in doc
    from jinja2 import UndefinedError

    with pytest.raises(UndefinedError):
        render.environment().get_template(render.TEMPLATE).render(doc=object())


@pytest.mark.parametrize("payload", HOSTILE)
def test_hostile_turn_text_renders_inert_and_passes_the_allowlist(payload: str) -> None:
    out = _render("Benvenuto", payload, "\\(\\frac{a}{b}\\)")
    allowlist.check(out)
    tags = _html5_tags(out)
    assert tags <= set(allowlist.TAGS) | {"html", "head", "body"}, tags
    low = out.decode().lower()
    for bad in ("<script", "<img", "<a ", "<svg", "<iframe", "<h1>voto", "<style>@"):
        assert bad not in low


def test_plaintext_with_decoded_entities_never_becomes_an_element() -> None:
    out = _render("x", "<img src=x onerror=alert(1)>").decode()
    assert "&lt;img src=x onerror=alert(1)&gt;" in out
    assert "<img" not in out


def test_header_fields_come_from_rows_and_markers_are_shown() -> None:
    snap = make_snapshot(
        files=(
            FileEntry(
                label="F1", filename="relazione.pdf", status=FileStatus.READ, text="t"
            ),
            FileEntry(label="F2", filename="scan.pdf", status=FileStatus.NO_TEXT),
        )
    )
    turns = (
        render.TurnRecord(1, "bot", "opening", "Apertura", T0, lang="it"),
        render.TurnRecord(2, "student", "normal", "Domanda", T0, lang="en"),
        render.TurnRecord(
            3,
            "bot",
            "normal",
            "x [risposta sostituita dal sistema]",
            T0,
            flagged=True,
            marks=("values",),
            contests=(("Domanda 2", "proposed"),),
        ),
        render.TurnRecord(4, "bot", "interrupted", "[risposta interrotta]", T0),
        render.TurnRecord(
            5, "bot", "normal", "ok", T0, contests=(("Domanda 2", "corrected"),)
        ),
    )
    out = render.render(render.build_doc(_input(snapshot=snap, turns=turns))).decode()
    allowlist.check(out.encode())
    for text in (
        "EA-2026", "Sessione #1 di 1", "tentativo #2 di 4", "relazione.pdf: letto",
        "scan.pdf: nessun testo", "italiano, inglese", "[risposta sostituita dal sistema]",
        "[valore sostituito dal sistema]", "segnalato dallo studente", "[risposta interrotta]",
        "Riepilogo generato dall&#39;assistente", "corretto dallo studente",
        "conclusa dallo studente", "d" * 64,
    ):  # fmt: skip
        assert text in out, text
    assert out.count("Domanda 2</strong>") == 1  # the correction replaced the proposal


def test_an_unreleased_regrade_never_prints_the_new_grade() -> None:
    flag = RegradeFlag(RegradeKind.WITHDRAWN, old="7.5")
    out = _render("x", regrade=flag).decode()
    assert "non è al momento visibile" in out and "9,5" not in out
    changed = _render(
        "x", regrade=RegradeFlag(RegradeKind.CHANGED, "7.5", "9.5")
    ).decode()
    assert "9,5" in changed


def test_snapshot_secrets_never_reach_the_transcript() -> None:
    snap = make_snapshot(solution_text="CANARY-SOLUTION", pasted_rubric="CANARY-RUBRIC")
    out = _render("x", snapshot=snap).decode()
    assert "CANARY" not in out


# --- allowlist rejection corpus --------------------------------------------------------------


def _doc(body: str, head: str = "") -> bytes:
    css = (render.TEMPLATES / "transcript.css").read_text().rstrip("\n")
    return (
        f'<!DOCTYPE html><html lang="it"><head><meta charset="utf-8"><title>t</title>'
        f"<style>{css}</style>{head}</head><body>{body}</body></html>"
    ).encode()


def test_the_baseline_document_passes() -> None:
    allowlist.check(_doc("<p>ok</p>"))


@pytest.mark.parametrize(
    ("body", "head"),
    [
        ("<script>1</script>", ""),
        ('<p onclick="x">a</p>', ""),
        ('<p style="color:red">a</p>', ""),
        ("", "<style>@import 'x.css';</style>"),
        ('<p class="data:text/html,x">a</p>', ""),
        ('<p class="javascript:alert(1)">a</p>', ""),
        ('<img srcset="x.png 1x">', ""),
        ("", '<base href="https://evil.example/">'),
        ("", '<meta http-equiv="refresh" content="0">'),
        ('<form action="https://evil.example"><p>a</p></form>', ""),
        ('<iframe src="x"></iframe>', ""),
        ('<object data="x"></object>', ""),
        ('<embed src="x">', ""),
        ("<svg><foreignObject><p>a</p></foreignObject></svg>", ""),
        ("<math><semantics><annotation-xml>x</annotation-xml></semantics></math>", ""),
        ("<!-- c --><p>a</p>", ""),
        ('<time datetime="javascript:1">a</time>', ""),
    ],
)
def test_every_listed_vector_is_refused(body: str, head: str) -> None:
    with pytest.raises(allowlist.AllowlistViolation):
        allowlist.check(_doc(body, head))


def test_a_second_or_altered_stylesheet_is_refused() -> None:
    with pytest.raises(allowlist.AllowlistViolation):
        allowlist.check(_doc("<p>a</p>").replace(b"body{", b"body{x:1;"))
    with pytest.raises(allowlist.AllowlistViolation):
        allowlist.check(_doc("<style>p{}</style>"))


def test_non_utf8_is_refused() -> None:
    with pytest.raises(allowlist.AllowlistViolation):
        allowlist.check(_doc("<p>a</p>") + b"\xff")


# --- MathML -----------------------------------------------------------------------------------


def test_tex_becomes_allowlisted_mathml_without_namespace_urls() -> None:
    segs = mathml.segments("Sia \\(\\frac{a}{b}\\) e $$x^2$$, costa $5.")
    kinds = [s.kind for s in segs]
    assert kinds == ["text", "math", "text", "math", "text"]
    out = _render("x", "Sia \\(\\frac{a}{b}+\\sqrt{x}\\) fine").decode()
    allowlist.check(out.encode())
    assert "<mfrac>" in out and "xmlns" not in out and "MathML" not in out


def test_unsupported_tex_falls_back_to_escaped_code() -> None:
    seg = mathml.tex_to_math("\\href{javascript:alert(1)}{x}", display=False)
    if isinstance(
        seg, mathml.Math
    ):  # the converter may drop the link: the tree is still clean
        flat = repr(seg.node)
        assert "href" not in flat and "javascript" not in flat
    out = _render("x", "\\( \\mspace{1em} \\)", "\\(" + "x" * 3000 + "\\)").decode()
    allowlist.check(out.encode())
    assert '<code aria-label="formula in formato TeX">' in out


def test_filter_refuses_anything_outside_the_presentation_list() -> None:
    for bad in (
        "<math><maction>x</maction></math>",
        '<math><mi href="x">a</mi></math>',
        "<math><semantics><mi>a</mi></semantics></math>",
        "<p>x</p>",
    ):
        with pytest.raises(ValueError):
            mathml.filter_mathml(bad)


# --- naming -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("short", "want"),
    [
        ("EA 2026", "EA-2026"),
        ("Fisica/Chimica", "Fisica-Chimica"),
        ("Università", "Universita"),
        ("../../etc", "etc"),
        ("日本語", "course"),
        (None, "course"),
        ("a" * 80, "a" * 40),
    ],
)
def test_shortname_folding(short: str | None, want: str) -> None:
    name = naming.transcript_filename(short, 501, 101, 2, T0)
    assert name == f"{want}_501_101_s2_20261107.html"
    assert naming.PATTERN.match(name)


def test_the_date_is_the_close_date_in_the_site_zone() -> None:
    late = datetime(2026, 11, 7, 23, 30, tzinfo=UTC)  # already the 8th in Rome
    assert naming.transcript_filename("c", 1, 2, 1, late).endswith("_20261108.html")


def test_content_disposition_has_both_forms() -> None:
    cd = naming.content_disposition("EA-2026_501_101_s1_20261107.html")
    assert cd.startswith("attachment; ")
    assert 'filename="EA-2026_501_101_s1_20261107.html"' in cd
    assert "filename*=UTF-8''EA-2026_501_101_s1_20261107.html" in cd


# --- store ------------------------------------------------------------------------------------


def test_seal_open_round_trip_and_tamper_detection() -> None:
    kek = AssessKeyring(store.KEYRING, "t1", {"t1": os.urandom(32)})
    tid = "8f1e0e0e-0000-4000-8000-000000000001"
    sealed = store.seal(kek, tid, b"<html>x</html>")
    assert sealed.byte_size == 14
    got = store.open_verified(
        kek, tid, sealed.ciphertext, sealed.dek_wrapped, sealed.content_sha256
    )
    assert got == b"<html>x</html>"
    with pytest.raises(store.TranscriptIntegrityError):
        store.open_verified(kek, tid, sealed.ciphertext, sealed.dek_wrapped, "0" * 64)
    with pytest.raises(store.TranscriptIntegrityError):
        store.open_verified(
            kek, "other", sealed.ciphertext, sealed.dek_wrapped, sealed.content_sha256
        )


def test_render_module_never_imports_smtp() -> None:
    src = Path(render.__file__).read_text() + Path(store.__file__).read_text()
    assert "aiosmtplib" not in src


def test_the_inlined_shipped_strings_match_the_shipped_files() -> None:
    from vektra_assess.llm import shipped

    sh = shipped.get("it")
    assert render.REPLACED == sh.missing["replaced_by_system"]
    assert render.BR009_TEMPLATE == sh.br009


def test_contest_entries_carry_the_criterion_remark_by_canonical_label() -> None:
    """CodeRabbit 4503025945: a contest on "Criterio 1" shows the remark stored under C1."""
    from vektra_assess.context.snapshot import ActivityKind, AssignCriterion

    base = make_snapshot()
    snap = base.model_copy(
        update={
            "header": base.header.model_copy(
                update={"activity_kind": ActivityKind.ASSIGN, "grade_method": None}
            ),
            "quiz_items": (),
            "criteria": (
                AssignCriterion(
                    label="C1", level_index=2, level_count=4, points="3",
                    max_points="5", remark="REMARK-ONE",
                ),
            ),
        }
    )  # fmt: skip
    turns = (
        render.TurnRecord(
            1, "bot", "normal", "x", T0, contests=(("Criterio 1", "proposed"),)
        ),
    )
    out = render.render(render.build_doc(_input(snapshot=snap, turns=turns))).decode()
    assert "REMARK-ONE" in out


def test_a_turn_in_another_language_is_not_tagged_italian() -> None:
    """CodeRabbit 4503025958: ``other`` renders as ``lang="und"``."""
    turns = (
        render.TurnRecord(1, "bot", "opening", "Ciao", T0, lang="it"),
        render.TurnRecord(
            2, "student", "normal", "Bonjour, pourquoi?", T0, lang="other"
        ),
    )
    out = render.render(render.build_doc(_input(turns=turns))).decode()
    allowlist.check(out.encode())
    assert '<li class="turn student" lang="und">' in out
    assert '<li class="turn bot" lang="it">' in out
