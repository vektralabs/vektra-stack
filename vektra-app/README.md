# vektra-app

FastAPI application assembly and startup validation for the Vektra platform.

This package wires all component modules (vektra-admin, vektra-core, vektra-ingest,
vektra-index) into a single FastAPI application with the 8-step startup validation
sequence (ARCH-057).

## Usage

```bash
uvicorn vektra_app.main:app --host 0.0.0.0 --port 8000
```
