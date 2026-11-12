"""The assess schema, as one base revision of its own branch (ADR-0033 D7, plan 2a-3).

This revision replaces the 36 revisions 0009 to 0044 of the root chain, squashed under OD-1 (no
assess data survives the separation, decided 2026-11-11). It creates their head schema with the
neutral names of ADR-0033 D5: ``assess_sites.lms_site_uuid``, ``assess_sites.lms_origin`` (CHECK
``ck_assess_sites_lms_origin``) and ``assess_setup_check_reports.lms_release``.

It lives on the ``assess`` branch, which depends on the platform's 0009 and is applied only where
assess is enabled: ``docker/entrypoint.sh`` runs ``alembic upgrade platform@head`` always and
``alembic upgrade assess@head`` only with ``VEKTRA_ASSESS_ENABLED=true``. A deployment that never
enables assess gets none of these tables, roles or functions, and needs no CREATEROLE.

Equivalence with the replaced chain is proven, not asserted: the integration test
``vektra-assess/tests/integration/test_schema_branch.py`` dumps this revision's schema and the
effective privileges of the three roles and compares them with the goldens taken from the old
chain at 0044 (``vektra-assess/tests/golden/``), with the D5 renames applied.

What the old chain guaranteed and this revision keeps:

- the four site-bound API key scopes and ``ck_api_keys_assess_scope_exclusive`` (formerly 0009):
  an assess key holds none of ``admin``, ``ingest``, ``query``;
- the three NOLOGIN roles, created here when missing; without CREATEROLE the upgrade stops with
  the managed-Postgres recipe, naming all three roles (formerly 0009 and 0012);
- the reserved ``assess-discussion`` namespace, refused when it already exists (formerly 0019),
  and the worker's RESTRICTIVE insert policy on ``query_traces`` (0020); the
  ``query_traces.pipeline`` column it narrows is the platform's (revision 0009: analytics maps
  and filters on it with assess off too);
- the SECURITY DEFINER functions, owned by ``vektra_assess_definer``: ``ALTER FUNCTION ... OWNER
  TO`` needs the migration user to be able to ``SET ROLE vektra_assess_definer``. A CREATEROLE
  user that creates the role here grants it to itself (PostgreSQL 16 gives the creator ADMIN
  only); a user that cannot SET ROLE to a pre-created one is stopped up front with the recipe
  (:data:`DEFINER_MEMBERSHIP_MESSAGE`);
- every table, CHECK constraint, index and trigger of 0044, and the grant matrix
  (:data:`GRANT_STATEMENTS`, rendered by ``vektra_assess.db.grants.render_statements``).

The DDL below is the old head's ``pg_dump --schema-only`` output, renamed: pg_dump's spelling
(qualified names, explicit sequences) is kept so the comparison is exact.

The downgrade refuses while an API key holds an assess scope or a session row exists (exam
evidence), then drops every assess object and restores the platform tables it touched. The roles
are cluster-level, shared with other databases, and stay.

Revision ID: a0001
Revises: (base of the ``assess`` branch; depends on 0009)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a0001"
down_revision = None
branch_labels = ("assess",)
depends_on = "0009"

ROLE_ATTRS = "NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS NOINHERIT"
ROLES = ("vektra_assess_api", "vektra_assess_worker", "vektra_assess_definer")
DEFINER = "vektra_assess_definer"

#: the message of a migration user without CREATEROLE; it names all three roles
CREATEROLE_MESSAGE = (
    "assess base revision a0001: cannot create role % (the migration user lacks CREATEROLE). "
    "On managed Postgres, pre-create all three assess roles, vektra_assess_api, "
    f"vektra_assess_worker and vektra_assess_definer, as {ROLE_ATTRS}; make the migration "
    "user a member of vektra_assess_definer, so that it can SET ROLE vektra_assess_definer "
    "(the definer functions are owned by it); then re-run the migration."
)

#: the message of a migration user that cannot SET ROLE to the definer it did not create
DEFINER_MEMBERSHIP_MESSAGE = (
    "assess base revision a0001: the migration user cannot SET ROLE vektra_assess_definer, "
    "which owns the SECURITY DEFINER functions (ALTER FUNCTION ... OWNER TO needs it). "
    "An administrator runs GRANT vektra_assess_definer TO <migration user>; "
    "then re-run the migration. Nothing was changed."
)

ASSESS_NAMESPACE = "assess-discussion"

PRE_EXISTING_MESSAGE = (
    f"The assess base revision a0001 reserves the namespace id '{ASSESS_NAMESPACE}', and this "
    "database already has a namespace with that id. It was not changed, and the upgrade was "
    "rolled back: the database keeps the revision it had before this run. "
    "Keeping it would make it read-only for ever: a reserved namespace refuses ingest, "
    "configuration and deletion. Way out, on the release you run now: re-ingest its documents "
    "into a namespace with another name, delete its documents, then delete the row "
    f"(DELETE FROM namespaces WHERE id = '{ASSESS_NAMESPACE}'; it fails while any row still "
    "references it), and start the upgrade again."
)

ASSESS_SCOPES = (
    "'assess:mint','assess:teacher','assess:privacy-export','assess:privacy-erase'"
)
_SEVEN_SCOPES = f"ARRAY['admin','ingest','query',{ASSESS_SCOPES}]::text[]"
_LEGACY_SCOPES = "ARRAY['admin','ingest','query']::text[]"

#: the platform tables the assess schema extends; restored by the downgrade
PLATFORM: tuple[str, ...] = (
    "ALTER TABLE api_keys DROP CONSTRAINT ck_api_keys_scopes",
    f"ALTER TABLE api_keys ADD CONSTRAINT ck_api_keys_scopes CHECK (scopes <@ {_SEVEN_SCOPES})",
    # a key holding any assess:* scope holds none of the legacy scopes
    "ALTER TABLE api_keys ADD CONSTRAINT ck_api_keys_assess_scope_exclusive "
    f"CHECK (NOT (scopes && ARRAY[{ASSESS_SCOPES}]::text[]) "
    f"OR NOT (scopes && {_LEGACY_SCOPES}))",
    "INSERT INTO namespaces (id, display_name, config) "
    f"VALUES ('{ASSESS_NAMESPACE}', 'Assess discussion traces (reserved)', "
    """'{"reserved": true}'::jsonb)""",
)

#: the worker may insert a trace only in the reserved namespace and pipeline (the permissive
#: policy of 0003 accepts any namespace while ``app.current_namespace`` is unset)
POLICY = (
    "CREATE POLICY query_traces_assess_worker_only ON query_traces AS RESTRICTIVE FOR INSERT "
    f"TO vektra_assess_worker WITH CHECK (namespace_id = '{ASSESS_NAMESPACE}' "
    f"AND pipeline = '{ASSESS_NAMESPACE}')"
)

TABLES: tuple[str, ...] = (
    # --- assess_activity_config
    r"""
CREATE TABLE public.assess_activity_config (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    site_id uuid NOT NULL,
    cmid bigint NOT NULL,
    activity_type text NOT NULL,
    enabled boolean DEFAULT false NOT NULL,
    flags jsonb DEFAULT '{}'::jsonb NOT NULL,
    rag_enabled boolean DEFAULT false NOT NULL,
    end_at timestamp with time zone,
    attested_opening_at timestamp with time zone,
    attested_by text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    cap_minutes integer,
    version integer DEFAULT 0 NOT NULL,
    updated_by bigint,
    CONSTRAINT ck_assess_activity_config_cap CHECK (((cap_minutes IS NULL) OR ((cap_minutes >= 5) AND (cap_minutes <= 120)))),
    CONSTRAINT ck_assess_activity_config_rag_off CHECK ((rag_enabled = false)),
    CONSTRAINT ck_assess_activity_config_type CHECK ((activity_type = ANY (ARRAY['quiz'::text, 'assign'::text]))),
    CONSTRAINT ck_assess_activity_config_version CHECK ((version >= 0))
)
    """,
    r"""
ALTER TABLE ONLY public.assess_activity_config
    ADD CONSTRAINT assess_activity_config_pkey PRIMARY KEY (id)
    """,
    r"""
ALTER TABLE ONLY public.assess_activity_config
    ADD CONSTRAINT assess_activity_config_site_id_cmid_key UNIQUE (site_id, cmid)
    """,
    # --- assess_activity_config_sealed
    r"""
CREATE TABLE public.assess_activity_config_sealed (
    site_id uuid NOT NULL,
    cmid bigint NOT NULL,
    field text NOT NULL,
    sealed_blob bytea,
    kid text,
    is_set boolean GENERATED ALWAYS AS ((sealed_blob IS NOT NULL)) STORED NOT NULL,
    refs text[],
    item_count integer,
    updated_by bigint,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_assess_activity_config_sealed_field CHECK ((field = ANY (ARRAY['solution'::text, 'pasted_rubric'::text, 'transcript_recipients'::text]))),
    CONSTRAINT ck_assess_activity_config_sealed_kid CHECK (((sealed_blob IS NULL) = (kid IS NULL))),
    CONSTRAINT ck_assess_activity_config_sealed_refs CHECK ((((refs IS NULL) AND (item_count IS NULL)) OR ((field = 'transcript_recipients'::text) AND (sealed_blob IS NOT NULL) AND ((item_count >= 1) AND (item_count <= 3)) AND (cardinality(refs) = item_count))))
)
    """,
    r"""
ALTER TABLE ONLY public.assess_activity_config_sealed
    ADD CONSTRAINT assess_activity_config_sealed_pkey PRIMARY KEY (site_id, cmid, field)
    """,
    # --- assess_audit_events
    r"""
CREATE TABLE public.assess_audit_events (
    id bigint NOT NULL,
    site_id uuid,
    session_id uuid,
    event text NOT NULL,
    reason_code text,
    actor_kind text NOT NULL,
    data jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_assess_audit_events_data_small CHECK ((pg_column_size(data) < 2048))
)
    """,
    r"""
CREATE SEQUENCE public.assess_audit_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
    """,
    r"""
ALTER SEQUENCE public.assess_audit_events_id_seq OWNED BY public.assess_audit_events.id
    """,
    r"""
ALTER TABLE ONLY public.assess_audit_events ALTER COLUMN id SET DEFAULT nextval('public.assess_audit_events_id_seq'::regclass)
    """,
    r"""
ALTER TABLE ONLY public.assess_audit_events
    ADD CONSTRAINT assess_audit_events_pkey PRIMARY KEY (id)
    """,
    # --- assess_consumed_jtis
    r"""
CREATE TABLE public.assess_consumed_jtis (
    jti_hash text NOT NULL,
    aud text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone DEFAULT now() NOT NULL
)
    """,
    r"""
ALTER TABLE ONLY public.assess_consumed_jtis
    ADD CONSTRAINT assess_consumed_jtis_pkey PRIMARY KEY (jti_hash)
    """,
    # --- assess_deliveries
    r"""
