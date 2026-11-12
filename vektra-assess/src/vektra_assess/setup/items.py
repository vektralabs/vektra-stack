"""Vektra's own setup-check items (REQ-079, ADR-0033 D3).

- ``ui_not_same_origin``: the chat UI origin is not the same site as the LMS or the API;
- ``ui_origin_distinct``: the UI origin is https (outside development) and is not the site's
  framing origin (plan 9, ARCH-076);
- ``lms_provider_registered``: an LMS provider is configured and loaded (ADR-0033 D1): without
  one the assessment vertical is unavailable, and this item is ``MISSING``.

Pluggable items (``parser_sidecar_running``, ``smtp_probe``, ``db_log_statement_none``,
``privacy_config``) are registered by the plans that own them; an unregistered one is
``MISSING`` (fail closed).
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from vektra_assess.setup.registry import (
    CheckContext,
    ItemRegistry,
    Outcome,
    missing,
    ok,
    unverifiable,
)

#: the plans that own these register them; until then they are MISSING (fail closed)
PLUGGABLE_ITEMS: tuple[str, ...] = (
    "parser_sidecar_running",
    "smtp_probe",
    "db_log_statement_none",
    "privacy_config",  # plan assess-privacy-retention (REQ-089)
)


def _host_of(origin: str) -> str | None:
    parts = urlsplit(origin.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return parts.hostname.lower().rstrip(".")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _site_key(host: str) -> str:
    """An approximation of the registrable domain WITHOUT the public-suffix list: the last two
    labels (the whole host for an IP literal or a single label). It can only err towards
    "same site": ``a.example.co.uk`` and ``b.example.co.uk`` end in ``co.uk`` here although a
    browser treats them as different sites. The check refuses on a match, so the error is a
    false refusal that an operator can read and fix, never an accepted same-site deployment."""
    if _is_ip(host):
        return host
    labels = host.split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def same_site(a: str, b: str) -> bool:
    """Same host, one a subdomain of the other, or the same (approximate) registrable domain."""
    ha, hb = _host_of(a), _host_of(b)
    if ha is None or hb is None:
        raise ValueError("not an http(s) origin")
    if ha == hb or ha.endswith("." + hb) or hb.endswith("." + ha):
        return True
    return _site_key(ha) == _site_key(hb)


async def ui_not_same_origin(ctx: CheckContext) -> Outcome:
    ui = ctx.settings.ui_origin
    if not ui:
        return unverifiable("ui_origin_unset")
    try:
        if same_site(ui, ctx.site.base_url):
            return missing("same_site_as_moodle")
        api = ctx.settings.api_public_origin
        if not api:
            return unverifiable("api_origin_unset")
        if same_site(ui, api):
            return missing("same_site_as_api")
    except ValueError:
        return unverifiable("origin_invalid")
    return ok()


async def ui_origin_distinct(ctx: CheckContext) -> Outcome:
    """Plan 9 (ARCH-076): the discussion UI origin is https (outside development) and is not
    this site's framing origin, the precondition of the iframe's ``allow-same-origin``
    sandbox. Same decision as the API's startup refusal (``ui.origins``)."""
    from vektra_assess.ui.origins import site_origin_or_problem, ui_origin_problem

    # the same origin the UI router puts in frame-ancestors: the column, else base_url's
    framing, invalid = site_origin_or_problem(ctx.moodle_origin, ctx.site.base_url)
    if invalid:
        return missing(invalid)
    problem = ui_origin_problem(
        ctx.settings.ui_origin,
        [framing] if framing else [],
        development=ctx.settings.is_development,
    )
    if problem is not None:
        return missing(problem)
    # another SITE, not only another origin, than both the framing origin (the column) and
    # base_url: either one same-site fails the item
    ui = str(ctx.settings.ui_origin)
    if framing and same_site(ui, framing):
        return missing("same_site_as_moodle_origin")
    if same_site(ui, ctx.site.base_url):
        return missing("same_site_as_moodle")
    return ok()


async def lms_provider_registered(ctx: CheckContext) -> Outcome:
    """ADR-0033 D1: no provider, no assessment vertical (fail closed)."""
    return ok() if ctx.provider is not None else missing("not_registered")


#: Vektra's built-in items, in report order (after the provider's own items)
GENERIC_ITEMS: tuple[str, ...] = (
    "ui_not_same_origin",
    "ui_origin_distinct",  # plan 9
    "lms_provider_registered",  # ADR-0033 D3
)


def builtin_registry() -> ItemRegistry:
    reg = ItemRegistry()
    for name, check in (
        ("ui_not_same_origin", ui_not_same_origin),
        ("ui_origin_distinct", ui_origin_distinct),
        ("lms_provider_registered", lms_provider_registered),
    ):
        reg.register(name, check)
    return reg


#: the registry the worker uses; other plans call ``DEFAULT_REGISTRY.register(...)``
DEFAULT_REGISTRY = builtin_registry()
