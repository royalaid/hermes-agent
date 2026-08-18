"""Regression coverage for terminal environment cache-key isolation."""

import json

import pytest

import tools.code_execution_tool as code_execution_tool
import tools.file_tools as file_tools
import tools.terminal_tool as terminal_tool


class _FakeEnvironment:
    def __init__(self, cwd):
        self.cwd = cwd
        self.cleanup_count = 0
        self.execute_count = 0

    def execute(self, _command, **_kwargs):
        self.execute_count += 1
        return {"output": "ok", "returncode": 0}

    def cleanup(self):
        self.cleanup_count += 1


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


def _configure_direct_creation_paths(monkeypatch, env_type):
    created_task_ids = []
    config = {
        "env_type": env_type,
        "cwd": ".",
        "timeout": 30,
        "local_persistent": False,
        "container_persistent": True,
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
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_tool, "_create_environment", fake_create_environment)
    monkeypatch.setattr(terminal_tool, "_active_environments", {})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_creation_locks", {})
    monkeypatch.setattr(file_tools, "_file_ops_cache", {})
    return created_task_ids


def _configure_override_creation_path(monkeypatch, tmp_path):
    created_cwds = []
    config_cwd = tmp_path / "session-fallback"
    leaked_default_cwd = tmp_path / "default-workspace"
    config_cwd.mkdir()
    leaked_default_cwd.mkdir()
    config = {
        "env_type": "local",
        "cwd": str(config_cwd),
        "timeout": 30,
        "local_persistent": False,
        "container_persistent": True,
        "docker_image": "docker-image",
        "singularity_image": "singularity-image",
        "modal_image": "modal-image",
        "daytona_image": "daytona-image",
    }

    def fake_create_environment(*, cwd, **_kwargs):
        created_cwds.append(cwd)
        return _FakeEnvironment(cwd)

    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: config)
    monkeypatch.setattr(terminal_tool, "_resolve_task_host_cwd", lambda *_args: None)
    monkeypatch.setattr(terminal_tool, "_container_config_from_config", lambda _config: {})
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_tool, "_create_environment", fake_create_environment)
    monkeypatch.setattr(terminal_tool, "_active_environments", {})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_creation_locks", {})
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    monkeypatch.setattr(
        terminal_tool,
        "_task_env_overrides",
        {"default": {"cwd": str(leaked_default_cwd)}},
    )
    monkeypatch.setattr(file_tools, "_file_ops_cache", {})
    return created_cwds, str(config_cwd)


def _configure_code_execution_override_path(monkeypatch, env_type, overrides):
    created = []
    config = {
        "env_type": env_type,
        "cwd": "config-cwd",
        "timeout": 30,
        "local_persistent": False,
        "container_persistent": True,
        "docker_image": "config-image",
        "singularity_image": "singularity-image",
        "modal_image": "modal-image",
        "daytona_image": "daytona-image",
    }

    def fake_create_environment(**kwargs):
        created.append(kwargs)
        return _FakeEnvironment(kwargs["cwd"])

    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: config)
    monkeypatch.setattr(terminal_tool, "_resolve_task_host_cwd", lambda *_args: None)
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_tool, "_create_environment", fake_create_environment)
    monkeypatch.setattr(terminal_tool, "_active_environments", {})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_creation_locks", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", overrides)
    return created


def test_local_backend_keeps_each_session_task_id(monkeypatch):
    created_task_ids, active_environments = _exercise_two_sessions(
        monkeypatch, "local"
    )

    assert created_task_ids == ["session-a", "session-b"]
    assert set(active_environments) == {"session-a", "session-b"}
    assert active_environments["session-a"] is not active_environments["session-b"]


def test_resolve_task_overrides_local_backend_does_not_fall_back_to_default(
    monkeypatch,
):
    monkeypatch.setattr(
        terminal_tool, "_task_env_overrides", {"default": {"cwd": "default-cwd"}}
    )
    monkeypatch.setattr(
        terminal_tool, "_get_env_config", lambda: {"env_type": "local"}
    )

    assert terminal_tool.resolve_task_overrides("session-a") == {}


