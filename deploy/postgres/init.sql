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

-- Assess roles (plan assess-foundations 7.1), NOLOGIN. Fresh volumes get them here;
-- migration 0009 creates them on existing volumes, where this script never re-runs.
-- LOGIN and passwords are set by the one-shot vektra-assess-db-init service
-- (deploy/postgres/assess-roles.sql) from secret files, never here.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vektra_assess_api') THEN
        CREATE ROLE vektra_assess_api NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS NOINHERIT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vektra_assess_worker') THEN
        CREATE ROLE vektra_assess_worker NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS NOINHERIT;
    END IF;
END
$$;
