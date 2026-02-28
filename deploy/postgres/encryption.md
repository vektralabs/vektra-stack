# PostgreSQL encryption at rest (NFR-013)

Vektra uses two layers of encryption at rest:

1. **pgcrypto functions** for conversation content (ARCH-031, ADR-0011)
2. **Storage-level encryption** for the entire database volume

## pgcrypto (application layer)

The `pgcrypto` extension is installed by the init script and Alembic migration.
Conversation encryption is activated by setting `VEKTRA_CONVERSATION_KEY` (Phase 2).

## Storage-level encryption

PostgreSQL does not include built-in TDE. Choose one of these approaches
depending on your deployment environment.

### Option A: LUKS volume encryption (Linux)

Encrypt the Docker volume's backing storage:

```bash
# Create an encrypted partition
cryptsetup luksFormat /dev/sdX
cryptsetup open /dev/sdX vektra-data
mkfs.ext4 /dev/mapper/vektra-data
# Create mount point (Docker creates this on first volume use, but mount needs it upfront)
mkdir -p /var/lib/docker/volumes/<COMPOSE_PROJECT_NAME>_vektra_pgdata/_data
# Adjust project name if COMPOSE_PROJECT_NAME differs from directory name
mount /dev/mapper/vektra-data /var/lib/docker/volumes/<COMPOSE_PROJECT_NAME>_vektra_pgdata/_data

# Docker will write postgres data to the encrypted volume
docker compose up -d
```

### Option B: Docker volume on encrypted block device

Mount a pre-encrypted block device (e.g. LUKS) as a Docker volume:

```yaml
volumes:
  vektra_pgdata:
    driver: local
    driver_opts:
      type: ext4
      o: defaults
      device: /dev/mapper/vektra-data
```

### Option C: Cloud-managed encryption

Most cloud providers encrypt block storage by default:

- **AWS EBS**: Encryption enabled at volume creation (AES-256)
- **GCP Persistent Disk**: Encrypted by default (AES-256)
- **Azure Managed Disk**: SSE with platform-managed keys by default

No additional configuration needed when using cloud-managed PostgreSQL
or encrypted block storage.

## Verification checklist

- [ ] `pgcrypto` extension is installed: `SELECT 1 FROM pg_extension WHERE extname = 'pgcrypto';`
- [ ] Storage encryption is active (varies by method):
  - LUKS: `cryptsetup status vektra-data` shows "active"
  - Cloud: check volume encryption status in provider console
- [ ] Backups are also encrypted (pg_dump output stored on encrypted volume or encrypted at rest in backup storage)
- [ ] `VEKTRA_CONVERSATION_KEY` is set when conversation encryption is required (Phase 2)
- [ ] Key rotation procedure is documented for your deployment
