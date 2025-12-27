# Vektra Architecture

This document describes the high-level architecture of Vektra. It defines the responsibilities of each component and how they interact.

## Overview

Vektra is structured in three conceptual layers:

```
┌─────────────────────────────────────────────────────────────┐
│                      APPLICATION LAYER                       │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │ vektra-admin│  │vektra-learn │  │   vektra-moodle     │  │
│  │ (Web Admin) │  │ (LMS Layer) │  │ (Moodle Plugin)     │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                        CORE LAYER                            │
│  ┌──────────────────────────────────────────────────────┐   │
│  │                    vektra-core                        │   │
│  │        (RAG Engine: Retrieval + Generation)           │   │
│  └──────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────┐   │
│  │                    vektra-index                       │   │
│  │         (Indexing + Vector Store Abstraction)         │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      INTEGRATION LAYER                       │
│  ┌─────────────────┐              ┌─────────────────────┐   │
│  │  vektra-sdk-py  │              │   vektra-sdk-js     │   │
│  │  (Python SDK)   │              │ (JavaScript SDK)    │   │
│  └─────────────────┘              └─────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

## Component Responsibilities

### Core Layer

#### vektra-core

The RAG engine. Responsible for:

- Document retrieval based on semantic similarity
- Response generation using retrieved context
- Orchestration of retrieval and generation pipelines
- LLM provider abstraction (multiple backends)
- Prompt management and templating

**Does not handle:**
- Vector storage implementation (delegated to vektra-index)
- User interface
- User management or authentication

#### vektra-index

Indexing and vector store abstraction. Responsible for:

- Document ingestion and preprocessing
- Text chunking strategies
- Embedding generation
- Vector store operations (CRUD)
- Adapter layer for multiple vector databases

**Supported backends (planned):**
- PostgreSQL with pgvector
- Qdrant
- Pinecone
- Weaviate
- In-memory (for development/testing)

### Application Layer

#### vektra-learn

E-learning / LMS functionality. Responsible for:

- Course and content structure
- Learning path management
- Progress tracking
- Assessment and quiz integration
- Knowledge delivery workflows

**Depends on:** vektra-core for RAG capabilities

#### vektra-admin

Web administration interface. Responsible for:

- System configuration
- Content management UI
- User and permission management
- Monitoring and analytics dashboards

**Depends on:** vektra-core, vektra-index

#### vektra-moodle

Moodle LMS plugin. Responsible for:

- Integration with Moodle platform
- Exposing Vektra capabilities within Moodle courses
- Synchronization of content and progress

**Depends on:** vektra-core

### Integration Layer

#### vektra-sdk-py

Python SDK for integrating Vektra into Python applications.

- Client library for core API
- Type definitions
- Convenience utilities

#### vektra-sdk-js

JavaScript/TypeScript SDK for web and Node.js applications.

- Client library for core API
- TypeScript types
- Framework integrations (optional)

## Inter-Component Communication

Components communicate via:

1. **HTTP APIs** — Primary interface between layers
2. **Message queues** — For async operations (indexing, batch processing)
3. **Shared contracts** — API schemas defined in OpenAPI format

Each component exposes a versioned API. Breaking changes follow semver.

## Data Flow

### Query Flow (RAG)

```
User Query
    │
    ▼
[Application Layer]  →  receives query from user/client
    │
    ▼
[vektra-core]        →  processes query, requests relevant documents
    │
    ▼
[vektra-index]       →  performs vector similarity search
    │
    ▼
[vektra-core]        →  generates response using retrieved context
    │
    ▼
[Application Layer]  →  returns response to user/client
```

### Indexing Flow

```
Source Document
    │
    ▼
[Application Layer]  →  initiates indexing
    │
    ▼
[vektra-index]       →  chunks, embeds, stores vectors
    │
    ▼
[Vector Store]       →  persists embeddings
```

## Deployment Considerations

Each component can be deployed independently:

- **vektra-core** and **vektra-index** can run as separate services or be embedded
- **vektra-admin** is a standalone web application
- **vektra-learn** can be deployed alongside core or as a separate service
- **SDKs** are client libraries, not deployed services

Detailed deployment guides will be provided in each component's repository.

## Security Boundaries

- Authentication and authorization are handled at the application layer
- Core components expect authenticated requests
- API keys or tokens are used for service-to-service communication
- No secrets stored in code; configuration via environment variables

## Future Considerations

This architecture is designed to accommodate:

- Horizontal scaling of core components
- Plugin systems for custom adapters
- Multi-tenancy support
- Federated deployments

These capabilities will be developed based on community needs and feedback.

---

For questions about architecture decisions, open an issue in this repository.
