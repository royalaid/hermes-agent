"""RPC history must retain visible Responses-API replies without raw sidecars (#68321)."""

from __future__ import annotations

import json
import threading

import pytest

from hermes_state import SessionDB
import tui_gateway.server as server


@pytest.mark.parametrize("content", ["", "Canonical reply"])
@pytest.mark.parametrize("phase", ["final", "final_answer"])
@pytest.mark.parametrize("with_details", [False, True])
def test_session_history_preserves_codex_reply_without_raw_sidecars(tmp_path, content, phase, with_details):
    message_items = [
        {
            "type": "message",
            "role": "assistant",
            "phase": "analysis",
            "content": [{"type": "output_text", "text": "ANALYSIS_SENTINEL"}],
        },
        {
            "type": "message",
            "role": "assistant",
            "phase": phase,
            "encrypted_content": "ENCRYPTED_SENTINEL",
            "content": [{"type": "output_text", "text": "Persisted answer"}],
        }
    ]
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("stored-session", source="desktop")
    db.append_message(
        "stored-session",
        "assistant",
        content,
        codex_message_items=message_items,
        reasoning="Thinking before the tool" if with_details else None,
        tool_calls=[{"id": "call-1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}] if with_details else None,
    )
    previous_db = server._db
    setattr(server, "_db", db)
    server._sessions["runtime-session"] = {
        "session_key": "stored-session",
        "history": [],
        "history_lock": threading.Lock(),
        "running": False,
        "agent": None,
    }

    try:
        response = server.handle_request(
            {"id": "1", "method": "session.history", "params": {"session_id": "runtime-session"}}
        )
    finally:
        server._sessions.pop("runtime-session", None)
        setattr(server, "_db", previous_db)
        db.close()

    assert isinstance(response, dict)
    assert "error" not in response
    assert response["result"]["count"] == 1
    messages = response["result"]["messages"]
    assert len(messages) == 1
    assert messages[0]["role"] == "assistant"
    assert messages[0]["text"] == (content or "Persisted answer")
    if with_details:
        assert messages[0]["reasoning"] == "Thinking before the tool"
    serialized = json.dumps(response)
    for forbidden in ("codex_message_items", "codex_reasoning_items", "ANALYSIS_SENTINEL", "ENCRYPTED_SENTINEL"):
        assert forbidden not in serialized


@pytest.mark.parametrize("overrides", [
    {"type": "reasoning"},
    {"role": "user"},
    {"phase": "analysis"},
    {"phase": "commentary"},
    {"phase": None},
    {"phase": ["final"]},
    {"content": "PRIVATE_SENTINEL"},
    {"content": [{"type": "reasoning_text", "text": "PRIVATE_SENTINEL"}]},
    {"content": [{"type": "output_text", "text": {"value": "PRIVATE_SENTINEL"}}]},
    {"content": [{"type": "output_text", "text": "Persisted answer"}, {"type": "image", "url": "PRIVATE_SENTINEL"}]},
])
def test_session_history_does_not_promote_nonfinal_or_malformed_codex_reply(overrides):
    item = {
        "type": "message", "role": "assistant", "phase": "final_answer",
        "content": [{"type": "output_text", "text": "PRIVATE_SENTINEL"}],
        **overrides,
    }
    assert server._history_to_messages([
        {"role": "assistant", "content": "", "codex_message_items": [item]},
    ]) == []


@pytest.mark.parametrize("sidecar", [None, "{invalid", "{}", {}, [None]])
def test_session_history_ignores_invalid_codex_reply_sidecars(sidecar):
    assert server._history_to_messages([
        {"role": "assistant", "content": "", "codex_message_items": sidecar},
    ]) == []


@pytest.mark.parametrize("metadata", [{"role": "user"}, {"display_kind": "hidden"}])
def test_codex_reply_projection_respects_message_visibility(metadata):
    from agent.codex_display_projection import project_codex_reply_text

    assert project_codex_reply_text({
        "role": "assistant", "content": "", **metadata,
        "codex_message_items": [{
            "type": "message", "role": "assistant", "phase": "final_answer",
            "content": [{"type": "output_text", "text": "HIDDEN_SENTINEL"}],
        }],
    }) == ""
