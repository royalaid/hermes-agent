"""Tests for the post-pull HEAD-movement gate in ``hermes update``.

Issue #79678: a detached/pinned checkout can report "N new commit(s)"
against origin, run the ff-only merge successfully, and still sit on the
old commit afterward (the branch-switch step re-detaches to the raw SHA).
Before this guard ``hermes update`` printed "✓ Code updated!" and
reinstalled deps + rebuilt the desktop app against the stale tree — no
error, no warning. The gate compares the pre-pull and post-pull HEAD SHA
and fails loudly when the update was a no-op.
"""

from types import SimpleNamespace

import pytest

from hermes_cli import main as hermes_main


def _make_head_moved_side_effect(pre_sha="abc123", post_sha="def456"):
    """Simulate git commands where HEAD advances from pre_sha to post_sha."""
    calls = {"n": 0}

    def side_effect(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)

        # git rev-parse --abbrev-ref HEAD  (get current branch)
        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return SimpleNamespace(returncode=0, stdout="main\n", stderr="")

        # git rev-list HEAD..origin/main --count  (behind count)
        if "rev-list" in joined:
            return SimpleNamespace(returncode=0, stdout="3\n", stderr="")

        # The updater records the exact fetched target and will only publish
        # a success receipt when the installed HEAD matches it.
        if "rev-parse" in joined and "--verify" in joined:
            return SimpleNamespace(returncode=0, stdout=f"{post_sha}\n", stderr="")

        # git rev-parse HEAD  — first call (pre-pull) returns pre_sha,
        # subsequent calls (post-pull) return post_sha.
        if joined.endswith("rev-parse HEAD"):
            if calls["n"] == 0:
                calls["n"] += 1
                return SimpleNamespace(returncode=0, stdout=f"{pre_sha}\n", stderr="")
            return SimpleNamespace(returncode=0, stdout=f"{post_sha}\n", stderr="")

        # Everything else (merge, checkout, etc.) succeeds quietly.
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return side_effect


def _make_head_pinned_side_effect(sha="abc123"):
    """Simulate a detached checkout pinned to ``sha``: HEAD never moves."""

    def side_effect(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)

        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return SimpleNamespace(returncode=0, stdout="HEAD\n", stderr="")

        if "rev-list" in joined:
            return SimpleNamespace(returncode=0, stdout="3\n", stderr="")

        if joined.endswith("rev-parse HEAD"):
            return SimpleNamespace(returncode=0, stdout=f"{sha}\n", stderr="")

        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return side_effect


