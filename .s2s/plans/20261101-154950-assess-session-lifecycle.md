# Implementation Plan: Assess session lifecycle — tokens, async Start, turns, timing, outage, limits and audit

**ID**: 20261101-154950-assess-session-lifecycle
**Status**: completed
**Branch**: feat/assess-session-lifecycle (PR 8a = M1-M2), then `feat/assess-session-lifecycle-m3-m5` for PR 8b = M3-M5
**Created**: 2026-11-01T14:49:50Z
**Updated**: 2026-11-06T23:00:00Z
**Approved**: 2026-11-01 (coordinator)

## Traceability

**Source**: COMP-002, ARCH-067, ARCH-068 (with ARCH-065, ARCH-079, INT-001, INT-002)
**Source Type**: architecture

## References

### Requirements
- REQ-067: Server-authoritative discussion lifecycle per (student, activity) @.s2s/requirements.md
- REQ-068: Button states and pre-session screen (API side: GET /state) @.s2s/requirements.md
- REQ-069: Server wall-clock time limit with resume @.s2s/requirements.md
- REQ-070: Timeout handling and server-side close of abandoned sessions @.s2s/requirements.md
- REQ-071: Start pre-flight, commit and rollback; failures never consume @.s2s/requirements.md
- REQ-072: Finish action and closing screen (API side) @.s2s/requirements.md
- REQ-073: Contest handling verification (end-to-end checks) @.s2s/requirements.md
- REQ-083: Per-session abuse and cost limits @.s2s/requirements.md
- REQ-084: Moodle 'login as' sessions cannot start or access a discussion @.s2s/requirements.md
- REQ-085: Minimum audit event set for discussions (lifecycle events) @.s2s/requirements.md
- REQ-086: Mid-session provider outage: detection, credit and service_failure close @.s2s/requirements.md
- REQ-091: Manual opening-date fallback (gate evaluation side; the teacher form is plan 11) @.s2s/requirements.md
- REQ-094: Opening message commit point (Part A + Part B within T) @.s2s/requirements.md
- REQ-102: Student in-session controls (API side and inventory) @.s2s/requirements.md
- REQ-103: 'Segnala un errore dell'assistente' marker (could) @.s2s/requirements.md
- BR-006: Discussion eligibility gate (START gates) @.s2s/requirements.md
- BR-007: Effective close required @.s2s/requirements.md
- BR-010: Grade and feedback must be released @.s2s/requirements.md
- BR-014: 'Fully graded' per activity, question type and grading method @.s2s/requirements.md
- NFR-014: Attempt isolation @.s2s/requirements.md
- NFR-018: Discussion latency (should) @.s2s/requirements.md
- NFR-020: Admission limit and expected peak @.s2s/requirements.md

### Architecture
- ARCH-065: Vektra-authoritative split; thin non-authoritative plugin @.s2s/architecture.md
- ARCH-067: Discussion token contract @.s2s/architecture.md
- ARCH-068: DB-enforced lifecycle, async Start, outbox, leased job loop @.s2s/architecture.md
- ARCH-079: Envelope encryption with live-session key hand-off @.s2s/architecture.md
- COMP-002: vektra_assess API (fixed gate order before decrypt) @.s2s/architecture.md
- COMP-003: vektra-assess-worker (handler bodies bound here) @.s2s/architecture.md
- INT-001: Assess REST API @.s2s/architecture.md
- INT-002: API to worker channel via Postgres @.s2s/architecture.md

### Decisions
- ADR-0029: Postgres-native leased job loop @.s2s/decisions/ADR-0029-postgres-native-leased-job-loop-for-assess-with-db.md
- ADR-0030: Discussion token contract and UI embedding (fetch streaming, no query-string tokens) @.s2s/decisions/ADR-0030-discussion-token-contract-and-ui-embedding-separat.md
- ADR-0031: Envelope encryption with live-session key hand-off @.s2s/decisions/ADR-0031-envelope-encryption-with-live-session-key-hand-off.md

### Dependencies
- 20261101-154950-assess-job-loop (plan 3): JobRunner, `enqueue_job` outbox, tick slots whose bodies this plan binds (`preflight`, sweeper, STARTING reaper, outage probe, `reconcile_reply`, enrolment re-check, gate-input refresh / `render_check`)
- 20261101-154950-assess-llm-runner (plan 7): `DiscussionTurnRunner.run_stream` / `run_part_b`, `ProviderOutageError`, `TurnResult` (usage, behaviour_class, call_records, interrupted)
- Transitively: plan 1 (`vektra_shared/tokens.py`, `require_exact_scope`, Hashable `RateLimiter`, settings, ERR_ASSESS_*, DB roles), plan 2 (session/turn/key/hand-off tables, SECURITY DEFINER functions, CLOSED trigger, audit store, grant matrix), plan 4 (WS readers, capability matrix, dev envs + fixture seeding), plan 6 (`capture_snapshot`, `render_part_a`, `assemble_context`)

## Overview

This plan binds the assess feature into a working student flow. It owns the `/api/v1/assess`
student routes in the API process and the worker handler and tick bodies that drive the REQ-067
state machine:

`NOT_ELIGIBLE → ELIGIBLE → STARTING → ACTIVE ⇄ OUTAGE → CLOSED(finished | timed_out |
service_failure | budget_exhausted | terminated_by_teacher | access_revoked)`.

The API mints and verifies discussion tokens, accepts Start asynchronously through the outbox, runs
turns under the live DEK, streams post-checked replies over fetch-compatible SSE, enforces
deadlines lazily and writes content-free audit events, failing closed. The worker runs the
pre-flight that ends in commit or rollback, plus the sweeper, reaper, outage probe, enrolment
re-check and interrupted-reply reconciliation.

Tasks are grouped in five milestones:
1. Walking skeleton, ending in the early 40-Start burst test (B.1 priority 2).
2. Gates.
3. Timing.
4. Outage.
5. Limits and audit, closing with the chaos suite across all six close reasons (B.1 priority 3).

## Design Notes

**Token contract (ARCH-067).** All tokens are signed with the assess signing key (kid in the
header), with `iss=VEKTRA_ASSESS_TOKEN_ISSUER` and `aud=discussion`. Decoding goes only through
`vektra_shared.tokens.decode_jwt` (plan 1), which requires aud, iss, exp, iat, jti and kid.
- *Pre-session*: `{sub, site, cmid, jti, iat, exp ≤ iat+120 s, loginas:false, stage:"pre"}`.
- *Session-bound*: `{sub, site, cmid, sid, seq, epoch, jti, iat, exp ≤ min(deadline+grace,
  iat+VEKTRA_ASSESS_SESSION_TOKEN_TTL_S), stage:"session"}`. `seq` is the session number N; `epoch`
  is the session's revocation counter.

`POST /tokens` requires `require_exact_scope("assess:mint")`, a key bound to `site`, and a body with
exactly `loginas:false` **and** `realuser_id:null`. The check is positive: a missing field, `true`
or a non-null value is a rejection audited as `loginas_rejected`, with 0 rows written. The body
must also carry `plugin_version ≥ VEKTRA_ASSESS_MIN_PLUGIN_VERSION`. A mint inserts a
`assess_token_jti(jti, site, sub, cmid, mint_key_id, minted_at, expires_at, consumed_at)` row. That
row is both the single-use store and the mint-to-Start correlation record: Start rejects a jti that
has no row, a row whose `mint_key_id` is not the site's current mint key, or a row that is already
consumed.

The epoch is bumped by revocations only: teacher terminate, close, access revoked and signing-key
rotation. Resume does not bump it, so two tabs of the same owner stay valid and are serialized
(REQ-069). Resume uses `GET /sessions/{sid}` with a fresh pre-session token, which consumes the jti
and returns a re-minted session token. A CLOSED session returns 409 and is never re-minted.
No route reads a token from the query string or from cookies.

**Fixed gate order (COMP-002), before any decrypt, on every session route.**
1. Signature, kid and aud.
2. Binding claims (stage, site, cmid, sub).
3. Live row under `SELECT … FOR UPDATE` via the plan-2 function, matching (sid, seq, epoch, owner,
   site).
4. State.
5. Deadline and C cap; this is where the lazy close runs.
6. Enrolment freshness: the last enrolment re-check is at most 150 s old.

Only then does the API call `get_session_material(sid, epoch)`. Any failure is an in-session gate
rejection: the request is refused, the state is unchanged and an audit event is written (REQ-067).
Client-sent attempt or Moodle ids are never read; the attempt is selected server-side (BR-012,
plan 4).

**Start (ARCH-068).** `POST /sessions` with a pre-session token runs one transaction:
- consume the jti;
- INSERT an `assess_sessions` row in STARTING, where a partial unique index on (site, sub, cmid)
  WHERE state <> 'CLOSED' guarantees exactly one;
- INSERT an `assess_start_attempts` row;
- `enqueue_job('preflight', sid)` (INSERT + NOTIFY);
- write the `start` audit event.

