# n8n workflow templates

Reference workflows for automating Vektra operations with [n8n](https://n8n.io).

## Periodic document ingestion

**File**: [`periodic-ingest.json`](periodic-ingest.json)

Automatically ingests new documents on a schedule. The workflow:

1. Runs on a daily schedule (configurable)
2. Lists files from a source directory or API
3. Computes SHA-256 hash for each file
4. Skips files already indexed (hash comparison)
5. Uploads new files via `POST /api/v1/ingest`
6. Polls async job status until completion
7. Produces a summary of ingested and skipped files

### Import into n8n

1. Open your n8n instance
2. Go to **Workflows** > **Import from file**
3. Select `periodic-ingest.json`
4. Configure the credential and environment variables (see below)

### Required credential

Create an **HTTP Header Auth** credential in n8n:

- **Name**: `Vektra API Key`
- **Header Name**: `Authorization`
- **Header Value**: `Bearer <your-vektra-api-key>`

The credential ID `vektra-api-key` is referenced by all HTTP Request nodes in the workflow. After importing, n8n will prompt you to map it to your actual credential.

### Environment variables

Set these in your n8n instance (Settings > Variables) or via environment:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `VEKTRA_BASE_URL` | yes | - | Vektra API base URL (e.g., `http://vektra:8000`) |
| `VEKTRA_NAMESPACE` | no | `default` | Target namespace for ingested documents |

### Customization

**File source**: The "List Source Files" node is a placeholder. Replace it with your actual source:

- **Local directory**: use the "Read/Write Files from Disk" node
- **S3 bucket**: use the "AWS S3" node
- **Google Drive**: use the "Google Drive" node
- **HTTP API**: use the "HTTP Request" node to fetch a file listing

**Schedule**: Edit the "Daily Schedule" trigger node to change the interval (e.g., every 6 hours, weekly).

**Notifications**: Connect the "Success Summary" and "Failure Summary" nodes to a notification service (Slack, email, webhook) to receive alerts on ingestion results.

### Workflow diagram

```
Schedule Trigger
    |
List Source Files
    |
Compute SHA-256 Hash
    |
Check Existing Documents (GET /stats)
    |
Is New Document?
    |           \
    | (yes)      (no) -> skip
    |
POST /ingest
    |
Is Async Job? (HTTP 202?)
    |           \
    | (yes)      (no, HTTP 200) -> Success Summary
    |
Wait 5s -> Poll Job Status -> Still Processing?
                                |           \
                                | (yes)      (no) -> Success Summary
                                |
                            (loop back to Wait 5s)
```
