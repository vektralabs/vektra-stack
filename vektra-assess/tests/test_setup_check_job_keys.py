"""The setup-check job's identity, without a database (plan assess-job-loop F4).

Three writers enqueue or look up a ``setup_check`` job for a site: the daily tick
(``worker.setup_check.queue_setup_checks``), a release change (``capabilities.observe_release``)
and the API's "is one pending" check (``api.setup_check``). They must agree on the dedupe key, or
a site could get two checks queued at once, or the API could report none while one is pending.
The handler's database work is the integration suites'.
"""

from __future__ import annotations

import inspect
import uuid

from vektra_assess import capabilities
from vektra_assess.api import setup_check as api_setup_check
from vektra_assess.jobs import JobKind
from vektra_assess.worker import setup_check
from vektra_assess.worker.policy import KIND_DEFAULTS


def test_the_tick_the_release_change_and_the_api_use_one_dedupe_key() -> None:
    site = uuid.uuid4()
    key = setup_check.dedupe_key(site)
    assert key == f"setup_check:{site}"
    # the same literal shape in both other writers (they build it inline, in SQL parameters)
    assert 'f"setup_check:{site_id}"' in inspect.getsource(capabilities)
    assert 'f"setup_check:{site_id}"' in inspect.getsource(api_setup_check)


def test_a_malformed_job_argument_is_no_site() -> None:
    site = uuid.uuid4()
    assert setup_check._uuid(str(site)) == site
    assert setup_check._uuid(site) == site
    for bad in ("", "not-a-uuid", None, 42):
        assert setup_check._uuid(bad) is None


def test_an_orphan_is_judged_after_the_jobs_own_lease() -> None:
    """A report is failed as abandoned only once no live job could still be running it."""
    assert setup_check.ORPHAN_AFTER_S == KIND_DEFAULTS[JobKind.SETUP_CHECK].lease_s
    assert setup_check.ORPHAN_AFTER_S >= 2 * KIND_DEFAULTS[JobKind.SETUP_CHECK].budget_s
    assert setup_check.KEEP_REPORTS == 30
