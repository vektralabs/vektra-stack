# Vektra Roadmap

This document outlines the planned development phases for Vektra. It is intentionally high-level and subject to change based on community feedback and evolving requirements.

## Phases

### Phase 0: Foundation (Current)

Establish the groundwork for the project.

- [x] Create GitHub organization (`vektralabs`)
- [x] Define naming conventions and repository strategy
- [x] Set up `vektra-stack` as the central reference
- [ ] Document architecture and component boundaries
- [ ] Define API contracts between core components
- [ ] Establish contribution guidelines and code standards

**Outcome:** A clear, documented foundation that enables focused development of individual components.

---

### Phase 1: Core RAG Engine

Build the fundamental RAG capabilities.

- [ ] Initialize `vektra-core` repository
- [ ] Implement basic retrieval pipeline
- [ ] Implement generation pipeline with LLM abstraction
- [ ] Initialize `vektra-index` repository
- [ ] Implement document ingestion and chunking
- [ ] Implement vector store abstraction with at least one backend
- [ ] Create initial API specifications (OpenAPI)
- [ ] Basic test coverage and CI setup

**Outcome:** A functional RAG engine that can ingest documents and answer queries.

---

### Phase 2: SDKs and Developer Experience

Make the core accessible to developers.

- [ ] Initialize `vektra-sdk-py`
- [ ] Initialize `vektra-sdk-js`
- [ ] Publish packages to PyPI and npm
- [ ] Create getting-started documentation
- [ ] Provide example applications

**Outcome:** Developers can integrate Vektra into their applications using familiar tools.

---

### Phase 3: Administration Interface

Provide operational tooling.

- [ ] Initialize `vektra-admin` repository
- [ ] Implement content management UI
- [ ] Implement system configuration interface
- [ ] Add monitoring and basic analytics

**Outcome:** Operators can manage Vektra deployments without direct API interaction.

---

### Phase 4: E-Learning Layer

Build the learning management capabilities.

- [ ] Initialize `vektra-learn` repository
- [ ] Implement course and content structures
- [ ] Implement learning path management
- [ ] Implement progress tracking
- [ ] Integrate with vektra-core for RAG-powered learning

**Outcome:** A functional LMS layer built on RAG capabilities.

---

### Phase 5: Integrations

Extend reach through platform integrations.

- [ ] Initialize `vektra-moodle` repository
- [ ] Implement Moodle plugin for Vektra integration
- [ ] Explore additional LMS integrations based on demand

**Outcome:** Vektra capabilities available within existing learning platforms.

---

## Beyond the Roadmap

Future considerations that are not yet scheduled:

- Multi-tenancy support
- Advanced analytics and reporting
- Plugin architecture for custom extensions
- Enterprise features (SSO, audit logs, compliance)
- Additional vector store backends
- Multilingual support improvements

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
