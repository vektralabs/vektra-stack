# Vektra Architecture

This document describes the high-level architecture of the Vektra platform. It defines the responsibilities of each component, their interactions, and how verticals and deployments are structured.

## Platform Overview

Vektra is a modular open-source platform for building Retrieval-Augmented Generation (RAG) applications. It is designed as infrastructure, providing building blocks that can be composed into vertical solutions.

### Architecture Layers

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              DEPLOYMENTS                                     │
│                                                                              │
│    Specific installations with custom branding and configuration             │
│    Example: Institution-specific deployments                                │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              VERTICALS                                       │
│                                                                              │
│    Domain-specific solutions built on Vektra core                           │
│    Example: vektra-learn (e-learning vertical)                              │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           LMS ADAPTERS                                       │
│                                                                              │
│    Platform-specific integrations                                           │
│    Example: vektra-moodle, vektra-canvas (future)                          │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              CORE LAYER                                      │
│                                                                              │
│    Reusable components: RAG engine, ingestion, indexing, analytics          │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           OPERATIONS                                         │
│                                                                              │
│    System administration and monitoring                                      │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           INTEGRATION                                        │
│                                                                              │
│    SDKs for programmatic access                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Full Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              DEPLOYMENTS                                     │
│                                                                              │
│    Institution A               Institution B          ...                   │
│    ┌────────────────┐          ┌────────────────┐                           │
│    │ vektra-* stack │          │ vektra-* stack │                           │
│    │ + custom config│          │ + custom config│                           │
│    │ + branding     │          │ + branding     │                           │
│    └────────────────┘          └────────────────┘                           │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              VERTICALS                                       │
│                                                                              │
│    ┌─────────────────────────────────────────────────────────────────┐      │
│    │                        vektra-learn                              │      │
│    │                  (E-Learning Vertical)                           │      │
│    │                                                                  │      │
│    │  • Chatbot web component (standalone, embeddable)               │      │
│    │  • Instructor dashboard (webapp, token-based auth)              │      │
│    │  • Course/enrollment abstractions                               │      │
│    │  • LMS adapter interface                                        │      │
│    │  • Student conversation UI logic                                │      │
│    └─────────────────────────────────────────────────────────────────┘      │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           LMS ADAPTERS                                       │
│                                                                              │
│    ┌───────────────────┐   ┌───────────────────┐   ┌──────────────────┐    │
│    │   vektra-moodle   │   │  vektra-canvas    │   │    (others)      │    │
│    │                   │   │    (future)       │   │                  │    │
│    │ • SSO bridge      │   │                   │   │                  │    │
│    │ • Content sync    │   │                   │   │                  │    │
│    │ • Chatbot embed   │   │                   │   │                  │    │
│    │ • Token generator │   │                   │   │                  │    │
│    │ • Dashboard widget│   │                   │   │                  │    │
│    └───────────────────┘   └───────────────────┘   └──────────────────┘    │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              CORE LAYER                                      │
│                                                                              │
│    ┌─────────────────────────────────────────────────────────────────┐      │
│    │                        vektra-core                               │      │
│    │                                                                  │      │
│    │  • RAG engine (retrieval + generation orchestration)            │      │
│    │  • LLM abstraction (OpenAI, Anthropic, Ollama, local models)   │      │
│    │  • Conversation management (context, history interface)         │      │
│    │  • Safeguards hooks (pluggable policies)                        │      │
│    │  • Streaming response support                                   │      │
│    │  • Confidence scoring                                           │      │
│    │  • Prompt templates                                             │      │
│    └─────────────────────────────────────────────────────────────────┘      │
│                                                                              │
│    ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────┐       │
│    │  vektra-ingest  │  │  vektra-index   │  │  vektra-analytics   │       │
│    │                 │  │                 │  │                     │       │
│    │ • Extractors    │  │ • Embeddings    │  │ • Metrics store     │       │
│    │   (PDF, OCR,    │─▶│ • Vector CRUD   │  │ • Query aggregation │       │
│    │    PPT, Word)   │  │ • Search        │  │ • Report API        │       │
│    │ • Chunking      │  │ • DB adapters   │  │ • Export            │       │
│    │ • Workflow hooks│  │                 │  │ • Alert triggers    │       │
│    └─────────────────┘  └─────────────────┘  └─────────────────────┘       │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           OPERATIONS                                         │
│                                                                              │
│    ┌─────────────────────────────────────────────────────────────────┐      │
│    │                        vektra-admin                              │      │
│    │                    (Ops Team Only)                               │      │
│    │                                                                  │      │
│    │  • System health monitoring                                     │      │
│    │  • Workflow orchestrator integration (n8n)                      │      │
│    │  • Pipeline status (ingest, index)                              │      │
│    │  • Configuration management                                     │      │
│    │  • Tenant/instance management                                   │      │
│    │  • Alerting configuration                                       │      │
│    └─────────────────────────────────────────────────────────────────┘      │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           INTEGRATION                                        │
│                                                                              │
│    ┌─────────────────────┐              ┌─────────────────────────┐         │
│    │    vektra-sdk-py    │              │      vektra-sdk-js      │         │
│    └─────────────────────┘              └─────────────────────────┘         │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Component Details

