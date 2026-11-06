"""Reserved namespaces (plan assess-llm-runner, task C)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from vektra_shared.namespace import (
    RESERVED_NAMESPACES,
    is_reserved,
    refuse_reserved_namespace,
)


def test_assess_discussion_is_reserved_case_and_space_insensitive() -> None:
    assert "assess-discussion" in RESERVED_NAMESPACES
    assert is_reserved(" Assess-Discussion ") and not is_reserved("default")


def test_refusal_is_the_403_envelope() -> None:
    with pytest.raises(HTTPException) as e:
        refuse_reserved_namespace("assess-discussion")
    assert e.value.status_code == 403
    assert e.value.detail["error"]["code"] == "ERR-NAMESPACE-001"
    refuse_reserved_namespace("course-101")  # no exception
