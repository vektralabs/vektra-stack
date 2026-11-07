# ADR-0029: Postgres-native leased job loop for assess with DB-enforced lifecycle invariants and transactional outbox (alongside ADR-0006)

**Status**: accepted
**Date**: 2026-11-01
**Context**: design session `20261101-143802-design-assessment-discussion-chatbot` (assessment discussion chatbot); covers ARCH-068

## ARCH-068: DB-enforced lifecycle, async Start, transactional outbox, on a Postgres-native leased job loop

### Context

REQ-067 requires exactly one session under concurrent starts. REQ-071 requires that a client
disconnect never rolls back and that no content leaks before commit. REQ-070 and REQ-086
require client-less closes and outage exits. REQ-075 requires durable delivery, and NFR-021
generation within 60 s at p99.

### Decision

Round-1 content stands, with these amendments:
- The Start transaction INSERTS an assess_jobs row and sends NOTIFY (the outbox pattern
  applied to Start).
- Jobs are claimed with UPDATE ... lease_until WHERE id IN (SELECT ... FOR UPDATE SKIP
  LOCKED). Leases are about 2x the handler budget (about 90 s for pre-flight). Handlers are
  idempotent per session id or per (transcript, recipient).
- NOTIFY is a wake-up hint only. A 1 s poll fallback plus the periodic ticks are the
  correctness backstop.
- Ticks run under pg_try_advisory_lock:
  * sweeper 10-15 s;
  * STARTING reaper 15 s, lease about 90 s (NOT the ADR-0006 5-min rule);
  * outage probe 5-10 s per provider;
  * enrolment re-check 60 s;
  * delivery retries by not_before;
  * gate-input refresh;
  * daily purge and setup check.
- On startup, a full reconciliation pass runs before new claims.
- The API lazily enforces deadline, C cap AND STARTING expiry on every request. Per-turn
  enrolment is a FRESHNESS check: the turn is rejected if the last enrolment re-check is older
  than 150 s. No WS call happens on the API path.
- Liveness: an assess_heartbeat row per tick, exposed through GET /health/assess on the API.
  Alerts on heartbeat age > 60 s and oldest ready job age > 30 s.
- Chaos tests (kill -9 mid-pre-flight, mid-generation, mid-SMTP; Postgres restart) are
  verified by row counts.
- The API NEVER takes over worker duties in-process.
- Handlers sit behind a small JobRunner interface, so a later move to arq/Redis (Phase 2) is
  mechanical.

### Rationale

OQ-028 resolved 4/4. Correctness already rests on Postgres, and Redis would add a non-authoritative second store and a dual write.

### Options considered

- **DB invariants + async Start + outbox** — pros: exactly-one by constraint; REQ-071 holds by construction; survives worker restarts; fail closed when workers are down; cons: UI needs polling or SSE on state; race tests required
- **Synchronous Start inside the HTTP request** — pros: simpler UI; cons: disconnect semantics tangled with REQ-071; no isolation from API process load

### Consequences

- (+) no new stateful technology
- (+) REQ-070/REQ-086/NFR-021/REQ-071 met with margin while the worker is up
- (+) fail closed while it is down
- (−) hand-rolled loop to own and test (about 5-6 dev-days)
- (−) side-effect SLAs bounded by one worker's availability
- (−) LISTEN/NOTIFY and advisory locks need direct Postgres connections (no transaction-mode pgbouncer)

