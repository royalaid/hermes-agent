"""Tests for branch-aware remote selection in ``hermes update``."""

import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli import update_cmd
from hermes_cli.update_cmd import _resolve_update_remote


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def test_resolve_update_remote_uses_branch_tracking_remote(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "remote", "add", "origin", "https://github.com/NousResearch/hermes-agent.git")
    _git(tmp_path, "remote", "add", "fork", "https://github.com/royalaid/hermes-agent.git")
    _git(tmp_path, "config", "branch.fork-integration.remote", "fork")

    assert _resolve_update_remote(["git"], tmp_path, "fork-integration") == "fork"


def test_resolve_update_remote_falls_back_to_origin_without_branch_tracking(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "remote", "add", "origin", "https://github.com/NousResearch/hermes-agent.git")

    assert _resolve_update_remote(["git"], tmp_path, "fork-integration") == "origin"


def test_resolve_update_remote_keeps_configured_missing_remote(tmp_path):
    """A broken authoritative remote must fail, never retarget to origin."""
    _git(tmp_path, "init")
    _git(
        tmp_path,
        "remote",
        "add",
        "origin",
        "https://github.com/NousResearch/hermes-agent.git",
    )
    _git(tmp_path, "config", "branch.fork-integration.remote", "missing-fork")

    assert (
        _resolve_update_remote(["git"], tmp_path, "fork-integration")
        == "missing-fork"
    )


def test_resolve_update_remote_falls_back_for_local_dot_remote(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "branch.local.remote", ".")

    assert _resolve_update_remote(["git"], tmp_path, "local") == "origin"


def test_update_uses_branch_remote_for_every_divergence_ref(monkeypatch):
    """Fetch, tracking checkout, comparison, merge, and reset stay on the fork."""
    from hermes_cli import main as hm

    branch = "fork-integration"
    remote = "fork"
    remote_ref = f"{remote}/{branch}"
    commands: list[list[str]] = []

    def run(cmd, **_kwargs):
        command = [str(part) for part in cmd]
        commands.append(command)

        if command[-3:] == ["config", "--get", f"branch.{branch}.remote"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{remote}\n", stderr="")
        if command[-3:] == ["remote", "get-url", remote]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout="https://github.com/example/hermes-agent.git\n",
                stderr="",
            )
        if command[-3:] == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="main\n", stderr="")
        if command[-2:] == ["checkout", branch]:
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="missing locally"
            )
        if "rev-list" in command:
            return subprocess.CompletedProcess(cmd, 0, stdout="1\n", stderr="")
        if command[-2:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"{'a' * 40}\n", stderr=""
            )
        if "merge" in command or "reset" in command:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="diverged")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(update_cmd.subprocess, "run", run)
    monkeypatch.setattr(update_cmd.sys, "platform", "linux")
    monkeypatch.setattr(update_cmd, "_discard_lockfile_churn", lambda *_args: None)
    monkeypatch.setattr(update_cmd, "_normalize_managed_eol", lambda *_args: None)
    monkeypatch.setattr(hm, "_is_windows", lambda: False)
    monkeypatch.setattr(hm, "_run_pre_update_backup", lambda _args: None)
    monkeypatch.setattr(hm, "_pause_windows_gateways_for_update", lambda: None)
    monkeypatch.setattr(
        hm,
        "_get_origin_url",
        lambda *_args: "https://github.com/NousResearch/hermes-agent.git",
    )
    monkeypatch.setattr(
        hm, "_stash_local_changes_if_needed", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})

    args = SimpleNamespace(
        branch=branch,
        yes=True,
        force=False,
        force_venv=False,
    )
    with pytest.raises(SystemExit) as exc_info:
        update_cmd._cmd_update_impl(args, gateway_mode=False)

    assert exc_info.value.code == 1
    assert ["git", "fetch", remote, branch] in commands
    assert ["git", "checkout", "-B", branch, remote_ref] in commands
    assert ["git", "rev-list", f"HEAD..{remote_ref}", "--count"] in commands
    assert ["git", "merge", "--ff-only", remote_ref] in commands
    assert ["git", "reset", "--hard", remote_ref] in commands
    assert not any(
        f"origin/{branch}" in argument
        for command in commands
        for argument in command
    )


def test_update_main_tracking_non_origin_never_syncs_origin(monkeypatch, tmp_path):
    """A successful fork-owned main update must not run origin fork-sync."""
    from hermes_cli import main as hm

    class ReachedDependencyInstall(RuntimeError):
        pass

    branch = "main"
    remote = "fork"
    commands: list[list[str]] = []
    sync_calls: list[tuple] = []
    (tmp_path / ".git").mkdir()

    def run(cmd, **_kwargs):
        command = [str(part) for part in cmd]
        commands.append(command)

        if command[-3:] == ["config", "--get", "branch.main.remote"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"{remote}\n", stderr=""
            )
        if command[-3:] == ["remote", "get-url", remote]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout="https://github.com/example/hermes-agent.git\n",
                stderr="",
            )
        if command[-3:] == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="main\n", stderr="")
        if command[-2:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"{'a' * 40}\n", stderr=""
            )
        if "rev-list" in command:
            return subprocess.CompletedProcess(cmd, 0, stdout="1\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(update_cmd.subprocess, "run", run)
    monkeypatch.setattr(update_cmd.sys, "platform", "linux")
    monkeypatch.setattr(update_cmd, "_discard_lockfile_churn", lambda *_args: None)
    monkeypatch.setattr(update_cmd, "_normalize_managed_eol", lambda *_args: None)
    monkeypatch.setattr(
        update_cmd, "_validate_critical_files_syntax", lambda *_args: (True, None, None)
    )
    monkeypatch.setattr(update_cmd, "_invalidate_update_cache", lambda: None)
    monkeypatch.setattr(
        update_cmd,
        "_write_update_incomplete_marker",
        lambda: (_ for _ in ()).throw(ReachedDependencyInstall()),
    )
    monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(hm, "_is_windows", lambda: False)
    monkeypatch.setattr(hm, "_run_pre_update_backup", lambda _args: None)
    monkeypatch.setattr(hm, "_pause_windows_gateways_for_update", lambda: None)
    monkeypatch.setattr(
        hm,
        "_get_origin_url",
        lambda *_args: "https://github.com/NousResearch/hermes-agent.git",
    )
    monkeypatch.setattr(hm, "_stash_local_changes_if_needed", lambda *_args: None)
    monkeypatch.setattr(hm, "_clear_bytecode_cache", lambda *_args: 0)
    monkeypatch.setattr(hm, "_record_bytecode_fingerprint", lambda: None)
    monkeypatch.setattr(hm, "_refresh_bootstrap_cache_scripts", lambda _branch: None)
    monkeypatch.setattr(
        hm,
        "_sync_with_upstream_if_needed",
        lambda *args: sync_calls.append(args),
    )
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})

    args = SimpleNamespace(branch=branch, yes=True, force=False, force_venv=False)
    with pytest.raises(ReachedDependencyInstall):
        update_cmd._cmd_update_impl(args, gateway_mode=False)

    assert ["git", "fetch", remote, branch] in commands
    assert ["git", "merge", "--ff-only", f"{remote}/{branch}"] in commands
    assert sync_calls == []
    assert not any(
        "origin/main" in argument or "upstream/main" in argument
        for command in commands
        for argument in command
    )
