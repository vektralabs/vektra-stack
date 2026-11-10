# Assess: the Moodle service account

The assessment discussion feature reads a student's attempt and grading state from Moodle through
web services, with one dedicated, read-only account per Moodle site. This guide describes that
account, how to provision it, and what the setup check verifies. The reference implementation
for a development site is `docker/provision/assess_service_account.php` in the `vektra-moodle`
repository; production sites follow the same shape.

## The account

- A normal Moodle user, **not a site administrator**, distinct from the account the ingestion
  pipeline uses.
- Authentication method **`webservice`** (the *Web services authentication* plugin enabled in
  Site administration > Plugins > Authentication). Do **not** use `nologin`: Moodle's web-service
  layer refuses such a user (`wsaccessusernologin`), so every call would fail. `webservice` still
  prevents an interactive login: the account has no usable password and can only act through its token.
- A permanent web-service token on a dedicated external service (shortname `vektra_assess`) with
  *Authorised users only* enabled and this account as the only authorised user.
- The token is delivered to the Vektra worker as the `assess_ws_tokens` secret (never in the
  environment, never in the database) and is rotated by replacing the token row and the secret.

## The service's function list (read-only)

The control that matters (REQ-079) is that the service lists **no grade-write function**. The list:

```
core_webservice_get_site_info
core_course_get_courses_by_field, core_course_get_contents, core_course_get_course_module
core_enrol_get_enrolled_users, core_enrol_get_enrolled_users_with_capability, core_enrol_get_users_courses
core_group_get_course_user_groups, core_user_get_users_by_field
mod_quiz_get_quizzes_by_courses, mod_quiz_get_user_attempts, mod_quiz_get_attempt_review
mod_quiz_get_combined_review_options, mod_quiz_get_user_best_grade, mod_quiz_get_overrides (5.2 only)
mod_quiz_get_user_quiz_attempts, mod_quiz_get_quiz_feedback_for_grade
mod_assign_get_assignments, mod_assign_get_submissions, mod_assign_get_submission_status
mod_assign_get_grades, mod_assign_get_user_flags, mod_assign_get_participant
core_grading_get_definitions, core_grading_get_gradingform_instances
gradereport_user_get_grade_items
```

`php assess_service_account.php --print-functions` prints the list that the provisioning script
creates (one constant, the single source of truth).

## The role

A system-level role with only the capabilities the reads need. Each one below was removed in turn
during verification and broke at least one read:

`webservice/rest:use`, `moodle/course:view`, `mod/quiz:viewreports`, `mod/quiz:viewoverrides`
(5.2 only), `mod/assign:viewgrades`, `moodle/grade:viewall`, `moodle/grade:managegradingforms`
(rubric *fillings*), `moodle/role:review` (graders, and the activity editors who may change the
discussion configuration: the same function asked for `moodle/course:manageactivities`, no
additional capability).

Also granted for coverage: `moodle/course:viewparticipants`, `moodle/course:viewhiddenactivities`,
`moodle/course:viewhiddencourses`, `moodle/site:accessallgroups`, `moodle/user:viewdetails`,
`moodle/grade:viewhidden`, `moodle/grade:view`, `gradereport/user:view`, `mod/quiz:view`,
`mod/quiz:reviewmyattempts`, `mod/assign:view`.

`mod/assign:grade` is **not** needed: `mod/assign:viewgrades` carries every read. Reading rubric
fillings needs `moodle/grade:managegradingforms`, which Moodle also uses for editing grading
forms; that is why the control is the function list, not the capability set.

## Provisioning

On a development site: `php docker/provision/assess_service_account.php` (idempotent; reads
`ASSESS_WS_TOKEN` for a fixed token, otherwise generates one and reuses it). It also creates three
**test-only** accounts used to prove the setup check refuses them: a site administrator, an
account whose service lists a grade-write function, and a stand-in for the ingestion account.
Never run it against a production site.

## Token creation and rotation

The token belongs to the service account and to the `vektra_assess` external service only. On
the LMS, as a site administrator, create it under the web-service token management page (in
Moodle: *Site administration > Server > Web services > Manage tokens*), for this account and this
service, with no expiry and no IP restriction unless your network requires one. Then put it into
the worker's `assess_ws_tokens` secret, keyed by the site's `assess_sites.id`:

```json
{"3f0c8c1e-0000-4000-8000-000000000001": "<token>"}
```

The worker re-reads this file on every LMS call, so no restart is needed. To rotate:

