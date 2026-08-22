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
from hermes_cli import update_cmd


PRE_UPDATE_SHA = "a" * 40
UPDATE_TARGET_SHA = "d" * 40


def _make_head_moved_side_effect(
    pre_sha=PRE_UPDATE_SHA, post_sha=UPDATE_TARGET_SHA
):
    """Simulate git commands where HEAD advances from pre_sha to post_sha."""
    calls = {"n": 0}

    def side_effect(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)

        # git rev-parse --abbrev-ref HEAD  (get current branch)
        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return SimpleNamespace(returncode=0, stdout="main\n", stderr="")

        # git rev-parse --verify refs/remotes/origin/main  (receipt target)
        if "rev-parse --verify refs/remotes/origin/main" in joined:
            return SimpleNamespace(returncode=0, stdout=f"{post_sha}\n", stderr="")

        # git rev-list HEAD..origin/main --count  (behind count)
        if "rev-list" in joined:
            return SimpleNamespace(returncode=0, stdout="3\n", stderr="")

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


def _make_head_pinned_side_effect(
    sha=PRE_UPDATE_SHA, target_sha=UPDATE_TARGET_SHA
):
    """Simulate a detached checkout pinned to ``sha``: HEAD never moves."""

    def side_effect(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)

        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return SimpleNamespace(returncode=0, stdout="HEAD\n", stderr="")

        if "rev-parse --verify refs/remotes/origin/main" in joined:
            return SimpleNamespace(
                returncode=0, stdout=f"{target_sha}\n", stderr=""
            )

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
    monkeypatch.setattr(hermes_main.subprocess, "run", run_side_effect)
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".git").mkdir()  # pass the "is a git repo" gate
    monkeypatch.setattr(
        hermes_main, "_resolve_update_branch", lambda args: "main"
    )
    monkeypatch.setattr(hermes_main, "_is_windows", lambda: False)
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
    # The production path purges cached modules after pulling fresh source.
    # This unit fixture patches the already-imported backup module below, so
    # keep that exact object alive across parametrized cases.
    monkeypatch.setattr(hermes_main, "_purge_stale_hermes_modules", lambda: None)
    monkeypatch.setattr(
        hermes_main, "_run_pre_update_backup", lambda *a, **k: None
    )
    monkeypatch.setattr(
        hermes_main, "_pause_windows_gateways_for_update", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        hermes_main, "_resume_windows_gateways_after_update", lambda *a, **k: None
    )
    # Short-circuit the long tail: dependency install + desktop build.
    monkeypatch.setattr(hermes_main, "_write_update_incomplete_marker", lambda: None)
    monkeypatch.setattr(hermes_main, "_clear_update_incomplete_marker", lambda: None)
    monkeypatch.setattr(
        update_cmd,
        "_validate_critical_files_syntax",
        lambda _root: (True, None, None),
    )
    monkeypatch.setattr(
        update_cmd,
        "_validate_critical_modules_import",
        lambda _root: (True, None, None),
    )
    monkeypatch.setattr(
        update_cmd, "_venv_core_imports_healthy", lambda: (True, "")
    )
    monkeypatch.setattr(
        update_cmd, "_node_dependencies_healthy_read_only", lambda: True
    )
    monkeypatch.setattr(update_cmd, "_update_node_dependencies", lambda: [])
    monkeypatch.setattr(
        update_cmd,
        "_rebuild_desktop_after_update",
        lambda *_args, **_kwargs: True,
    )
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


def test_update_success_when_head_moves(monkeypatch, tmp_path, capsys):
    """When the pull advances HEAD, the update proceeds normally."""
    args = SimpleNamespace(branch=None, yes=False, force=False, force_venv=False)
    _patch_update_deps(monkeypatch, tmp_path, _make_head_moved_side_effect())

    hermes_main.cmd_update(args)  # completes normally (no SystemExit)

    out = capsys.readouterr().out
    assert "✓ Code updated!" in out
    assert "Code did not move" not in out


@pytest.mark.parametrize(
    ("node_failures", "terminal_event", "expected_exit_code"),
    [([], "receipt", None), (["ui-tui"], "summary", 1)],
    ids=["proven-success", "degraded-node-refresh"],
)
def test_sibling_cron_restore_precedes_terminal_update_outcome(
    monkeypatch, tmp_path, node_failures, terminal_event, expected_exit_code
):
    """Sibling recovery runs before either success proof or degraded output."""
    from hermes_cli import backup

    target_sha = UPDATE_TARGET_SHA
    base = _make_head_moved_side_effect(post_sha=target_sha)

    def run_side_effect(command, **kwargs):
        joined = " ".join(str(value) for value in command)
        if "rev-parse --verify refs/remotes/origin/main" in joined:
            return SimpleNamespace(returncode=0, stdout=f"{target_sha}\n", stderr="")
        return base(command, **kwargs)

    _patch_update_deps(monkeypatch, tmp_path, run_side_effect)
    events = []
    snapshots = {"sibling": "snapshot-id"}
    monkeypatch.setattr(update_cmd, "_LAST_SIBLING_SNAPSHOTS", snapshots)
    monkeypatch.setattr(
        backup,
        "restore_cron_jobs_all_profiles",
        lambda value: events.append("restore") or ([] if value is snapshots else None),
    )
    monkeypatch.setattr(
        update_cmd, "_update_node_dependencies", lambda: list(node_failures)
    )
    monkeypatch.setattr(
        update_cmd, "_rebuild_desktop_after_update", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        update_cmd,
        "_record_update_success",
        lambda *_args, **_kwargs: events.append("receipt"),
    )
    monkeypatch.setattr(
        update_cmd,
        "_print_update_summary",
        lambda **_kwargs: events.append("summary"),
    )
    monkeypatch.setattr(
        update_cmd,
        "_print_update_completion",
        lambda _message: events.append("completion"),
    )

    args = SimpleNamespace(branch=None, yes=False, force=False, force_venv=False)
    if expected_exit_code is None:
        hermes_main.cmd_update(args)
    else:
        with pytest.raises(SystemExit) as exc_info:
            hermes_main.cmd_update(args)
        assert exc_info.value.code == expected_exit_code

    assert events[0] == "restore"
    assert terminal_event in events[1:]
    if terminal_event == "receipt":
        assert events[-2:] == ["receipt", "completion"]
        assert "summary" not in events
    else:
        assert events[-1] == "summary"
        assert "receipt" not in events


def test_update_fails_loudly_when_head_pinned(monkeypatch, tmp_path, capsys):
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
