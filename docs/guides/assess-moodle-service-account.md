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
(rubric *fillings*), `moodle/role:review` (graders).

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

## What the setup check verifies

Run it from the Moodle plugin ("Verifica configurazione") or `POST /api/v1/assess/sites/{site}/setup-check`.
Each item is `OK`, `MISSING` or `UNVERIFIABLE`; **only `OK` counts**. An item that cannot be
verified is never assumed fine.

| Item | Meaning |
|------|---------|
| `token_works` | `core_webservice_get_site_info` answers with the token |
| `required_reads` | every reader group (quiz, assignment, grading, enrolment, graders) returns a well-formed answer |
| `interactive_login_disabled` | the account's `auth` is `webservice` |
| `not_site_admin` | `userissiteadmin` is false |
| `not_ingestion_account` | the account is not the ingestion account (`VEKTRA_ASSESS_INGEST_ACCOUNT_USERNAME`); unset means `UNVERIFIABLE` |
| `denied_function_probe` | a harmless read function left out of the service is refused (`accessexception`) |
| `no_grade_write` | no grade-write function appears in the service's function list |
| `ws_tls_ok` | the connection is https verified against the configured CA |
| `ui_not_same_origin` | the discussion UI origin is not same-site with Moodle or the API |
| `ui_origin_distinct` | the discussion UI origin is https (outside development) and is not this site's Moodle origin: the precondition of the iframe's `allow-same-origin` sandbox (ARCH-076); the API also refuses to start otherwise |
| `parser_sidecar_running`, `smtp_probe`, `db_log_statement_none` | registered by other components; an unregistered item is `MISSING` (fail closed) |

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

## The discussion UI origin and framing (plan 9)

The student discussion runs in an iframe on the Moodle results page, but the page inside it is
served by Vektra (`GET /api/v1/assess/ui?site=<site id>`) on its own origin,
`VEKTRA_ASSESS_UI_ORIGIN`. Three settings must agree:

- `VEKTRA_ASSESS_UI_ORIGIN` is an `https` origin (scheme, host, port; no path), different from
  every Moodle origin. The API refuses to start otherwise, and the setup check reports
  `ui_origin_distinct`. The plugin's iframe sandbox includes `allow-same-origin`, which is safe
  only because the two origins differ.
- Each site's `moodle_origin` (on `assess_sites`, a bare origin) is the one origin allowed to
  frame the page (CSP `frame-ancestors`) and to exchange postMessage with it. A site without it
  falls back to the origin of its `base_url`.
- The UI origin has its own server on the reverse proxy. The origin model: ONE backend, TWO
  public origins. The API origin (`VEKTRA_ASSESS_API_PUBLIC_ORIGIN`) serves Moodle's
  server-side calls (token mint), admin and every other route, with `X-Frame-Options: DENY`.
  The UI origin (`VEKTRA_ASSESS_UI_ORIGIN`) serves the page, its assets and only the student
  routes the page calls on its own origin (CSP `connect-src 'self'`): `GET /state`,
  `/sessions...` (Start, resume, turns, retry, flag, Fine, the event stream) and, with plan 10,
  `/transcripts/...`; everything else there is 404, and it never sends `X-Frame-Options`. "The
  UI calls the API on its own origin" and "the UI origin is distinct from the API origin" do
  not conflict: the rule is about the browser-visible origin (another site than the API and
  than Moodle, so `ui_not_same_origin` and `ui_origin_distinct` pass), not about the backend
  behind it. The shipped nginx example (`deploy/nginx/vektra.conf.example`) has both servers;
  the UI page answers only when the request's scheme and `Host` are the UI origin, so the
  proxy must forward `Host` (and, from another container, be in uvicorn's
  `FORWARDED_ALLOW_IPS`). A deployment that copied an older example serves the page on the API
  origin, where it is now 503.

