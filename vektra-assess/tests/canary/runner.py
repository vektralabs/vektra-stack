"""Run one canary case through capture, assembly, Part A and the REAL discussion runner.

Returns every sink's rendered content so the test can count canaries per sink:
1 snapshot plaintext (encrypted and decrypted with a session DEK), 2 the assembled bundle,
3 Part A, 4 the exact request bytes at a ``RecordingLLMProvider``, 5 logs (from the caller's
``canary_log_capture``), 6 the audit events (in memory; a DB subset is in the integration suite),
7 the QueryTrace the runner wrote (stored JSON), 8 the calls a ``RecordingVectorStoreProvider(
forbid_calls=True)`` received, 9 the text the runner released to the student.

Since plan 7 the turn goes through ``DiscussionTurnRunner`` (a student turn and a Part B), with a
provider that ECHOES the whole context back as its reply: the worst case of a model trying to
repeat what it was given. The runner's own audit rows join the capture's in the audit sink.
"""

from __future__ import annotations

import dataclasses
import os
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from assess_moodle_support import FIXTURES, Replay, manifest, site_readers, uid
from test_context_capture import InProcessParser

from canary.matrix import ASSIGN_FIELDS, REVIEW_VARIANTS, Case, canary, finalize_quiz
from vektra_assess.context.assemble import assemble_context
from vektra_assess.context.capture.capture import (
    AssignTarget,
    Built,
    Capabilities,
    CaptureNotEligible,
    QuizTarget,
    TeacherConfigView,
    _files,
    capture_assign,
    capture_quiz,
)
from vektra_assess.context.part_a import SessionFacts, render_part_a
from vektra_assess.context.snapshot import canonical_bytes
from vektra_assess.crypto import envelope
from vektra_shared.lms import Readable
from vektra_shared.types import Message
from vektra_testkit.recorders import (
    RecordingLLMProvider,
    RecordingVectorStoreProvider,
)
from vektra_testkit.recorders import (
    canonical_bytes as canon,
)

SINKS = (
    "snapshot",
    "bundle",
    "part_a",
    "request_bytes",
    "logs",
    "audit",
    "query_trace",
    "vector_store",
    "released",
)


class FakeTeacherStore:
    """Plan 11's write-only teacher config, as capture sees it: the solution is decrypted ONLY
    when the toggle is ON, and every decryption is counted."""

    def __init__(self, solution: str, pasted: str) -> None:
        self._solution = solution
        self._pasted = pasted
        self.solution_fetches = 0

    def view(self, show_solution: bool) -> TeacherConfigView:
        solution = None
        if show_solution:
            self.solution_fetches += 1
            solution = self._solution
        return TeacherConfigView(
            show_solution, "pasted", solution_text=solution, pasted_rubric=self._pasted
        )


@dataclass
class Outcome:
    built: Built | None
    not_eligible: CaptureNotEligible | None
    sinks: dict[str, bytes] = field(default_factory=dict)
    vector_calls: int = 0
    solution_fetches: int = 0


#: positive controls for the released sink: safe sentences the runner must let through, so an
#: unwired or always-empty sink cannot pass the forbidden-canary search vacuously
RELEASED_TURN_CANARY = "VISIBLE-RELEASED-TURN"
RELEASED_PART_B_CANARY = "VISIBLE-RELEASED-PARTB"
_RELEASABLE_TURN = f"Ripercorriamo insieme il ragionamento, {RELEASED_TURN_CANARY}."
_RELEASABLE_PART_B = f"Possiamo partire dalla prima domanda, {RELEASED_PART_B_CANARY}."


