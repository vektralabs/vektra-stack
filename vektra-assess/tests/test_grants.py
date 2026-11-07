"""Static invariants of the grant matrix (the catalog comparison is in tests/integration)."""

from __future__ import annotations

from vektra_assess.db import grants as g

NO_API_ACCESS = (
    "assess_snapshots",
    "assess_turns",
    "assess_session_keys",
    "assess_session_handoff",
)


def test_api_has_no_privilege_on_session_material():
    for table in NO_API_ACCESS:
        assert table not in g.MATRIX[g.API]


def test_api_cannot_read_the_sealed_teacher_config():
    config = g.MATRIX[g.API]["assess_activity_config"]
    assert "teacher_config_sealed" not in (config["SELECT"] or ())
    assert "teacher_config_sealed" in (config["INSERT"] or ())  # it may seal, not read


def test_nobody_may_update_snapshots_turns_or_audit():
    for role in g.ROLES:
        for table in ("assess_snapshots", "assess_turns", "assess_audit_events"):
            assert "UPDATE" not in g.MATRIX[role].get(table, {}), (role, table)


def test_only_the_worker_deletes_session_data_and_audit_is_insert_only():
    for table in ("assess_audit_events",):
        for role in g.ROLES:
            assert set(g.MATRIX[role].get(table, {})) <= {"INSERT"}
    assert "DELETE" not in g.MATRIX[g.API].get("assess_sessions", {})


def test_every_matrix_table_and_sequence_is_known():
    for role in g.ROLES:
        assert set(g.MATRIX[role]) <= set(g.TABLES)
        assert set(g.SEQUENCE_USAGE.get(role, ())) <= set(g.SEQUENCES)


def test_render_starts_with_revoke_and_never_grants_to_public():
    stmts = g.render_statements()
    assert stmts[0].startswith("REVOKE ALL ON assess_")
    assert not [s for s in stmts if s.startswith("GRANT") and " PUBLIC" in s]
    assert "GRANT SELECT (id, site_id" in " ".join(stmts)  # column-level grants render