It returns 202 with the sid and a session-bound token. A cheap advisory refusal happens first, with
no row written: the per-(student, activity) Start cap, the per-site breaker, admission at or over K,
and stale or negative `render_state`. Each refusal returns its REQ-071 message. The worker
pre-flight is authoritative.

**Pre-flight handler (worker, budget < T).** `start_requested_at` is the reference point, and every
step re-validates against authoritative rows rather than trusting job args.
1. Admission count under advisory lock (ACTIVE + OUTAGE + STARTING-in-pre-flight ≤ K).
2. Model activation (plan 7 NFR-023 gate).
3. Fresh gate inputs via plan-4 readers: BR-006 (a)-(h) with (i), the BR-007 effective close, the
   REQ-091 attested-date path when any input is Unreadable (gate = max(automatic, teacher date);
   no teacher date means NOT_AVAILABLE), BR-010 release and BR-014 fully graded.
4. Snapshot via plan 6.
5. DEK generation, `kek_wrapped`, AEAD snapshot.
6. Part A via `render_part_a`; Part B via `run_part_b`.
7. One commit transaction: Part A and Part B turns, `committed_at`, deadline, the hand-off row,
   state ACTIVE and the `commit` audit event, guarded by `now() - start_requested_at < T`.

Any server-attributable failure deletes the STARTING row, the snapshot, the keys and any turns in
one transaction, and writes `start_rollback` with a reason code. The record is ELIGIBLE again and
the row counts are verified. A client disconnect never triggers rollback, because nothing on the
client path does.

**Turns (CONF-007: in the API).** `POST /sessions/{sid}/turns` runs the gates, then the REQ-083
pre-LLM checks (length, rate, budget), then `assess_append_turn` for the student turn, which sets
`pending_reply_since`. A second send while a reply is pending gets 409 "attendi la risposta". The
reply runs in a detached API task (`TurnDriver`) that is independent of the HTTP connection. It
pushes released segments to an in-process per-session `EventBroker`, then persists the bot turn
plus its hash via `assess_append_turn`. Plaintext exists only in memory and is never persisted
chunk-wise.

`GET /sessions/{sid}/events` is a `text/event-stream` with monotonic per-session ids and supports
`Last-Event-ID`. Reconnect yields a `state` snapshot event; persisted turns come from
`GET /sessions/{sid}`. Event set, owned here:
`state`, `committed`, `start_rolled_back`, `capacity_refused`, `turn_accepted`, `reply_delta`,
`reply_done`, `reply_interrupted`, `warning`, `budget_warning`, `outage_start`, `outage_end`,
`closed`. Payloads are content-free, except `reply_delta`, which carries post-checked text only.

**Timing.** Deadline = `committed_at + cap + extra_time` (frozen at Start from plan-11
`assess_extra_time`, default 0) + credited outage seconds. Warnings at 10, 5 and 1 minutes, with 10
only when cap ≥ 30 min, are computed against the current deadline. `TurnDriver` emits each once per
(sid, threshold, deadline). At the deadline, input locks. An in-flight reply may finish until
deadline + grace (60 s). Otherwise `TurnDriver` cancels it and persists a marked
"risposta interrotta" turn. `assess_append_turn` refuses bot turns after deadline + grace (DB
clock). If the API crashes mid-reply, the worker's `reconcile_reply` job finds a pending turn older
than outage timeout + grace with no in-flight claim and appends the marked turn under the KEK.

The sweeper (10-15 s) closes past-deadline sessions as `timed_out` and never touches OUTAGE. The API
closes lazily on any request (allowed by ARCH-079). Every close goes through one plan-2 close
function: state CLOSED + reason, hand-off DELETE, epoch bump, `enqueue_job('transcript_generate')`
and the `close` audit event in the same transaction.

**Outage (REQ-086).** A `ProviderOutageError` from the runner moves the session ACTIVE → OUTAGE via
the plan-2 function and records an `assess_outage_intervals` row from the server-side
`ProviderCallRecord`. The timer freezes and input locks. The pending student turn is kept.

The worker outage probe (every 5-10 s, only while a session on that provider is in OUTAGE) runs a
minimal call (`max_tokens=1`). On success it ends OUTAGE: `ended_at` is set, credited =
duration if ≥ G (20 s) else 0, capped so that the cumulative credit stays ≤ C = min(15 min, 50% of
cap). Credit stops at the probe success, `outage_end` is audited and the session returns to ACTIVE.
The API-side `PendingTurnDriver` then answers the pending turn without a client.

`POST /retry` only asks the driver to attempt now (rate-limited) and never ends OUTAGE itself.
Cumulative outage > C, or 3 consecutive failed calls, closes the session as `service_failure` and
enqueues the teacher notification within 1 min (delivery: plan 10). Refusals, over-length, rate,
budget and content-filter outcomes earn no credit.

**Limits (REQ-083).** Defaults are sized in task M5.1 and operator-configurable:
- max message chars;
- messages per minute per session (generalized `RateLimiter` keyed by sid);
- session token budget from `TurnUsage`, excluding outage retries and earlier-session turns, with
  `budget_warning` at 80% and `budget_exhausted` at 100%;
- per-token rate limits on `/state`, `/turns`, `/flag` and `/retry` (keyed by jti or sid);
- the Start cap of 5 per (student, activity) per hour from `assess_start_attempts`;
- after 3 consecutive rollbacks, the "Problema tecnico persistente" message plus teacher and
  operator notification;
- a per-site Start breaker: it opens after `VEKTRA_ASSESS_START_BREAKER_ROLLBACKS` system rollbacks
  in a window and fast-refuses Starts without consuming anything.

The RateLimiter instances are assess-local. The dormant global `app.state.rate_limiter` stays unset.

**Audit (REQ-085).** `AssessAuditEvent` is an enum, and events are written in the same transaction
as the state change through the plan-2 audit store. If the audit write fails, the action aborts and
returns 503 ERR_ASSESS_001: it fails closed, unlike the swallowed `vektra_admin.audit.log_event`.
Payloads hold ids, timestamps and reason codes only. The `start` event carries the privacy-notice
version.

## Tasks

### M1 — Walking skeleton (thin Start path → burst test)
- [x] M1.1 `vektra_assess/api/router.py` mounted at `/api/v1/assess` by `create_app` (only when `VEKTRA_ASSESS_ENABLED`); Pydantic request/response models in `api/models.py`; error codes ERR_ASSESS_002..0NN with `_CODE_STATUS_OVERRIDE` entries (401 token, 403 loginas/scope/site, 404 unknown or not-owned sid, 409 closed/pending/state, 429 rate/cap with `retry_after`, 503 capacity/unavailable/breaker); 400 envelopes for over-length (avoid the 422 bypass, DEBT-035)
- [x] M1.2 `vektra_assess/tokens.py`: `mint_presession()`, `mint_session()`, `verify(stage=…)` on top of `vektra_shared.tokens`; settings `VEKTRA_ASSESS_PRESESSION_TTL_S` (max 120, startup refuses larger), `VEKTRA_ASSESS_SESSION_TOKEN_TTL_S`, `VEKTRA_ASSESS_TOKEN_ISSUER`; signing key from the plan-1 keyring (kid rotation: verify accepts current + previous kid)
- [x] M1.3 `POST /tokens` (`require_exact_scope("assess:mint")`, site binding, positive login-as assertion, `plugin_version` minimum, `assess_token_jti` insert, `token_mint` audit); verify `assess_token_jti` exists in the plan-2 schema, else add it in the next revision in the chain
- [x] M1.4 `POST /sessions` Start transaction (jti consume, STARTING insert under the partial unique index, `assess_start_attempts`, `enqueue_job('preflight')`, `start` audit with notice version) → 202 + sid + session token; concurrent-Start test (20 parallel requests, same sub/cmid) → exactly 1 non-CLOSED row
- [x] M1.5 Bind the `preflight` handler (`vektra_assess/worker/preflight.py`) — thin version: activation check, admission check, snapshot (plan 6), DEK + `kek_wrapped` + hand-off, Part A, `run_part_b`, commit transaction with `now()-start_requested_at < T`; rollback transaction deleting session/snapshot/keys/turns + `start_rollback(reason)`; handler budget = T − margin, lease from plan 3 (≈ 90 s)
- [x] M1.5a Assess model reaches the production vLLM (OQ-026): `build_assess_llm` (`vektra-app/src/vektra_app/assess_llm.py`) takes the API key from `VEKTRA_LLM_API_KEY`, the same key the RAG pipeline uses today (user decision 2026-11-06: no new secret), instead of forcing None; new setting `VEKTRA_ASSESS_LLM_EXTRA_BODY` (JSON, default none) passed as `extra_body` (Qwen3.6 needs `chat_template_kwargs.enable_thinking=false`); the worker container gets both through its environment (it runs Part B and the outage probe); the RAG fallback model still never leaks in; tests + docs (`configuration.md`, `.env.example`)
- [x] M1.6 Bind the STARTING reaper tick: STARTING rows with `start_requested_at + T` passed and no live lease → rollback path (`reason=start_timeout`); test with a stalled fake LLM
- [x] M1.7 `GET /sessions/{sid}` (owner only): state, deadline, server_now, turns (decrypted via `get_session_material`, gate order enforced); with a pre-session token → resume path that consumes jti and re-mints the session token; CLOSED → 409
- [x] M1.8 `vektra_assess/api/events.py`: `EventBroker` (per-sid bounded ring, monotonic ids, evict on CLOSED), `GET /sessions/{sid}/events` SSE with `Last-Event-ID`, keep-alive comments, state polling of the session row via the plan-2 status function (1 s), `committed` / `start_rolled_back` / `closed` events
- [x] M1.9 `POST /sessions/{sid}/turns` thin path + `TurnDriver` (student append → `run_stream` → `reply_delta`/`reply_done` → bot append with hash; `pending_reply_since` serialization; second send → 409); DEK cache per (sid, epoch), evicted on epoch bump/CLOSED/TTL
- [x] M1.10 Test-only seeding helper `vektra-assess/tests/support/sessions.py` (seed ELIGIBLE/ACTIVE/OUTAGE/CLOSED sessions with a fake LLM) for this plan and plan 9's e2e
- [x] M1.11 **Early burst test** `tests/nfr/test_assess_burst.py` + `scripts/assess_burst.py` (httpx async): 40 concurrent Starts, each with a 10-file assignment submission seeded by plan 4's fixture tooling on the dev Moodle, parser sidecar (plan 5) and the target LLM (OQ-026 candidate); record per-Start mint→202→commit latency from audit timestamps, outcomes (commit / capacity refusal / rollback reason), worker RSS, and row counts (sessions, snapshots, keys, hand-off) after the run; result file under `tests/nfr/results/`; run as soon as M1.5 works, re-run at the end of M5

