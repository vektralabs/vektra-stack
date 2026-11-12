"""Runtime guard of the site binding (ADR-0033 D9.2): no API statement on a site-scoped table
runs in a transaction that has not bound ``app.assess_site_id``.

RLS makes such a statement read nothing, which a route can mistake for "no row" (a 404, an empty
list) rather than fail. So every route test that goes through ``support.world`` installs this
guard on the API engine and fails at teardown when a statement reached a covered table unbound:
the forgotten binding is reported as such, with the statement, instead of as a puzzling 404.

The covered tables are computed, not listed: the mapped tables with a ``site_id`` column
(``db/models.py``) that the API role may SELECT (``vektra_assess.db.grants``) — the set the
RLS revision covers, which ``integration/test_api_rls.py`` checks against the catalog.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine
from test_site_predicate_static import site_scoped_tables

from vektra_assess.db import grants as g

COVERED: frozenset[str] = frozenset(
    t for t in site_scoped_tables() if "SELECT" in g.MATRIX[g.API].get(t, {})
)
#: a table name, not a function call (``assess_site_of_key(`` is a definer function)
_TABLE = re.compile(r"\b(assess_\w+)\b(?!\s*\()")
_BINDS = re.compile(r"set_config\(\s*'app\.assess_site_id'", re.I)
_FLAG = "assess_site_bound_test_guard"


class BindingGuard:
    """Watches one or more API engines (``watch``: a test that swaps the app's engine for
    another must have the new one watched too); ``remove`` detaches from all of them."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.violations: list[str] = []
        self._engines: list[Any] = []
        self.watch(engine)

    def watch(self, engine: AsyncEngine) -> AsyncEngine:
        sync = engine.sync_engine
        if sync not in self._engines:
            event.listen(sync, "before_cursor_execute", self._before)
            event.listen(sync, "commit", self._end)
            event.listen(sync, "rollback", self._end)
            self._engines.append(sync)
        return engine

    def remove(self) -> None:
        for sync in self._engines:
            event.remove(sync, "before_cursor_execute", self._before)
            event.remove(sync, "commit", self._end)
            event.remove(sync, "rollback", self._end)
        self._engines.clear()

    def _end(self, conn: Any) -> None:
        conn.info.pop(_FLAG, None)

    def _before(
        self,
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if _BINDS.search(statement):
            conn.info[_FLAG] = True
            return
        tables = {t.lower() for t in _TABLE.findall(statement)} & COVERED
        if tables and not conn.info.get(_FLAG):
            self.violations.append(" ".join(statement.split())[:160])

    def take(self) -> list[str]:
        out, self.violations = self.violations, []
        return out
