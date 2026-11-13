"""litellm's own exception classes, built as litellm builds them (the classifier's contract).

With no API key configured, litellm maps the OpenAI client's failure through its OpenAI handler,
whose error carries a synthetic 500 response: the resulting ``AuthenticationError`` reports
``status_code=500`` (the ``openai.APIStatusError`` base takes the status from the response).
``litellm_error(name, status)`` reproduces that for any class that keeps the given response;
``RateLimitError`` always builds its own 429.
"""

from __future__ import annotations

import httpx
import pytest

#: a reserved host: nothing here resolves or is contacted
REQUEST_URL = "https://provider.invalid/v1"


def litellm_error(name: str, status: int, *, message: str = "refused") -> BaseException:
    litellm = pytest.importorskip("litellm")
    response = httpx.Response(status, request=httpx.Request("POST", REQUEST_URL))
    return getattr(litellm, name)(
        message=message, model="m", llm_provider="openai", response=response
    )
