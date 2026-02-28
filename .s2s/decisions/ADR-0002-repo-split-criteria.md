# ADR-0002: Repository split criteria

**Status**: Accepted
**Date**: 2026-01-28
**Deciders**: Roundtable session 20260128-roundtable-vektra (product-manager, documentation-specialist, oss-community-manager)

## Context

With the hybrid monorepo strategy (ADR-0001), most components start in the monorepo. Clear criteria are needed to determine when a component should be split into its own repository.

## Decision

A component should be split into its own repository when **at least two** of the following conditions are true:

1. **Independent external adopters**: The component is used by external projects without the rest of Vektra (e.g., someone uses vektra-index as a standalone vector store abstraction)

2. **Dedicated maintainer team**: The component has a dedicated maintainer or team with its own review cadence, distinct from the core maintainers

3. **Divergent release cadence**: The component needs to release on a different schedule than the monorepo (e.g., security patches, hotfixes, or slower/faster iteration)

4. **Stable interfaces**: The component's interfaces have stabilized with no breaking changes in 2+ releases, indicating maturity for independent evolution

## Consequences

### Positive

- Prevents premature splitting
- Clear, measurable criteria
- Preserves monorepo benefits until genuinely needed
- Reduces maintenance burden of unnecessary repos

### Negative

- Requires tracking these conditions over time
- Split decision requires maintainer consensus
- Some gray area in interpreting "independent adopters"

## Process

When a split is proposed:

1. Document which criteria are met
2. Open a discussion in GitHub Discussions
3. Reach maintainer consensus
4. Create new repo, migrate code and history
5. Update workspace.yaml and cross-references
6. Announce in release notes

## References

- Roundtable session: `.s2s/sessions/20260128-roundtable-vektra.yaml`
- Requirement: REQ-006 (Repo-split readiness criteria)
- Related: ADR-0001 (Hybrid monorepo strategy)