def test_resolve_task_overrides_local_backend_uses_exact_session_override(
    monkeypatch,
):
    exact = {"cwd": "session-a-cwd"}
    monkeypatch.setattr(
        terminal_tool,
        "_task_env_overrides",
        {"default": {"cwd": "default-cwd"}, "session-a": exact},
    )
    monkeypatch.setattr(
        terminal_tool, "_get_env_config", lambda: {"env_type": "local"}
    )

    assert terminal_tool.resolve_task_overrides("session-a") is exact


@pytest.mark.parametrize("resolved_key", ["default", "parent-session"])
def test_resolve_task_overrides_container_backend_keeps_resolved_fallback(
    monkeypatch, resolved_key
):
    fallback = {"docker_image": "shared-image"}
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {resolved_key: fallback})
    monkeypatch.setattr(
        terminal_tool, "_get_env_config", lambda: {"env_type": "docker"}
    )
    monkeypatch.setattr(
        terminal_tool, "_resolve_container_task_id", lambda _task_id: resolved_key
    )

    assert terminal_tool.resolve_task_overrides("child-session") is fallback


@pytest.mark.parametrize("caller", ["terminal", "file_tools"])
def test_local_creation_does_not_inherit_default_cwd_override(
    monkeypatch, tmp_path, caller
):
    created_cwds, config_cwd = _configure_override_creation_path(monkeypatch, tmp_path)

    if caller == "terminal":
        result = json.loads(
            terminal_tool.terminal_tool("pwd", task_id="session-a", force=True)
        )
        assert result["exit_code"] == 0
    else:
        file_tools._get_file_ops("session-a")

    assert created_cwds == [config_cwd]


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
    monkeypatch.setattr(
        terminal_tool, "_get_env_config", lambda: {"env_type": "local"}
    )

    assert terminal_tool.get_active_env("session-a") is session_env


def test_get_active_env_local_backend_does_not_fall_back_to_collapsed_default(
    monkeypatch,
):
    default_env = _FakeEnvironment("default-cwd")
    monkeypatch.setattr(
        terminal_tool, "_active_environments", {"default": default_env}
    )
    monkeypatch.setattr(
        terminal_tool, "_resolve_container_task_id", lambda _task_id: "default"
    )
    monkeypatch.setattr(
        terminal_tool, "_get_env_config", lambda: {"env_type": "local"}
    )

    assert terminal_tool.get_active_env("session-a") is None


def test_get_active_env_container_backend_keeps_collapsed_default_fallback(monkeypatch):
    default_env = _FakeEnvironment("default-cwd")
    monkeypatch.setattr(
        terminal_tool, "_active_environments", {"default": default_env}
    )
    monkeypatch.setattr(
        terminal_tool, "_resolve_container_task_id", lambda _task_id: "default"
    )
    monkeypatch.setattr(
        terminal_tool, "_get_env_config", lambda: {"env_type": "docker"}
    )

    assert terminal_tool.get_active_env("session-a") is default_env


def test_get_active_env_container_backend_prefers_raw_over_collapsed_default(
    monkeypatch,
):
    raw_env = _FakeEnvironment("raw-cwd")
    default_env = _FakeEnvironment("default-cwd")
    monkeypatch.setattr(
        terminal_tool,
        "_active_environments",
        {"session-a": raw_env, "default": default_env},
    )
    monkeypatch.setattr(
        terminal_tool, "_resolve_container_task_id", lambda _task_id: "default"
    )
    monkeypatch.setattr(
        terminal_tool, "_get_env_config", lambda: {"env_type": "docker"}
    )

    assert terminal_tool.get_active_env("session-a") is raw_env


