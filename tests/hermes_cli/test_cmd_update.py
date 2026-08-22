"""Tests for cmd_update — branch fallback when remote branch doesn't exist."""

import hashlib
import os
import shutil
import subprocess
import sys
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import ANY, patch

import pytest

from hermes_cli.main import cmd_update, PROJECT_ROOT
from hermes_cli.update_transaction import _UpdateTransaction


def _transaction_lease(root, lease_id, *, owner_pid=None, created_at=100):
    owner_pid = os.getpid() if owner_pid is None else owner_pid
    return {
        "schema_version": 1,
        "lease_id": lease_id,
        "owner_pid": owner_pid,
        "created_at": created_at,
        "expires_at": created_at + 120,
        "handoff_grace_until": created_at + 60,
        "install_root": str(root.resolve()),
    }


def _make_run_side_effect(branch="main", verify_ok=True, commit_count="0"):
    """Build a side_effect function for subprocess.run that simulates git commands."""

    target_sha = "a" * 40
    head_sha = "0" * 40 if int(commit_count) > 0 else target_sha

    def side_effect(cmd, **kwargs):
        nonlocal head_sha
        joined = " ".join(str(c) for c in cmd)

        # git rev-parse --abbrev-ref HEAD  (get current branch)
        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{branch}\n", stderr="")

        # git rev-parse --verify origin/{branch}  (check remote branch exists)
        if "rev-parse" in joined and "--verify" in joined:
            rc = 0 if verify_ok else 128
            stdout = f"{target_sha}\n" if verify_ok else ""
            return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr="")

        # Receipt proof resolves the installed HEAD after fork sync/update.
        if "rev-parse" in joined and "HEAD" in joined:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"{head_sha}\n", stderr=""
            )

        if "merge" in [str(value) for value in cmd] and "merge-base" not in joined:
            head_sha = target_sha
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        # git rev-list HEAD..origin/{branch} --count
        if "rev-list" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{commit_count}\n", stderr="")

        # Fallback: return a successful CompletedProcess with empty stdout
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return side_effect


@pytest.fixture
def mock_args():
    return SimpleNamespace()


# ---------------------------------------------------------------------------
# Managed-uv compatibility for tests that patch shutil.which
# ---------------------------------------------------------------------------
# The production code now uses ``ensure_uv()`` / ``update_managed_uv()``
# instead of ``shutil.which("uv")``.  Many tests in this file patch
# ``shutil.which`` to control whether uv is "available" — these autouse
# fixtures make the managed_uv functions delegate to the patched
# ``shutil.which`` so the existing test setup keeps working without
# per-test changes.
@pytest.fixture(autouse=True)
def _patch_managed_uv(request, platform_neutral_update_lifecycle):
    """Make managed_uv helpers follow shutil.which mocking in tests."""
    import shutil

    # resolve_uv delegates to shutil.which("uv") so that test patches
    # on shutil.which flow through naturally.
    def _fake_resolve_uv():
        return shutil.which("uv")

    def _fake_ensure_uv(**_kwargs):
        return shutil.which("uv")

    def _fake_update_managed_uv(**_kwargs):
        return None  # never actually self-update in tests

    generic_cmd_update_classes = {
        "TestCmdUpdateBranchFallback",
        "TestCmdUpdateMigrationPrompt",
        "TestCmdUpdateProfileSkillSync",
        "TestCmdUpdateBranchFlag",
    }
    patch_node = (
        patch("hermes_cli.update_cmd._update_node_dependencies", return_value=[])
        if request.cls and request.cls.__name__ in generic_cmd_update_classes
        else nullcontext()
    )
    patch_node_health = (
        patch(
            "hermes_cli.update_cmd._node_dependencies_healthy_read_only",
            return_value=True,
        )
        if request.cls and request.cls.__name__ in generic_cmd_update_classes
        else nullcontext()
    )

    with patch("hermes_cli.managed_uv.resolve_uv", side_effect=_fake_resolve_uv), \
         patch("hermes_cli.managed_uv.ensure_uv", side_effect=_fake_ensure_uv), \
         patch("hermes_cli.managed_uv.update_managed_uv", side_effect=_fake_update_managed_uv), \
         patch_node, patch_node_health:
        yield


@pytest.fixture(autouse=True)
def _patch_gateway_discovery():
    """Keep cmd_update's gateway auto-restart phase off this machine's gateways.

    The restart phase used to swallow every exception at debug level, so these
    end-to-end tests never noticed it touching real gateway discovery. Since
    the phase is surfaced (#78574: an aborted restart now fails the update),
    an unmocked ``find_gateway_pids`` on a box with a live gateway reaches the
    conftest live-system guard and turns into a spurious ``sys.exit(1)``.
    Discovery returning nothing makes the phase a clean no-op for every test
    in this module (none of them assert on gateway restarts).
    """
    with patch("hermes_cli.gateway.find_gateway_pids", return_value=[]), \
         patch("hermes_cli.gateway.supports_systemd_services", return_value=False), \
         patch("hermes_cli.gateway.find_profile_gateway_processes", return_value=[]):
        yield


class TestCmdUpdateNpmLockfileCache:
    @staticmethod
    def _cache_file(hermes_root, project_root):
        cache_key = hashlib.sha256(str(project_root).encode()).hexdigest()[:12]
        return hermes_root / f".npm_lock_hash_{cache_key}"



    def test_record_npm_lockfile_hash(self, tmp_path, monkeypatch):
        from hermes_cli import main as hm

        monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)
        (tmp_path / "package-lock.json").write_text('{"lockfileVersion": 3}')
        (tmp_path / "package.json").write_text('{"dependencies": {}}')

        hm._record_npm_lockfile_hash(tmp_path)

        assert (
            self._cache_file(tmp_path, tmp_path).read_text()
            == hm._npm_manifests_digest()
        )

    def test_package_json_only_edit_defeats_skip(self, tmp_path, monkeypatch):
        """Reviewer scenario (#61580): dev edits package.json WITHOUT running
        npm — lockfile unchanged. `hermes update` must still install (the
        npm-install fallback is what syncs node_modules in that state)."""
        from hermes_cli import main as hm

        monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)
        (tmp_path / "package-lock.json").write_text('{"lockfileVersion": 3}')
        (tmp_path / "package.json").write_text('{"dependencies": {}}')
        (tmp_path / "node_modules").mkdir()
        hm._record_npm_lockfile_hash(tmp_path)
        assert hm._npm_lockfile_changed(tmp_path) is False

        (tmp_path / "package.json").write_text(
            '{"dependencies": {"left-pad": "^1.0.0"}}'
        )
        assert hm._npm_lockfile_changed(tmp_path) is True

    def test_node_health_probe_reports_cache_read_failure_as_unknown(
        self, tmp_path, monkeypatch
    ):
        from hermes_cli import main as hm
        from hermes_cli import update_cmd

        (tmp_path / "package.json").write_text("{}")
        monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)

        with patch.object(
            update_cmd,
            "_npm_lockfile_changed",
            side_effect=OSError("cache unreadable"),
        ):
            assert update_cmd._node_dependencies_healthy_read_only() is None







    def test_update_uses_one_shared_npm_cache_across_profiles(
        self, tmp_path, monkeypatch
    ):
        """The npm cache describes checkout-global node_modules, not a profile."""
        from hermes_cli import main as hm
        import hermes_constants

        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / "package.json").write_text("{}")
        shared_root = tmp_path / ".hermes"
        named_profile = shared_root / "profiles" / "work"
        named_profile.mkdir(parents=True)

        monkeypatch.setattr(hm, "PROJECT_ROOT", checkout)
        monkeypatch.setattr(hermes_constants.Path, "home", lambda: tmp_path)
        monkeypatch.setattr(
            hermes_constants, "find_node_executable", lambda _name: "/usr/bin/npm"
        )

        cache_roots = []
        with patch.object(
            hm,
            "_npm_lockfile_changed",
            side_effect=lambda root: cache_roots.append(root) or False,
        ):
            monkeypatch.setenv("HERMES_HOME", str(shared_root))
            hm._update_node_dependencies()

            monkeypatch.setenv("HERMES_HOME", str(named_profile))
            hm._update_node_dependencies()

        assert cache_roots == [shared_root, shared_root]


class TestCmdUpdateTermuxUvBootstrap:
    """Regression tests for Termux-specific uv bootstrap behavior."""

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_termux_uv_bootstrap_uses_binary_only_install(
        self, mock_run, _mock_which, monkeypatch
    ):
        from hermes_cli import main as hm

        mock_run.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="")
        monkeypatch.setattr(hm, "_is_termux_env", lambda env=None: True)

        uv_bin = hm._ensure_uv_for_termux(["/termux/python", "-m", "pip"])

        assert uv_bin is None
        assert mock_run.call_count == 1
        assert mock_run.call_args.args[0] == [
            "/termux/python",
            "-m",
            "pip",
            "install",
            "uv",
            "--only-binary",
            ":all:",
        ]
        assert mock_run.call_args.kwargs["cwd"] == PROJECT_ROOT
        assert mock_run.call_args.kwargs["check"] is False

    @patch("subprocess.run")
    def test_termux_reuses_existing_path_uv_without_pip(self, mock_run, monkeypatch):
        """A uv already on PATH (e.g. ``pkg install uv``) is reused before pip runs."""
        from hermes_cli import main as hm

        pkg_uv = "/data/data/com.termux/files/usr/bin/uv"
        monkeypatch.setattr(hm, "_is_termux_env", lambda env=None: True)
        # Production resolve_uv only checks $HERMES_HOME/bin/uv; model an empty
        # managed dir so the PATH probe is what surfaces the packaged uv.
        monkeypatch.setattr("hermes_cli.managed_uv.resolve_uv", lambda: None)
        monkeypatch.setattr("shutil.which", lambda name: pkg_uv if name == "uv" else None)

        uv_bin = hm._ensure_uv_for_termux(["/termux/python", "-m", "pip"])

        assert uv_bin == pkg_uv
        mock_run.assert_not_called()


