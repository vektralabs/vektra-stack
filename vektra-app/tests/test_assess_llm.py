"""Composition of the discussion runner (plan assess-llm-runner, task C)."""

from __future__ import annotations

from typing import Any

import pytest

from vektra_app.assess_llm import (
    AnalyticsTraceWriter,
    build_assess_llm,
    build_assess_runner,
    build_assess_safeguard,
)
from vektra_assess.config import VektraAssessSettings
from vektra_shared.safeguards import PassthroughSafeguard
from vektra_shared.startup import StartupValidationError
from vektra_testkit.recorders import RecordingLLMProvider


class _Real:
    async def pre_query(self, *a: Any) -> Any: ...
    async def post_retrieval(self, *a: Any) -> Any: ...
    async def pre_response(self, *a: Any) -> Any: ...


def _settings(**kw: Any) -> VektraAssessSettings:
    return VektraAssessSettings.model_validate(
        {"VEKTRA_ASSESS_LLM_MODEL": "ollama/x", **kw}
    )


def test_presidio_falling_open_is_a_startup_error() -> None:
    with pytest.raises(StartupValidationError, match="fell back to passthrough"):
        build_assess_safeguard(_settings(), factory=lambda _m: PassthroughSafeguard())


def test_a_loaded_presidio_is_returned() -> None:
    real = _Real()
    assert build_assess_safeguard(_settings(), factory=lambda _m: real) is real


def test_none_only_when_explicit() -> None:
    none_dev = _settings(VEKTRA_ASSESS_SAFEGUARD_MODE="none", VEKTRA_ENV="development")
    assert isinstance(build_assess_safeguard(none_dev), PassthroughSafeguard)
    assert _settings().safeguard_mode == "presidio"  # the default is the safe one


@pytest.mark.parametrize(
    "env",
    [{}, {"VEKTRA_ENV": "production"}, {"VEKTRA_ENV": "staging"}],
    ids=["unset", "production", "staging"],
)
def test_the_builder_itself_refuses_none_with_a_cloud_model(
    env: dict[str, str],
) -> None:
    """Review of #203: the rule holds wherever the safeguard is built, not only at step 13."""
    settings = _settings(VEKTRA_ASSESS_SAFEGUARD_MODE="none", **env)
    with pytest.raises(StartupValidationError, match="LOCALITY=cloud") as e:
        build_assess_safeguard(settings)
    assert "with a cloud model the safeguard is required" in e.value.remediation
    onprem = _settings(
        VEKTRA_ASSESS_SAFEGUARD_MODE="none",
        VEKTRA_ASSESS_PROVIDER_LOCALITY="onprem",
        **env,
    )
    assert isinstance(build_assess_safeguard(onprem), PassthroughSafeguard)


def test_the_assess_llm_has_its_own_model_and_no_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VEKTRA_LLM_PROVIDER", "openai/gpt-rag")
    monkeypatch.setenv("VEKTRA_LLM_FALLBACK_MODEL", "openai/gpt-fallback")
    monkeypatch.setenv("VEKTRA_LLM_API_BASE", "http://rag-endpoint")
    llm = build_assess_llm(_settings(VEKTRA_ASSESS_LLM_API_BASE="http://assess:11434"))
    cfg = llm._config  # type: ignore[attr-defined]
    assert cfg.provider == "ollama/x" and cfg.fallback_model is None
    assert cfg.api_base == "http://assess:11434" and cfg.api_key is None


