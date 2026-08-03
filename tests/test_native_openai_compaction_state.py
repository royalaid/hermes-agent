"""Behavior contracts for native OpenAI compaction SQLite state."""

from __future__ import annotations

import dataclasses
import logging

import pytest

from agent.native_openai_compaction import (
    NativeCompactionCheckpoint,
    NativeCompactionIdentity,
    canonical_input_sha256,
)
from hermes_state import SessionDB


OPAQUE_PAYLOAD_SENTINEL = "opaque-payload-sentinel-8f3a"
API_KEY_SENTINEL = "sk-api-key-sentinel-7d2b"
MIXED_OUTPUT = [
    {
        "type": "compaction",
        "encrypted_content": OPAQUE_PAYLOAD_SENTINEL,
        "unknown_nested": {"future": [3, 1, {"enabled": True}]},
    },
    {
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "output_text", "text": "preserve me"},
            {"future_part": {"ordered": ["first", "second"]}},
        ],
    },
    {"type": "future_item", "unknown": None},
]


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    yield session_db
    session_db.close()


def _identity(**overrides) -> NativeCompactionIdentity:
    values = {
        "provider": "openai",
        "api_mode": "codex_responses",
        "model": "gpt-5",
        "base_url": "https://api.openai.com/v1",
        "issuer_kind": "api_key",
        "credential_scope": "account-a",
        "replay_encrypted_reasoning": True,
    }
    values.update(overrides)
    return NativeCompactionIdentity(**values)


def _checkpoint(session_id: str = "session-1", **overrides) -> NativeCompactionCheckpoint:
    source = [{"role": "user", "content": "source input"}]
    output = overrides.pop("output", MIXED_OUTPUT)
    values = {
        "session_id": session_id,
        "identity": _identity(),
        "source_input_item_count": len(source),
        "source_input_sha256": canonical_input_sha256(source),
        "output": output,
        "compact_response_id": "resp_1",
        "compact_created_at": 10.5,
        "input_item_count": len(source),
        "output_item_count": len(output),
        "generation": 1,
        "created_at": 11.0,
        "updated_at": 12.0,
    }
    values.update(overrides)
    return NativeCompactionCheckpoint(**values)


def test_checkpoint_round_trip_preserves_opaque_semantics_and_immutability(db):
    db.create_session("session-1", source="cli")
    original = _checkpoint()

    db.upsert_native_openai_checkpoint(original)
    loaded = db.load_native_openai_checkpoint("session-1")

    assert loaded == original
    assert loaded is not original
    assert loaded.output == MIXED_OUTPUT
    mutated_output = loaded.output
    mutated_output[0]["unknown_nested"]["future"][2]["enabled"] = False
    assert loaded.output == MIXED_OUTPUT
    with pytest.raises(dataclasses.FrozenInstanceError):
        loaded.generation = 2


def test_upsert_replaces_the_complete_checkpoint_in_one_session_row(db):
    db.create_session("session-1", source="cli")
    first = _checkpoint()
    replacement_output = [{"type": "compaction", "new": ["z", "a"]}]
    replacement = _checkpoint(
        identity=_identity(
            provider="azure",
            api_mode="responses",
            model="gpt-5-mini",
            base_url="https://example.invalid/v2",
            issuer_kind="oauth",
            credential_scope="tenant-b",
            replay_encrypted_reasoning=False,
        ),
        source_input_item_count=7,
        source_input_sha256="b" * 64,
        output=replacement_output,
        compact_response_id=None,
        compact_created_at=None,
        input_item_count=9,
        output_item_count=1,
        generation=2,
        created_at=20.0,
        updated_at=21.0,
    )

    db.upsert_native_openai_checkpoint(first)
    db.upsert_native_openai_checkpoint(replacement)

    with db._read_ctx() as conn:
        row_count = conn.execute(
            "SELECT COUNT(*) FROM native_openai_compaction WHERE session_id = ?",
            ("session-1",),
        ).fetchone()[0]
    assert row_count == 1
    assert db.load_native_openai_checkpoint("session-1") == replacement


def test_checkpoint_lifecycle_does_not_change_visible_transcript(db):
    db.create_session("session-1", source="cli")
    db.append_message("session-1", role="user", content="visible user")
    db.append_message("session-1", role="assistant", content="visible assistant")
    before = db.get_messages("session-1")

    db.upsert_native_openai_checkpoint(_checkpoint())
    assert db.load_native_openai_checkpoint("session-1") is not None
    assert db.delete_native_openai_checkpoint("session-1") is True

    after = db.get_messages("session-1")
    assert [(msg["role"], msg["content"]) for msg in after] == [
        (msg["role"], msg["content"]) for msg in before
    ]
    assert len(after) == len(before) == 2


