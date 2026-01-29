# Vektra

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
| Deployment | Build your own | `docker-compose up` |
| Configuration | Code changes | YAML/environment |
| On-premises | DIY | First-class support |
| GDPR compliance | DIY | Built-in (retention, audit logs) |
| Vertical features | None | E-learning module included |

## Quick start

```bash
git clone https://github.com/vektralabs/vektra-stack.git
cd vektra-stack
docker-compose up
```

Target: from clone to first RAG query in under 30 minutes.

## Repository structure

Vektra uses a hybrid monorepo approach ([ADR-0001](.s2s/decisions/ADR-0001-hybrid-monorepo-strategy.md)):

```
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

```
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
| Phase 1 | MVP (core + ingest + index + minimal admin) | In progress |
| Phase 2 | Verticals (learn + moodle + analytics) | Planned |
| Phase 3 | Ecosystem (SDKs, plugins) | Planned |

## Documentation

- [ROADMAP.md](ROADMAP.md) - Development phases and milestones
- [GOVERNANCE.md](GOVERNANCE.md) - Project governance and contributor ladder
- [CONTRIBUTING.md](CONTRIBUTING.md) - How to contribute
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
