"""Allowlist log safety for the assess packages (plan assess-context A, BR-011 / NFR-017).

The older ``_pii_redactor`` in ``vektra_app.main`` is WARNING+ only and key-name based: it removes
a few known keys and lets everything else through. That is not a control for the assessment bot,
where a student's answer, a hidden right answer or a teacher's solution must never reach a log at
any level. For loggers under a **registered strict prefix** this module inverts the rule: **only
allowlisted keys survive**, at every level, and every other key is replaced with ``[dropped]``.
Exception text and tracebacks are replaced by the exception type, because an exception message can
carry the content that raised it. Other packages keep their current behaviour.

A logger namespace opts in with ``register_strict_logger_prefix(prefix)``, called by the
composition root that configures logging: the API and the worker register ``vektra_assess``,
and each LMS provider's prefix is registered the same way (ADR-0033 D8). Not from a package
``__init__``: the parser sidecar's children inherit everything their preload imported. Once a
provider's loggers leave the ``vektra_assess.*`` namespace, the registration is what keeps them
strict; nothing is hard-coded here.

Two logging stacks are covered, because the assess code uses both:

- structlog: ``assess_allowlist_processor``, placed ahead of ``_pii_redactor``. structlog's
  ``PrintLogger`` has no name, so ``NamedPrintLoggerFactory`` keeps the name passed to
  ``structlog.get_logger(__name__)``. A bare ``get_logger()`` in ``vektra_assess`` would carry no
  name and escape the filter; a test forbids it.
- stdlib ``logging``: ``AssessAllowlistFilter`` on the HANDLER (a filter on a logger does not see
  its children's records). It scrubs ``extra=`` attributes, message arguments and exception info.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
from collections.abc import Iterable, MutableMapping
from typing import Any, TextIO

import structlog

DROPPED = "[dropped]"

# The plan's allowlist (content-free identifiers, codes and measures) ...
PLAN_KEYS = frozenset(
    {
        "event",
        "session_id",
        "site_id",
        "job_id",
        "kind",
        "wsfunction",
        "status",
        "reason_code",
        "duration_ms",
        "size_bytes",
        "count",
        "hash",
        "flag",
    }
)
# ... plus the operational fields the worker already logs: names, counts, enums, never content.
OPERATIONAL_KEYS = frozenset(
    {
        "error_type",
        "pool",
        "tick",
        "step",
        "role",
        "worker_id",
        "attempt",
        "state",
        "deadline_s",
        "released",
        "cancelled",
        "requeued",
        "marked_dead",
        "audit_failed",
        "deleted",
        "policy_version",
        "schema_version",
        "file_status",
        "alert",
        "bytes",
        "error_code",
        "errorcode",
        "exit_code",
        "item",
        "ok",
        "not_ok",
        "outcome",
        "reader",
        "retention_h",
        "round_trip_ms",
        "signal",
    }
)
ALLOWED_KEYS = PLAN_KEYS | OPERATIONAL_KEYS
# structlog's own bookkeeping keys, added by the processors that run before this one
# (add_log_level sets "level", TimeStamper "timestamp"). Not "logger" nor "log_level": no
# processor in the chain adds them, so a value there came from the caller
_STRUCTLOG_KEYS = frozenset({"level", "timestamp"})
_MAX_VALUE_LEN = 200  # an allowlisted key still cannot carry a paragraph


#: keys replaced with ``[dropped]`` since the process started, both logging stacks together
#: (plan assess-privacy-retention 12b: exposed on the API's /metrics). A plain int: the GIL
#: makes the increment atomic enough for a monitoring figure.
_dropped_keys = 0


def dropped_keys_total() -> int:
    return _dropped_keys


def _count_dropped(n: int) -> None:
    global _dropped_keys
    _dropped_keys += n


# --- the strict prefixes ----------------------------------------------------------------------

_PREFIX = re.compile(r"[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)*")
#: logger-name prefixes held to the allowlist. Registration only adds: a package cannot opt out.
#: An immutable tuple, replaced whole under ``_PREFIXES_LOCK``: the filter iterates it on every
#: record, from any thread, without a lock, and always sees a complete snapshot
_STRICT_PREFIXES: tuple[str, ...] = ()
_PREFIXES_LOCK = threading.Lock()
#: the level ``install_assess_stdlib_handler`` ran with; None until it ran. A prefix registered
#: later gets its scrub handler at registration, so the order of imports and startup is irrelevant
_installed_level: int | None = None


def register_strict_logger_prefix(prefix: str) -> None:
    """Hold every logger named ``prefix`` or ``prefix.*`` to the allowlist, on both stacks.

    Idempotent and thread-safe. Call it from the composition root that configures logging
    (the API and the worker register ``vektra_assess``), before the package logs.
    """
    global _STRICT_PREFIXES
    if not isinstance(prefix, str) or not _PREFIX.fullmatch(prefix):
        raise ValueError(
            "a strict logger prefix is a dotted lower-case module path, e.g. 'vektra_assess'"
        )
    with _PREFIXES_LOCK:
        if prefix not in _STRICT_PREFIXES:
            _STRICT_PREFIXES = (*_STRICT_PREFIXES, prefix)
        level = _installed_level
    if level is not None:
        _install_scrub_handler(prefix, level)


def strict_logger_prefixes() -> tuple[str, ...]:
    return tuple(sorted(_STRICT_PREFIXES))


def is_strict_logger(name: str | None) -> bool:
    if not name:
        return False
    return any(name == p or name.startswith(p + ".") for p in _STRICT_PREFIXES)


#: the name every caller already uses: an "assess" logger is any strict one
is_assess_logger = is_strict_logger


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str) and len(value) <= _MAX_VALUE_LEN:
        return value
    return DROPPED


def _exc_type(exc_info: Any) -> str | None:
    if exc_info is None or exc_info is False:
        return None
    if exc_info is True:
        exc = sys.exc_info()[1]
    elif isinstance(exc_info, BaseException):
        exc = exc_info
    elif isinstance(exc_info, tuple) and len(exc_info) == 3:
        exc = exc_info[1]
    else:
        return None
    return type(exc).__name__ if exc is not None else None


def scrub_event_dict(event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """Apply the allowlist to one structlog event dict, in place."""
    error_type = _exc_type(event_dict.pop("exc_info", None))
    for key in ("exception", "stack", "stack_info"):
        event_dict.pop(key, None)
    for key in list(event_dict):
        if key in _STRUCTLOG_KEYS:
            continue
        if key in ALLOWED_KEYS:
            event_dict[key] = _safe_value(event_dict[key])
        else:
            if event_dict[key] is not DROPPED:  # a record filtered twice counts once
                _count_dropped(1)
            event_dict[key] = DROPPED
    if error_type and "error_type" not in event_dict:
        event_dict["error_type"] = error_type
    return event_dict


def assess_allowlist_processor(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: allowlist for loggers under a strict prefix, untouched otherwise."""
    # either name makes it an assess event: a caller cannot opt out by passing logger="other"
    names = (getattr(logger, "name", None), event_dict.get("logger"))
    if any(is_assess_logger(n if isinstance(n, str) else None) for n in names):
        return scrub_event_dict(event_dict)
    return event_dict