CREATE TABLE public.assess_deliveries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    transcript_id uuid,
    kind text DEFAULT 'transcript'::text NOT NULL,
    round integer DEFAULT 1 NOT NULL,
    recipient_kind text NOT NULL,
    recipient_userid bigint,
    status text DEFAULT 'PENDING'::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    not_before timestamp with time zone DEFAULT now() NOT NULL,
    attempt_started_at timestamp with time zone,
    last_error_class text,
    smtp_code integer,
    message_id text,
    sent_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    recipient_ref text,
    job_id bigint,
    CONSTRAINT ck_assess_deliveries_attempts CHECK ((attempts >= 0)),
    CONSTRAINT ck_assess_deliveries_job CHECK ((((kind = ANY (ARRAY['teacher_notify'::text, 'operator_notify'::text])) = (job_id IS NOT NULL)) AND ((NOT (kind = ANY (ARRAY['teacher_notify'::text, 'operator_notify'::text]))) OR (recipient_kind = 'address'::text)))),
    CONSTRAINT ck_assess_deliveries_kind CHECK ((kind = ANY (ARRAY['transcript'::text, 'reset_notice'::text, 'teacher_notify'::text, 'operator_notify'::text, 'smtp_test'::text]))),
    CONSTRAINT ck_assess_deliveries_recipient CHECK ((((recipient_kind = 'address'::text) = (recipient_ref IS NOT NULL)) AND ((recipient_kind = 'address'::text) = (recipient_userid IS NULL)))),
    CONSTRAINT ck_assess_deliveries_recipient_kind CHECK ((recipient_kind = ANY (ARRAY['grader'::text, 'student'::text, 'address'::text]))),
    CONSTRAINT ck_assess_deliveries_ref CHECK (((recipient_ref IS NULL) OR (recipient_ref ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_assess_deliveries_round CHECK ((round >= 1)),
    CONSTRAINT ck_assess_deliveries_sent CHECK (((status = 'SENT'::text) = (sent_at IS NOT NULL))),
    CONSTRAINT ck_assess_deliveries_smtp_test CHECK (((kind <> 'smtp_test'::text) OR (recipient_kind = 'address'::text))),
    CONSTRAINT ck_assess_deliveries_status CHECK ((status = ANY (ARRAY['PENDING'::text, 'RETRYING'::text, 'SENT'::text, 'FAILED'::text, 'ABANDONED'::text, 'REPLACED'::text]))),
    CONSTRAINT ck_assess_deliveries_transcript CHECK (((kind = 'transcript'::text) = (transcript_id IS NOT NULL)))
)
    """,
    r"""
ALTER TABLE ONLY public.assess_deliveries
    ADD CONSTRAINT assess_deliveries_pkey PRIMARY KEY (id)
    """,
    r"""
ALTER TABLE ONLY public.assess_deliveries
    ADD CONSTRAINT uq_assess_deliveries_round UNIQUE (transcript_id, round, recipient_userid)
    """,
    r"""
ALTER TABLE ONLY public.assess_deliveries
    ADD CONSTRAINT uq_assess_deliveries_round_ref UNIQUE (transcript_id, round, recipient_ref)
    """,
    r"""
CREATE INDEX ix_assess_deliveries_due ON public.assess_deliveries USING btree (status, not_before)
    """,
    r"""
CREATE UNIQUE INDEX uq_assess_deliveries_job_ref ON public.assess_deliveries USING btree (job_id, recipient_ref) WHERE (job_id IS NOT NULL)
    """,
    # --- assess_enrolment_checks
    r"""
CREATE TABLE public.assess_enrolment_checks (
    id bigint NOT NULL,
    session_id uuid NOT NULL,
    checked_at timestamp with time zone DEFAULT now() NOT NULL,
    result text NOT NULL,
    CONSTRAINT ck_assess_enrolment_checks_result CHECK ((result = ANY (ARRAY['enrolled'::text, 'not_enrolled'::text, 'unreadable'::text])))
)
    """,
    r"""
CREATE SEQUENCE public.assess_enrolment_checks_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
    """,
    r"""
ALTER SEQUENCE public.assess_enrolment_checks_id_seq OWNED BY public.assess_enrolment_checks.id
    """,
    r"""
ALTER TABLE ONLY public.assess_enrolment_checks ALTER COLUMN id SET DEFAULT nextval('public.assess_enrolment_checks_id_seq'::regclass)
    """,
    r"""
ALTER TABLE ONLY public.assess_enrolment_checks
    ADD CONSTRAINT assess_enrolment_checks_pkey PRIMARY KEY (id)
    """,
    # --- assess_erasure_records
    r"""
CREATE TABLE public.assess_erasure_records (
    id bigint NOT NULL,
    site_id uuid NOT NULL,
    subject_userid_hash text,
    requested_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    rows_deleted jsonb DEFAULT '{}'::jsonb NOT NULL,
    scope text NOT NULL,
    cmid bigint,
    subject_userids bigint[],
    outcome text,
    error_code text,
    CONSTRAINT ck_assess_erasure_records_cleared CHECK (((completed_at IS NULL) OR ((subject_userids IS NULL) AND (subject_userid_hash IS NULL)))),
    CONSTRAINT ck_assess_erasure_records_outcome CHECK ((outcome = ANY (ARRAY['erased'::text, 'failed'::text]))),
    CONSTRAINT ck_assess_erasure_records_scope CHECK ((scope = ANY (ARRAY['subject'::text, 'activity'::text]))),
    CONSTRAINT ck_assess_erasure_records_shape CHECK ((((scope = 'subject'::text) AND ((subject_userid_hash IS NOT NULL) OR (completed_at IS NOT NULL))) OR ((scope = 'activity'::text) AND (cmid IS NOT NULL))))
)
    """,
    r"""
CREATE SEQUENCE public.assess_erasure_records_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
    """,
    r"""
ALTER SEQUENCE public.assess_erasure_records_id_seq OWNED BY public.assess_erasure_records.id
    """,
    r"""
ALTER TABLE ONLY public.assess_erasure_records ALTER COLUMN id SET DEFAULT nextval('public.assess_erasure_records_id_seq'::regclass)
    """,
    r"""
ALTER TABLE ONLY public.assess_erasure_records
    ADD CONSTRAINT assess_erasure_records_pkey PRIMARY KEY (id)
    """,
    # --- assess_extra_time
    r"""
CREATE TABLE public.assess_extra_time (
    site_id uuid NOT NULL,
    cmid bigint NOT NULL,
    student_userid bigint NOT NULL,
    extra_seconds integer NOT NULL,
    granted_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_assess_extra_time_nonnegative CHECK ((extra_seconds >= 0))
)
    """,
    r"""
ALTER TABLE ONLY public.assess_extra_time
    ADD CONSTRAINT assess_extra_time_pkey PRIMARY KEY (site_id, cmid, student_userid)
    """,
    # --- assess_gate_inputs
    r"""
CREATE TABLE public.assess_gate_inputs (
    site_id uuid NOT NULL,
    student_userid bigint NOT NULL,
    cmid bigint NOT NULL,
    inputs jsonb DEFAULT '{}'::jsonb NOT NULL,
    refreshed_at timestamp with time zone DEFAULT now() NOT NULL
)
    """,
    r"""
ALTER TABLE ONLY public.assess_gate_inputs
    ADD CONSTRAINT assess_gate_inputs_pkey PRIMARY KEY (site_id, student_userid, cmid)
    """,
    # --- assess_heartbeat
    r"""
CREATE TABLE public.assess_heartbeat (
    component text NOT NULL,
    beat_at timestamp with time zone DEFAULT now() NOT NULL,
    details jsonb DEFAULT '{}'::jsonb NOT NULL
)
    """,
    r"""
ALTER TABLE ONLY public.assess_heartbeat
    ADD CONSTRAINT assess_heartbeat_pkey PRIMARY KEY (component)
    """,
    # --- assess_jobs
    r"""
CREATE TABLE public.assess_jobs (
    id bigint NOT NULL,
    kind text NOT NULL,
    args jsonb DEFAULT '{}'::jsonb NOT NULL,
    dedupe_key text,
    state text DEFAULT 'ready'::text NOT NULL,
    not_before timestamp with time zone DEFAULT now() NOT NULL,
    lease_until timestamp with time zone,
    leased_by text,
    attempts integer DEFAULT 0 NOT NULL,
    max_attempts integer DEFAULT 5 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    done_at timestamp with time zone,
    last_error_code text,
    CONSTRAINT ck_assess_jobs_args_small CHECK ((pg_column_size(args) < 1024)),
    CONSTRAINT ck_assess_jobs_kind CHECK ((kind = ANY (ARRAY['setup_check'::text, 'gate_refresh'::text, 'render_check'::text, 'transcript_generate'::text, 'transcript_deliver'::text, 'enrolment_recheck'::text, 'purge'::text, 'erasure'::text, 'preflight'::text, 'reconcile_reply'::text, 'teacher_notify'::text, 'operator_notify'::text, 'teacher_gate'::text, 'smtp_test_send'::text]))),
    CONSTRAINT ck_assess_jobs_state CHECK ((state = ANY (ARRAY['ready'::text, 'leased'::text, 'done'::text, 'failed'::text, 'dead'::text])))
)
WITH (autovacuum_vacuum_cost_delay='0', fillfactor='70', autovacuum_vacuum_scale_factor='0.01', autovacuum_vacuum_threshold='200', autovacuum_analyze_scale_factor='0.02')
    """,
    r"""
CREATE SEQUENCE public.assess_jobs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
    """,
    r"""
ALTER SEQUENCE public.assess_jobs_id_seq OWNED BY public.assess_jobs.id
    """,
    r"""
ALTER TABLE ONLY public.assess_jobs ALTER COLUMN id SET DEFAULT nextval('public.assess_jobs_id_seq'::regclass)
    """,
    r"""
ALTER TABLE ONLY public.assess_jobs
    ADD CONSTRAINT assess_jobs_pkey PRIMARY KEY (id)
    """,
    r"""
CREATE INDEX ix_assess_jobs_kind_claim ON public.assess_jobs USING btree (kind, not_before) WHERE (state = ANY (ARRAY['ready'::text, 'leased'::text]))
    """,
    r"""
CREATE INDEX ix_assess_jobs_root ON public.assess_jobs USING btree (((args ->> 'root_job_id'::text))) WHERE ((args ->> 'root_job_id'::text) IS NOT NULL)
    """,
    r"""
CREATE UNIQUE INDEX uq_assess_jobs_dedupe ON public.assess_jobs USING btree (dedupe_key) WHERE (state = ANY (ARRAY['ready'::text, 'leased'::text]))
    """,
    # --- assess_model_activations
    r"""
CREATE TABLE public.assess_model_activations (
    id bigint NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    prompt_version character varying(8) NOT NULL,
    report_sha256 character(64) NOT NULL,
    hard_pass boolean NOT NULL,
    activated_at timestamp with time zone DEFAULT now() NOT NULL,
    activated_by_key_id uuid,
    CONSTRAINT ck_assess_model_activations_pv CHECK (((prompt_version)::text ~ '^[0-9a-f]{8}$'::text)),
    CONSTRAINT ck_assess_model_activations_sha CHECK ((report_sha256 ~ '^[0-9a-f]{64}$'::text))
)
    """,
    r"""
CREATE SEQUENCE public.assess_model_activations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
    """,
    r"""
ALTER SEQUENCE public.assess_model_activations_id_seq OWNED BY public.assess_model_activations.id
    """,
    r"""
ALTER TABLE ONLY public.assess_model_activations ALTER COLUMN id SET DEFAULT nextval('public.assess_model_activations_id_seq'::regclass)
    """,
    r"""
ALTER TABLE ONLY public.assess_model_activations
    ADD CONSTRAINT assess_model_activations_pkey PRIMARY KEY (id)
    """,
    r"""
CREATE INDEX ix_assess_model_activations_triple ON public.assess_model_activations USING btree (model, prompt_version) WHERE hard_pass
    """,
    # --- assess_outage_incidents
    r"""
CREATE TABLE public.assess_outage_incidents (
    id bigint NOT NULL,
    session_id uuid NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    ended_at timestamp with time zone,
    reason_code text,
    credited_s integer DEFAULT 0 NOT NULL,
    CONSTRAINT ck_assess_outage_incidents_credited CHECK ((credited_s >= 0))
)
    """,
    r"""
CREATE SEQUENCE public.assess_outage_incidents_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
    """,
    r"""
ALTER SEQUENCE public.assess_outage_incidents_id_seq OWNED BY public.assess_outage_incidents.id
    """,
    r"""
ALTER TABLE ONLY public.assess_outage_incidents ALTER COLUMN id SET DEFAULT nextval('public.assess_outage_incidents_id_seq'::regclass)
    """,
    r"""
ALTER TABLE ONLY public.assess_outage_incidents
    ADD CONSTRAINT assess_outage_incidents_pkey PRIMARY KEY (id)
    """,
    # --- assess_privacy_config
    r"""
CREATE TABLE public.assess_privacy_config (
    site_id uuid NOT NULL,
    retention_days integer NOT NULL,
    legal_basis_label text NOT NULL,
    legal_basis_text text NOT NULL,
    notice_url text NOT NULL,
    notice_text text NOT NULL,
    notice_version text NOT NULL,
    updated_by text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_assess_privacy_basis CHECK (((length(legal_basis_text) >= 1) AND (length(legal_basis_text) <= 4000))),
    CONSTRAINT ck_assess_privacy_label CHECK (((length(legal_basis_label) >= 1) AND (length(legal_basis_label) <= 200))),
    CONSTRAINT ck_assess_privacy_notice CHECK (((length(notice_text) >= 1) AND (length(notice_text) <= 8000))),
    CONSTRAINT ck_assess_privacy_retention CHECK (((retention_days >= 1) AND (retention_days <= 3650))),
    CONSTRAINT ck_assess_privacy_url CHECK (length(notice_url) BETWEEN 9 AND 2000 AND notice_url LIKE 'https://%'),
    CONSTRAINT ck_assess_privacy_version CHECK (((length(notice_version) >= 1) AND (length(notice_version) <= 40)))
)
    """,
    r"""
ALTER TABLE ONLY public.assess_privacy_config
    ADD CONSTRAINT assess_privacy_config_pkey PRIMARY KEY (site_id)
    """,
    # --- assess_purge_records
    r"""
CREATE TABLE public.assess_purge_records (
    id bigint NOT NULL,
    site_id uuid NOT NULL,
    subject_ref text NOT NULL,
    session_id uuid NOT NULL,
    transcript_id uuid,
    session_no integer NOT NULL,
    cmid bigint NOT NULL,
    reason text NOT NULL,
    purged_at timestamp with time zone DEFAULT now() NOT NULL,
    counts jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT ck_assess_purge_records_reason CHECK ((reason = ANY (ARRAY['retention'::text, 'erasure'::text])))
)
    """,
    r"""
CREATE SEQUENCE public.assess_purge_records_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
    """,
    r"""
ALTER SEQUENCE public.assess_purge_records_id_seq OWNED BY public.assess_purge_records.id
    """,
    r"""
ALTER TABLE ONLY public.assess_purge_records ALTER COLUMN id SET DEFAULT nextval('public.assess_purge_records_id_seq'::regclass)
    """,
    r"""
ALTER TABLE ONLY public.assess_purge_records
    ADD CONSTRAINT assess_purge_records_pkey PRIMARY KEY (id)
    """,
    r"""
ALTER TABLE ONLY public.assess_purge_records
    ADD CONSTRAINT uq_assess_purge_records_session UNIQUE (session_id)
    """,
    r"""
CREATE INDEX ix_assess_purge_records_subject ON public.assess_purge_records USING btree (site_id, cmid, subject_ref)
    """,
    r"""
CREATE INDEX ix_assess_purge_records_transcript ON public.assess_purge_records USING btree (transcript_id) WHERE (transcript_id IS NOT NULL)
    """,
    # --- assess_render_checks
    r"""
CREATE TABLE public.assess_render_checks (
    id bigint NOT NULL,
    site_id uuid NOT NULL,
    student_userid bigint NOT NULL,
    cmid bigint NOT NULL,
    requested_at timestamp with time zone DEFAULT now() NOT NULL,
    done_at timestamp with time zone
)
    """,
    r"""
CREATE SEQUENCE public.assess_render_checks_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
    """,
    r"""
ALTER SEQUENCE public.assess_render_checks_id_seq OWNED BY public.assess_render_checks.id
    """,
    r"""
ALTER TABLE ONLY public.assess_render_checks ALTER COLUMN id SET DEFAULT nextval('public.assess_render_checks_id_seq'::regclass)
    """,
    r"""
ALTER TABLE ONLY public.assess_render_checks
    ADD CONSTRAINT assess_render_checks_pkey PRIMARY KEY (id)
    """,
    r"""
CREATE UNIQUE INDEX uq_assess_render_checks_pending ON public.assess_render_checks USING btree (site_id, student_userid, cmid) WHERE (done_at IS NULL)
    """,
    # --- assess_render_state
    r"""
CREATE TABLE public.assess_render_state (
    site_id uuid NOT NULL,
    student_userid bigint NOT NULL,
    cmid bigint NOT NULL,
    status text NOT NULL,
    reason_code text,
    computed_at timestamp with time zone DEFAULT now() NOT NULL
)
    """,
    r"""
ALTER TABLE ONLY public.assess_render_state
    ADD CONSTRAINT assess_render_state_pkey PRIMARY KEY (site_id, student_userid, cmid)
    """,
    # --- assess_reset_grants
    r"""
CREATE TABLE public.assess_reset_grants (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    site_id uuid NOT NULL,
    cmid bigint NOT NULL,
    student_userid bigint NOT NULL,
    granted_by text NOT NULL,
    granted_at timestamp with time zone DEFAULT now() NOT NULL,
    consumed_at timestamp with time zone,
    session_no integer NOT NULL,
    reason_ct bytea NOT NULL,
    reason_kid text NOT NULL,
    include_previous_turns boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_assess_reset_grants_session_no CHECK ((session_no >= 1))
)
    """,
    r"""
ALTER TABLE ONLY public.assess_reset_grants
    ADD CONSTRAINT assess_reset_grants_pkey PRIMARY KEY (id)
    """,
    r"""
ALTER TABLE ONLY public.assess_reset_grants
    ADD CONSTRAINT uq_assess_reset_grants_session UNIQUE (site_id, cmid, student_userid, session_no)
    """,
    # --- assess_session_handoff
    r"""
CREATE TABLE public.assess_session_handoff (
    session_id uuid NOT NULL,
    session_state text NOT NULL,
    epoch integer NOT NULL,
    sealed_dek bytea NOT NULL,
    handoff_kid text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_assess_session_handoff_live CHECK ((session_state = ANY (ARRAY['ACTIVE'::text, 'OUTAGE'::text])))
)
    """,
    r"""
ALTER TABLE ONLY public.assess_session_handoff
    ADD CONSTRAINT assess_session_handoff_pkey PRIMARY KEY (session_id)
    """,
    # --- assess_session_keys
    r"""
CREATE TABLE public.assess_session_keys (
    session_id uuid NOT NULL,
    kek_wrapped bytea,
    kek_kid text NOT NULL,
    key_version integer DEFAULT 1 NOT NULL,
    shredded_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_assess_session_keys_shredded CHECK (((kek_wrapped IS NULL) = (shredded_at IS NOT NULL)))
)
    """,
    r"""
ALTER TABLE ONLY public.assess_session_keys
    ADD CONSTRAINT assess_session_keys_pkey PRIMARY KEY (session_id)
    """,
    # --- assess_sessions
    r"""
CREATE TABLE public.assess_sessions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    site_id uuid NOT NULL,
    cmid bigint NOT NULL,
    student_userid bigint NOT NULL,
    session_no integer NOT NULL,
    attempt_ref text,
    state text NOT NULL,
    close_reason text,
    starting_expires_at timestamp with time zone,
    deadline_at timestamp with time zone,
    credited_outage_s integer DEFAULT 0 NOT NULL,
    seq integer DEFAULT 0 NOT NULL,
    epoch integer DEFAULT 1 NOT NULL,
    notice_version text,
    regrade_flag boolean DEFAULT false NOT NULL,
    committed_at timestamp with time zone,
    closed_at timestamp with time zone,
    last_activity_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    courseid_hint bigint,
    duration_s integer,
    frozen_policy jsonb,
    preflight_started_at timestamp with time zone,
    pending_reply_since timestamp with time zone,
    reply_grace_s integer DEFAULT 60 NOT NULL,
    tokens_used bigint DEFAULT 0 NOT NULL,
    cap_s integer,
    CONSTRAINT ck_assess_sessions_cap_s CHECK (((cap_s IS NULL) OR (cap_s > 0))),
    CONSTRAINT ck_assess_sessions_close_reason CHECK (((close_reason IS NULL) OR (close_reason = ANY (ARRAY['finished'::text, 'timed_out'::text, 'service_failure'::text, 'budget_exhausted'::text, 'terminated_by_teacher'::text, 'access_revoked'::text])))),
    CONSTRAINT ck_assess_sessions_closed_has_reason CHECK (((state = 'CLOSED'::text) = (close_reason IS NOT NULL))),
    CONSTRAINT ck_assess_sessions_credited_outage CHECK ((credited_outage_s >= 0)),
    CONSTRAINT ck_assess_sessions_duration CHECK (((duration_s IS NULL) OR (duration_s > 0))),
    CONSTRAINT ck_assess_sessions_reply_grace CHECK ((reply_grace_s >= 0)),
    CONSTRAINT ck_assess_sessions_state CHECK ((state = ANY (ARRAY['STARTING'::text, 'ACTIVE'::text, 'OUTAGE'::text, 'CLOSED'::text]))),
    CONSTRAINT ck_assess_sessions_tokens_used CHECK ((tokens_used >= 0))
)
    """,
    r"""
ALTER TABLE ONLY public.assess_sessions
    ADD CONSTRAINT assess_sessions_pkey PRIMARY KEY (id)
    """,
    r"""
ALTER TABLE ONLY public.assess_sessions
    ADD CONSTRAINT uq_assess_sessions_id_state UNIQUE (id, state)
    """,
    r"""
ALTER TABLE ONLY public.assess_sessions
    ADD CONSTRAINT uq_assess_sessions_no UNIQUE (site_id, student_userid, cmid, session_no)
    """,
    r"""
CREATE UNIQUE INDEX uq_assess_sessions_one_open ON public.assess_sessions USING btree (site_id, student_userid, cmid) WHERE (state <> 'CLOSED'::text)
    """,
    # --- assess_setup_check_reports
    r"""
CREATE TABLE public.assess_setup_check_reports (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    site_id uuid NOT NULL,
    requested_by_key_id uuid,
    status text DEFAULT 'queued'::text NOT NULL,
    lms_release text,
    items jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    CONSTRAINT ck_assess_setup_check_reports_items_small CHECK ((pg_column_size(items) < 16384)),
    CONSTRAINT ck_assess_setup_check_reports_status CHECK ((status = ANY (ARRAY['queued'::text, 'running'::text, 'done'::text, 'failed'::text])))
)
    """,
    r"""
ALTER TABLE ONLY public.assess_setup_check_reports
    ADD CONSTRAINT assess_setup_check_reports_pkey PRIMARY KEY (id)
    """,
    r"""
CREATE INDEX ix_assess_setup_check_reports_site ON public.assess_setup_check_reports USING btree (site_id, created_at DESC)
    """,
    # --- assess_setup_checks
    r"""
CREATE TABLE public.assess_setup_checks (
    id bigint NOT NULL,
    site_id uuid NOT NULL,
    item text NOT NULL,
    result text NOT NULL,
    run_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_assess_setup_checks_result CHECK ((result = ANY (ARRAY['ok'::text, 'missing'::text])))
)
    """,
    r"""
CREATE SEQUENCE public.assess_setup_checks_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
    """,
    r"""
ALTER SEQUENCE public.assess_setup_checks_id_seq OWNED BY public.assess_setup_checks.id
    """,
    r"""
ALTER TABLE ONLY public.assess_setup_checks ALTER COLUMN id SET DEFAULT nextval('public.assess_setup_checks_id_seq'::regclass)
    """,
    r"""
ALTER TABLE ONLY public.assess_setup_checks
    ADD CONSTRAINT assess_setup_checks_pkey PRIMARY KEY (id)
    """,
    # --- assess_site_capabilities
    r"""
CREATE TABLE public.assess_site_capabilities (
    site_id uuid NOT NULL,
    flag text NOT NULL,
    release text,
    state text NOT NULL,
    evidence_hash text,
    checked_at timestamp with time zone DEFAULT now() NOT NULL,
    reason_code text,
    CONSTRAINT ck_assess_site_capabilities_flag CHECK ((flag = ANY (ARRAY['quiz_overrides'::text, 'assign_extensions'::text, 'assign_cutoff'::text, 'quiz_grace'::text, 'attempt_state'::text, 'per_student_timelimit'::text, 'review_html_filterable'::text, 'rubric_display_settings'::text, 'module_visibility_per_student'::text, 'loginas_crosscheck'::text, 'graders_resolvable'::text, 'enrolment_readable'::text, 'release_state'::text, 'grading_state'::text, 'assign_user_overrides'::text, 'review_options_per_student'::text]))),
    CONSTRAINT ck_assess_site_capabilities_state CHECK ((state = ANY (ARRAY['readable'::text, 'partial'::text, 'unreadable'::text])))
)
    """,
    r"""
ALTER TABLE ONLY public.assess_site_capabilities
    ADD CONSTRAINT assess_site_capabilities_pkey PRIMARY KEY (site_id, flag)
    """,
    # --- assess_site_keys
    r"""
CREATE TABLE public.assess_site_keys (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    site_id uuid NOT NULL,
    api_key_id uuid NOT NULL,
    purpose text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_assess_site_keys_purpose CHECK ((purpose = ANY (ARRAY['mint'::text, 'teacher'::text, 'privacy-export'::text, 'privacy-erase'::text])))
)
    """,
    r"""
ALTER TABLE ONLY public.assess_site_keys
    ADD CONSTRAINT assess_site_keys_api_key_id_key UNIQUE (api_key_id)
    """,
    r"""
ALTER TABLE ONLY public.assess_site_keys
    ADD CONSTRAINT assess_site_keys_pkey PRIMARY KEY (id)
    """,
    # --- assess_sites
    r"""
CREATE TABLE public.assess_sites (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    lms_site_uuid text NOT NULL,
    base_url text NOT NULL,
    release text,
    min_plugin_version text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    disabled_at timestamp with time zone,
    probe_manifest jsonb,
    last_seen_release text,
    lms_origin text,
    CONSTRAINT ck_assess_sites_lms_origin CHECK (((lms_origin IS NULL) OR (lms_origin ~ '^https?://([a-z0-9.\-]+|\[[0-9a-f:.]+\])(:[0-9]{1,5})?$'::text)))
)
    """,
    r"""
ALTER TABLE ONLY public.assess_sites
    ADD CONSTRAINT assess_sites_lms_site_uuid_key UNIQUE (lms_site_uuid)
    """,
    r"""
ALTER TABLE ONLY public.assess_sites
    ADD CONSTRAINT assess_sites_pkey PRIMARY KEY (id)
    """,
    # --- assess_snapshots
    r"""
CREATE TABLE public.assess_snapshots (
    session_id uuid NOT NULL,
    ciphertext bytea NOT NULL,
    content_hash text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
)
    """,
    r"""
ALTER TABLE ONLY public.assess_snapshots
    ADD CONSTRAINT assess_snapshots_pkey PRIMARY KEY (session_id)
    """,
    # --- assess_start_attempts
    r"""
CREATE TABLE public.assess_start_attempts (
    id bigint NOT NULL,
    site_id uuid NOT NULL,
    cmid bigint NOT NULL,
    student_userid bigint NOT NULL,
    outcome text NOT NULL,
    reason_code text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    session_id uuid,
    CONSTRAINT ck_assess_start_attempts_outcome CHECK ((outcome = ANY (ARRAY['requested'::text, 'committed'::text, 'rolled_back'::text, 'refused'::text])))
)
    """,
    r"""
CREATE SEQUENCE public.assess_start_attempts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
    """,
    r"""
ALTER SEQUENCE public.assess_start_attempts_id_seq OWNED BY public.assess_start_attempts.id
    """,
    r"""
ALTER TABLE ONLY public.assess_start_attempts ALTER COLUMN id SET DEFAULT nextval('public.assess_start_attempts_id_seq'::regclass)
    """,
    r"""
ALTER TABLE ONLY public.assess_start_attempts
    ADD CONSTRAINT assess_start_attempts_pkey PRIMARY KEY (id)
    """,
    r"""
CREATE INDEX ix_assess_start_attempts_student ON public.assess_start_attempts USING btree (site_id, student_userid, cmid, created_at)
    """,
    # --- assess_start_breaker
    r"""
CREATE TABLE public.assess_start_breaker (
    site_id uuid NOT NULL,
    state text NOT NULL,
    opened_at timestamp with time zone,
    half_open_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_assess_start_breaker_state CHECK ((state = ANY (ARRAY['open'::text, 'closed'::text])))
)
    """,
    r"""
ALTER TABLE ONLY public.assess_start_breaker
    ADD CONSTRAINT assess_start_breaker_pkey PRIMARY KEY (site_id)
    """,
    # --- assess_teacher_gate
    r"""
CREATE TABLE public.assess_teacher_gate (
    site_id uuid NOT NULL,
    cmid bigint NOT NULL,
    inputs jsonb DEFAULT '{}'::jsonb NOT NULL,
    refreshed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_assess_teacher_gate_small CHECK ((pg_column_size(inputs) < 16384))
)
    """,
    r"""
ALTER TABLE ONLY public.assess_teacher_gate
    ADD CONSTRAINT assess_teacher_gate_pkey PRIMARY KEY (site_id, cmid)
    """,
    # --- assess_terminations
    r"""
CREATE TABLE public.assess_terminations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    site_id uuid NOT NULL,
    cmid bigint NOT NULL,
    reason_ct bytea NOT NULL,
    reason_kid text NOT NULL,
    closed_count integer NOT NULL,
    rolled_back_count integer NOT NULL,
    also_disable boolean NOT NULL,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT assess_terminations_closed_count_check CHECK ((closed_count >= 0)),
    CONSTRAINT assess_terminations_rolled_back_count_check CHECK ((rolled_back_count >= 0))
)
    """,
    r"""
