# Vektra

**Modular open-source platform for Retrieval-Augmented Generation (RAG) with specialized verticals for e-learning and beyond.**

Vektra is designed as infrastructure, not as a consumer application. It provides building blocks for developers and organizations that need to integrate RAG capabilities into their systems, with a specialized vertical for learning management and knowledge delivery.

## What is this repository?

`vektra-stack` is the **entrypoint** for understanding Vektra. It does not contain core implementation code. Instead, it serves as:

- A central reference for the overall architecture
- Documentation hub for the multi-repo ecosystem
- A guide to understanding how components relate to each other

The actual implementation lives in separate, focused repositories.

## Platform Structure

Vektra is organized in layers:

```
┌───────────────────────────────────────────────────────────────────┐
│  DEPLOYMENTS         Branded instances for specific institutions │
├───────────────────────────────────────────────────────────────────┤
│  VERTICALS           Domain solutions (vektra-learn for e-learning)│
├───────────────────────────────────────────────────────────────────┤
│  LMS ADAPTERS        Platform integrations (vektra-moodle, etc.) │
├───────────────────────────────────────────────────────────────────┤
│  CORE                RAG engine, ingestion, indexing, analytics  │
├───────────────────────────────────────────────────────────────────┤
│  OPERATIONS          System administration (vektra-admin)        │
├───────────────────────────────────────────────────────────────────┤
│  INTEGRATION         SDKs (Python, JavaScript)                   │
└───────────────────────────────────────────────────────────────────┘
```

For detailed architecture documentation, see [ARCHITECTURE.md](./ARCHITECTURE.md).

## Design Principles

1. **Separation of concerns** — Each repository has a single, well-defined responsibility
2. **No tight coupling** — Components communicate through stable interfaces, not internal dependencies
3. **Infrastructure-first** — Built as platform components, not end-user applications
4. **Composability** — Use only what you need; components work independently or together
5. **Deployment parity** — Same architecture for cloud and on-premises installations
6. **Workflow orchestration** — Pipeline components designed for external orchestration (n8n)

## Repository Structure

```
vektra-stack/
├── README.md           # This file
├── ARCHITECTURE.md     # High-level system design
├── ROADMAP.md          # Development phases and milestones
└── CONTRIBUTING.md     # How to contribute
```

## Current Status

Vektra is in early development (Phase 0: Foundation). We are:

- Defining component boundaries and interfaces
- Documenting architecture and data flows
- Preparing the foundation for individual component repositories

See [ROADMAP.md](./ROADMAP.md) for planned phases.

## Ecosystem Repositories

### Core Layer

| Repository | Description | Status |
|------------|-------------|--------|
| [vektra-core](https://github.com/vektralabs/vektra-core) | RAG engine, LLM abstraction, conversation management, safeguards | Planned |
| [vektra-ingest](https://github.com/vektralabs/vektra-ingest) | Document processing pipeline (PDF, OCR, PPT, Word) | Planned |
| [vektra-index](https://github.com/vektralabs/vektra-index) | Vector store abstraction, embedding, semantic search | Planned |
| [vektra-analytics](https://github.com/vektralabs/vektra-analytics) | Metrics aggregation, reporting API, alerting | Planned |

### Verticals and Adapters

| Repository | Description | Status |
|------------|-------------|--------|
| [vektra-learn](https://github.com/vektralabs/vektra-learn) | E-learning vertical (chatbot, instructor dashboard) | Planned |
| [vektra-moodle](https://github.com/vektralabs/vektra-moodle) | Moodle LMS adapter (SSO, content sync, embed) | Planned |

### Operations and Integration

| Repository | Description | Status |
|------------|-------------|--------|
| [vektra-admin](https://github.com/vektralabs/vektra-admin) | System administration interface | Planned |
| [vektra-sdk-py](https://github.com/vektralabs/vektra-sdk-py) | Python SDK | Planned |
| [vektra-sdk-js](https://github.com/vektralabs/vektra-sdk-js) | JavaScript/TypeScript SDK | Planned |

## Contributing

We welcome contributions. Please read [CONTRIBUTING.md](./CONTRIBUTING.md) before submitting issues or pull requests.

## License

This project is open source. License details will be finalized as the project matures.

---

**Vektra** is maintained by [VektraLabs](https://github.com/vektralabs).