def test_corrupt_rows_fail_open_without_logging_or_repr_payloads(db, caplog):
    db.create_session("session-1", source="cli")
    db.upsert_native_openai_checkpoint(_checkpoint())
    caplog.set_level(logging.DEBUG)

    def _corrupt_output(conn):
        conn.execute(
            "UPDATE native_openai_compaction "
            "SET output_json = ?, credential_scope = ? WHERE session_id = ?",
            (f'not-json-{OPAQUE_PAYLOAD_SENTINEL}', API_KEY_SENTINEL, "session-1"),
        )

    db._execute_write(_corrupt_output)
    corrupt_json_result = db.load_native_openai_checkpoint("session-1")

    db.upsert_native_openai_checkpoint(_checkpoint())

    def _corrupt_scalar(conn):
        conn.execute(
            "UPDATE native_openai_compaction "
            "SET generation = ?, output_json = ?, credential_scope = ? "
            "WHERE session_id = ?",
            (
                "not-an-integer",
                f'[{ {"secret": OPAQUE_PAYLOAD_SENTINEL}!r}]'.replace("'", '"'),
                API_KEY_SENTINEL,
                "session-1",
            ),
        )

    db._execute_write(_corrupt_scalar)
    malformed_scalar_result = db.load_native_openai_checkpoint("session-1")

    observed = caplog.text + repr(corrupt_json_result) + repr(malformed_scalar_result)
    assert corrupt_json_result is None
    assert malformed_scalar_result is None
    assert OPAQUE_PAYLOAD_SENTINEL not in observed
    assert API_KEY_SENTINEL not in observed


def test_read_only_session_db_loads_committed_checkpoint(tmp_path):
    db_path = tmp_path / "state.db"
    writable = SessionDB(db_path=db_path)
    writable.create_session("session-1", source="cli")
    checkpoint = _checkpoint()
    writable.upsert_native_openai_checkpoint(checkpoint)
    writable.close()

    read_only = SessionDB(db_path=db_path, read_only=True)
    try:
        assert read_only.load_native_openai_checkpoint("session-1") == checkpoint
    finally:
        read_only.close()


def test_delete_method_and_session_delete_cascade(db):
    db.create_session("delete-explicit", source="cli")
    db.upsert_native_openai_checkpoint(_checkpoint("delete-explicit"))
    assert db.delete_native_openai_checkpoint("delete-explicit") is True
    assert db.delete_native_openai_checkpoint("delete-explicit") is False

    db.create_session("delete-cascade", source="cli")
    db.upsert_native_openai_checkpoint(_checkpoint("delete-cascade"))
    assert db.delete_session("delete-cascade") is True
    assert db.load_native_openai_checkpoint("delete-cascade") is None


def test_export_import_and_child_creation_do_not_copy_checkpoint(tmp_path):
    source = SessionDB(db_path=tmp_path / "source.db")
    destination = SessionDB(db_path=tmp_path / "destination.db")
    try:
        source.create_session("session-1", source="cli")
        source.append_message("session-1", role="user", content="portable transcript")
        source.upsert_native_openai_checkpoint(_checkpoint())

        exported = source.export_session("session-1")
        assert exported is not None
        assert "native_openai_compaction" not in exported
        assert destination.import_sessions([exported])["ok"] is True
        assert destination.load_native_openai_checkpoint("session-1") is None

        source.create_session(
            "session-child", source="cli", parent_session_id="session-1"
        )
        assert source.load_native_openai_checkpoint("session-child") is None
    finally:
        source.close()
        destination.close()


def test_native_compaction_table_has_scoped_schema_and_cascade(db):
    expected_columns = [
        "session_id",
        "provider",
        "api_mode",
        "model",
        "base_url",
        "issuer_kind",
        "credential_scope",
        "replay_encrypted_reasoning",
        "source_input_item_count",
        "source_input_sha256",
        "output_json",
        "compact_response_id",
        "compact_created_at",
        "input_item_count",
        "output_item_count",
        "generation",
        "created_at",
        "updated_at",
    ]
    with db._read_ctx() as conn:
        columns = [
            row["name"]
            for row in conn.execute(
                'PRAGMA table_info("native_openai_compaction")'
            ).fetchall()
        ]
        foreign_keys = conn.execute(
            'PRAGMA foreign_key_list("native_openai_compaction")'
        ).fetchall()

    assert columns == expected_columns
    assert len(foreign_keys) == 1
    assert foreign_keys[0]["table"] == "sessions"
    assert foreign_keys[0]["from"] == "session_id"
    assert foreign_keys[0]["to"] == "id"
    assert foreign_keys[0]["on_delete"] == "CASCADE"