1. create a second token for the same account and service;
2. replace the site's value in `assess_ws_tokens`;
3. run the setup check: `token_works` must be `OK`;
4. delete the old token on the LMS.

A token is never in the environment, the database or a log; the worker logs the web-service
function, the site id, the HTTP status and timings only. A suspected leak is handled the same way,
starting from step 4 (delete first, then create and install the new token). In between, the
LMS refuses the worker's calls and the reads of that site are reported unreadable, which keeps
the START gates closed rather than opening them.

The LMS menu path above is Moodle's, quoted for orientation; this repository does not test it.

## Register the site and mint the plugin's keys

No API route creates a site or binds a key to it. Both are rows the operator writes.

1. **The site**, as the database owner:

   ```sql
   INSERT INTO assess_sites (moodle_site_uuid, base_url, moodle_origin)
   VALUES ('<unique site identifier>', 'https://moodle.uni.example', 'https://moodle.uni.example')
   RETURNING id;
   ```

   `id` is the site id used everywhere else: the `assess_ws_tokens` key, the setup-check and
   privacy routes, the UI's `?site=`. `moodle_site_uuid` must be unique; no code reads it beyond
   that, so use an identifier you can trace to the LMS site (which value the plugin expects there,
   if any, is not verified in this repository). `moodle_origin` is the framing origin
   ([framing](operators/assess-framing.md)). `min_plugin_version` (optional) raises the minimum
   plugin release for this site above `VEKTRA_ASSESS_MIN_PLUGIN_VERSION`.
2. **Four API keys**, one per purpose, each with a single `assess:*` scope (an `assess:*` scope
   cannot be combined with `admin`, `ingest` or `query` on one key). With an `admin` key:

   ```bash
   for scope in mint teacher privacy-export privacy-erase; do
     curl -s -H "Authorization: Bearer $VEKTRA_ADMIN_KEY" -H "Content-Type: application/json" \
       -d "{\"label\": \"assess $scope <site>\", \"scopes\": [\"assess:$scope\"]}" \
       https://vektra.uni.example/api/v1/api-keys
   done
   ```

   Each answer shows the key once (`key`) and its `id`.
3. **Bind each key to the site**, as the database owner, with the purpose matching its scope:

   ```sql
   INSERT INTO assess_site_keys (site_id, api_key_id, purpose) VALUES
     ('<site id>', '<id of the assess:mint key>',           'mint'),
     ('<site id>', '<id of the assess:teacher key>',        'teacher'),
     ('<site id>', '<id of the assess:privacy-export key>', 'privacy-export'),
     ('<site id>', '<id of the assess:privacy-erase key>',  'privacy-erase');
   ```

   A key is bound to at most one site (`api_key_id` is unique). Every site-bound route checks
   both the exact scope and the binding for its purpose: a key bound to no site, or for another
   purpose, gets `403 ERR-ASSESS-007`, and the `admin` key never satisfies these routes.
