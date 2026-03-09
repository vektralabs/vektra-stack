# vektra-learn

E-learning vertical for the Vektra platform. Provides LMS-agnostic REST APIs for enrollment management, course-scoped RAG queries, and a chatbot widget.

## Components

- **Python backend** (`src/vektra_learn/`): FastAPI router with enrollment, content ingestion, token, and query endpoints
- **Chatbot widget** (`widget/`): Vanilla JS chat interface built with esbuild

## Widget build

```bash
cd widget && npm install && npm run build
```

Output: `static/vektra-chat.js`
