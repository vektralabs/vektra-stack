# vektra-app

FastAPI application assembly and startup validation for the Vektra platform.

This package wires all component modules (vektra-shared, vektra-core, vektra-ingest,
vektra-index, vektra-analytics, vektra-learn, vektra-admin) into a single FastAPI
application with the 11-step startup validation sequence (ARCH-057). It also mounts
the chatbot widget bundle at `/static/learn/vektra-chat.js` and the admin static
assets at `/admin/static`.

## Usage

Production / container: run the module entrypoint. It runs the ARCH-057
validation before starting uvicorn, so a misconfiguration exits with a
structured `[STARTUP ERROR]` and no traceback (BUG-025, NFR-009).

```bash
python -m vektra_app.main
```

Local development with live reload (validation runs inside the ASGI lifespan):

```bash
uvicorn vektra_app.main:app --reload --host 0.0.0.0 --port 8000
```