ALTER TABLE ONLY public.assess_terminations
    ADD CONSTRAINT assess_terminations_pkey PRIMARY KEY (id)
    """,
    r"""
CREATE INDEX ix_assess_terminations_activity ON public.assess_terminations USING btree (site_id, cmid)
    """,
    # --- assess_token_jti
    r"""
CREATE TABLE public.assess_token_jti (
    jti text NOT NULL,
    site_id uuid NOT NULL,
    student_userid bigint NOT NULL,
    cmid bigint NOT NULL,
    courseid_hint bigint,
    mint_key_id uuid NOT NULL,
    minted_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    CONSTRAINT ck_assess_token_jti_len CHECK (((length(jti) >= 8) AND (length(jti) <= 64)))
)
    """,
    r"""
ALTER TABLE ONLY public.assess_token_jti
    ADD CONSTRAINT assess_token_jti_pkey PRIMARY KEY (jti)
    """,
    r"""
CREATE INDEX ix_assess_token_jti_expires ON public.assess_token_jti USING btree (expires_at)
    """,
    # --- assess_transcript_download_tokens
    r"""
CREATE TABLE public.assess_transcript_download_tokens (
    jti_hash text NOT NULL,
    transcript_id uuid NOT NULL,
    subject_userid bigint NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    audience_role text NOT NULL,
    site_id uuid,
    CONSTRAINT ck_assess_download_tokens_role CHECK ((audience_role = ANY (ARRAY['owner'::text, 'grader'::text])))
)
    """,
    r"""
