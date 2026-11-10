"""``python -m vektra_assess.keys rewrap``: re-wrap the rows under an old kid (plan 13a).

The rotation helpers of ``crypto.rotation`` existed since plan 2. Nothing called
``rewrap_teacher_reasons`` (plan 11b), so a transcript-key rotation could have retired a kid that
the teachers' reasons still needed. This command is the code half of the rotation runbook: it runs
the helpers as the worker role, in committed batches, until no row carries the old kid.

- ``--what session-keys --from-kid K``: the session DEKs under the KEK ``assess_kek``.
- ``--what transcripts --from-kid K``: the transcript DEKs **and** the teachers' reset and
  termination reasons, both under ``assess_transcript_kek``. One command covers both, so the kid
  cannot be retired with reasons left behind.

The loaded keyring must hold the old kid and the new active one. Exit status: 0 when nothing is
left (the old kid can go), 1 when rows remain (locked: run it again; or rows that do not
decrypt, listed by id: a data or key problem, the other rows are still re-wrapped), 2 when the
command cannot run. Only counts and kids are printed, never keys or the DSN.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence
from typing import Any, TextIO

from vektra_assess.crypto import rotation

WHAT = {
    "session-keys": ("assess_kek", ("rewrap_session_keys",)),
    "transcripts": (
        "assess_transcript_kek",
        ("rewrap_transcript_deks", "rewrap_teacher_reasons"),
    ),
}


def steps_for(
    what: str, ring: Any, from_kid: str, batch: int, skip: set[str] | None = None
) -> list[Callable[[Any], Any]]:
    """The re-wrap steps of ``what``. With ``skip``, a row that does not decrypt is recorded
    there by ``"<table>:<id>"`` and left alone instead of aborting the batch."""
    _, names = WHAT[what]
    return [
        (lambda fn: lambda conn: fn(conn, ring, from_kid, batch, skip=skip))(
            getattr(rotation, n)
        )
        for n in names
    ]


async def run(what: str, from_kid: str, batch: int, out: TextIO) -> int:
    from vektra_assess.keys_verify import open_role

    opened = await open_role("worker", out, step="keys_rewrap")
    if isinstance(opened, int):
        return opened
    loaded, engine = opened
    key_name, _ = WHAT[what]
    from vektra_assess.keys import AssessKeyring

    # the key matrix loads keyrings as ``AssessKeyring`` (only passwords carry ``.value``)
    ring = loaded.get(key_name)
    failed: set[str] = set()
    try:  # one ``finally`` disposes the engine on every path after ``open_role``
        if not isinstance(ring, AssessKeyring) or from_kid not in ring.keys:
            print(f"cannot run: {key_name} does not hold kid {from_kid!r}", file=out)
            return 2
        if ring.active == from_kid:
            print(
                f"cannot run: {from_kid!r} is still the active kid of {key_name}",
                file=out,
            )
            return 2
        try:
            left = await rotation.rewrap_to_done(
                engine.connect, steps_for(what, ring, from_kid, batch, failed)
            )
        except Exception as exc:
            print(f"cannot run: database {type(exc).__name__}", file=out)
            return 2
    finally:
        await engine.dispose()
    print(f"{what}: {left + len(failed)} row(s) still under {from_kid}", file=out)
    if failed:
        # ids only, never content: a corrupt row or a key that is not the one it was sealed under
        print(
            f"{len(failed)} row(s) did not decrypt under {key_name} kid {from_kid!r} and were "
            "left as they are (a data or key problem, not a database one): "
            + ", ".join(sorted(failed)),
            file=out,
        )
    return 0 if left == 0 and not failed else 1


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m vektra_assess.keys rewrap")
    parser.add_argument("--what", choices=sorted(WHAT), required=True)
    parser.add_argument("--from-kid", required=True)
    parser.add_argument("--batch", type=int, default=100)
    args = parser.parse_args(list(argv))
    if not 1 <= args.batch <= 10_000:
        print("cannot run: --batch must be between 1 and 10000", file=sys.stdout)
        return 2
    return asyncio.run(run(args.what, args.from_kid, args.batch, sys.stdout))
