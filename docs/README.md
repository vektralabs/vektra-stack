# Vektra RAG documentation

## Getting started

- [Quick start](getting-started/index.md) - from clone to first query in under 30 minutes
- [First query tutorial](getting-started/first-query.md) - ingest a PDF, query it, understand the response

## Reference

- [API reference](reference/api.md) - all endpoints with curl examples
- [Configuration](reference/configuration.md) - environment variables with types, defaults and ranges
- [Error codes](reference/error-codes.md) - error response codes and remediation

## Architecture

- [Overview](architecture/index.md) - components, deployment, protocols, startup sequence

## Guides

- [Widget integration](guides/widget-integration.md) - embed the LMS-agnostic chatbot widget in any course page (data-* attributes, JWT flow, conversation persistence, token refresh)
- [Contributors](guides/contributors/index.md) - dev setup, testing, extending providers

### Assessment discussion (operators)

- [Deployment](guides/operators/assess-deployment.md) - the `assess` Compose profile, roles and DSNs, limits, health checks, setup check
- [Secrets and the key matrix](guides/operators/assess-secrets.md) - which process must and must not hold each secret, file formats, generation, startup refusals
- [Framing](guides/operators/assess-framing.md) - the UI origin, the per-site LMS origin, the reverse proxy and how to verify them
- [LMS service account and plugin keys](guides/assess-moodle-service-account.md) - the read-only web-service account, its token, site registration, the four `assess:*` keys
- [Pilot go/no-go](guides/operators/assess-pilot-go-no-go.md) - the pilot criteria, each with its evidence or the reason it is blocked
- [Keys runbook](guides/assess-keys-runbook.md) - key verification, offline backup, restore drill, rotation per key

## Workflows

- [n8n periodic ingestion](workflows/README.md) - reference workflow for automated document sync
