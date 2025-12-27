# Contributing to Vektra

Thank you for your interest in contributing to Vektra. This document provides guidelines for contributing to any repository within the Vektra ecosystem.

## Before You Start

1. **Read the documentation** — Familiarize yourself with [README.md](./README.md) and [ARCHITECTURE.md](./ARCHITECTURE.md)
2. **Check existing issues** — Your idea may already be under discussion
3. **Understand the scope** — Each repository has specific responsibilities; ensure your contribution is in the right place

## Ways to Contribute

### Reporting Issues

- Use the issue tracker of the relevant repository
- Search existing issues before creating a new one
- Provide clear, minimal reproduction steps for bugs
- For security issues, contact maintainers directly (do not open public issues)

### Suggesting Features

- Open an issue with the `enhancement` label
- Explain the use case, not just the solution
- Be prepared to discuss trade-offs

### Contributing Code

1. **Fork the repository**
2. **Create a feature branch** from `main`
   ```
   git checkout -b feature/your-feature-name
   ```
3. **Make your changes**
4. **Write or update tests** as appropriate
5. **Ensure all tests pass**
6. **Submit a pull request**

### Documentation

Documentation improvements are always welcome:

- Fix typos or unclear explanations
- Add examples
- Improve API documentation

## Code Standards

### General

- Write clear, readable code
- Follow the existing style of the codebase
- Keep changes focused; one PR per feature/fix
- Write meaningful commit messages

### Commit Messages

Use clear, descriptive commit messages:

```
Add vector store abstraction for Qdrant

- Implement QdrantAdapter class
- Add configuration options for connection
- Include integration tests
```

Avoid:
- `fix stuff`
- `WIP`
- `updates`

### Pull Request Guidelines

- Provide a clear description of what the PR does
- Reference related issues
- Keep PRs reasonably sized (prefer smaller, focused PRs)
- Respond to review feedback promptly

## Repository-Specific Guidelines

Each repository may have additional guidelines. Check the `CONTRIBUTING.md` in the specific repository you're contributing to.

## Code of Conduct

- Be respectful and constructive
- Focus on the work, not the person
- Assume good intent
- Welcome newcomers

## Questions?

- Open an issue for project-related questions
- Use discussions for broader topics

---

We appreciate your contributions to making Vektra better.
