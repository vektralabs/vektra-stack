# Vektra Roadmap

This document outlines the planned development phases for Vektra. It is intentionally high-level and subject to change based on community feedback and evolving requirements.

## Phases

### Phase 0: Foundation (Current)

Establish the groundwork for the project.

- [x] Create GitHub organization (`vektralabs`)
- [x] Define naming conventions and repository strategy
- [x] Set up `vektra-stack` as the central reference
- [x] Document architecture and component boundaries
- [ ] Define API contracts between core components (OpenAPI specs)
- [ ] Establish contribution guidelines and code standards
- [ ] Define safeguards policy interface

**Outcome:** A clear, documented foundation that enables focused development of individual components.

---

### Phase 1: Core RAG Engine and Pipeline

Build the fundamental RAG capabilities with orchestration support.

**vektra-core:**
- [ ] Initialize repository
- [ ] Implement RAG orchestration (retrieval + generation)
- [ ] Implement LLM abstraction (OpenAI, Anthropic, Ollama)
- [ ] Implement conversation management interfaces
- [ ] Implement safeguards hooks (pluggable policies)
- [ ] Implement streaming response support
- [ ] Implement confidence scoring

**vektra-ingest:**
- [ ] Initialize repository
- [ ] Implement document extractors (PDF, Word, PowerPoint, Markdown)
- [ ] Implement OCR for scanned documents
- [ ] Implement text cleaning and normalization
- [ ] Implement chunking strategies
- [ ] Expose granular API endpoints for n8n orchestration
- [ ] Emit events for pipeline monitoring

**vektra-index:**
- [ ] Initialize repository
- [ ] Implement embedding generation
- [ ] Implement vector store abstraction
- [ ] Implement PostgreSQL + pgvector adapter
- [ ] Implement semantic search
- [ ] Basic test coverage and CI setup

**Outcome:** A functional, orchestrable RAG pipeline that can ingest documents and answer queries.

---

### Phase 2: Analytics and Operations

Enable monitoring, metrics, and system administration.

**vektra-analytics:**
- [ ] Initialize repository
- [ ] Implement metrics collection from core components
- [ ] Implement query volume and topic aggregation
- [ ] Implement reporting API
- [ ] Implement export functionality (CSV, PDF)
- [ ] Implement alert triggers for n8n

**vektra-admin:**
- [ ] Initialize repository
- [ ] Implement system health monitoring dashboard
- [ ] Implement n8n workflow integration
- [ ] Implement pipeline status views
- [ ] Implement configuration management
- [ ] Implement alerting configuration

**Outcome:** Operators can monitor and manage Vektra deployments effectively.

---

### Phase 3: E-Learning Vertical

Build the learning management vertical and LMS integration.

**vektra-learn:**
- [ ] Initialize repository
- [ ] Implement chatbot web component (standalone, embeddable)
- [ ] Implement instructor dashboard (webapp)
- [ ] Implement token-based authentication for dashboard
- [ ] Implement LMS adapter interface
- [ ] Implement course enrollment abstractions

**vektra-moodle:**
- [ ] Initialize repository
- [ ] Implement SSO bridge with Moodle
- [ ] Implement content sync from Moodle courses
- [ ] Implement Moodle block for chatbot embed
- [ ] Implement token generator for instructor dashboard
- [ ] Implement enrollment sync

**Outcome:** A complete e-learning solution integrated with Moodle.

---

### Phase 4: SDKs and Developer Experience

Make the platform accessible to developers.

**vektra-sdk-py:**
- [ ] Initialize repository
- [ ] Implement Python client for core APIs
- [ ] Add type definitions
- [ ] Add async support
- [ ] Publish to PyPI
- [ ] Create getting-started documentation

**vektra-sdk-js:**
- [ ] Initialize repository
- [ ] Implement JavaScript/TypeScript client
- [ ] Add TypeScript types
- [ ] Support browser and Node.js
- [ ] Publish to npm
- [ ] Provide example applications

**Outcome:** Developers can integrate Vektra into their applications using familiar tools.

---

### Phase 5: First Production Deployment

Deploy the first production instance to validate the platform.

- [ ] Configure vektra-learn with institution branding
- [ ] Deploy on-premises infrastructure
- [ ] Configure local LLM (Ollama) or cloud provider
- [ ] Set up n8n workflows for document processing
- [ ] Configure PostgreSQL + pgvector
- [ ] Install vektra-moodle plugin
- [ ] Conduct user acceptance testing
- [ ] Go live with pilot courses

**Outcome:** First production deployment validating the platform in a real environment.

---

### Phase 6: Consolidation and Enhancement

Improve based on real-world usage.

- [ ] Additional vector store backends (Qdrant, Pinecone)
- [ ] Enhanced response quality for complex queries
- [ ] Advanced instructor dashboard features
- [ ] User feedback mechanisms
- [ ] Performance optimization
- [ ] Dashboard widgets embeddable in Moodle

**Outcome:** A mature, battle-tested platform.

---

## Beyond the Roadmap

Future considerations that are not yet scheduled:

- Multi-tenancy at core level
- Additional LMS adapters (Canvas, Blackboard)
- Support for mathematical formulas and diagrams
- Auto-generated self-assessment quizzes
- Proactive suggestions based on study patterns
- Personalized response style adaptation
- Predictive difficulty analysis
- Adaptive tutoring capabilities
- Advanced multilingual support
- Plugin architecture for custom safeguards
- Enterprise features (advanced SSO, audit logs, compliance)

These items will be prioritized based on community feedback and real-world usage.

---

## How to Influence the Roadmap

- Open issues for feature requests
- Participate in discussions
- Contribute to development

The roadmap is a living document. We value input from the community.

---

## Status Legend

- [x] Completed
- [ ] Planned / In Progress