ALTER TABLE ONLY public.assess_transcript_download_tokens
    ADD CONSTRAINT assess_transcript_download_tokens_pkey PRIMARY KEY (jti_hash)
    """,
    # --- assess_transcripts
    r"""
CREATE TABLE public.assess_transcripts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    session_id uuid NOT NULL,
    ciphertext bytea,
    dek_wrapped bytea,
    transcript_kid text NOT NULL,
    content_hash text NOT NULL,
    generated_at timestamp with time zone DEFAULT now() NOT NULL,
    shredded_at timestamp with time zone,
    turns_digest text,
    filename text,
    byte_size integer,
    course_shortname text,
    CONSTRAINT ck_assess_transcripts_shredded CHECK (((ciphertext IS NULL) = (shredded_at IS NOT NULL)))
)
    """,
    r"""
ALTER TABLE ONLY public.assess_transcripts
    ADD CONSTRAINT assess_transcripts_pkey PRIMARY KEY (id)
    """,
    r"""
ALTER TABLE ONLY public.assess_transcripts
    ADD CONSTRAINT assess_transcripts_session_id_key UNIQUE (session_id)
    """,
    r"""
ALTER TABLE public.assess_transcripts
    ADD CONSTRAINT ck_assess_transcripts_live_fields CHECK (((ciphertext IS NULL) OR ((turns_digest IS NOT NULL) AND (filename IS NOT NULL) AND (byte_size IS NOT NULL)))) NOT VALID
    """,
    r"""
CREATE INDEX ix_assess_transcripts_generated_live ON public.assess_transcripts USING btree (generated_at) WHERE (ciphertext IS NOT NULL)
    """,
    # --- assess_turn_flags
    r"""
CREATE TABLE public.assess_turn_flags (
    session_id uuid NOT NULL,
    turn_no integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
)
    """,
    r"""
ALTER TABLE ONLY public.assess_turn_flags
    ADD CONSTRAINT assess_turn_flags_pkey PRIMARY KEY (session_id, turn_no)
    """,
    # --- assess_turn_notes
    r"""
CREATE TABLE public.assess_turn_notes (
    session_id uuid NOT NULL,
    turn_no integer NOT NULL,
    marks text[] DEFAULT '{}'::text[] NOT NULL,
    contests jsonb DEFAULT '[]'::jsonb NOT NULL,
    lang text,
    CONSTRAINT assess_turn_notes_lang_check CHECK ((lang = ANY (ARRAY['it'::text, 'en'::text, 'other'::text]))),
    CONSTRAINT ck_assess_turn_notes_small CHECK (((pg_column_size(contests) < 4096) AND (cardinality(marks) <= 16)))
)
    """,
    r"""
ALTER TABLE ONLY public.assess_turn_notes
    ADD CONSTRAINT assess_turn_notes_pkey PRIMARY KEY (session_id, turn_no)
    """,
    # --- assess_turns
    r"""
CREATE TABLE public.assess_turns (
    session_id uuid NOT NULL,
    turn_no integer NOT NULL,
    role text NOT NULL,
    kind text NOT NULL,
    ciphertext bytea NOT NULL,
    turn_hash text NOT NULL,
    prev_hash text NOT NULL,
    marker_flags integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_assess_turns_kind CHECK ((kind = ANY (ARRAY['opening'::text, 'normal'::text, 'interrupted'::text, 'system'::text]))),
    CONSTRAINT ck_assess_turns_no CHECK ((turn_no >= 1)),
    CONSTRAINT ck_assess_turns_role CHECK ((role = ANY (ARRAY['student'::text, 'bot'::text, 'system'::text])))
)
    """,
    r"""
ALTER TABLE ONLY public.assess_turns
    ADD CONSTRAINT assess_turns_pkey PRIMARY KEY (session_id, turn_no)
    """,
)

FOREIGN_KEYS: tuple[str, ...] = (
    r"""
ALTER TABLE ONLY public.assess_activity_config_sealed
    ADD CONSTRAINT assess_activity_config_sealed_site_id_cmid_fkey FOREIGN KEY (site_id, cmid) REFERENCES public.assess_activity_config(site_id, cmid) ON DELETE CASCADE
    """,
    r"""
ALTER TABLE ONLY public.assess_activity_config
    ADD CONSTRAINT assess_activity_config_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id) ON DELETE CASCADE
    """,
    r"""
ALTER TABLE ONLY public.assess_deliveries
    ADD CONSTRAINT assess_deliveries_transcript_id_fkey FOREIGN KEY (transcript_id) REFERENCES public.assess_transcripts(id)
    """,
    r"""
ALTER TABLE ONLY public.assess_enrolment_checks
    ADD CONSTRAINT assess_enrolment_checks_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.assess_sessions(id) ON DELETE CASCADE
    """,
    r"""
ALTER TABLE ONLY public.assess_erasure_records
    ADD CONSTRAINT assess_erasure_records_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id)
    """,
    r"""
ALTER TABLE ONLY public.assess_extra_time
    ADD CONSTRAINT assess_extra_time_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id) ON DELETE CASCADE
    """,
    r"""
ALTER TABLE ONLY public.assess_gate_inputs
    ADD CONSTRAINT assess_gate_inputs_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id)
    """,
    r"""
ALTER TABLE ONLY public.assess_outage_incidents
    ADD CONSTRAINT assess_outage_incidents_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.assess_sessions(id) ON DELETE CASCADE
    """,
    r"""
ALTER TABLE ONLY public.assess_privacy_config
    ADD CONSTRAINT assess_privacy_config_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id) ON DELETE CASCADE
    """,
    r"""
ALTER TABLE ONLY public.assess_purge_records
    ADD CONSTRAINT assess_purge_records_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id) ON DELETE CASCADE
    """,
    r"""
ALTER TABLE ONLY public.assess_render_checks
    ADD CONSTRAINT assess_render_checks_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id)
    """,
    r"""
ALTER TABLE ONLY public.assess_render_state
    ADD CONSTRAINT assess_render_state_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id)
    """,
    r"""
ALTER TABLE ONLY public.assess_reset_grants
    ADD CONSTRAINT assess_reset_grants_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id) ON DELETE CASCADE
    """,
    r"""
ALTER TABLE ONLY public.assess_session_keys
    ADD CONSTRAINT assess_session_keys_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.assess_sessions(id) ON DELETE CASCADE
    """,
    r"""
ALTER TABLE ONLY public.assess_sessions
    ADD CONSTRAINT assess_sessions_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id)
    """,
    r"""
ALTER TABLE ONLY public.assess_setup_check_reports
    ADD CONSTRAINT assess_setup_check_reports_requested_by_key_id_fkey FOREIGN KEY (requested_by_key_id) REFERENCES public.api_keys(id) ON DELETE SET NULL
    """,
    r"""
ALTER TABLE ONLY public.assess_setup_check_reports
    ADD CONSTRAINT assess_setup_check_reports_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id) ON DELETE CASCADE
    """,
    r"""
ALTER TABLE ONLY public.assess_setup_checks
    ADD CONSTRAINT assess_setup_checks_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id) ON DELETE CASCADE
    """,
    r"""
ALTER TABLE ONLY public.assess_site_capabilities
    ADD CONSTRAINT assess_site_capabilities_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id) ON DELETE CASCADE
    """,
    r"""
ALTER TABLE ONLY public.assess_site_keys
    ADD CONSTRAINT assess_site_keys_api_key_id_fkey FOREIGN KEY (api_key_id) REFERENCES public.api_keys(id) ON DELETE CASCADE
    """,
    r"""
ALTER TABLE ONLY public.assess_site_keys
    ADD CONSTRAINT assess_site_keys_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id) ON DELETE CASCADE
    """,
    r"""
ALTER TABLE ONLY public.assess_snapshots
    ADD CONSTRAINT assess_snapshots_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.assess_sessions(id) ON DELETE CASCADE
    """,
    r"""
ALTER TABLE ONLY public.assess_start_attempts
    ADD CONSTRAINT assess_start_attempts_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id)
    """,
    r"""
ALTER TABLE ONLY public.assess_start_breaker
    ADD CONSTRAINT assess_start_breaker_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id) ON DELETE CASCADE
    """,
    r"""
ALTER TABLE ONLY public.assess_teacher_gate
    ADD CONSTRAINT assess_teacher_gate_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id) ON DELETE CASCADE
    """,
    r"""
ALTER TABLE ONLY public.assess_terminations
    ADD CONSTRAINT assess_terminations_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id) ON DELETE CASCADE
    """,
    r"""
ALTER TABLE ONLY public.assess_token_jti
    ADD CONSTRAINT assess_token_jti_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id) ON DELETE CASCADE
    """,
    r"""
ALTER TABLE ONLY public.assess_transcript_download_tokens
    ADD CONSTRAINT assess_transcript_download_tokens_site_id_fkey FOREIGN KEY (site_id) REFERENCES public.assess_sites(id)
    """,
    r"""
ALTER TABLE ONLY public.assess_transcript_download_tokens
    ADD CONSTRAINT assess_transcript_download_tokens_transcript_id_fkey FOREIGN KEY (transcript_id) REFERENCES public.assess_transcripts(id)
    """,
    r"""
ALTER TABLE ONLY public.assess_transcripts
    ADD CONSTRAINT assess_transcripts_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.assess_sessions(id)
    """,
    r"""
ALTER TABLE ONLY public.assess_turn_flags
    ADD CONSTRAINT assess_turn_flags_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.assess_sessions(id) ON DELETE CASCADE
    """,
    r"""
ALTER TABLE ONLY public.assess_turn_flags
    ADD CONSTRAINT assess_turn_flags_session_id_turn_no_fkey FOREIGN KEY (session_id, turn_no) REFERENCES public.assess_turns(session_id, turn_no) ON DELETE CASCADE
    """,
    r"""
ALTER TABLE ONLY public.assess_turn_notes
    ADD CONSTRAINT assess_turn_notes_session_id_turn_no_fkey FOREIGN KEY (session_id, turn_no) REFERENCES public.assess_turns(session_id, turn_no) ON DELETE CASCADE
    """,
    r"""
ALTER TABLE ONLY public.assess_turns
    ADD CONSTRAINT assess_turns_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.assess_sessions(id) ON DELETE CASCADE
    """,
    r"""
ALTER TABLE ONLY public.assess_session_handoff
    ADD CONSTRAINT fk_assess_session_handoff_session FOREIGN KEY (session_id, session_state) REFERENCES public.assess_sessions(id, state) ON UPDATE CASCADE ON DELETE CASCADE
    """,
)

FUNCTIONS: tuple[str, ...] = (
    r"""
