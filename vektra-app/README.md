# vektra-app

FastAPI application assembly and startup validation for the Vektra platform.

This package wires all component modules (vektra-shared, vektra-core, vektra-ingest,
vektra-index, vektra-analytics, vektra-learn, vektra-admin) into a single FastAPI
application with the 11-step startup validation sequence (ARCH-057). It also mounts
the chatbot widget bundle at `/static/learn/vektra-chat.js` and the admin static
assets at `/admin/static`.

## Usage

```bash
uvicorn vektra_app.main:app --host 0.0.0.0 --port 8000
```
