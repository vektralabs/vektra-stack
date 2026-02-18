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

## Development workflow

See `CONTRIBUTING.md` at the repository root for the full contribution process, including branch naming, commit message conventions, and the pull request checklist.
