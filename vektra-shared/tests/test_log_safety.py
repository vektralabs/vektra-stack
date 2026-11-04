"""Allowlist log safety for vektra_assess.* loggers (plan assess-context A)."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest
import structlog

from vektra_shared.log_safety import (
    DROPPED,
    AssessAllowlistFilter,
    NamedPrintLoggerFactory,
    assess_allowlist_processor,
    configure_assess_worker_logging,
    install_assess_stdlib_handler,
    is_assess_logger,
)

SECRET = "CNRY-right_answer-multichoice-1a2b3c4d"


@pytest.fixture
def stream() -> Iterator[io.StringIO]:
    buf = io.StringIO()
    configure_assess_worker_logging(buf, level=logging.DEBUG)
    yield buf
    structlog.reset_defaults()
    target = logging.getLogger("vektra_assess")
    for h in list(target.handlers):
        target.removeHandler(h)
    target.propagate = True


def _lines(buf: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


@pytest.mark.parametrize("level", ["debug", "info", "warning"])
def test_structlog_assess_logger_keeps_only_allowlisted_keys(
    stream: io.StringIO, level: str
) -> None:
    log = structlog.get_logger("vektra_assess.context.capture")
    getattr(log, level)(
        "assess_capture_done", session_id="s1", count=3, answer=SECRET, text=SECRET
    )
    (line,) = _lines(stream)
    assert line["event"] == "assess_capture_done"
    assert (line["session_id"], line["count"]) == ("s1", 3)
    assert line["answer"] == DROPPED and line["text"] == DROPPED
    assert SECRET not in stream.getvalue()


def test_structlog_other_loggers_are_untouched(stream: io.StringIO) -> None:
    structlog.get_logger("vektra_core.pipeline").info("query_done", answer="visible")
    (line,) = _lines(stream)
    assert line["answer"] == "visible"


def test_structlog_exception_becomes_its_type(stream: io.StringIO) -> None:
    log = structlog.get_logger("vektra_assess.worker")
    try:
        raise ValueError(SECRET)
    except ValueError:
        log.exception("assess_failed")
    (line,) = _lines(stream)
    assert line["error_type"] == "ValueError" and "exception" not in line
    assert SECRET not in stream.getvalue()


def test_allowlisted_key_cannot_carry_a_long_or_nested_value(
    stream: io.StringIO,
) -> None:
    log = structlog.get_logger("vektra_assess.x")
    log.info("e", status="x" * 500, count={"nested": SECRET})
    (line,) = _lines(stream)
    assert line["status"] == DROPPED and line["count"] == DROPPED


@pytest.mark.parametrize("level", [logging.DEBUG, logging.INFO, logging.WARNING])
def test_stdlib_assess_logger_drops_extras_args_and_tracebacks(
    stream: io.StringIO, level: int
) -> None:
    log = logging.getLogger("vektra_assess.worker")
    try:
        raise RuntimeError(SECRET)
    except RuntimeError:
        log.log(
            level,
            "assess_job_failed %s",
            SECRET,
            exc_info=True,
            extra={"job_id": 7, "kind": "purge", "payload": SECRET},
        )
    (line,) = _lines(stream)
    assert line["event"] == "assess_job_failed %s"  # template kept, argument dropped
    assert (line["job_id"], line["kind"], line["payload"]) == (7, "purge", DROPPED)
    assert line["error_type"] == "RuntimeError"
    assert SECRET not in stream.getvalue()


def test_stdlib_handler_does_not_touch_other_packages(
    caplog: pytest.LogCaptureFixture,
) -> None:
    buf = io.StringIO()
    install_assess_stdlib_handler(buf)
    try:
        with caplog.at_level(logging.INFO, logger="vektra_core"):
            logging.getLogger("vektra_core.x").info(
                "hello %s", "world", extra={"answer": "a"}
            )
        assert caplog.records[0].getMessage() == "hello world"
        assert caplog.records[0].answer == "a"  # type: ignore[attr-defined]
        assert buf.getvalue() == ""  # not routed through the assess handler
    finally:
        target = logging.getLogger("vektra_assess")
        for h in list(target.handlers):
            target.removeHandler(h)
        target.propagate = True


def test_stdlib_filter_lets_non_assess_records_through_unchanged() -> None:
    rec = logging.makeLogRecord(
        {"name": "uvicorn.error", "msg": "x %s", "args": ("y",), "answer": "z"}
    )
    assert AssessAllowlistFilter().filter(rec)
    assert rec.getMessage() == "x y" and rec.answer == "z"  # type: ignore[attr-defined]


def test_install_is_idempotent() -> None:
    try:
        install_assess_stdlib_handler(io.StringIO())
        install_assess_stdlib_handler(io.StringIO())
        handlers = logging.getLogger("vektra_assess").handlers
        assert len(handlers) == 1
    finally:
        target = logging.getLogger("vektra_assess")
        for h in list(target.handlers):
            target.removeHandler(h)
        target.propagate = True


def test_named_factory_keeps_the_logger_name() -> None:
    logger = NamedPrintLoggerFactory(io.StringIO())("vektra_assess.moodle.client")
    assert logger.name == "vektra_assess.moodle.client"
    assert NamedPrintLoggerFactory()().name == ""


def test_prefix_match_is_exact() -> None:
    assert is_assess_logger("vektra_assess") and is_assess_logger(
        "vektra_assess.worker"
    )
    assert not is_assess_logger("vektra_assessment") and not is_assess_logger(None)


def test_processor_reads_the_logger_key_too() -> None:
    out = assess_allowlist_processor(
        None, "info", {"event": "e", "logger": "vektra_assess.a", "text": SECRET}
    )
    assert out["text"] == DROPPED