### Core Layer

#### vektra-core

The central RAG engine. Responsible for:

- **Retrieval orchestration** — Coordinates with vektra-index to find relevant content
- **Response generation** — Uses LLM to generate contextual answers
- **LLM abstraction** — Supports multiple providers (OpenAI, Anthropic, Ollama, local models)
- **Conversation management** — Defines conversation schema, context building, history interfaces
- **Safeguards hooks** — Pluggable policy system for content filtering, anti-cheating, PII detection
- **Streaming** — Real-time response streaming support
- **Confidence scoring** — Reliability indicators for generated responses
- **Prompt templates** — Configurable prompt engineering

**Does not handle:**
- Document extraction (delegated to vektra-ingest)
- Vector storage (delegated to vektra-index)
- User interface
- Authentication (handled by verticals/adapters)

#### vektra-ingest

Document processing pipeline. Designed for workflow orchestration (n8n). Responsible for:

- **Extractors** — PDF, PowerPoint, Word, Markdown, OCR for scanned documents and images
- **Text cleaning** — Normalization, formatting cleanup
- **Chunking** — Configurable strategies for splitting documents
- **Workflow hooks** — Granular API endpoints for external orchestration

**Pipeline architecture:**
```
[Source] → [Extract] → [Clean] → [Chunk] → [Queue]
    ↓          ↓          ↓          ↓         ↓
 (event)    (event)    (event)    (event)  → vektra-index
```

Each step emits events that workflow engines can consume for monitoring and retry logic.

#### vektra-index

Vector store abstraction. Responsible for:

- **Embedding generation** — Text to vector conversion
- **Vector CRUD** — Create, read, update, delete operations
- **Semantic search** — Similarity-based retrieval
- **Database adapters** — Pluggable backends

**Supported backends (planned):**
- PostgreSQL with pgvector
- Qdrant
- Pinecone
- Weaviate
- Milvus
- In-memory (development/testing)

#### vektra-analytics

Metrics and reporting engine. Responsible for:

- **Metrics collection** — Ingests telemetry from core components
- **Aggregation** — Query volume, topic trends, confidence distribution
- **Reporting API** — Data endpoints for dashboards
- **Export** — CSV, PDF report generation
- **Alert triggers** — Threshold-based notifications for n8n

**Consumed by:**
- vektra-admin (system metrics)
- vektra-learn instructor dashboard (course metrics)
- n8n workflows (automated alerting)

---

### Verticals

#### vektra-learn

E-learning vertical for higher education, corporate training, and other LMS-based contexts.

**Components:**

| Component | Description |
|-----------|-------------|
| **Chatbot web component** | Standalone, embeddable widget for student interactions |
| **Instructor dashboard** | Web application for course analytics, accessible via token-based auth |
| **LMS adapter interface** | Abstraction layer for different LMS platforms |
| **Course abstractions** | Enrollment, content, progress tracking models |

**Dashboard access pattern:**
```
Instructor in LMS → [Click analytics link] → [Token-authenticated URL] → Dashboard
```

Token is generated by LMS adapter (e.g., vektra-moodle), contains claims for user, course, role, and expiration.

---

### LMS Adapters

#### vektra-moodle

Moodle LMS integration. Responsible for:

- **SSO bridge** — Authentication via Moodle credentials
- **Content sync** — Automatic ingestion of course materials from Moodle
- **Chatbot embed** — Moodle block that embeds vektra-learn chatbot component
- **Token generator** — Creates signed tokens for instructor dashboard access
- **Dashboard widget** — Optional mini-dashboard embeddable in Moodle (roadmap)

**Implements:** vektra-learn LMS adapter interface

#### Future: vektra-canvas

Canvas LMS integration (planned). Same responsibilities as vektra-moodle, adapted for Canvas API.

---

