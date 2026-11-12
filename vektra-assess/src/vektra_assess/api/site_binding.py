"""The site an API request is bound to, and its row-level-security binding (ADR-0033 D9.2).

The database holds the API role to one site per transaction: revision ``a0002`` puts a policy on
every site-scoped table the role reads, comparing ``site_id`` with the transaction-local setting
``app.assess_site_id``. Unset, the role reads and writes nothing. This module is the one place
that sets it.

How a request gets its site:

- a student or download token: the verifying dependency (``verify_assess_token_*``) binds the
  site the signed token names, before the route runs;
- a site-bound key (``assess:mint``, ``assess:teacher``, ``assess:privacy-*``):
  ``common.site_of_key`` resolves the key's site through ``assess_site_of_key`` (a definer
  function: the resolution precedes the binding) and binds it in the same transaction;
- an admin route that names a site in its path: the route binds that site once it has checked
  it (``privacy``, ``setup_check``); an operator's transcript resend binds the transcript's site
  (``assess_transcript_site``);
- the ``PendingTurnDriver``: each answer binds the site its scan returned.

The site is kept in a context variable for the rest of the request, and every transaction the
API engine begins afterwards is bound at its start by a ``begin`` listener
(:func:`install_site_binding`), so a route's second and third transactions, and the reply task it
spawns, need no call. The assess router resets the variable at the start of every request
(:func:`reset_site`): no site is carried from one request to the next, even where requests share
a task (an in-process test client).

``set_config(..., true)`` is transaction-local: the value ends with the transaction, so a pooled
connection never hands one request's site to the next.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any
from uuid import UUID

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine

#: the setting the policies of revision a0002 read
SITE_SETTING = "app.assess_site_id"

_SITE: ContextVar[UUID | None] = ContextVar("vektra_assess_site", default=None)

_BIND = text(f"SELECT pg_catalog.set_config('{SITE_SETTING}', :site, true)")


class SiteBindingConflict(RuntimeError):
    """A request tried to bind a second, different site: a programming error, refused."""


def bound_site() -> UUID | None:
    return _SITE.get()


def reset_site() -> None:
    """No site: what every request starts with (the assess router's first dependency)."""
    _SITE.set(None)


def set_request_site(site: Any) -> UUID:
    """Bind the rest of this request (its later transactions, the tasks it starts) to ``site``.

    Refuses a different site once one is bound: one request, one site."""
    value = site if isinstance(site, UUID) else UUID(str(site))
    current = _SITE.get()
    if current is not None and current != value:
        raise SiteBindingConflict(
            "this request is already bound to another assess site"
        )
    _SITE.set(value)
    return value


async def bind_site(conn: Any, site: Any) -> UUID:
    """Bind the request to ``site`` and the transaction ``conn`` is in now."""
    value = set_request_site(site)
    await conn.execute(_BIND, {"site": str(value)})
    return value


def _on_begin(conn: Any) -> None:
    site = _SITE.get()
    if site is not None:
        # ``str(UUID)`` is canonical hex and hyphens: safe to inline
        conn.exec_driver_sql(
            f"SELECT pg_catalog.set_config('{SITE_SETTING}', '{site}', true)"
        )


def install_site_binding(engine: AsyncEngine) -> AsyncEngine:
    """Bind every transaction of the API engine to the request's site, at its start.
    Idempotent: safe on every ``engine_of`` call."""
    sync = engine.sync_engine
    if not event.contains(sync, "begin", _on_begin):
        event.listen(sync, "begin", _on_begin)
    return engine


async def reset_site_dependency() -> None:
    """The assess router's first dependency: every request starts unbound."""
    reset_site()