CREATE FUNCTION public.assess_append_turn(p_sid uuid, p_epoch integer, p_expected_seq integer, p_role text, p_kind text, p_ciphertext bytea, p_turn_hash text, p_prev_hash text) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$

    DECLARE
        v_seq integer;
        v_pending timestamptz;
        v_deadline timestamptz;
        v_grace integer;
        v_last_no integer;
        v_expected_prev text;
        v_last_role text;
    BEGIN
        IF p_kind NOT IN ('normal', 'interrupted', 'system')
           OR p_role NOT IN ('student', 'bot', 'system') THEN
            RETURN NULL;  -- the opening turn is written by the worker, never here
        END IF;
        SELECT s.seq, s.pending_reply_since, s.deadline_at, s.reply_grace_s
          INTO v_seq, v_pending, v_deadline, v_grace
        FROM public.assess_sessions s
        WHERE s.id = p_sid AND s.state = 'ACTIVE' AND s.epoch = p_epoch
        FOR UPDATE;
        IF NOT FOUND OR v_seq <> p_expected_seq THEN
            RETURN NULL;
        END IF;
        -- REQ-070, on the database's wall clock: no student message from the deadline on, and
        -- no ordinary bot reply after deadline + grace; the marked interrupted turn (no content
        -- of the model) is still accepted so that a pending student turn is always answered
        IF v_deadline IS NOT NULL THEN
            IF p_role = 'student' AND pg_catalog.clock_timestamp() >= v_deadline THEN
                RETURN NULL;
            END IF;
            IF p_role = 'bot' AND p_kind = 'normal' AND pg_catalog.clock_timestamp()
               > v_deadline + pg_catalog.make_interval(secs => v_grace) THEN
                RETURN NULL;
            END IF;
        END IF;
        IF p_role = 'student' AND v_pending IS NOT NULL THEN
            RETURN NULL;  -- a reply is still pending: one student turn at a time
        END IF;
        IF p_role = 'bot' AND v_pending IS NULL THEN
            RETURN NULL;  -- no student turn is waiting: a second reply is never appended
        END IF;
        SELECT t.turn_no, t.turn_hash, t.role INTO v_last_no, v_expected_prev, v_last_role
        FROM public.assess_turns t WHERE t.session_id = p_sid
        ORDER BY t.turn_no DESC LIMIT 1;
        IF NOT FOUND THEN
            RETURN NULL;  -- no opening turn yet: nothing to chain onto
        END IF;
        IF p_role = 'bot' AND v_last_role <> 'student' THEN
            RETURN NULL;  -- a bot turn answers exactly one student turn
        END IF;
        IF v_expected_prev IS DISTINCT FROM p_prev_hash THEN
            RETURN NULL;
        END IF;
        INSERT INTO public.assess_turns
            (session_id, turn_no, role, kind, ciphertext, turn_hash, prev_hash)
        VALUES (p_sid, v_last_no + 1, p_role, p_kind, p_ciphertext, p_turn_hash,
                p_prev_hash);
        UPDATE public.assess_sessions
           SET seq = seq + 1, last_activity_at = pg_catalog.now(),
               pending_reply_since = CASE
                   WHEN p_role = 'student' THEN pg_catalog.now()
                   WHEN p_role = 'bot' THEN NULL
                   ELSE pending_reply_since END
         WHERE id = p_sid;
        RETURN v_last_no + 1;
    END
    $$
    """,
    r"""
CREATE FUNCTION public.assess_audit_events_append_only() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        BEGIN
            RAISE EXCEPTION 'assess_audit_events is append-only'
                USING ERRCODE = 'integrity_constraint_violation';
        END
        $$
    """,
    r"""
CREATE FUNCTION public.assess_audit_sealed_change() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
    DECLARE
        v_action text;
        v_data jsonb;
        v_old text[] := '{}';
        v_new text[] := coalesce(NEW.refs, '{}');
        i integer;
    BEGIN
        -- the worker's key rotation re-seals blob and kid (its only UPDATE columns): not a
        -- teacher's change. Decided by the login role, which a SECURITY DEFINER function keeps
        -- in session_user; every other write, whatever columns it names, is audited.
        IF TG_OP = 'UPDATE' AND session_user = 'vektra_assess_worker' THEN
            RETURN NULL;
        END IF;
        IF TG_OP = 'INSERT' THEN
            IF NEW.sealed_blob IS NULL THEN
                RETURN NULL;  -- clearing a field that was never set: nothing changed
            END IF;
            v_action := 'set';
        ELSE
            v_old := coalesce(OLD.refs, '{}');
            IF OLD.sealed_blob IS NULL AND NEW.sealed_blob IS NULL THEN
                RETURN NULL;
            END IF;
            -- the same recipients sent again (the form re-sends them): re-sealed, not changed
            IF OLD.sealed_blob IS NOT NULL AND NEW.sealed_blob IS NOT NULL
               AND NEW.field = 'transcript_recipients' AND v_old = v_new THEN
                RETURN NULL;
            END IF;
            v_action := CASE WHEN NEW.sealed_blob IS NULL THEN 'cleared'
                             WHEN OLD.sealed_blob IS NULL THEN 'set'
                             ELSE 'replaced' END;
        END IF;
        v_data := pg_catalog.jsonb_build_object(
            'field', NEW.field, 'action', v_action, 'cmid', NEW.cmid, 'actor', NEW.updated_by);
        IF NEW.field = 'transcript_recipients' THEN
            v_data := v_data || pg_catalog.jsonb_build_object(
                'old_count', pg_catalog.cardinality(v_old),
                'new_count', pg_catalog.cardinality(v_new));
            FOR i IN 1 .. pg_catalog.cardinality(v_old) LOOP
                v_data := v_data || pg_catalog.jsonb_build_object('old_' || i, v_old[i]);
            END LOOP;
            FOR i IN 1 .. pg_catalog.cardinality(v_new) LOOP
                v_data := v_data || pg_catalog.jsonb_build_object('new_' || i, v_new[i]);
            END LOOP;
        END IF;
        INSERT INTO public.assess_audit_events
            (site_id, session_id, event, reason_code, actor_kind, data, created_at)
        VALUES (NEW.site_id, NULL, 'assess_teacher_config_changed', NEW.field, 'api', v_data,
                pg_catalog.clock_timestamp());
        RETURN NULL;
    END
    $$
    """,
    r"""
CREATE FUNCTION public.assess_close_session(p_sid uuid, p_epoch integer, p_reason text) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
    DECLARE
        v_state text;
        v_epoch integer;
        v_site uuid;
    BEGIN
        IF p_reason NOT IN ('finished', 'timed_out', 'service_failure', 'budget_exhausted', 'terminated_by_teacher', 'access_revoked') THEN
            RETURN false;
        END IF;
        SELECT s.state, s.epoch, s.site_id INTO v_state, v_epoch, v_site
        FROM public.assess_sessions s WHERE s.id = p_sid FOR UPDATE;
        IF NOT FOUND THEN
            RETURN false;
        END IF;
        IF v_state = 'CLOSED' THEN
            RETURN true;  -- idempotent
        END IF;
        IF v_state NOT IN ('ACTIVE', 'OUTAGE') OR v_epoch <> p_epoch THEN
            RETURN false;
        END IF;
        -- an open outage interval ends with the close (no credit: the session is over)
        UPDATE public.assess_outage_incidents SET ended_at = pg_catalog.now()
         WHERE session_id = p_sid AND ended_at IS NULL;
        UPDATE public.assess_sessions
           SET state = 'CLOSED', close_reason = p_reason, closed_at = pg_catalog.now(), epoch = epoch + 1
         WHERE id = p_sid;
        INSERT INTO public.assess_jobs (kind, args, dedupe_key)
        VALUES ('transcript_generate',
                pg_catalog.jsonb_build_object('session_id', p_sid),
                'transcript_generate:' || p_sid::text);
        IF p_reason = 'service_failure' THEN
            -- REQ-086: the teacher hears of it within a minute (delivery: plan 10)
            INSERT INTO public.assess_jobs (kind, args, dedupe_key)
            VALUES ('teacher_notify',
                    pg_catalog.jsonb_build_object('session_id', p_sid, 'reason', p_reason),
                    'teacher_notify:' || p_sid::text);
        END IF;
        INSERT INTO public.assess_audit_events
            (site_id, session_id, event, reason_code, actor_kind)
        VALUES (v_site, p_sid, 'assess_close', p_reason,
                CASE WHEN session_user = 'vektra_assess_worker'
                     THEN 'worker' ELSE 'api' END);
        RETURN true;
    END
    $$
    """,
    r"""
CREATE FUNCTION public.assess_end_outage(p_sid uuid, p_min_credit_s integer) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
    DECLARE
        v_state text;
        v_epoch integer;
        v_site uuid;
        v_duration integer;
        v_credited integer;
        v_cap integer;
        v_spent double precision;
        v_open_id bigint;
        v_open_s double precision;
        v_credit integer;
    BEGIN
        SELECT s.state, s.epoch, s.site_id, s.duration_s, s.credited_outage_s
          INTO v_state, v_epoch, v_site, v_duration, v_credited
        FROM public.assess_sessions s WHERE s.id = p_sid FOR UPDATE;
        IF NOT FOUND OR v_state <> 'OUTAGE' THEN
            RETURN NULL;
        END IF;
        -- C and the spent outage: the one definition (revision 0032)
        v_cap := public.assess_outage_cap(v_duration);
        v_spent := public.assess_outage_spent(p_sid);
        IF v_spent > v_cap THEN
            PERFORM public.assess_close_session(p_sid, v_epoch, 'service_failure');
            RETURN -1;
        END IF;
        SELECT i.id, extract(epoch FROM pg_catalog.now() - i.started_at)
          INTO v_open_id, v_open_s
          FROM public.assess_outage_incidents i
         WHERE i.session_id = p_sid AND i.ended_at IS NULL
         ORDER BY i.started_at DESC LIMIT 1;
        v_credit := 0;
        IF v_open_id IS NOT NULL AND v_open_s >= p_min_credit_s THEN
            v_credit := least(floor(v_open_s)::integer, greatest(v_cap - v_credited, 0));
        END IF;
        UPDATE public.assess_outage_incidents
           SET ended_at = pg_catalog.now(), credited_s = v_credit
         WHERE id = v_open_id;
        UPDATE public.assess_sessions
           SET state = 'ACTIVE',
               credited_outage_s = credited_outage_s + v_credit,
               -- DEADLINE_SQL with the new credit (SET reads the old column values)
               deadline_at = committed_at
                   + make_interval(secs => duration_s + credited_outage_s + v_credit),
               pending_reply_since = CASE WHEN pending_reply_since IS NULL THEN NULL
                                          ELSE pg_catalog.now() END
         WHERE id = p_sid;
        INSERT INTO public.assess_audit_events
            (site_id, session_id, event, reason_code, actor_kind, data, created_at)
        VALUES (v_site, p_sid, 'assess_outage_end', NULL, 'worker',
                pg_catalog.jsonb_build_object('credited_s', v_credit),
                pg_catalog.clock_timestamp());
        RETURN v_credit;
    END
    $$
    """,
    r"""
CREATE FUNCTION public.assess_enqueue_resend(p_tid uuid, p_actor text, p_min_interval_s integer) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
    DECLARE
        v_round integer;
        v_site uuid;
        v_sid uuid;
        v_n integer;
    BEGIN
        IF p_actor IS NULL OR length(p_actor) > 64 THEN
            RETURN 0;
        END IF;
        SELECT s.site_id, s.id INTO v_site, v_sid
          FROM public.assess_transcripts t JOIN public.assess_sessions s ON s.id = t.session_id
         WHERE t.id = p_tid AND t.ciphertext IS NOT NULL;
        IF NOT FOUND THEN
            RETURN 0;
        END IF;
        -- two resends of one transcript never compute the same round; the worker's recipient
        -- fan-out takes the same lock
        PERFORM pg_catalog.pg_advisory_xact_lock(
            pg_catalog.hashtextextended('assess_resend:' || p_tid::text, 0));
        SELECT max(d.round) INTO v_round FROM public.assess_deliveries d
         WHERE d.transcript_id = p_tid AND d.kind = 'transcript';
        IF v_round IS NULL THEN
            RETURN 0;  -- never delivered to anyone: nothing to resend
        END IF;
        IF p_min_interval_s > 0 AND EXISTS (SELECT 1 FROM public.assess_deliveries d
                    WHERE d.transcript_id = p_tid AND d.kind = 'transcript'
                      AND d.created_at > pg_catalog.now()
                          - pg_catalog.make_interval(secs => p_min_interval_s)) THEN
            RETURN -1;
        END IF;
        INSERT INTO public.assess_deliveries
            (transcript_id, kind, round, recipient_kind, recipient_userid, recipient_ref, status)
        SELECT p_tid, 'transcript', v_round + 1, d.recipient_kind, d.recipient_userid,
               d.recipient_ref, 'PENDING'
          FROM public.assess_deliveries d
         WHERE d.transcript_id = p_tid AND d.kind = 'transcript' AND d.round = v_round
           AND d.status <> 'REPLACED';
        GET DIAGNOSTICS v_n = ROW_COUNT;
        IF v_n = 0 THEN
            RETURN 0;
        END IF;
        INSERT INTO public.assess_audit_events
            (site_id, session_id, event, reason_code, actor_kind, data)
        VALUES (v_site, v_sid, 'assess_transcript_resend', NULL, 'api',
                pg_catalog.jsonb_build_object('round', v_round + 1, 'recipients', v_n,
                                              'actor', p_actor));
        RETURN v_round + 1;
    END
    $$
    """,
    r"""
