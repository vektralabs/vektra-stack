"""Recording doubles conform to the Protocols they stand in for (plan assess-context A)."""

from __future__ import annotations

import inspect
import json
from typing import Any
from uuid import uuid4

import pytest

from vektra_shared.errors import ActiveIndexVersionError
from vektra_shared.protocols import LLMProvider, SafeguardHook, VectorStoreProvider
from vektra_shared.registry import ProviderRegistry
from vektra_shared.testing_recorders import (
    ForbiddenProviderCall,
    RecordingLLMProvider,
    RecordingSafeguardHook,
    RecordingVectorStoreProvider,
    ScriptExhausted,
    assert_no_canaries,
    canonical_bytes,
    find_canaries,
    is_recorder,
)
from vektra_shared.types import Message, QueryEmbedding, SafeguardContext

# mypy checks these assignments structurally: a missing or mistyped method fails type-checking
_llm: LLMProvider = RecordingLLMProvider()
_vs: VectorStoreProvider = RecordingVectorStoreProvider()
_sg: SafeguardHook = RecordingSafeguardHook()


def _public_methods(cls: type) -> dict[str, inspect.Signature]:
    return {
        n: inspect.signature(getattr(cls, n))
        for n in dir(cls)
        if not n.startswith("_") and callable(getattr(cls, n))
    }


@pytest.mark.parametrize(
    ("protocol", "double"),
    [
        (LLMProvider, RecordingLLMProvider),
        (VectorStoreProvider, RecordingVectorStoreProvider),
        (SafeguardHook, RecordingSafeguardHook),
    ],
)
def test_every_protocol_method_exists_with_the_same_parameters(
    protocol: type, double: type
) -> None:
    proto = {
        n: s
        for n, s in _public_methods(protocol).items()
        if n in protocol.__dict__  # the Protocol's own methods, not object's
    }
    assert proto, "the protocol exposes methods"
    impl = _public_methods(double)
    for name, sig in proto.items():
        assert name in impl, f"{double.__name__} lacks {name}"
        assert list(impl[name].parameters) == list(sig.parameters), name
        for pname, p in sig.parameters.items():
            assert impl[name].parameters[pname].default == p.default, (name, pname)
        assert inspect.iscoroutinefunction(getattr(double, name)) == (
            inspect.iscoroutinefunction(getattr(protocol, name))
        ), name


def test_runtime_checkable_protocols_accept_the_doubles() -> None:
    assert isinstance(RecordingLLMProvider(), LLMProvider)
    assert isinstance(RecordingVectorStoreProvider(), VectorStoreProvider)


async def test_llm_records_exact_request_bytes_and_plays_the_script() -> None:
    llm = RecordingLLMProvider(["primo", "secondo turno"])
    msgs = [Message("system", "sei un tutor"), Message("user", "perché è sbagliata?")]
    r1 = await llm.complete(msgs, "m", temperature=0.2, max_tokens=50, seed=1)
    stream = await llm.stream(msgs, "m")
    text = "".join([c.content async for c in stream])
    assert (r1.content, text) == ("primo", "secondo turno")
    assert len(llm.all_request_bytes) == 2
    body = json.loads(llm.all_request_bytes[0])
    assert body == {
        "messages": [
            {"role": "system", "content": "sei un tutor"},
            {"role": "user", "content": "perché è sbagliata?"},
        ],
        "model": "m",
        "temperature": 0.2,
        "max_tokens": 50,
        "params": {"seed": 1},
    }
    assert "perché".encode() in llm.all_request_bytes[0]  # UTF-8, not \\u escapes
    with pytest.raises(ScriptExhausted):
        await llm.complete(msgs, "m")


async def test_forbidden_vector_store_records_then_raises() -> None:
    vs = RecordingVectorStoreProvider(forbid_calls=True)
    with pytest.raises(ForbiddenProviderCall):
        await vs.search("ns", QueryEmbedding(dense=[0.1]), top_k=3)
    with pytest.raises(ForbiddenProviderCall):
        await vs.retrieve("ns", ["a"])
    assert [c.method for c in vs.calls] == ["search", "retrieve"]
    await vs.health_check()  # not a retrieval call
    assert len(vs.calls) == 2


async def test_vector_store_refuses_to_delete_its_active_version() -> None:
    vs = RecordingVectorStoreProvider(active_index_version=3)
    assert await vs.delete_index_version("ns", 2) == 0
    with pytest.raises(ActiveIndexVersionError):
        await vs.delete_index_version("ns", 3)


async def test_safeguard_records_the_three_boundaries() -> None:
    sg, ctx = RecordingSafeguardHook(), SafeguardContext()
    assert (await sg.pre_query("q", ctx)).allowed
    await sg.post_retrieval("q", [], ctx)
    await sg.pre_response("r", ctx)
    assert [c.method for c in sg.calls] == [
        "pre_query",
        "post_retrieval",
        "pre_response",
    ]


def test_canary_search_walks_every_shape() -> None:
    blob: Any = {
        "a": ["x CNRY-1 y", b"CNRY-2", ("z", {"k": "CNRY-1"})],
        "CNRY-3-key": None,
        "msg": Message("user", "CNRY-2 again CNRY-2"),
    }
    assert find_canaries(blob, ["CNRY-1", "CNRY-2", "CNRY-3", "CNRY-4"]) == {
        "CNRY-1": 2,
        "CNRY-2": 3,
        "CNRY-3": 1,
        "CNRY-4": 0,
    }
    assert_no_canaries(blob, ["CNRY-4"])
    with pytest.raises(
        AssertionError, match=r"payload: 2 canary hit\(s\): \['CNRY-1'\]"
    ):
        assert_no_canaries(blob, ["CNRY-1"], sink="payload")


def test_canonical_bytes_are_stable() -> None:
    assert canonical_bytes({"b": 1, "a": {"y", "x"}}) == b'{"a":["x","y"],"b":1}'


def test_a_production_registry_refuses_the_doubles() -> None:
    reg = ProviderRegistry()
    for double in (RecordingLLMProvider(), RecordingVectorStoreProvider()):
        assert is_recorder(double)
        with pytest.raises(ValueError, match="test double"):
            reg.register("llm", "x", double)
    assert reg.list("llm") == []
    test_reg = ProviderRegistry(allow_test_doubles=True)
    test_reg.register("llm", "rec", RecordingLLMProvider())
    assert test_reg.has("llm", "rec")
    assert not is_recorder(object()) and not is_recorder(uuid4())