def _patch_update_deps(monkeypatch, tmp_path, run_side_effect):
    """Patch the hermes_cli.main helpers ``_cmd_update_impl`` touches.

    ``_m()`` in update_cmd.py lazily returns hermes_cli.main, so patching
    attributes on that module is the canonical test surface (matches
    tests/hermes_cli/test_cmd_update.py).
    """
    from hermes_cli import managed_uv, update_cmd

    monkeypatch.setattr(hermes_main.subprocess, "run", run_side_effect)
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".git").mkdir()  # pass the "is a git repo" gate
    monkeypatch.setattr(
        hermes_main, "_resolve_update_branch", lambda args: "main"
    )
    monkeypatch.setattr(
        hermes_main, "_get_origin_url",
        lambda *a, **k: "https://github.com/NousResearch/hermes-agent.git",
    )
    monkeypatch.setattr(hermes_main, "_is_fork", lambda *a, **k: False)
    monkeypatch.setattr(
        hermes_main, "_stash_local_changes_if_needed", lambda *a, **k: None
    )
    monkeypatch.setattr(hermes_main, "_clear_bytecode_cache", lambda *a, **k: 0)
    monkeypatch.setattr(
        hermes_main, "_record_bytecode_fingerprint", lambda *a, **k: None
    )
    monkeypatch.setattr(
        hermes_main, "_run_pre_update_backup", lambda *a, **k: None
    )
    monkeypatch.setattr(
        hermes_main, "_resume_windows_gateways_after_update", lambda *a, **k: None
    )
    # Short-circuit the long tail: this module proves only the relationship
    # between the fetched target and pre/post-pull HEAD values.
    monkeypatch.setattr(managed_uv, "ensure_uv", lambda **_kwargs: "uv")
    monkeypatch.setattr(managed_uv, "update_managed_uv", lambda **_kwargs: None)
    monkeypatch.setattr(
        hermes_main,
        "_install_python_dependencies_with_optional_fallback",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        hermes_main, "_abort_dependency_sync_if_self_locked", lambda *a, **k: None
    )
    monkeypatch.setattr(
        hermes_main, "_capture_active_lazy_features", lambda: []
    )
    monkeypatch.setattr(
        hermes_main, "_capture_active_tool_dependencies", lambda: []
    )
    monkeypatch.setattr(
        hermes_main, "_refresh_active_lazy_features", lambda *a, **k: True
    )
    monkeypatch.setattr(
        hermes_main, "_restore_active_tool_dependencies", lambda *a, **k: None
    )
    monkeypatch.setattr(
        hermes_main, "_refresh_active_memory_provider_dependencies", lambda: None
    )
    monkeypatch.setattr(
        hermes_main, "_upgrade_pip_before_lazy_refresh", lambda *a, **k: None
    )
    monkeypatch.setattr(
        hermes_main, "_reload_updated_runtime_modules", lambda: None
    )
    monkeypatch.setattr(
        hermes_main, "_refresh_bootstrap_cache_scripts", lambda *a, **k: None
    )
    monkeypatch.setattr(hermes_main, "_build_web_ui", lambda *a, **k: True)
    monkeypatch.setattr(hermes_main, "_write_update_incomplete_marker", lambda: None)
    monkeypatch.setattr(hermes_main, "_clear_update_incomplete_marker", lambda: None)
    monkeypatch.setattr(
        hermes_main, "_clear_lazy_refresh_incomplete_marker", lambda: None
    )
    monkeypatch.setattr(update_cmd, "_write_lazy_refresh_incomplete_marker", lambda: None)
    monkeypatch.setattr(update_cmd, "_venv_core_imports_healthy", lambda: (True, ""))
    monkeypatch.setattr(
        update_cmd,
        "_validate_critical_files_syntax",
        lambda *_args: (True, None, None),
    )
    monkeypatch.setattr(
        update_cmd,
        "_validate_critical_modules_import",
        lambda *_args: (True, None, None),
    )
    monkeypatch.setattr(update_cmd, "_update_node_dependencies", lambda: [])
    monkeypatch.setattr(
        update_cmd, "_node_dependencies_healthy_read_only", lambda: True
    )
    monkeypatch.setattr(update_cmd, "_rebuild_desktop_after_update", lambda *a, **k: None)
    monkeypatch.setattr(update_cmd, "_record_update_success", lambda *a, **k: None)
    # Gateway restart path (called after a successful update).
    monkeypatch.setattr(hermes_main, "_finish_dashboard_update_cleanup", lambda *a: None)
    # Keep the (now surfaced — #78574) gateway auto-restart phase away from
    # this machine's real gateways: discovery returns nothing, systemd is
    # unsupported, so the phase is a clean no-op for both snapshots.
    import hermes_cli.gateway as hermes_gateway

    monkeypatch.setattr(
        hermes_gateway, "find_gateway_pids", lambda all_profiles=False: []
    )
    monkeypatch.setattr(
        hermes_gateway, "supports_systemd_services", lambda: False
    )
    monkeypatch.setattr(
        hermes_gateway, "find_profile_gateway_processes", lambda *a, **k: []
    )


def test_update_success_when_head_moves(
    monkeypatch, tmp_path, capsys, platform_neutral_update_lifecycle
):
    """When the pull advances HEAD, the update proceeds normally."""
    args = SimpleNamespace(branch=None, yes=False, force=False, force_venv=False)
    _patch_update_deps(monkeypatch, tmp_path, _make_head_moved_side_effect())

    hermes_main.cmd_update(args)  # completes normally (no SystemExit)

    out = capsys.readouterr().out
    assert "✓ Code updated!" in out
    assert "Code did not move" not in out


def test_update_fails_loudly_when_head_pinned(
    monkeypatch, tmp_path, capsys, platform_neutral_update_lifecycle
):
    """A detached/pinned HEAD that never moves must fail loudly, not print
    '✓ Code updated!' against the stale tree."""
    args = SimpleNamespace(branch=None, yes=False, force=False, force_venv=False)
    _patch_update_deps(monkeypatch, tmp_path, _make_head_pinned_side_effect())

    with pytest.raises(SystemExit) as exc_info:
        hermes_main.cmd_update(args)

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "Code did not move" in out
    assert "✓ Code updated!" not in out
    assert "checkout main" in out
