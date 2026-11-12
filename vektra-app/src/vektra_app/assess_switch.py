"""The assess switch, read in one place (ADR-0033 D7).

``VEKTRA_ASSESS_ENABLED`` decides three things: whether the API mounts the assess routes, which
Alembic branches the API's schema step requires, and which branches ``docker/entrypoint.sh``
migrates. All three read :func:`assess_enabled`, so they cannot disagree on a spelling the shell
would have parsed differently. A broken assess configuration reads as off: no route, no assess
migration, and the API's step 12 reports the error.

Run as ``python -m vektra_app.assess_switch``: prints the branches to migrate, one per line, for
the entrypoint. Light on purpose: no FastAPI, no database.
"""

from __future__ import annotations

from pydantic import ValidationError

from vektra_assess.config import VektraAssessSettings


def assess_enabled() -> bool:
    """``VektraAssessSettings().enabled``; False when the assess settings do not validate."""
    try:
        return VektraAssessSettings().enabled
    except ValidationError:
        return False


def migration_branches() -> tuple[str, ...]:
    """The platform always; the assess branch only when assess is enabled."""
    return ("platform", "assess") if assess_enabled() else ("platform",)


def main() -> None:
    for branch in migration_branches():
        print(branch)


if __name__ == "__main__":
    main()
