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

from agent.context_engine import ContextEngine
from hermes_state import SessionDB
from run_agent import AIAgent


def _config(max_attempts=None, *, openai_native=None, context_engine=None) -> dict:
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
    session_db=True,
):
    from hermes_cli import config as config_mod

    def config():
        return _config(
            max_attempts=max_attempts,
            openai_native=openai_native,
            context_engine=context_engine,
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
    agent = _make_agent(monkeypatch, tmp_path, openai_native=True)

    assert agent.native_compaction_policy.feature_enabled is True
    assert agent.native_compaction_policy.is_eligible(
        client=agent.client,
        provider=agent.provider,
        api_mode=agent.api_mode,
        base_url=agent.base_url,
    )


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

    assert not agent.native_compaction_policy.built_in_compressor
    assert not agent.native_compaction_policy.is_eligible(
        client=agent.client,
        provider=agent.provider,
        api_mode=agent.api_mode,
        base_url=agent.base_url,
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
