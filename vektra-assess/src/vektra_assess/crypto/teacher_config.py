"""Teacher configuration sealed to the worker's public key (OQ-034 default, plan 4.4).

The API can WRITE teacher config (it holds the worker's PUBLIC key) but can never read it:
it has no private key, and the ``teacher_config_sealed`` column is outside its SELECT grant.
"""

from __future__ import annotations

from vektra_assess.crypto.envelope import Purpose
from vektra_assess.crypto.seal import open_sealed, seal
from vektra_assess.keys import AssessKeyring

#: The ONE binding field of ``assess_activity_config.teacher_config_sealed``: the column holds a
#: single JSON object, ``{"solution"?: str, "transcript_recipients": [str, ...]}``, sealed under
#: this field (decided 2026-11-08, plan assess-transcript-delivery 10b review #4). Every writer
#: (plan 11's config PUT), reader (snapshot capture, delivery) and the rotation use it.
CONFIG_FIELD = "config"


def _binding(site_id: object, cmid: int, field: str) -> list[object]:
    return [site_id, cmid, field]


def seal_teacher_config(
    worker_public: AssessKeyring,
    site_id: object,
    cmid: int,
    field: str,
    text: str,
) -> tuple[bytes, str]:
    """API side. Returns ``(sealed_blob, kid)`` for ``teacher_config_sealed/_kid``."""
    kid = worker_public.active
    blob = seal(
        worker_public.active_key,
        kid,
        text.encode("utf-8"),
        purpose=Purpose.TEACHER_CONFIG,
        binding=_binding(site_id, cmid, field),
    )
    return blob, kid


def open_teacher_config(
    worker_private: AssessKeyring,
    blob: bytes,
    site_id: object,
    cmid: int,
    field: str,
) -> str:
    """Worker side."""
    return open_sealed(
        worker_private,
        blob,
        purpose=Purpose.TEACHER_CONFIG,
        binding=_binding(site_id, cmid, field),
    ).decode("utf-8")
