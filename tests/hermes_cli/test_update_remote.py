"""Tests for branch-aware remote selection in ``hermes update``."""

import subprocess

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
