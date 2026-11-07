"""Render one transcript (REQ-074, BR-016, NFR-016): system header + escaped turns + digest.

``build_doc`` turns the facts the generator gathered (session rows, the decrypted snapshot and
turns, the Moodle facts read at close) into a ``TranscriptDoc`` of plain strings; ``render``
fills the Jinja2 template with ``autoescape=True`` and ``StrictUndefined`` and returns UTF-8
bytes. The header comes ONLY from session and snapshot rows and from Vektra-shipped strings,
never from model output; the snapshot's solution, pasted rubric, prompts and reset reasons are
never read here. Turn text is TEXT (``PlainText`` included): it is escaped by the template, and
only TeX segments become MathML, through the allowlisted node tree of ``mathml``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from vektra_assess.context.regrade import RegradeFlag, RegradeKind
from vektra_assess.context.snapshot import GradeKind, Snapshot
from vektra_assess.parser_protocol import FileStatus
from vektra_assess.transcript.mathml import Segment, segments

TEMPLATES = Path(__file__).parent / "templates"
TEMPLATE = "transcript.html.j2"

CLOSE_REASONS = {
    "finished": "conclusa dallo studente",
    "timed_out": "tempo scaduto",
    "service_failure": "interrotta per un problema tecnico",
    "budget_exhausted": "limite di utilizzo raggiunto",
    "terminated_by_teacher": "terminata dal docente",
    "access_revoked": "accesso revocato",
}
FILE_STATUS = {
    FileStatus.READ: "letto",
    FileStatus.READ_PARTIAL: "letto in parte",
    FileStatus.UNSUPPORTED_FORMAT: "formato non supportato",
    FileStatus.TOO_LARGE: "troppo grande",
    FileStatus.NO_TEXT: "nessun testo",
    FileStatus.PROTECTED: "protetto",
    FileStatus.CORRUPT: "danneggiato",
    FileStatus.OVER_MAX_FILES: "oltre il numero massimo di file",
}
METHODS = {
    "highest": "voto più alto",
    "average": "media",
    "first": "primo tentativo",
    "last": "ultimo tentativo",
}
ROLE_LABELS = {"student": "Studente", "bot": "Assistente", "system": "Sistema"}
LANG_LABELS = {"it": "italiano", "en": "inglese", "other": "altra lingua"}
CONTEST_STATUS = {
    "proposed": "proposto",
    "confirmed": "confermato dallo studente",
    "corrected": "corretto dallo studente",
}
#: post-check stage codes (``TurnResult.marks``) and the marker each one shows
MARK_LABELS = {
    "safeguard": "[risposta sostituita dal sistema]",
    "lexicon": "[frase sostituita dal sistema]",
    "values": "[valore sostituito dal sistema]",
    "feedback_quote": "[citazione sostituita dal sistema]",
    "citations": "[citazione rimossa dal sistema]",
    "marker": "[marcatore rimosso dal sistema]",
}
MARK_OTHER = "[testo modificato dal sistema]"
#: shipped strings (``vektra_assess.llm.shipped``, Italian), handed in by the composition root:
#: this package may not import ``vektra_assess.llm`` (import-linter). A test pins them equal.
REPLACED = "[risposta sostituita dal sistema]"
BR009_TEMPLATE = (
    "Voto registrato: {grade} (letto il {date}). Il voto NON è stato modificato da questa "
    "discussione; decide l'orale"
)
INTERRUPTED = "[risposta interrotta]"
FLAGGED = "segnalato dallo studente"
NOT_AVAILABLE = "non disponibile"


@dataclass(frozen=True)
class Entry:
    label: str
    value: str


@dataclass(frozen=True)
class ListBlock:
    heading: str
    items: tuple[str, ...]
    empty: str


@dataclass(frozen=True)
class TurnView:
    no: int
    role: str
    role_label: str
    at_iso: str
    at_label: str
    lang: str
    segments: tuple[Segment, ...]
    markers: tuple[str, ...]


@dataclass(frozen=True)
class ContestView:
    label: str
    status: str
    where: str
    feedback: str | None


@dataclass(frozen=True)
class TranscriptDoc:
    lang: str
    title: str
    heading: str
    entries: tuple[Entry, ...]
    lists: tuple[ListBlock, ...]
    statement: str
    turns: tuple[TurnView, ...]
    contests: tuple[ContestView, ...]
    footer: str
    turns_heading: str = "Conversazione"
    contests_heading: str = "PUNTI CONTESTATI"
    contests_label: str = "Riepilogo generato dall'assistente"
    contests_empty: str = "Nessun punto contestato registrato."
    tex_label: str = "formula in formato TeX"


@dataclass(frozen=True)
class TurnRecord:
    no: int
    role: str
    kind: str
    text: str
    at: datetime
    flagged: bool = False
    marks: tuple[str, ...] = ()
    contests: tuple[tuple[str, str], ...] = ()  # (label, status)
    lang: str | None = None


@dataclass(frozen=True)
class EarlierSession:
    no: int
    closed_at: datetime | None
    reason: str | None


@dataclass(frozen=True)
class Outage:
    started_at: datetime
    ended_at: datetime | None
    credited_s: int


@dataclass(frozen=True)
class TranscriptInput:
    course: str | None
    cmid: int
    student_userid: int
    session_no: int
    sessions_total: int
    earlier: tuple[EarlierSession, ...]
    previous_context: bool
    snapshot: Snapshot
    regrade: RegradeFlag
    cap_minutes: int | None
    duration_s: int | None
    committed_at: datetime | None
    closed_at: datetime
    close_reason: str | None
    outages: tuple[Outage, ...]
    credited_s: int
    failed_starts: int
    turns: tuple[TurnRecord, ...]
    digest: str
    generated_at: datetime
    tz: str = "Europe/Rome"
    br009_template: str = BR009_TEMPLATE
    extra: dict[str, str] = field(default_factory=dict)


def fmt_number(value: str | None) -> str:
    return (value or "").replace(".", ",")


def _when(at: datetime | None, tz: ZoneInfo) -> str:
    if at is None:
        return NOT_AVAILABLE
    return at.astimezone(tz).strftime("%d/%m/%Y %H:%M")


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def grade_text(snapshot: Snapshot) -> str:
    ov = snapshot.overall
    if ov.grade_kind is GradeKind.SCALE:
        return ov.scale_label or NOT_AVAILABLE
    if ov.grade is None:
        return NOT_AVAILABLE
    shown = fmt_number(ov.grade_display or ov.grade)
    return f"{shown}/{fmt_number(ov.grade_max)}" if ov.grade_max else shown


def grade_at_close(flag: RegradeFlag) -> str:
    """Only a RELEASED grade is ever printed; otherwise the neutral notice (REQ-074)."""
    if flag.kind is RegradeKind.CHANGED:
        return f"modificato dopo l'avvio: {fmt_number(flag.new)} (era {fmt_number(flag.old)})"
    if flag.kind is RegradeKind.NONE:
        return "invariato"
    if flag.kind is RegradeKind.WITHDRAWN:
        return "il voto non è al momento visibile allo studente"
    return NOT_AVAILABLE


def _minutes(seconds: int) -> str:
    return f"{seconds // 60} min" + (f" {seconds % 60} s" if seconds % 60 else "")


def _markers(t: TurnRecord) -> tuple[str, ...]:
    out: list[str] = []
    if t.kind == "interrupted" or INTERRUPTED in t.text:
        out.append(INTERRUPTED)
    if REPLACED in t.text and REPLACED not in out:
        out.append(REPLACED)
    for code in t.marks:
        label = MARK_LABELS.get(code, MARK_OTHER)
        if label not in out:
            out.append(label)
    if t.flagged:
        out.append(FLAGGED)
    return tuple(out)


def _contests(
    turns: Sequence[TurnRecord], snapshot: Snapshot
) -> tuple[ContestView, ...]:
    """Latest entry per label wins (a student's correction replaces the earlier one)."""
    latest: dict[str, tuple[str, int]] = {}
    for t in turns:
        for label, status in t.contests:
            latest.pop(label, None)
            latest[label] = (status, t.no)
    remarks = {c.label: c.remark for c in snapshot.criteria}
    return tuple(
        ContestView(
            label=label,
            status=CONTEST_STATUS.get(status, status),
            where=f"turno {no}",
            feedback=remarks.get(label) or None,
        )
        for label, (status, no) in latest.items()
    )


def build_doc(inp: TranscriptInput) -> TranscriptDoc:
    tz = ZoneInfo(inp.tz)
    snap = inp.snapshot
    h = snap.header
    kind = "Quiz" if h.activity_kind.value == "quiz" else "Compito"
    course = inp.course or NOT_AVAILABLE
    attempt = f"tentativo #{h.attempt_number} di {h.attempt_count}"
    if h.grade_method is not None:
        attempt += (
            f" (metodo: {METHODS.get(h.grade_method.value, h.grade_method.value)})"
        )
    cap_s = (inp.cap_minutes or 0) * 60
    extra_s = max(0, (inp.duration_s or cap_s) - cap_s)
    # in order of first appearance; turns written before languages were recorded count as "it"
    langs = list(dict.fromkeys(t.lang for t in inp.turns if t.lang)) or ["it"]
    entries = [
        Entry("Corso", course),
        Entry("Attività", f"{kind} (id {inp.cmid})"),
        Entry("Studente (id Moodle)", str(inp.student_userid)),
        Entry("Sessione", f"Sessione #{inp.session_no} di {inp.sessions_total}"),
        Entry("Contesto sessioni precedenti", "sì" if inp.previous_context else "no"),
        Entry("Tentativo", attempt),
        Entry("Voto all'avvio", grade_text(snap)),
        Entry("Voto alla chiusura", grade_at_close(inp.regrade)),
        Entry("Durata massima", f"{inp.cap_minutes or 0} min"),
        Entry("Tempo aggiuntivo", _minutes(extra_s) if extra_s else "nessuno"),
        Entry("Avvio", _when(inp.committed_at, tz)),
        Entry("Chiusura", _when(inp.closed_at, tz)),
        Entry(
            "Motivo della chiusura",
            CLOSE_REASONS.get(inp.close_reason or "", NOT_AVAILABLE),
        ),
        Entry("Tempo recuperato per interruzioni", _minutes(inp.credited_s)),
        Entry("Avvii non riusciti", str(inp.failed_starts)),
        Entry(
            "Lingue della conversazione",
            ", ".join(LANG_LABELS.get(lang, lang) for lang in langs),
        ),
    ]
    flags = h.flags
    config = (
        f"show_solution={'true' if flags.show_solution else 'false'}",
        f"rubric_source={flags.rubric_source}",
        f"course_material={'true' if flags.course_material else 'false'}",
    )
    lists = (
        ListBlock(
            "Sessioni precedenti",
            tuple(
                f"Sessione #{e.no}: {_when(e.closed_at, tz)}, "
                f"{CLOSE_REASONS.get(e.reason or '', NOT_AVAILABLE)}"
                for e in inp.earlier
            ),
            "Nessuna sessione precedente.",
        ),
        ListBlock(
            "Interruzioni del servizio",
            tuple(
                f"dalle {_when(o.started_at, tz)} alle {_when(o.ended_at, tz)}, "
                f"recuperati {_minutes(o.credited_s)}"
                for o in inp.outages
            ),
            "Nessuna interruzione.",
        ),
        ListBlock(
            "File consegnati",
            tuple(
                f"{f.label} {f.filename}: {FILE_STATUS.get(f.status, str(f.status))}"
                for f in snap.files
            ),
            "Nessun file.",
        ),
        ListBlock("Configurazione", config, ""),
    )
    statement = inp.br009_template.format(
        grade=grade_text(snap),
        date=datetime.fromtimestamp(snap.overall.grade_read_at, UTC)
        .astimezone(tz)
        .strftime("%d/%m/%Y"),
    )
    turns = tuple(
        TurnView(
            no=t.no,
            role=t.role,
            role_label=ROLE_LABELS.get(t.role, t.role)
            + (" (messaggio iniziale)" if t.kind == "opening" else ""),
            at_iso=_iso(t.at),
            at_label=_when(t.at, tz),
            lang=t.lang if t.lang in ("it", "en") else "it",
            segments=tuple(segments(t.text)),
            markers=_markers(t),
        )
        for t in inp.turns
    )
    title = f"Trascrizione: {course}, attività {inp.cmid}, sessione {inp.session_no}"
    return TranscriptDoc(
        lang="it",
        title=title,
        heading=f"Trascrizione della discussione, sessione #{inp.session_no}",
        entries=tuple(entries),
        lists=lists,
        statement=statement,
        turns=turns,
        contests=_contests(inp.turns, snap),
        footer=(
            f"Impronta SHA-256 dei turni: {inp.digest}. "
            f"Generata il {_when(inp.generated_at, tz)}."
        ),
    )


_ENV: Environment | None = None


def environment() -> Environment:
    global _ENV
    if _ENV is None:
        _ENV = Environment(
            loader=FileSystemLoader(TEMPLATES),
            autoescape=True,
            undefined=StrictUndefined,
            trim_blocks=False,
            lstrip_blocks=False,
        )
    return _ENV


def render(doc: TranscriptDoc) -> bytes:
    return environment().get_template(TEMPLATE).render(doc=doc).encode("utf-8")
