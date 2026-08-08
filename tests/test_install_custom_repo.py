"""Behavior tests for installer repository overrides.

An explicit repository URL is authoritative only after the requested branch
has been fetched successfully.  A typo, unavailable repository, or missing
branch must not poison an existing managed checkout's ``origin`` or branch
tracking configuration.  Once validation succeeds, branch installs follow
``origin/<branch>``; detached commit pins deliberately keep the branch's prior
tracking configuration untouched.

The tests run the real repository stage against tiny local bare repositories.
Both installer implementations are exercised when their host shell exists.
"""

from __future__ import annotations

import base64
import os
import shlex
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"
TARGET_BRANCH = "fork-integration"
INSTALL_TAG = "installer-authority-tag"


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if check and result.returncode != 0:
        pytest.fail(
            f"command failed ({result.returncode}): {command!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _git(cwd: Path, *args: str) -> str:
    return _run(
        [
            "git",
            "-c",
            "user.email=installer-test@example.invalid",
            "-c",
            "user.name=Installer Test",
            "-c",
            "safe.directory=*",
            *args,
        ],
        cwd=cwd,
    ).stdout.strip()


@dataclass(frozen=True)
class RemoteRepo:
    path: Path
    branch_sha: str
    ancestor_sha: str
    pin_sha: str
    tag_sha: str | None


def _make_remote(
    tmp_path: Path, name: str, marker: str, *, include_tag: bool = True
) -> RemoteRepo:
    work = tmp_path / f"{name}-work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "marker.txt").write_text(f"{marker}-main\n", encoding="utf-8")
    _git(work, "add", "marker.txt")
    _git(work, "commit", "-qm", f"{marker} main")
    ancestor_sha = _git(work, "rev-parse", "HEAD")
    _git(work, "checkout", "-qb", TARGET_BRANCH)
    (work / "marker.txt").write_text(f"{marker}-branch\n", encoding="utf-8")
    _git(work, "commit", "-qam", f"{marker} branch")
    branch_sha = _git(work, "rev-parse", "HEAD")
    _git(work, "checkout", "-q", "--orphan", "commit-pin")
    (work / "marker.txt").write_text(f"{marker}-pin\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", f"{marker} disjoint pin")
    pin_sha = _git(work, "rev-parse", "HEAD")
    if include_tag:
        _git(work, "tag", INSTALL_TAG)
    _git(work, "checkout", "-q", TARGET_BRANCH)

    bare = tmp_path / f"{name}.git"
    _run(["git", "clone", "-q", "--bare", str(work), str(bare)], cwd=tmp_path)
    return RemoteRepo(
        path=bare,
        branch_sha=branch_sha,
        ancestor_sha=ancestor_sha,
        pin_sha=pin_sha,
        tag_sha=pin_sha if include_tag else None,
    )


def _find_bash() -> tuple[str, bool] | None:
    if os.name == "nt":
        # install.sh deliberately rejects Git Bash/MSYS on Windows. Exercise
        # its real Linux path through WSL instead. The host's login profile
        # switches to fish, so actual test commands use the base64 temp-script
        # bridge built in ``run_repository_stage`` below.
        candidate = shutil.which("wsl") or shutil.which("wsl.exe")
        if candidate:
            try:
                probe = subprocess.run(
                    [candidate, "--", "bash", "-c", "printf wsl-bash-ready"],
                    capture_output=True,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired):
                return None
            if probe.returncode == 0 and "wsl-bash-ready" in probe.stdout:
                return candidate, True
        return None

    candidate = shutil.which("bash")
    if candidate:
        try:
            probe = subprocess.run(
                [candidate, "--version"],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if probe.returncode == 0 and "GNU bash" in probe.stdout:
            return candidate, False
    return None


def _find_powershell() -> str | None:
    if os.name != "nt":
        return None
    candidates: list[str | None] = [shutil.which("pwsh"), shutil.which("powershell")]
    candidates.extend(
        [
            str(
                Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
                / "PowerShell"
                / "7"
                / "pwsh.exe"
            ),
            str(
                Path(os.environ.get("SystemRoot", r"C:\Windows"))
                / "System32"
                / "WindowsPowerShell"
                / "v1.0"
                / "powershell.exe"
            ),
        ]
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


@dataclass(frozen=True)
class Installer:
    name: str
    executable: str
    via_wsl: bool = False

    def _path(self, path: Path) -> str:
        if self.via_wsl:
            resolved = path.resolve()
            drive = resolved.drive.rstrip(":").lower()
            tail = resolved.as_posix()[len(resolved.drive) :].lstrip("/")
            return f"/mnt/{drive}/{tail}"
        return str(path.resolve())

    def run_repository_stage(
        self,
        *,
        tmp_path: Path,
        install_dir: Path,
        repo_url: Path,
        branch: str = TARGET_BRANCH,
        commit: str | None = None,
        tag: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        hermes_home = tmp_path / f"home-{self.name}"
        hermes_home.mkdir(exist_ok=True)
        isolated_home = tmp_path / f"profile-{self.name}"
        isolated_home.mkdir(exist_ok=True)
        global_config = tmp_path / f"gitconfig-{self.name}"

        env = os.environ.copy()
        env.update(
            {
                "GIT_CONFIG_GLOBAL": str(global_config),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "HOME": str(isolated_home),
                "USERPROFILE": str(isolated_home),
                "LOCALAPPDATA": str(isolated_home / "AppData" / "Local"),
                "HERMES_HOME": str(hermes_home),
            }
        )

        if self.name == "bash":
            bash_command = [
                "bash",
                self._path(INSTALL_SH),
                "--stage",
                "repository",
                "--json",
                "--non-interactive",
                "--dir",
                self._path(install_dir),
                "--hermes-home",
                self._path(hermes_home),
                "--repo-url",
                self._path(repo_url),
                "--branch",
                branch,
            ]
            if commit:
                bash_command.extend(["--commit", commit])
            if tag:
                raise ValueError("install.sh does not expose a tag option")
            if self.via_wsl:
                wsl_env = [
                    f"GIT_CONFIG_GLOBAL={self._path(global_config)}",
                    "GIT_CONFIG_NOSYSTEM=1",
                    "GIT_TERMINAL_PROMPT=0",
                    f"HOME={self._path(isolated_home)}",
                    f"HERMES_HOME={self._path(hermes_home)}",
                ]
                script = shlex.join(["env", *wsl_env, *bash_command])
                body = 'trap \'rm -f "$0"\' EXIT\n' + script
                encoded = base64.b64encode(body.encode("utf-8")).decode("ascii")
                remote_script = f"/tmp/hermes-installer-test-{uuid.uuid4().hex}.sh"
                runner = (
                    f"printf %s {shlex.quote(encoded)} | base64 -d > "
                    f"{shlex.quote(remote_script)}; bash {shlex.quote(remote_script)}"
                )
                command = [self.executable, "--", "bash", "-c", runner]
            else:
                command = [self.executable, str(INSTALL_SH), *bash_command[2:]]
        else:
            command = [
                self.executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(INSTALL_PS1),
                "-Stage",
                "repository",
                "-Json",
                "-NonInteractive",
                "-InstallDir",
                str(install_dir),
                "-HermesHome",
                str(hermes_home),
                "-RepoUrl",
                str(repo_url),
                "-Branch",
                branch,
            ]
            if commit:
                command.extend(["-Commit", commit])
            if tag:
                command.extend(["-Tag", tag])

        return _run(command, cwd=tmp_path, env=env, check=check)


@pytest.fixture(
    scope="module",
    params=("bash", pytest.param("powershell", marks=pytest.mark.windows_only))
)
def installer(request: pytest.FixtureRequest) -> Installer:
    if request.param == "bash":
        bash_host = _find_bash()
        if bash_host is None:
            pytest.skip("bash host is unavailable")
        executable, via_wsl = bash_host
        return Installer(name="bash", executable=executable, via_wsl=via_wsl)

    executable = _find_powershell()
    if executable is None:
        pytest.skip(f"{request.param} host is unavailable")
    return Installer(name=request.param, executable=executable)


@pytest.fixture
def powershell_installer() -> Installer:
    executable = _find_powershell()
    if executable is None:
        pytest.skip("PowerShell host is unavailable")
    return Installer(name="powershell", executable=executable)


@pytest.fixture
def remotes(tmp_path: Path) -> tuple[RemoteRepo, RemoteRepo]:
    return (
        _make_remote(tmp_path, "prior", "prior"),
        _make_remote(tmp_path, "override", "override"),
    )


def _make_tag_only_remote(tmp_path: Path) -> Path:
    """Make a repository with a tag, but no branch, named TARGET_BRANCH."""
    work = tmp_path / "tag-only-work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "marker.txt").write_text("tag only\n", encoding="utf-8")
    _git(work, "add", "marker.txt")
    _git(work, "commit", "-qm", "tag only")
    _git(work, "tag", TARGET_BRANCH)
    bare = tmp_path / "tag-only.git"
    _run(["git", "clone", "-q", "--bare", str(work), str(bare)], cwd=tmp_path)
    return bare


def _clone(remote: RemoteRepo, install_dir: Path) -> None:
    _run(
        [
            "git",
            "-c",
            "core.autocrlf=false",
            "clone",
            "-q",
            "--branch",
            TARGET_BRANCH,
            str(remote.path),
            str(install_dir),
        ],
        cwd=install_dir.parent,
    )


def _clone_main_only(remote: RemoteRepo, install_dir: Path) -> None:
    _run(
        [
            "git",
            "-c",
            "core.autocrlf=false",
            "clone",
            "-q",
            "--single-branch",
            "--branch",
            "main",
            str(remote.path),
            str(install_dir),
        ],
        cwd=install_dir.parent,
    )


def _origin_url(install_dir: Path) -> str:
    return _git(install_dir, "remote", "get-url", "origin")


def _upstream(install_dir: Path) -> str:
    return _git(install_dir, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")


def _symbolic_branch(install_dir: Path) -> str | None:
    result = _run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=install_dir,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _config_values(install_dir: Path, key: str) -> tuple[str, ...]:
    result = _run(
        ["git", "config", "--local", "--get-all", key],
        cwd=install_dir,
        check=False,
    )
    if result.returncode != 0:
        return ()
    return tuple(result.stdout.splitlines())


def _local_config_dump(install_dir: Path) -> str:
    return _run(
        ["git", "config", "--local", "--null", "--list"], cwd=install_dir
    ).stdout


def _remote_ref(install_dir: Path, branch: str = TARGET_BRANCH) -> str | None:
    result = _run(
        ["git", "show-ref", "--verify", "--hash", f"refs/remotes/origin/{branch}"],
        cwd=install_dir,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def test_fresh_clone_uses_custom_repository_and_tracks_origin(
    tmp_path: Path, installer: Installer, remotes: tuple[RemoteRepo, RemoteRepo]
) -> None:
    _, override = remotes
    install_dir = tmp_path / f"fresh-{installer.name}"

    installer.run_repository_stage(
        tmp_path=tmp_path,
        install_dir=install_dir,
        repo_url=override.path,
    )

    assert _origin_url(install_dir) == installer._path(override.path)
    assert _git(install_dir, "rev-parse", "HEAD") == override.branch_sha
    assert _upstream(install_dir) == f"origin/{TARGET_BRANCH}"


def test_existing_checkout_repairs_poisoned_origin(
    tmp_path: Path, installer: Installer, remotes: tuple[RemoteRepo, RemoteRepo]
) -> None:
    prior, override = remotes
    install_dir = tmp_path / f"poisoned-{installer.name}"
    _clone(prior, install_dir)
    _git(install_dir, "remote", "set-url", "origin", str(tmp_path / "unreachable.git"))

    installer.run_repository_stage(
        tmp_path=tmp_path,
        install_dir=install_dir,
        repo_url=override.path,
    )

    assert _origin_url(install_dir) == installer._path(override.path)
    assert _git(install_dir, "rev-parse", "HEAD") == override.branch_sha


def test_branch_install_repairs_stale_tracking_remote(
    tmp_path: Path, installer: Installer, remotes: tuple[RemoteRepo, RemoteRepo]
) -> None:
    prior, override = remotes
    install_dir = tmp_path / f"stale-tracking-{installer.name}"
    _clone(prior, install_dir)
    _git(install_dir, "remote", "add", "stale", str(prior.path))
    _git(install_dir, "fetch", "-q", "stale", TARGET_BRANCH)
    _git(
        install_dir,
        "branch",
        f"--set-upstream-to=stale/{TARGET_BRANCH}",
        TARGET_BRANCH,
    )

    installer.run_repository_stage(
        tmp_path=tmp_path,
        install_dir=install_dir,
        repo_url=override.path,
    )

    assert _git(install_dir, "rev-parse", "HEAD") == override.branch_sha
    assert _upstream(install_dir) == f"origin/{TARGET_BRANCH}"


@pytest.mark.parametrize(
    "override_kind", ("unreachable", "missing-branch", "same-named-tag")
)
def test_invalid_override_leaves_origin_and_tracking_unchanged(
    tmp_path: Path,
    installer: Installer,
    remotes: tuple[RemoteRepo, RemoteRepo],
    override_kind: str,
) -> None:
    prior, override = remotes
    install_dir = tmp_path / f"invalid-{override_kind}-{installer.name}"
    _clone(prior, install_dir)
    _git(install_dir, "remote", "add", "stale", str(prior.path))
    _git(install_dir, "fetch", "-q", "stale", TARGET_BRANCH)
    _git(
        install_dir,
        "branch",
        f"--set-upstream-to=stale/{TARGET_BRANCH}",
        TARGET_BRANCH,
    )

    origin_before = _origin_url(install_dir)
    upstream_before = _upstream(install_dir)
    origin_ref_before = _git(
        install_dir, "rev-parse", f"refs/remotes/origin/{TARGET_BRANCH}"
    )

    if override_kind == "unreachable":
        repo_url = tmp_path / "does-not-exist.git"
        branch = TARGET_BRANCH
    elif override_kind == "same-named-tag":
        repo_url = _make_tag_only_remote(tmp_path)
        branch = TARGET_BRANCH
    else:
        repo_url = override.path
        branch = "branch-that-does-not-exist"
    result = installer.run_repository_stage(
        tmp_path=tmp_path,
        install_dir=install_dir,
        repo_url=repo_url,
        branch=branch,
        check=False,
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert _origin_url(install_dir) == origin_before
    assert _upstream(install_dir) == upstream_before
    assert (
        _git(install_dir, "rev-parse", f"refs/remotes/origin/{TARGET_BRANCH}")
        == origin_ref_before
    )


def test_detached_commit_pin_does_not_rewrite_branch_upstream(
    tmp_path: Path, installer: Installer, remotes: tuple[RemoteRepo, RemoteRepo]
) -> None:
    prior, override = remotes
    install_dir = tmp_path / f"commit-pin-{installer.name}"
    _clone(prior, install_dir)
    _git(install_dir, "remote", "add", "stale", str(prior.path))
    _git(install_dir, "fetch", "-q", "stale", TARGET_BRANCH)
    _git(
        install_dir,
        "branch",
        f"--set-upstream-to=stale/{TARGET_BRANCH}",
        TARGET_BRANCH,
    )

    installer.run_repository_stage(
        tmp_path=tmp_path,
        install_dir=install_dir,
        repo_url=override.path,
        commit=override.pin_sha,
    )

    assert _git(install_dir, "rev-parse", "HEAD") == override.pin_sha
    assert (install_dir / "marker.txt").read_text(encoding="utf-8") == "override-pin\n"
    symbolic_head = _run(
        ["git", "symbolic-ref", "-q", "HEAD"], cwd=install_dir, check=False
    )
    assert symbolic_head.returncode == 1
    assert (
        _git(install_dir, "config", f"branch.{TARGET_BRANCH}.remote") == "stale"
    )


def test_override_commit_missing_from_override_never_falls_back_to_old_origin(
    tmp_path: Path, installer: Installer, remotes: tuple[RemoteRepo, RemoteRepo]
) -> None:
    prior, override = remotes
    install_dir = tmp_path / f"commit-not-in-override-{installer.name}"
    _clone(prior, install_dir)
    head_before = _git(install_dir, "rev-parse", "HEAD")
    symbolic_branch_before = _symbolic_branch(install_dir)
    worktree_before = (install_dir / "marker.txt").read_bytes()
    status_before = _git(install_dir, "status", "--porcelain")
    config_before = _local_config_dump(install_dir)
    origin_url_before = _origin_url(install_dir)

    result = installer.run_repository_stage(
        tmp_path=tmp_path,
        install_dir=install_dir,
        repo_url=override.path,
        commit=prior.pin_sha,
        check=False,
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert "does not provide commit" in (result.stdout + result.stderr).lower()
    assert _git(install_dir, "rev-parse", "HEAD") == head_before
    assert _symbolic_branch(install_dir) == symbolic_branch_before
    assert (install_dir / "marker.txt").read_bytes() == worktree_before
    assert _git(install_dir, "status", "--porcelain") == status_before
    assert _local_config_dump(install_dir) == config_before
    assert _origin_url(install_dir) == origin_url_before


def test_override_refuses_existing_checkout_without_origin_before_mutation(
    tmp_path: Path, installer: Installer, remotes: tuple[RemoteRepo, RemoteRepo]
) -> None:
    prior, override = remotes
    install_dir = tmp_path / f"no-origin-{installer.name}"
    _clone(prior, install_dir)
    _git(install_dir, "remote", "remove", "origin")
    head_before = _git(install_dir, "rev-parse", "HEAD")
    symbolic_branch_before = _symbolic_branch(install_dir)
    worktree_before = (install_dir / "marker.txt").read_bytes()
    status_before = _git(install_dir, "status", "--porcelain")
    config_before = _local_config_dump(install_dir)

    result = installer.run_repository_stage(
        tmp_path=tmp_path,
        install_dir=install_dir,
        repo_url=override.path,
        check=False,
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert "no origin remote" in (result.stdout + result.stderr).lower()
    assert _git(install_dir, "rev-parse", "HEAD") == head_before
    assert _symbolic_branch(install_dir) == symbolic_branch_before
    assert (install_dir / "marker.txt").read_bytes() == worktree_before
    assert _git(install_dir, "status", "--porcelain") == status_before
    assert _local_config_dump(install_dir) == config_before
    assert _run(
        ["git", "remote", "get-url", "origin"], cwd=install_dir, check=False
    ).returncode != 0


def test_ignored_ancestor_commit_repairs_upstream_when_head_stays_attached(
    tmp_path: Path, installer: Installer, remotes: tuple[RemoteRepo, RemoteRepo]
) -> None:
    prior, override = remotes
    install_dir = tmp_path / f"ignored-ancestor-{installer.name}"
    _clone(override, install_dir)
    _git(install_dir, "remote", "add", "stale", str(prior.path))
    _git(install_dir, "fetch", "-q", "stale", TARGET_BRANCH)
    _git(
        install_dir,
        "branch",
        f"--set-upstream-to=stale/{TARGET_BRANCH}",
        TARGET_BRANCH,
    )
    _git(install_dir, "remote", "set-url", "origin", str(prior.path))

    installer.run_repository_stage(
        tmp_path=tmp_path,
        install_dir=install_dir,
        repo_url=override.path,
        commit=override.ancestor_sha,
    )

    assert _symbolic_branch(install_dir) == TARGET_BRANCH
    assert _git(install_dir, "rev-parse", "HEAD") == override.branch_sha
    assert _origin_url(install_dir) == installer._path(override.path)
    assert _remote_ref(install_dir) == override.branch_sha
    assert _upstream(install_dir) == f"origin/{TARGET_BRANCH}"


def test_main_only_checkout_creates_requested_override_branch(
    tmp_path: Path, installer: Installer, remotes: tuple[RemoteRepo, RemoteRepo]
) -> None:
    prior, override = remotes
    install_dir = tmp_path / f"main-only-{installer.name}"
    _clone_main_only(prior, install_dir)
    main_branch_before = _git(install_dir, "rev-parse", "refs/heads/main")
    assert _remote_ref(install_dir) is None
    assert (
        _run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{TARGET_BRANCH}"],
            cwd=install_dir,
            check=False,
        ).returncode
        != 0
    )

    installer.run_repository_stage(
        tmp_path=tmp_path,
        install_dir=install_dir,
        repo_url=override.path,
    )

    assert _symbolic_branch(install_dir) == TARGET_BRANCH
    assert _git(install_dir, "rev-parse", "HEAD") == override.branch_sha
    assert _origin_url(install_dir) == installer._path(override.path)
    assert _remote_ref(install_dir) == override.branch_sha
    assert _config_values(install_dir, "remote.origin.fetch") == (
        f"+refs/heads/{TARGET_BRANCH}:refs/remotes/origin/{TARGET_BRANCH}",
    )
    assert _upstream(install_dir) == f"origin/{TARGET_BRANCH}"
    assert _git(install_dir, "rev-parse", "refs/heads/main") == main_branch_before


def test_override_transaction_rolls_back_ref_prefix_collision(
    tmp_path: Path, installer: Installer, remotes: tuple[RemoteRepo, RemoteRepo]
) -> None:
    prior, override = remotes
    install_dir = tmp_path / f"ref-collision-{installer.name}"
    _clone(prior, install_dir)
    _git(install_dir, "remote", "add", "stale", str(prior.path))
    _git(install_dir, "fetch", "-q", "stale", TARGET_BRANCH)
    _git(
        install_dir,
        "branch",
        f"--set-upstream-to=stale/{TARGET_BRANCH}",
        TARGET_BRANCH,
    )
    _git(install_dir, "update-ref", "-d", f"refs/remotes/origin/{TARGET_BRANCH}")
    collision_ref = f"refs/remotes/origin/{TARGET_BRANCH}/child"
    collision_sha = _git(install_dir, "rev-parse", "HEAD")
    _git(install_dir, "update-ref", collision_ref, collision_sha)

    head_before = _git(install_dir, "rev-parse", "HEAD")
    symbolic_branch_before = _symbolic_branch(install_dir)
    worktree_before = (install_dir / "marker.txt").read_bytes()
    status_before = _git(install_dir, "status", "--porcelain")
    origin_url_before = _origin_url(install_dir)
    origin_fetch_before = _config_values(install_dir, "remote.origin.fetch")
    branch_remote_before = _config_values(
        install_dir, f"branch.{TARGET_BRANCH}.remote"
    )
    branch_merge_before = _config_values(
        install_dir, f"branch.{TARGET_BRANCH}.merge"
    )

    result = installer.run_repository_stage(
        tmp_path=tmp_path,
        install_dir=install_dir,
        repo_url=override.path,
        check=False,
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert "remote-tracking ref namespace collision" in (
        result.stdout + result.stderr
    ).lower()
    assert _git(install_dir, "rev-parse", "HEAD") == head_before
    assert _symbolic_branch(install_dir) == symbolic_branch_before
    assert (install_dir / "marker.txt").read_bytes() == worktree_before
    assert _git(install_dir, "status", "--porcelain") == status_before
    assert _origin_url(install_dir) == origin_url_before
    assert _config_values(install_dir, "remote.origin.fetch") == origin_fetch_before
    assert (
        _config_values(install_dir, f"branch.{TARGET_BRANCH}.remote")
        == branch_remote_before
    )
    assert (
        _config_values(install_dir, f"branch.{TARGET_BRANCH}.merge")
        == branch_merge_before
    )
    assert _remote_ref(install_dir) is None
    assert _git(install_dir, "rev-parse", collision_ref) == collision_sha


@pytest.mark.parametrize("override_kind", ("unreachable", "missing-branch"))
def test_fresh_invalid_override_never_falls_back_to_official_repository(
    tmp_path: Path,
    installer: Installer,
    remotes: tuple[RemoteRepo, RemoteRepo],
    override_kind: str,
) -> None:
    _, override = remotes
    install_dir = tmp_path / f"fresh-invalid-{override_kind}-{installer.name}"
    if override_kind == "unreachable":
        repo_url = tmp_path / "fresh-does-not-exist.git"
        branch = TARGET_BRANCH
    else:
        repo_url = override.path
        branch = "fresh-branch-that-does-not-exist"

    result = installer.run_repository_stage(
        tmp_path=tmp_path,
        install_dir=install_dir,
        repo_url=repo_url,
        branch=branch,
        check=False,
    )

    assert result.returncode != 0, result.stdout + result.stderr
    probe = _run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=install_dir if install_dir.exists() else tmp_path,
        check=False,
    )
    assert probe.returncode != 0


def test_powershell_conflicting_local_tag_uses_override_tag(
    tmp_path: Path,
    powershell_installer: Installer,
    remotes: tuple[RemoteRepo, RemoteRepo],
) -> None:
    prior, override = remotes
    assert prior.tag_sha and override.tag_sha and prior.tag_sha != override.tag_sha
    install_dir = tmp_path / "conflicting-tag-powershell"
    _clone(prior, install_dir)
    branch_remote_before = _config_values(
        install_dir, f"branch.{TARGET_BRANCH}.remote"
    )
    branch_merge_before = _config_values(
        install_dir, f"branch.{TARGET_BRANCH}.merge"
    )

    powershell_installer.run_repository_stage(
        tmp_path=tmp_path,
        install_dir=install_dir,
        repo_url=override.path,
        tag=INSTALL_TAG,
    )

    assert _symbolic_branch(install_dir) is None
    assert _git(install_dir, "rev-parse", "HEAD") == override.tag_sha
    assert (
        _git(install_dir, "rev-parse", f"refs/tags/{INSTALL_TAG}^{{commit}}")
        == prior.tag_sha
    )
    assert _origin_url(install_dir) == str(override.path.resolve())
    assert (
        _config_values(install_dir, f"branch.{TARGET_BRANCH}.remote")
        == branch_remote_before
    )
    assert (
        _config_values(install_dir, f"branch.{TARGET_BRANCH}.merge")
        == branch_merge_before
    )


def test_powershell_missing_override_tag_fails_without_stale_tag_or_mutation(
    tmp_path: Path,
    powershell_installer: Installer,
    remotes: tuple[RemoteRepo, RemoteRepo],
) -> None:
    prior, _ = remotes
    override = _make_remote(tmp_path, "untagged-override", "untagged", include_tag=False)
    install_dir = tmp_path / "missing-tag-powershell"
    _clone(prior, install_dir)
    local_tag_before = _git(
        install_dir, "rev-parse", f"refs/tags/{INSTALL_TAG}^{{commit}}"
    )
    head_before = _git(install_dir, "rev-parse", "HEAD")
    origin_url_before = _origin_url(install_dir)
    origin_fetch_before = _config_values(install_dir, "remote.origin.fetch")
    origin_ref_before = _remote_ref(install_dir)
    branch_remote_before = _config_values(
        install_dir, f"branch.{TARGET_BRANCH}.remote"
    )
    branch_merge_before = _config_values(
        install_dir, f"branch.{TARGET_BRANCH}.merge"
    )

    result = powershell_installer.run_repository_stage(
        tmp_path=tmp_path,
        install_dir=install_dir,
        repo_url=override.path,
        tag=INSTALL_TAG,
        check=False,
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert _symbolic_branch(install_dir) == TARGET_BRANCH
    assert _git(install_dir, "rev-parse", "HEAD") == head_before
    assert _origin_url(install_dir) == origin_url_before
    assert _config_values(install_dir, "remote.origin.fetch") == origin_fetch_before
    assert _remote_ref(install_dir) == origin_ref_before
    assert (
        _git(install_dir, "rev-parse", f"refs/tags/{INSTALL_TAG}^{{commit}}")
        == local_tag_before
    )
    assert (
        _config_values(install_dir, f"branch.{TARGET_BRANCH}.remote")
        == branch_remote_before
    )
    assert (
        _config_values(install_dir, f"branch.{TARGET_BRANCH}.merge")
        == branch_merge_before
    )
