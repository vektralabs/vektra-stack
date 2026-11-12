-- Assess role provisioning, run by the one-shot vektra-assess-db-init service
-- (docker-compose.assess.yml, plan assess-foundations 7.1) as the superuser.
--
-- Idempotent: creates the roles if the assess migration branch has not yet, then
-- (re)sets LOGIN and the passwords from the secret files. Re-run it to rotate a
-- password. The passwords are read by psql from /run/secrets and never appear in a
-- migration, in .env or on a command line; postgres runs with log_statement=none.
-- Grants are not set here (plan assess-data-crypto owns the grant matrix).
\set ON_ERROR_STOP on
\set QUIET on
\set api_pw `cat /run/secrets/assess_db_api_password`
\set worker_pw `cat /run/secrets/assess_db_worker_password`

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vektra_assess_api') THEN
        CREATE ROLE vektra_assess_api NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vektra_assess_worker') THEN
        CREATE ROLE vektra_assess_worker NOLOGIN;
    END IF;
END
$$;

ALTER ROLE vektra_assess_api WITH LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS NOINHERIT
    PASSWORD :'api_pw';
ALTER ROLE vektra_assess_worker WITH LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS NOINHERIT
    PASSWORD :'worker_pw';

\unset api_pw
\unset worker_pw
\echo 'vektra-assess-db-init: roles vektra_assess_api and vektra_assess_worker ready'
