"""Guard: the image pre-caches the model the code actually loads.

The Dockerfile downloads an embedding model at build time so the container does
not fetch it while serving its first request. That only helps if it is the same
model `EmbeddingConfig` defaults to — and for months it was not: the Dockerfile
pre-cached `all-MiniLM-L6-v2` (English) while the default was the multilingual
model, so every default deployment shipped 80MB it never used and downloaded the
real weights at startup anyway. The deployment that noticed had to patch the
Dockerfile on the host, which is the failure mode this repo keeps paying for.

The two values live in different files and nothing tied them together.
"""

from __future__ import annotations

import re
from pathlib import Path

from vektra_shared.config import EmbeddingConfig

DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"
COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"


def _dockerfile_arg_default(name: str) -> str | None:
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        m = re.match(rf"^ARG\s+{re.escape(name)}=(.+)$", line.strip())
        if m:
            return m.group(1).strip().strip('"').strip("'")
    return None


def test_precache_default_matches_the_config_default() -> None:
    config_default = EmbeddingConfig().embedding_model
    arg_default = _dockerfile_arg_default("VEKTRA_EMBEDDING_MODEL")

    assert arg_default is not None, (
        "Dockerfile has no ARG VEKTRA_EMBEDDING_MODEL: the pre-cache step is "
        "back to a hardcoded model, which drifts from the config the moment "
        "either side changes."
    )
    assert arg_default == config_default, (
        f"the image pre-caches {arg_default!r} but EmbeddingConfig defaults to "
        f"{config_default!r}, so a default deployment downloads its real model "
        "at startup and carries one it never loads."
    )


def test_precache_step_uses_the_arg() -> None:
    """A default that nothing reads would satisfy the check above vacuously."""
    body = DOCKERFILE.read_text(encoding="utf-8")
    precache = next(
        (line for line in body.splitlines() if "SentenceTransformer" in line),
        None,
    )
    assert precache is not None, "the pre-cache RUN step is gone"
    assert "VEKTRA_EMBEDDING_MODEL" in precache, (
        "the pre-cache step does not read ARG VEKTRA_EMBEDDING_MODEL, so the "
        "declared default is decoration"
    )


def test_compose_passes_the_model_through() -> None:
    """Without this, a deployment still has to edit the Dockerfile by hand."""
    body = COMPOSE.read_text(encoding="utf-8")
    assert "VEKTRA_EMBEDDING_MODEL: ${VEKTRA_EMBEDDING_MODEL" in body, (
        "docker-compose.yml does not forward VEKTRA_EMBEDDING_MODEL as a build "
        "arg, so `docker compose build` on a host running a different model "
        "bakes in the wrong one"
    )