4. **Give the keys to the plugin**, in its site settings (the plugin's own documentation). The
   `mint` key mints the discussion tokens and the student's own transcript download token,
   server-side; `teacher` for the activity configuration,
   the teacher operations, the setup check and transcript download or resend by a grader;
   `privacy-export` and `privacy-erase` for the LMS privacy provider.

To rotate a plugin key: mint and bind a new key for the same purpose, update the plugin, then
revoke the old one (`DELETE /api/v1/api-keys/{key_id}`).

## What the setup check verifies

Run it from the Moodle plugin ("Verifica configurazione") or `POST /api/v1/assess/sites/{site}/setup-check`.
Each item is `OK`, `MISSING` or `UNVERIFIABLE`; **only `OK` counts**. An item that cannot be
verified is never assumed fine.

| Item | Meaning |
|------|---------|
| `token_works` | `core_webservice_get_site_info` answers with the token |
| `required_reads_quiz`, `required_reads_assign`, `required_reads_grade`, `required_reads_grading`, `required_reads_enrol`, `required_reads_files` | each reader group returns a well-formed answer (one item per group) |
| `interactive_login_disabled` | the account's `auth` is `webservice` |
| `not_site_admin` | `userissiteadmin` is false |
| `not_ingestion_account` | the account is not the ingestion account (`VEKTRA_ASSESS_INGEST_ACCOUNT_USERNAME`); unset means `UNVERIFIABLE` |
| `denied_function_probe` | a harmless read function left out of the service is refused (`accessexception`) |
| `no_grade_write` | no grade-write function appears in the service's function list |
| `ws_tls_ok` | the connection is https verified against the configured CA |
| `ui_not_same_origin` | the discussion UI origin is not same-site with Moodle or the API |
| `ui_origin_distinct` | the discussion UI origin is https (outside development) and is not this site's Moodle origin: the precondition of the iframe's `allow-same-origin` sandbox (ARCH-076); the API also refuses to start otherwise |
| `parser_sidecar_running`, `smtp_probe`, `db_log_statement_none`, `privacy_config` | registered by the worker (parser deep ping, SMTP probe, Postgres logging settings, the site's REQ-089 privacy settings); an item no component registered would be `MISSING` (fail closed) |

The full walkthrough, with what to fix for each item, is in the
[deployment guide](operators/assess-deployment.md#setup-check-walkthrough).

## Capability flags and their fail-closed fallbacks

The setup check also probes, per site and Moodle release, what can really be read, and stores the
answer (`readable`, `partial`, `unreadable`). Anything never probed, older than 26 hours, or probed
on another release is `unreadable`; `partial` counts as unreadable. A release change invalidates
every row at once and queues a new check; Starts are blocked until it passes.

| Flag | If unreadable |
|------|---------------|
| `quiz_overrides` (absent on Moodle 4.3) | the teacher must attest the opening date for quizzes that have overrides |
| `quiz_grace`, `assign_cutoff`, `assign_extensions`, `assign_user_overrides` | the effective close cannot be computed: attested date |
| `attempt_state` | the discussed attempt cannot be selected: no discussion |
| `review_html_filterable` | always unreadable: only structured, option-filtered data reaches the model |
| `review_options_per_student` | no discussion of that quiz |
| `rubric_display_settings` | always unreadable: rubric descriptors are excluded from the model context |
| `module_visibility_per_student`, `loginas_crosscheck` | unreadable: the retrieval of course material stays off, and login-as is trusted to the plugin |
| `release_state`, `grading_state`, `graders_resolvable`, `enrolment_readable`, `per_student_timelimit` | the matching gate stays closed |

## The discussion UI origin and framing

The discussion page is served by Vektra on its own origin and framed by the LMS. The origins, the
per-site `moodle_origin`, the reverse proxy and how to verify them are in the
[framing runbook](operators/assess-framing.md).

## Transcript recipients and the domain allowlist (plan 10b)

The service account is NOT given any capability that exposes email addresses
(`moodle/course:useremail`, `moodle/site:viewuseridentity`, `moodle/user:viewalldetails`): a
site-level grant would let it read every enrolled user's address, which REQ-079's minimal role
forbids. Transcripts are mailed instead to the addresses the teacher enters in the plugin's
activity form (REQ-082 as amended on 2026-11-07):

- 1 to 3 addresses, prefilled with the teacher's own Moodle address; they travel to Vektra
  inside the sealed teacher config (`transcript_recipients`), which only the worker can open.
  Vektra stores no address: delivery rows and audit events carry `sha256(site|address)` only,
  and `GET /state` shows them masked (`m***@uni.example`).
- The site admin keeps the domain allowlist in the plugin settings; the operator sets the same
  list in `VEKTRA_ASSESS_RECIPIENT_DOMAINS` (exact domains, comma separated). The plugin refuses
  an address outside it with a field error naming the allowed domains; the worker re-checks at
  send time and marks a refused address `FAILED` (`domain_not_allowed`) with an alert. **An
  empty list refuses every address**, so set it before enabling the feature, and keep the two
  copies equal: a domain the plugin accepts but Vektra does not ends in a `FAILED` delivery
  (the transcript stays downloadable from the activity page).
- Changing the addresses after a session closed takes effect at the next send attempt: a removed
  address is `REPLACED` (no mail), an added one gets its own delivery, never more than 3.
- The same addresses receive the teacher notifications: a session closed for a service failure
  (within the minute) and a student whose Starts failed for a technical problem several times in
  a row. They carry ids and the event only (activity, numeric Moodle user id, session number).
  The operator's copy of the rollback streak, and the "Moodle could not be read" alert, go to
  `VEKTRA_ASSESS_OPERATOR_EMAIL`, which the worker requires to start. A notification that
  reached nobody (no address configured, all refused, the retry window over) is audited
  `assess_notify_failed` and raises the `notify_failed` alert in the worker log.
- Who may DOWNLOAD a transcript or ask a resend is unchanged: the course's graders, re-checked by
  Moodle user id (`core_enrol_get_enrolled_users_with_capability`, no email field read).