class EchoLLM(RecordingLLMProvider):
    """Records the request like the recording provider, then replies with every message it got
    followed by ``tail``; ``next_reply``, when set, replaces the echo for the next call only."""

    def __init__(self, tail: str = "") -> None:
        super().__init__([])
        self.tail = tail
        self.next_reply: str | None = None

    async def stream(  # type: ignore[override]
        self,
        messages: list[Message],
        model: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> Any:
        if self.next_reply is not None:
            reply, self.next_reply = self.next_reply, None
        else:
            reply = "\n".join(m.content for m in messages) + self.tail
        self._responses = [reply]
        self._served = 0
        return await super().stream(messages, model, temperature, max_tokens, **kwargs)


async def real_turn(
    bundle: Any, store: RecordingVectorStoreProvider
) -> dict[str, bytes]:
    """A student turn and a Part B through the real runner; returns sinks 4, 6 (runner), 7, 9."""
    from fakes import RecordingAuditSink, RecordingSafeguard, RecordingTraceWriter

    from vektra_assess.config import VektraAssessSettings
    from vektra_assess.llm import (
        DiscussionTurnRunner,
        PartBRejectedError,
        ReleasedSegment,
        StudentTurn,
    )

    del store  # RAG is OFF in v1: the runner has no store at all (NFR-015)
    llm = EchoLLM(tail="\n\n" + _RELEASABLE_TURN)
    writer, audit = RecordingTraceWriter(), RecordingAuditSink()
    runner = DiscussionTurnRunner(
        llm=llm,
        safeguard=RecordingSafeguard(),
        trace_writer=writer,
        settings=VektraAssessSettings.model_validate(
            {"VEKTRA_ASSESS_LLM_MODEL": "recording-model"}
        ),
        audit=audit,
    )
    released: list[str] = []
    async for item in runner.run_stream(bundle, StudentTurn("Spiegami la prova.", 1)):
        if isinstance(item, ReleasedSegment):
            released.append(item.text)
    try:
        part_b = await runner.run_part_b(bundle, remaining_s=0)
        released.append(part_b.text)
    except PartBRejectedError:
        pass  # an echoed context is not a valid Part B; its request and trace still count
    # a clean Part B, so the released sink is shown to collect Part B output too
    llm.next_reply = _RELEASABLE_PART_B
    part_b = await runner.run_part_b(bundle, remaining_s=0)
    released.append(part_b.text)
    return {
        "request_bytes": b"".join(llm.all_request_bytes),
        "audit_runner": canon([[e, dict(d)] for e, d in audit.rows]),
        "query_trace": canon([dataclasses.asdict(t) for t, _ in writer.rows]),
        "released": "\n".join(released).encode(),
    }


def _manifest_canaries(m: dict[str, Any], *needles: str) -> list[str]:
    return [c for c in m["canaries"] if all(n in c for n in needles)]


async def run_case(case: Case) -> Outcome:
    m = manifest(case.version)
    solution = canary(
        "solution_text", case.kind if case.kind == "quiz" else case.shape or "assign"
    )
    pasted = canary("pasted_rubric", case.kind)
    store = FakeTeacherStore(solution, pasted)
    teacher = store.view(case.toggle)
    if case.toggle:
        case.required[solution] = ("snapshot", "bundle", "request_bytes")
    else:
        case.forbidden[solution] = (
            "solution_text",
            "quiz" if case.kind == "quiz" else case.shape or "",
        )
    case.required[pasted] = ("snapshot", "bundle", "request_bytes")
    for cnry in (RELEASED_TURN_CANARY, RELEASED_PART_B_CANARY):
        case.required[cnry] = ("released",)
    caps = Capabilities(
        review_html_filterable="readable"
        if case.html_path == "filterable"
        else "unreadable",
        rubric_display_settings="readable"
        if case.shape == "rubric_visible"
        else "unreadable",
    )
    replay = Replay(case.version)
    replay.mutate = case.mutate
    # the vertical's capture over the Moodle provider's recorded reviews: Vektra's canary
    # sinks over a provider's recordings (the joint BR-011 guarantee, ADR-0033 D4)
    readers = site_readers(replay)
    parser = InProcessParser()
    now = 1_800_000_000
    if case.kind == "quiz":
        q = m["quizzes"]["types"]
        result = await capture_quiz(
            QuizTarget(m["courseid"], q["id"], q["cmid"], uid(case.version, "stu1")),
            readers, parser, teacher, caps, moodle_release=m["release"], now=now,
        )  # fmt: skip
        finalize_quiz(case)
        case.required.update({c: ("bundle",) for c in _visible_qtext()})
    else:
        result = await _assign_case(
            case, m, replay, readers, parser, teacher, caps, now
        )
    assert not replay.unmatched, replay.unmatched
    _seeded_canaries(case, m)
    out = Outcome(
        built=result if isinstance(result, Built) else None,
        not_eligible=result if isinstance(result, CaptureNotEligible) else None,
        solution_fetches=store.solution_fetches,
    )
    if out.built is None:
        return out
    snap = out.built.snapshot
    dek, sid = os.urandom(32), uuid.uuid4()
    blob = envelope.encrypt_snapshot(dek, sid, canonical_bytes(snap))
    out.sinks["snapshot"] = envelope.decrypt_snapshot(dek, sid, blob)
    bundle = assemble_context(snap, out.built.frozen_policy)
    out.sinks["bundle"] = bundle.model_dump_json().encode()
    out.sinks["part_a"] = render_part_a(bundle, SessionFacts(1, 30)).text.encode()
    vs = RecordingVectorStoreProvider(forbid_calls=True)
    turn = await real_turn(bundle, vs)
    out.sinks["request_bytes"] = turn["request_bytes"]
    out.sinks["audit"] = (
        canon([dataclasses.asdict(e) for e in out.built.audit]) + turn["audit_runner"]
    )
    out.sinks["query_trace"] = turn["query_trace"]
    out.sinks["released"] = turn["released"]
    out.vector_calls = len(vs.calls)
    return out


def _visible_qtext() -> list[str]:
    """Positive control: the question text of the extraction-enabled types reaches the bundle."""
    return [f"VISIBLE-QTEXT-{s}" for s in ("tf", "mc", "sa", "num")]


_SHAPE_ASSIGN = {
    "rubric_visible": "rubric_vis", "rubric_hidden": "rubric_hidden", "rubric_settings_unreadable": "rubric_vis",
    "simple": "simple", "scale": "scale", "files": "simple", "unreleased": "hiddengrade",
}  # fmt: skip


async def _assign_case(
    case: Case,
    m: dict[str, Any],
    replay: Replay,
    readers: Any,
    parser: Any,
    teacher: Any,
    caps: Any,
    now: int,
) -> Any:
    name = _SHAPE_ASSIGN[case.shape or ""]
    a = m["assigns"][name]
    tag = "m" + case.version.replace(".", "")
    if case.shape in ("rubric_visible", "rubric_hidden", "rubric_settings_unreadable"):
        for c in _manifest_canaries(m, "RUBRIC", name):
            case.forbidden[c] = ("rubric_descriptors", case.shape)
    if case.shape == "unreleased":
        for c in _manifest_canaries(m, "UNRELEASEDFB", name):
            case.forbidden[c] = ("unreleased_feedback", case.shape)
    result = await capture_assign(
        AssignTarget(m["courseid"], a["id"], a["cmid"], uid(case.version, "stu1")),
        readers, parser, teacher, caps, moodle_release=m["release"], now=now,
    )  # fmt: skip
    if case.shape == "files" and isinstance(result, Built):
        files = await _intake(case.version, replay, m)
        result = dataclasses.replace(
            result, snapshot=result.snapshot.model_copy(update={"files": tuple(files)})
        )
        case.forbidden[f"CANARY-PPTX-{tag}"] = ("unread_file_text", "files")
        case.forbidden[f"VISIBLE-FILE-multi_11.txt-{tag}"] = (
            "unread_file_text",
            "files",
        )
        case.required[f"CANARY-PDFTEXT-{tag}"] = ("snapshot", "bundle", "request_bytes")
    assert case.shape in ASSIGN_FIELDS
    return result


async def _intake(version: str, replay: Replay, m: dict[str, Any]) -> list[Any]:
    """Files of stu1 (one per format) and stu2 (11 text files), relabelled F1..Fn, through the
    real intake: 10-file cap, ownership re-check, sidecar statuses."""
    readers = site_readers(replay)
    a = m["assigns"]["files"]
    out = []
    for user in ("stu1", "stu2"):
        status = await readers.submission(a["id"], uid(version, user))
        assert isinstance(status, Readable)
        for f in status.value.files:
            replay.files[urlsplit(f.locator).path] = (
                FIXTURES / version / "files" / f"{user}__{f.filename}"
            ).read_bytes()
        target = AssignTarget(m["courseid"], a["id"], a["cmid"], uid(version, user))
        out += await _files(
            target,
            status.value,
            readers,
            InProcessParser(),
            _limits(),
            [],
        )
    return [f.model_copy(update={"label": f"F{n}"}) for n, f in enumerate(out, start=1)]


def _limits() -> Any:
    from vektra_assess.context.capture.capture import CaptureLimits

    return CaptureLimits()


#: seeded canaries that a case may legitimately carry: the text of files that are READ
_READ_FILE_CANARIES = ("CANARY-PDFTEXT", "CANARY-DOCX", "CANARY-ODT")


def _seeded_canaries(case: Case, m: dict[str, Any]) -> None:
    """Every canary the seeder planted is hidden material for EVERY case (other students, other
    activities, guide markers, unreleased feedback), except what the case's policy permits."""
    gfb_visible = case.kind == "quiz" and bool(
        REVIEW_VARIANTS[case.review or "all_hidden"][1]
    )
    for c in m["canaries"]:
        if c in case.required or c in case.forbidden:
            continue
        if case.shape == "files" and c.startswith(_READ_FILE_CANARIES):
            continue
        if gfb_visible and c.startswith("CANARY-GENFB"):
            case.allowed.add(
                c
            )  # permitted by the review options (and not extracted anyway)
            continue
        case.forbidden[c] = ("seeded", case.shape or case.kind)
