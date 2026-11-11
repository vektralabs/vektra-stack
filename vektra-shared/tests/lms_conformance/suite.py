"""The provider-parametrised conformance suite (ADR-0033 D10).

A provider's test module subclasses it and names its target::

    from lms_conformance.suite import LmsConformanceSuite

    class TestMyProviderConformance(LmsConformanceSuite):
        target = my_conformance_target()

The class name does not start with ``Test``, so pytest never collects the suite on its own: it
only ever runs bound to a provider. A subclass without a ``target`` fails, it does not skip.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from lms_conformance import kit


class LmsConformanceSuite:
    target: ClassVar[kit.ConformanceTarget | None] = None

    @pytest.fixture
    def lms_target(self) -> kit.ConformanceTarget:
        if self.target is None:
            pytest.fail(f"{type(self).__name__} names no conformance target")
        return self.target

    def test_the_kit_covers_every_protocol_member(self) -> None:
        """False-pass guard: a member the kit does not call is a member nobody checks."""
        covered = set(kit.READING_MEMBERS) | kit.OTHER_MEMBERS
        assert covered == kit.protocol_members()

    def test_it_implements_the_protocol(
        self, lms_target: kit.ConformanceTarget
    ) -> None:
        kit.check_protocol(lms_target.factory(kit.TransportMode.RECORDED))

    async def test_every_read_on_the_recording_is_a_reading(
        self, lms_target: kit.ConformanceTarget
    ) -> None:
        await kit.check_recorded_reads(lms_target)

    @pytest.mark.parametrize("mode", kit.FAULT_MODES, ids=str)
    async def test_a_transport_fault_is_unreadable_and_never_raises(
        self, lms_target: kit.ConformanceTarget, mode: kit.TransportMode
    ) -> None:
        await kit.check_fault_mode(lms_target, mode)

    async def test_an_activity_outside_the_users_courses_is_refused(
        self, lms_target: kit.ConformanceTarget
    ) -> None:
        await kit.check_re_resolution(lms_target)

    async def test_no_canary_reaches_a_returned_value(
        self, lms_target: kit.ConformanceTarget
    ) -> None:
        await kit.check_no_canary(lms_target)

    async def test_no_credential_reaches_a_log_line(
        self, lms_target: kit.ConformanceTarget
    ) -> None:
        await kit.check_no_secret_in_logs(lms_target)

    async def test_the_declared_setup_items_are_the_returned_ones(
        self, lms_target: kit.ConformanceTarget
    ) -> None:
        await kit.check_setup_items(lms_target)

    async def test_one_capability_result_per_requested_flag(
        self, lms_target: kit.ConformanceTarget
    ) -> None:
        await kit.check_capabilities(lms_target)

    async def test_startup_problems_and_aclose(
        self, lms_target: kit.ConformanceTarget
    ) -> None:
        await kit.check_lifecycle(lms_target)
