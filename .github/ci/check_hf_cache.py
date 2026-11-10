"""CI only: refuse to save a Hugging Face cache that lacks a model the next run needs.

A saved cache is restored as an exact hit and the job then runs with
HF_HUB_OFFLINE=1, so an incomplete cache would fail the next run instead of this
one. The required set is the code's default embedding model and default reranker
(read from vektra_shared.config, so it follows the defaults) plus any repo ids
given on the command line. Each must have a snapshot holding model weights.

Usage: HF_HOME=<cache dir> python .github/ci/check_hf_cache.py [repo_id ...]
Exit 0: complete. Exit 1: something is missing (the save step is skipped).
"""

from __future__ import annotations

import sys
from pathlib import Path

from huggingface_hub import snapshot_download

from vektra_shared.config import EmbeddingConfig, RerankConfig

WEIGHTS = ("*.safetensors", "*.bin", "*.onnx")


def _repo(name: str) -> str:
    # sentence-transformers resolves a bare model name under its own organisation
    return name if "/" in name else f"sentence-transformers/{name}"


def main(extra: list[str]) -> int:
    embedding = EmbeddingConfig.model_fields["embedding_model"].default
    reranker = RerankConfig.model_fields["model"].default
    required = [_repo(embedding), *([reranker] if reranker else []), *map(_repo, extra)]
    missing = []
    for repo in required:
        try:
            path = Path(snapshot_download(repo, local_files_only=True))
        except Exception as exc:  # any failure means "not cached"
            missing.append(f"{repo}: {type(exc).__name__}")
            continue
        if not any(next(path.rglob(p), None) for p in WEIGHTS):
            missing.append(f"{repo}: snapshot without weights")
        else:
            print(f"cached: {repo}")
    for line in missing:
        print(f"missing: {line}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
