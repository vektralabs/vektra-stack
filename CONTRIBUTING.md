# Contributing to Vektra

Thank you for your interest in contributing to Vektra! This document explains how to contribute effectively.

## Quick Start

```bash
# Clone the repository
git clone https://github.com/vektralabs/vektra-stack.git
cd vektra-stack

# Set up development environment (single command)
make dev-setup   # or: ./scripts/dev-setup.sh

# Run tests
make test
```

## Developer Certificate of Origin (DCO)

All contributions must be signed off with a DCO. This certifies that you have the right to submit the code under the project's license.

Add the `-s` flag to your commits:

```bash
git commit -s -m "feat(core): add streaming response support"
```

This adds a `Signed-off-by` line to your commit message:

```
feat(core): add streaming response support

Signed-off-by: Your Name <your.email@example.com>
```

## Commit Message Format

We use [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <subject>

[optional body]

[optional footer]
Signed-off-by: Your Name <your.email@example.com>
```

**Types**: `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`

**Scopes**: `core`, `ingest`, `index`, `analytics`, `learn`, `admin`, `docs`

**Examples**:
- `feat(ingest): add PDF text extraction`
- `fix(core): handle empty conversation history`
- `docs(readme): update installation instructions`

## PR Workflow

### Single-component changes

1. Create a branch: `git checkout -b feat/core-streaming`
2. Make changes in one component
3. Add tests for your changes
4. Update documentation if needed
5. Open PR against `develop`

### Cross-component changes

1. Create a branch from `develop`
2. Make changes across components in a single PR
3. Ensure all affected components have passing tests
4. Document the cross-component impact in PR description

### Documentation-only changes

1. Changes to `docs/` or `README.md` files
2. No code changes required
3. Mark PR with `docs` label

## PR Checklist

Before submitting a PR, ensure:

- [ ] Code follows project style (run `make lint`)
- [ ] Tests pass (`make test`)
- [ ] Documentation updated if adding/changing features
- [ ] Commit messages follow Conventional Commits
- [ ] All commits are signed off (DCO)
- [ ] PR description explains the change and motivation

## Documentation Standards

When contributing documentation:

1. **Follow Diataxis**: Identify if your doc is a tutorial, how-to guide, reference, or explanation
2. **Audience awareness**: Know which audience you're writing for (integrators, e-learning users, contributors)
3. **Location**:
   - Cross-cutting docs: `docs/`
   - Component-specific: component's `README.md`
   - Architecture decisions: `.s2s/decisions/ADR-*.md`

## Getting Help

- **Questions**: Open a GitHub Discussion
- **Bugs**: Open a GitHub Issue with reproduction steps
- **Ideas**: Open a GitHub Discussion in the Ideas category

## Code of Conduct

By participating in this project, you agree to abide by our Code of Conduct (see CODE_OF_CONDUCT.md).

---

*Contribution guidelines derived from roundtable session 20260128-roundtable-vektra (REQ-016, REQ-011).*