class TestCmdUpdateBranchFallback:
    """cmd_update falls back to main when current branch has no remote counterpart."""

    def test_source_mutation_state_blocks_archive_after_failed_operation(self):
        from hermes_cli import update_cmd

        state = update_cmd._SourceMutationState()

        with pytest.raises(RuntimeError, match="injected mutation failure"):
            state.run(
                lambda: (_ for _ in ()).throw(
                    RuntimeError("injected mutation failure")
                )
            )

        assert state.phase is update_cmd._SourcePhase.MUTATION_STARTED
        assert not update_cmd._archive_fallback_is_safe(
            is_windows=True,
            deferred_gateway_resume=False,
            source_phase=state.phase,
        )

    @pytest.mark.windows_only
    def test_transactional_acquisition_freezes_target_before_live_ref_update(
        self, tmp_path, monkeypatch
    ):
        """A moving remote cannot change the target proven in isolation."""
        from hermes_cli import update_cmd

        home = tmp_path / "home"
        remote = tmp_path / "remote.git"
        writer = tmp_path / "writer"
        live = tmp_path / "live"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(update_cmd, "get_default_hermes_root", lambda: home)

        subprocess.run(["git", "init", "--bare", str(remote)], check=True)
        subprocess.run(["git", "init", "-b", "main", str(writer)], check=True)
        subprocess.run(
            ["git", "-C", str(writer), "config", "user.name", "Hermes Test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(writer), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        (writer / "payload.txt").write_text("first", encoding="utf-8")
        subprocess.run(["git", "-C", str(writer), "add", "payload.txt"], check=True)
        subprocess.run(["git", "-C", str(writer), "commit", "-m", "first"], check=True)
        first_sha = subprocess.run(
            ["git", "-C", str(writer), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(writer), "remote", "add", "origin", str(remote)],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(writer), "push", "-u", "origin", "main"],
            check=True,
        )
        subprocess.run(
            ["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"],
            check=True,
        )
        subprocess.run(["git", "clone", str(remote), str(live)], check=True)

        target = update_cmd._UpdateTarget(
            branch="main",
            remote="origin",
            tracking_ref="refs/remotes/origin/main",
            refspec="+refs/heads/main:refs/remotes/origin/main",
        )
        acquisition = update_cmd._acquire_transactional_git_target(
            update_cmd._git_cmd(),
            live,
            target,
            remote_url=str(remote),
            invocation_id="invocation-acquisition-123456",
            lease=_transaction_lease(live, "lease-acquisition-123456"),
        )
        try:
            assert acquisition.target_sha == first_sha

            (writer / "payload.txt").write_text("second", encoding="utf-8")
            subprocess.run(["git", "-C", str(writer), "add", "payload.txt"], check=True)
            subprocess.run(["git", "-C", str(writer), "commit", "-m", "second"], check=True)
            subprocess.run(["git", "-C", str(writer), "push", "origin", "main"], check=True)

            update_cmd._import_transactional_git_target(
                update_cmd._git_cmd(),
                live,
                target,
                acquisition,
            )
        finally:
            acquisition.cleanup()

        assert not (home / ".hermes-update-acquisition.json").exists()
        assert not acquisition.workspace.exists()

        imported = subprocess.run(
            ["git", "-C", str(live), "rev-parse", "refs/remotes/origin/main"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert imported == first_sha

    @pytest.mark.windows_only
    def test_transactional_acquisition_never_persists_or_argv_logs_remote_secret(
        self, tmp_path, monkeypatch, caplog, capsys
    ):
        from hermes_cli import update_cmd

        secret = "https://credential-shaped.invalid/user:token-value@example/repo.git"
        home = tmp_path / "home"
        live = tmp_path / "live"
        home.mkdir()
        live.mkdir()
        monkeypatch.setattr(update_cmd, "get_default_hermes_root", lambda: home)
        monkeypatch.setattr(update_cmd, "_tracking_ref_sha", lambda *_a, **_k: None)
        monkeypatch.setattr(
            update_cmd,
            "_verify_complete_git_graph",
            lambda *_a, **_k: True,
        )
        commands = []

        def quiet_git(command, *, cwd, env, timeout=120):
            commands.append(list(command))
            if "init" in command:
                repository = update_cmd.Path(command[-1])
                repository.mkdir()
                (repository / "config").write_text(
                    "[core]\n\tbare = true\n",
                    encoding="utf-8",
                )
            assert env["GIT_CONFIG_VALUE_0"] == secret
            return True

        monkeypatch.setattr(update_cmd, "_run_quiet_git", quiet_git)
        real_run = update_cmd.subprocess.run

        def fake_run(command, **kwargs):
            if "rev-parse" in command:
                return subprocess.CompletedProcess(command, 0, "d" * 40 + "\n", "")
            return real_run(command, **kwargs)

        monkeypatch.setattr(update_cmd.subprocess, "run", fake_run)
        target = update_cmd._UpdateTarget(
            branch="codex/disposable",
            remote="origin",
            tracking_ref="refs/remotes/origin/codex/disposable",
            refspec=(
                "+refs/heads/codex/disposable:"
                "refs/remotes/origin/codex/disposable"
            ),
        )

        acquisition = update_cmd._acquire_transactional_git_target(
            update_cmd._git_cmd(),
            live,
            target,
            remote_url=secret,
            invocation_id="invocation-acquisition-secret-1234",
            lease=_transaction_lease(live, "lease-acquisition-secret-1234"),
        )
        try:
            assert all(secret not in str(argument) for command in commands for argument in command)
            for path in acquisition.workspace.rglob("*"):
                if path.is_file():
                    assert secret.encode() not in path.read_bytes()
            captured = capsys.readouterr()
            assert secret not in captured.out
            assert secret not in captured.err
            assert secret not in caplog.text
        finally:
            acquisition.cleanup()

        assert not (home / ".hermes-update-acquisition.json").exists()
        assert not acquisition.repository.exists()

    @pytest.mark.windows_only
    def test_complete_graph_proof_rejects_missing_object(self, tmp_path):
        from hermes_cli import update_cmd

        source = tmp_path / "source"
        repository = tmp_path / "repository.git"
        subprocess.run(["git", "init", "-b", "main", str(source)], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.name", "Hermes Test"], check=True)
        subprocess.run(
            ["git", "-C", str(source), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        (source / "payload.txt").write_text("reachable object", encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", "payload.txt"], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-m", "payload"], check=True)
        target_sha = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        blob_sha = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD:payload.txt"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(["git", "clone", "--bare", str(source), str(repository)], check=True)
        env = update_cmd._acquisition_git_env()
        assert update_cmd._verify_complete_git_graph(
            update_cmd._git_cmd(), repository, target_sha, env=env
        )

        blob_object = repository / "objects" / blob_sha[:2] / blob_sha[2:]
        assert blob_object.is_file()
        os.chmod(blob_object, 0o700)
        blob_object.unlink()

        assert not update_cmd._verify_complete_git_graph(
            update_cmd._git_cmd(), repository, target_sha, env=env
        )

    @pytest.mark.windows_only
    def test_complete_graph_proof_requires_fsck_success(self, tmp_path, monkeypatch):
        from hermes_cli import update_cmd

        repository = tmp_path / "repository.git"
        repository.mkdir()
        seen = []
        monkeypatch.setattr(
            update_cmd.subprocess,
            "run",
            lambda command, **_kwargs: subprocess.CompletedProcess(command, 1),
        )

        def proof(command, **_kwargs):
            seen.append(command)
            return "fsck" not in command

        monkeypatch.setattr(update_cmd, "_run_quiet_git", proof)

        assert not update_cmd._verify_complete_git_graph(
            update_cmd._git_cmd(), repository, "d" * 40, env={}
        )
        assert any("fsck" in command for command in seen)

    @pytest.mark.windows_only
    @pytest.mark.parametrize("unsafe", ["shallow", "alternates", "promisor"])
    def test_complete_graph_proof_rejects_partial_repository_modes(
        self, tmp_path, unsafe
    ):
        from hermes_cli import update_cmd

        repository = tmp_path / "repository.git"
        subprocess.run(["git", "init", "--bare", str(repository)], check=True)
        if unsafe == "shallow":
            (repository / "shallow").write_text("d" * 40 + "\n", encoding="ascii")
        elif unsafe == "alternates":
            alternates = repository / "objects" / "info" / "alternates"
            alternates.parent.mkdir(parents=True, exist_ok=True)
            alternates.write_text(str(tmp_path / "foreign-objects"), encoding="utf-8")
        else:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "config",
                    "remote.source.promisor",
                    "true",
                ],
                check=True,
            )

        assert not update_cmd._verify_complete_git_graph(
            update_cmd._git_cmd(),
            repository,
            "d" * 40,
            env=update_cmd._acquisition_git_env(),
        )

    @pytest.mark.windows_only
    @pytest.mark.parametrize(
        ("commit_count", "handed_off_sync", "stage_fails"),
        [("0", True, False), ("1", False, False), ("1", False, True)],
        ids=("current-checkout-repair", "changed-checkout", "changed-stage-failure"),
    )
    def test_deferred_windows_update_stages_without_live_venv_mutation(
        self,
        monkeypatch,
        commit_count,
        handed_off_sync,
        stage_fails,
    ):
        """The real deferred Desktop route ends after publishing a candidate."""
        from hermes_cli import main as hm
        from hermes_cli import update_cmd

        transaction = _UpdateTransaction(
            invocation_id="invocation-stage-route-123456",
            lease={
                "schema_version": 1,
                "lease_id": "lease-stage-route-123456",
                "owner_pid": __import__("os").getpid(),
            },
        )
        events = []
        commands = []
        staged = []
        live_mutations = []
        target_sha = "b" * 40
        target = update_cmd._UpdateTarget(
            branch="main",
            remote="origin",
            tracking_ref="refs/remotes/origin/main",
            refspec="+refs/heads/main:refs/remotes/origin/main",
        )
        monkeypatch.setattr(hm, "_run_pre_update_backup", lambda _args: None)
        monkeypatch.setattr(
            hm,
            "_pause_windows_gateways_for_update",
            lambda **_kwargs: None,
        )
        monkeypatch.setattr(hm, "_capture_active_lazy_features", lambda: [])
        monkeypatch.setattr(hm, "_capture_active_tool_dependencies", lambda: [])
        monkeypatch.setattr(update_cmd, "_desktop_app_present", lambda _path: False)
        monkeypatch.setattr(update_cmd, "_discard_lockfile_churn", lambda *_args: None)
        monkeypatch.setattr(update_cmd, "_normalize_managed_eol", lambda *_args: None)
        monkeypatch.setattr(
            update_cmd,
            "_get_remote_url",
            lambda *_args: "https://example.invalid/hermes-agent.git",
        )
        monkeypatch.setattr(update_cmd, "_is_fork", lambda _url: False)
        monkeypatch.setattr(
            update_cmd,
            "_resolve_update_target",
            lambda *_args, **_kwargs: target,
        )
        monkeypatch.setattr(
            update_cmd, "_assert_safe_git_configuration", lambda *_a, **_k: None
        )
        from hermes_cli import gitlock

        monkeypatch.setattr(gitlock, "clear_stale_git_locks", lambda _root: [])
        acquisition = SimpleNamespace(
            target_sha=target_sha,
            cleanup=lambda: events.append("acquisition-cleaned"),
        )
        monkeypatch.setattr(
            update_cmd,
            "_acquire_transactional_git_target",
            lambda *_args, **_kwargs: events.append("package-verified")
            or acquisition,
        )
        monkeypatch.setattr(
            update_cmd,
            "_import_transactional_git_target",
            lambda *_args, **_kwargs: events.append("package-imported"),
        )
        monkeypatch.setattr(
            hm,
            "_stash_local_changes_if_needed",
            lambda *_args: events.append("source-prepared"),
        )
        monkeypatch.setattr(
            update_cmd,
            "_write_deferred_gateway_plan",
            lambda *_args, **_kwargs: None,
        )
        def stage(**kwargs):
            kwargs["source_mutation"].run(lambda: None)
            events.append("candidate-staged")
            staged.append(kwargs)
            if stage_fails:
                raise RuntimeError("injected candidate staging failure")

        monkeypatch.setattr(
            update_cmd, "_run_transactional_desktop_route", stage
        )
        monkeypatch.setattr(
            update_cmd,
            "_write_update_incomplete_marker",
            lambda: live_mutations.append("core-marker"),
        )
        monkeypatch.setattr(
            hm,
            "_install_python_dependencies_with_optional_fallback",
            lambda *_args, **_kwargs: live_mutations.append("live-install"),
        )
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
        monkeypatch.setattr(update_cmd, "_node_dependencies_healthy_read_only", lambda: True)
        monkeypatch.setattr(update_cmd, "_update_node_dependencies", lambda: [])
        monkeypatch.setattr(update_cmd, "_record_update_success", lambda *_args, **_kwargs: {})
        monkeypatch.setattr(update_cmd, "_print_update_completion", lambda *_args: None)
        fake_run = _make_run_side_effect(commit_count=commit_count)

        def traced_run(command, **kwargs):
            commands.append([str(value) for value in command])
            return fake_run(command, **kwargs)

        monkeypatch.setattr(update_cmd.subprocess, "run", traced_run)
        if handed_off_sync:
            monkeypatch.setenv(hm._UPDATE_REEXEC_ENV, "1")
        else:
            monkeypatch.delenv(hm._UPDATE_REEXEC_ENV, raising=False)

        call = lambda: update_cmd._cmd_update_impl(
                SimpleNamespace(
                    branch="main",
                    defer_gateway_resume=True,
                    force=True,
                    force_venv=True,
                    gateway=True,
                    keep_stash=True,
                    switch_branch=False,
                    yes=True,
                ),
                gateway_mode=True,
                transaction=transaction,
            )
        if stage_fails:
            with pytest.raises(RuntimeError, match="injected candidate staging failure"):
                call()
        else:
            call()

        assert len(staged) == 1
        assert live_mutations == []
        assert events == [
            "package-verified",
            "package-imported",
            "acquisition-cleaned",
            "candidate-staged",
        ]
        assert not any("fetch" in command for command in commands)

    @pytest.mark.windows_only
    def test_deferred_windows_update_refuses_archive_without_git(
        self, tmp_path, monkeypatch
    ):
        from hermes_cli import main as hm
        from hermes_cli import update_cmd

        root = tmp_path / "not-a-git-checkout"
        root.mkdir()
        archive_calls = []
        monkeypatch.setattr(hm, "PROJECT_ROOT", root)
        monkeypatch.setattr(hm, "_run_pre_update_backup", lambda _args: None)
        monkeypatch.setattr(
            hm, "_pause_windows_gateways_for_update", lambda **_kwargs: None
        )
        monkeypatch.setattr(hm, "_capture_active_lazy_features", lambda: [])
        monkeypatch.setattr(hm, "_capture_active_tool_dependencies", lambda: [])
        monkeypatch.setattr(update_cmd, "_desktop_app_present", lambda _path: False)
        monkeypatch.setattr(
            update_cmd,
            "_update_via_zip",
            lambda *_args, **_kwargs: archive_calls.append(True),
        )

        with pytest.raises(SystemExit) as exit_info:
            update_cmd._cmd_update_impl(
                SimpleNamespace(
                    branch="main",
                    defer_gateway_resume=True,
                    force=True,
                    force_venv=True,
                    gateway=True,
                    keep_stash=True,
                    switch_branch=False,
                    yes=True,
                ),
                gateway_mode=True,
                transaction=_UpdateTransaction(
                    invocation_id="invocation-no-archive-123456",
                    lease={
                        "schema_version": 1,
                        "lease_id": "lease-no-archive-123456",
                        "owner_pid": __import__("os").getpid(),
                    },
                ),
            )

        assert exit_info.value.code == 1
        assert archive_calls == []

    @pytest.mark.windows_only
    @pytest.mark.parametrize("stage_fails", [False, True])
    def test_deferred_windows_update_retargets_without_rewriting_custom_branch(
        self, tmp_path, monkeypatch, stage_fails
    ):
        from hermes_cli import desktop_update_activation as activation
        from hermes_cli import main as hm
        from hermes_cli import update_cmd

        root = tmp_path / "checkout"
        home = tmp_path / "home"
        home.mkdir()
        subprocess.run(["git", "init", "-b", "fork-integration", str(root)], check=True)
        subprocess.run(
            ["git", "-C", str(root), "config", "user.name", "Hermes Test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        (root / "base.txt").write_text("base", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "base.txt"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", "base"], check=True)
        subprocess.run(["git", "-C", str(root), "branch", "main"], check=True)
        (root / "custom.txt").write_text("custom", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "custom.txt"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", "custom"], check=True)
        custom_sha = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(["git", "-C", str(root), "checkout", "main"], check=True)
        (root / "target.txt").write_text("target", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "target.txt"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", "target"], check=True)
        target_sha = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(["git", "-C", str(root), "checkout", "fork-integration"], check=True)
        target = update_cmd._UpdateTarget(
            branch="main",
            remote="origin",
            tracking_ref="refs/remotes/origin/main",
            refspec="refs/heads/main:refs/remotes/origin/main",
        )
        monkeypatch.setattr(hm, "PROJECT_ROOT", root)
        monkeypatch.setattr(update_cmd, "get_default_hermes_root", lambda: home)
        transaction = _UpdateTransaction(
            invocation_id="invocation-custom-branch-123456",
            lease=_transaction_lease(root, "lease-custom-branch-123456"),
        )

        def stage(**kwargs):
            assert update_cmd._capture_head_sha(update_cmd._git_cmd(), root) == target_sha
            if stage_fails:
                raise RuntimeError("injected staging failure")
            activation.retire_staging_journal(
                root,
                home=home,
                invocation_id=transaction.invocation_id,
                lease_id=transaction.lease["lease_id"],
            )

        monkeypatch.setattr(update_cmd, "_stage_transactional_desktop_environment", stage)
        call = lambda: update_cmd._run_transactional_desktop_route(
            git_cmd=update_cmd._git_cmd(),
            git_env=update_cmd._sanitized_git_env(),
            transaction=transaction,
            update_target=target,
            target_sha=target_sha,
            source_mutation=update_cmd._SourceMutationState(),
            active_lazy_features=[],
            active_tool_dependencies=[],
        )
        if stage_fails:
            from hermes_cli.update_diagnostics import UpdateDiagnosticError

            with pytest.raises(UpdateDiagnosticError) as failure:
                call()
            assert failure.value.code == "HDU201"
            assert failure.value.stage == "candidate-staging"
            assert failure.value.__cause__ is None
        else:
            call()

        branch = subprocess.run(
            ["git", "-C", str(root), "branch", "--show-current"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert branch == ("fork-integration" if stage_fails else "main")
        assert subprocess.run(
            ["git", "-C", str(root), "rev-parse", "refs/heads/fork-integration"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip() == custom_sha

    @pytest.mark.windows_only
    def test_divergent_selected_branch_failure_restores_original_branch(
        self, tmp_path, monkeypatch
    ):
        from hermes_cli import main as hm
        from hermes_cli import update_cmd

        root = tmp_path / "checkout"
        home = tmp_path / "home"
        home.mkdir()
        subprocess.run(["git", "init", "-b", "fork-integration", str(root)], check=True)
        subprocess.run(
            ["git", "-C", str(root), "config", "user.name", "Hermes Test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        (root / "base.txt").write_text("base", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "base.txt"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", "base"], check=True)
        base_sha = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(["git", "-C", str(root), "branch", "main"], check=True)

        (root / "custom.txt").write_text("custom", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "custom.txt"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", "custom"], check=True)
        original_sha = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        subprocess.run(["git", "-C", str(root), "checkout", "main"], check=True)
        (root / "main-only.txt").write_text("main", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "main-only.txt"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", "main-only"], check=True)
        selected_pre_head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        subprocess.run(["git", "-C", str(root), "checkout", "--detach", base_sha], check=True)
        (root / "target.txt").write_text("target", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "target.txt"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", "target"], check=True)
        target_sha = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(root), "checkout", "fork-integration"], check=True
        )

        monkeypatch.setattr(hm, "PROJECT_ROOT", root)
        monkeypatch.setattr(update_cmd, "get_default_hermes_root", lambda: home)
        monkeypatch.setattr(
            update_cmd,
            "_stage_transactional_desktop_environment",
            lambda **_kwargs: pytest.fail("divergent source must not reach staging"),
        )
        transaction = _UpdateTransaction(
            invocation_id="invocation-divergent-branch-123456",
            lease=_transaction_lease(root, "lease-divergent-branch-123456"),
        )
        target = update_cmd._UpdateTarget(
            branch="main",
            remote="origin",
            tracking_ref="refs/remotes/origin/main",
            refspec="+refs/heads/main:refs/remotes/origin/main",
        )

        from hermes_cli.update_diagnostics import UpdateDiagnosticError

        with pytest.raises(UpdateDiagnosticError) as failure:
            update_cmd._run_transactional_desktop_route(
                git_cmd=update_cmd._git_cmd(),
                git_env=update_cmd._sanitized_git_env(),
                transaction=transaction,
                update_target=target,
                target_sha=target_sha,
                source_mutation=update_cmd._SourceMutationState(),
                active_lazy_features=[],
                active_tool_dependencies=[],
            )
        assert failure.value.code == "HDU301"
        assert failure.value.stage == "source-route"
        assert failure.value.__cause__ is None

        assert subprocess.run(
            ["git", "-C", str(root), "branch", "--show-current"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip() == "fork-integration"
        assert subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip() == original_sha
        assert subprocess.run(
            ["git", "-C", str(root), "rev-parse", "refs/heads/main"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip() == selected_pre_head

    @pytest.mark.windows_only
    def test_crash_after_selected_branch_checkout_restores_exact_original(
        self, tmp_path
    ):
        from hermes_cli import desktop_update_activation as activation

        root = tmp_path / "checkout"
        home = tmp_path / "home"
        home.mkdir()
        subprocess.run(["git", "init", "-b", "fork-integration", str(root)], check=True)
        subprocess.run(
            ["git", "-C", str(root), "config", "user.name", "Hermes Test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        (root / "base.txt").write_text("base", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "base.txt"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", "base"], check=True)
        selected_pre_head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(["git", "-C", str(root), "branch", "main"], check=True)
        (root / "custom.txt").write_text("custom", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "custom.txt"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", "custom"], check=True)
        original_sha = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        invocation = "invocation-crash-after-checkout-123456"
        lease = _transaction_lease(root, "lease-crash-after-checkout-123456")
        activation.write_staging_journal(
            root,
            home=home,
            invocation_id=invocation,
            lease=lease,
            pre_update_head=original_sha,
            pre_update_branch="fork-integration",
            branch="main",
            selected_pre_head=selected_pre_head,
            target_head=selected_pre_head,
        )
        activation.update_staging_journal(
            root,
            home=home,
            invocation_id=invocation,
            lease_id=lease["lease_id"],
            phase="source-selecting",
        )
        subprocess.run(["git", "-C", str(root), "checkout", "main"], check=True)

        activation.recover_staging_journal(
            root, home, invocation, lease["lease_id"]
        )

        assert subprocess.run(
            ["git", "-C", str(root), "branch", "--show-current"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip() == "fork-integration"
        assert subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip() == original_sha
        assert subprocess.run(
            ["git", "-C", str(root), "rev-parse", "refs/heads/main"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip() == selected_pre_head
        assert not (home / ".hermes-update-staging.json").exists()

    @pytest.mark.windows_only
    def test_real_windows_transactional_route_preserves_live_venv_and_publishes_recovery_first(
        self, tmp_path, monkeypatch
    ):
        from hermes_cli import desktop_update_activation as activation
        from hermes_cli import main as hm
        from hermes_cli import managed_uv
        from hermes_cli import update_cmd
        import hermes_mcp_update_gate as gate
        import importlib

        gate = importlib.reload(gate)

        remote = tmp_path / "remote.git"
        writer = tmp_path / "writer"
        root = tmp_path / "install"
        home = tmp_path / "home"
        home.mkdir()
        installed_uv = (
            update_cmd.Path(os.environ["LOCALAPPDATA"])
            / "hermes"
            / "bin"
            / "uv.exe"
        )
        uv_source = str(installed_uv) if installed_uv.is_file() else shutil.which("uv")
        assert update_cmd.Path(uv_source).is_file(), "real uv executable is required"
        managed_uv_path = home / "bin" / "uv.exe"
        managed_uv_path.parent.mkdir()
        shutil.copy2(uv_source, managed_uv_path)
        uv_cache = tmp_path / "uv-cache"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True)
        subprocess.run(["git", "init", "-b", "main", str(writer)], check=True)
        subprocess.run(["git", "-C", str(writer), "config", "user.name", "Hermes Test"], check=True)
        subprocess.run(
            ["git", "-C", str(writer), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        for relative in (
            "hermes_cli/__init__.py",
            "hermes_cli/config.py",
            "hermes_cli/main.py",
            "hermes_cli/web_server.py",
            "cli.py",
            "hermes_constants.py",
            "hermes_state.py",
            "model_tools.py",
            "run_agent.py",
            "toolsets.py",
        ):
            (writer / relative).parent.mkdir(parents=True, exist_ok=True)
            (writer / relative).write_text("# integration smoke module\n", encoding="utf-8")
        (writer / "pyproject.toml").write_text(
            """[project]
name = "hermes-route-fixture"
version = "0.0.0"
requires-python = ">=3.11,<3.14"
dependencies = [
  "fastapi>=0.104.0,<1",
  "openai==2.24.0",
  "prompt_toolkit==3.0.52",
  "pydantic==2.13.4",
  "python-dotenv==1.2.2",
  "pyyaml==6.0.3",
  "rich==14.3.3",
  "uvicorn>=0.24.0,<1",
]

[project.optional-dependencies]
all = []

[build-system]
requires = ["setuptools>=70"]
build-backend = "setuptools.build_meta"

[tool.setuptools]
packages = ["hermes_cli"]
py-modules = [
  "cli",
  "hermes_constants",
  "hermes_state",
  "model_tools",
  "run_agent",
  "toolsets",
]
""",
            encoding="utf-8",
        )
        (writer / ".gitignore").write_text(
            "venv/\n.hermes-runtime/\n__pycache__/\n*.pyc*\n*.egg-info/\n",
            encoding="utf-8",
        )
        subprocess.run(
            [str(managed_uv_path), "lock", "--python", sys.executable],
            cwd=writer,
            env={**os.environ, "UV_CACHE_DIR": str(uv_cache), "UV_NO_CONFIG": "true"},
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        subprocess.run(["git", "-C", str(writer), "add", "."], check=True)
        subprocess.run(["git", "-C", str(writer), "commit", "-m", "base"], check=True)
        subprocess.run(["git", "-C", str(writer), "remote", "add", "origin", str(remote)], check=True)
        subprocess.run(["git", "-C", str(writer), "push", "-u", "origin", "main"], check=True)
        subprocess.run(["git", "clone", "--branch", "main", str(remote), str(root)], check=True)
        pre_head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        (writer / "integration-target.txt").write_text("target", encoding="utf-8")
        subprocess.run(["git", "-C", str(writer), "add", "integration-target.txt"], check=True)
        subprocess.run(["git", "-C", str(writer), "commit", "-m", "integration target"], check=True)
        subprocess.run(["git", "-C", str(writer), "push", "origin", "main"], check=True)

        live = root / "venv"
        subprocess.run(
            [sys.executable, "-m", "venv", str(live)],
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        (live / "live-sentinel.txt").write_text("unchanged", encoding="utf-8")

        def tree_digest(path):
            digest = hashlib.sha256()
            for item in sorted(path.rglob("*")):
                if item.is_file():
                    digest.update(item.relative_to(path).as_posix().encode())
                    digest.update(item.read_bytes())
            return digest.hexdigest()

        before_live = tree_digest(live)
        marker = home / ".quiesce-lease.json"
        lease = gate.write_quiesce_lease(root, marker=marker, owner_pid=os.getpid())
        transaction = _UpdateTransaction(
            invocation_id="invocation-real-route-123456",
            lease=lease,
        )
        target = update_cmd._UpdateTarget(
            branch="main",
            remote="origin",
            tracking_ref="refs/remotes/origin/main",
            refspec="+refs/heads/main:refs/remotes/origin/main",
        )
        monkeypatch.setattr(hm, "PROJECT_ROOT", root)
        monkeypatch.setattr(update_cmd, "get_default_hermes_root", lambda: home)
        monkeypatch.setattr(managed_uv, "get_hermes_home", lambda: home)
        monkeypatch.setattr(managed_uv, "resolve_uv", lambda: str(managed_uv_path))
        monkeypatch.setenv("UV_CACHE_DIR", str(uv_cache))
        monkeypatch.setattr(gate, "marker_path", lambda: marker)
        monkeypatch.setattr(update_cmd, "_active_memory_provider_specs", lambda: ())
        assert managed_uv.ensure_uv_without_runtime_cutover() == str(managed_uv_path)
        uv_commands = []
        real_run = subprocess.run

        def trace_uv(command, *args, **kwargs):
            argv = [str(value) for value in command]
            if argv and os.path.normcase(argv[0]) == os.path.normcase(str(managed_uv_path)):
                uv_commands.append(argv)
            return real_run(command, *args, **kwargs)

        monkeypatch.setattr(managed_uv.subprocess, "run", trace_uv)
        acquisition = update_cmd._acquire_transactional_git_target(
            update_cmd._git_cmd(),
            root,
            target,
            remote_url=str(remote),
            invocation_id=transaction.invocation_id,
            lease=lease,
        )
        try:
            target_sha = acquisition.target_sha
            update_cmd._import_transactional_git_target(
                update_cmd._git_cmd(), root, target, acquisition
            )
        finally:
            acquisition.cleanup()

        lease = gate.write_quiesce_lease(
            root,
            marker=marker,
            lease_id=lease["lease_id"],
            owner_pid=os.getpid(),
            expected_owner_pid=os.getpid(),
        )
        transaction.lease = lease
        assert gate.marker_path() == marker
        assert gate.live_quiesce_lease(marker, install_root=root) == lease

        update_cmd._run_transactional_desktop_route(
            git_cmd=update_cmd._git_cmd(),
            git_env=update_cmd._sanitized_git_env(),
            transaction=transaction,
            update_target=target,
            target_sha=target_sha,
            source_mutation=update_cmd._SourceMutationState(),
            active_lazy_features=[],
            active_tool_dependencies=[],
        )

        manifest_path = home / activation._MANIFEST_NAME
        plan_path = transaction.deferred_gateway_plan_path
        assert isinstance(plan_path, update_cmd.Path) and plan_path.is_file()
        assert manifest_path.is_file()
        assert plan_path.stat().st_mtime_ns <= manifest_path.stat().st_mtime_ns
        manifest, _ = activation._validated_manifest(
            root,
            home,
            transaction.invocation_id,
            lease["lease_id"],
        )
        assert manifest["pre_update_head"] == pre_head
        assert manifest["target_head"] == target_sha
        assert any(
            command[1] == "venv"
            and "--relocatable" in command
            for command in uv_commands
        )
        assert any(
            command[1] == "sync"
            and "--frozen" in command
            for command in uv_commands
        )
        candidate = root / manifest["candidate_rel"]
        assert (candidate / "Scripts" / "python.exe").is_file()
        assert tree_digest(live) == before_live
        assert not (home / activation._STAGING_NAME).exists()
        assert not (home / activation._ACQUISITION_NAME).exists()


    def test_foreign_upstream_remote_refuses_before_fetch_or_merge(
        self, tmp_path, monkeypatch, capsys
    ):
        """A remote named upstream is not authority to merge foreign code."""
        from hermes_cli import update_cmd

        repo = tmp_path / "repo"
        foreign = tmp_path / "foreign.git"
        subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
        subprocess.run(["git", "init", "--bare", "--quiet", str(foreign)], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "remote", "add", "upstream", str(foreign)],
            check=True,
        )

        real_run = subprocess.run
        commands: list[list[str]] = []

        def traced_run(command, **kwargs):
            commands.append([str(value) for value in command])
            return real_run(command, **kwargs)

        monkeypatch.setattr(update_cmd.subprocess, "run", traced_run)

        assert not update_cmd._has_upstream_remote(["git"], repo)
        update_cmd._sync_with_upstream_if_needed(["git"], repo)

        operative = [
            command
            for command in commands
            if "fetch" in command or "merge" in command
        ]
        assert operative == []
        assert "not the official Hermes repository" in capsys.readouterr().out

    def test_local_instead_of_refuses_no_upstream_path_before_fetch_or_merge(
        self, tmp_path, monkeypatch, capsys
    ):
        """A literal official URL must not be redirected by repo config."""
        from hermes_cli import update_cmd

        repo = tmp_path / "repo"
        foreign = tmp_path / "foreign.git"
        subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
        subprocess.run(["git", "init", "--bare", "--quiet", str(foreign)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "config",
                "--local",
                f"url.{foreign.as_uri()}.insteadOf",
                update_cmd.OFFICIAL_REPO_URL,
            ],
            check=True,
        )

        real_run = subprocess.run
        commands: list[list[str]] = []

        def traced_run(command, **kwargs):
            values = [str(value) for value in command]
            commands.append(values)
            if "fetch" in values:
                # Never touch the network while reproducing the vulnerable
                # command selection; observing the fetch is already failure.
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            return real_run(command, **kwargs)

        monkeypatch.setattr(update_cmd.subprocess, "run", traced_run)

        with patch("builtins.input", return_value="y"):
            update_cmd._sync_with_upstream_if_needed(["git"], repo)

        assert not any("fetch" in command or "merge" in command for command in commands)
        assert "URL rewrite" in capsys.readouterr().out
        assert real_run(
            ["git", "-C", str(repo), "remote", "get-url", "upstream"],
            capture_output=True,
            text=True,
        ).returncode != 0

    def test_official_upstream_fetch_uses_immutable_url_and_exact_refspec(
        self, tmp_path, monkeypatch
    ):
        from hermes_cli import update_cmd

        commands: list[list[str]] = []

        def fake_run(command, **_kwargs):
            commands.append([str(value) for value in command])
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        monkeypatch.setattr(
            update_cmd,
            "_get_remote_url",
            lambda *_args: update_cmd.OFFICIAL_REPO_URL,
        )
        monkeypatch.setattr(update_cmd.subprocess, "run", fake_run)
        monkeypatch.setattr(update_cmd, "_count_commits_between", lambda *_args: 0)

        assert update_cmd._has_upstream_remote(["git"], tmp_path)
        update_cmd._sync_with_upstream_if_needed(["git"], tmp_path)

        fetches = [command for command in commands if "fetch" in command]
        assert fetches == [
            [
                "git",
                "fetch",
                update_cmd.OFFICIAL_REPO_URL,
                "+refs/heads/main:refs/remotes/upstream/main",
                "--quiet",
            ]
        ]




    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_update_on_fork_checks_upstream_when_origin_up_to_date(
        self, mock_run, _mock_which, mock_args, capsys
    ):
        """Regression for issue #26172: forks whose local HEAD already matches
        origin/main must still consult upstream/main before printing
        "Already up to date!" — otherwise a fork that's caught up to its own
        origin but behind NousResearch/hermes-agent silently misses updates.
        """
        from hermes_cli import main as hm

        mock_run.side_effect = _make_run_side_effect(
            branch="main", verify_ok=True, commit_count="0"
        )

        with patch(
            "hermes_cli.update_cmd._get_remote_url",
            return_value="https://github.com/example/hermes-agent.git",
        ), patch.object(hm, "_sync_with_upstream_if_needed") as sync_mock:
            cmd_update(mock_args)

        from hermes_cli.update_cmd import _git_cmd

        expected_git_cmd = _git_cmd()
        sync_mock.assert_called_once_with(
            expected_git_cmd, PROJECT_ROOT, fork_remote="origin"
        )
        captured = capsys.readouterr()
        assert "Already up to date!" in captured.out

    def test_update_non_interactive_runs_safe_config_migrations(self, mock_args, capsys):
        """Dashboard/web updates apply non-interactive migrations before restart."""
        with patch("shutil.which", return_value=None), patch(
            "subprocess.run"
        ) as mock_run, patch("builtins.input") as mock_input, patch(
            "hermes_cli.config.get_missing_env_vars", return_value=["MISSING_KEY"]
        ), patch(
            "hermes_cli.config.get_missing_config_fields",
            return_value=[{"key": "new.option", "default": True}],
        ), patch(
            "hermes_cli.update_cmd._reload_config_modules"
        ), patch(
            "hermes_cli.update_cmd._run_config_check_fresh", return_value=(1, 2)
        ), patch(
            "hermes_cli.update_cmd._run_migrate_config_fresh",
            return_value={"env_added": [], "config_added": ["new.option"]},
        ) as migrate_config, patch("hermes_cli.main.sys") as mock_sys:
            mock_sys.stdin.isatty.return_value = False
            mock_sys.stdout.isatty.return_value = False
            mock_run.side_effect = _make_run_side_effect(
                branch="main", verify_ok=True, commit_count="1"
            )

            cmd_update(mock_args)

            mock_input.assert_not_called()
            migrate_config.assert_called_once_with(interactive=False, quiet=False)
            captured = capsys.readouterr()
            assert "applying safe config migrations" in captured.out
            assert "API keys require manual entry" in captured.out


class TestCmdUpdateMigrationPrompt:
    """The config-migration prompt names what changed and skips the prompt
    entirely when only the config format version moved.

    Regression guard for the contentless-prompt report (ScottFive / Tt2021):
    previously the prompt printed only counts ("1 new config option") and
    asked "configure them now?" even for pure version bumps, where saying
    yes looked like a no-op.
    """

    def test_version_bump_only_applies_silently_without_prompt(
        self, mock_args, capsys
    ):
        """Only the version moved → apply non-interactively, never prompt."""
        with patch("shutil.which", return_value=None), patch(
            "subprocess.run"
        ) as mock_run, patch("builtins.input") as mock_input, patch(
            "hermes_cli.config.get_missing_env_vars", return_value=[]
        ), patch(
            "hermes_cli.config.get_missing_config_fields", return_value=[]
        ), patch(
            "hermes_cli.update_cmd._reload_config_modules"
        ), patch(
            "hermes_cli.update_cmd._run_config_check_fresh", return_value=(5, 24)
        ), patch(
            "hermes_cli.update_cmd._run_migrate_config_fresh",
            return_value={"env_added": [], "config_added": [], "warnings": []},
        ) as mock_migrate:
            mock_run.side_effect = _make_run_side_effect(
                branch="main", verify_ok=True, commit_count="1"
            )

            cmd_update(mock_args)

            mock_input.assert_not_called()
            mock_migrate.assert_called_once_with(interactive=False, quiet=True)
            out = capsys.readouterr().out
            assert "Updating config format (v5 → v24)" in out
            assert "no new settings to configure" in out
            # The misleading question must NOT appear for a pure version bump.
            assert "configure them now" not in out.lower()

    def test_version_bump_only_surfaces_migration_resets(
        self, mock_args, capsys
    ):
        """A quiet version-bump migration that RESETS a user setting must say so.

        Regression for #86656: the v33→v34 personality reset ran with
        quiet=True and its results dict was discarded, so the update printed
        "no new settings to configure" while silently wiping
        display.personality. Migration-step mutations (config_added) and
        warnings must be re-surfaced even in the silent branch.
        """
        with patch("shutil.which", return_value=None), patch(
            "subprocess.run"
        ) as mock_run, patch("builtins.input") as mock_input, patch(
            "hermes_cli.config.get_missing_env_vars", return_value=[]
        ), patch(
            "hermes_cli.config.get_missing_config_fields", return_value=[]
        ), patch(
            "hermes_cli.update_cmd._reload_config_modules"
        ), patch(
            "hermes_cli.update_cmd._run_config_check_fresh", return_value=(33, 34)
        ), patch(
            "hermes_cli.update_cmd._run_migrate_config_fresh",
            return_value={
                "env_added": [],
                "config_added": ["display.personality=none (one-time reset)"],
                "warnings": ["Disabled suspicious MCP server 'evil'"],
            },
        ):
            mock_run.side_effect = _make_run_side_effect(
                branch="main", verify_ok=True, commit_count="1"
            )

            cmd_update(mock_args)

            mock_input.assert_not_called()
            out = capsys.readouterr().out
            assert "Updating config format (v33 → v34)" in out
            assert "no new settings to configure" in out
            # The migration's mutation note and warning must NOT be swallowed.
            assert "display.personality=none (one-time reset)" in out
            assert "Disabled suspicious MCP server 'evil'" in out

    def test_new_options_are_listed_by_name_before_prompt(
        self, mock_args, capsys
    ):
        """New env/config keys are printed by name so the user can decide."""
        env_items = [
            {"name": "FOO_API_KEY", "description": "Foo service API key"},
        ]
        cfg_items = [
            {"key": "display.new_widget", "description": "New config option: display.new_widget"},
        ]
        with patch("shutil.which", return_value=None), patch(
            "subprocess.run"
        ) as mock_run, patch("builtins.input", return_value="n"), patch(
            "hermes_cli.config.get_missing_env_vars", return_value=env_items
        ), patch(
            "hermes_cli.config.get_missing_config_fields", return_value=cfg_items
        ), patch(
            "hermes_cli.update_cmd._reload_config_modules"
        ), patch(
            "hermes_cli.update_cmd._run_config_check_fresh", return_value=(1, 24)
        ), patch(
            "hermes_cli.update_cmd._run_migrate_config_fresh",
            return_value={"env_added": [], "config_added": [], "warnings": []},
        ), patch("hermes_cli.main.sys") as mock_sys:
            mock_sys.stdin.isatty.return_value = True
            mock_sys.stdout.isatty.return_value = True
            mock_run.side_effect = _make_run_side_effect(
                branch="main", verify_ok=True, commit_count="1"
            )

            cmd_update(mock_args)

            out = capsys.readouterr().out
            # Names, not just counts.
            assert "FOO_API_KEY" in out
            assert "Foo service API key" in out
            assert "display.new_widget" in out


class TestConfigVersionCheckUsesFreshModules:
    """Regression: config migration must use freshly-reloaded modules, not the
    sys.modules cache from before git pull.

    Before the fix, ``hermes update`` ran in the PRE-pull Python process.
    After ``git pull`` updated the source on disk, function-level imports
    returned the OLD cached ``hermes_cli.config`` module — so
    ``DEFAULT_CONFIG["_config_version"]`` was stale and
    ``check_config_version()`` reported ``(33, 33)`` "up to date" even though
    the freshly-pulled code had v34 with a migration to run. The personality
    reset migration (#81946) was silently skipped this way.
    """

    def test_run_config_check_fresh_reloads_modules(self):
        """_run_config_check_fresh must call _reload_config_modules which
        force-reloads the config modules from disk.

        Regression: config migration was silently skipped because
        sys.modules held the OLD hermes_cli.config with the OLD
        DEFAULT_CONFIG["_config_version"] after git pull.
        """
        from unittest.mock import patch

        import hermes_cli.update_cmd as update_cmd

        with patch.object(update_cmd, "_reload_config_modules") as mock_reload:
            update_cmd._run_config_check_fresh()

        mock_reload.assert_called_once()


class TestCmdUpdateProfileSkillSync:
    """cmd_update syncs bundled skills to all profiles, including the active one.

    Regression guard for #16176: previously the active profile was excluded
    from the seed_profile_skills loop, leaving it on stale skill content.
    """

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_active_profile_included_in_skill_sync(
        self, mock_run, _mock_which, mock_args, capsys
    ):
        from pathlib import Path

        mock_run.side_effect = _make_run_side_effect(
            branch="main", verify_ok=True, commit_count="1"
        )

        default_p = SimpleNamespace(name="default", path=Path("/fake/.hermes"))
        active_p = SimpleNamespace(name="bit", path=Path("/fake/.hermes/profiles/bit"))
        other_p = SimpleNamespace(name="work", path=Path("/fake/.hermes/profiles/work"))
        all_profiles = [default_p, active_p, other_p]

        synced_paths = []

        def fake_seed(path, quiet=False):
            synced_paths.append(path)
            return {"copied": [], "updated": [], "user_modified": []}

        empty_sync = {"copied": [], "updated": [], "user_modified": [], "cleaned": []}

        with (
            patch("hermes_cli.profiles.list_profiles", return_value=all_profiles),
            patch("hermes_cli.profiles.seed_profile_skills", side_effect=fake_seed),
            patch("tools.skills_sync.sync_skills", return_value=empty_sync),
        ):
            cmd_update(mock_args)

        assert active_p.path in synced_paths, (
            f"Active profile 'bit' must be included in skill sync; got: {synced_paths}"
        )
        assert set(synced_paths) == {p.path for p in all_profiles}, (
            f"All profiles must be synced; got: {synced_paths}"
        )

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_single_profile_default_is_synced(
        self, mock_run, _mock_which, mock_args, capsys
    ):
        from pathlib import Path

        mock_run.side_effect = _make_run_side_effect(
            branch="main", verify_ok=True, commit_count="1"
        )

        default_p = SimpleNamespace(name="default", path=Path("/fake/.hermes"))
        synced_paths = []

        def fake_seed(path, quiet=False):
            synced_paths.append(path)
            return {"copied": [], "updated": [], "user_modified": []}

        empty_sync = {"copied": [], "updated": [], "user_modified": [], "cleaned": []}

        with (
            patch("hermes_cli.profiles.list_profiles", return_value=[default_p]),
            patch("hermes_cli.profiles.seed_profile_skills", side_effect=fake_seed),
            patch("tools.skills_sync.sync_skills", return_value=empty_sync),
        ):
            cmd_update(mock_args)

        assert default_p.path in synced_paths


class TestCmdUpdateBranchFlag:
    """``hermes update --branch <name>`` targets the requested branch.

    The CLI default stays 'main'; --branch lets callers pick a different
    target without monkey-patching the implementation.
    """

    def _branch_side_effect(self, current_branch, target_branch, *, checkout_fails=False, track_fails=False, commit_count="0"):
        """Mock side-effect that knows about checkout/track behavior.

        - ``current_branch``  what ``git rev-parse --abbrev-ref HEAD`` returns
        - ``target_branch``   passed via --branch; what we expect the code to switch to
        - ``checkout_fails``  if True, ``git checkout <target>`` returns non-zero
                              (simulates branch absent locally; code should retry with -B)
        - ``track_fails``     if True, ``git checkout -B <target> origin/<target>`` ALSO fails
                              (simulates branch absent on origin too)
        - ``commit_count``    rev-list count returned (0 = up-to-date, >0 = behind)
        """

        target_sha = "a" * 40
        head_sha = "0" * 40 if int(commit_count) > 0 else target_sha

        def side_effect(cmd, **kwargs):
            nonlocal head_sha
            joined = " ".join(str(c) for c in cmd)

            if "rev-parse" in joined and "--abbrev-ref" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{current_branch}\n", stderr="")

            if "rev-parse" in joined and "--verify" in joined:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=f"{target_sha}\n", stderr=""
                )

            if "rev-parse" in joined and "HEAD" in joined:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=f"{head_sha}\n", stderr=""
                )

            if "merge" in [str(value) for value in cmd] and "merge-base" not in joined:
                head_sha = target_sha
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            if "checkout" in joined and "-B" in joined:
                rc = 128 if track_fails else 0
                err = f"fatal: '{target_branch}' did not match any file(s) known to git\n" if track_fails else ""
                return subprocess.CompletedProcess(cmd, rc, stdout="", stderr=err)

            if "checkout" in joined and "-B" not in joined and "rev-parse" not in joined:
                rc = 128 if checkout_fails else 0
                err = f"error: pathspec '{target_branch}' did not match\n" if checkout_fails else ""
                return subprocess.CompletedProcess(cmd, rc, stdout="", stderr=err)

            if "rev-list" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{commit_count}\n", stderr="")

            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        return side_effect

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_branch_flag_pulls_against_named_branch(self, mock_run, _mock_which, capsys):
        """--branch bb/gui makes rev-list and pull target origin/bb/gui."""
        mock_run.side_effect = self._branch_side_effect(
            current_branch="bb/gui", target_branch="bb/gui", commit_count="3"
        )
        args = SimpleNamespace(branch="bb/gui")

        cmd_update(args)

        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]

        # rev-list must compare against origin/bb/gui, not origin/main
        rev_list_cmds = [c for c in commands if "rev-list" in c]
        assert any("origin/bb/gui" in c for c in rev_list_cmds), rev_list_cmds
        assert not any("origin/main" in c for c in rev_list_cmds), rev_list_cmds

        # the ff-only merge must target origin/bb/gui
        merge_cmds = [c for c in commands if "merge --ff-only" in c]
        assert any("origin/bb/gui" in c and "origin/main" not in c for c in merge_cmds), merge_cmds

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_divergent_custom_branch_uses_resolved_tracking_ref_without_reset(
        self, mock_run, _mock_which, monkeypatch
    ):
        """A custom branch merges the resolved target and never hard-resets."""
        from hermes_cli import update_cmd

        target = SimpleNamespace(
            branch="canary",
            remote="fork",
            tracking_ref="refs/remotes/fork/canary",
            refspec="+refs/heads/canary:refs/remotes/fork/canary",
        )
        monkeypatch.setattr(
            update_cmd,
            "_resolve_update_target",
            lambda *_args, **_kwargs: target,
        )
        base = self._branch_side_effect(
            current_branch="canary",
            target_branch="canary",
            commit_count="3",
        )

        def side_effect(command, **kwargs):
            joined = " ".join(str(value) for value in command)
            if "merge --ff-only refs/remotes/fork/canary" in joined:
                return subprocess.CompletedProcess(
                    command, 1, stdout="", stderr="injected divergence"
                )
            if "branch --show-current" in joined:
                return subprocess.CompletedProcess(
                    command, 0, stdout="local-work\n", stderr=""
                )
            if "reset --hard" in joined:
                pytest.fail(f"custom branch reached destructive reset: {joined}")
            return base(command, **kwargs)

        mock_run.side_effect = side_effect
        cmd_update(SimpleNamespace(branch="canary"))

        commands = [
            " ".join(str(value) for value in call.args[0])
            for call in mock_run.call_args_list
        ]
        assert any(
            "merge --no-edit refs/remotes/fork/canary" in command
            for command in commands
        )
        assert not any("reset --hard" in command for command in commands)


    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_branch_flag_fails_when_branch_missing_everywhere(self, mock_run, _mock_which, capsys):
        """If branch doesn't exist locally OR on origin, exit non-zero with clear error."""
        mock_run.side_effect = self._branch_side_effect(
            current_branch="main",
            target_branch="nonexistent",
            checkout_fails=True,
            track_fails=True,
            commit_count="0",
        )
        args = SimpleNamespace(branch="nonexistent")

        with pytest.raises(SystemExit) as exc_info:
            cmd_update(args)
        assert exc_info.value.code == 1

        out = capsys.readouterr().out
        assert "does not exist locally or on origin" in out
        assert "nonexistent" in out

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_no_update_refuses_receipt_when_original_branch_cannot_be_restored(
        self, mock_run, _mock_which, capsys
    ):
        from hermes_cli import update_cmd

        base = self._branch_side_effect(
            current_branch="work", target_branch="main", commit_count="0"
        )

        def side_effect(command, **kwargs):
            joined = " ".join(str(value) for value in command)
            if " cherry " in f" {joined} ":
                return subprocess.CompletedProcess(
                    command, 0, stdout=f"+ {'c' * 40}\n", stderr=""
                )
            if "checkout work" in joined:
                return subprocess.CompletedProcess(
                    command, 1, stdout="", stderr="injected checkout failure"
                )
            if "symbolic-ref --quiet --short HEAD" in joined:
                return subprocess.CompletedProcess(
                    command, 0, stdout="work\n", stderr=""
                )
            return base(command, **kwargs)

        mock_run.side_effect = side_effect
        args = SimpleNamespace(branch="main")

        with (
            patch(
                "hermes_cli.config.load_config",
                return_value={
                    "updates": {"parked_branch_strategy": "update_in_place"}
                },
            ),
            patch.object(update_cmd, "_record_update_success") as record,
        ):
            with pytest.raises(SystemExit) as exit_info:
                cmd_update(args)

        assert exit_info.value.code == 1
        record.assert_not_called()
        assert "could not be restored" in capsys.readouterr().out

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_no_update_refuses_receipt_when_symbolic_branch_proof_is_wrong(
        self, mock_run, _mock_which, capsys
    ):
        from hermes_cli import update_cmd

        base = self._branch_side_effect(
            current_branch="work", target_branch="main", commit_count="0"
        )

        def side_effect(command, **kwargs):
            joined = " ".join(str(value) for value in command)
            if " cherry " in f" {joined} ":
                return subprocess.CompletedProcess(
                    command, 0, stdout=f"+ {'c' * 40}\n", stderr=""
                )
            if "checkout work" in joined:
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            if "symbolic-ref --quiet --short HEAD" in joined:
                return subprocess.CompletedProcess(
                    command, 0, stdout="main\n", stderr=""
                )
            return base(command, **kwargs)

        mock_run.side_effect = side_effect
        args = SimpleNamespace(branch="main")

        with (
            patch(
                "hermes_cli.config.load_config",
                return_value={
                    "updates": {"parked_branch_strategy": "update_in_place"}
                },
            ),
            patch.object(update_cmd, "_record_update_success") as record,
        ):
            with pytest.raises(SystemExit) as exit_info:
                cmd_update(args)

        assert exit_info.value.code == 1
        record.assert_not_called()
        assert "could not be restored" in capsys.readouterr().out

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_no_update_refuses_receipt_when_stash_restore_is_not_clean(
        self, mock_run, _mock_which, capsys
    ):
        from hermes_cli import main as hm
        from hermes_cli import update_cmd

        mock_run.side_effect = self._branch_side_effect(
            current_branch="main", target_branch="main", commit_count="0"
        )
        args = SimpleNamespace(branch="main")

        with (
            patch.object(hm, "_stash_local_changes_if_needed", return_value="stash-ref"),
            patch.object(hm, "_restore_stashed_changes", return_value=False),
            patch.object(update_cmd, "_record_update_success") as record,
            pytest.raises(SystemExit) as exit_info,
        ):
            cmd_update(args)

        assert exit_info.value.code == 1
        record.assert_not_called()
        assert "no success receipt" in capsys.readouterr().out

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_no_update_identity_mismatch_exits_nonzero(
        self, mock_run, _mock_which, capsys
    ):
        from hermes_cli import main as hm
        from hermes_cli import update_cmd

        mock_run.side_effect = self._branch_side_effect(
            current_branch="main", target_branch="main", commit_count="0"
        )
        args = SimpleNamespace(branch="main")

        with (
            patch.object(update_cmd, "_is_fork", return_value=True),
            patch.object(hm, "_sync_with_upstream_if_needed"),
            patch.object(update_cmd, "_capture_head_sha", return_value="a" * 40),
            patch.object(
                update_cmd, "_refresh_update_target_sha", return_value="b" * 40
            ) as refresh,
            patch.object(update_cmd, "_record_update_success") as record,
            pytest.raises(SystemExit) as exit_info,
        ):
            cmd_update(args)

        assert exit_info.value.code == 1
        refresh.assert_called_once()
        refreshed_target = refresh.call_args.args[2]
        assert refreshed_target.remote == "origin"
        assert refreshed_target.refspec == (
            "+refs/heads/main:refs/remotes/origin/main"
        )
        record.assert_not_called()
        assert "identity could not be proven" in capsys.readouterr().out

    @pytest.mark.parametrize("initial_health", [False, None])
    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_no_update_repairs_and_reproves_node_dependencies(
        self,
        mock_run,
        _mock_which,
        initial_health,
        capsys,
    ):
        from hermes_cli import update_cmd

        installed_sha = "a" * 40
        base = self._branch_side_effect(
            current_branch="main", target_branch="main", commit_count="0"
        )

        def side_effect(command, **kwargs):
            joined = " ".join(str(value) for value in command)
            if "rev-parse" in joined and "--verify" in joined:
                return subprocess.CompletedProcess(
                    command, 0, stdout=f"{installed_sha}\n", stderr=""
                )
            return base(command, **kwargs)

        mock_run.side_effect = side_effect
        args = SimpleNamespace(branch="main")

        with (
            patch.object(
                update_cmd, "_venv_core_imports_healthy", return_value=(True, "")
            ),
            patch.object(
                update_cmd,
                "_validate_critical_files_syntax",
                return_value=(True, None, None),
            ),
            patch.object(
                update_cmd,
                "_validate_critical_modules_import",
                return_value=(True, None, None),
            ),
            patch.object(
                update_cmd,
                "_node_dependencies_healthy_read_only",
                side_effect=[initial_health, True],
            ) as prove_node,
            patch.object(
                update_cmd, "_update_node_dependencies", return_value=[]
            ) as repair_node,
            patch.object(
                update_cmd, "_capture_head_sha", return_value=installed_sha
            ),
            patch.object(update_cmd, "_record_update_success") as record,
        ):
            cmd_update(args)

        repair_node.assert_called_once_with()
        assert prove_node.call_count == 2
        record.assert_called_once()
        assert all(record.call_args.kwargs["health"].values())
        assert "Node dependencies repaired" in capsys.readouterr().out

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_no_update_unknown_import_health_exits_nonzero(
        self, mock_run, _mock_which, capsys
    ):
        from hermes_cli import update_cmd

        installed_sha = "a" * 40
        base = self._branch_side_effect(
            current_branch="main", target_branch="main", commit_count="0"
        )

        def side_effect(command, **kwargs):
            joined = " ".join(str(value) for value in command)
            if "rev-parse" in joined and "--verify" in joined:
                return subprocess.CompletedProcess(
                    command, 0, stdout=f"{installed_sha}\n", stderr=""
                )
            return base(command, **kwargs)

        mock_run.side_effect = side_effect
        args = SimpleNamespace(branch="main")

        with (
            patch.object(
                update_cmd, "_venv_core_imports_healthy", return_value=(True, "")
            ),
            patch.object(
                update_cmd,
                "_validate_critical_files_syntax",
                return_value=(True, None, None),
            ),
            patch.object(
                update_cmd,
                "_validate_critical_modules_import",
                return_value=(None, None, "probe timed out"),
            ),
            patch.object(
                update_cmd, "_node_dependencies_healthy_read_only", return_value=True
            ),
            patch.object(
                update_cmd, "_capture_head_sha", return_value=installed_sha
            ),
            patch.object(update_cmd, "_record_update_success") as record,
            pytest.raises(SystemExit) as exit_info,
        ):
            cmd_update(args)

        assert exit_info.value.code == 1
        record.assert_not_called()
        assert "health proof" in capsys.readouterr().out

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_updated_checkout_failed_import_health_exits_nonzero(
        self, mock_run, _mock_which, capsys
    ):
        from hermes_cli import update_cmd

        mock_run.side_effect = self._branch_side_effect(
            current_branch="main", target_branch="main", commit_count="1"
        )
        args = SimpleNamespace(branch="main")

        with (
            patch.object(
                update_cmd,
                "_validate_critical_files_syntax",
                return_value=(True, None, None),
            ),
            patch.object(
                update_cmd,
                "_validate_critical_modules_import",
                return_value=(False, "hermes_cli.main", "injected import failure"),
            ),
            patch.object(
                update_cmd, "_venv_core_imports_healthy", return_value=(True, "")
            ),
            patch.object(
                update_cmd, "_node_dependencies_healthy_read_only", return_value=True
            ),
            patch.object(update_cmd, "_record_update_success") as record,
            pytest.raises(SystemExit) as exit_info,
        ):
            cmd_update(args)

        assert exit_info.value.code == 1
        record.assert_not_called()
        assert "health proof" in capsys.readouterr().out

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_updated_checkout_failed_node_repair_exits_nonzero(
        self, mock_run, _mock_which, capsys
    ):
        from hermes_cli import update_cmd

        mock_run.side_effect = self._branch_side_effect(
            current_branch="main", target_branch="main", commit_count="1"
        )
        args = SimpleNamespace(branch="main")

        with (
            patch.object(
                update_cmd,
                "_validate_critical_files_syntax",
                return_value=(True, None, None),
            ),
            patch.object(
                update_cmd,
                "_validate_critical_modules_import",
                return_value=(True, None, None),
            ),
            patch.object(
                update_cmd, "_venv_core_imports_healthy", return_value=(True, "")
            ),
            patch.object(
                update_cmd, "_node_dependencies_healthy_read_only", return_value=False
            ),
            patch.object(
                update_cmd, "_update_node_dependencies", return_value=["repo root"]
            ),
            patch.object(update_cmd, "_record_update_success") as record,
            pytest.raises(SystemExit) as exit_info,
        ):
            cmd_update(args)

        assert exit_info.value.code == 1
        record.assert_not_called()
        assert "partially complete" in capsys.readouterr().out

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_updated_checkout_identity_mismatch_exits_nonzero(
        self, mock_run, _mock_which, capsys
    ):
        from hermes_cli import update_cmd

        target_sha = "a" * 40
        installed_sha = "b" * 40
        base = self._branch_side_effect(
            current_branch="main", target_branch="main", commit_count="1"
        )

        def side_effect(command, **kwargs):
            joined = " ".join(str(value) for value in command)
            if "rev-parse" in joined and "--verify" in joined:
                return subprocess.CompletedProcess(
                    command, 0, stdout=f"{target_sha}\n", stderr=""
                )
            return base(command, **kwargs)

        mock_run.side_effect = side_effect
        args = SimpleNamespace(branch="main")

        with (
            patch.object(
                update_cmd,
                "_validate_critical_files_syntax",
                return_value=(True, None, None),
            ),
            patch.object(
                update_cmd,
                "_validate_critical_modules_import",
                return_value=(True, None, None),
            ),
            patch.object(
                update_cmd, "_venv_core_imports_healthy", return_value=(True, "")
            ),
            patch.object(
                update_cmd, "_node_dependencies_healthy_read_only", return_value=True
            ),
            patch.object(
                update_cmd,
                "_capture_head_sha",
                side_effect=["0" * 40, installed_sha, installed_sha],
            ),
            patch.object(update_cmd, "_record_update_success") as record,
            pytest.raises(SystemExit) as exit_info,
        ):
            cmd_update(args)

        assert exit_info.value.code == 1
        record.assert_not_called()
        assert "identity could not be proven" in capsys.readouterr().out

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_fork_sync_that_advances_head_runs_full_update_pipeline(
        self, mock_run, _mock_which
    ):
        from hermes_cli import main as hm
        from hermes_cli import update_cmd

        before_sha = "a" * 40
        after_sha = "b" * 40
        mock_run.side_effect = self._branch_side_effect(
            current_branch="main", target_branch="main", commit_count="0"
        )
        args = SimpleNamespace(branch="main")

        with (
            patch.object(update_cmd, "_is_fork", return_value=True),
            patch.object(hm, "_sync_with_upstream_if_needed") as sync,
            patch.object(
                update_cmd,
                "_capture_head_sha",
                side_effect=[before_sha, after_sha, after_sha, after_sha],
            ),
            patch.object(
                update_cmd, "_refresh_update_target_sha", return_value=after_sha
            ),
            patch.object(
                update_cmd,
                "_validate_critical_files_syntax",
                return_value=(True, None, None),
            ),
            patch.object(
                update_cmd,
                "_validate_critical_modules_import",
                return_value=(True, None, None),
            ),
            patch.object(
                update_cmd, "_venv_core_imports_healthy", return_value=(True, "")
            ),
            patch.object(
                update_cmd, "_node_dependencies_healthy_read_only", return_value=True
            ),
            patch.object(
                update_cmd, "_editable_install_is_current", return_value=False
            ) as editable,
            patch.object(update_cmd, "_record_update_success") as record,
        ):
            cmd_update(args)

        commands = [
            " ".join(str(value) for value in call.args[0])
            for call in mock_run.call_args_list
        ]
        assert any(
            "merge --ff-only refs/remotes/origin/main" in command
            for command in commands
        )
        sync.assert_called_once()
        assert editable.call_args.args[2] == before_sha
        record.assert_called_once()

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_unsafe_local_git_driver_refuses_before_worktree_cleanup(
        self, mock_run, _mock_which, capsys
    ):
        from hermes_cli import update_cmd

        mock_run.side_effect = self._branch_side_effect(
            current_branch="main", target_branch="main", commit_count="0"
        )
        args = SimpleNamespace(branch="main")

        with (
            patch.object(
                update_cmd,
                "_assert_safe_git_configuration",
                side_effect=RuntimeError("executable filter driver"),
            ),
            patch.object(update_cmd, "_discard_lockfile_churn") as discard,
            pytest.raises(SystemExit) as exit_info,
        ):
            cmd_update(args)

        assert exit_info.value.code == 1
        discard.assert_not_called()
        assert "Unsafe Git configuration" in capsys.readouterr().out


class TestCmdUpdateCheckBranchFlag:
    """``hermes update --check --branch <name>`` honors the branch override.

    The check path used to call ``git rev-list HEAD..origin/<branch> --count``
    with ``check=True``. When the branch didn't exist on origin, the fetch
    silently succeeded (no refspec) but rev-list exited 128 and a raw
    ``CalledProcessError`` propagated to the user. These tests pin the
    friendlier behavior: detect-the-missing-ref before rev-list, exit 1
    with a clear message.
    """

    def _check_side_effect(
        self,
        target_branch: str,
        *,
        verify_ok: bool = True,
        commit_count: str = "0",
        upstream_fetch_ok: bool = True,
    ):
        """Mock side-effect for the _cmd_update_check git pipeline.

        - ``target_branch``      what we expect compare ref to point at
        - ``verify_ok``          if False, ``git rev-parse --verify --quiet
                                 origin/<branch>`` fails (branch missing
                                 on origin)
        - ``commit_count``       rev-list count (0 = up-to-date)
        - ``upstream_fetch_ok``  if False, ``git fetch upstream`` fails
                                 (forces fallback to origin on branch==main)
        """

        def side_effect(cmd, **kwargs):
            joined = " ".join(str(c) for c in cmd)

            if "fetch" in joined and "upstream" in joined:
                rc = 0 if upstream_fetch_ok else 128
                err = "" if upstream_fetch_ok else "fatal: 'upstream' does not appear to be a git repository\n"
                return subprocess.CompletedProcess(cmd, rc, stdout="", stderr=err)

            if "fetch" in joined and "origin" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            if "rev-parse" in joined and "--verify" in joined:
                rc = 0 if verify_ok else 1
                return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")

            if "rev-list" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{commit_count}\n", stderr="")

            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        return side_effect

    @patch("hermes_cli.config.detect_install_method", return_value="git")
    @patch("subprocess.run")
    def test_check_branch_compares_against_named_origin_branch(
        self, mock_run, _mock_method, capsys
    ):
        """--check --branch bb/gui compares against origin/bb/gui, never origin/main."""
        mock_run.side_effect = self._check_side_effect(
            target_branch="bb/gui", verify_ok=True, commit_count="2"
        )
        args = SimpleNamespace(check=True, branch="bb/gui")

        cmd_update(args)

        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]
        # Non-main branch skips upstream probe entirely.
        assert not any("fetch" in c and "upstream" in c for c in commands), commands
        # Verify and rev-list both target origin/bb/gui.
        verify_cmds = [c for c in commands if "rev-parse" in c and "--verify" in c]
        assert any("origin/bb/gui" in c for c in verify_cmds), verify_cmds
        rev_list_cmds = [c for c in commands if "rev-list" in c]
        assert any("origin/bb/gui" in c for c in rev_list_cmds), rev_list_cmds
        assert not any("origin/main" in c for c in rev_list_cmds), rev_list_cmds

    @patch("hermes_cli.config.detect_install_method", return_value="git")
    @patch("subprocess.run")
    def test_check_branch_missing_on_origin_exits_cleanly(
        self, mock_run, _mock_method, capsys
    ):
        """If origin/<branch> doesn't exist, surface a friendly error and exit 1.

        Pre-fix this case raised CalledProcessError from rev-list's check=True
        and dumped a Python traceback to stdout.
        """
        mock_run.side_effect = self._check_side_effect(
            target_branch="ghost", verify_ok=False
        )
        args = SimpleNamespace(check=True, branch="ghost")

        with pytest.raises(SystemExit) as exc_info:
            cmd_update(args)
        assert exc_info.value.code == 1

        out = capsys.readouterr().out
        # No raw Python traceback.
        assert "Traceback" not in out
        assert "CalledProcessError" not in out
        # Friendly message naming the branch.
        assert "ghost" in out
        assert "not found" in out

        # rev-list must never have been called once verify failed.
        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]
        assert not any("rev-list" in c for c in commands), commands

    @patch("hermes_cli.config.detect_install_method", return_value="git")
    @patch("subprocess.run")
    def test_check_default_main_uses_configured_tracking_remote(
        self, mock_run, _mock_method, capsys
    ):
        """No --branch uses the branch's configured tracking remote."""
        mock_run.side_effect = self._check_side_effect(
            target_branch="main", verify_ok=True, commit_count="0"
        )
        args = SimpleNamespace(check=True, branch=None)

        cmd_update(args)

        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]
        # Fetch and compare must use the same configured origin tracking ref.
        assert any(
            "fetch" in c
            and "origin" in c
            and "+refs/heads/main:refs/remotes/origin/main" in c
            for c in commands
        ), commands
        rev_list_cmds = [c for c in commands if "rev-list" in c]
        assert any("refs/remotes/origin/main" in c for c in rev_list_cmds), rev_list_cmds


class TestCmdUpdateZipBranchRefusal:
    """``hermes update --branch=<non-main>`` must refuse on the ZIP fallback path.

    The ZIP fallback hard-codes a GitHub archive URL for main.zip; honoring
    --branch arbitrarily would require remote-branch existence checks the
    fallback can't easily do. Refusing is the right move — silently lying
    about which branch got installed is the bug --branch was meant to prevent.
    """

    def test_zip_fallback_refuses_non_main_branch(self, capsys):
        from hermes_cli.main import _update_via_zip

        args = SimpleNamespace(branch="bb/gui")
        with pytest.raises(SystemExit) as exc_info:
            _update_via_zip(args, transaction=_UpdateTransaction())
        assert exc_info.value.code == 1

        out = capsys.readouterr().out
        assert "bb/gui" in out
        assert "not supported" in out
        # No actual download attempted.
        assert "Downloading latest version" not in out


def test_is_termux_env_true_for_termux_prefix():
    from hermes_cli import main as hm

    assert hm._is_termux_env({"PREFIX": "/data/data/com.termux/files/usr"}) is True


def test_load_installable_optional_extras_supports_termux_group(tmp_path, monkeypatch):
    from hermes_cli import main as hm

    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[project]
name = "x"
version = "0.0.0"

[project.optional-dependencies]
all = ["x[mcp]"]
termux-all = ["x[termux]", "x[mcp]"]
mcp = ["mcp>=1"]
termux = ["rich>=14"]
""".strip()
    )
    monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)

    assert hm._load_installable_optional_extras(group="all") == ["mcp"]
    assert hm._load_installable_optional_extras(group="termux-all") == ["termux", "mcp"]


class TestNodeRuntimeNpmResolution:
    """Regression tests for #30271 — WSL must not run Windows npm against the
    Linux checkout, and a failed Node refresh must not report success."""






    def test_node_failure_returns_failed_labels_and_warns(
        self, tmp_path, monkeypatch, capsys
    ):
        from hermes_cli import main as hm

        (tmp_path / "package.json").write_text("{}")
        monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(hm, "_resolve_node_runtime_npm", lambda: "/usr/bin/npm")
        monkeypatch.setattr(
            hm,
            "_run_npm_install_deterministic",
            lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr=""),
        )

        with patch(
            "tools.browser_tool.warm_agent_browser_npx_cache", return_value=True
        ):
            failed = hm._update_node_dependencies()
        assert failed == ["ui-tui, web workspaces"]
        out = capsys.readouterr().out
        assert "mixed state" in out

    @pytest.mark.linux_only
    def test_wsl_update_skips_windows_npm_build_paths(self, mock_args, monkeypatch):
        """A Windows-only npm on WSL must not reach web or desktop builds."""
        from hermes_cli import main as hm
        import hermes_constants

        windows_npm = "/mnt/c/Program Files/nodejs/npm"
        monkeypatch.setattr(hermes_constants, "is_wsl", lambda: True)
        monkeypatch.setattr(
            hermes_constants,
            "find_node_executable",
            lambda command: windows_npm if command == "npm" else None,
        )
        monkeypatch.setattr(
            hm.shutil,
            "which",
            lambda command, path=None: windows_npm if command == "npm" else "/usr/bin/uv",
        )
        monkeypatch.setenv("PATH", "/mnt/c/Program Files/nodejs")

        with patch("subprocess.run") as mock_run, \
             patch.object(hm, "_web_ui_build_needed", return_value=True), \
             patch.object(hm, "_desktop_packaged_executable", return_value=None), \
             patch.object(hm, "_desktop_dist_exists", return_value=True), \
             patch.object(hm, "_run_npm_install_deterministic") as mock_npm_install, \
             patch.object(hm, "_run_with_idle_timeout") as mock_idle_build, \
             patch.object(hm, "_run_logged_subprocess") as mock_desktop_build:
            mock_run.side_effect = _make_run_side_effect(
                branch="main", verify_ok=True, commit_count="1"
            )
            cmd_update(mock_args)

        mock_npm_install.assert_not_called()
        mock_idle_build.assert_not_called()
        mock_desktop_build.assert_not_called()
        assert all(
            not call.args or not call.args[0] or call.args[0][0] != windows_npm
            for call in mock_run.call_args_list
        )

    def test_update_rebuilds_desktop_that_disappears_mid_update(self):
        """A previously packaged Desktop must be rebuilt when its release tree vanishes."""
        from hermes_cli import main as hm
        from hermes_cli import update_cmd

        desktop_dir = PROJECT_ROOT / "apps" / "desktop"
        packaged_exe = desktop_dir / "release" / "win-unpacked" / "Hermes.exe"
        build_ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")

        with (
            patch.object(
                hm, "_desktop_packaged_executable", side_effect=[packaged_exe, None]
            ) as packaged,
            patch.object(hm, "_desktop_dist_exists", return_value=False),
            patch.object(hm, "_resolve_node_runtime_npm", return_value="npm.cmd"),
            patch.object(hm, "_desktop_build_needed", return_value=True),
            patch.object(hm, "_run_logged_subprocess", return_value=build_ok) as desktop_build,
        ):
            had_desktop_app_before_update = update_cmd._desktop_app_present(desktop_dir)
            assert not update_cmd._desktop_app_present(desktop_dir)
            update_cmd._rebuild_desktop_after_update(
                desktop_dir,
                had_desktop_app_before_update=had_desktop_app_before_update,
            )

        assert packaged.call_count == 2
        desktop_build.assert_called_once_with(
            [hm.sys.executable, "-m", "hermes_cli.main", "desktop", "--build-only"],
            cwd=PROJECT_ROOT,
            env=ANY,
        )

    def test_git_failure_zip_fallback_rebuilds_missing_desktop(self, tmp_path, monkeypatch):
        """The Windows ZIP fallback keeps Desktop intact when replacing ``apps/``.

        Contract updated for the #70337/#87331 release-dir graft: the built
        desktop app (release/win-unpacked/Hermes.exe) is preserved THROUGH
        the swap — previously this test pinned the old repair shape (exe
        deleted by the swap, then rebuilt from scratch). The rebuild hook
        still runs (mocked _desktop_build_needed=True), but it now finds
        the packaged exe alive rather than missing.
        """
        import zipfile

        from hermes_cli import main as hm
        from hermes_cli import update_cmd

        project_root = tmp_path / "hermes-agent"
        (project_root / ".git").mkdir(parents=True)
        desktop_dir = project_root / "apps" / "desktop"
        packaged_exe = desktop_dir / "release" / "win-unpacked" / "Hermes.exe"
        packaged_exe.parent.mkdir(parents=True)
        packaged_exe.write_bytes(b"desktop")

        def write_source_zip(_url, destination):
            with zipfile.ZipFile(destination, "w") as archive:
                archive.writestr("hermes-agent-main/apps/desktop/package.json", "{}")

        def fail_git_fetch(command, **_kwargs):
            if "fetch" in command:
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        desktop_builds = []

        def rebuild_desktop(*_args, **_kwargs):
            desktop_builds.append(not packaged_exe.exists())
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        monkeypatch.setattr(hm, "PROJECT_ROOT", project_root)
        monkeypatch.setattr(hm, "_is_windows", lambda: True)
        monkeypatch.setattr(hm, "_run_pre_update_backup", lambda _args: None)
        monkeypatch.setattr(
            hm,
            "_pause_windows_gateways_for_update",
            lambda **_kwargs: None,
        )
        monkeypatch.setattr(hm, "_get_origin_url", lambda *_args: "")
        monkeypatch.setattr(
            hm,
            "_desktop_packaged_executable",
            lambda _desktop_dir: packaged_exe if packaged_exe.exists() else None,
        )
        monkeypatch.setattr(hm, "_desktop_dist_exists", lambda _desktop_dir: False)
        monkeypatch.setattr(hm, "_resolve_node_runtime_npm", lambda: "npm.cmd")
        monkeypatch.setattr(hm, "_desktop_build_needed", lambda *_args, **_kwargs: True)
        monkeypatch.setattr(hm, "_run_logged_subprocess", rebuild_desktop)
        monkeypatch.setattr(hm, "_clear_bytecode_cache", lambda *_args: 0)
        monkeypatch.setattr(hm, "_record_bytecode_fingerprint", lambda: None)
        monkeypatch.setattr(hm, "_refresh_bootstrap_cache_scripts", lambda _branch: None)
        monkeypatch.setattr(
            hm, "_install_python_dependencies_with_optional_fallback", lambda *_args, **_kwargs: None
        )
        monkeypatch.setattr(hm, "_refresh_active_memory_provider_dependencies", lambda: None)
        monkeypatch.setattr(hm, "_build_web_ui", lambda *_args: None)
        monkeypatch.setattr(update_cmd, "_discard_lockfile_churn", lambda *_args: None)
        monkeypatch.setattr(update_cmd, "_normalize_managed_eol", lambda *_args: None)
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
        monkeypatch.setattr(
            update_cmd,
            "_venv_core_imports_healthy",
            lambda: (True, ""),
        )
        monkeypatch.setattr(
            update_cmd,
            "_node_dependencies_healthy_read_only",
            lambda: True,
        )
        monkeypatch.setattr(update_cmd, "_update_node_dependencies", lambda: [])
        monkeypatch.setattr(update_cmd, "_print_curator_first_run_notice", lambda: None)
        monkeypatch.setattr(update_cmd, "_print_curator_recent_run_notice", lambda: None)
        monkeypatch.setattr(update_cmd, "_finish_dashboard_update_cleanup", lambda _failures: None)
        monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: tmp_path / "hermes-home")

        with (
            patch("hermes_cli.config.load_config", return_value={}),
            patch("subprocess.run", side_effect=fail_git_fetch),
            patch("urllib.request.urlretrieve", side_effect=write_source_zip),
            patch("hermes_cli.managed_uv.ensure_uv", return_value="uv"),
            patch("hermes_cli.managed_uv.update_managed_uv"),
            patch(
                "tools.skills_sync.sync_skills",
                return_value={
                    "copied": [],
                    "updated": [],
                    "user_modified": [],
                    "cleaned": [],
                    "relocated": [],
                },
            ),
            patch("hermes_cli.model_catalog.seed_cache_from_checkout", return_value=False),
        ):
            update_cmd._cmd_update_impl(
                SimpleNamespace(yes=True, force=True, force_venv=True, branch=None),
                gateway_mode=False,
                transaction=_UpdateTransaction(),
            )

        # Release-dir graft (#70337): the packaged exe SURVIVES the swap, so
        # the rebuild hook observed it present (False), and the bytes are the
        # original build — never deleted, never rebuilt from nothing.
        assert desktop_builds == [False]
        assert packaged_exe.exists()
        assert packaged_exe.read_bytes() == b"desktop"


class TestUpdateNodeDependencies:
    """Unit tests for _update_node_dependencies — issue #43564.

    Root package.json has no dependencies of its own: agent-browser
    resolves at runtime via npx (tools/browser_tool.py), and @streamdown/math
    moved to apps/desktop/package.json since it's a desktop-only import.
    With nothing root-only left to protect, a single workspace-scoped
    install (ui-tui, web) is safe — apps/desktop is simply never named, so
    its ~200 MB Electron devDependency is never resolved. Skipping is
    governed by _npm_lockfile_changed (content hash over the lockfile +
    every workspace package.json), tested separately in
    TestNpmLockfileChanged.
    Uses a tmp_path root so tests never touch real node_modules.
    """

    @pytest.fixture(autouse=True)
    def _stub_npx_warmup(self, monkeypatch):
        """The npx cache warm-up is covered by its own dedicated test below;
        stub it out everywhere else so it doesn't add a spurious npm/npx
        call to the workspace-install assertions in this class."""
        from hermes_cli import main as hm

        monkeypatch.setattr(hm, "_resolve_node_runtime_npm", lambda: "/usr/bin/npm")

        with patch(
            "tools.browser_tool.warm_agent_browser_npx_cache", return_value=True
        ):
            yield

    def _npm_calls(self, mock_run):
        return [
            call.args[0]
            for call in mock_run.call_args_list
            if call.args and "npm" in str(call.args[0][0])
        ]

    def _make_popen(self, calls, returncode=0, stderr_lines=()):
        """Fake subprocess.Popen recording each invocation's cmd/kwargs.

        _update_node_dependencies always runs npm with capture_output=False,
        which routes through the Popen-based stderr-teeing path in
        _run_npm_watching_for_engine_failure rather than subprocess.run.
        """

        class _FakeProc:
            def __init__(self, cmd, **kwargs):
                calls.append({"cmd": cmd, "kwargs": kwargs})
                self.stderr = iter(stderr_lines)

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

            def wait(self):
                return returncode

        return _FakeProc

    def _popen_npm_calls(self, calls):
        return [c["cmd"] for c in calls if c["cmd"] and "npm" in str(c["cmd"][0])]

    @patch("subprocess.Popen")
    def test_install_names_ui_tui_and_web_workspaces(self, mock_popen, tmp_path, monkeypatch):
        """Regression for #43564: install ui-tui + web directly. apps/desktop
        must never appear, so its Electron postinstall is never triggered.
        """
        from hermes_cli import main as hm

        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "package-lock.json").write_text("{}")
        monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(hm, "_npm_lockfile_changed", lambda root: True)
        popen_calls = []
        mock_popen.side_effect = self._make_popen(popen_calls)

        hm._update_node_dependencies()

        calls = self._popen_npm_calls(popen_calls)
        assert len(calls) == 1, f"expected exactly 1 npm call, got: {calls}"
        joined = " ".join(str(a) for a in calls[0])
        assert "--workspace ui-tui" in joined and "--workspace web" in joined, (
            f"expected ui-tui + web workspace selectors; actual: {calls[0]}"
        )
        assert "desktop" not in joined, (
            f"apps/desktop must not appear (avoids ~200 MB Electron download); actual: {calls[0]}"
        )
        assert "--workspaces=false" not in joined, (
            f"no root-only deps remain to protect; --workspaces=false is unnecessary now; actual: {calls[0]}"
        )

    @patch("subprocess.Popen")
    def test_install_includes_workspace_root_to_protect_root_devdependencies(
        self, mock_popen, tmp_path, monkeypatch
    ):
        """Root package.json still owns devDependencies (the shared ESLint
        flat config every workspace's own eslint.config.mjs imports) even
        though agent-browser and @streamdown/math were removed from root
        `dependencies` (#43564). --include-workspace-root keeps them from
        being pruned by this scoped install, while --workspace ui-tui
        --workspace web still excludes the unnamed apps/desktop workspace
        (confirmed empirically against npm 10.9.8 and 11.9.0 in PR #44772
        review)."""
        from hermes_cli import main as hm

        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "package-lock.json").write_text("{}")
        monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(hm, "_npm_lockfile_changed", lambda root: True)
        popen_calls = []
        mock_popen.side_effect = self._make_popen(popen_calls)

        hm._update_node_dependencies()

        calls = self._popen_npm_calls(popen_calls)
        assert len(calls) == 1
        joined = " ".join(str(a) for a in calls[0])
        assert "--include-workspace-root" in joined
        assert "desktop" not in joined

    @patch("subprocess.Popen")
    def test_install_preserves_standard_flags(self, mock_popen, tmp_path, monkeypatch):
        """--no-fund, --no-audit, --progress=false must survive."""
        from hermes_cli import main as hm

        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "package-lock.json").write_text("{}")
        monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(hm, "_npm_lockfile_changed", lambda root: True)
        popen_calls = []
        mock_popen.side_effect = self._make_popen(popen_calls)

        hm._update_node_dependencies()

        calls = self._popen_npm_calls(popen_calls)
        assert len(calls) == 1
        joined = " ".join(str(a) for a in calls[0])
        for flag in ("--no-fund", "--no-audit", "--progress=false"):
            assert flag in joined, f"{flag} missing from npm call; actual: {calls[0]}"

    @patch("subprocess.run")
    def test_skips_install_when_deps_up_to_date(self, mock_run, tmp_path, monkeypatch):
        """When _npm_lockfile_changed reports no change, npm must not be called."""
        from hermes_cli import main as hm

        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "package-lock.json").write_text("{}")
        monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)
        with patch.object(hm, "_npm_lockfile_changed", return_value=False) as lock_changed:
            hm._update_node_dependencies()

        lock_changed.assert_called_once()
        assert not self._npm_calls(mock_run), (
            "npm must not run when _npm_lockfile_changed reports no change"
        )

    @patch("subprocess.Popen")
    def test_runs_install_when_lockfile_changed(self, mock_popen, tmp_path, monkeypatch):
        """When _npm_lockfile_changed reports a change, npm must run."""
        from hermes_cli import main as hm

        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "package-lock.json").write_text("{}")
        monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(hm, "_npm_lockfile_changed", lambda root: True)
        popen_calls = []
        mock_popen.side_effect = self._make_popen(popen_calls)

        hm._update_node_dependencies()

        calls = self._popen_npm_calls(popen_calls)
        assert len(calls) == 1, f"expected npm to run when lockfile changed; got: {calls}"

    @patch("subprocess.Popen")
    def test_records_lockfile_hash_only_on_success(self, mock_popen, tmp_path, monkeypatch):
        """A failed install must not record the lockfile hash (so the next
        run retries instead of wrongly believing deps are up to date)."""
        from hermes_cli import main as hm

        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "package-lock.json").write_text("{}")
        monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(hm, "_npm_lockfile_changed", lambda root: True)
        recorded = []
        monkeypatch.setattr(hm, "_record_npm_lockfile_hash", lambda root: recorded.append(root))
        popen_calls = []
        mock_popen.side_effect = self._make_popen(
            popen_calls, returncode=1, stderr_lines=["npm ERR!\n"]
        )

        hm._update_node_dependencies()

        assert self._popen_npm_calls(popen_calls), (
            "expected npm install to be attempted before checking failed-install cache behavior"
        )
        assert not recorded, "lockfile hash must not be recorded when npm install fails"

    @patch("subprocess.Popen")
    def test_warms_npx_agent_browser_cache_regardless_of_install_result(
        self, mock_popen, tmp_path, monkeypatch
    ):
        """The npx warm-up must fire even when the workspace install fails —
        it's independent of ui-tui/web dependency state (#43564)."""
        from hermes_cli import main as hm

        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "package-lock.json").write_text("{}")
        monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(hm, "_npm_lockfile_changed", lambda root: True)
        popen_calls = []
        mock_popen.side_effect = self._make_popen(
            popen_calls, returncode=1, stderr_lines=["npm ERR!\n"]
        )

        with patch(
            "tools.browser_tool.warm_agent_browser_npx_cache", return_value=True
        ) as mock_warm:
            hm._update_node_dependencies()

        assert self._popen_npm_calls(popen_calls), (
            "expected a failed npm install attempt while verifying cache warm-up"
        )
        mock_warm.assert_called_once()

    @patch("subprocess.run")
    def test_returns_silently_when_npm_not_found(self, mock_run, tmp_path, monkeypatch):
        """No npm on PATH → return without calling subprocess."""
        from hermes_cli import main as hm

        (tmp_path / "package.json").write_text("{}")
        monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(hm, "_resolve_node_runtime_npm", lambda: None)

        hm._update_node_dependencies()

        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_returns_silently_when_package_json_absent(self, mock_run, tmp_path, monkeypatch):
        """No package.json → return without calling npm."""
        from hermes_cli import main as hm

        monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)

        hm._update_node_dependencies()

        mock_run.assert_not_called()

    @patch("subprocess.Popen")
    def test_install_runs_from_project_root(self, mock_popen, tmp_path, monkeypatch):
        """npm install must execute from PROJECT_ROOT, not a workspace subdir."""
        from hermes_cli import main as hm

        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "package-lock.json").write_text("{}")
        monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)

        popen_calls = []
        mock_popen.side_effect = self._make_popen(popen_calls)

        hm._update_node_dependencies()

        cwd_calls = [
            c["kwargs"].get("cwd")
            for c in popen_calls
            if c["cmd"] and "npm" in str(c["cmd"][0])
        ]
        assert cwd_calls, "expected at least one npm call"
        for cwd in cwd_calls:
            assert cwd == tmp_path, f"npm must run from PROJECT_ROOT; got cwd={cwd}"
