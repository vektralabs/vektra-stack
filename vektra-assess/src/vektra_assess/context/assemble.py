"""``assemble_context(snapshot, frozen_policy) -> ContextBundle``: pure, deterministic."""

from __future__ import annotations

from vektra_assess.context._assembly import assemble
from vektra_assess.context.bundle import ContextBundle
from vektra_assess.context.policy import FrozenPolicy
from vektra_assess.context.snapshot import Snapshot


def assemble_context(snapshot: Snapshot, frozen_policy: FrozenPolicy) -> ContextBundle:
    return assemble(snapshot, frozen_policy)