def test_the_assess_llm_uses_the_shared_key_and_its_own_extra_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M1.5a (OQ-026, decision of 2026-11-06): the key is the RAG's VEKTRA_LLM_API_KEY, the extra
    body is VEKTRA_ASSESS_LLM_EXTRA_BODY; the RAG's extra body and fallback never leak in."""
    monkeypatch.setenv("VEKTRA_ASSESS_LLM_MODEL", "hosted_vllm/qwen3.6")
    monkeypatch.setenv("VEKTRA_ASSESS_LLM_API_BASE", "http://vllm.example:8000/v1")
    monkeypatch.setenv("VEKTRA_LLM_API_KEY", "sk-shared-test-key")
    monkeypatch.setenv(
        "VEKTRA_ASSESS_LLM_EXTRA_BODY",
        '{"chat_template_kwargs": {"enable_thinking": false}}',
    )
    monkeypatch.setenv("VEKTRA_LLM_EXTRA_BODY", '{"rag": true}')
    monkeypatch.setenv("VEKTRA_LLM_FALLBACK_MODEL", "openai/gpt-fallback")
    settings = VektraAssessSettings()
    assert "sk-shared-test-key" not in repr(settings)
    llm = build_assess_llm(settings)
    cfg = llm._config  # type: ignore[attr-defined]
    assert cfg.api_key == "sk-shared-test-key"
    assert cfg.extra_body == {"chat_template_kwargs": {"enable_thinking": False}}
    assert cfg.fallback_model is None
    kwargs = llm._base_kwargs  # type: ignore[attr-defined]
    assert kwargs["extra_body"] == cfg.extra_body and kwargs["api_key"] == cfg.api_key


@pytest.mark.parametrize("empty", ["", "   "])
def test_empty_compose_values_mean_none(
    monkeypatch: pytest.MonkeyPatch, empty: str
) -> None:
    """The worker gets ``${VAR:-}`` from compose: an empty value is "unset", not an error."""
    monkeypatch.setenv("VEKTRA_ASSESS_LLM_MODEL", "ollama/x")
    for name in (
        "VEKTRA_LLM_API_KEY",
        "VEKTRA_ASSESS_LLM_EXTRA_BODY",
        "VEKTRA_ASSESS_LLM_API_BASE",
    ):
        monkeypatch.setenv(name, empty)
    s = VektraAssessSettings()
    assert (s.llm_api_key, s.llm_extra_body, s.llm_api_base) == (None, None, None)
    cfg = build_assess_llm(s)._config  # type: ignore[attr-defined]
    assert cfg.api_key is None and cfg.extra_body is None


