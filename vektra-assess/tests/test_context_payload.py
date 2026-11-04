"""bundle_to_model_payload: the single serialization path to model input (NFR-017 sink 4)."""

from __future__ import annotations

from test_context_bundle import _frozen
from test_context_snapshot import make

from vektra_assess.context.assemble import assemble_context
from vektra_assess.context.payload import BEGIN, END, bundle_to_model_payload
from vektra_shared.testing_recorders import RecordingLLMProvider


async def test_payload_bytes_reach_the_provider_exactly() -> None:
    s = make("risposta CNRY-allowed-onlinetext")
    payload = bundle_to_model_payload(assemble_context(s, _frozen(s)))
    llm = RecordingLLMProvider(["ok"])
    await llm.complete(payload, "m")
    (sent,) = llm.all_request_bytes
    assert len(sent) > 0 and b"CNRY-allowed-onlinetext" in sent
    assert payload[0].content.startswith(BEGIN) and payload[0].content.endswith(END)


def test_payload_is_deterministic_and_has_only_labels() -> None:
    s = make()
    a = bundle_to_model_payload(assemble_context(s, _frozen(s)))
    b = bundle_to_model_payload(assemble_context(s, _frozen(s)))
    assert a == b
    text = a[0].content
    assert "Domanda 1" in text and "Domanda 2" in text
    for word in ("attemptid", "cmid", "slot", "userid"):
        assert word not in text


def test_solution_only_when_on() -> None:
    s = make()
    off = bundle_to_model_payload(assemble_context(s, _frozen(s)))[0].content
    assert "Soluzione fornita" not in off