@pytest.mark.parametrize("raw_present", [True, False])
def test_terminal_execute_container_selects_raw_then_collapsed_environment(
    monkeypatch, raw_present
):
    raw_env = _FakeEnvironment("raw-cwd")
    default_env = _FakeEnvironment("default-cwd")
    active = {"default": default_env}
    if raw_present:
        active["session-a"] = raw_env
    config = {
        "env_type": "docker",
        "cwd": "/workspace",
        "timeout": 30,
        "docker_image": "docker-image",
        "singularity_image": "singularity-image",
        "modal_image": "modal-image",
        "daytona_image": "daytona-image",
    }
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: config)
    monkeypatch.setattr(
        terminal_tool, "_resolve_container_task_id", lambda _task_id: "default"
    )
    monkeypatch.setattr(terminal_tool, "_resolve_task_host_cwd", lambda *_args: None)
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_tool, "_active_environments", active)
    monkeypatch.setattr(terminal_tool, "_last_activity", {})

    result = json.loads(
        terminal_tool.terminal_tool("pwd", task_id="session-a", force=True)
    )

    expected_key = "session-a" if raw_present else "default"
    assert result["exit_code"] == 0
    assert raw_env.execute_count == int(raw_present)
    assert default_env.execute_count == int(not raw_present)
    assert set(terminal_tool._last_activity) == {expected_key}


def test_degraded_local_eviction_only_removes_exact_session(monkeypatch):
    default_env = _FakeEnvironment("default-cwd")
    session_env = _FakeEnvironment("session-a-cwd")
    sibling_env = _FakeEnvironment("session-b-cwd")
    monkeypatch.setattr(
        terminal_tool,
        "_active_environments",
        {
            "default": default_env,
            "session-a": session_env,
            "session-b": sibling_env,
        },
    )
    monkeypatch.setattr(
        terminal_tool,
        "_last_activity",
        {"default": 1.0, "session-a": 2.0, "session-b": 3.0},
    )
    monkeypatch.setattr(
        terminal_tool, "_get_env_config", lambda: {"env_type": "local"}
    )

    terminal_tool._evict_environment_for_task("session-a")

    assert terminal_tool._active_environments == {
        "default": default_env,
        "session-b": sibling_env,
    }
    assert terminal_tool._last_activity == {"default": 1.0, "session-b": 3.0}
    assert session_env.cleanup_count == 1
    assert default_env.cleanup_count == 0
    assert sibling_env.cleanup_count == 0


def test_degraded_container_eviction_removes_resolved_shared_default(monkeypatch):
    default_env = _FakeEnvironment("default-cwd")
    monkeypatch.setattr(
        terminal_tool, "_active_environments", {"default": default_env}
    )
    monkeypatch.setattr(terminal_tool, "_last_activity", {"default": 1.0})
    monkeypatch.setattr(
        terminal_tool, "_get_env_config", lambda: {"env_type": "docker"}
    )
    monkeypatch.setattr(
        terminal_tool, "_resolve_container_task_id", lambda _task_id: "default"
    )

    terminal_tool._evict_environment_for_task("session-a")

    assert terminal_tool._active_environments == {}
    assert terminal_tool._last_activity == {}
    assert default_env.cleanup_count == 1


def test_degraded_container_eviction_deduplicates_aliased_environment_cleanup(
    monkeypatch,
):
    shared_env = _FakeEnvironment("shared-cwd")
    monkeypatch.setattr(
        terminal_tool,
        "_active_environments",
        {"default": shared_env, "session-a": shared_env},
    )
    monkeypatch.setattr(
        terminal_tool, "_last_activity", {"default": 1.0, "session-a": 2.0}
    )
    monkeypatch.setattr(
        terminal_tool, "_get_env_config", lambda: {"env_type": "docker"}
    )
    monkeypatch.setattr(
        terminal_tool, "_resolve_container_task_id", lambda _task_id: "default"
    )

    terminal_tool._evict_environment_for_task("session-a")

    assert terminal_tool._active_environments == {}
    assert terminal_tool._last_activity == {}
    assert shared_env.cleanup_count == 1


def test_cwd_override_local_backend_does_not_mutate_default_environment(monkeypatch):
    default_env = _FakeEnvironment("default-cwd")
    monkeypatch.setattr(
        terminal_tool, "_active_environments", {"default": default_env}
    )
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    monkeypatch.setattr(
        terminal_tool, "_resolve_container_task_id", lambda _task_id: "default"
    )
    monkeypatch.setattr(
        terminal_tool, "_get_env_config", lambda: {"env_type": "local"}
    )

    terminal_tool.register_task_env_overrides("session-a", {"cwd": "session-a-cwd"})

    assert default_env.cwd == "default-cwd"


