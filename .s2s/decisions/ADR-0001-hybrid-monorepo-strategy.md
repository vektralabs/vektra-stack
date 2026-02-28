# ADR-0001: Hybrid monorepo strategy

**Status**: Accepted
**Date**: 2026-01-28
**Deciders**: Roundtable session 20260128-roundtable-vektra (product-manager, documentation-specialist, oss-community-manager)

## Context

The initial project constraints specified "Multi-repo structure: each component in a separate repo for independent deployment" with 9 planned components. However, this approach presents challenges for an early-stage project:

- **Overhead**: 9 separate repos require independent CI/CD, issue tracking, and release management
- **Contributor friction**: New contributors must understand which repo to clone and how repos relate
- **Cross-component changes**: Features touching multiple components require coordinated PRs across repos
- **Ghost-town effect**: Multiple empty or low-activity repos create perception of abandoned project

## Decision

Adopt a **hybrid monorepo strategy**:

1. **Monorepo (vektra-stack)** for all Python components sharing types, configuration, and deployment target:
   - vektra-core (RAG engine)
   - vektra-ingest (document pipeline)
   - vektra-index (vector store)
   - vektra-analytics (metrics)
   - vektra-learn (e-learning vertical)
   - vektra-admin (administration UI)

2. **Separate repositories** only for components with incompatible tech stacks or deployment targets:
   - vektra-moodle (PHP plugin, deployed into Moodle, tied to Moodle versions)
   - vektra-sdk-py (published to PyPI, independent versioning)
   - vektra-sdk-js (published to npm, independent versioning)

3. **Split criteria** (see ADR-0002): A monorepo component may be split when 2+ of these conditions are met:
   - Independent external adopters
   - Dedicated maintainer team
   - Divergent release cadence
   - Stable interfaces (no breaking changes in 2+ releases)

## Consequences

### Positive

- Single clone for full development environment
- Unified CI/CD pipeline
- Atomic commits for cross-component changes
- Simplified contributor onboarding
- Coherent documentation in one place

### Negative

- Larger repo size over time
- Need for module boundary enforcement (CI-level import checks)
- vektra-moodle requires separate development environment (PHP)

### Neutral

- S2S workspace mode still useful for coordinating with external repos (moodle, SDKs)
- Future splits remain possible when criteria are met

## Alternatives Considered

1. **Full multi-repo from start**: Rejected due to overhead and ghost-town risk
2. **Full monorepo including Moodle**: Rejected because PHP plugin has incompatible stack and deployment target
3. **Defer decision**: Rejected because structure affects early architecture decisions

## References

- Roundtable session: `.s2s/sessions/20260128-roundtable-vektra.yaml`
- Requirements: REQ-005 (Monorepo-first development strategy), REQ-006 (Repo-split readiness criteria)
