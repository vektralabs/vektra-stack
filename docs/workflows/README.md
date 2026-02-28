# n8n workflow templates

Reference workflows for automating Vektra operations with [n8n](https://n8n.io).

## Periodic document ingestion

**File**: [`periodic-ingest.json`](periodic-ingest.json)

Automatically ingests new documents on a schedule. The workflow:

1. Runs on a daily schedule (configurable)
2. Lists files from a source directory or API
3. Computes SHA-256 hash, checks workflow static data, and optimistically stores new hashes before ingestion
4. Uploads new files via `POST /api/v1/ingest`
5. Polls async job status until completion (with a 5-minute timeout)
6. Produces a summary of ingested, skipped, and timed-out files

### Import into n8n

1. Open your n8n instance
2. Go to **Workflows** > **Import from file**
3. Select `periodic-ingest.json`
4. Configure the credential and environment variables (see below)

### Required credential

Create an **HTTP Header Auth** credential in n8n:

- **Name**: `Header Auth account` (or any name you prefer)
- **Header Name**: `Authorization`
- **Header Value**: `Bearer <your-vektra-api-key>`

After importing, n8n will prompt you to assign the credential to the HTTP Request nodes (`POST /ingest` and `Poll Job Status`).

### Environment variables

Set these in your n8n instance (Settings > Variables) or via environment:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `VEKTRA_BASE_URL` | yes | - | Vektra API base URL (e.g., `http://vektra:8000`) |
| `VEKTRA_NAMESPACE` | no | `default` | Target namespace for ingested documents |

### Customization

**File source**: The "List Source Files" node reads PDFs from `/files`. Replace it with your actual source:

- **Local directory**: use the "Read/Write Files from Disk" node
- **S3 bucket**: use the "AWS S3" node
- **Google Drive**: use the "Google Drive" node
- **HTTP API**: use the "HTTP Request" node to fetch a file listing

**Schedule**: Edit the "Daily Schedule" trigger node to change the interval (e.g., every 6 hours, weekly).

**Binary key**: The `POST /ingest` node expects binary data under the key `"data"` (the default for `readWriteFile`). If you replace the source node with S3, Google Drive, or another connector, ensure its binary output key matches `"data"`, or add a Set node before `POST /ingest` to rename it.

**Notifications**: Connect the "Ingestion Summary" node to a notification service (Slack, email, webhook) to receive alerts on ingestion results.

### Workflow diagram

```text
Schedule Trigger
    |
List Source Files
    |
Hash & Dedup Check (SHA-256 + optimistic store)
    |
Is Skipped?
    |           \
    | (no)       (yes) -> Ingestion Summary (skip)
    |
POST /ingest
    |
Is Async Job? (HTTP 202?)
    |           \
    | (yes)      (no, HTTP 200) -> Ingestion Summary
    |
Wait 5s -> Poll Job Status -> Track Poll Count -> Still Processing?
                                                    |           \
                                                    | (yes)      (no) -> Ingestion Summary
                                                    |
                                                (loop back to Wait 5s)
```

Hashes are stored **optimistically** in n8n's [workflow static data](https://docs.n8n.io/code/cookbook/builtin/get-workflow-static-data/) before the HTTP request is made. This ensures dedup works even though the HTTP Request node (`fullResponse: true`) replaces item data downstream. Tradeoff: if ingestion fails, the file is skipped on subsequent runs. Clear `staticData.processedHashes` in the workflow editor to force re-processing.

Static data survives across executions but is lost if the workflow is deleted and re-imported. For durable deduplication, replace the static data lookup with an external store (database, Redis, or a dedicated Vektra endpoint).

The async job polling loop is limited to 60 iterations (5 minutes at 5-second intervals). After timeout, the item is routed to "Ingestion Summary" with status `timeout`. Adjust `MAX_POLLS` in the "Track Poll Count" node to change this limit.