def test_cwd_override_container_backend_updates_collapsed_default_environment(
    monkeypatch,
):
    default_env = _FakeEnvironment("default-cwd")
    monkeypatch.setattr(
        terminal_tool, "_active_environments", {"default": default_env}
    )
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    monkeypatch.setattr(
        terminal_tool, "_resolve_container_task_id", lambda _task_id: "default"
    )
    monkeypatch.setattr(
        terminal_tool, "_get_env_config", lambda: {"env_type": "docker"}
    )

    terminal_tool.register_task_env_overrides("session-a", {"cwd": "session-a-cwd"})

    assert default_env.cwd == "session-a-cwd"


def test_code_execution_local_backend_keeps_session_environment_keys(monkeypatch):
    created_task_ids = _configure_direct_creation_paths(monkeypatch, "local")

    env_a, _ = code_execution_tool._get_or_create_env("session-a")
    env_b, _ = code_execution_tool._get_or_create_env("session-b")

    assert env_a is not env_b
    assert created_task_ids == ["session-a", "session-b"]
    assert set(terminal_tool._active_environments) == {"session-a", "session-b"}
    assert set(terminal_tool._creation_locks) == {"session-a", "session-b"}


def test_code_execution_container_prefers_raw_session_overrides(monkeypatch):
    created = _configure_code_execution_override_path(
        monkeypatch,
        "docker",
        {
            "session-a": {"cwd": "/workspace/session-a", "docker_image": "raw-image"},
            "default": {"cwd": "/workspace/default", "docker_image": "default-image"},
        },
    )
    monkeypatch.setattr(
        terminal_tool, "_resolve_container_task_id", lambda _task_id: "default"
    )

    code_execution_tool._get_or_create_env("session-a")

    assert created[0]["task_id"] == "default"
    assert created[0]["cwd"] == "/workspace/session-a"
    assert created[0]["image"] == "raw-image"


def test_code_execution_local_absent_exact_override_does_not_inherit_default(
    monkeypatch,
):
    created = _configure_code_execution_override_path(
        monkeypatch,
        "local",
        {"default": {"cwd": "default-cwd"}},
    )

    code_execution_tool._get_or_create_env("session-a")

    assert created[0]["task_id"] == "session-a"
    assert created[0]["cwd"] == "config-cwd"


@pytest.mark.parametrize("raw_present", [True, False])
def test_code_execution_container_selects_raw_then_collapsed_environment(
    monkeypatch, raw_present
):
    raw_env = _FakeEnvironment("raw-cwd")
    default_env = _FakeEnvironment("default-cwd")
    active = {"default": default_env}
    if raw_present:
        active["session-a"] = raw_env
    config = {
        "env_type": "docker",
        "cwd": "/workspace",
        "timeout": 30,
        "container_persistent": True,
        "docker_image": "docker-image",
        "singularity_image": "singularity-image",
        "modal_image": "modal-image",
        "daytona_image": "daytona-image",
    }
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: config)
    monkeypatch.setattr(
        terminal_tool, "_resolve_container_task_id", lambda _task_id: "default"
    )
    monkeypatch.setattr(terminal_tool, "_active_environments", active)
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_creation_locks", {})

    selected, env_type = code_execution_tool._get_or_create_env("session-a")

    expected_key = "session-a" if raw_present else "default"
    assert selected is (raw_env if raw_present else default_env)
    assert env_type == "docker"
    assert set(terminal_tool._last_activity) == {expected_key}


def test_clear_file_ops_cache_local_removes_only_exact_session(monkeypatch):
    raw_ops = object()
    default_ops = object()
    monkeypatch.setattr(
        file_tools,
        "_file_ops_cache",
        {"session-a": raw_ops, "default": default_ops},
    )
    monkeypatch.setattr(
        terminal_tool, "_get_env_config", lambda: {"env_type": "local"}
    )

    file_tools.clear_file_ops_cache("session-a")

    assert file_tools._file_ops_cache == {"default": default_ops}