@pytest.mark.parametrize("bad", ["{not json", "[1, 2]", '"text"'])
def test_a_malformed_extra_body_refuses_startup(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    from pydantic import ValidationError

    monkeypatch.setenv("VEKTRA_ASSESS_LLM_EXTRA_BODY", bad)
    with pytest.raises(ValidationError):
        VektraAssessSettings()


def test_no_model_is_a_startup_error() -> None:
    with pytest.raises(StartupValidationError, match="VEKTRA_ASSESS_LLM_MODEL"):
        build_assess_llm(VektraAssessSettings())


async def test_the_trace_writer_stores_through_analytics_and_commits() -> None:
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from vektra_shared.types import QueryTrace

    session = AsyncMock()

    class _Factory:
        async def __aenter__(self) -> Any:
            return session

        async def __aexit__(self, *a: Any) -> None:
            return None

    trace = QueryTrace(
        uuid4(), [], 1, [], "m", "abcd1234", datetime.now(UTC), "assess-discussion"
    )
    await AnalyticsTraceWriter(lambda: _Factory()).write(trace, "assess-discussion")
    orm = session.add.call_args[0][0]
    assert (
        orm.namespace_id == "assess-discussion" and orm.pipeline == "assess-discussion"
    )
    session.commit.assert_awaited_once()


def test_build_runner_wires_the_injected_parts() -> None:
    llm = RecordingLLMProvider([])
    r = build_assess_runner(
        _settings(), safeguard=_Real(), trace_writer=AnalyticsTraceWriter(lambda: None),
        audit=_audit(), llm=llm,
    )  # fmt: skip
    assert r.model == "ollama/x" and r._llm is llm  # type: ignore[attr-defined]


def _audit() -> Any:
    async def audit(event: str, data: Any) -> None:
        return None

    return audit


def test_the_worker_part_b_runner_uses_an_explicit_passthrough() -> None:
    from vektra_app.assess_llm import WorkerTraceWriter, build_worker_part_b_runner

    r = build_worker_part_b_runner(
        _settings(),
        acquire_db=lambda: None,
        audit=_audit(),
        llm=RecordingLLMProvider([]),
    )
    assert isinstance(r._safeguard, PassthroughSafeguard)  # type: ignore[attr-defined]
    assert isinstance(r._trace_writer, WorkerTraceWriter)  # type: ignore[attr-defined]


def test_the_worker_module_does_not_pull_presidio() -> None:
    import subprocess
    import sys

    code = (
        "import sys, vektra_app.assess_llm as m; "
        "m.build_worker_part_b_runner; "
        "print([x for x in sys.modules if x.startswith(('presidio', 'spacy', 'vektra_analytics'))])"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]"


# --- startup step 13 ----------------------------------------------------------------------------


class _App:
    def __init__(self) -> None:
        from types import SimpleNamespace

        self.state = SimpleNamespace()


async def test_step_13_is_dormant_when_assess_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vektra_app.assess_llm import step_13_assess_llm_check

    monkeypatch.delenv("VEKTRA_ASSESS_ENABLED", raising=False)
    app = _App()
    await step_13_assess_llm_check(
        app, safeguard_factory=lambda _m: PassthroughSafeguard()
    )
    assert vars(app.state) == {}


async def test_step_13_refuses_a_safeguard_that_fell_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vektra_app.assess_llm import step_13_assess_llm_check

    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    monkeypatch.setenv("VEKTRA_ASSESS_LLM_MODEL", "ollama/x")
    with pytest.raises(StartupValidationError) as e:
        await step_13_assess_llm_check(
            _App(), safeguard_factory=lambda _m: PassthroughSafeguard()
        )
    assert e.value.step == "assess_llm_check"


async def test_step_13_refuses_a_missing_model(monkeypatch: pytest.MonkeyPatch) -> None:
    from vektra_app.assess_llm import step_13_assess_llm_check

    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    monkeypatch.delenv("VEKTRA_ASSESS_LLM_MODEL", raising=False)
    with pytest.raises(StartupValidationError, match="VEKTRA_ASSESS_LLM_MODEL"):
        await step_13_assess_llm_check(_App(), safeguard_factory=lambda _m: _Real())


async def test_step_13_keeps_the_parts_for_the_turn_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vektra_app.assess_llm import step_13_assess_llm_check

    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    monkeypatch.setenv("VEKTRA_ASSESS_LLM_MODEL", "ollama/x")
    monkeypatch.setenv(
        "VEKTRA_ASSESS_LLM_ALLOW_UNACTIVATED", "true"
    )  # no activation table here
    monkeypatch.setenv("VEKTRA_ENV", "development")
    app = _App()
    real = _Real()
    await step_13_assess_llm_check(app, safeguard_factory=lambda _m: real)
    assert app.state.assess_safeguard is real
    assert app.state.assess_llm._config.provider == "ollama/x"
    assert len(app.state.assess_prompt_version) == 8


def test_step_13_runs_after_step_12_in_the_api_startup() -> None:
    import inspect

    import vektra_app.main as main

    src = inspect.getsource(main)
    assert src.index('"assess_check"') < src.index('"assess_llm_check"')


def test_a_real_presidio_load_failure_refuses_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not a stub factory: vektra_core's own create_safeguard, with Presidio failing to import."""
    import builtins

    real_import = builtins.__import__

    def failing(name: str, *a: Any, **k: Any) -> Any:
        if name.startswith("vektra_core.safeguards.presidio") or name.startswith(
            "presidio"
        ):
            raise ImportError("presidio not installed")
        return real_import(name, *a, **k)

    import sys

    monkeypatch.delitem(sys.modules, "vektra_core.safeguards.presidio", raising=False)
    monkeypatch.setattr(builtins, "__import__", failing)
    with pytest.raises(StartupValidationError, match="fell back to passthrough"):
        build_assess_safeguard(_settings())


# --- NFR-023 activation at startup ---------------------------------------------------------------


class _Conn:
    def __init__(self, active: bool) -> None:
        self.active = active
        self.sql: list[str] = []

    async def execute(self, stmt: Any, params: Any = None) -> Any:
        self.sql.append(str(stmt))
        active = self.active

        class _R:
            def first(self) -> Any:
                return (1,) if active else None

        return _R()


class _Engine:
    def __init__(self, active: bool) -> None:
        self.conn = _Conn(active)

    def connect(self) -> Any:
        conn = self.conn

        class _Ctx:
            async def __aenter__(self) -> _Conn:
                return conn

            async def __aexit__(self, *a: Any) -> None:
                return None

        return _Ctx()


def _enabled(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    monkeypatch.setenv("VEKTRA_ASSESS_ENABLED", "true")
    monkeypatch.setenv("VEKTRA_ASSESS_LLM_MODEL", "ollama/x")
    for k, v in env.items():
        monkeypatch.setenv(k, v)


async def test_step_13_starts_degraded_on_an_unactivated_triple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The activation endpoint lives in this API: an unactivated triple must not stop startup
    (nothing could ever activate it), it is recorded so no discussion is served."""
    from vektra_app.assess_llm import step_13_assess_llm_check

    _enabled(monkeypatch)
    app = _App()
    app.state.assess_engine = _Engine(active=False)
    await step_13_assess_llm_check(app, safeguard_factory=lambda _m: _Real())
    assert app.state.assess_llm_activated is False
    assert "assess_model_activations" in app.state.assess_engine.conn.sql[0]


async def test_step_13_accepts_an_activated_triple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vektra_app.assess_llm import step_13_assess_llm_check

    _enabled(monkeypatch)
    app = _App()
    app.state.assess_engine = _Engine(active=True)
    await step_13_assess_llm_check(app, safeguard_factory=lambda _m: _Real())
    assert app.state.assess_prompt_version and app.state.assess_llm_activated is True


async def test_changing_a_template_changes_prompt_version_and_deactivates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The activation is keyed by prompt_version: a template edit makes a new, unactivated triple."""
    import shutil

    from vektra_app import assess_llm
    from vektra_assess.llm import prompts
    from vektra_assess.llm.postcheck.lexicon import LEXICON_VERSION

    activated = prompts.PromptRenderer(LEXICON_VERSION).prompt_version
    t = tmp_path / "templates"
    shutil.copytree(prompts.TEMPLATES_DIR, t)
    (t / "system.j2").write_text((t / "system.j2").read_text() + " ", encoding="utf-8")
    monkeypatch.setattr(prompts, "TEMPLATES_DIR", t)
    monkeypatch.setattr(
        prompts.PromptRenderer.__init__, "__defaults__", (t, prompts.STRINGS_DIR)
    )
    edited = prompts.PromptRenderer(LEXICON_VERSION).prompt_version
    assert edited != activated

    class _Only(_Engine):
        def __init__(self) -> None:
            super().__init__(active=False)
            outer = self

            async def execute(stmt: Any, params: Any = None) -> Any:
                outer.conn.sql.append(str(stmt))
                ok = params["p"] == activated

                class _R:
                    def first(self) -> Any:
                        return (1,) if ok else None

                return _R()

            self.conn.execute = execute  # type: ignore[method-assign]

    _enabled(monkeypatch)
    app = _App()
    app.state.assess_engine = _Only()
    await assess_llm.step_13_assess_llm_check(app, safeguard_factory=lambda _m: _Real())
    assert app.state.assess_prompt_version == edited
    assert app.state.assess_llm_activated is False


async def test_the_dev_bypass_is_refused_outside_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vektra_app.assess_llm import step_13_assess_llm_check

    _enabled(
        monkeypatch, VEKTRA_ASSESS_LLM_ALLOW_UNACTIVATED="true", VEKTRA_ENV="production"
    )
    with pytest.raises(
        StartupValidationError, match="only with VEKTRA_ENV=development"
    ):
        await step_13_assess_llm_check(_App(), safeguard_factory=lambda _m: _Real())
    _enabled(monkeypatch, VEKTRA_ENV="development")
    app = _App()
    app.state.assess_engine = _Engine(active=False)
    await step_13_assess_llm_check(
        app, safeguard_factory=lambda _m: _Real()
    )  # allowed in dev
    assert app.state.assess_llm_activated is True


# --- the safeguard and the model's locality (plan 2a-1.11, product owner 2026-11-11) -------------
#
# | VEKTRA_ENV      | VEKTRA_ASSESS_PROVIDER_LOCALITY | VEKTRA_ASSESS_SAFEGUARD_MODE=none |
# | development     | any                             | accepted                          |
# | not development | onprem                          | accepted                          |
# | not development | cloud (the default)             | refused at startup                |

_CLOUD_REFUSAL = (
    "VEKTRA_ASSESS_SAFEGUARD_MODE=none with VEKTRA_ASSESS_PROVIDER_LOCALITY=cloud is only "
    "honoured with VEKTRA_ENV=development"
)


@pytest.mark.parametrize("locality", ["cloud", "onprem"])
async def test_none_is_accepted_in_development_with_any_locality(
    monkeypatch: pytest.MonkeyPatch, locality: str
) -> None:
    from vektra_app.assess_llm import step_13_assess_llm_check

    _enabled(
        monkeypatch,
        VEKTRA_ENV="development",
        VEKTRA_ASSESS_SAFEGUARD_MODE="none",
        VEKTRA_ASSESS_PROVIDER_LOCALITY=locality,
    )
    app = _App()
    app.state.assess_engine = _Engine(active=True)
    await step_13_assess_llm_check(app)
    assert isinstance(app.state.assess_safeguard, PassthroughSafeguard)


@pytest.mark.parametrize("env", ["production", "staging"])
async def test_none_is_accepted_outside_development_with_an_onprem_model(
    monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    from vektra_app.assess_llm import step_13_assess_llm_check

    _enabled(
        monkeypatch,
        VEKTRA_ENV=env,
        VEKTRA_ASSESS_SAFEGUARD_MODE="none",
        VEKTRA_ASSESS_PROVIDER_LOCALITY="onprem",
    )
    app = _App()
    app.state.assess_engine = _Engine(active=True)
    await step_13_assess_llm_check(app)
    assert isinstance(app.state.assess_safeguard, PassthroughSafeguard)


@pytest.mark.parametrize("locality", [None, "cloud"])  # None: the default, cloud
@pytest.mark.parametrize("env", ["production", "staging"])
async def test_none_is_refused_outside_development_with_a_cloud_model(
    monkeypatch: pytest.MonkeyPatch, env: str, locality: str | None
) -> None:
    from vektra_app.assess_llm import step_13_assess_llm_check

    extra = {"VEKTRA_ASSESS_PROVIDER_LOCALITY": locality} if locality else {}
    monkeypatch.delenv("VEKTRA_ASSESS_PROVIDER_LOCALITY", raising=False)
    _enabled(monkeypatch, VEKTRA_ENV=env, VEKTRA_ASSESS_SAFEGUARD_MODE="none", **extra)
    app = _App()
    app.state.assess_engine = _Engine(active=True)
    with pytest.raises(StartupValidationError) as e:
        await step_13_assess_llm_check(app)
    err = e.value
    assert err.step == "assess_llm_check"
    assert _CLOUD_REFUSAL in err.detail and f"VEKTRA_ENV={env}" in err.detail
    assert "with a cloud model the safeguard is required" in err.remediation
    assert "VEKTRA_ASSESS_SAFEGUARD_MODE=presidio" in err.remediation
    assert "VEKTRA_ASSESS_PROVIDER_LOCALITY=onprem" in err.remediation
    assert not hasattr(
        app.state, "assess_safeguard"
    )  # refused before anything is built


async def test_presidio_is_still_the_default_and_starts_with_a_cloud_model_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vektra_app.assess_llm import step_13_assess_llm_check

    monkeypatch.delenv("VEKTRA_ASSESS_SAFEGUARD_MODE", raising=False)
    monkeypatch.delenv("VEKTRA_ASSESS_PROVIDER_LOCALITY", raising=False)
    _enabled(monkeypatch, VEKTRA_ENV="production")
    assert _settings().safeguard_mode == "presidio"
    assert _settings().provider_locality == "cloud"
    app = _App()
    app.state.assess_engine = _Engine(active=True)
    real = _Real()
    seen: list[str] = []
    await step_13_assess_llm_check(
        app, safeguard_factory=lambda m: seen.append(m) or real
    )
    assert seen == ["presidio"] and app.state.assess_safeguard is real


# --- the API's dev switches need VEKTRA_ENV=development set EXPLICITLY ------------------------

_API_DEV_SWITCHES = (
    {"VEKTRA_ASSESS_LLM_ALLOW_UNACTIVATED": "true"},
    {"VEKTRA_ASSESS_SAFEGUARD_MODE": "none"},  # with the default cloud locality
)


async def _step_13_with(
    monkeypatch: pytest.MonkeyPatch, switch: dict[str, str], env: str | None
) -> Any:
    from vektra_app.assess_llm import step_13_assess_llm_check

    monkeypatch.delenv("VEKTRA_ASSESS_PROVIDER_LOCALITY", raising=False)
    _enabled(monkeypatch, **switch)
    if env is None:
        monkeypatch.delenv("VEKTRA_ENV", raising=False)
    else:
        monkeypatch.setenv("VEKTRA_ENV", env)
    app = _App()
    app.state.assess_engine = _Engine(active=False)
    await step_13_assess_llm_check(app, safeguard_factory=lambda _m: _Real())
    return app


@pytest.mark.parametrize("switch", _API_DEV_SWITCHES, ids=lambda s: next(iter(s)))
async def test_an_api_dev_switch_with_vektra_env_unset_is_refused(
    monkeypatch: pytest.MonkeyPatch, switch: dict[str, str]
) -> None:
    with pytest.raises(StartupValidationError) as e:
        await _step_13_with(monkeypatch, switch, None)
    assert "VEKTRA_ENV unset" in e.value.detail
    assert "set VEKTRA_ENV=development explicitly" in e.value.detail


@pytest.mark.parametrize("switch", _API_DEV_SWITCHES, ids=lambda s: next(iter(s)))
async def test_an_api_dev_switch_with_explicit_development_is_accepted(
    monkeypatch: pytest.MonkeyPatch, switch: dict[str, str]
) -> None:
    app = await _step_13_with(monkeypatch, switch, "development")
    assert app.state.assess_runner is not None


@pytest.mark.parametrize("switch", _API_DEV_SWITCHES, ids=lambda s: next(iter(s)))
async def test_an_api_dev_switch_with_explicit_production_is_refused(
    monkeypatch: pytest.MonkeyPatch, switch: dict[str, str]
) -> None:
    with pytest.raises(StartupValidationError, match="VEKTRA_ENV=production"):
        await _step_13_with(monkeypatch, switch, "production")


def test_prometheus_metrics_count_with_code_labels() -> None:
    from prometheus_client import REGISTRY

    from vektra_app.assess_llm import PrometheusRunnerMetrics

    m = PrometheusRunnerMetrics()
    PrometheusRunnerMetrics()  # a second instance reuses the registered counters

    def val(name: str, **labels: str) -> float:
        return REGISTRY.get_sample_value(name, labels) or 0.0

    before = val("vektra_assess_postcheck_replacements_total", stage="lexicon")
    m.replacement("lexicon", 2)
    m.behaviour_class("R-SOL")
    m.call_outcome("first_token_timeout")
    m.part_b_rejected("part_b_value")
    assert (
        val("vektra_assess_postcheck_replacements_total", stage="lexicon") == before + 2
    )
    assert val("vektra_assess_behaviour_class_total", behaviour_class="R-SOL") >= 1
    assert val("vektra_assess_provider_calls_total", outcome="first_token_timeout") >= 1
    assert val("vektra_assess_part_b_rejections_total", code="part_b_value") >= 1


def test_a_lazily_failing_presidio_is_caught_at_startup() -> None:
    """vektra_core's Presidio loads its engines on first use and falls open there: the build
    loads them now, so a missing spaCy model stops startup instead of passing every turn."""

    class LazyPresidio(_Real):
        def _ensure_engines(self) -> bool:
            return False  # no spaCy model installed

    with pytest.raises(StartupValidationError, match="engines failed to load"):
        build_assess_safeguard(_settings(), factory=lambda _m: LazyPresidio())

    class LoadedPresidio(_Real):
        def _ensure_engines(self) -> bool:
            return True

    assert isinstance(
        build_assess_safeguard(_settings(), factory=lambda _m: LoadedPresidio()),
        LoadedPresidio,
    )
