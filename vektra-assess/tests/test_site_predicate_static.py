"""The structural backstop of ADR-0033 D9: every API statement that reads a site-scoped table
names the site.

No ``assess_*`` table has a row-level-security policy (D9 records the deviation from ADR-0009;
RLS for the API role is phase 3). Until then, isolation between sites rests on the column
grants, the SECURITY DEFINER functions and a ``site_id`` predicate in every route. This test
parses every SQL text in ``vektra_assess.api`` (string literals, implicitly concatenated, and
f-strings with their expressions blanked) and fails when a statement reads a site-scoped table
(a mapped table with a ``site_id`` column, read from ``db/models.py``) without a ``site_id``
predicate. A statement that is safe for another reason is in ``ALLOWED``, with its reason.

It is a static check of the text, not a proof: it cannot see a predicate on the wrong value.
That is what the IDOR matrix (``test_idor.py``) and, in phase 3, the RLS test prove.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "src" / "vektra_assess"
API = PKG / "api"

_SQL = re.compile(r"\b(select|update|delete)\b", re.I)
#: a table the statement reads rows of: FROM / JOIN / UPDATE (DELETE FROM reads too)
_READ = re.compile(r"\b(?:from|join|update)\s+(assess_\w+)", re.I)
#: the site predicate: a comparison of a ``site_id`` column (qualified or not) with a value
_PREDICATE = re.compile(r"\bsite_id\s*(?:=|\bin\b|<>|is not distinct from)", re.I)
#: the SECURITY DEFINER functions are the API's only route to session material (D9 layer 2):
#: they re-check the session's site themselves
_DEFINER = re.compile(r"\bassess_\w+\s*\(", re.I)

#: statements that read a site-scoped table without a ``site_id`` predicate, each safe for a
#: reason the text alone does not show. A statement is identified by its file and the first
#: 60 characters of its normalised text. Keep the list short and every reason checkable.
#:
#: Measured on the first run (2026-11-11): 14 statements. None reads another site's rows; they
#: are of three kinds, and phase 3's RLS (D9.2) is what makes each one safe in the database too.
#: - the site resolution itself (the API key -> site lookup);
#: - a lookup by an unguessable row identity the route obtained from a token it already verified
#:   for this site (session id, transcript id, download-token hash, jti, erasure id), whose
#:   site the code then compares again (``session_gate.py:183``, ``transcripts.py:218,330,428``);
#: - one cross-site aggregate (a count), which returns no row.
_SESSION_OF_TOKEN = (
    "keyed by the session id of a session token already verified for this site "
    "(session_gate.load_owned checks the row's site_id)"
)
ALLOWED: dict[tuple[str, str], str] = {
    ("common.py", "SELECT s.id, s.min_plugin_version FROM assess_site_keys k JO"): (
        "the site resolution itself: the API key's own binding is what names the site"
    ),
    (
        "session_gate.py",
        "SELECT id, site_id, cmid, student_userid, session_no, epoch,",
    ): (
        "reads the row by session id and returns site_id: the caller compares (site, "
        "student, cmid) with the token's claims before using it (session_gate.py:183)"
    ),
    ("events.py", "SELECT state, close_reason, epoch, deadline_at, duration_s, "): (
        _SESSION_OF_TOKEN
    ),
    ("state.py", "SELECT s.session_no, s.state, s.deadline_at, s.duration_s, n"): (
        _SESSION_OF_TOKEN
    ),
    ("turns.py", "SELECT seq, pending_reply_since FROM assess_sessions WHERE i"): (
        _SESSION_OF_TOKEN
    ),
    ("turns.py", "UPDATE assess_sessions SET tokens_used = tokens_used + :n WH"): (
        _SESSION_OF_TOKEN
    ),
    ("turns.py", "SELECT seq FROM assess_sessions WHERE id = :s AND state = 'A"): (
        _SESSION_OF_TOKEN
    ),
    ("teacher_ops.py", "SELECT 1 FROM assess_purge_records WHERE session_id = :sid"): (
        "an existence check by the id of a session the route already read with the site "
        "predicate; returns no column"
    ),
    ("state.py", "SELECT courseid_hint FROM assess_token_jti WHERE jti = :j"): (
        "keyed by the jti of the token the route just verified (unique, unguessable)"
    ),
    (
        "transcripts.py",
        "SELECT t.id, t.ciphertext, t.dek_wrapped, t.content_hash, t.",
    ): (
        "by transcript id; returns s.site_id, which the route compares with the key's or "
        "the token's site before anything is used (transcripts.py:218, 330, 428)"
    ),
    (
        "transcripts.py",
        "SELECT subject_userid, audience_role, site_id FROM assess_tr",
    ): (
        "keyed by the hash of a single-use download token (unguessable); returns site_id, "
        "compared with the claims (transcripts.py:326)"
    ),
    (
        "transcripts.py",
        "UPDATE assess_transcript_download_tokens SET consumed_at = n",
    ): ("consumes the download token just validated above, by its hash"),
    ("privacy.py", "SELECT completed_at, outcome, rows_deleted FROM assess_erasu"): (
        "the erasure record this same request queued or found for its own site "
        "(_pending_or_queue); returns counts and an outcome, no content"
    ),
    ("start.py", "SELECT count(*) FROM assess_sessions WHERE state <> 'CLOSED'"): (
        "the deployment-wide live-session cap: an aggregate over every site by design, "
        "returns one number and no row"
    ),
}


def site_scoped_tables(models: Path = PKG / "db" / "models.py") -> frozenset[str]:
    """The mapped tables that have a ``site_id`` column (``assess_sites`` is the site itself)."""
    out = set()
    for node in ast.walk(ast.parse(models.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.ClassDef):
            continue
        name, has_site = None, False
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__tablename__"
                for t in stmt.targets
            ):
                if isinstance(stmt.value, ast.Constant):
                    name = str(stmt.value.value)
            if (
                isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and stmt.target.id == "site_id"
            ):
                has_site = True
        if name and has_site:
            out.add(name)
    return frozenset(out)


def _text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) else "{}" for v in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _text(node.left), _text(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def sql_texts(root: Path = API) -> list[tuple[str, str]]:
    """(file, normalised SQL text) for every string literal that looks like a statement."""
    out = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        inner: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(
                node, ast.BinOp
            ):  # a concatenation: its parts are not statements
                inner |= {id(node.left), id(node.right)}
            if isinstance(node, ast.JoinedStr):  # nor are an f-string's literal pieces
                inner |= {id(v) for v in node.values}
        for node in ast.walk(tree):
            if id(node) in inner:
                continue
            text = _text(node)
            if text and _SQL.search(text) and "assess_" in text:
                out.append((rel, " ".join(text.split())))
    return out


def unscoped(
    statements: list[tuple[str, str]], scoped: frozenset[str]
) -> list[tuple[str, str]]:
    """The statements that read a site-scoped table with no site predicate and no definer
    call, minus the allowlisted ones."""
    bad = []
    for rel, sql in statements:
        tables = {t.lower() for t in _READ.findall(sql)} & scoped
        if not tables or _PREDICATE.search(sql) or _DEFINER.search(sql):
            continue
        if (rel, sql[:60]) in ALLOWED:
            continue
        bad.append((rel, sql))
    return bad


def test_the_scan_sees_the_api_and_the_scoped_tables() -> None:
    """False-pass guard: an empty table set or an empty statement list proves nothing."""
    scoped = site_scoped_tables()
    assert {"assess_sessions", "assess_activity_config", "assess_site_keys"} <= scoped
    statements = sql_texts()
    assert len(statements) > 30
    assert any(_READ.search(s) for _f, s in statements)


def test_every_api_read_of_a_site_scoped_table_names_the_site() -> None:
    bad = unscoped(sql_texts(), site_scoped_tables())
    assert bad == [], "\n".join(f"{f}: {s}" for f, s in bad)


def test_every_allowlisted_statement_still_exists() -> None:
    """A stale exception is a hole waiting for a new statement with the same prefix."""
    present = {(f, s[:60]) for f, s in sql_texts()}
    assert set(ALLOWED) <= present, sorted(set(ALLOWED) - present)


def test_a_route_without_the_predicate_turns_it_red(tmp_path: Path) -> None:
    """Positive control: a planted route that reads sessions by cmid alone is reported; the
    same statement with the predicate, and a definer call, are not."""
    (tmp_path / "planted.py").write_text(
        "from sqlalchemy import text\n"
        "LEAK = text(\n"
        '    "SELECT s.id, s.state FROM assess_sessions s "\n'
        '    "WHERE s.cmid = :c AND s.student_userid = :u"\n'
        ")\n"
        'OK = text("SELECT id FROM assess_sessions WHERE site_id = :s AND cmid = :c")\n'
        'DEF = text("SELECT * FROM assess_get_session_material(:sid, :e)")\n'
        'JOINED = f"SELECT 1 FROM assess_turns t JOIN assess_sessions s ON s.id = t.session_id '
        'WHERE s.site_id = {1}"\n'
    )
    statements = sql_texts(tmp_path)
    assert len(statements) == 4
    bad = unscoped(statements, site_scoped_tables())
    assert [s for _f, s in bad] == [
        "SELECT s.id, s.state FROM assess_sessions s WHERE s.cmid = :c "
        "AND s.student_userid = :u"
    ]
