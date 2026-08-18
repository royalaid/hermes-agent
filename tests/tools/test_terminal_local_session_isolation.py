"""Regression coverage for terminal environment cache-key isolation."""

import json

import pytest

import tools.terminal_tool as terminal_tool


class _FakeEnvironment:
    def __init__(self, cwd):
        self.cwd = cwd

    def execute(self, _command, **_kwargs):
        return {"output": "ok", "returncode": 0}


def _exercise_two_sessions(monkeypatch, env_type):
    created_task_ids = []
    config = {
        "env_type": env_type,
        "cwd": ".",
        "timeout": 30,
        "local_persistent": False,
        "docker_image": "docker-image",
        "singularity_image": "singularity-image",
        "modal_image": "modal-image",
        "daytona_image": "daytona-image",
    }

    def fake_create_environment(*, cwd, task_id, **_kwargs):
        created_task_ids.append(task_id)
        return _FakeEnvironment(cwd)

    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: config)
    monkeypatch.setattr(terminal_tool, "_resolve_task_host_cwd", lambda *_args: None)
    monkeypatch.setattr(terminal_tool, "_container_config_from_config", lambda _config: {})
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_tool, "_create_environment", fake_create_environment)
    monkeypatch.setattr(terminal_tool, "_active_environments", {})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_creation_locks", {})

    for task_id in ("session-a", "session-b"):
        result = json.loads(
            terminal_tool.terminal_tool("pwd", task_id=task_id, force=True)
        )
        assert result["exit_code"] == 0

    return created_task_ids, terminal_tool._active_environments


def test_local_backend_keeps_each_session_task_id(monkeypatch):
    created_task_ids, active_environments = _exercise_two_sessions(
        monkeypatch, "local"
    )

    assert created_task_ids == ["session-a", "session-b"]
    assert set(active_environments) == {"session-a", "session-b"}
    assert active_environments["session-a"] is not active_environments["session-b"]


def test_get_active_env_prefers_exact_session_over_collapsed_default(monkeypatch):
    default_env = _FakeEnvironment("default-cwd")
    session_env = _FakeEnvironment("session-a-cwd")
    monkeypatch.setattr(
        terminal_tool,
        "_active_environments",
        {"default": default_env, "session-a": session_env},
    )
    monkeypatch.setattr(
        terminal_tool, "_resolve_container_task_id", lambda _task_id: "default"
    )

    assert terminal_tool.get_active_env("session-a") is session_env


def test_get_active_env_falls_back_to_collapsed_default(monkeypatch):
    default_env = _FakeEnvironment("default-cwd")
    monkeypatch.setattr(
        terminal_tool, "_active_environments", {"default": default_env}
    )
    monkeypatch.setattr(
        terminal_tool, "_resolve_container_task_id", lambda _task_id: "default"
    )

    assert terminal_tool.get_active_env("session-a") is default_env


@pytest.mark.parametrize(
    "env_type",
    ["docker", "modal", "singularity", "daytona", "vercel_sandbox"],
)
def test_container_backends_keep_collapsed_default_sharing(monkeypatch, env_type):
    created_task_ids, active_environments = _exercise_two_sessions(
        monkeypatch, env_type
    )

    assert created_task_ids == ["default"]
    assert set(active_environments) == {"default"}
