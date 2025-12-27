# Vektra

**Modular open-source platform for Retrieval-Augmented Generation (RAG) with an e-learning layer.**

Vektra is designed as infrastructure, not as a consumer application. It provides building blocks for developers and organizations that need to integrate RAG capabilities into their systems, with a specialized layer for learning management and knowledge delivery.

## What is this repository?

`vektra-stack` is the **entrypoint** for understanding Vektra. It does not contain core implementation code. Instead, it serves as:

- A central reference for the overall architecture
- Documentation hub for the multi-repo ecosystem
- A guide to understanding how components relate to each other

The actual implementation lives in separate, focused repositories.

## Architecture Overview

Vektra follows a **multi-repository architecture**. Each component is an independent repository with clear boundaries and responsibilities.

```
vektra-stack (you are here)
│
├── vektra-core        → RAG engine (retrieval, generation, orchestration)
├── vektra-index       → Indexing and vector store abstraction
├── vektra-learn       → E-learning / LMS layer
├── vektra-admin       → Web administration interface
├── vektra-moodle      → Moodle plugin integration
├── vektra-sdk-py      → Python SDK
└── vektra-sdk-js      → JavaScript/TypeScript SDK
```

For detailed architecture documentation, see [ARCHITECTURE.md](./ARCHITECTURE.md).

## Design Principles

1. **Separation of concerns** — Each repository has a single, well-defined responsibility
2. **No tight coupling** — Components communicate through stable interfaces, not internal dependencies
3. **Infrastructure-first** — Built as platform components, not end-user applications
4. **Composability** — Use only what you need; components work independently or together
5. **Transparency** — Clear documentation of boundaries, capabilities, and limitations

## Repository Structure

```
vektra-stack/
├── README.md           # This file
├── ARCHITECTURE.md     # High-level system design
├── ROADMAP.md          # Development phases and milestones
├── CONTRIBUTING.md     # How to contribute
├── docs/               # Extended documentation
└── examples/           # Integration examples and patterns
```

## Current Status

Vektra is in early development. We are:

- Defining component boundaries and interfaces
- Establishing documentation standards
- Preparing the foundation for individual component repositories

See [ROADMAP.md](./ROADMAP.md) for planned phases.

## Related Repositories

| Repository | Description | Status |
|------------|-------------|--------|
| [vektra-core](https://github.com/vektralabs/vektra-core) | RAG engine | Planned |
| [vektra-index](https://github.com/vektralabs/vektra-index) | Indexing layer | Planned |
| [vektra-learn](https://github.com/vektralabs/vektra-learn) | E-learning layer | Planned |
| [vektra-admin](https://github.com/vektralabs/vektra-admin) | Admin interface | Planned |
| [vektra-moodle](https://github.com/vektralabs/vektra-moodle) | Moodle integration | Planned |
| [vektra-sdk-py](https://github.com/vektralabs/vektra-sdk-py) | Python SDK | Planned |
| [vektra-sdk-js](https://github.com/vektralabs/vektra-sdk-js) | JavaScript SDK | Planned |

## Contributing

We welcome contributions. Please read [CONTRIBUTING.md](./CONTRIBUTING.md) before submitting issues or pull requests.

## License

This project is open source. License details will be finalized as the project matures.

---

**Vektra** is maintained by [VektraLabs](https://github.com/vektralabs).
