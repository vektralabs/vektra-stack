-- PostgreSQL extensions required by Vektra.
-- Mounted as /docker-entrypoint-initdb.d/01-extensions.sql
-- and executed automatically on first database creation.
--
-- The pgvector/pgvector:pg16 image ships the compiled extension;
-- this script creates it in the vektra database.
-- The initial Alembic migration (0001) also includes these statements
-- as a safety net.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- No assess role here (ADR-0033 D7): a deployment that does not enable assess gets none. Where it
-- is enabled, the assess migration branch creates the three NOLOGIN roles, and the one-shot
-- vektra-assess-db-init service (deploy/postgres/assess-roles.sql) sets LOGIN and the passwords.