### Operations

#### vektra-admin

System administration interface for ops teams. Responsible for:

- **System health** — Real-time monitoring of all components
- **Workflow integration** — n8n status, trigger management
- **Pipeline monitoring** — Ingest and index job status
- **Configuration** — System-wide settings management
- **Tenant management** — Multi-instance administration
- **Alerting** — Notification rules and channels

**Access:** Dedicated login for operations team only. Does not show conversation content.

---

### Integration

#### vektra-sdk-py

Python SDK for programmatic access to Vektra APIs.

- Type-safe client library
- Async support
- Convenience utilities

#### vektra-sdk-js

JavaScript/TypeScript SDK for web and Node.js applications.

- TypeScript types
- Browser and Node.js compatible
- Framework integrations (optional)

---

## Data Flows

### Query Flow (RAG)

```
User submits question
         │
         ▼
┌─────────────────┐
│  vektra-learn   │  (or other vertical)
│  chatbot        │
└────────┬────────┘
         │ conversation context + query
         ▼
┌─────────────────┐
│   vektra-core   │  processes query
└────────┬────────┘
         │ retrieval request
         ▼
┌─────────────────┐
│  vektra-index   │  semantic search
└────────┬────────┘
         │ relevant chunks
         ▼
┌─────────────────┐
│   vektra-core   │  generates response with LLM
└────────┬────────┘
         │ streamed response + sources
         ▼
┌─────────────────┐
│  vektra-learn   │  displays to user
│  chatbot        │
└─────────────────┘
```

### Indexing Flow (n8n Orchestrated)

```
Document uploaded to LMS
         │
         ▼
┌─────────────────┐
│ vektra-moodle   │  detects new content
└────────┬────────┘
         │ webhook to n8n
         ▼
┌─────────────────┐
│      n8n        │  orchestrates pipeline
└────────┬────────┘
         │
    ┌────┴────┬────────────┬────────────┐
    ▼         ▼            ▼            ▼
[extract] [clean]     [chunk]      [embed+store]
    │         │            │            │
    └────┬────┴────────────┴────────────┘
         │
         ▼
┌─────────────────┐
│ vektra-index    │  vectors stored
└─────────────────┘
```

---

## Deployment Models

Vektra supports both cloud and on-premises deployments with architectural parity.

### On-Premises

```
Institution Infrastructure
├── vektra-core (container/VM)
├── vektra-ingest (container/VM)
├── vektra-index (container/VM)
├── vektra-analytics (container/VM)
├── vektra-admin (container/VM)
├── vektra-learn (container/VM)
├── vektra-moodle (Moodle plugin)
├── n8n (container/VM)
├── PostgreSQL + pgvector
└── Local LLM (Ollama) or API proxy
```

### Cloud

```
Cloud Infrastructure
├── vektra-* services (containers/serverless)
├── Managed vector DB (Qdrant Cloud, Pinecone)
├── LLM APIs (OpenAI, Anthropic)
└── n8n Cloud or self-hosted
```

---

## Security Boundaries

### Authentication

- **Students** — Authenticate via LMS (SSO through vektra-moodle)
- **Instructors** — Token-based access to dashboard (generated by LMS adapter)
- **Operators** — Dedicated credentials for vektra-admin

### Authorization

- **Course isolation** — Students only access materials from enrolled courses
- **Role-based access** — Instructors see only their courses; operators see system metrics
- **No content access for ops** — vektra-admin shows metrics, not conversation content

### Data Protection

- **PII detection** — Configurable hooks in vektra-core
- **Retention policies** — Automatic conversation expiration
- **Encryption** — At rest and in transit (TLS)
- **No secrets in code** — Configuration via environment variables

---

## Configuration and Customization

Deployments are customized through configuration, not code forks.

| Customization | Configuration Location |
|--------------|------------------------|
| Branding (logo, colors) | vektra-learn config |
| Safeguard policies | vektra-core config |
| Retention period | vektra-core config |
| Prompt templates | vektra-core config |
| LLM provider | vektra-core config |
| Vector backend | vektra-index config |
| LMS type | vektra-learn config + appropriate adapter |

---

## Future Considerations

Planned capabilities based on roadmap:

- Multi-tenancy at core level
- Plugin architecture for custom safeguards
- Advanced analytics (learning patterns, predictive difficulty)
- Additional LMS adapters (Canvas, Blackboard)
- Formula and diagram interpretation
- Adaptive tutoring capabilities

---

For questions about architecture decisions, open an issue in this repository.
