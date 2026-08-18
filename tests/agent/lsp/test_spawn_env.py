"""Subprocess-environment boundary tests for LSP server startup."""

from __future__ import annotations

import asyncio

import pytest

from agent.lsp.client import LSPClient, LSPProtocolError


@pytest.mark.asyncio
@pytest.mark.parametrize("host_authorized", [False, True])
async def test_lsp_spawn_consumes_buzz_authority_after_overlay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    host_authorized: bool,
) -> None:
    """The actual LSP spawn gets one final host-authoritative scrub."""
    for name in (
        "BUZZ_MANAGED_AGENT",
        "BUZZ_PRIVATE_KEY",
        "BUZZ_RELAY_URL",
        "BUZZ_AUTH_TAG",
        "GH_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)

    if host_authorized:
        monkeypatch.setenv("BUZZ_MANAGED_AGENT", "xyz.block.buzz.app")
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "host-private-key")
        monkeypatch.setenv("BUZZ_RELAY_URL", "wss://host.example")
        monkeypatch.setenv("BUZZ_AUTH_TAG", "host-auth-tag")

    captured: dict[str, object] = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["env"] = kwargs["env"]
        raise FileNotFoundError("stop after environment capture")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    client = LSPClient(
        server_id="env-boundary",
        workspace_root=str(tmp_path),
        command=["synthetic-lsp"],
        env={
            "BUZZ_MANAGED_AGENT": "xyz.block.buzz.app",
            "BUZZ_PRIVATE_KEY": "overlay-private-key",
            "BUZZ_RELAY_URL": "wss://overlay.example",
            "BUZZ_AUTH_TAG": "overlay-auth-tag",
            "GH_TOKEN": "overlay-github-token",
            "REQUIRED_LSP_ENV": "preserved",
        },
    )

    with pytest.raises(LSPProtocolError, match="binary not found"):
        await client._spawn()

    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert "BUZZ_MANAGED_AGENT" not in child_env
    assert "GH_TOKEN" not in child_env
    assert child_env["REQUIRED_LSP_ENV"] == "preserved"
    if host_authorized:
        assert child_env["BUZZ_PRIVATE_KEY"] == "overlay-private-key"
        assert child_env["BUZZ_RELAY_URL"] == "wss://overlay.example"
        assert child_env["BUZZ_AUTH_TAG"] == "overlay-auth-tag"
    else:
        assert "BUZZ_PRIVATE_KEY" not in child_env
        assert "BUZZ_RELAY_URL" not in child_env
        assert "BUZZ_AUTH_TAG" not in child_env
