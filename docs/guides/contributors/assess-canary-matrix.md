# The assess canary matrix

The assessment bot must never put into the model's context what the student may not see in Moodle: the teacher's solution with "mostra soluzione" OFF, right answers, marks and feedback hidden by the quiz review options, unreleased grades and feedback, rubric descriptors whose visibility cannot be read. A prompt instruction is not a control (BR-011, NFR-017). The control is that the material is **absent**, and the canary matrix is how that is proven.

## What it checks

The matrix is the joint guarantee of ADR-0033 D4 (Vektra's sinks over the LMS provider's recorded reviews), so it lives with the Moodle provider: `adapters/vektra-lms-moodle/tests/canary/` builds the cases from the recorded Moodle 4.3 and 5.2 responses (`adapters/vektra-lms-moodle/tests/fixtures/moodle_ws/`) and plants a unique token (`CNRY-<field>-<type>-<id>`, or a number for a mark) in every forbidden field of every question type and assignment shape. Each case then runs capture, assembly, Part A and a reference model call, and `adapters/vektra-lms-moodle/tests/test_canary_matrix.py` counts the tokens in eight sinks:

1. the snapshot plaintext after an encrypt/decrypt round trip
2. the assembled `ContextBundle`
3. Part A
4. the exact request bytes at `RecordingLLMProvider`
5. the logs, through the production log-safety chain, at DEBUG
6. the audit events (and, in the integration suite, the audit rows and a data dump)
7. the QueryTrace
8. the calls to `RecordingVectorStoreProvider(forbid_calls=True)`: RAG is OFF, so zero

A forbidden token must appear zero times in every sink. Every sink is asserted non-empty first, the executed-case count must equal the generated count, and every forbidden field x type / shape cell must have been exercised. Positive controls (the pasted rubric, the solution with the toggle ON, the question text, read file text) must appear, and two negative controls break the sanitizer and the policy on purpose to prove the matrix fails when something leaks.

## Adding a field

A new value can reach the model only through a change to the allowlist models in `vektra_assess/context/snapshot.py`; there is no free-form dict. When you add one:

1. Decide which BR-011 rule governs it (always visible, review option, release state, toggle, display setting) and add it to `DisclosurePolicy` if it is conditional.
2. Add the cell: a forbidden field in `canary/matrix.py` (`QUIZ_FIELDS` or `ASSIGN_FIELDS`), with an injection that plants its token where Moodle would put the value, and a permitted-case rule in `finalize_quiz` if a policy can allow it.
3. Run `make test-lms-moodle` (or, with the provider installed, `UV_NO_SYNC=1 uv run pytest adapters/vektra-lms-moodle/tests/test_canary_matrix.py`). The cell-coverage test fails until the field is exercised for every type or shape it applies to.
4. If Part A shows it, regenerate the golden renderings with `VEKTRA_UPDATE_GOLDEN=1` and review the diff.

## Re-recording on a Moodle upgrade

The extraction of the question text and the student's answer depends on Moodle's question markup (`div.qtext`, `fieldset.ablock`). Both fail closed, differently: a change to `div.qtext` omits the question text and makes the item degraded, so Part A flags it; a change to `fieldset.ablock` omits only the student's response and does not set the degraded flag. After re-recording on a new Moodle release, run the matrix and `tests/test_review_block_extraction.py`, which catch either change before production.

## Live subset

`UV_NO_SYNC=1 uv run pytest adapters/vektra-lms-moodle/tests -m moodle` (provider installed: `uv sync --group lms-moodle`) with the variables in `adapters/vektra-lms-moodle/tests/integration_moodle/test_context_live.py` repeats one capture per shape and type against the real seeded 4.3 and 5.2 environments. Without them the tests skip with `BLOCKED: ...`; they never pass silently.
