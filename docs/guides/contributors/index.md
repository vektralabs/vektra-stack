# Contributor guide

## Code ownership and PR review

Vektra uses GitHub's [CODEOWNERS](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners) feature for automatic review assignment.

The file lives at `.github/CODEOWNERS`. Each component directory is mapped to one or more maintainers. When a PR touches a file in a component directory, GitHub automatically requests a review from the listed owner(s).

### To become a reviewer for a component

1. Open a PR that modifies the relevant `CODEOWNERS` entry to add your GitHub username.
2. The current owner must approve and merge it.

### Component ownership map

| Directory | Component | Owner(s) |
|-----------|-----------|---------|
| `vektra-shared/` | Shared protocols and types | @fvadicamo |
| `vektra-core/` | RAG engine and query pipeline | @fvadicamo |
| `vektra-ingest/` | Document ingestion pipeline | @fvadicamo |
| `vektra-index/` | Vector store and embedding | @fvadicamo |
| `vektra-admin/` | Administration interface | @fvadicamo |
| `.s2s/` | Specifications and architecture | @fvadicamo |
| `docs/` | Documentation | @fvadicamo |
| `deploy/` | Deployment configuration | @fvadicamo |
| `*` | All other files (catch-all) | @fvadicamo |

## Development setup

```bash
git clone https://github.com/vektralabs/vektra-stack.git
cd vektra-stack
uv sync
```

## Running tests

```bash
# All unit tests (excludes integration tests)
make test

# Single component
pytest vektra-core/tests/ -m "not integration"

# With coverage
pytest vektra-core/tests/ --cov=vektra_core --cov-fail-under=80
```

## Linting

```bash
make lint
```

This runs ruff (check + format), mypy, and import-linter. All must pass before a PR can merge.

## Adding a new DocumentExtractor

1. Create a class implementing `DocumentExtractor` Protocol from `vektra_shared.protocols`:

```python
from vektra_shared.protocols import DocumentExtractor, ExtractedDocument

class MyExtractor:
    async def extract(self, content: bytes, content_type: str) -> ExtractedDocument:
        ...

    def supported_types(self) -> list[str]:
        return ["application/pdf"]
```

2. Register it in `vektra-app/src/vektra_app/main.py` step 5 (`_step_5_register_providers`).

3. Add `VEKTRA_DOCUMENT_EXTRACTOR=my-extractor` as a config option.

## Adding a new LLM provider

LLM providers go through litellm, so most providers work out of the box. Set `VEKTRA_LLM_PROVIDER` to any litellm-compatible model string.

For a custom provider not supported by litellm:

1. Implement the `LLMProvider` Protocol from `vektra_shared.protocols`.
2. Register it in the provider registration step.

See [architecture overview](../../architecture/index.md) for the Protocol interface list.

## Development workflow

See `CONTRIBUTING.md` at the repository root for the full contribution process, including branch naming, commit message conventions, and the pull request checklist.
