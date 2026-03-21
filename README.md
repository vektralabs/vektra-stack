# Vektra

[![CI](https://github.com/vektralabs/vektra-stack/actions/workflows/ci-unit.yml/badge.svg?branch=main)](https://github.com/vektralabs/vektra-stack/actions/workflows/ci-unit.yml)
[![Lint](https://github.com/vektralabs/vektra-stack/actions/workflows/lint.yml/badge.svg?branch=main)](https://github.com/vektralabs/vektra-stack/actions/workflows/lint.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/downloads/)

**Modular, open-source RAG infrastructure you deploy on your own terms.**

Plug in any LLM provider, vector store, or pipeline, keep data on-premises, and extend with vertical modules like e-learning, all through configuration rather than code forks.

## Who is Vektra for?

- **Platform engineers / DevOps teams** deploying RAG at their organization with data sovereignty requirements
- **Developers** building RAG-powered applications via SDKs
- **Domain specialists** (e.g., university e-learning teams) using vertical modules without touching infrastructure

## What makes Vektra different?

Unlike RAG toolkits (LangChain, LlamaIndex, Haystack), Vektra ships as a **deployable platform**:

| Aspect | Toolkits | Vektra |
|--------|----------|--------|
| Deployment | Build your own | `docker compose up` |
| Configuration | Code changes | YAML/environment |
| On-premises | DIY | First-class support |
| GDPR compliance | DIY | Built-in (retention, audit logs) |
| Vertical features | None | E-learning module included |

## Quick start

```bash
git clone https://github.com/vektralabs/vektra-stack.git
cd vektra-stack
cp .env.example .env
# Edit .env: set VEKTRA_LLM_PROVIDER and your LLM API key
docker compose up -d
make demo
```

See [docs/getting-started/](docs/getting-started/index.md) for the full walkthrough. Target: from clone to first RAG query in under 30 minutes.

## Repository structure

Vektra uses a hybrid monorepo approach ([ADR-0001](.s2s/decisions/ADR-0001-hybrid-monorepo-strategy.md)):

```text
vektra-stack/              # This repository (monorepo)
├── vektra-core/           # RAG engine, LLM abstraction
├── vektra-ingest/         # Document processing (PDF, OCR, PPT, Word)
├── vektra-index/          # Vector store abstraction, embedding
├── vektra-analytics/      # Metrics, reporting, alerting
├── vektra-learn/          # E-learning vertical (chatbot, dashboard)
├── vektra-admin/          # System administration
├── docs/                  # Documentation
└── docker-compose.yml     # Orchestration

# Separate repositories (different tech stack or deployment target)
vektra-moodle/             # Moodle LMS adapter (PHP plugin)
vektra-sdk-py/             # Python SDK (published to PyPI)
vektra-sdk-js/             # JavaScript SDK (published to npm)
```

## Platform architecture

```text
┌───────────────────────────────────────────────────────────────────┐
│  VERTICALS           Domain solutions (vektra-learn for e-learning)│
├───────────────────────────────────────────────────────────────────┤
│  LMS ADAPTERS        Platform integrations (vektra-moodle, etc.)  │
├───────────────────────────────────────────────────────────────────┤
│  CORE                RAG engine, ingestion, indexing, analytics   │
├───────────────────────────────────────────────────────────────────┤
│  OPERATIONS          System administration (vektra-admin)         │
├───────────────────────────────────────────────────────────────────┤
│  INTEGRATION         SDKs (Python, JavaScript)                    │
└───────────────────────────────────────────────────────────────────┘
```

## Development status

See [ROADMAP.md](ROADMAP.md) for the full development plan.

| Phase | Focus | Status |
|-------|-------|--------|
| Phase 1 | MVP (core + ingest + index + minimal admin) | Complete |
| Phase 2 | Verticals (learn + moodle + analytics) | Complete |
| Phase 3 | Ecosystem (SDKs, plugins) | Planned |

## Documentation

- **[docs/](docs/)** - Full documentation (quick start, API reference, configuration, error codes)
- [CHANGELOG.md](CHANGELOG.md) - Release history
- [ROADMAP.md](ROADMAP.md) - Development phases and milestones
- [GOVERNANCE.md](GOVERNANCE.md) - Project governance and contributor ladder
- [CONTRIBUTING.md](CONTRIBUTING.md) - How to contribute
- [SECURITY.md](SECURITY.md) - Vulnerability reporting policy
- [.s2s/decisions/](.s2s/decisions/) - Architecture Decision Records

## Contributing

We welcome contributions! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on:
- Developer Certificate of Origin (DCO) sign-off
- Conventional Commits format
- PR workflow

## License

Apache License 2.0. See [LICENSE](LICENSE) for details.

---

**Vektra** is maintained by [VektraLabs](https://github.com/vektralabs).
