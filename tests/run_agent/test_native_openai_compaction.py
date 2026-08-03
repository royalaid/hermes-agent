"""Task 11 lifecycle fail-open behavior for native compaction projections."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent import chat_completion_helpers

from agent.chat_completion_helpers import (
    bind_native_openai_checkpoint_cache,
    maybe_apply_native_openai_projection,
    native_openai_identity_for_agent,
)
from agent.native_openai_compaction import (
    NativeCompactionCheckpoint,
    NativeCompactionIdentity,
    canonical_input_sha256,
)
from hermes_state import SessionDB


OPAQUE = [{"type": "compaction", "encrypted_content": "opaque-checkpoint"}]
ORDINARY = [
    {"role": "user", "content": "readable-prefix"},
    {"role": "assistant", "content": "readable-tail"},
]


def _identity(**overrides) -> NativeCompactionIdentity:
    values = {
        "provider": "openai",
        "api_mode": "codex_responses",
        "model": "gpt-5",
        "base_url": "https://api.openai.com/v1",
        "issuer_kind": "other:https://api.openai.com/v1",
        "credential_scope": "",
        "replay_encrypted_reasoning": True,
    }
    values.update(overrides)
    return NativeCompactionIdentity(**values)


def _checkpoint(
    session_id: str, *, prefix=None, identity=None
) -> NativeCompactionCheckpoint:
    prefix = ORDINARY[:1] if prefix is None else prefix
    return NativeCompactionCheckpoint(
        session_id=session_id,
        identity=identity or _identity(),
        source_input_item_count=len(prefix),
        source_input_sha256=canonical_input_sha256(prefix),
        output=OPAQUE,
        compact_response_id="resp-task11",
        compact_created_at=1.0,
        input_item_count=len(prefix),
        output_item_count=len(OPAQUE),
        generation=1,
        created_at=1.0,
        updated_at=1.0,
    )


def _agent(checkpoint, *, session_id="session-a", **route):
    values = {
        "provider": "openai",
        "api_mode": "codex_responses",
        "model": "gpt-5",
        "base_url": "https://api.openai.com/v1",
    }
    values.update(route)
    return SimpleNamespace(
        **values,
        session_id=session_id,
        _native_openai_checkpoint=checkpoint,
        _native_openai_checkpoint_session_id=session_id,
        native_compaction_policy=SimpleNamespace(is_eligible=lambda **_kwargs: True),
        client=SimpleNamespace(),
        _codex_reasoning_replay_enabled=True,
        _base_url_hostname="api.openai.com",
        _base_url_lower=values["base_url"].lower(),
    )


def _project(agent, ordinary=None, *, model=None):
    ordinary = ORDINARY if ordinary is None else ordinary
    kwargs = {"model": model or agent.model, "input": ordinary}
    return maybe_apply_native_openai_projection(agent, kwargs)["input"]


def test_native_identity_uses_stable_credential_pool_entry_id():
    agent = _agent(None)
    agent._credential_pool_entry_id = "pool-entry-account-a"

    identity = native_openai_identity_for_agent(agent)

    assert identity.credential_scope.startswith("pool-entry-sha256:")
    assert "pool-entry-account-a" not in identity.credential_scope


def test_pool_entry_scope_preserves_exact_opaque_id_bytes():
    agent = _agent(None)
    agent._credential_pool_entry_id = "AccountA"
    unpadded = native_openai_identity_for_agent(agent).credential_scope
    agent._credential_pool_entry_id = "accounta"
    lower = native_openai_identity_for_agent(agent).credential_scope
    agent._credential_pool_entry_id = " AccountA "
    padded = native_openai_identity_for_agent(agent).credential_scope
    agent._credential_pool_entry_id = "   "
    whitespace_only = native_openai_identity_for_agent(agent).credential_scope

    assert len({unpadded, lower, padded, whitespace_only}) == 4
    assert all(
        scope.startswith("pool-entry-sha256:")
        for scope in (unpadded, lower, padded, whitespace_only)
    )
    assert all(
        raw not in scope
        for raw in ("AccountA", "accounta")
        for scope in (unpadded, lower, padded, whitespace_only)
    )


def test_direct_credential_scope_is_stable_across_agents_without_persisting_key_hash(
    monkeypatch, tmp_path
):
    import hashlib

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    first_agent = _agent(None)
    second_agent = _agent(None)
    changed_agent = _agent(None)
    first_key = "sk-test-direct-credential-alpha"
    first_agent.api_key = first_key
    second_agent.api_key = first_key
    changed_agent.api_key = "sk-test-direct-credential-beta"

    first = native_openai_identity_for_agent(first_agent).credential_scope
    repeated = native_openai_identity_for_agent(first_agent).credential_scope
    recreated = native_openai_identity_for_agent(second_agent).credential_scope
    changed = native_openai_identity_for_agent(changed_agent).credential_scope

    assert first
    assert first.startswith("direct-hmac-sha256:")
    assert first == repeated == recreated
    assert changed != first
    assert first_key not in first
    assert hashlib.sha256(first_key.encode()).hexdigest() not in first
    scope_key = tmp_path / "cache" / "native_openai_scope.key"
    assert scope_key.stat().st_size == 32
    assert first_key.encode() not in scope_key.read_bytes()


def test_direct_credential_scope_never_uses_symlinked_installation_key(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    target = cache_dir / "replay-secret.bin"
    target.write_bytes(b"x" * 32)
    scope_key = cache_dir / "native_openai_scope.key"
    scope_key.symlink_to(target)

    first_agent = _agent(None)
    first_agent.api_key = "sk-test-direct-symlink"
    second_agent = _agent(None)
    second_agent.api_key = "sk-test-direct-symlink"

    first = native_openai_identity_for_agent(first_agent).credential_scope
    second = native_openai_identity_for_agent(second_agent).credential_scope

    assert first.startswith("direct-instance:")
    assert second.startswith("direct-instance:")
    assert first != second
    assert target.read_bytes() == b"x" * 32


def test_direct_scope_key_read_rejects_post_open_path_identity_change(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    key_path = tmp_path / "cache" / "native_openai_scope.key"
    replacement = tmp_path / "cache" / "replacement-key.bin"
    key_path.parent.mkdir(parents=True)
    key_path.write_bytes(b"t" * 32)
    replacement.write_bytes(b"a" * 32)

    original_open = os.open
    original_lstat = Path.lstat
    opened_fds = []
    identity_changed = False

    def record_open(path, flags, *args):
        fd = original_open(path, flags, *args)
        if Path(path) == key_path:
            opened_fds.append(fd)
        return fd

    def report_replacement_identity(path):
        nonlocal identity_changed
        if path == key_path:
            identity_changed = True
            return original_lstat(replacement)
        return original_lstat(path)

    monkeypatch.setattr(chat_completion_helpers.os, "open", record_open)
    monkeypatch.setattr(Path, "lstat", report_replacement_identity)

    loaded = chat_completion_helpers._load_or_create_native_scope_key()

    assert identity_changed is True
    assert loaded is None
    assert len(opened_fds) == 1
    with pytest.raises(OSError):
        os.fstat(opened_fds[0])


def test_native_identity_ignores_non_string_credential_pool_entry_id():
    agent = _agent(None)
    agent._credential_pool_entry_id = object()

    identity = native_openai_identity_for_agent(agent)

    assert identity.credential_scope == ""


@pytest.mark.parametrize(
    ("in_place", "prompt_update_fails"),
    [(True, False), (False, False), (True, True)],
    ids=("in-place", "rotation", "in-place-prompt-update-failure"),
)
def test_textual_compaction_deletes_old_session_checkpoint_and_cache(
    tmp_path, monkeypatch, in_place, prompt_update_fails
):
    from run_agent import AIAgent

    db = SessionDB(db_path=tmp_path / "sessions.db")
    db.create_session("session-a", source="cli")
    checkpoint = _checkpoint("session-a")
    db.upsert_native_openai_checkpoint(checkpoint)
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=db,
            session_id="session-a",
            skip_context_files=True,
            skip_memory=True,
        )
    agent.compression_in_place = in_place
    agent._native_openai_checkpoint = checkpoint
    agent._native_openai_checkpoint_session_id = "session-a"
    compressor = MagicMock()
    compressor.compress.return_value = [
        {"role": "user", "content": "readable summary"},
        {"role": "assistant", "content": "readable tail"},
    ]
    compressor.compression_count = 1
    compressor.last_prompt_tokens = 0
    compressor.last_completion_tokens = 0
    compressor._last_summary_error = None
    compressor._last_compress_aborted = False
    agent.context_compressor = compressor
    if prompt_update_fails:
        monkeypatch.setattr(
            db,
            "update_system_prompt",
            MagicMock(side_effect=RuntimeError("post-rewrite failure")),
        )

    old_session_id = agent.session_id
    agent._compress_context(
        [{"role": "user", "content": f"message-{index}"} for index in range(10)],
        "sys",
        approx_tokens=10_000,
    )

    assert (agent.session_id == old_session_id) is in_place
    assert db.load_native_openai_checkpoint(old_session_id) is None
    assert db.load_native_openai_checkpoint(agent.session_id) is None
    assert agent._native_openai_checkpoint is None


def test_session_rotation_does_not_copy_checkpoint_to_child(tmp_path):
    db = SessionDB(db_path=tmp_path / "sessions.db")
    db.create_session("parent", source="cli")
    db.create_session("child", source="cli", parent_session_id="parent")
    db.upsert_native_openai_checkpoint(_checkpoint("parent"))
    agent = _agent(None, session_id="parent")
    agent._session_db = db

    bind_native_openai_checkpoint_cache(agent, "child")

    assert db.load_native_openai_checkpoint("parent") is not None
    assert db.load_native_openai_checkpoint("child") is None
    assert agent._native_openai_checkpoint is None


def test_rewind_prefix_mismatch_uses_full_readable_history():
    checkpoint = _checkpoint("session-a")
    rewound = [{"role": "user", "content": "edited-prefix"}, *ORDINARY[1:]]
    deleted = []
    agent = _agent(checkpoint)
    agent._session_db = SimpleNamespace(
        delete_native_openai_checkpoint=lambda session_id: (
            deleted.append(session_id) or True
        )
    )

    assert _project(agent, rewound) == rewound
    assert agent._native_openai_checkpoint is None
    assert deleted == ["session-a"]


@pytest.mark.parametrize(
    "route",
    [
        {"model": "gpt-5-mini"},
        {"provider": "openai-codex"},
        {"api_mode": "chat_completions"},
        {"base_url": "https://chatgpt.com/backend-api/codex"},
    ],
)
def test_route_identity_mismatch_uses_full_readable_transcript(route):
    checkpoint = _checkpoint("session-a")
    agent = _agent(checkpoint, **route)

    assert _project(agent) == ORDINARY


def test_provider_fallback_mid_turn_rebuilds_without_opaque_output():
    checkpoint = _checkpoint("session-a")
    agent = _agent(checkpoint)
    assert _project(agent) == OPAQUE + ORDINARY[1:]

    agent.provider = "xai"
    agent.model = "grok-4"
    agent.base_url = "https://api.x.ai/v1"
    rebuilt = _project(agent)

    assert rebuilt == ORDINARY
    assert all(item not in rebuilt for item in OPAQUE)


def test_switching_back_can_reuse_checkpoint_only_if_prefix_still_matches():
    checkpoint = _checkpoint("session-a")
    agent = _agent(checkpoint, model="gpt-5-mini")
    assert _project(agent) == ORDINARY

    agent.model = "gpt-5"
    assert _project(agent) == OPAQUE + ORDINARY[1:]

    edited = [{"role": "user", "content": "changed"}, *ORDINARY[1:]]
    assert _project(agent, edited) == edited


def test_new_session_has_no_checkpoint(tmp_path):
    db = SessionDB(db_path=tmp_path / "sessions.db")
    db.create_session("new-session", source="cli")

    assert db.load_native_openai_checkpoint("new-session") is None


def test_corrupt_checkpoint_never_blocks_request(tmp_path):
    db = SessionDB(db_path=tmp_path / "sessions.db")
    db.create_session("session-a", source="cli")
    db.upsert_native_openai_checkpoint(_checkpoint("session-a"))
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE native_openai_compaction_checkpoints SET output_json = ? WHERE session_id = ?",
            ("{corrupt", "session-a"),
        )
    )
    agent = _agent(None)
    agent._session_db = db

    bind_native_openai_checkpoint_cache(agent, "session-a")

    assert agent._native_openai_checkpoint is None
    assert _project(agent) == ORDINARY
