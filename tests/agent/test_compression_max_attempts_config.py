"""compression.max_attempts — config-driven compression retry cap.

The conversation loop's compression retry cap was hardcoded to 3, stranding
sessions that legitimately need more rounds — e.g. a restart history reload
whose incompressible tool schemas keep the request estimate above the
threshold while the messages themselves compress fine (the #62605 failure
class).  The cap is now parsed from ``compression.max_attempts`` in
``agent_init`` and read by the loop via
``getattr(agent, "max_compression_attempts", 3)``.

These tests pin the parse/validate/attach seam: default preserved, custom
value honored, floor and ceiling enforced, garbage tolerated.
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

import pytest

from agent.context_engine import ContextEngine
from hermes_state import SessionDB
from run_agent import AIAgent


def _config(
    max_attempts=None,
    *,
    openai_native=None,
    context_engine=None,
    native_keep_recent_tokens=None,
) -> dict:
    compression = {
        "enabled": True,
        "threshold": 0.50,
        "target_ratio": 0.20,
        "protect_first_n": 3,
        "protect_last_n": 20,
    }
    if max_attempts is not None:
        compression["max_attempts"] = max_attempts
    if openai_native is not None:
        compression["openai_native"] = openai_native
    context = {}
    if context_engine is not None:
        context["engine"] = context_engine
    if native_keep_recent_tokens is not None:
        context["openai_native"] = {
            "keep_recent_tokens": native_keep_recent_tokens
        }
    return {
        "compression": compression,
        "context": context,
        "prompt_caching": {"cache_ttl": "5m"},
        "sessions": {},
        "bedrock": {},
    }


def _make_agent(
    monkeypatch,
    tmp_path: Path,
    *,
    max_attempts=None,
    openai_native=None,
    context_engine=None,
    native_keep_recent_tokens=None,
    session_db=True,
):
    from hermes_cli import config as config_mod

    def config():
        return _config(
            max_attempts=max_attempts,
            openai_native=openai_native,
            context_engine=context_engine,
            native_keep_recent_tokens=native_keep_recent_tokens,
        )

    monkeypatch.setattr(config_mod, "load_config", config)
    monkeypatch.setattr(config_mod, "load_config_readonly", config)
    db = SessionDB(db_path=tmp_path / "state.db") if session_db else None
    with contextlib.redirect_stdout(io.StringIO()):
        agent = AIAgent(
            base_url="https://chatgpt.com/backend-api/codex",
            api_key="test-key",
            provider="openai-codex",
            model="gpt-5.5",
            enabled_toolsets=[],
            disabled_toolsets=[],
            quiet_mode=True,
            skip_memory=True,
            session_db=db,
            session_id="max-attempts-test",
        )
    return agent


class _CustomContextEngine(ContextEngine):
    @property
    def name(self):
        return "task4-custom"

    def __init__(self):
        self.context_length = 128_000
        self.threshold_percent = 0.5
        self.threshold_tokens = 64_000

    def update_model(self, **kwargs):
        return None

    def update_from_response(self, usage):
        return None

    def should_compress(self, prompt_tokens=None):
        return False

    def compress(self, messages, **kwargs):
        return messages


def test_native_openai_compaction_defaults_false():
    from hermes_cli.config import openai_native_compaction_enabled
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["compression"]["openai_native"] is False
    assert openai_native_compaction_enabled({}) is False
    assert openai_native_compaction_enabled({"compression": {}}) is False


def test_explicit_native_openai_compaction_boolean_is_loaded():
    from hermes_cli.config import openai_native_compaction_enabled

    assert openai_native_compaction_enabled({"compression": {"openai_native": True}})
    assert not openai_native_compaction_enabled(
        {"compression": {"openai_native": False}}
    )


def test_invalid_native_openai_compaction_values_normalize_false_without_mutation():
    from hermes_cli.config import openai_native_compaction_enabled

    invalid_values = ["true", "false", 1, 0, 1.0, [], {}, None, object()]
    for value in invalid_values:
        config = {"compression": {"openai_native": value}}
        before = config["compression"]["openai_native"]
        assert openai_native_compaction_enabled(config) is False
        assert config["compression"]["openai_native"] is before


def test_agent_init_loads_native_compaction_config_and_current_route(monkeypatch, tmp_path):
    from agent.openai_native_context_engine import OpenAINativeContextEngine

    agent = _make_agent(monkeypatch, tmp_path, openai_native=True)

    assert type(agent.context_compressor) is OpenAINativeContextEngine
    assert agent.context_compressor.native_keep_recent_tokens == 20_000
    assert agent.native_compaction_policy.feature_enabled is True
    assert agent.native_compaction_policy.is_eligible(
        client=agent.client,
        provider=agent.provider,
        api_mode=agent.api_mode,
        base_url=agent.base_url,
    )


def test_legacy_native_openai_compaction_loads_and_validates_custom_token_budget(
    monkeypatch, tmp_path
):
    agent = _make_agent(
        monkeypatch,
        tmp_path,
        openai_native=True,
        native_keep_recent_tokens=32_000,
    )

    assert agent.context_compressor.native_keep_recent_tokens == 32_000

    with pytest.raises(
        ValueError, match="native_keep_recent_tokens must be a positive integer"
    ):
        _make_agent(
            monkeypatch,
            tmp_path,
            openai_native=True,
            native_keep_recent_tokens="invalid",
        )


def test_agent_init_caches_bound_session_native_checkpoint_once(monkeypatch, tmp_path):
    sentinel = object()
    loads = []

    def _load_checkpoint(_db, session_id):
        loads.append(session_id)
        return sentinel

    monkeypatch.setattr(SessionDB, "load_native_openai_checkpoint", _load_checkpoint)

    agent = _make_agent(monkeypatch, tmp_path, openai_native=True)

    assert agent._native_openai_checkpoint is sentinel
    assert loads == [agent.session_id]


def test_native_checkpoint_cache_rebinds_once_to_new_session():
    from types import SimpleNamespace

    from agent.chat_completion_helpers import bind_native_openai_checkpoint_cache

    loaded = object()
    loads = []
    agent = SimpleNamespace(
        _session_db=SimpleNamespace(
            load_native_openai_checkpoint=lambda session_id: (
                loads.append(session_id) or loaded
            )
        ),
        _native_openai_checkpoint=object(),
        _native_openai_checkpoint_session_id="old-session",
    )

    bind_native_openai_checkpoint_cache(agent, "new-session")

    assert agent._native_openai_checkpoint is loaded
    assert agent._native_openai_checkpoint_session_id == "new-session"
    assert loads == ["new-session"]


def test_native_checkpoint_cache_rebind_clears_stale_state_on_load_failure():
    from types import SimpleNamespace

    from agent.chat_completion_helpers import bind_native_openai_checkpoint_cache

    def _fail(_session_id):
        raise RuntimeError("unavailable")

    agent = SimpleNamespace(
        _session_db=SimpleNamespace(load_native_openai_checkpoint=_fail),
        _native_openai_checkpoint=object(),
        _native_openai_checkpoint_session_id="old-session",
    )

    bind_native_openai_checkpoint_cache(agent, "new-session")

    assert agent._native_openai_checkpoint is None
    assert agent._native_openai_checkpoint_session_id == "new-session"


def test_agent_init_marks_missing_session_state_ineligible(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch, tmp_path, openai_native=True, session_db=False)

    assert not agent.native_compaction_policy.has_session_state
    assert not agent.native_compaction_policy.is_eligible(
        client=agent.client,
        provider=agent.provider,
        api_mode=agent.api_mode,
        base_url=agent.base_url,
    )


def test_agent_init_marks_failed_session_state_binding_ineligible(monkeypatch, tmp_path):
    from agent.context_compressor import ContextCompressor

    def fail_binding(self, *, session_db, session_id):
        raise RuntimeError("binding failed")

    monkeypatch.setattr(ContextCompressor, "bind_session_state", fail_binding)

    agent = _make_agent(monkeypatch, tmp_path, openai_native=True)

    assert not agent.native_compaction_policy.has_session_state
    assert not agent.native_compaction_policy.is_eligible(
        client=agent.client,
        provider=agent.provider,
        api_mode=agent.api_mode,
        base_url=agent.base_url,
    )


def test_agent_init_marks_custom_context_engine_ineligible(monkeypatch, tmp_path):
    import plugins.context_engine as context_plugins

    monkeypatch.setattr(
        context_plugins,
        "load_context_engine",
        lambda name: _CustomContextEngine() if name == "task4-custom" else None,
    )
    agent = _make_agent(
        monkeypatch,
        tmp_path,
        openai_native=True,
        context_engine="task4-custom",
    )

    assert agent.context_compressor.name == "task4-custom"
    assert not agent.native_compaction_policy.built_in_compressor
    assert not agent.native_compaction_policy.is_eligible(
        client=agent.client,
        provider=agent.provider,
        api_mode=agent.api_mode,
        base_url=agent.base_url,
    )


def test_openai_native_context_engine_enables_native_policy_without_legacy_flag(
    monkeypatch, tmp_path
):
    from agent.openai_native_context_engine import OpenAINativeContextEngine

    agent = _make_agent(
        monkeypatch,
        tmp_path,
        openai_native=False,
        context_engine="openai-native",
    )

    assert type(agent.context_compressor) is OpenAINativeContextEngine
    assert agent.context_compressor.name == "openai-native"
    assert agent.context_compressor.native_keep_recent_tokens == 20_000
    assert agent.native_compaction_policy.feature_enabled is True
    assert agent.native_compaction_policy.is_eligible(
        client=agent.client,
        provider=agent.provider,
        api_mode=agent.api_mode,
        base_url=agent.base_url,
    )


def test_openai_native_context_engine_loads_token_budget_from_context_settings(
    monkeypatch, tmp_path
):
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["context"]["openai_native"]["keep_recent_tokens"] == 20_000

    agent = _make_agent(
        monkeypatch,
        tmp_path,
        context_engine="openai-native",
        native_keep_recent_tokens=32_000,
    )

    assert agent.context_compressor.native_keep_recent_tokens == 32_000


@pytest.mark.parametrize("invalid_budget", [0, -1, True, 1.5, "20000", "invalid"])
def test_openai_native_context_engine_rejects_invalid_token_budget(
    monkeypatch, tmp_path, invalid_budget
):
    with pytest.raises(ValueError, match="native_keep_recent_tokens must be a positive integer"):
        _make_agent(
            monkeypatch,
            tmp_path,
            context_engine="openai-native",
            native_keep_recent_tokens=invalid_budget,
        )


class TestCompressionMaxAttemptsConfig:
    def test_default_is_three_when_unset(self, monkeypatch, tmp_path):
        agent = _make_agent(monkeypatch, tmp_path)
        assert agent.max_compression_attempts == 3

    def test_custom_value_is_honored(self, monkeypatch, tmp_path):
        agent = _make_agent(monkeypatch, tmp_path, max_attempts=6)
        assert agent.max_compression_attempts == 6







    def test_loop_pickup_degrades_to_default_when_attribute_missing(
        self, monkeypatch, tmp_path
    ):
        # The loop reads getattr(agent, "max_compression_attempts", 3): a
        # configured agent exposes its value, and an object without the
        # attribute (older pickle / minimal stub) degrades to the prior
        # hardcoded behavior.
        agent = _make_agent(monkeypatch, tmp_path, max_attempts=7)
        assert getattr(agent, "max_compression_attempts", 3) == 7
        assert getattr(object(), "max_compression_attempts", 3) == 3
