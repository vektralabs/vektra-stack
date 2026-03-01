# Vektra Governance

This document describes the governance model for the Vektra project.

## License

Vektra is licensed under the **Apache License 2.0**. This license:
- Permits on-premises deployment with custom integrations
- Allows commercial use
- Requires attribution and license notice preservation
- Provides patent protection

See [LICENSE](LICENSE) for the full text.

## Governance Model: BDFL-transitional

During **Phases 1 and 2** (core development), Vektra follows a Benevolent Dictator For Life (BDFL) model:
- Final decision authority rests with the project founder
- Decisions are made transparently with community input via GitHub Discussions
- This model enables rapid iteration without governance overhead

**Transition criteria**: The project will transition to a maintainer committee when:
- There are 3+ active maintainers with 6+ months of consistent contribution
- The contributor base has grown beyond the founding team
- Core interfaces have stabilized (Phase 1 complete, Phase 2 in progress)

## Contributor Ladder

| Role | Responsibilities | How to achieve |
|------|-----------------|----------------|
| **Contributor** | Submit PRs, report issues, participate in discussions | First merged PR |
| **Reviewer** | Review PRs in specific components, provide feedback | Consistent quality contributions, nominated by maintainer |
| **Maintainer** | Merge PRs, triage issues, guide direction | Demonstrated expertise, nominated by BDFL/committee |

## Decision Process

1. **Minor decisions** (bug fixes, small features): Maintainer approval via PR review
2. **Significant decisions** (new features, API changes): Discussion in GitHub Discussions, maintainer consensus
3. **Architectural decisions**: Documented as ADR in `.s2s/decisions/`, requires BDFL approval (Phase 1) or maintainer consensus (post-transition)

## Code Ownership

Component ownership is defined in [CODEOWNERS](.github/CODEOWNERS). Component maintainers:
- Are automatically assigned as reviewers for PRs in their area
- Own the documentation for their component
- Guide technical direction within their component

## Community Channels

| Channel | Purpose |
|---------|---------|
| **GitHub Discussions** | Q&A, ideas, general discussion, announcements |
| **GitHub Issues** | Bug reports, feature requests, task tracking |
| **Pull Requests** | Code review, contribution workflow |

Discord/Slack channels may be added when the community grows beyond GitHub Discussions capacity.

## Community Readiness Checklist

Before public announcement, the project will ensure:
- [ ] 5-10 "good first issue" items available
- [ ] Single-command dev setup verified on clean machine
- [ ] Full contribution workflow tested end-to-end
- [ ] Documentation reviewed for completeness
- [x] Code of Conduct in place

## Conflict Resolution

1. Technical disputes: Resolved by maintainer discussion, escalate to BDFL if no consensus
2. Code of Conduct violations: Handled by maintainers per Code of Conduct
3. Governance disputes: BDFL decision (Phase 1), maintainer vote (post-transition)

---

*This governance model is derived from roundtable session 20260128-roundtable-vektra (REQ-014, REQ-015, REQ-016, REQ-017, REQ-018, REQ-021).*