class NamedPrintLogger(structlog.PrintLogger):
    def __init__(self, file: TextIO | None = None, name: str = "") -> None:
        super().__init__(file)
        self.name = name


class NamedPrintLoggerFactory:
    """``PrintLoggerFactory`` that remembers the name given to ``structlog.get_logger(name)``."""

    def __init__(self, file: TextIO | None = None) -> None:
        self._file = file

    def __call__(self, *args: Any) -> NamedPrintLogger:
        name = str(args[0]) if args and isinstance(args[0], str) else ""
        return NamedPrintLogger(self._file, name)


# --- stdlib ------------------------------------------------------------------------------------

# attributes every LogRecord has; anything else on a record came from ``extra=``
_RECORD_ATTRS = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class AssessAllowlistFilter(logging.Filter):
    """Handler filter: the same allowlist for stdlib records of ``vektra_assess.*`` loggers.

    Install it on the handler, not on a logger: a logger's own filters do not see records that
    propagate from its children.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not is_assess_logger(record.name):
            return True
        error_type = _exc_type(record.exc_info)
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        if record.args:
            # the formatted message could interpolate content; keep the constant template only
            record.args = ()
        # the message is an event name. A non-string or long message is dropped here; a short
        # f-string cannot be told from an event name at runtime, so the call sites are held to
        # literal messages by a static guard (vektra-assess tests/test_log_safety_wiring.py)
        if not isinstance(record.msg, str) or len(record.msg) > _MAX_VALUE_LEN:
            record.msg = DROPPED
        for key in list(record.__dict__):
            if key in _RECORD_ATTRS:
                continue
            if key in ALLOWED_KEYS:
                record.__dict__[key] = _safe_value(record.__dict__[key])
            else:
                if record.__dict__[key] is not DROPPED:  # two handlers, one record
                    _count_dropped(1)
                record.__dict__[key] = DROPPED
        if error_type and "error_type" not in record.__dict__:
            record.__dict__["error_type"] = error_type
        return True


class JsonExtrasFormatter(logging.Formatter):
    """One JSON object per record: level, logger, event (the message) and the ``extra=`` keys."""

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RECORD_ATTRS:
                out[key] = value
        if record.exc_info:
            out["exception"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str, ensure_ascii=False)


def install_stdlib_filter(handlers: Iterable[logging.Handler]) -> None:
    for handler in handlers:
        if not any(isinstance(f, AssessAllowlistFilter) for f in handler.filters):
            handler.addFilter(AssessAllowlistFilter())


class _ScrubHandler(logging.Handler):
    """Writes nothing: its filter scrubs the record in place, and the record then propagates to
    the root handlers (the worker's stream, uvicorn's, pytest's caplog) already scrubbed."""

    def emit(
        self, record: logging.LogRecord
    ) -> None:  # pragma: no cover - never writes
        return None


_INSTALL_LOCK = (
    threading.Lock()
)  # remove-then-add of one prefix's handler is not atomic


def _install_scrub_handler(prefix: str, level: int) -> logging.Handler:
    with _INSTALL_LOCK:
        target = logging.getLogger(prefix)
        for old in [h for h in target.handlers if getattr(h, "_vektra_assess", False)]:
            target.removeHandler(old)
        handler = _ScrubHandler(logging.NOTSET)
        install_stdlib_filter([handler])
        handler._vektra_assess = True  # type: ignore[attr-defined]
        target.addHandler(handler)
        if target.level == logging.NOTSET or target.level > level:
            target.setLevel(level)
        return handler


def install_assess_stdlib_handler(level: int = logging.INFO) -> list[logging.Handler]:
    """Scrub every stdlib record of a strict logger before the root handlers see it.

    One scrub handler per registered prefix, on the prefix's own logger: it runs before the
    root's (the logger hierarchy is walked upwards), and its filter mutates the record. Handlers
    already attached to the prefix logger, and handlers on a child logger such as
    ``vektra_assess.worker``, run BEFORE the scrub and see unscrubbed records: do not attach
    handlers there (none does today). ``propagate`` stays True, so the records keep reaching the
    root handlers and flushing the root still flushes them. A prefix registered after this call
    gets its handler at registration. Idempotent.
    """
    global _installed_level
    with (
        _PREFIXES_LOCK
    ):  # a registration racing this call installs its own handler either way
        _installed_level = level
        prefixes = _STRICT_PREFIXES
    return [_install_scrub_handler(p, level) for p in sorted(prefixes)]


def structlog_processors(
    *, extra: Iterable[Any] = (), render: bool = True
) -> list[Any]:
    """The processor chain shared by the API and the assess worker: the allowlist comes first,
    then any ``extra`` processors (the API's ``_pii_redactor``), then rendering."""
    chain: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        assess_allowlist_processor,
        *extra,
    ]
    if render:
        chain += [
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ]
    return chain


def configure_assess_worker_logging(
    stream: TextIO | None = None, level: int = logging.INFO
) -> logging.Handler:
    """Logging for the assess worker process: JSON on stderr, allowlist on both stacks.

    The JSON stream handler sits on the ROOT logger (the watchdog flushes the root handlers before
    a hard exit); it carries the same filter, so a record from any logger is scrubbed if it is an
    assess one. Returns that handler.
    """
    out = stream if stream is not None else sys.stderr
    structlog.configure(
        processors=structlog_processors(),
        context_class=dict,
        logger_factory=NamedPrintLoggerFactory(out),
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=False,
    )
    install_assess_stdlib_handler(level)
    root = logging.getLogger()
    for old in [h for h in root.handlers if getattr(h, "_vektra_assess", False)]:
        root.removeHandler(old)
    handler = logging.StreamHandler(out)
    handler.setFormatter(JsonExtrasFormatter())
    install_stdlib_filter([handler])
    handler._vektra_assess = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)
    return handler
