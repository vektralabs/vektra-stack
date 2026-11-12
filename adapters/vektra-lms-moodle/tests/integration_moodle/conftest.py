"""The vertical's real-Postgres fixtures (``vektra-assess/tests/integration/conftest.py``) for the
provider's joint integration suites: one migrated container per module, the real API and worker
roles, the consumed-invariant check. Taken over unchanged, so these suites hold the vertical's
database to the same rules its own suites do.
"""

from integration.conftest import *  # noqa: F403
from integration.conftest import Pg, dump_data, make_session  # noqa: F401