CREATE FUNCTION public.assess_enter_outage(p_sid uuid, p_epoch integer, p_started_at timestamp with time zone, p_reason text) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
    DECLARE
        v_state text;
        v_epoch integer;
        v_site uuid;
        v_committed timestamptz;
        v_last_ok timestamptz;
        v_last_end timestamptz;
        v_failed integer;
        v_start timestamptz;
    BEGIN
        SELECT s.state, s.epoch, s.site_id, s.committed_at
          INTO v_state, v_epoch, v_site, v_committed
        FROM public.assess_sessions s WHERE s.id = p_sid FOR UPDATE;
        IF NOT FOUND OR v_epoch <> p_epoch THEN
            RETURN 'noop';
        END IF;
        IF v_state = 'OUTAGE' THEN
            RETURN 'outage';  -- idempotent: one interval at a time
        END IF;
        IF v_state <> 'ACTIVE' THEN
            RETURN 'noop';
        END IF;
        -- consecutive failed calls: intervals opened since the last answered bot turn
        SELECT max(t.created_at) INTO v_last_ok FROM public.assess_turns t
         WHERE t.session_id = p_sid AND t.role = 'bot' AND t.kind IN ('opening', 'normal');
        SELECT count(*) + 1 INTO v_failed
          FROM public.assess_outage_incidents i
         WHERE i.session_id = p_sid
           AND i.started_at >= coalesce(v_last_ok, v_committed, '-infinity'::timestamptz);
        IF v_failed >= 3 THEN
            PERFORM public.assess_close_session(p_sid, p_epoch, 'service_failure');
            RETURN 'closed';
        END IF;
        SELECT max(i.ended_at) INTO v_last_end
          FROM public.assess_outage_incidents i WHERE i.session_id = p_sid;
        v_start := least(pg_catalog.now(), greatest(
            coalesce(p_started_at, pg_catalog.now()),
            coalesce(v_committed, '-infinity'::timestamptz),
            coalesce(v_last_end, '-infinity'::timestamptz)));
        UPDATE public.assess_sessions SET state = 'OUTAGE' WHERE id = p_sid;
        INSERT INTO public.assess_outage_incidents (session_id, started_at, reason_code)
        VALUES (p_sid, v_start, left(p_reason, 64));
        INSERT INTO public.assess_audit_events
            (site_id, session_id, event, reason_code, actor_kind, data, created_at)
        VALUES (v_site, p_sid, 'assess_outage_start', left(p_reason, 64), 'api',
                pg_catalog.jsonb_build_object('failed_calls', v_failed),
                pg_catalog.clock_timestamp());
        RETURN 'outage';
    END
    $$
    """,
    r"""
CREATE FUNCTION public.assess_metrics_snapshot() RETURNS jsonb
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        SELECT jsonb_build_object(
            'sessions', COALESCE((SELECT jsonb_object_agg(state, n) FROM (
                SELECT state, count(*) AS n FROM public.assess_sessions GROUP BY state) s),
                '{}'::jsonb),
            'closed', COALESCE((SELECT jsonb_object_agg(close_reason, n) FROM (
                SELECT close_reason, count(*) AS n FROM public.assess_sessions
                WHERE state = 'CLOSED' GROUP BY close_reason) c), '{}'::jsonb),
            'deliveries', COALESCE((SELECT jsonb_object_agg(status, n) FROM (
                SELECT status, count(*) AS n FROM public.assess_deliveries
                WHERE kind <> 'smtp_test' GROUP BY status) d), '{}'::jsonb),
            'retrying_oldest_s', (SELECT extract(epoch FROM now() - min(created_at))
                FROM public.assess_deliveries WHERE status = 'RETRYING'
                AND kind <> 'smtp_test'),
            'tls_failures', (SELECT count(*) FROM public.assess_deliveries
                WHERE last_error_class = 'tls' AND kind <> 'smtp_test'
                AND status IN ('PENDING', 'RETRYING', 'FAILED')),
            'capacity_refusals', (SELECT count(*) FROM public.assess_start_attempts
                WHERE outcome IN ('rolled_back', 'refused') AND reason_code = 'capacity'),
            'rollbacks', COALESCE((SELECT jsonb_object_agg(reason, n) FROM (
                SELECT COALESCE(reason_code, 'none') AS reason, count(*) AS n
                FROM public.assess_start_attempts WHERE outcome = 'rolled_back'
                GROUP BY 1) r), '{}'::jsonb),
            'jobs_oldest_ready_s', (SELECT extract(epoch FROM now() - min(not_before))
                FROM public.assess_jobs WHERE state = 'ready' AND not_before <= now()),
            'worker_heartbeat_age_s', (SELECT extract(epoch FROM now() - beat_at)
                FROM public.assess_heartbeat WHERE component = 'worker'),
            'smtp_probe_ok', (SELECT CASE
                    WHEN details->>'state' = 'busy' THEN (details->'last'->>'ok')::boolean
                    ELSE (details->>'ok')::boolean END
                FROM public.assess_heartbeat WHERE component = 'smtp'),
            'smtp_probe_age_s', (SELECT extract(epoch FROM now() - beat_at)
                FROM public.assess_heartbeat WHERE component = 'smtp'),
            'purge_last_success', (SELECT (details->>'last_success')::double precision
                FROM public.assess_heartbeat WHERE component = 'purge'),
            'purge_rows_deleted', (SELECT (details->>'rows_deleted')::bigint
                FROM public.assess_heartbeat WHERE component = 'purge'),
            'purge_last_ok', (SELECT (details->>'ok')::boolean
                FROM public.assess_heartbeat WHERE component = 'purge')
        )
    $$
    """,
    r"""
CREATE FUNCTION public.assess_outage_cap(p_duration_s integer) RETURNS integer
    LANGUAGE sql IMMUTABLE
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        SELECT least(900, coalesce(p_duration_s, 0) / 2)
    $$
    """,
    r"""
CREATE FUNCTION public.assess_outage_spent(p_sid uuid) RETURNS double precision
    LANGUAGE sql STABLE
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        SELECT coalesce(sum(extract(epoch FROM coalesce(i.ended_at, now()) - i.started_at)),
                        0)::double precision
        FROM public.assess_outage_incidents i WHERE i.session_id = p_sid
    $$
    """,
    r"""