### M2 — Gates and isolation
- [x] M2.1 `vektra_assess/gates.py`: pure `evaluate_start_gates(inputs) -> GateDecision(code)` for BR-006 (a)-(i), BR-007 (quiz close + grace; assignment cut-off required), BR-010, BR-014 (quiz requires-grading = 0 incl. regrade-in-progress; rubric every criterion has a level; scale; marking guide refused), REQ-091 (any Unreadable input → fallback: NOT_AVAILABLE without a teacher date; gate = max(automatic, teacher date) when readable again; never the activity close alone); fixture table test with every gate negated individually
- [x] M2.2 Pre-flight calls the plan-4 readers fresh and `evaluate_start_gates`; a failed gate → rollback with the gate code; team submission → `team_submission_refused` audit
- [x] M2.3 Bind the gate-input refresh tick and `render_check` job (worker writes `render_state` / `gate_inputs`); `GET /state` returns the advisory enum + reason code for (token sub, cmid), dedupes `render_check` per (user, cmid), per-token rate-limited; another student's override never leaks a date, name or count (response and HTML fixture grep)
- [x] M2.4 Enforce the fixed gate order in one dependency `session_gate(stage)` used by every session route; a test per step asserting that `get_session_material` is called 0 times when an earlier step fails (spy on the DB function)
- [x] M2.5 REQ-084: login-as rejection on mint and on any token lacking `loginas:false`; `loginas_rejected` audit; test that 0 session rows are created and the record is unchanged; record OQ-031(2) cross-check capability (default Unreadable) as residual risk
- [x] M2.6 IDOR / NFR-014 negative matrix `vektra-assess/tests/test_idor.py`: token for A with B's sid; other site; other cmid; other course; expired; replay after CLOSED; unenrolled; forged Moodle attempt id in body (ignored); each → rejected, audited, 0 cross-student bytes (response body grep for B's canary); run against 4.3 and 5.2 fixtures in the integration job
- [x] M2.7 No query-string tokens: route-inventory test that every assess route rejects `?token=`/`?access_token=` without header (401) and that no handler reads `request.query_params`/cookies for credentials (shared with plan 9's inventory test; whichever lands first owns it)

### M3 — Timing, resume, finish, abandon
- [x] M3.1 Deadline computation in one function (`committed_at + cap + extra_time + credited`), extra time frozen at Start from `assess_extra_time` (plan 11 table; until it exists, 0 with a TODO-free explicit default); boundary tests ±1 s UTC; client time ignored (no request field accepted)
- [x] M3.2 Warnings 10/5/1 from `TurnDriver` timer per live session (emitted once per (sid, threshold, deadline)); tests with outage credit X: fire at new deadline − 10/5/1 min ±5 s, none at the old deadline
- [x] M3.3 Input lock at the deadline; grace window `VEKTRA_ASSESS_REPLY_GRACE_S` (60): in-flight reply finishes or is cut → marked "risposta interrotta"; slowed fake-provider tests for both branches; `assess_append_turn` refuses bot turns after deadline + grace (DB test)
- [x] M3.4 Lazy enforcement on every API request: past deadline (not OUTAGE) → close `timed_out`; C exceeded → `service_failure`; STARTING past T → reported as rolled back (the reaper does the deletion)
- [x] M3.5 Bind the sweeper tick: close `timed_out` (deadline + grace passed, not OUTAGE), never OUTAGE; browser-closed test: CLOSED(timed_out) and a `transcript_generate` job exist within 5 min of the deadline
- [x] M3.6 Bind `reconcile_reply` (worker, KEK path): pending student turn with no bot turn after outage timeout + grace and no live driver claim → append marked interrupted turn; kill -9 API mid-reply test → exactly one marked turn
- [x] M3.7 Per-turn enrolment freshness (150 s) gate step; bind the enrolment re-check tick (60 s, plan-4 reader) writing `enrolment_checks`; unenrolled or lost student role → close `access_revoked` + transcript job; worker stopped > 150 s → turns rejected (fail closed, state unchanged, audited)
- [x] M3.8 `POST /sessions/{sid}/finish` → close `finished`; idempotent (Fine twice → one close, one transcript job; row count); no "cancel" endpoint needed (confirmation is UI-only, deadline unchanged)
- [x] M3.9 Resume test: reload → same sid, full history, remaining time within 1 s of the server; two tabs → second send refused, 0 duplicate turns; after CLOSED → message, re-mint and new Start rejected unless a reset created #N+1 (plan 11 reset fixture)

### M4 — Outage
- [x] M4.1 ACTIVE → OUTAGE on `ProviderOutageError` (plan-2 function), `assess_outage_intervals` row from `ProviderCallRecord`, `outage_start` audit + event; timer frozen; pending turn kept
- [x] M4.2 Bind the outage probe tick (per provider, 5-10 s, only while ≥ 1 OUTAGE session; `max_tokens=1`; litellm `health_check` NOT used — it makes a full paid completion); success → end intervals, credit (≥ G only, cumulative ≤ C), `outage_end(credited_s)` audit + event, state ACTIVE
- [x] M4.3 `PendingTurnDriver` in the API: started from `vektra_app.main` server startup (lifespan is off, BUG-025), claims ACTIVE sessions with a pending turn and no live driver via `pg_try_advisory_xact_lock(sid)`; answers without a client
- [x] M4.4 `POST /sessions/{sid}/retry`: rate-limited, only nudges the driver/probe; never changes state by itself (test: retry with provider still down → still OUTAGE, credit unchanged)
- [x] M4.5 `service_failure` close: cumulative outage > C, or 3 consecutive failed calls; transcript job + teacher notification job enqueued in the same transaction (delivery by plan 10, within 1 min); service_failure message code
- [x] M4.6 Outage test matrix with a fault-injecting fake provider: 3-min outage → +3 min ±5 s; 15 s → +0; slow 40 s success → +0; over-length / rate / content-filter → +0; > C → service_failure + intervals recorded; browser killed at outage start → ACTIVE (answered) or CLOSED(service_failure) within C; recovery with client absent → OUTAGE ends within the probe interval and the session later closes `timed_out`; sweeper never closes OUTAGE; refusal turns never set OUTAGE

### M5 — Limits, audit, verification, chaos
- [x] M5.1 Size REQ-083 defaults on a scripted reference 120-min session (turn count/length distribution from the eval set) against the plan-7 `TurnUsage` estimates; document in `docs/reference/configuration.md`; settings `VEKTRA_ASSESS_MAX_MESSAGE_CHARS`, `VEKTRA_ASSESS_MESSAGES_PER_MIN`, `VEKTRA_ASSESS_SESSION_TOKEN_BUDGET`
- [x] M5.2 Pre-LLM rejections (over-length → 0 LLM calls on the recording provider; rate-limited → "Attendi qualche secondo"; deadline +0 asserted); budget warning at 80%, `budget_exhausted` close + exactly one transcript job
- [x] M5.3 Per-token rate limiters for `/state`, `/turns`, `/retry`, `/flag` (assess-local `RateLimiter` instances, Hashable keys from plan 1); assert `app.state.rate_limiter` still unset (dormant global per-key RPM not armed)
- [x] M5.4 Start cap (5/h per student × activity) and 3-consecutive-rollback rule ("Problema tecnico persistente" code; teacher + operator notification jobs); per-site Start breaker (worker-written state, API fast refusal, half-open after cooldown, audited `start_breaker_open/close`)
- [x] M5.5 Mint-to-Start correlation: reject + audit Starts whose jti has no mint row from the site's current mint key; test with a token signed by the correct key but never minted through `/tokens`
- [x] M5.6 `vektra_assess/audit.py` event enum + in-transaction writer (fail closed); events owned here: token_mint, start (+notice version), start_rollback(+reason), start_rate_cap_hit, rollback_cap_hit, commit, outage_start/outage_end(+credited s), close(+reason), in_session_gate_rejected, loginas_rejected, ownership_mismatch_rejected, attempt_mismatch_rejected, team_submission_refused, budget_warning, flag_added; a test drives each and asserts exactly one row; audit-store failure → action aborted (row counts unchanged)
- [x] M5.7 Canary grep: after the full lifecycle suite, grep audit rows, structlog output (all levels, not only WARNING+), Prometheus `/metrics` and `assess_jobs` args for every snapshot/turn canary → 0 hits
- [x] M5.8 REQ-102 API side: route inventory has no regenerate/edit/delete-turn/upload endpoint; each turn's hash recorded at persist (consumed by plan 10 re-hash)
- [x] M5.9 REQ-103 (could; last in order, skip and record if time-boxed out): `POST /sessions/{sid}/turns/{tid}/flag` on bot turns, ACTIVE/OUTAGE only, exactly one marker (unique constraint, idempotent), `flag_added` audit; after CLOSED → 409
- [x] M5.10 REQ-073 end-to-end on the dev Moodle (4.3 and 5.2): Moodle grade, rubric and feedback byte-identical before/after a full session (plan-4 reader dump hash); the adversarial suite verdict counts come from the plan-7 eval report; service-account grade-write absence from the plan-4 setup check
- [x] M5.11 Chaos suite `tests/chaos/assess/test_lifecycle_chaos.py` (on plan 3's harness): each of the six close reasons end-to-end (finished, timed_out, service_failure, budget_exhausted, terminated_by_teacher via the plan-2 close function stub until plan 11, access_revoked) asserting by row count: hand-off rows 0, exactly 1 `transcript_generate` job, epoch bumped, old session token rejected; kill -9 worker mid-pre-flight → ELIGIBLE with snapshot/key/turn rows 0; kill -9 API mid-reply → one interrupted turn; Postgres restart mid-turn → no duplicate turn, state consistent
- [x] M5.12 Re-run the M1.11 burst test with all gates, limits and audit enabled; record NFR-018 commit percentiles and NFR-020 capacity refusals; set the documented K with evidence
- [x] M5.13 CI wiring: assess job in `ci-unit.yml` runs unit tests; integration tests (`@pytest.mark.integration`, testcontainers pg16) picked up by the package-integration glob; chaos and burst as manual/nightly workflow jobs with explicit SKIPPED/BLOCKED output; `make test` includes the unit suite (structural guard `test_suite_execution_coverage.py`)
- [x] M5.14 Docs: `docs/reference/api.md` assess student endpoints, token claims, SSE event catalogue and error codes; `.env.example` (`VEKTRA_ASSESS_MIN_PLUGIN_VERSION`, `VEKTRA_ASSESS_START_T_S`=60, `VEKTRA_ASSESS_REPLY_GRACE_S`=60, `VEKTRA_ASSESS_OUTAGE_G_S`=20, `VEKTRA_ASSESS_OUTAGE_CAP_MAX_S`=900, `VEKTRA_ASSESS_OUTAGE_PROBE_S`, `VEKTRA_ASSESS_ADMISSION_K`=10, `VEKTRA_ASSESS_START_RATE_PER_HOUR`=5, `VEKTRA_ASSESS_ROLLBACK_NOTIFY_AFTER`=3, `VEKTRA_ASSESS_ENROLMENT_FRESHNESS_S`=150, `VEKTRA_ASSESS_START_BREAKER_*`, limits from M5.1); CHANGELOG [Unreleased]

## State & Data Lifecycle

| State element | Created by | Updated by | Invalidated/Deleted by |
|---------------|-----------|------------|------------------------|
| `assess_token_jti` rows | M1.3 mint | M1.4 Start / M1.7 resume (`consumed_at`) | Cleanup in the plan-3 done-job tick extended here: rows with `expires_at < now() - 1 day` deleted (task M1.3 adds the predicate) |
| `assess_sessions` STARTING row | M1.4 Start | M1.5 commit → ACTIVE | M1.5 rollback / M1.6 reaper DELETE (row count verified) |
| `assess_sessions` ACTIVE/OUTAGE/CLOSED | M1.5 commit | M3/M4 transitions via plan-2 functions; epoch bump on every close | Purge by 20261101-154950-assess-privacy-retention |
| Snapshot, `assess_session_keys` | M1.5 pre-flight | never (immutable) | Rollback (M1.5/M1.6) here; purge (crypto-shred) by plan 12 |
| `assess_session_handoff` | M1.5 commit | never (re-seal only by the plan-2 key-rotation job) | Every close path (plan-2 close function + trigger); verified 0 rows in M5.11 |
| Turns (ciphertext + hash) | M1.5 (Part A/B), M1.9, M3.6 | never (append-only; REQ-103 marker is a separate row) | Rollback here; purge plan 12 |
| `pending_reply_since` | M1.9 student append | cleared by bot append / interrupted append | M3.3, M3.6 |
| `assess_start_attempts` | M1.4 | — | Rows older than the Start-cap window + 1 day deleted by the done-job cleanup tick (M5.4 adds the predicate) |
| `assess_outage_intervals` | M4.1 | M4.2 (`ended_at`, credited) | Purge with the session (plan 12) |
| `enrolment_checks`, `render_state`, `gate_inputs` | M2.3 / M3.7 worker ticks | each tick (upsert) | Overwritten; rows of CLOSED sessions/unused (user, cmid) pairs older than 7 days deleted by the refresh tick |
| Site Start breaker state | M5.4 worker | worker on rollback/cooldown | Reset on cooldown; row per site |
| In-memory DEK cache (API) | M1.9 first decrypt | — | Epoch bump, CLOSED, TTL > deadline, process exit |
| In-memory `EventBroker` ring per sid | M1.8 | each event | CLOSED + drain, idle TTL, process exit (content lost on crash is reconciled by M3.6) |
| `TurnDriver` / `PendingTurnDriver` tasks | M1.9 / M4.3 | — | Turn completion, cancel at deadline + grace, process shutdown (advisory xact lock released) |
| Assess-local RateLimiter windows | M5.3 | each request | `cleanup()` on a timer; process exit |

## Acceptance Criteria

- [x] 20 parallel Starts for one (student, activity) → exactly 1 non-CLOSED row; Fine twice → 1 close + 1 transcript job (REQ-067) — evidence: vektra-assess/tests/integration/test_session_start.py::test_twenty_parallel_starts_leave_exactly_one_open_row, vektra-assess/tests/integration/test_session_finish.py::test_fine_twice_is_one_close_and_one_transcript_job
- [x] Negating any single START gate (fixture table) → the `/state` button state changes AND a crafted Start ends in rollback with that gate code, 0 session rows left (REQ-067, BR-006, BR-007, BR-010, BR-014, REQ-091) — evidence: vektra-assess/tests/integration/test_session_state.py::test_a_negated_gate_changes_the_button_and_refuses_a_crafted_start (4 recorded Moodle gates), vektra-assess/tests/integration/test_session_preflight_gates.py (every gate code incl. end date, enrolment, privacy, REQ-091, BR-007 cut-off), vektra-assess/tests/integration/test_session_preflight.py::test_every_failure_rolls_back_to_eligible
- [x] `consumed == exists(persisted opening bot turn)` holds for every session row after the full suite (SQL check) (REQ-067) — evidence: vektra-assess/tests/integration/conftest.py::consumed_invariant (CONSUMED_INVARIANT checked after every integration test that uses pg; full run 2026-11-06: 371 passed, 0 violations), vektra-assess/tests/integration/test_session_consumed_invariant.py::test_every_lifecycle_exit_keeps_consumed_equal_to_an_opening_turn
- [x] Provider stopped / WS unreachable / K reached / Part B forced to fail / T exceeded → record ELIGIBLE, 0 snapshot, key and hand-off rows, `start_rollback` with reason, operator alert on WS failure (REQ-071, REQ-094) — evidence: vektra-assess/tests/integration/test_session_preflight.py::test_every_failure_rolls_back_to_eligible (provider_stopped, ws_unreachable, capacity, part_b_rejected, timeout: ELIGIBLE, 0 snapshot / key / hand-off rows, start_rollback with reason), vektra-assess/tests/integration/test_session_start_guard.py::test_moodle_unreachable_alerts_the_operator_once_per_window (operator alert)
- [x] Client disconnect after Start → session commits; the opening message is visible on resume; no attempt content or bot text in any response before commit (byte grep of 202 and event payloads) (REQ-071) — evidence: vektra-assess/tests/integration/test_session_get.py::test_a_client_gone_after_start_still_commits_and_resumes (byte grep of the 202), vektra-assess/tests/integration/test_session_events_sse.py::test_starting_then_committed_then_closed (byte grep of the event payloads)
- [x] 6th Start within an hour → refused 429; 3 consecutive rollbacks → teacher + operator notification jobs (REQ-071) — evidence: vektra-assess/tests/integration/test_session_start_guard.py::test_the_sixth_start_within_an_hour_is_refused_and_consumes_nothing, ::test_three_consecutive_system_rollbacks_notify_teacher_and_operator_once
- [x] Login-as mint or token → refused, `loginas_rejected` audited, 0 session rows (REQ-084) — evidence: vektra-assess/tests/integration/test_session_loginas.py::test_a_loginas_presession_token_starts_nothing, ::test_a_loginas_session_token_reads_and_writes_nothing, vektra-assess/tests/integration/test_session_mint.py
- [x] IDOR matrix on 4.3 and 5.2 → every case rejected and audited; 0 cross-student bytes (NFR-014) — evidence: vektra-assess/tests/integration/test_idor_live.py::test_idor_matrix_live on the live 4.3 and 5.2 dev sites (2/2, 2026-11-08, W3 gate follow-up) + vektra-assess/tests/test_idor.py::test_idor_matrix on the recorded responses
- [x] No assess route accepts a token from the query string or cookies (ADR-0030) — evidence: vektra-assess/tests/integration/test_route_credentials.py::test_no_route_accepts_a_query_string_or_cookie_credential
- [x] Reload resumes the same sid with full history and server time ±1 s; second tab send refused; own 3-min network drop → deadline +0 (REQ-069) — evidence: vektra-assess/tests/integration/test_session_resume.py::test_reload_two_tabs_and_after_the_close (same sid, full history, remaining_s ±1 s, second tab 409, deadline unchanged), vektra-assess/tests/integration/test_session_turns.py::test_a_second_send_while_pending_is_409_and_adds_nothing
- [x] No student message accepted after the deadline; no bot turn stored after deadline + grace; both grace branches verified; browser closed → CLOSED(timed_out) + transcript job within 5 min (REQ-070) — evidence: vektra-assess/tests/integration/test_session_turns.py::test_input_is_locked_from_the_deadline, ::test_a_reply_in_flight_at_the_deadline_finishes_within_the_grace, ::test_a_reply_past_the_grace_is_cut_and_marked, ::test_the_database_enforces_the_input_lock_and_the_grace, vektra-assess/tests/integration/test_session_sweeper.py::test_a_browser_closed_session_is_closed_with_its_transcript_job (sweeper tick 10-15 s: vektra-assess/tests/test_worker_slots.py)
- [x] Outage matrix (M4.6) passes exactly with the stated ±5 s tolerances; no session stays in OUTAGE beyond C; the sweeper never closes OUTAGE (REQ-086) — evidence: vektra-assess/tests/integration/test_session_outage_matrix.py (12 cases), vektra-assess/tests/integration/test_session_service_failure.py::test_cumulative_outage_above_c_closes_through_the_probe_tick, vektra-assess/tests/integration/test_session_sweeper.py
- [x] Over-length → 0 LLM calls; budget exhausted → CLOSED(budget_exhausted) + 1 transcript job; rate-limited / over-length → deadline +0 (REQ-083) — evidence: vektra-assess/tests/integration/test_session_limits.py::test_over_length_and_blank_are_refused_before_any_model_call, ::test_messages_per_minute_answers_wait_a_few_seconds, ::test_budget_warning_at_80_and_budget_exhausted_at_100
- [x] Each owned REQ-085 event produces exactly one audit row; an audit-store failure aborts the action; canary grep of audit, logs, metrics and job args → 0 (REQ-085) — evidence: vektra-assess/tests/integration/test_session_audit.py::test_each_lifecycle_event_writes_exactly_one_row, ::test_an_audit_store_failure_aborts_the_action, vektra-assess/tests/integration/test_session_canary.py::test_no_canary_outside_the_ciphertext
- [x] Chaos suite: six close reasons + rollback verified by row counts (hand-off 0, transcript job 1, epoch bumped) (B.1 priority 3) — evidence: tests/chaos/assess/test_lifecycle_chaos.py::test_each_close_reason_by_row_count, ::test_kill9_mid_preflight_commit_leaves_the_student_eligible (chaos run 2026-11-06: 23 passed)
- [x] Moodle grade/rubric/feedback byte-identical before and after a session on 4.3 and 5.2 (REQ-073) — evidence: vektra-assess/tests/integration/test_req073_live.py::test_a_full_session_leaves_moodle_byte_identical and ::test_the_service_account_cannot_write_grades on the live 4.3 and 5.2 dev sites (4/4, 2026-11-08, W3 gate follow-up); plus the setup check refusing a grade-write function (test_moodle_setup_check.py::test_bad_grader_fails_no_grade_write_by_name)
- [x] API route inventory contains no regenerate/edit/delete/upload; turn hashes stored at persist (REQ-102); flag after CLOSED → 409, one flag → one marker (REQ-103, if built) — evidence: vektra-assess/tests/integration/test_route_credentials.py::test_req102_no_route_regenerates_edits_deletes_or_uploads, vektra-assess/tests/integration/test_session_turns.py::test_each_turn_hash_is_recorded_and_chained_at_persist, vektra-assess/tests/integration/test_session_flag.py::test_a_flag_after_closed_is_rejected, ::test_one_flag_is_exactly_one_marker_and_the_hashes_stay_equal
- [ ] Burst: 40 concurrent Starts with 10-file submissions recorded; commit p50 ≤ 10 s, p95 ≤ 30 s, max < 60 s at the chosen K, or the breach reported per provider/model; excess Starts refused without consumption (NFR-018, NFR-020) — BLOCKED: no target configured (API, mint key, DSN, dev Moodle, model; tests/nfr/results/assess-burst-20261106T201527Z.json); verified instead: vektra-assess/tests/integration/test_session_burst_inprocess.py (40 Starts, K=10, 10 commits, 30 capacity, 0 rows left), with no NFR latency evidence

## Testing Approach

- **Unit (hermetic)**: token mint/verify matrix, gate evaluation table, deadline and credit
  arithmetic with a fake clock, the gate-order spy, the event broker, limiters, the audit enum
  coverage and the route inventory. The fake LLM and recording providers come from plan 7.
- **Integration (testcontainers pg16, `@pytest.mark.integration`)**: the real plan-2 schema,
  running as the restricted API and worker roles. It covers the Start race, commit and rollback row
  counts, `assess_append_turn` deadline enforcement, the close function, the lazy close, the
  sweeper, the reaper, outage transitions and the audit fail-closed path. API and worker run
  in-process against the same DB, with the WS readers faked at the plan-4 interface.
- **Dev-Moodle end-to-end (4.3 and 5.2, plan 4 envs)**: the IDOR matrix, gate fixtures (BR-014
  essay, regrade, rubric gap, overrides, unreadable extensions → REQ-091) and the REQ-073 byte
  identity.
- **Chaos**: plan 3's harness with kill -9 and Postgres restart, assertions by row counts only.
- **NFR**: the burst test, early (M1.11) and final (M5.12).

Every acceptance check reads rows, bytes or real responses, never a status field
(project rule: verify empirically).

### Test Infrastructure

- **Required**: testcontainers Postgres pg16 with the plan-2 migrations and roles; fake/recording LLM providers (plan 7); fault-injecting provider (M4.6, this plan); seeded sessions helper (M1.10); Moodle 4.3 and 5.2 dev envs with seeded quiz/assignment fixtures, overrides, extensions and 40 students with 10-file submissions; parser sidecar; the target LLM endpoint for the burst test; plan 3 chaos harness
- **Provided by**: M1.10, M4.6 in this plan; 20261101-154950-assess-llm-runner (fakes); 20261101-154950-assess-moodle-ws (dev envs + seeding, extended by M1.11 for the 40×10-file fixture); 20261101-154950-assess-parser (sidecar); 20261101-154950-assess-job-loop (chaos harness); the operator (target LLM, OQ-026)
- **False-pass guard**: dev-Moodle, chaos and burst tests are marked and, when their infrastructure is absent, report `SKIPPED (reason)` in pytest and `BLOCKED` in the result file; CI asserts the integration job collected > 0 assess tests; the burst result file records the model and row counts and is rejected by the report check if any of the 40 outcomes is missing

### NFR Verification

| NFR | Dataset/Load | Tool | Environment | Pass criterion |
|-----|--------------|------|-------------|----------------|
| NFR-014 | IDOR negative matrix (8 cases × quiz/assignment × 4.3/5.2) | `test_idor.py` + response-body canary grep | Integration + dev Moodle | 100% rejected and audited; 0 cross-student bytes |
| NFR-018 | 40 concurrent Starts, 10-file submissions; then 10 concurrent sessions × 20 turns | `scripts/assess_burst.py`, latencies from REQ-085 audit timestamps | Dev stack + target LLM (OQ-026 candidate) | Commit p50 ≤ 10 s, p95 ≤ 30 s, max < 60 s; first visible output p95 ≤ 8 s; ~150-word reply p95 ≤ 30 s (should: breaches reported per provider/model) |
| NFR-020 | Same burst at K = 10 and at the candidate K | Burst script; refusal counts from `assess_start_attempts` | Dev stack + target LLM | K documented with evidence; every Start beyond K refused with 0 consumed sessions (row count) |

## Integration Notes

- **Plan 3**: this plan binds every slot assigned to it in plan 3's ownership table. The pre-flight
  handler budget must be < T = 60 s, and the plan-3 lease (≈ 90 s) is kept so the reaper does not
  race a live handler. The `assess_token_jti` and `assess_start_attempts` cleanup predicates are
  added to the done-job cleanup tick.
- **Plan 2** must provide: the partial unique index on non-CLOSED sessions; status, close and
  outage-transition SECURITY DEFINER functions alongside `get_session_material` and
  `assess_append_turn` (where they are missing, this plan adds them in the next revision in the
  chain with the same grant rules); and an append-only audit store usable in-transaction by both
  roles.
- **Plan 7**: `run_part_b` and `run_stream`, `TurnResult` fields and `ProviderOutageError`. The
  runner never decides credit or state; this plan does.
- **Plan 9 (UI)** consumes the event catalogue and the error codes as defined here, and must not
  add events. **Plan P2 (plugin)** mints via `POST /tokens` with the positive login-as body and
  `plugin_version`.
- **Plan 10** binds `transcript_generate` and the teacher notifications (service_failure,
  persistent rollback). **Plan 11** owns reset, terminate (calls the same close function),
  extra-time input and the REQ-091 teacher date form. **Plan 12** purges sessions and outage
  intervals.
- `GET /health/assess` readiness (plan 1) reads the heartbeat; Starts are fast-refused with 503
  while it is not ready.

## Notes

- **From plan 7 (2026-11-06, coordinator decision on PR #151)**: the API no longer refuses to start without an NFR-023 activation; it starts degraded (`app.state.assess_llm_activated`). The pre-flight activation check of M1.5 is therefore the ONLY gate that keeps an unactivated (model, prompt_version) from serving: it must query `vektra_assess.llm.activation.is_activated` (worker role has SELECT on `assess_model_activations`) before every start, honour `VEKTRA_ASSESS_LLM_ALLOW_UNACTIVATED` only in development, and refuse with a content-free reason. A test must show a start refused for an unactivated triple and accepted after a row is inserted, without a restart.

- **Drift: worker vs server-answered pending turns.** COMP-003 says the worker "runs no
  student-turn LLM calls", while REQ-086 requires the server to answer a pending call with no
  client present. Resolution: the worker probe ends OUTAGE, and an API-side `PendingTurnDriver`
  answers the turn under the live DEK, as CONF-007 places turns in the API. This is turn
  execution, not a worker duty taken over.
- **Drift: the API runs with `lifespan="off"` (BUG-025).** The `PendingTurnDriver`, warning timers
  and limiter cleanup cannot use FastAPI lifespan. They start from `vektra_app.main` before
  `serve`, with an explicit shutdown hook.
- **Risk: `EventBroker` and `TurnDriver` are in-process.** This is correct only with a single API
  process or replica, which matches today's uvicorn setup. Multiple workers or replicas would need
  sticky routing or a Postgres-backed broker; flagged for plan 13.
- **Risk: litellm `health_check` makes a paid completion.** The probe uses a 1-token call and runs
  only while sessions are in OUTAGE.
- **Risk: worker outage blocks turns.** The 150 s freshness gate fails closed when the worker is
  down for more than 150 s: turns are rejected but sessions are not closed. This is intended
  (ARCH-068), but sessions keep losing time. An ops alert on heartbeat age > 60 s (plan 3) is the
  mitigation; a worker outage earns no outage credit under REQ-086.
- **Design choice**: resume via `GET /sessions/{sid}` with a pre-session token. The epoch is bumped
  only by revocations, so two owner tabs remain serialized rather than revoked.
- **OQ-026 / OQ-032**: the burst test runs on the candidate model. K stays 10 until the evidence is
  recorded. A CPU-only co-located LLM is not expected to meet NFR-018.
- **OQ-025 / OQ-031**: gate inputs default to Unreadable, which means REQ-091 fallback or
  NOT_AVAILABLE. The login-as cross-check is a residual risk until the spike reads it.
- **OQ-026 (user answers 2026-11-06)**: production model on-prem, Qwen 3.5 or larger served by vLLM. No throughput or concurrency measurement exists: M1.11 is the first. Local tests run on the cloud API with `openai/gpt-6-luna`.
- **For PR 8b (found reviewing 8a, 2026-11-06)**: the plan-2 `assess_close_session` (revision 0014) closes without bumping `epoch`, while this plan's Design Notes and M5.11 require every close to bump it so the old session token is rejected. 8b must add the bump in a new revision (same grants) and then settle how a tab reconnecting after the close drains `closed`: review fix 1 lets the events route through for a CLOSED row only because the epoch still matches. With the bump, either the stream returns a content-free `closed` on an epoch mismatch for a CLOSED row, or the client learns the close from the 409 of GET. Decide it there, with a test.
- **CodeRabbit on PR #153, deferred to PR 8b (2026-11-06)**: (a) deadline and C-cap check in the session gate (comment 4493109378) -> M3.3/M3.4; (b) enrolment freshness step (4493120917) -> M3.7; (c) recovery of a bot turn whose persist failed, which today leaves `pending_reply_since` set -> M3.6 `reconcile_reply`, with a test that the session accepts the next message afterwards; (d) epoch-checked delivery of reply deltas on the event stream (4493120906) -> with the epoch bump on close above. Not reachable in production before 8b: assess is disabled by default and every production Start rolls back with `privacy_not_configured` until plan 12. Not applied by design: validating a Start body (4493120925) contradicts M2.6, the route reads no body.
- **OQ-020**: the burst and e2e fixtures are synthetic only.
- **PR 8b implementation choices (2026-11-06)**:
  - Warnings (M3.2) are published by the event stream's 1 s poll of the authoritative
    `deadline_at` (credit included), not by a per-session timer in the TurnDriver: they only
    matter to a connected client, the deadline read is the DB's, and the broker dedupes them per
    (session, threshold, deadline) across tabs. A threshold is due only inside a 5 s window, so a
    tab connecting late does not replay a past warning.
  - **Epoch on every close (M3.4, revision 0025)**: `assess_close_session` bumps `epoch` in the
    statement that closes. Decision for a tab that reconnects after the close: an older-epoch token
    of the OWNER (owner, site, activity and session number already verified) on a CLOSED row is
    reported as closed, never as revoked: `GET` is 409 `ERR-ASSESS-009`, the event stream answers
    200 with a content-free `state` + `closed` and no reply text; on a live row any epoch mismatch
    is a revocation (401). Tested in `test_session_clock.py`.
  - **Epoch-checked delivery (M3.4, CodeRabbit 4493120906)**: every `reply_delta` carries the epoch
    it was produced under (not serialized). Before delivering a batch that contains reply text the
    stream re-reads the row: text goes out only if the event's epoch is the token's and the row is
    live at that epoch; a live row at another epoch ends the stream.
  - **Lazy close (M3.4)**: gate step 5 runs as `enforce_clock`, its own committed transaction
    after the owner checks and before the route's gate: ACTIVE past the deadline is closed
    `timed_out` unless a reply is still pending inside the grace (the input is locked meanwhile and
    the next request after the reply closes it); OUTAGE is never timed out and is closed
    `service_failure` once its cumulative outage passes C; a STARTING row past T is reported as
    rolled back (409 `start_timeout`), the reaper deletes it.
  - **Outage (M4, revision 0026)**: no plan-2 function moved a session into OUTAGE, so 0026 adds
    two SECURITY DEFINER functions: `assess_enter_outage` (API) and `assess_end_outage` (worker).
    The interval starts at the failed call's own `ProviderCallRecord.started_at`, clamped to the
    commit, the previous interval's end and `now()`. "3 consecutive failed calls" = intervals opened
    since the last answered bot turn (`opening` / `normal`): the third failure closes
    `service_failure` instead of opening an interval, so the count needs no new column and resets
    on an answer. `assess_close_session` v3 enqueues `teacher_notify` (new job kind, owned by plan
    transcript-delivery, handler unbound until then) in the close transaction of every
    `service_failure`, whatever the path (API lazy close, probe tick, third failure), and ends any
    open interval. One probe per deployment (one discussion provider, OQ-032). The outage end
    restarts `pending_reply_since`, so the `PendingTurnDriver` (3 s minimum age, 2 s poll) answers
    the turn long before `reconcile_reply` (timeout + grace) would mark it. Provider errors that
    are an answer, not an outage (`RateLimitError`, `ContextWindowExceededError`,
    `ContentPolicyViolationError`, `BudgetExceededError`, `BadRequestError`, by type name only),
    never set OUTAGE: the marked turn ends the attempt (`provider_refused`), no credit.
  - **PendingTurnDriver claim (M4.3)**: a session-level `pg_try_advisory_lock` on the TurnDriver's
    live-reply key, held on a dedicated connection for the whole reply, instead of the plan's
    `pg_try_advisory_xact_lock(sid)`: the claim must outlive one transaction (the reply streams).
  - **Outage matrix (M4.6)**: `tests/integration/test_session_outage_matrix.py` runs the real
    runner against a fault-injecting provider in the real API and worker bodies. Outage LENGTHS
    (3 min, 15 s, > C) are simulated on the database clock by moving the open interval's start;
    the credit is still computed by the database from those timestamps. The slow-success case runs
    in real time, scaled (outage timeout 0.3 s).
  - Close message codes: the `closed` event carries `message_code` (`closed_<reason>`;
    `closed_service_failure` is REQ-086's "Sessione interrotta per un problema tecnico..."). The
    plugin renders the text.
  - **Limits (M5.1)**: sized on a scripted reference session (`tests/test_limits_sizing.py`,
    rationale in `docs/reference/configuration.md`): `MAX_MESSAGE_CHARS` 2000 (no longer
    provisional), `MESSAGES_PER_MIN` 4, `SESSION_TOKEN_BUDGET` 500,000 (reference 120-min session
    ~373k tokens). G is `VEKTRA_ASSESS_OUTAGE_G_S` (the M5.14 name). C's 15-minute maximum stays a
    constant (`timing.OUTAGE_CAP_MAX_S`, mirrored in revision 0026's functions), not a setting:
    REQ-086 fixes it and an uncapped variant is not allowed.
  - **Audit (M5.6, revision 0029)**: the close function now writes `assess_close` (the enum's
    name) instead of the plan-2 `session_closed`. `assess_attempt_mismatch_rejected` cannot be
    driven: no route takes a Moodle or attempt id (M2.6); it stays in the enum for plan 9.
  - **REQ-103 flag (M5.9, revision 0030)**: done, not time-boxed out. `assess_turn_flags` (PK =
    the turn, FK to it), API INSERT/SELECT only; the turn row is never updated, so hashes and
    copies stay equal. The route reads the turn's role through the material function (the API
    has no SELECT on `assess_turns`). Plan 10 reads the flags for the transcript marker
    ("segnalato dallo studente"); the worker may SELECT and DELETE them.
  - **M5.10 status: BLOCKED (environment), the test is written.**
    `tests/integration/test_req073_live.py` (`-m moodle`) runs a full session against the live
    4.3 and 5.2 dev sites and compares the plan-4 capture's canonical bytes before and after
    (grade, rubric, feedback), and asserts `no_grade_write` OK for the service account. On this
    machine (2026-11-06) it SKIPS: `BLOCKED: VEKTRA_ASSESS_MOODLE43_URL / 52_URL unset`; the
    integration job lists it among the live modules allowed to skip. Never run against a site
    yet: its first real run is the user's (assess-moodle-contract workflow or local dev envs).
    The adversarial verdict counts are the plan-7 eval report's, not re-measured here.
  - **Chaos (M5.11)**: `tests/chaos/assess/test_lifecycle_chaos.py` runs locally against the
    harness's pg16 containers (23/23 chaos cases green on 2026-11-06, the six close reasons
    included). The kill -9 cases kill real client processes holding the pre-flight commit
    transaction (worker role) or the live-reply claim (API role); Postgres is restarted under an
    open append. The real pre-flight under kill -9 against Moodle is not reproduced (it needs the
    dev Moodle): its DB-level effect, an aborted commit transaction, is.
  - **M5.12 status: BLOCKED (environment).** Re-run on 2026-11-06 with every M3-M5 gate, limit
    and audit in the code: `tests/nfr/results/assess-burst-20261106T201527Z.json` is BLOCKED (no
    API, mint key, DSN, dev Moodle, model or manifest configured), so no NFR-018 percentiles and
    no NFR-020 refusal counts are recorded and K stays the documented placeholder 10 (OQ-026).
    The in-process run against fakes (`test_session_burst_inprocess.py`: 40 Starts, K=10, 10
    commits, 30 `capacity`, 0 rows left) still passes with the Start cap, the rollback streak and
    the per-site breaker active (capacity refusals are not system rollbacks, so the breaker stays
    closed). The real run, and setting K from it, is the user's.
  - **CI (M5.13)**: unit tests run in `ci-unit.yml` (`test-assess`, `-m "not integration"`) and in
    `make test`; the assess integration tests run in the package-integration job (live-Moodle
    modules, `test_req073_live` included, may skip there by design); the chaos suite runs on
    every push and PR in `assess-chaos` and now must collect at least 23 cases with none skipped;
    the burst runs manual and weekly in `assess-moodle-contract.yml` (`assess-burst`), printing
    PASS / FAIL / BLOCKED to the job summary, so it left `EXPECTED_UNRUN` of the suite guard.
  - **Docs (M5.14)**: `docs/reference/api.md` documents the student routes, token claims, the SSE
    event catalogue and the error codes; `.env.example` and `configuration.md` carry every new
    setting. `VEKTRA_ASSESS_OUTAGE_CAP_MAX_S` and `VEKTRA_ASSESS_OUTAGE_PROBE_S` are NOT settings:
    C's 15-minute maximum and the 5-10 s probe interval are fixed (documented as such). The
    CHANGELOG `[Unreleased]` entry covers PR 8a and PR 8b together.
- **Code review of PR 8b (high, 10 findings, 2026-11-06)**, one commit each:
  1. one bot turn per student turn: the claim is a non-blocking try-lock and a driver without it
     does not reply; revision 0031 makes `assess_append_turn` refuse a bot turn unless a student
     turn is pending;
  2. replies take a slot (at most half the API pool after a quarter margin) before their claim
     connection, shared by `TurnDriver` and `PendingTurnDriver`;
  3. `/retry` and `/flag` limiters run after the gate; 4. `409 reply_pending` before the message
     rate;
  5. `enforce_clock` reads without a lock and locks only when a close is due;
  6. the event stream reuses a row read younger than `poll_s` for the delivery check (a
     revocation lets through at most `poll_s` of reply text);
  7. `session_gate.on_session_closed` is the one API-side close hook: DEK out and the reply task
     cancelled; closes made by the worker still reach a running reply only through its refused
     append (open point: a worker-side close does not stop the model at once);
  8. the half-open breaker is decided under a per-site transaction lock in the Start transaction;
  9. enrolment re-checks are queued oldest check first;
  10. revision 0032: `assess_outage_cap` / `assess_outage_spent` are the one definition of C and
     of the spent outage; a NULL duration means C = 0 everywhere (closed `service_failure`).
- **Known residual (worker-side close of a running reply)**: when the WORKER closes a session
  (sweeper `timed_out`, enrolment `access_revoked`, probe `service_failure`) while the API is
  streaming a reply, the reply is not cancelled at once: the model keeps generating until the
  reply's append is refused by the database (the session is no longer ACTIVE at that epoch).
  Mitigations in place: event delivery to the student stops within `poll_s` (the stream re-reads
  the row at most `poll_s` old before sending reply text); a timed-out reply is cut at deadline +
  grace by the TurnDriver's own timer; a `service_failure` close by the probe only happens in
  OUTAGE, when no reply is running. Decision for plan 13 or for the coordinator (for example a
  watcher in the reply on the row state, or a NOTIFY from the close function); not implemented.
- **Operator alert on a Moodle failure during the pre-flight (coordinator decision, 2026-11-06)**:
  minimal form in 8b. A rollback whose reason means Moodle could not be read (`unreadable`,
  `capture_failed_{policy_input,attempt,submission}_unreadable`: the readers turn unreachable,
  timeout and client errors into Unreadable) queues, in the rollback's transaction, a
  content-free `operator_notify` job (`site_id`, `reason`, `kind: moodle_unreachable`; delivery
  by plan 10). One per site per fixed 15-minute bucket of the worker clock
  (`start_guard.MOODLE_ALERT_WINDOW_S`, in the `dedupe_key`), also when the first alert was
  already delivered (the unique dedupe index covers pending jobs only, so the key is looked up
  first). A rollback for a negated gate queues nothing.
- **CodeRabbit round 1 on PR #154 (2026-11-07), design decisions taken here**:
  - gate step 6 (enrolment freshness) also guards `GET /sessions/{sid}` (read and resume), not
    only turns: no decrypted history and no resume token without a fresh `enrolled` check. A
    worker stopped longer than the freshness window now also blocks reading (503), the
    fail-closed side of NFR-014 (comment 4498473572);
  - the per-route limiters stay process-local (`RateLimiter` per API process, plan M5.3): the
    shipped deployment runs one API process, the one-reply-at-a-time rule and the database
    guard bound model calls per session whatever the process count, and a shared limiter
    needs a shared store the plan does not have (comment 4498473536, not applied);
  - the event stream's revocation bound stays "at most `poll_s` of reply text" (review fix 6,
    coordinator decision; comment 4498473544, not applied).
  - not applied, recorded: the live REQ-073 test keeps comparing the plan-4 capture (read
    times zeroed); comparing the unprojected reader records needs the plan-4 raw-dump helper
    and a live site to write against, so it is left to the first live run (comment
    4498473615); a session token minted before an outage credit keeps its earlier expiry:
    tokens live at most `VEKTRA_ASSESS_SESSION_TOKEN_TTL_S` (15 min) and the client re-mints
    through resume, which caps the new token at the current (credited) deadline (unverified
    finding, outage_probe.py 94-97); the worker does not check the Alembic head at start: the
    schema is migrated by the operator before the services start (`alembic upgrade head`, API
    startup step 3 refuses a behind schema), and the worker role has no grant on
    `alembic_version`; adding one is a grant-matrix change for a later plan (minor comment
    on models.py 202-204).
- REQ-103 is a could-have and is ordered last (M5.9). Plan 9's marker task depends on it.
- **Implementation choices, PR 8a (2026-11-06)**:
  - `assess_token_jti` is a new table (revision 0022) next to the plan-2 `assess_consumed_jtis`,
    which stays for the transcript download path (plan 10). Mint derives the site from the key's
    `assess_site_keys` binding (purpose `mint`); the body carries `sub`, `cmid`, `courseid` (hint),
    `loginas`, `realuser_id`, `plugin_version`, as the discuss-student plan sends it.
  - The Start window T is measured from `assess_sessions.created_at` (no separate
    `start_requested_at` column); `starting_expires_at` = created_at + T.
  - `assess_start_attempts` gets outcome `requested` (API, at Start) and `session_id`; the worker
    appends `committed` / `rolled_back` rows, so no role needs UPDATE there. The API reads the table
    (Start cap, rollback reason).
  - `assess_activity_config.cap_minutes` (5-120, NULL = not configured) is added here because the
    pre-flight needs the cap; plan 11 owns the form that writes it and should reuse the column.
    Until plan 11, `flags.show_solution` / `flags.rubric_source` give the teacher view (defaults:
    solution OFF, rubric source `moodle` for assignments, `none` for quizzes).
  - The session freezes `duration_s` (cap + extra time) and `frozen_policy` (content-free BR-011
    decision, needed by the API to assemble the bundle per turn) at commit.
  - The cmid is resolved to (course, instance) by a new reader `resolve_activity`, inside the
    student's own courses (hinted course first), with the recorded functions the capture readers
    already use; an unresolvable module rolls the Start back.
  - Opening message: Part A and Part B are two `opening` bot turns (1 and 2), chained on the
    snapshot hash; `seq` is 2 after commit. The `seq` claim of a session token is the session
    number N, matched against `session_no`.
  - **BR-006 (h) privacy gate (M2.2), for review**: its store (`assess_privacy_config`, REQ-089)
    belongs to plan 12 and does not exist yet. The pre-flight evaluates (h) through a
    `PreflightDeps.privacy` port whose default returns "not configured", so in production every
    Start is rolled back with `privacy_not_configured` until plan 12 binds the real check (REQ-089
    says fail closed). Development and CI pass it with `VEKTRA_ASSESS_PRIVACY_ALLOW_UNSET`
    (development only; the worker refuses it elsewhere; the dev overlay sets it), the same pattern
    as the LLM activation bypass. **Plan 12 must bind `PreflightDeps.privacy`.**
  - **Gate inputs (M2.2)**: assignments always take the REQ-091 fallback today (the extensions and
    user overrides of ALL participants are not listable with the minimal role, OQ-025), so a Start
    needs the teacher-attested `attested_opening_at` (plan 11's form). A quiz regrade in progress is
    detected by the capture's per-item BR-014 check, not as a separate gate input. The gate order
    puts enrolment right after `enabled`.
  - **Render state at Start (M2.3)**: the Design Notes list "stale or negative `render_state`" among
    the advisory Start refusals. Implemented: a FRESH (`VEKTRA_ASSESS_RENDER_FRESH_S`, 300 s)
    negative state of a Moodle-side gate refuses with that gate's code and writes nothing; a stale
    or missing state does not refuse (the pre-flight re-reads and is authoritative), so a Start
    right after a grade is released, or with no `/state` call before it, is never refused on old
    data. Codes the Start transaction re-checks itself (`session_open`, `already_discussed`,
    `not_enabled`) never refuse from a render row.
  - `GET /state` uses the pre-session token's own cmid (the `?cmid=` the plugin adds is not read)
    and does not consume its jti. Before the first render it answers `unavailable` / `checking`.
  - **Gate order (M2.4)**: step 1 is the token dependency (`verify_assess_token_*`, the names the
    structural route test accepts); steps 2-6 are ONE function, `api.session_gate.gate`, used by
    every session route either through `session_tx` (turns, events) or directly (`GET
    /sessions/{sid}`, whose resume path consumes the jti in the same transaction). Steps 5 and 6
    are placeholders bound by M3.4 and M3.7; their spy rows are added with them.
  - **REQ-084 residual risk (M2.5, OQ-031(2))**: the login-as refusal rests on the plugin's
    positive assertion at mint (`loginas: false`, `realuser_id: null`, both present) and on every
    token carrying `loginas: false` (a signed token without it is refused with 403 and audited
    `assess_loginas_rejected` on every route, nothing written). Vektra cannot cross-check it against
    Moodle: the `loginas_crosscheck` capability is Unreadable by default (`read_loginas_events`
    returns Unreadable; the OQ-025 spike found no WS read of the login-as log). A compromised or
    buggy plugin that asserts `false` during a login-as session is not detected by the backend.
    Residual risk, accepted until the spike reads it.
  - **IDOR matrix (M2.6)**: `vektra-assess/tests/test_idor.py` runs the matrix on the recorded 4.3
    and 5.2 responses (B's snapshot is a real capture of each branch) in the package integration job.
    The live variant against the seeded dev sites is `tests/integration/test_idor_live.py`
    (`-m moodle`, SKIPPED with BLOCKED here: the dev envs are not running; the integration job's
    skip guard lists it with the other live modules). "Other course" is covered as a client course
    hint: it is never trusted (the cmid is resolved inside the student's own courses).
  - **Review fix 5 (turns)**: a provider outage, an internal error or a refused bot append now
    ends the attempt with the marked bot turn `[risposta interrotta]` (`vektra_assess.turn_markers`,
    kind `interrupted`) through `assess_append_turn`, which clears `pending_reply_since`, so the next
    student turn is accepted. This is the thin form: the real ACTIVE -> OUTAGE transition (credit,
    probe, server-side answer of the pending turn) is M4.1-M4.3, and M3.3 / M3.6 write the same
    marker. A bot append refused by the database is left to the database (from M3.3 that includes
    deadline + grace by the DB clock); the marked turn is then attempted once and may be refused too.
  - **Review fix 9**: `VEKTRA_ASSESS_MAX_MESSAGE_CHARS` exists now with a PROVISIONAL default of
    2000; a longer or blank message is a 400 before any model call. M5.1 sizes the value (and the
    other REQ-083 limits) on the reference session.
  - **Review fix 10**: the pre-flight's budget and lease follow T: budget = T − 15 s, lease =
    max(2 × budget, budget + 30 s); overrides with budget > T − 5 s or lease < budget + 30 s refuse
    worker startup. A cancellation at the budget (or at shutdown) runs the rollback (shielded,
    reason `start_timeout`) instead of waiting for the reaper. The worker's environment in
    `docker-compose.assess.yml` passes `VEKTRA_ASSESS_START_T_S` (and admission K, render freshness
    and the model parameters) from `.env`, so both processes use the same values.
  - **M1.11 status: BLOCKED (environment), the tooling is done.** `scripts/assess_burst.py` and
    `tests/nfr/test_assess_burst.py` run against a configured target only (`VEKTRA_ASSESS_BURST_*`:
    API, mint key, DSN, dev Moodle, model label, manifest with a `burst` section; optional LLM base
    and worker container). With anything missing or unreachable they write a BLOCKED result under
    `tests/nfr/results/` and pytest reports SKIPPED; a result missing any of the N outcomes, its
    model or its row counts is rejected by `validate_result`. The first run on this machine
    (2026-11-06) is BLOCKED: no target configured (result file committed). The real run is the
    user's (it reaches the dev Moodle and the candidate model). The logic is verified in-process
    against fakes (`vektra-assess/tests/integration/test_session_burst_inprocess.py`: 40 Starts, K=10,
    exactly 10 commits, 30 refused or rolled back with `capacity`, 0 rows left by the refused ones);
    that test writes no NFR evidence.
  - **Request for vektra-moodle (owned by the other session)**: the burst needs a seeded fixture of
    40 students enrolled in one course, each with a graded and released 10-file submission on one
    assignment (cut-off set, rubric or simple grading, not a team submission), added to
    `MOODLE/docker/fixtures/fixtures.json` and written to the seed manifest as
    `"burst": {"cmid": <cmid>, "courseid": <id>, "students": [<40 userids>], "files_per_submission": 10}`.
    Synthetic content only (OQ-020). Until it exists the burst reports BLOCKED
    (`burst fixture ... not seeded`).
  - The import contract "`_assembly` is private to `vektra_assess.context`" now allows indirect
    imports: `assemble_context` (the public wrapper) is how the pre-flight and the turn route build
    bundles; a direct import of `_assembly` still breaks the contract (canary test unchanged).