def test_clear_file_ops_cache_container_removes_collapsed_default(monkeypatch):
    raw_ops = object()
    default_ops = object()
    monkeypatch.setattr(
        file_tools,
        "_file_ops_cache",
        {"session-a": raw_ops, "default": default_ops},
    )
    monkeypatch.setattr(
        terminal_tool, "_get_env_config", lambda: {"env_type": "docker"}
    )
    monkeypatch.setattr(
        terminal_tool, "_resolve_container_task_id", lambda _task_id: "default"
    )

    file_tools.clear_file_ops_cache("session-a")

    assert file_tools._file_ops_cache == {"session-a": raw_ops}


def test_clear_file_ops_cache_container_removes_raw_cache_for_selected_raw_env(
    monkeypatch,
):
    raw_env = _FakeEnvironment("raw-cwd")
    default_env = _FakeEnvironment("default-cwd")
    raw_ops = object()
    default_ops = object()
    monkeypatch.setattr(
        terminal_tool,
        "_active_environments",
        {"session-a": raw_env, "default": default_env},
    )
    monkeypatch.setattr(
        file_tools,
        "_file_ops_cache",
        {"session-a": raw_ops, "default": default_ops},
    )
    monkeypatch.setattr(
        terminal_tool, "_get_env_config", lambda: {"env_type": "docker"}
    )
    monkeypatch.setattr(
        terminal_tool, "_resolve_container_task_id", lambda _task_id: "default"
    )

    file_tools.clear_file_ops_cache("session-a")

    assert file_tools._file_ops_cache == {"default": default_ops}


def test_file_tools_local_backend_keeps_session_cache_and_creation_keys(monkeypatch):
    created_task_ids = _configure_direct_creation_paths(monkeypatch, "local")

    file_ops_a = file_tools._get_file_ops("session-a")
    file_ops_b = file_tools._get_file_ops("session-b")

    assert file_ops_a is not file_ops_b
    assert created_task_ids == ["session-a", "session-b"]
    assert set(terminal_tool._active_environments) == {"session-a", "session-b"}
    assert set(terminal_tool._creation_locks) == {"session-a", "session-b"}
    assert set(file_tools._file_ops_cache) == {"session-a", "session-b"}


@pytest.mark.parametrize("raw_present", [True, False])
def test_file_tools_container_selects_raw_then_collapsed_environment_and_cache(
    monkeypatch, raw_present
):
    raw_env = _FakeEnvironment("raw-cwd")
    default_env = _FakeEnvironment("default-cwd")
    active = {"default": default_env}
    if raw_present:
        active["session-a"] = raw_env
    raw_ops = file_tools.ShellFileOperations(raw_env)
    default_ops = file_tools.ShellFileOperations(default_env)
    cache = {"default": default_ops}
    if raw_present:
        cache["session-a"] = raw_ops
    monkeypatch.setattr(
        terminal_tool, "_get_env_config", lambda: {"env_type": "docker"}
    )
    monkeypatch.setattr(
        terminal_tool, "_resolve_container_task_id", lambda _task_id: "default"
    )
    monkeypatch.setattr(terminal_tool, "_active_environments", active)
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(file_tools, "_file_ops_cache", cache)

    selected = file_tools._get_file_ops("session-a")

    expected_key = "session-a" if raw_present else "default"
    assert selected is (raw_ops if raw_present else default_ops)
    assert set(terminal_tool._last_activity) == {expected_key}


@pytest.mark.parametrize("caller", ["code_execution", "file_tools"])
def test_direct_creation_paths_keep_container_default_sharing(monkeypatch, caller):
    created_task_ids = _configure_direct_creation_paths(monkeypatch, "docker")

    if caller == "code_execution":
        first, _ = code_execution_tool._get_or_create_env("session-a")
        second, _ = code_execution_tool._get_or_create_env("session-b")
    else:
        first = file_tools._get_file_ops("session-a")
        second = file_tools._get_file_ops("session-b")

    assert first is second
    assert created_task_ids == ["default"]
    assert set(terminal_tool._active_environments) == {"default"}
    assert set(terminal_tool._creation_locks) == {"default"}
    if caller == "file_tools":
        assert set(file_tools._file_ops_cache) == {"default"}


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