CREATE FUNCTION public.assess_rollback_starting(p_sid uuid) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
    DECLARE
        v_site uuid;
        v_cmid bigint;
        v_user bigint;
        v_no integer;
        v_left bigint;
    BEGIN
        DELETE FROM public.assess_sessions WHERE id = p_sid AND state = 'STARTING'
        RETURNING site_id, cmid, student_userid, session_no INTO v_site, v_cmid, v_user, v_no;
        IF NOT FOUND THEN
            RETURN false;  -- committed meanwhile, already rolled back, or never STARTING
        END IF;
        -- verified by count, never assumed from the cascade (as the worker's rollback does)
        SELECT (SELECT count(*) FROM public.assess_sessions WHERE id = p_sid)
             + (SELECT count(*) FROM public.assess_snapshots WHERE session_id = p_sid)
             + (SELECT count(*) FROM public.assess_session_keys WHERE session_id = p_sid)
             + (SELECT count(*) FROM public.assess_turns WHERE session_id = p_sid)
             + (SELECT count(*) FROM public.assess_session_handoff WHERE session_id = p_sid)
          INTO v_left;
        IF v_left <> 0 THEN
            RAISE EXCEPTION 'assess_rollback_starting: % row(s) of the session survived', v_left;
        END IF;
        INSERT INTO public.assess_start_attempts
            (site_id, cmid, student_userid, outcome, reason_code, session_id)
        VALUES (v_site, v_cmid, v_user, 'rolled_back', 'terminated_by_teacher', p_sid);
        INSERT INTO public.assess_audit_events
            (site_id, session_id, event, reason_code, actor_kind, data, created_at)
        VALUES (v_site, p_sid, 'assess_start_rollback', 'terminated_by_teacher', 'api',
                pg_catalog.jsonb_build_object('cmid', v_cmid, 'session_no', v_no),
                pg_catalog.clock_timestamp());
        RETURN true;
    END
    $$
    """,
    r"""
CREATE FUNCTION public.assess_sessions_drop_handoff_on_close() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        BEGIN
            IF NEW.state = 'CLOSED' THEN
                DELETE FROM public.assess_session_handoff WHERE session_id = NEW.id;
            END IF;
            RETURN NEW;
        END
        $$
    """,
    r"""
CREATE FUNCTION public.assess_sessions_no_revival() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        BEGIN
            IF OLD.state = 'CLOSED' AND NEW.state <> 'CLOSED' THEN
                RAISE EXCEPTION 'assess session % is CLOSED and cannot be reopened', OLD.id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $$
    """,
    r"""
CREATE FUNCTION public.assess_sessions_number() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        BEGIN
            -- session #N+1 after a reset (REQ-087): number it under a lock on the triple,
            -- so no caller has to compute max()+1 itself
            IF NEW.session_no IS NULL THEN
                PERFORM pg_advisory_xact_lock(
                    pg_catalog.hashtextextended(
                        NEW.site_id::text || ':' || NEW.student_userid || ':' || NEW.cmid, 0));
                SELECT coalesce(max(s.session_no), 0) + 1 INTO NEW.session_no
                FROM public.assess_sessions s
                WHERE s.site_id = NEW.site_id AND s.student_userid = NEW.student_userid
                  AND s.cmid = NEW.cmid;
            END IF;
            RETURN NEW;
        END
        $$
    """,
    r"""
CREATE FUNCTION public.assess_sessions_require_opening_turn() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        BEGIN
            IF NEW.state IN ('ACTIVE', 'OUTAGE', 'CLOSED') AND NOT EXISTS (
                SELECT 1 FROM public.assess_turns t
                WHERE t.session_id = NEW.id AND t.kind = 'opening'
            ) THEN
                RAISE EXCEPTION
                    'assess session % is % without an opening turn (REQ-067)',
                    NEW.id, NEW.state
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NULL;
        END
        $$
    """,
    r"""
CREATE FUNCTION public.assess_snapshots_immutable() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
        BEGIN
            RAISE EXCEPTION 'assess_snapshots rows are immutable (BR-015)'
                USING ERRCODE = 'integrity_constraint_violation';
        END
        $$
    """,
    r"""
CREATE FUNCTION public.get_session_material(p_sid uuid, p_epoch integer) RETURNS TABLE(sealed_dek bytea, handoff_kid text, snapshot_ciphertext bytea, snapshot_hash text, seq integer, turn_nos integer[], turn_roles text[], turn_kinds text[], turn_ciphertexts bytea[], turn_hashes text[])
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
            SELECT h.sealed_dek, h.handoff_kid, sn.ciphertext, sn.content_hash, s.seq,
                   array(SELECT t.turn_no FROM public.assess_turns t
                         WHERE t.session_id = s.id ORDER BY t.turn_no),
                   array(SELECT t.role FROM public.assess_turns t
                         WHERE t.session_id = s.id ORDER BY t.turn_no),
                   array(SELECT t.kind FROM public.assess_turns t
                         WHERE t.session_id = s.id ORDER BY t.turn_no),
                   array(SELECT t.ciphertext FROM public.assess_turns t
                         WHERE t.session_id = s.id ORDER BY t.turn_no),
                   array(SELECT t.turn_hash FROM public.assess_turns t
                         WHERE t.session_id = s.id ORDER BY t.turn_no)
            FROM public.assess_sessions s
            JOIN public.assess_session_handoff h ON h.session_id = s.id
            JOIN public.assess_snapshots sn ON sn.session_id = s.id
            WHERE s.id = p_sid
              AND s.state IN ('ACTIVE', 'OUTAGE')
              AND s.epoch = p_epoch
              AND h.epoch = s.epoch
        $$
    """,
)

TRIGGERS: tuple[str, ...] = (
    r"""
CREATE TRIGGER trg_assess_audit_events_append_only BEFORE DELETE OR UPDATE ON public.assess_audit_events FOR EACH ROW EXECUTE FUNCTION public.assess_audit_events_append_only()
    """,
    r"""
CREATE TRIGGER trg_assess_audit_sealed_change AFTER INSERT OR UPDATE ON public.assess_activity_config_sealed FOR EACH ROW EXECUTE FUNCTION public.assess_audit_sealed_change()
    """,
    r"""
CREATE TRIGGER trg_assess_sessions_drop_handoff BEFORE UPDATE OF state ON public.assess_sessions FOR EACH ROW EXECUTE FUNCTION public.assess_sessions_drop_handoff_on_close()
    """,
    r"""
CREATE TRIGGER trg_assess_sessions_no_revival BEFORE UPDATE OF state ON public.assess_sessions FOR EACH ROW EXECUTE FUNCTION public.assess_sessions_no_revival()
    """,
    r"""
CREATE TRIGGER trg_assess_sessions_number BEFORE INSERT ON public.assess_sessions FOR EACH ROW EXECUTE FUNCTION public.assess_sessions_number()
    """,
    r"""
CREATE CONSTRAINT TRIGGER trg_assess_sessions_opening_turn AFTER INSERT OR UPDATE OF state ON public.assess_sessions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.assess_sessions_require_opening_turn()
    """,
    r"""
CREATE TRIGGER trg_assess_snapshots_immutable BEFORE UPDATE ON public.assess_snapshots FOR EACH ROW EXECUTE FUNCTION public.assess_snapshots_immutable()
    """,
)

DEFINER_OWNED: tuple[str, ...] = (
    "public.assess_append_turn(p_sid uuid, p_epoch integer, p_expected_seq integer, p_role text, p_kind text, p_ciphertext bytea, p_turn_hash text, p_prev_hash text)",
    "public.assess_audit_sealed_change()",
    "public.assess_close_session(p_sid uuid, p_epoch integer, p_reason text)",
    "public.assess_end_outage(p_sid uuid, p_min_credit_s integer)",
    "public.assess_enqueue_resend(p_tid uuid, p_actor text, p_min_interval_s integer)",
    "public.assess_enter_outage(p_sid uuid, p_epoch integer, p_started_at timestamp with time zone, p_reason text)",
    "public.assess_metrics_snapshot()",
    "public.assess_rollback_starting(p_sid uuid)",
    "public.assess_sessions_drop_handoff_on_close()",
    "public.assess_sessions_require_opening_turn()",
    "public.get_session_material(p_sid uuid, p_epoch integer)",
)


#: the definer's trigger functions outside the matrix: nobody but the trigger EXECUTEs them
DEFINER_TRIGGER_FUNCTIONS: tuple[str, ...] = (
    "public.assess_sessions_drop_handoff_on_close()",
    "public.assess_sessions_require_opening_turn()",
)

#: the grant matrix, rendered by ``vektra_assess.db.grants.render_statements()`` (a migration does
#: not import application code at run time; ``test_grant_matrix.py`` asserts the two are equal)
GRANT_STATEMENTS = (
    "REVOKE ALL ON assess_activity_config, assess_activity_config_sealed, assess_audit_events, assess_consumed_jtis, assess_deliveries, assess_enrolment_checks, assess_erasure_records, assess_extra_time, assess_gate_inputs, assess_heartbeat, assess_jobs, assess_model_activations, assess_outage_incidents, assess_privacy_config, assess_purge_records, assess_render_checks, assess_render_state, assess_reset_grants, assess_session_handoff, assess_session_keys, assess_sessions, assess_setup_check_reports, assess_setup_checks, assess_site_capabilities, assess_site_keys, assess_sites, assess_start_breaker, assess_snapshots, assess_start_attempts, assess_teacher_gate, assess_terminations, assess_token_jti, assess_transcript_download_tokens, assess_transcripts, assess_turn_flags, assess_turn_notes, assess_turns FROM vektra_assess_api, vektra_assess_worker, vektra_assess_definer",
    "REVOKE ALL ON SEQUENCE assess_audit_events_id_seq, assess_enrolment_checks_id_seq, assess_erasure_records_id_seq, assess_jobs_id_seq, assess_model_activations_id_seq, assess_outage_incidents_id_seq, assess_purge_records_id_seq, assess_render_checks_id_seq, assess_setup_checks_id_seq, assess_start_attempts_id_seq FROM vektra_assess_api, vektra_assess_worker, vektra_assess_definer",
    "REVOKE CREATE ON SCHEMA public FROM vektra_assess_api, vektra_assess_worker, vektra_assess_definer",
    "REVOKE ALL ON query_traces FROM vektra_assess_api, vektra_assess_worker, vektra_assess_definer",
    "GRANT INSERT (id, site_id, cmid, activity_type, enabled, flags, rag_enabled, end_at, attested_opening_at, attested_by, updated_at, cap_minutes, version, updated_by) ON assess_activity_config TO vektra_assess_api",
    "GRANT SELECT (id, site_id, cmid, activity_type, enabled, flags, rag_enabled, end_at, attested_opening_at, attested_by, updated_at, cap_minutes, version, updated_by) ON assess_activity_config TO vektra_assess_api",
    "GRANT UPDATE (enabled, flags, rag_enabled, end_at, attested_opening_at, attested_by, updated_at, cap_minutes, version, updated_by) ON assess_activity_config TO vektra_assess_api",
    "GRANT INSERT (site_id, cmid, field, sealed_blob, kid, refs, item_count, updated_by, updated_at) ON assess_activity_config_sealed TO vektra_assess_api",
    "GRANT SELECT (site_id, cmid, field, is_set, item_count, updated_by, updated_at) ON assess_activity_config_sealed TO vektra_assess_api",
    "GRANT UPDATE (sealed_blob, kid, refs, item_count, updated_by, updated_at) ON assess_activity_config_sealed TO vektra_assess_api",
    "GRANT INSERT ON assess_audit_events TO vektra_assess_api",
    "GRANT INSERT ON assess_consumed_jtis TO vektra_assess_api",
    "GRANT SELECT ON assess_consumed_jtis TO vektra_assess_api",
    "GRANT UPDATE (consumed_at) ON assess_consumed_jtis TO vektra_assess_api",
    "GRANT SELECT (id, transcript_id, kind, round, recipient_kind, recipient_userid, status, attempts, not_before, attempt_started_at, last_error_class, smtp_code, sent_at, created_at) ON assess_deliveries TO vektra_assess_api",
    "GRANT SELECT ON assess_enrolment_checks TO vektra_assess_api",
    "GRANT INSERT ON assess_erasure_records TO vektra_assess_api",
    "GRANT SELECT ON assess_erasure_records TO vektra_assess_api",
    "GRANT INSERT ON assess_extra_time TO vektra_assess_api",
    "GRANT SELECT ON assess_extra_time TO vektra_assess_api",
    "GRANT UPDATE ON assess_extra_time TO vektra_assess_api",
    "GRANT SELECT ON assess_gate_inputs TO vektra_assess_api",
    "GRANT SELECT ON assess_heartbeat TO vektra_assess_api",
    "GRANT INSERT ON assess_jobs TO vektra_assess_api",
    "GRANT INSERT ON assess_model_activations TO vektra_assess_api",
    "GRANT SELECT ON assess_model_activations TO vektra_assess_api",
    "GRANT SELECT ON assess_outage_incidents TO vektra_assess_api",
    "GRANT INSERT ON assess_privacy_config TO vektra_assess_api",
    "GRANT SELECT ON assess_privacy_config TO vektra_assess_api",
    "GRANT UPDATE (retention_days, legal_basis_label, legal_basis_text, notice_url, notice_text, notice_version, updated_by, updated_at) ON assess_privacy_config TO vektra_assess_api",
    "GRANT SELECT ON assess_purge_records TO vektra_assess_api",
    "GRANT INSERT ON assess_render_checks TO vektra_assess_api",
    "GRANT SELECT ON assess_render_checks TO vektra_assess_api",
    "GRANT SELECT ON assess_render_state TO vektra_assess_api",
    "GRANT INSERT ON assess_reset_grants TO vektra_assess_api",
    "GRANT SELECT ON assess_reset_grants TO vektra_assess_api",
    "GRANT INSERT ON assess_sessions TO vektra_assess_api",
    "GRANT SELECT ON assess_sessions TO vektra_assess_api",
    "GRANT UPDATE (epoch, tokens_used) ON assess_sessions TO vektra_assess_api",
    "GRANT INSERT ON assess_setup_check_reports TO vektra_assess_api",
    "GRANT SELECT ON assess_setup_check_reports TO vektra_assess_api",
    "GRANT SELECT ON assess_site_capabilities TO vektra_assess_api",
    "GRANT SELECT ON assess_site_keys TO vektra_assess_api",
    "GRANT SELECT (id, lms_site_uuid, base_url, release, min_plugin_version, created_at, disabled_at, last_seen_release, lms_origin) ON assess_sites TO vektra_assess_api",
    "GRANT INSERT ON assess_start_attempts TO vektra_assess_api",
    "GRANT SELECT ON assess_start_attempts TO vektra_assess_api",
    "GRANT SELECT ON assess_start_breaker TO vektra_assess_api",
    "GRANT SELECT ON assess_teacher_gate TO vektra_assess_api",
    "GRANT INSERT ON assess_terminations TO vektra_assess_api",
    "GRANT SELECT ON assess_terminations TO vektra_assess_api",
    "GRANT INSERT ON assess_token_jti TO vektra_assess_api",
    "GRANT SELECT ON assess_token_jti TO vektra_assess_api",
    "GRANT UPDATE (consumed_at) ON assess_token_jti TO vektra_assess_api",
    "GRANT INSERT ON assess_transcript_download_tokens TO vektra_assess_api",
    "GRANT SELECT ON assess_transcript_download_tokens TO vektra_assess_api",
    "GRANT UPDATE (consumed_at) ON assess_transcript_download_tokens TO vektra_assess_api",
    "GRANT SELECT ON assess_transcripts TO vektra_assess_api",
    "GRANT INSERT ON assess_turn_flags TO vektra_assess_api",
    "GRANT SELECT ON assess_turn_flags TO vektra_assess_api",
    "GRANT INSERT ON assess_turn_notes TO vektra_assess_api",
    "GRANT USAGE ON SEQUENCE assess_audit_events_id_seq, assess_erasure_records_id_seq, assess_jobs_id_seq, assess_model_activations_id_seq, assess_render_checks_id_seq, assess_start_attempts_id_seq TO vektra_assess_api",
    "GRANT SELECT ON assess_activity_config TO vektra_assess_worker",
    "GRANT SELECT ON assess_activity_config_sealed TO vektra_assess_worker",
    "GRANT UPDATE (sealed_blob, kid) ON assess_activity_config_sealed TO vektra_assess_worker",
    "GRANT INSERT ON assess_audit_events TO vektra_assess_worker",
    "GRANT DELETE ON assess_consumed_jtis TO vektra_assess_worker",
    "GRANT SELECT ON assess_consumed_jtis TO vektra_assess_worker",
    "GRANT DELETE ON assess_deliveries TO vektra_assess_worker",
    "GRANT INSERT ON assess_deliveries TO vektra_assess_worker",
    "GRANT SELECT ON assess_deliveries TO vektra_assess_worker",
    "GRANT UPDATE ON assess_deliveries TO vektra_assess_worker",
    "GRANT DELETE ON assess_enrolment_checks TO vektra_assess_worker",
    "GRANT INSERT ON assess_enrolment_checks TO vektra_assess_worker",
    "GRANT SELECT ON assess_enrolment_checks TO vektra_assess_worker",
    "GRANT INSERT ON assess_erasure_records TO vektra_assess_worker",
    "GRANT SELECT ON assess_erasure_records TO vektra_assess_worker",
    "GRANT UPDATE ON assess_erasure_records TO vektra_assess_worker",
    "GRANT DELETE ON assess_extra_time TO vektra_assess_worker",
    "GRANT SELECT ON assess_extra_time TO vektra_assess_worker",
    "GRANT DELETE ON assess_gate_inputs TO vektra_assess_worker",
    "GRANT INSERT ON assess_gate_inputs TO vektra_assess_worker",
    "GRANT SELECT ON assess_gate_inputs TO vektra_assess_worker",
    "GRANT UPDATE ON assess_gate_inputs TO vektra_assess_worker",
    "GRANT DELETE ON assess_heartbeat TO vektra_assess_worker",
    "GRANT INSERT ON assess_heartbeat TO vektra_assess_worker",
    "GRANT SELECT ON assess_heartbeat TO vektra_assess_worker",
    "GRANT UPDATE ON assess_heartbeat TO vektra_assess_worker",
    "GRANT DELETE ON assess_jobs TO vektra_assess_worker",
    "GRANT INSERT ON assess_jobs TO vektra_assess_worker",
    "GRANT SELECT ON assess_jobs TO vektra_assess_worker",
    "GRANT UPDATE ON assess_jobs TO vektra_assess_worker",
    "GRANT SELECT ON assess_model_activations TO vektra_assess_worker",
    "GRANT DELETE ON assess_outage_incidents TO vektra_assess_worker",
    "GRANT INSERT ON assess_outage_incidents TO vektra_assess_worker",
    "GRANT SELECT ON assess_outage_incidents TO vektra_assess_worker",
    "GRANT UPDATE ON assess_outage_incidents TO vektra_assess_worker",
    "GRANT SELECT ON assess_privacy_config TO vektra_assess_worker",
    "GRANT INSERT ON assess_purge_records TO vektra_assess_worker",
    "GRANT SELECT ON assess_purge_records TO vektra_assess_worker",
    "GRANT UPDATE (subject_ref) ON assess_purge_records TO vektra_assess_worker",
    "GRANT DELETE ON assess_render_checks TO vektra_assess_worker",
    "GRANT INSERT ON assess_render_checks TO vektra_assess_worker",
    "GRANT SELECT ON assess_render_checks TO vektra_assess_worker",
    "GRANT UPDATE ON assess_render_checks TO vektra_assess_worker",
    "GRANT DELETE ON assess_render_state TO vektra_assess_worker",
    "GRANT INSERT ON assess_render_state TO vektra_assess_worker",
    "GRANT SELECT ON assess_render_state TO vektra_assess_worker",
    "GRANT UPDATE ON assess_render_state TO vektra_assess_worker",
    "GRANT DELETE ON assess_reset_grants TO vektra_assess_worker",
    "GRANT SELECT ON assess_reset_grants TO vektra_assess_worker",
    "GRANT UPDATE (consumed_at, reason_ct, reason_kid) ON assess_reset_grants TO vektra_assess_worker",
    "GRANT DELETE ON assess_session_handoff TO vektra_assess_worker",
    "GRANT INSERT ON assess_session_handoff TO vektra_assess_worker",
    "GRANT SELECT ON assess_session_handoff TO vektra_assess_worker",
    "GRANT UPDATE ON assess_session_handoff TO vektra_assess_worker",
    "GRANT DELETE ON assess_session_keys TO vektra_assess_worker",
    "GRANT INSERT ON assess_session_keys TO vektra_assess_worker",
    "GRANT SELECT ON assess_session_keys TO vektra_assess_worker",
    "GRANT UPDATE ON assess_session_keys TO vektra_assess_worker",
    "GRANT DELETE ON assess_sessions TO vektra_assess_worker",
    "GRANT INSERT ON assess_sessions TO vektra_assess_worker",
    "GRANT SELECT ON assess_sessions TO vektra_assess_worker",
    "GRANT UPDATE ON assess_sessions TO vektra_assess_worker",
    "GRANT DELETE ON assess_setup_check_reports TO vektra_assess_worker",
    "GRANT INSERT ON assess_setup_check_reports TO vektra_assess_worker",
    "GRANT SELECT ON assess_setup_check_reports TO vektra_assess_worker",
    "GRANT UPDATE ON assess_setup_check_reports TO vektra_assess_worker",
    "GRANT DELETE ON assess_setup_checks TO vektra_assess_worker",
    "GRANT INSERT ON assess_setup_checks TO vektra_assess_worker",
    "GRANT SELECT ON assess_setup_checks TO vektra_assess_worker",
    "GRANT DELETE ON assess_site_capabilities TO vektra_assess_worker",
    "GRANT INSERT ON assess_site_capabilities TO vektra_assess_worker",
    "GRANT SELECT ON assess_site_capabilities TO vektra_assess_worker",
    "GRANT UPDATE ON assess_site_capabilities TO vektra_assess_worker",
    "GRANT SELECT ON assess_site_keys TO vektra_assess_worker",
    "GRANT SELECT ON assess_sites TO vektra_assess_worker",
    "GRANT UPDATE (release, min_plugin_version, last_seen_release, probe_manifest) ON assess_sites TO vektra_assess_worker",
    "GRANT DELETE ON assess_snapshots TO vektra_assess_worker",
    "GRANT INSERT ON assess_snapshots TO vektra_assess_worker",
    "GRANT SELECT ON assess_snapshots TO vektra_assess_worker",
    "GRANT DELETE ON assess_start_attempts TO vektra_assess_worker",
    "GRANT INSERT ON assess_start_attempts TO vektra_assess_worker",
    "GRANT SELECT ON assess_start_attempts TO vektra_assess_worker",
    "GRANT DELETE ON assess_start_breaker TO vektra_assess_worker",
    "GRANT INSERT ON assess_start_breaker TO vektra_assess_worker",
    "GRANT SELECT ON assess_start_breaker TO vektra_assess_worker",
    "GRANT UPDATE ON assess_start_breaker TO vektra_assess_worker",
    "GRANT DELETE ON assess_teacher_gate TO vektra_assess_worker",
    "GRANT INSERT ON assess_teacher_gate TO vektra_assess_worker",
    "GRANT SELECT ON assess_teacher_gate TO vektra_assess_worker",
    "GRANT UPDATE ON assess_teacher_gate TO vektra_assess_worker",
    "GRANT SELECT ON assess_terminations TO vektra_assess_worker",
    "GRANT UPDATE (reason_ct, reason_kid) ON assess_terminations TO vektra_assess_worker",
    "GRANT DELETE ON assess_token_jti TO vektra_assess_worker",
    "GRANT SELECT ON assess_token_jti TO vektra_assess_worker",
    "GRANT DELETE ON assess_transcript_download_tokens TO vektra_assess_worker",
    "GRANT SELECT ON assess_transcript_download_tokens TO vektra_assess_worker",
    "GRANT DELETE ON assess_transcripts TO vektra_assess_worker",
    "GRANT INSERT ON assess_transcripts TO vektra_assess_worker",
    "GRANT SELECT ON assess_transcripts TO vektra_assess_worker",
    "GRANT UPDATE ON assess_transcripts TO vektra_assess_worker",
    "GRANT DELETE ON assess_turn_flags TO vektra_assess_worker",
    "GRANT SELECT ON assess_turn_flags TO vektra_assess_worker",
    "GRANT DELETE ON assess_turn_notes TO vektra_assess_worker",
    "GRANT SELECT ON assess_turn_notes TO vektra_assess_worker",
    "GRANT DELETE ON assess_turns TO vektra_assess_worker",
    "GRANT INSERT ON assess_turns TO vektra_assess_worker",
    "GRANT SELECT ON assess_turns TO vektra_assess_worker",
    "GRANT INSERT (response_id, namespace_id, steps, total_duration_ms, chunks_retrieved, llm_model, prompt_version, created_at, pipeline) ON query_traces TO vektra_assess_worker",
    "GRANT USAGE ON SEQUENCE assess_audit_events_id_seq, assess_enrolment_checks_id_seq, assess_erasure_records_id_seq, assess_jobs_id_seq, assess_model_activations_id_seq, assess_outage_incidents_id_seq, assess_purge_records_id_seq, assess_render_checks_id_seq, assess_setup_checks_id_seq, assess_start_attempts_id_seq TO vektra_assess_worker",
    "GRANT INSERT ON assess_audit_events TO vektra_assess_definer",
    "GRANT INSERT ON assess_deliveries TO vektra_assess_definer",
    "GRANT SELECT ON assess_deliveries TO vektra_assess_definer",
    "GRANT SELECT ON assess_heartbeat TO vektra_assess_definer",
    "GRANT INSERT ON assess_jobs TO vektra_assess_definer",
    "GRANT SELECT ON assess_jobs TO vektra_assess_definer",
    "GRANT INSERT ON assess_outage_incidents TO vektra_assess_definer",
    "GRANT SELECT ON assess_outage_incidents TO vektra_assess_definer",
    "GRANT UPDATE (ended_at, credited_s) ON assess_outage_incidents TO vektra_assess_definer",
    "GRANT DELETE ON assess_session_handoff TO vektra_assess_definer",
    "GRANT SELECT ON assess_session_handoff TO vektra_assess_definer",
    "GRANT SELECT (session_id) ON assess_session_keys TO vektra_assess_definer",
    "GRANT DELETE ON assess_sessions TO vektra_assess_definer",
    "GRANT SELECT ON assess_sessions TO vektra_assess_definer",
    "GRANT UPDATE (seq, last_activity_at, state, close_reason, closed_at, pending_reply_since, epoch, credited_outage_s, deadline_at) ON assess_sessions TO vektra_assess_definer",
    "GRANT SELECT ON assess_snapshots TO vektra_assess_definer",
    "GRANT INSERT ON assess_start_attempts TO vektra_assess_definer",
    "GRANT SELECT ON assess_start_attempts TO vektra_assess_definer",
    "GRANT SELECT ON assess_transcripts TO vektra_assess_definer",
    "GRANT INSERT ON assess_turns TO vektra_assess_definer",
    "GRANT SELECT ON assess_turns TO vektra_assess_definer",
    "GRANT USAGE ON SEQUENCE assess_audit_events_id_seq, assess_jobs_id_seq, assess_outage_incidents_id_seq, assess_start_attempts_id_seq TO vektra_assess_definer",
    "REVOKE ALL ON FUNCTION public.assess_append_turn(uuid,integer,integer,text,text,bytea,text,text) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_append_turn(uuid,integer,integer,text,text,bytea,text,text) TO vektra_assess_api",
    "GRANT EXECUTE ON FUNCTION public.assess_append_turn(uuid,integer,integer,text,text,bytea,text,text) TO vektra_assess_worker",
    "REVOKE ALL ON FUNCTION public.assess_audit_sealed_change() FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "REVOKE ALL ON FUNCTION public.assess_close_session(uuid,integer,text) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_close_session(uuid,integer,text) TO vektra_assess_api",
    "GRANT EXECUTE ON FUNCTION public.assess_close_session(uuid,integer,text) TO vektra_assess_worker",
    "REVOKE ALL ON FUNCTION public.assess_end_outage(uuid,integer) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_end_outage(uuid,integer) TO vektra_assess_worker",
    "REVOKE ALL ON FUNCTION public.assess_enqueue_resend(uuid,text,integer) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_enqueue_resend(uuid,text,integer) TO vektra_assess_api",
    "REVOKE ALL ON FUNCTION public.assess_enter_outage(uuid,integer,timestamp with time zone,text) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_enter_outage(uuid,integer,timestamp with time zone,text) TO vektra_assess_api",
    "REVOKE ALL ON FUNCTION public.assess_metrics_snapshot() FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_metrics_snapshot() TO vektra_assess_api",
    "REVOKE ALL ON FUNCTION public.assess_rollback_starting(uuid) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.assess_rollback_starting(uuid) TO vektra_assess_api",
    "REVOKE ALL ON FUNCTION public.get_session_material(uuid,integer) FROM PUBLIC, vektra_assess_api, vektra_assess_worker",
    "GRANT EXECUTE ON FUNCTION public.get_session_material(uuid,integer) TO vektra_assess_api",
)


def _create_roles() -> None:
    names = ", ".join(f"'{r}'" for r in ROLES)
    op.execute(f"""
        DO $$
        DECLARE
            r text;
        BEGIN
            FOREACH r IN ARRAY ARRAY[{names}] LOOP
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
                    BEGIN
                        EXECUTE format('CREATE ROLE %I {ROLE_ATTRS}', r);
                    EXCEPTION WHEN insufficient_privilege THEN
                        RAISE EXCEPTION '{CREATEROLE_MESSAGE}', r;
                    END;
                    -- PostgreSQL 16: a CREATEROLE non-superuser gets ADMIN on the role it
                    -- creates, not SET; the definer must be SET-able for OWNER TO below
                    IF r = '{DEFINER}' AND NOT pg_has_role(current_user, r, 'SET') THEN
                        EXECUTE format('GRANT %I TO %I', r, current_user);
                    END IF;
                END IF;
            END LOOP;
        END
        $$
    """)


def _function_signatures() -> list[str]:
    sigs = []
    for stmt in FUNCTIONS:
        head = stmt.split("RETURNS", 1)[0]
        sigs.append(head.replace("CREATE FUNCTION", "", 1).strip())
    return sigs


def upgrade() -> None:
    existing = (
        op.get_bind()
        .execute(
            sa.text("SELECT 1 FROM namespaces WHERE id = :id"), {"id": ASSESS_NAMESPACE}
        )
        .first()
    )
    if existing is not None:
        raise RuntimeError(PRE_EXISTING_MESSAGE)
    _create_roles()
    # a pre-created definer the migration user cannot SET ROLE to: stop with the recipe before
    # any DDL, never with the raw ALTER FUNCTION error halfway through
    can_set = (
        op.get_bind()
        .execute(sa.text("SELECT pg_has_role(current_user, :r, 'SET')"), {"r": DEFINER})
        .scalar()
    )
    if not can_set:
        raise RuntimeError(DEFINER_MEMBERSHIP_MESSAGE)
    for stmt in (*PLATFORM, *TABLES, *FOREIGN_KEYS, *FUNCTIONS, *TRIGGERS, POLICY):
        op.execute(stmt)
    # ALTER FUNCTION ... OWNER TO needs CREATE on the schema for the new owner (unless the
    # migration user is a superuser); the matrix below revokes it again
    op.execute(f"GRANT CREATE ON SCHEMA public TO {DEFINER}")
    for sig in DEFINER_OWNED:
        op.execute(f"ALTER FUNCTION {sig} OWNER TO {DEFINER}")
    for sig in DEFINER_TRIGGER_FUNCTIONS:
        op.execute(f"REVOKE EXECUTE ON FUNCTION {sig} FROM PUBLIC")
    for stmt in GRANT_STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    op.execute(f"""
        DO $$
        DECLARE
            n integer;
        BEGIN
            SELECT count(*) INTO n FROM api_keys
             WHERE scopes && ARRAY[{ASSESS_SCOPES}]::text[];
            IF n > 0 THEN
                RAISE EXCEPTION
                    'assess base revision a0001 downgrade refused: % API key(s) hold an '
                    'assess:* scope. Revoke and delete them first.', n;
            END IF;
            IF EXISTS (SELECT 1 FROM public.assess_sessions) THEN
                RAISE EXCEPTION 'assess base revision a0001 downgrade refused: '
                    'assess_sessions holds rows (exam evidence). Export and purge them first.';
            END IF;
        END
        $$
    """)
    tables = ", ".join(
        stmt.split("public.", 1)[1].split(" ", 1)[0]
        for stmt in TABLES
        if stmt.lstrip().startswith("CREATE TABLE")
    )
    op.execute(f"DROP TABLE {tables} CASCADE")
    for sig in _function_signatures():
        op.execute(f"DROP FUNCTION {sig}")
    op.execute("DROP POLICY query_traces_assess_worker_only ON query_traces")
    op.execute(
        "REVOKE ALL ON query_traces FROM vektra_assess_api, vektra_assess_worker, "
        "vektra_assess_definer"
    )
    # the assess traces first: they would otherwise block the namespace delete
    op.execute(f"DELETE FROM query_traces WHERE namespace_id = '{ASSESS_NAMESPACE}'")
    # the upgrade refuses a pre-existing row, so this is the row it created; the marker check is
    # a second lock, never the first
    op.execute(
        f"DELETE FROM namespaces WHERE id = '{ASSESS_NAMESPACE}' "
        "AND config->>'reserved' = 'true'"
    )
    op.execute(
        "ALTER TABLE api_keys DROP CONSTRAINT ck_api_keys_assess_scope_exclusive"
    )
    op.execute("ALTER TABLE api_keys DROP CONSTRAINT ck_api_keys_scopes")
    op.execute(
        f"ALTER TABLE api_keys ADD CONSTRAINT ck_api_keys_scopes CHECK (scopes <@ {_LEGACY_SCOPES})"
    )
    # the roles are cluster-level objects shared with other databases: left in place
