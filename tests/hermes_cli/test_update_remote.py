"""Tests for branch-aware remote selection in ``hermes update``."""

import subprocess

from hermes_cli.update_readiness import _resolve_update_target


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def test_resolve_update_target_uses_branch_tracking_remote(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "remote", "add", "origin", "https://github.com/NousResearch/hermes-agent.git")
    _git(tmp_path, "remote", "add", "fork", "https://github.com/royalaid/hermes-agent.git")
    _git(tmp_path, "config", "branch.fork-integration.remote", "fork")

    target = _resolve_update_target(["git"], tmp_path, "fork-integration")

    assert target.remote == "fork"
    assert target.tracking_ref == "refs/remotes/fork/fork-integration"


def test_resolve_update_target_falls_back_to_origin_without_branch_tracking(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "remote", "add", "origin", "https://github.com/NousResearch/hermes-agent.git")

    target = _resolve_update_target(["git"], tmp_path, "fork-integration")

    assert target.remote == "origin"
    assert target.tracking_ref == "refs/remotes/origin/fork-integration"
