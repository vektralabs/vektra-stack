# vektra-lms-moodle

The Moodle provider of Vektra's `LmsProvider` Protocol (ADR-0033): the web-service client, the
4.3 and 5.2 response models, the breaker and rate cap, the capability probes and the
service-account items of the setup check.

It is not part of the platform. It is staged in `adapters/` until phase 2b moves it to its home
repository; the default Vektra image does not contain it, and the `lms-moodle` image variant
does. A deployment selects it with `VEKTRA_ASSESS_LMS_PROVIDER=moodle` and configures it with the
`VEKTRA_LMS_MOODLE_*` settings (`docs/reference/configuration.md`).

- Install for development: `uv sync --group lms-moodle` at the repository root.
- Test: `make test-lms-moodle` (its unit tests, the conformance kit on the 328 recorded
  responses, and the joint suites that run the vertical's capture and setup check over them).
