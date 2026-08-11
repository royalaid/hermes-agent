from __future__ import annotations

import base64
from copy import deepcopy
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from urllib.parse import urlsplit

import pytest

from fork_integration.audit import (
    CANONICAL_MANIFEST_PATH,
    GitProbe,
    audit_manifest,
    audit_release_candidate,
    canonical_repository_identity,
)
from fork_integration.finalize import (
    ReplacementFinalizationBlocked,
    finalize_component_replacement,
)
import fork_integration.prepare as prepare_module
from fork_integration.prepare import (
    PreparationBlocked,
    PreparationFailed,
    prepare_worktree,
)
from fork_integration.publish import (
    PublicationBlocked,
    PublicationFailed,
    publish_release_candidate,
)


def run_git(repository: Path, *args: str, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return completed.stdout.strip()


def commit_file(
    repository: Path,
    relative_path: str,
    content: str,
    subject: str,
    timestamp: str,
) -> str:
    path = repository / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    run_git(repository, "add", relative_path)
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_DATE": timestamp,
            "GIT_COMMITTER_DATE": timestamp,
        }
    )
    run_git(repository, "commit", "-m", subject, env=environment)
    return run_git(repository, "rev-parse", "HEAD")


def stable_patch_id(repository: Path, commit: str) -> str:
    patch = subprocess.run(
        ["git", "-C", str(repository), "show", "--format=email", "--patch", commit],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    result = subprocess.run(
        ["git", "-C", str(repository), "patch-id", "--stable"],
        check=True,
        capture_output=True,
        text=True,
        input=patch,
    ).stdout
    return result.split()[0]


def commit_canonical_manifest(repository: Path, manifest: dict) -> str:
    run_git(repository, "checkout", "-b", "release-control")
    return commit_file(
        repository,
        CANONICAL_MANIFEST_PATH,
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        "chore(release): bind canonical fork manifest",
        "2001-01-05T00:00:00+00:00",
    )


@pytest.fixture
def audited_repository(tmp_path: Path) -> tuple[Path, dict]:
    repository = tmp_path / "work"
    upstream = tmp_path / "upstream.git"
    fork = tmp_path / "fork.git"
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repository)],
        check=True,
        capture_output=True,
        text=True,
    )
    run_git(repository, "config", "user.email", "tests@example.test")
    run_git(repository, "config", "user.name", "Manifest Tests")
    run_git(repository, "config", "core.autocrlf", "false")
    base = commit_file(
        repository,
        "README.md",
        "base\n",
        "chore: base",
        "2001-01-01T00:00:00+00:00",
    )
    subprocess.run(["git", "init", "--bare", str(upstream)], check=True, capture_output=True)
    subprocess.run(["git", "init", "--bare", str(fork)], check=True, capture_output=True)
    run_git(repository, "remote", "add", "upstream-test", str(upstream))
    run_git(repository, "remote", "add", "origin", str(fork))
    run_git(repository, "push", "upstream-test", "main:refs/heads/main")

    run_git(repository, "checkout", "-b", "feature/updater")
    subject = "fix(updater): release Windows runtime holders"
    source_commit = commit_file(
        repository,
        "native-updater.txt",
        "release holders\n",
        subject,
        "2001-01-02T00:00:00+00:00",
    )
    source_patch_id = stable_patch_id(repository, source_commit)
    run_git(repository, "commit", "--allow-empty", "-m", "chore: source branch marker")
    run_git(repository, "push", "origin", "feature/updater:refs/heads/feature/updater")

    run_git(repository, "checkout", "-b", "fork-integration", base)
    integration_environment = os.environ.copy()
    integration_environment["GIT_COMMITTER_DATE"] = "2001-01-03T00:00:00+00:00"
    run_git(repository, "cherry-pick", source_commit, env=integration_environment)
    integration_commit = run_git(repository, "rev-parse", "HEAD")
    integration_patch_id = stable_patch_id(repository, integration_commit)
    integration_head = integration_commit
    run_git(repository, "push", "origin", "fork-integration:refs/heads/fork-integration")

    manifest = {
        "schema_version": 2,
        "manifest_state": "ready",
        "repositories": {
            "upstream": {"url": str(upstream)},
            "fork": {"url": str(fork)},
        },
        "integration": {
            "repository": "fork",
            "ref": "refs/heads/fork-integration",
            "upstream_repository": "upstream",
            "upstream_ref": "refs/heads/main",
            "expected_base_commit": base,
            "expected_head_commit": integration_head,
        },
        "required_categories": ["updater"],
        "required_patch_ids": [source_patch_id],
        "components": [
            {
                "id": "native-windows-updater",
                "category": "updater",
                "upstream_status": "required",
                "source": {
                    "repository": "fork",
                    "ref": "refs/heads/feature/updater",
                },
                "tests": ["tests/updater/test_native_windows.py"],
                "patches": [
                    {
                        "subject": subject,
                        "role": "implementation",
                        "disposition": "required",
                        "source": {
                            "commit": source_commit,
                            "stable_patch_id": source_patch_id,
                        },
                        "integration": {
                            "state": "expected",
                            "commit": integration_commit,
                            "stable_patch_id": integration_patch_id,
                        },
                    }
                ],
            }
        ],
    }
    return repository, manifest


def finding_codes(report: dict) -> set[str]:
    return {finding["code"] for finding in report["findings"]}


def test_status_reports_exact_live_and_local_identities_without_mutating_refs(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    refs_before = run_git(repository, "for-each-ref", "--format=%(refname) %(objectname)")
    locks_before = sorted(path.relative_to(repository) for path in repository.rglob("*.lock"))

    report = audit_manifest(manifest, repository, installed_repository=repository)

    assert report["ready"] is True, report["findings"]
    assert report["writes"] == []
    assert report["identities"]["upstream"]["commit"] == manifest["integration"]["expected_base_commit"]
    assert report["identities"]["published"]["commit"] == manifest["integration"]["expected_head_commit"]
    assert report["identities"]["local"]["commit"] == manifest["integration"]["expected_head_commit"]
    assert report["identities"]["installed"]["commit"] == manifest["integration"]["expected_head_commit"]
    assert report["identities"]["installed"]["ref"] == "refs/heads/fork-integration"
    assert report["identities"]["installed"]["remote_url"] == manifest["repositories"]["fork"]["url"]
    assert run_git(repository, "for-each-ref", "--format=%(refname) %(objectname)") == refs_before
    assert sorted(path.relative_to(repository) for path in repository.rglob("*.lock")) == locks_before

    allowed_local = {
        "rev-parse",
        "symbolic-ref",
        "remote",
        "show",
        "patch-id",
        "diff-tree",
        "merge-base",
        "rev-list",
        "check-ref-format",
    }
    for command in report["git_commands"]:
        if "-C" not in command:
            assert "ls-remote" in command
            continue
        subcommand = command[command.index("-C") + 2]
        assert subcommand in allowed_local


def test_detects_different_ref_that_resolves_to_integration_tip(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    run_git(repository, "branch", "source-cycle", "fork-integration")
    manifest["components"][0]["source"]["ref"] = "refs/heads/source-cycle"

    report = audit_manifest(manifest, repository, observe_live=False)

    assert "circular_source_ref" in finding_codes(report)


def test_audit_rejects_source_ref_at_earlier_integration_commit(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    base = manifest["integration"]["expected_base_commit"]
    run_git(repository, "branch", "source-from-integration", base)
    manifest["components"][0]["source"]["ref"] = (
        "refs/heads/source-from-integration"
    )

    report = audit_manifest(manifest, repository, observe_live=False)

    assert "source_ref_integration_lineage" in finding_codes(report)


def test_audit_rejects_required_source_commit_equal_to_final_commit(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    patch = manifest["components"][0]["patches"][0]
    final_commit = patch["integration"]["commit"]
    run_git(repository, "branch", "source-is-final", final_commit)
    manifest["components"][0]["source"]["ref"] = "refs/heads/source-is-final"
    patch["source"] = {
        "commit": final_commit,
        "stable_patch_id": patch["integration"]["stable_patch_id"],
    }

    report = audit_manifest(manifest, repository, observe_live=False)

    assert "source_final_commit_same" in finding_codes(report)
    assert "source_commit_integration_lineage" in finding_codes(report)


def test_prepare_blocks_different_source_ref_at_integration_tip_before_writes(
    audited_repository: tuple[Path, dict], tmp_path: Path,
):
    repository, manifest = audited_repository
    run_git(repository, "branch", "source-cycle", "fork-integration")
    run_git(
        repository,
        "push",
        "origin",
        "source-cycle:refs/heads/source-cycle",
    )
    manifest["components"][0]["source"]["ref"] = "refs/heads/source-cycle"
    target = tmp_path / "source-cycle-prepare"

    with pytest.raises(PreparationBlocked) as caught:
        prepare_worktree(manifest, repository, target)

    assert "circular_source_ref" in {
        finding.code for finding in caught.value.findings
    }
    assert target.exists() is False


def test_detects_unrelated_implementation_patches_grouped_as_one_component(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    run_git(repository, "checkout", "feature/updater")
    second_commit = commit_file(
        repository,
        "scripts/bootstrap-marker.txt",
        "bootstrap\n",
        "fix(bootstrap): pin installer source",
        "2001-01-04T00:00:00+00:00",
    )
    second_patch_id = stable_patch_id(repository, second_commit)
    component = manifest["components"][0]
    component["upstream_status"] = "review_required"
    component["patches"].append(
        {
            "subject": "fix(bootstrap): pin installer source",
            "role": "implementation",
            "disposition": "review_required",
            "source": {
                "commit": second_commit,
                "stable_patch_id": second_patch_id,
            },
            "integration": {
                "state": "pending",
                "commit": None,
                "stable_patch_id": None,
            },
        }
    )
    manifest["manifest_state"] = "review_required"

    report = audit_manifest(manifest, repository, observe_live=False)

    assert "unrelated_patches_grouped" in finding_codes(report)


def test_subject_match_never_substitutes_for_pending_patch_identity(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    component = manifest["components"][0]
    component["upstream_status"] = "review_required"
    component["patches"][0]["integration"] = {
        "state": "pending",
        "commit": None,
        "stable_patch_id": None,
    }
    manifest["manifest_state"] = "review_required"

    report = audit_manifest(manifest, repository, observe_live=False)

    assert "same_subject_non_equivalent" in finding_codes(report)


def test_expected_patch_identity_does_not_depend_on_commit_subject(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    manifest["components"][0]["patches"][0]["subject"] = (
        "fix(updater): retained behavior with reviewed wording"
    )

    report = audit_manifest(manifest, repository, observe_live=False)

    assert "expected_patch_missing" not in finding_codes(report)


def test_detects_declared_patch_id_that_does_not_match_git_object(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    manifest = deepcopy(manifest)
    manifest["components"][0]["patches"][0]["source"]["stable_patch_id"] = "f" * 40

    report = audit_manifest(manifest, repository, observe_live=False)

    assert "source_patch_id_mismatch" in finding_codes(report)


def test_audit_rejects_absorbed_patch_missing_from_upstream_base(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    component = manifest["components"][0]
    component["upstream_status"] = "absorbed"
    patch = component["patches"][0]
    patch["disposition"] = "absorbed_upstream"
    patch["integration"] = {
        "state": "not_replayed",
        "commit": None,
        "stable_patch_id": None,
    }
    manifest["required_patch_ids"] = []

    report = audit_manifest(manifest, repository, observe_live=False)

    assert "absorbed_patch_missing" in finding_codes(report)


def test_ready_source_ref_must_be_available_locally_and_live(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    manifest["components"][0]["source"]["ref"] = "refs/heads/missing-source"

    report = audit_manifest(manifest, repository)

    assert {"source_ref_unavailable", "source_ref_live_unknown"} <= finding_codes(
        report
    )


def test_ready_source_ref_live_tip_must_match_local_tip(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    run_git(repository, "checkout", "feature/updater")
    run_git(repository, "commit", "--allow-empty", "-m", "chore: local-only source tip")

    report = audit_manifest(manifest, repository)

    assert "source_ref_live_mismatch" in finding_codes(report)


def test_ready_source_repository_must_bind_to_a_configured_remote(
    audited_repository: tuple[Path, dict], tmp_path: Path,
):
    repository, manifest = audited_repository
    foreign = tmp_path / "foreign.git"
    subprocess.run(["git", "init", "--bare", str(foreign)], check=True, capture_output=True)
    manifest["repositories"]["foreign"] = {"url": str(foreign)}
    manifest["components"][0]["source"]["repository"] = "foreign"

    report = audit_manifest(manifest, repository, observe_live=False)

    assert "source_repository_unbound" in finding_codes(report)


def test_audit_rejects_integration_head_unrelated_to_base(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    tree = run_git(
        repository,
        "rev-parse",
        f"{manifest['integration']['expected_base_commit']}^{{tree}}",
    )
    unrelated = run_git(repository, "commit-tree", tree, "-m", "unrelated root")
    manifest["integration"]["expected_head_commit"] = unrelated

    report = audit_manifest(manifest, repository, observe_live=False)

    assert "integration_base_not_ancestor" in finding_codes(report)


def test_git_probe_sanitizes_routing_environment_and_disables_hooks_and_signing(
    audited_repository: tuple[Path, dict], monkeypatch: pytest.MonkeyPatch,
):
    repository, _manifest = audited_repository
    for key, value in {
        "GIT_DIR": "C:/attacker/repository.git",
        "GIT_WORK_TREE": "C:/attacker/worktree",
        "GIT_OBJECT_DIRECTORY": "C:/attacker/objects",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": "C:/attacker/alternate",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": "C:/attacker/hooks",
        "GIT_SSH_COMMAND": "attacker-command",
    }.items():
        monkeypatch.setenv(key, value)
    captured_environments: list[dict[str, str]] = []
    real_run = subprocess.run

    def recording_run(*args, **kwargs):
        captured_environments.append(kwargs["env"])
        return real_run(*args, **kwargs)

    probe = GitProbe(repository, run=recording_run)

    assert probe.resolve_commit("HEAD") is not None
    assert captured_environments
    assert {
        key
        for key in captured_environments[0]
        if key.upper().startswith("GIT_")
    } == {
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_OPTIONAL_LOCKS",
        "GIT_TERMINAL_PROMPT",
        "GIT_NO_LAZY_FETCH",
        "GIT_NO_REPLACE_OBJECTS",
    }
    command = probe.commands[0]
    assert "commit.gpgSign=false" in command
    assert "tag.gpgSign=false" in command
    assert any(argument.startswith("core.hooksPath=") for argument in command)


def test_repository_identity_preserves_nondefault_port_and_remote_path_case(
    tmp_path: Path,
):
    assert canonical_repository_identity(
        "https://Example.test:8443/Owner/Repo.git", base=tmp_path
    ) == "example.test:8443/Owner/Repo"
    assert canonical_repository_identity(
        "https://Example.test:443/Owner/Repo.git", base=tmp_path
    ) == "example.test/Owner/Repo"
    assert canonical_repository_identity(
        "git@Example.test:Owner/Repo.git", base=tmp_path
    ) == "example.test/Owner/Repo"
    assert canonical_repository_identity(
        "https://example.test/owner/repo.git", base=tmp_path
    ) != canonical_repository_identity(
        "https://example.test/Owner/Repo.git", base=tmp_path
    )


@pytest.mark.windows_only
def test_repository_identity_normalizes_windows_drive_file_uri_and_unc_aliases(
    tmp_path: Path,
):
    local = tmp_path / "Owner" / "Repo.git"

    assert canonical_repository_identity(
        local.as_uri(), base=tmp_path
    ) == canonical_repository_identity(str(local), base=tmp_path)
    assert canonical_repository_identity(
        "file://Server/Share/Owner/Repo.git", base=tmp_path
    ) == canonical_repository_identity(
        r"\\Server\Share\Owner\Repo.git", base=tmp_path
    )


def test_stable_patch_id_is_independent_of_diff_config_and_external_drivers(
    tmp_path: Path,
):
    repository = tmp_path / "deterministic-patch"
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repository)],
        check=True,
        capture_output=True,
    )
    run_git(repository, "config", "user.email", "tests@example.test")
    run_git(repository, "config", "user.name", "Manifest Tests")
    run_git(repository, "config", "core.autocrlf", "false")
    (repository / ".gitattributes").write_text(
        "*.txt diff=fork-integration-sentinel\n", encoding="utf-8"
    )
    (repository / "alpha.txt").write_text("alpha\n", encoding="utf-8")
    (repository / "beta.txt").write_text("beta\n", encoding="utf-8")
    run_git(repository, "add", ".gitattributes", "alpha.txt", "beta.txt")
    run_git(repository, "commit", "-m", "test: deterministic patch base")
    (repository / "alpha.txt").write_text("alpha changed\n", encoding="utf-8")
    (repository / "beta.txt").write_text("beta changed\n", encoding="utf-8")
    run_git(repository, "add", "alpha.txt", "beta.txt")
    run_git(repository, "commit", "-m", "fix(test): deterministic patch")
    commit = run_git(repository, "rev-parse", "HEAD")
    probe = GitProbe(repository)
    baseline = probe.stable_patch_id(commit)

    sentinel = tmp_path / "diff-driver-ran.txt"
    helper = tmp_path / "diff_driver.py"
    helper.write_text(
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('ran', encoding='utf-8')\n"
        "print('external output')\n",
        encoding="utf-8",
    )
    driver = (
        f'"{Path(sys.executable).as_posix()}" '
        f'"{helper.as_posix()}" "{sentinel.as_posix()}"'
    )
    order_file = tmp_path / "diff-order.txt"
    order_file.write_text("beta.txt\nalpha.txt\n", encoding="utf-8")
    run_git(repository, "config", "diff.fork-integration-sentinel.textconv", driver)
    run_git(repository, "config", "diff.external", driver)
    run_git(repository, "config", "diff.algorithm", "histogram")
    run_git(repository, "config", "diff.renames", "true")
    run_git(repository, "config", "diff.orderFile", str(order_file))
    run_git(repository, "config", "color.ui", "always")

    configured = probe.stable_patch_id(commit)

    assert configured == baseline
    assert sentinel.exists() is False
    show_command = next(command for command in reversed(probe.commands) if "show" in command)
    for option in (
        "--no-textconv",
        "--no-ext-diff",
        "--full-index",
        "--binary",
        "--no-color",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        "--no-renames",
        "--diff-algorithm=myers",
        "--submodule=short",
    ):
        assert option in show_command
    assert "core.quotePath=true" in show_command


def test_cleanliness_accepts_native_autocrlf_checkout_under_isolated_config(
    tmp_path: Path,
):
    repository = tmp_path / "autocrlf"
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repository)],
        check=True,
        capture_output=True,
    )
    run_git(repository, "config", "user.email", "tests@example.test")
    run_git(repository, "config", "user.name", "Manifest Tests")
    run_git(repository, "config", "core.autocrlf", "false")
    payload = repository / "payload.txt"
    payload.write_bytes(b"line\n")
    run_git(repository, "add", "payload.txt")
    run_git(repository, "commit", "-m", "test: autocrlf base")
    run_git(repository, "config", "core.autocrlf", "true")
    payload.write_bytes(b"line\r\n")
    probe = GitProbe(repository)

    isolated_default = probe._run(
        ("status", "--porcelain=v1", "--untracked-files=all")
    )
    assert isolated_default.stdout
    assert probe.repository_is_clean() is True
    assert probe.last_cleanliness_mode == "autocrlf"

    payload.write_bytes(b"changed\r\n")
    assert GitProbe(repository).repository_is_clean() is False


def test_cleanliness_rejects_crlf_mutation_for_explicit_nontext_path(
    tmp_path: Path,
):
    repository = tmp_path / "nontext-crlf"
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repository)],
        check=True,
        capture_output=True,
    )
    run_git(repository, "config", "user.email", "tests@example.test")
    run_git(repository, "config", "user.name", "Manifest Tests")
    run_git(repository, "config", "core.autocrlf", "false")
    (repository / ".gitattributes").write_bytes(b"payload.bin -text\n")
    payload = repository / "payload.bin"
    payload.write_bytes(b"line\n")
    run_git(repository, "add", ".gitattributes", "payload.bin")
    run_git(repository, "commit", "-m", "test: explicit nontext payload")
    run_git(repository, "config", "core.autocrlf", "true")
    payload.write_bytes(b"line\r\n")
    probe = GitProbe(repository)

    assert probe.repository_is_clean() is False
    assert probe.last_cleanliness_mode is None
    assert any("check-attr" in command for command in probe.commands)
    assert all("status" not in command and "diff" not in command for command in probe.commands)


def test_cleanliness_never_executes_repository_content_filters_or_writes(
    tmp_path: Path,
):
    repository = tmp_path / "filter-sentinel"
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repository)],
        check=True,
        capture_output=True,
    )
    run_git(repository, "config", "user.email", "tests@example.test")
    run_git(repository, "config", "user.name", "Manifest Tests")
    (repository / ".gitattributes").write_bytes(
        b"payload.txt filter=sentinel\n"
    )
    payload = repository / "payload.txt"
    payload.write_bytes(b"unchanged payload\n")
    run_git(repository, "add", ".gitattributes", "payload.txt")
    run_git(repository, "commit", "-m", "test: content filter sentinel")

    clean_marker = tmp_path / "clean-filter-ran.txt"
    process_marker = tmp_path / "process-filter-ran.txt"
    clean_helper = tmp_path / "clean_filter.py"
    clean_helper.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "Path(sys.argv[1]).write_text('ran', encoding='utf-8')\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n",
        encoding="utf-8",
    )
    process_helper = tmp_path / "process_filter.py"
    process_helper.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "Path(sys.argv[1]).write_text('ran', encoding='utf-8')\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    clean_command = (
        f'"{Path(sys.executable).as_posix()}" '
        f'"{clean_helper.as_posix()}" "{clean_marker.as_posix()}"'
    )
    process_command = (
        f'"{Path(sys.executable).as_posix()}" '
        f'"{process_helper.as_posix()}" "{process_marker.as_posix()}"'
    )
    run_git(repository, "config", "filter.sentinel.clean", clean_command)
    run_git(repository, "config", "filter.sentinel.process", process_command)
    run_git(repository, "config", "filter.sentinel.required", "true")

    payload_stat = payload.stat()
    os.utime(
        payload,
        ns=(payload_stat.st_atime_ns, payload_stat.st_mtime_ns + 2_000_000_000),
    )
    refs_before = run_git(
        repository, "for-each-ref", "--format=%(refname) %(objectname)"
    )

    def git_metadata() -> dict[str, tuple[int, int, bytes]]:
        git_directory = repository / ".git"
        return {
            path.relative_to(git_directory).as_posix(): (
                path.stat().st_mtime_ns,
                path.stat().st_size,
                path.read_bytes(),
            )
            for path in git_directory.rglob("*")
            if path.is_file()
        }

    metadata_before = git_metadata()
    probe = GitProbe(repository)

    assert probe.repository_is_clean() is True

    metadata_after = git_metadata()
    refs_after = run_git(
        repository, "for-each-ref", "--format=%(refname) %(objectname)"
    )
    assert probe.last_cleanliness_mode == "raw-bytes"
    assert clean_marker.exists() is False
    assert process_marker.exists() is False
    assert metadata_after == metadata_before
    assert refs_after == refs_before
    assert (repository / ".git" / "index.lock").exists() is False
    assert all("status" not in command and "diff" not in command for command in probe.commands)


def test_git_probe_ignores_repository_replace_refs(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    base = manifest["integration"]["expected_base_commit"]
    replacement = manifest["components"][0]["patches"][0]["source"]["commit"]
    run_git(repository, "replace", base, replacement)

    probe = GitProbe(repository)

    assert probe.subject(base) == "chore: base"


def test_audit_fails_closed_when_reachability_cannot_be_proven(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    probe = GitProbe(repository)
    base = manifest["integration"]["expected_base_commit"]
    head = manifest["integration"]["expected_head_commit"]
    real_is_ancestor = probe.is_ancestor

    def unreliable_is_ancestor(ancestor: str, descendant: str) -> bool | None:
        if (ancestor, descendant) == (base, head):
            return real_is_ancestor(ancestor, descendant)
        return None

    probe.is_ancestor = unreliable_is_ancestor  # type: ignore[method-assign]

    report = audit_manifest(manifest, repository, observe_live=False, probe=probe)

    result = finding_codes(report)
    assert "source_commit_not_reachable" in result
    assert "integration_commit_not_reachable" in result


def test_git_probe_timeout_becomes_unknown_structured_audit_findings(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository

    def time_out(command, **_kwargs):
        raise subprocess.TimeoutExpired(command, 30)

    probe = GitProbe(repository, run=time_out)

    report = audit_manifest(manifest, repository, observe_live=False, probe=probe)

    assert report["ready"] is False
    assert "expected_base_missing" in finding_codes(report)
    assert report["identities"]["local"]["state"] == "unknown"


def test_mutation_runner_sanitizes_git_environment_and_forces_safe_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("GIT_DIR", "C:/attacker/repository.git")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    captured: dict = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["environment"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(prepare_module.subprocess, "run", fake_run)

    prepare_module._run_mutation(("status",), cwd=tmp_path)

    assert "GIT_DIR" not in captured["environment"]
    assert "GIT_CONFIG_COUNT" not in captured["environment"]
    assert captured["environment"]["GIT_CONFIG_GLOBAL"] == os.devnull
    assert captured["environment"]["GIT_CONFIG_NOSYSTEM"] == "1"
    assert "commit.gpgSign=false" in captured["command"]
    assert "tag.gpgSign=false" in captured["command"]
    assert "core.useReplaceRefs=false" in captured["command"]
    hooks_argument = next(
        argument
        for argument in captured["command"]
        if argument.startswith("core.hooksPath=")
    )
    assert ".git" not in hooks_argument


def test_mutation_runner_ignores_ambient_global_template_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    template = tmp_path / "attacker-template"
    template_hook = template / "hooks" / "post-checkout"
    template_hook.parent.mkdir(parents=True)
    template_hook.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    global_config = tmp_path / "attacker.gitconfig"
    global_config.write_text(
        f"[init]\n\ttemplateDir = {template.as_posix()}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    target = tmp_path / "isolated-init"

    prepare_module._run_mutation(("init", "--", str(target)))

    assert (target / ".git" / "hooks" / "post-checkout").exists() is False


def test_mutation_runner_ignores_ambient_global_filter_driver(tmp_path: Path):
    source = tmp_path / "filter-source"
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(source)],
        check=True,
        capture_output=True,
    )
    run_git(source, "config", "user.email", "tests@example.test")
    run_git(source, "config", "user.name", "Manifest Tests")
    (source / ".gitattributes").write_text(
        "*.txt filter=fork-integration-sentinel\n", encoding="utf-8"
    )
    (source / "payload.txt").write_text("payload\n", encoding="utf-8")
    run_git(source, "add", ".gitattributes", "payload.txt")
    run_git(source, "commit", "-m", "test: filtered payload")

    sentinel = tmp_path / "filter-ran.txt"
    helper = tmp_path / "filter_helper.py"
    helper.write_text(
        "import pathlib, sys\n"
        "data = sys.stdin.buffer.read()\n"
        "pathlib.Path(sys.argv[1]).write_text('ran', encoding='utf-8')\n"
        "sys.stdout.buffer.write(data)\n",
        encoding="utf-8",
    )
    global_config = tmp_path / "filter.gitconfig"
    global_config.write_text(
        "[filter \"fork-integration-sentinel\"]\n"
        f"\tsmudge = {Path(sys.executable).as_posix()} "
        f"{helper.as_posix()} {sentinel.as_posix()}\n"
        "\trequired = true\n",
        encoding="utf-8",
    )
    target = tmp_path / "filter-target"
    ambient = os.environ.copy()
    ambient["GIT_CONFIG_GLOBAL"] = str(global_config)

    prepare_module._run_mutation(
        ("clone", "--no-hardlinks", "--", str(source), str(target)),
        environment=ambient,
    )

    assert sentinel.exists() is False
    assert (target / "payload.txt").read_text(encoding="utf-8") == "payload\n"


@pytest.mark.parametrize("malformed_field", ["patches", "required_patch_ids"])
def test_audit_and_prepare_report_malformed_collections_without_crashing(
    audited_repository: tuple[Path, dict],
    tmp_path: Path,
    malformed_field: str,
):
    repository, manifest = audited_repository
    if malformed_field == "patches":
        manifest["components"][0]["patches"] = 42
        expected_code = "missing_patches"
    else:
        manifest["required_patch_ids"] = 42
        expected_code = "invalid_required_patch_ids"

    report = audit_manifest(manifest, repository, observe_live=False)

    assert expected_code in finding_codes(report)
    target = tmp_path / f"malformed-{malformed_field}"
    with pytest.raises(PreparationBlocked) as caught:
        prepare_worktree(manifest, repository, target)
    assert expected_code in {finding.code for finding in caught.value.findings}
    assert target.exists() is False


def test_option_like_revision_is_rejected_before_git_and_live_url_is_delimited(
    audited_repository: tuple[Path, dict],
):
    repository, _manifest = audited_repository
    probe = GitProbe(repository)
    before = len(probe.commands)

    assert probe.resolve_commit("--help") is None
    assert len(probe.commands) == before
    probe.live_ref("-u", "refs/heads/main")
    ls_remote = next(command for command in probe.commands if "ls-remote" in command)
    assert ls_remote.index("--") < ls_remote.index("-u")


def test_prepare_rejects_option_like_branch_before_creating_target(
    audited_repository: tuple[Path, dict], tmp_path: Path,
):
    repository, manifest = audited_repository
    manifest["integration"]["ref"] = "refs/heads/-danger"
    target = tmp_path / "blocked-option-branch"

    with pytest.raises(PreparationBlocked) as caught:
        prepare_worktree(manifest, repository, target, dry_run=True)

    assert target.exists() is False
    assert "invalid_prepare_branch" in {
        finding.code for finding in caught.value.findings
    }


def test_prepare_rejects_unpublished_local_source_tip_before_creating_target(
    audited_repository: tuple[Path, dict], tmp_path: Path,
):
    repository, manifest = audited_repository
    run_git(repository, "checkout", "feature/updater")
    run_git(repository, "commit", "--allow-empty", "-m", "chore: unpublished tip")
    target = tmp_path / "blocked-stale-source"

    with pytest.raises(PreparationBlocked) as caught:
        prepare_worktree(manifest, repository, target, dry_run=True)

    assert target.exists() is False
    assert "prepare_source_ref_live_mismatch" in {
        finding.code for finding in caught.value.findings
    }


def test_strict_release_loads_canonical_candidate_blob_and_binds_live_refs(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    candidate = commit_canonical_manifest(repository, manifest)

    report = audit_release_candidate(repository, candidate)

    assert report["ready"] is True, report["findings"]
    assert report["release_candidate"]["commit"] == candidate
    assert report["release_candidate"]["manifest_path"] == CANONICAL_MANIFEST_PATH
    assert report["release_candidate"]["manifest_blob"] != "unknown"
    assert report["identities"]["published"]["commit"] == manifest["integration"][
        "expected_head_commit"
    ]
    assert report["identities"]["upstream"]["commit"] == manifest["integration"][
        "expected_base_commit"
    ]


def test_strict_release_candidate_requires_exact_integration_parent(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    run_git(repository, "checkout", "main")
    candidate = commit_canonical_manifest(repository, manifest)

    report = audit_release_candidate(repository, candidate)

    assert "release_candidate_parent_mismatch" in finding_codes(report)


def test_strict_release_candidate_allows_only_manifest_tree_change(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    run_git(repository, "checkout", "-b", "release-control")
    manifest_path = repository / CANONICAL_MANIFEST_PATH
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (repository / "unexpected-release-change.txt").write_text(
        "not manifest-only\n", encoding="utf-8"
    )
    run_git(
        repository,
        "add",
        CANONICAL_MANIFEST_PATH,
        "unexpected-release-change.txt",
    )
    run_git(repository, "commit", "-m", "chore(release): invalid candidate")
    candidate = run_git(repository, "rev-parse", "HEAD")

    report = audit_release_candidate(repository, candidate)

    assert "release_candidate_not_manifest_only" in finding_codes(report)


def test_strict_release_candidate_rejects_duplicate_manifest_keys(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    run_git(repository, "checkout", "-b", "release-control")
    manifest_path = repository / CANONICAL_MANIFEST_PATH
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(manifest, sort_keys=True)
    duplicate = encoded.replace(
        '"schema_version": 2',
        '"schema_version": 2, "schema_version": 2',
        1,
    )
    manifest_path.write_text(duplicate + "\n", encoding="utf-8")
    run_git(repository, "add", CANONICAL_MANIFEST_PATH)
    run_git(repository, "commit", "-m", "chore(release): duplicate manifest key")
    candidate = run_git(repository, "rev-parse", "HEAD")

    report = audit_release_candidate(repository, candidate)

    assert "release_manifest_blob_invalid" in finding_codes(report)


def test_strict_release_rejects_undeclared_commit_in_integration_history(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    extra_commit = commit_file(
        repository,
        "undeclared.txt",
        "not in the ledger\n",
        "fix(unrelated): undeclared integration change",
        "2001-01-04T00:00:00+00:00",
    )
    manifest["integration"]["expected_head_commit"] = extra_commit
    run_git(
        repository,
        "push",
        "--force",
        "origin",
        f"{extra_commit}:refs/heads/fork-integration",
    )
    candidate = commit_canonical_manifest(repository, manifest)

    report = audit_release_candidate(repository, candidate)

    assert "integration_history_mismatch" in finding_codes(report)


def test_strict_release_rejects_merge_commit_in_integration_history(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    base = manifest["integration"]["expected_base_commit"]
    run_git(repository, "checkout", "-b", "side-change", base)
    commit_file(
        repository,
        "side.txt",
        "side change\n",
        "fix(side): undeclared merged change",
        "2001-01-03T12:00:00+00:00",
    )
    run_git(repository, "checkout", "fork-integration")
    merge_environment = os.environ.copy()
    merge_environment["GIT_COMMITTER_DATE"] = "2001-01-04T00:00:00+00:00"
    run_git(
        repository,
        "merge",
        "--no-ff",
        "side-change",
        "-m",
        "merge: undeclared side change",
        env=merge_environment,
    )
    merge_head = run_git(repository, "rev-parse", "HEAD")
    manifest["integration"]["expected_head_commit"] = merge_head
    run_git(
        repository,
        "push",
        "--force",
        "origin",
        f"{merge_head}:refs/heads/fork-integration",
    )
    candidate = commit_canonical_manifest(repository, manifest)

    report = audit_release_candidate(repository, candidate)

    assert "integration_history_mismatch" in finding_codes(report)


def test_strict_release_rejects_reordered_required_integration_commits(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    first_component = manifest["components"][0]
    first_final = first_component["patches"][0]["integration"]["commit"]
    run_git(repository, "checkout", "feature/updater")
    second_subject = "fix(updater): preserve updater diagnostics"
    second_source = commit_file(
        repository,
        "native-updater-diagnostics.txt",
        "preserve diagnostics\n",
        second_subject,
        "2001-01-03T00:00:00+00:00",
    )
    second_source_patch_id = stable_patch_id(repository, second_source)
    run_git(
        repository,
        "push",
        "--force",
        "origin",
        "feature/updater:refs/heads/feature/updater",
    )
    run_git(repository, "checkout", "fork-integration")
    integration_environment = os.environ.copy()
    integration_environment["GIT_COMMITTER_DATE"] = "2001-01-04T00:00:00+00:00"
    run_git(repository, "cherry-pick", second_source, env=integration_environment)
    second_final = run_git(repository, "rev-parse", "HEAD")
    second_final_patch_id = stable_patch_id(repository, second_final)
    run_git(
        repository,
        "push",
        "--force",
        "origin",
        f"{second_final}:refs/heads/fork-integration",
    )
    second_component = {
        "id": "native-windows-updater-diagnostics",
        "category": "updater",
        "upstream_status": "required",
        "source": {
            "repository": "fork",
            "ref": "refs/heads/feature/updater",
        },
        "tests": ["tests/updater/test_native_windows_diagnostics.py"],
        "patches": [
            {
                "subject": second_subject,
                "role": "implementation",
                "disposition": "required",
                "source": {
                    "commit": second_source,
                    "stable_patch_id": second_source_patch_id,
                },
                "integration": {
                    "state": "expected",
                    "commit": second_final,
                    "stable_patch_id": second_final_patch_id,
                },
            }
        ],
    }
    manifest["components"] = [second_component, first_component]
    manifest["required_patch_ids"] = [
        second_source_patch_id,
        first_component["patches"][0]["source"]["stable_patch_id"],
    ]
    manifest["integration"]["expected_head_commit"] = second_final
    assert first_final != second_final
    candidate = commit_canonical_manifest(repository, manifest)

    report = audit_release_candidate(repository, candidate)

    assert "integration_history_mismatch" in finding_codes(report)


def test_strict_release_rejects_amended_integration_author_and_message(
    audited_repository: tuple[Path, dict], tmp_path: Path,
):
    repository, manifest = audited_repository
    original = manifest["integration"]["expected_head_commit"]
    tree = run_git(repository, "rev-parse", f"{original}^{{tree}}")
    base = manifest["integration"]["expected_base_commit"]
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_NAME": "Different Author",
            "GIT_AUTHOR_EMAIL": "different@example.test",
            "GIT_AUTHOR_DATE": "2001-01-02T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2001-01-03T00:00:00+00:00",
        }
    )
    amended = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "commit-tree",
            tree,
            "-p",
            base,
            "-m",
            "fix(updater): amended release metadata",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    ).stdout.strip()
    run_git(repository, "update-ref", "refs/heads/fork-integration", amended)
    run_git(
        repository,
        "push",
        "--force",
        "origin",
        f"{amended}:refs/heads/fork-integration",
    )
    patch = manifest["components"][0]["patches"][0]
    patch["integration"]["commit"] = amended
    patch["integration"]["stable_patch_id"] = stable_patch_id(repository, amended)
    manifest["integration"]["expected_head_commit"] = amended
    candidate = commit_canonical_manifest(repository, manifest)

    report = audit_release_candidate(repository, candidate)

    assert "integration_replay_metadata_mismatch" in finding_codes(report)
    target = tmp_path / "unreproducible"
    with pytest.raises(PreparationBlocked) as caught:
        prepare_worktree(manifest, repository, target)
    assert "prepare_replay_metadata_mismatch" in {
        finding.code for finding in caught.value.findings
    }
    assert target.exists() is False


def test_strict_release_ignores_candidate_git_replace_object(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    candidate = commit_canonical_manifest(repository, manifest)
    run_git(
        repository,
        "replace",
        candidate,
        manifest["integration"]["expected_head_commit"],
    )

    report = audit_release_candidate(repository, candidate)

    assert report["ready"] is True
    assert "release_manifest_blob_missing" not in finding_codes(report)


def test_strict_release_rejects_published_ref_mismatch(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    candidate = commit_canonical_manifest(repository, manifest)
    run_git(
        repository,
        "push",
        "--force",
        "origin",
        f"{candidate}:refs/heads/fork-integration",
    )

    report = audit_release_candidate(repository, candidate)

    assert "published_head_mismatch" in finding_codes(report)


def test_strict_release_rejects_advanced_upstream_even_when_base_is_ancestor(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    candidate = commit_canonical_manifest(repository, manifest)
    advanced = manifest["components"][0]["patches"][0]["source"]["commit"]
    run_git(
        repository,
        "push",
        "--force",
        "upstream-test",
        f"{advanced}:refs/heads/main",
    )

    report = audit_release_candidate(repository, candidate)

    assert "upstream_base_mismatch" in finding_codes(report)


def test_strict_release_rejects_unknown_live_published_identity(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    candidate = commit_canonical_manifest(repository, manifest)
    fork = Path(manifest["repositories"]["fork"]["url"])
    run_git(fork, "update-ref", "-d", "refs/heads/fork-integration")

    report = audit_release_candidate(repository, candidate)

    assert "published_head_mismatch" in finding_codes(report)
    assert report["identities"]["published"]["state"] == "unknown"


def test_strict_release_rejects_missing_candidate_manifest_blob(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    candidate = manifest["integration"]["expected_head_commit"]
    run_git(repository, "checkout", "fork-integration")

    report = audit_release_candidate(repository, candidate)

    assert "release_manifest_blob_missing" in finding_codes(report)
    assert report["ready"] is False


def test_strict_release_rejects_dirty_checkout_without_override(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    candidate = commit_canonical_manifest(repository, manifest)
    (repository / "untracked-release-override.json").write_text("{}\n", encoding="utf-8")

    report = audit_release_candidate(repository, candidate)

    assert "release_repository_dirty" in finding_codes(report)


def test_strict_release_rejects_cross_repository_manifest_binding(
    audited_repository: tuple[Path, dict], tmp_path: Path,
):
    repository, manifest = audited_repository
    foreign = tmp_path / "foreign.git"
    subprocess.run(["git", "init", "--bare", str(foreign)], check=True, capture_output=True)
    manifest["repositories"]["fork"]["url"] = str(foreign)
    candidate = commit_canonical_manifest(repository, manifest)

    report = audit_release_candidate(repository, candidate)

    assert "release_repository_unbound" in finding_codes(report)


def test_publication_uses_atomic_expected_old_lease_and_verifies_live_head(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    expected_old = manifest["integration"]["expected_base_commit"]
    expected_new = manifest["integration"]["expected_head_commit"]
    ref = manifest["integration"]["ref"]
    remote_url = manifest["repositories"]["fork"]["url"]
    run_git(
        repository,
        "push",
        "--force",
        "origin",
        f"{expected_old}:{ref}",
    )
    candidate = commit_canonical_manifest(repository, manifest)
    refs_before = run_git(
        repository, "for-each-ref", "--format=%(refname) %(objectname)"
    )
    captured_commands: list[tuple[str, ...]] = []

    def recording_run(command, **kwargs):
        captured_commands.append(tuple(command))
        return subprocess.run(command, **kwargs)

    receipt = publish_release_candidate(
        repository,
        candidate,
        expected_old,
        run=recording_run,
    )

    assert receipt["publication_state"] == "published"
    assert receipt["push"] == {"outcome": "exit_zero", "returncode": 0}
    assert receipt["atomic"] is True
    assert receipt["lease"] == f"{ref}:{expected_old}"
    assert receipt["post_push"] == {
        "ref": ref,
        "commit": expected_new,
        "state": "known",
    }
    push = captured_commands[0]
    assert "--atomic" in push
    assert f"--force-with-lease={ref}:{expected_old}" in push
    assert push.index("--") < push.index(remote_url)
    assert "push_command" not in receipt
    assert remote_url not in json.dumps(receipt)
    assert run_git(repository, "for-each-ref", "--format=%(refname) %(objectname)") == refs_before
    assert run_git(Path(remote_url), "rev-parse", ref) == expected_new


def test_publication_isolates_hostile_config_and_pushes_exactly_one_ref(
    audited_repository: tuple[Path, dict], tmp_path: Path,
):
    repository, manifest = audited_repository
    expected_old = manifest["integration"]["expected_base_commit"]
    expected_new = manifest["integration"]["expected_head_commit"]
    ref = manifest["integration"]["ref"]
    remote_url = manifest["repositories"]["fork"]["url"]
    wrong_remote = tmp_path / "wrong-endpoint.git"
    subprocess.run(
        ["git", "init", "--bare", str(wrong_remote)],
        check=True,
        capture_output=True,
    )
    run_git(
        repository,
        "push",
        "--force",
        "origin",
        f"{expected_old}:{ref}",
    )
    run_git(
        repository,
        "tag",
        "-a",
        "hostile-follow-tag",
        "-m",
        "must not publish",
        expected_new,
    )
    local_helper_marker = tmp_path / "repo-local-helper-ran.txt"
    local_filter_marker = tmp_path / "repo-local-filter-ran.txt"
    hostile_include = tmp_path / "hostile-publish.inc"
    escaped_remote_url = remote_url.replace("\\", "\\\\")
    hostile_include.write_text(
        "[push]\n\tfollowTags = true\n"
        f'[url "{wrong_remote.as_uri()}"]\n'
        f'\tpushInsteadOf = "{escaped_remote_url}"\n'
        "[credential]\n"
        f"\thelper = !echo ran > {local_helper_marker.as_posix()}\n"
        "[filter \"hostile\"]\n"
        f"\tclean = echo ran > {local_filter_marker.as_posix()}\n",
        encoding="utf-8",
    )
    run_git(repository, "config", "push.followTags", "true")
    run_git(
        repository,
        "config",
        f"url.{wrong_remote.as_uri()}.pushInsteadOf",
        remote_url,
    )
    run_git(
        repository,
        "config",
        "credential.helper",
        f"!echo ran > {local_helper_marker.as_posix()}",
    )
    run_git(repository, "config", "include.path", str(hostile_include))
    target_refs_before = run_git(
        Path(remote_url), "for-each-ref", "--format=%(refname) %(objectname)"
    )
    candidate = commit_canonical_manifest(repository, manifest)
    captured: dict[str, object] = {}

    def hostile_global_config(command, **_kwargs):
        if "--system" in command:
            return subprocess.CompletedProcess(command, 1, "", "")
        output = (
            "credential.helper\nfixture\0"
            f"url.{wrong_remote.as_uri()}.pushinsteadof\n{remote_url}\0"
            "push.followtags\ntrue\0"
            f"include.path\n{hostile_include}\0"
        )
        return subprocess.CompletedProcess(command, 0, output, "")

    def recording_push(command, **kwargs):
        captured["command"] = tuple(command)
        captured["transport"] = Path(command[command.index("-C") + 1])
        credential_path = Path(kwargs["env"]["GIT_CONFIG_GLOBAL"])
        captured["credential_path"] = credential_path
        captured["credential_config"] = credential_path.read_text(encoding="utf-8")
        return subprocess.run(command, **kwargs)

    receipt = publish_release_candidate(
        repository,
        candidate,
        expected_old,
        run=recording_push,
        credential_config_run=hostile_global_config,
    )

    command = captured["command"]
    assert isinstance(command, tuple)
    delimiter = command.index("--")
    assert command[delimiter + 1 :] == (
        remote_url,
        f"{expected_new}:{ref}",
    )
    assert "--no-follow-tags" in command
    assert Path(command[command.index("-C") + 1]) != repository
    credential_text = captured["credential_config"]
    assert isinstance(credential_text, str)
    assert "credential" in credential_text
    assert "fixture" in credential_text
    assert "url" not in credential_text
    assert "push" not in credential_text
    assert "include" not in credential_text
    assert receipt["publication_state"] == "published"
    assert receipt["transport"] == {"isolated": True, "cleanup": "complete"}
    assert not Path(captured["transport"]).exists()
    assert not Path(captured["credential_path"]).exists()
    assert local_helper_marker.exists() is False
    assert local_filter_marker.exists() is False
    target_refs_after = run_git(
        Path(remote_url), "for-each-ref", "--format=%(refname) %(objectname)"
    )
    expected_before = target_refs_before.replace(
        f"{ref} {expected_old}", f"{ref} {expected_new}"
    )
    assert target_refs_after == expected_before
    assert run_git(Path(remote_url), "for-each-ref", "refs/tags") == ""
    assert run_git(wrong_remote, "for-each-ref") == ""


@pytest.mark.windows_only
def test_publication_preserves_filtered_global_credential_helper_for_auth_transport(
    audited_repository: tuple[Path, dict],
    tmp_path: Path,
):
    repository, manifest = audited_repository
    expected_old = manifest["integration"]["expected_base_commit"]
    expected_new = manifest["integration"]["expected_head_commit"]
    ref = manifest["integration"]["ref"]
    authenticated_remote = tmp_path / "authenticated-target.git"
    wrong_remote = tmp_path / "credential-wrong-endpoint.git"
    for remote in (authenticated_remote, wrong_remote):
        subprocess.run(
            ["git", "init", "--bare", str(remote)],
            check=True,
            capture_output=True,
        )
    run_git(authenticated_remote, "config", "http.receivepack", "true")
    source_ref = manifest["components"][0]["source"]["ref"]
    run_git(
        repository,
        "push",
        str(authenticated_remote),
        f"{source_ref}:{source_ref}",
        f"{expected_old}:{ref}",
    )

    username = "fixture-user"
    password = "fixture-password"
    expected_authorization = "Basic " + base64.b64encode(
        f"{username}:{password}".encode("utf-8")
    ).decode("ascii")

    class AuthenticatedGitHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._serve_git()

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._serve_git()

        def _serve_git(self) -> None:
            parsed = urlsplit(self.path)
            receive_pack = (
                "service=git-receive-pack" in parsed.query
                or parsed.path.endswith("/git-receive-pack")
            )
            server = self.server
            if receive_pack and self.headers.get("Authorization") != expected_authorization:
                server.unauthorized_requests += 1  # type: ignore[attr-defined]
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="fork-test"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if receive_pack:
                server.authenticated_requests += 1  # type: ignore[attr-defined]
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            environment = os.environ.copy()
            environment.update(
                {
                    "GIT_PROJECT_ROOT": str(tmp_path),
                    "GIT_HTTP_EXPORT_ALL": "1",
                    "PATH_INFO": parsed.path,
                    "QUERY_STRING": parsed.query,
                    "REQUEST_METHOD": self.command,
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": str(length),
                    "REMOTE_USER": username if receive_pack else "",
                    "SERVER_PROTOCOL": self.request_version,
                    "GATEWAY_INTERFACE": "CGI/1.1",
                    "SERVER_NAME": "127.0.0.1",
                    "SERVER_PORT": str(self.server.server_port),
                }
            )
            git_protocol = self.headers.get("Git-Protocol")
            if git_protocol:
                environment["HTTP_GIT_PROTOCOL"] = git_protocol
            completed = subprocess.run(
                ["git", "http-backend"],
                input=body,
                check=False,
                capture_output=True,
                env=environment,
            )
            if completed.returncode != 0:
                self.send_error(500)
                return
            separator = b"\r\n\r\n" if b"\r\n\r\n" in completed.stdout else b"\n\n"
            header_blob, response_body = completed.stdout.split(separator, 1)
            status = 200
            response_headers: list[tuple[str, str]] = []
            for raw_line in header_blob.replace(b"\r\n", b"\n").split(b"\n"):
                if not raw_line:
                    continue
                name, value = raw_line.decode("iso-8859-1").split(":", 1)
                if name.casefold() == "status":
                    status = int(value.strip().split(" ", 1)[0])
                else:
                    response_headers.append((name, value.strip()))
            self.send_response(status)
            for name, value in response_headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(response_body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), AuthenticatedGitHandler)
    server.unauthorized_requests = 0  # type: ignore[attr-defined]
    server.authenticated_requests = 0  # type: ignore[attr-defined]
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    remote_url = (
        f"http://127.0.0.1:{server.server_port}/{authenticated_remote.name}"
    )

    global_helper_marker = tmp_path / "global-helper-ran.txt"
    local_helper_marker = tmp_path / "local-helper-ran.txt"
    include_helper_marker = tmp_path / "include-helper-ran.txt"

    def credential_helper(name: str, marker: Path, user: str, secret: str) -> str:
        helper = tmp_path / f"credential-{name}.py"
        helper.write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n"
            f"print('username={user}')\n"
            f"print('password={secret}')\n",
            encoding="utf-8",
        )
        return f"!{Path(sys.executable).as_posix()} {helper.as_posix()}"

    global_helper = credential_helper(
        "forkfixture", global_helper_marker, username, password
    )
    local_helper = credential_helper(
        "repolocal", local_helper_marker, "wrong", "wrong"
    )
    include_helper = credential_helper(
        "includeevil", include_helper_marker, "wrong", "wrong"
    )
    hostile_include = tmp_path / "credential-hostile.inc"
    hostile_include.write_text(
        f"[credential]\n\thelper = {include_helper}\n"
        f'[url "{wrong_remote.as_uri()}"]\n\tpushInsteadOf = {remote_url}\n',
        encoding="utf-8",
    )
    run_git(repository, "remote", "set-url", "origin", remote_url)
    run_git(repository, "config", "credential.helper", local_helper)
    run_git(repository, "config", "include.path", str(hostile_include))
    run_git(repository, "config", "push.followTags", "true")
    run_git(
        repository,
        "config",
        f"url.{wrong_remote.as_uri()}.pushInsteadOf",
        remote_url,
    )
    manifest["repositories"]["fork"]["url"] = remote_url
    candidate = commit_canonical_manifest(repository, manifest)
    captured_paths: list[Path] = []

    def filtered_global_config(command, **_kwargs):
        if "--system" in command:
            return subprocess.CompletedProcess(command, 1, "", "")
        output = (
            f"credential.helper\n{global_helper}\0"
            f"url.{wrong_remote.as_uri()}.pushinsteadof\n{remote_url}\0"
            "push.followtags\ntrue\0"
            f"include.path\n{hostile_include}\0"
        )
        return subprocess.CompletedProcess(command, 0, output, "")

    def recording_push(command, **kwargs):
        captured_paths.extend(
            [
                Path(command[command.index("-C") + 1]),
                Path(kwargs["env"]["GIT_CONFIG_GLOBAL"]),
            ]
        )
        completed = subprocess.run(command, **kwargs)
        assert completed.returncode == 0, completed.stderr
        return completed

    try:
        receipt = publish_release_candidate(
            repository,
            candidate,
            expected_old,
            run=recording_push,
            credential_config_run=filtered_global_config,
        )
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=10)

    assert receipt["publication_state"] == "published"
    assert server.unauthorized_requests > 0  # type: ignore[attr-defined]
    assert server.authenticated_requests > 0  # type: ignore[attr-defined]
    assert global_helper_marker.exists()
    assert local_helper_marker.exists() is False
    assert include_helper_marker.exists() is False
    assert run_git(authenticated_remote, "rev-parse", ref) == expected_new
    assert run_git(authenticated_remote, "for-each-ref", "refs/tags") == ""
    assert run_git(wrong_remote, "for-each-ref") == ""
    assert captured_paths and all(not path.exists() for path in captured_paths)
    encoded_receipt = json.dumps(receipt)
    assert remote_url not in encoded_receipt
    assert username not in encoded_receipt
    assert password not in encoded_receipt


def test_publication_expected_old_mismatch_blocks_before_push(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    remote_url = manifest["repositories"]["fork"]["url"]
    published_before = run_git(Path(remote_url), "rev-parse", manifest["integration"]["ref"])
    candidate = commit_canonical_manifest(repository, manifest)

    with pytest.raises(PublicationBlocked) as caught:
        publish_release_candidate(
            repository,
            candidate,
            manifest["integration"]["expected_base_commit"],
        )

    assert "published_expected_old_mismatch" in finding_codes(caught.value.report)
    assert run_git(Path(remote_url), "rev-parse", manifest["integration"]["ref"]) == published_before


def test_publication_block_report_redacts_credentialed_manifest_url(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    secret_url = "https://release-user:do-not-print@example.test/Owner/Fork.git"
    manifest["repositories"]["fork"]["url"] = secret_url
    candidate = commit_canonical_manifest(repository, manifest)

    with pytest.raises(PublicationBlocked) as caught:
        publish_release_candidate(
            repository,
            candidate,
            manifest["integration"]["expected_head_commit"],
        )

    encoded = json.dumps(caught.value.report)
    assert secret_url not in encoded
    assert "do-not-print" not in encoded
    assert "unsafe_repository_url" in finding_codes(caught.value.report)


def test_publication_timeout_before_update_remains_unknown(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    expected_old = manifest["integration"]["expected_base_commit"]
    remote_url = manifest["repositories"]["fork"]["url"]
    ref = manifest["integration"]["ref"]
    run_git(
        repository,
        "push",
        "--force",
        "origin",
        f"{expected_old}:{ref}",
    )
    candidate = commit_canonical_manifest(repository, manifest)

    push_calls = 0

    def time_out(command, **_kwargs):
        nonlocal push_calls
        push_calls += 1
        raise subprocess.TimeoutExpired(command, 120)

    with pytest.raises(PublicationFailed) as caught:
        publish_release_candidate(
            repository, candidate, expected_old, run=time_out
        )

    assert str(caught.value) == "publication outcome remains unknown after push timeout"
    assert caught.value.receipt["publication_state"] == "unknown"
    assert caught.value.receipt["push"] == {
        "outcome": "timeout",
        "returncode": None,
    }
    assert caught.value.receipt["post_push"] == {
        "ref": ref,
        "commit": expected_old,
        "state": "known",
    }
    assert push_calls == 1
    assert remote_url not in json.dumps(caught.value.receipt)
    assert run_git(Path(remote_url), "rev-parse", ref) == expected_old


def test_publication_timeout_after_remote_update_reconciles_as_published(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    expected_old = manifest["integration"]["expected_base_commit"]
    expected_new = manifest["integration"]["expected_head_commit"]
    remote_url = manifest["repositories"]["fork"]["url"]
    ref = manifest["integration"]["ref"]
    run_git(
        repository,
        "push",
        "--force",
        "origin",
        f"{expected_old}:{ref}",
    )
    candidate = commit_canonical_manifest(repository, manifest)
    push_calls = 0

    def push_then_time_out(command, **kwargs):
        nonlocal push_calls
        push_calls += 1
        completed = subprocess.run(command, **kwargs)
        assert completed.returncode == 0
        raise subprocess.TimeoutExpired(command, 120)

    receipt = publish_release_candidate(
        repository,
        candidate,
        expected_old,
        run=push_then_time_out,
    )

    assert receipt["publication_state"] == "published"
    assert receipt["push"] == {"outcome": "timeout", "returncode": None}
    assert receipt["post_push"] == {
        "ref": ref,
        "commit": expected_new,
        "state": "known",
    }
    assert push_calls == 1
    assert remote_url not in json.dumps(receipt)
    assert run_git(Path(remote_url), "rev-parse", ref) == expected_new


def test_publication_timeout_old_sample_stays_unknown_when_update_arrives_late(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    expected_old = manifest["integration"]["expected_base_commit"]
    expected_new = manifest["integration"]["expected_head_commit"]
    remote_url = manifest["repositories"]["fork"]["url"]
    ref = manifest["integration"]["ref"]
    run_git(
        repository,
        "push",
        "--force",
        "origin",
        f"{expected_old}:{ref}",
    )
    candidate = commit_canonical_manifest(repository, manifest)
    release_update = threading.Event()
    update_finished = threading.Event()
    push_calls = 0

    def publish_late() -> None:
        if release_update.wait(timeout=10):
            run_git(
                repository,
                "push",
                "--force",
                "origin",
                f"{expected_new}:{ref}",
            )
            update_finished.set()

    worker = threading.Thread(target=publish_late, daemon=True)
    worker.start()

    def time_out(command, **_kwargs):
        nonlocal push_calls
        push_calls += 1
        raise subprocess.TimeoutExpired(command, 120)

    def observe_old_then_release(command, **kwargs):
        completed = subprocess.run(command, **kwargs)
        release_update.set()
        return completed

    with pytest.raises(PublicationFailed) as caught:
        publish_release_candidate(
            repository,
            candidate,
            expected_old,
            run=time_out,
            reconcile_run=observe_old_then_release,
        )

    assert caught.value.receipt["publication_state"] == "unknown"
    assert caught.value.receipt["post_push"]["commit"] == expected_old
    assert push_calls == 1
    assert update_finished.wait(timeout=10)
    worker.join(timeout=10)
    assert run_git(Path(remote_url), "rev-parse", ref) == expected_new


def test_publication_reconciliation_detects_third_party_ref_move(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    expected_old = manifest["integration"]["expected_base_commit"]
    remote_url = manifest["repositories"]["fork"]["url"]
    ref = manifest["integration"]["ref"]
    run_git(
        repository,
        "push",
        "--force",
        "origin",
        f"{expected_old}:{ref}",
    )
    candidate = commit_canonical_manifest(repository, manifest)
    push_calls = 0

    def move_ref_then_fail(command, **_kwargs):
        nonlocal push_calls
        push_calls += 1
        run_git(repository, "push", "--force", "origin", f"{candidate}:{ref}")
        return subprocess.CompletedProcess(command, 1, "", "simulated push failure")

    with pytest.raises(PublicationFailed) as caught:
        publish_release_candidate(
            repository,
            candidate,
            expected_old,
            run=move_ref_then_fail,
        )

    assert str(caught.value) == "publication conflict: live ref moved to a third commit"
    assert caught.value.receipt["publication_state"] == "conflict"
    assert caught.value.receipt["push"] == {
        "outcome": "exit_nonzero",
        "returncode": 1,
    }
    assert caught.value.receipt["post_push"]["commit"] == candidate
    assert push_calls == 1
    assert remote_url not in json.dumps(caught.value.receipt)


def test_publication_verification_network_failure_is_unknown_and_redacted(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    expected_old = manifest["integration"]["expected_base_commit"]
    expected_new = manifest["integration"]["expected_head_commit"]
    remote_url = manifest["repositories"]["fork"]["url"]
    ref = manifest["integration"]["ref"]
    run_git(
        repository,
        "push",
        "--force",
        "origin",
        f"{expected_old}:{ref}",
    )
    candidate = commit_canonical_manifest(repository, manifest)
    push_calls = 0
    reconciliation_calls = 0

    def recording_push(command, **kwargs):
        nonlocal push_calls
        push_calls += 1
        return subprocess.run(command, **kwargs)

    def verification_timeout(command, **_kwargs):
        nonlocal reconciliation_calls
        reconciliation_calls += 1
        raise subprocess.TimeoutExpired(
            command,
            30,
            stderr=f"credential-like diagnostic for {remote_url}",
        )

    with pytest.raises(PublicationFailed) as caught:
        publish_release_candidate(
            repository,
            candidate,
            expected_old,
            run=recording_push,
            reconcile_run=verification_timeout,
        )

    assert str(caught.value) == (
        "publication outcome is unknown because live ref reconciliation failed"
    )
    assert caught.value.receipt["publication_state"] == "unknown"
    assert caught.value.receipt["push"] == {
        "outcome": "exit_zero",
        "returncode": 0,
    }
    assert caught.value.receipt["post_push"] == {
        "ref": ref,
        "commit": "unknown",
        "state": "unknown",
    }
    assert push_calls == 1
    assert reconciliation_calls == 1
    assert remote_url not in json.dumps(caught.value.receipt)
    assert "credential-like" not in json.dumps(caught.value.receipt)
    assert run_git(Path(remote_url), "rev-parse", ref) == expected_new


def test_publication_requires_post_push_live_sha_verification(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    expected_old = manifest["integration"]["expected_base_commit"]
    remote_url = manifest["repositories"]["fork"]["url"]
    ref = manifest["integration"]["ref"]
    run_git(
        repository,
        "push",
        "--force",
        "origin",
        f"{expected_old}:{ref}",
    )
    candidate = commit_canonical_manifest(repository, manifest)

    def false_success(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(PublicationFailed) as caught:
        publish_release_candidate(
            repository, candidate, expected_old, run=false_success
        )

    assert str(caught.value) == "publication was reconciled at the expected-old commit"
    assert caught.value.receipt["publication_state"] == "not_published"
    assert caught.value.receipt["post_push"]["commit"] == expected_old
    assert run_git(Path(remote_url), "rev-parse", ref) == expected_old


def test_unbound_ready_urls_are_rejected_without_live_observation(
    audited_repository: tuple[Path, dict], tmp_path: Path,
):
    repository, manifest = audited_repository
    foreign = tmp_path / "foreign.git"
    subprocess.run(["git", "init", "--bare", str(foreign)], check=True, capture_output=True)
    manifest["repositories"]["foreign"] = {"url": str(foreign)}
    manifest["components"][0]["source"]["repository"] = "foreign"
    probe = GitProbe(repository)
    live_calls: list[tuple[str | None, str | None]] = []
    real_live_ref = probe.live_ref

    def recording_live_ref(url: str | None, ref: str | None) -> dict:
        live_calls.append((url, ref))
        return real_live_ref(url, ref)

    probe.live_ref = recording_live_ref  # type: ignore[method-assign]

    report = audit_manifest(
        manifest,
        repository,
        observe_live=True,
        probe=probe,
        strict_release=True,
    )

    assert "source_repository_unbound" in finding_codes(report)
    assert all(url != str(foreign) for url, _ref in live_calls)
    target = tmp_path / "unbound-prepare"
    with pytest.raises(PreparationBlocked):
        prepare_worktree(manifest, repository, target, dry_run=True, probe=probe)
    assert all(url != str(foreign) for url, _ref in live_calls)
    assert target.exists() is False


def test_review_draft_never_contacts_unbound_manifest_urls(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    manifest["manifest_state"] = "review_required"
    manifest["repositories"]["upstream"]["url"] = (
        "https://unbound.example.test/Owner/Upstream.git"
    )
    manifest["repositories"]["fork"]["url"] = (
        "https://unbound.example.test/Owner/Fork.git"
    )
    probe = GitProbe(repository)
    live_calls: list[tuple[str | None, str | None]] = []

    def recording_live_ref(url: str | None, ref: str | None) -> dict:
        live_calls.append((url, ref))
        return {"repository": url, "ref": ref, "commit": "f" * 40, "state": "known"}

    probe.live_ref = recording_live_ref  # type: ignore[method-assign]

    report = audit_manifest(manifest, repository, observe_live=True, probe=probe)

    assert live_calls == []
    assert report["identities"]["upstream"]["state"] == "unknown"
    assert report["identities"]["published"]["state"] == "unknown"


@pytest.mark.windows_only
def test_circular_source_detection_uses_canonical_windows_repository_identity(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    fork_path = Path(manifest["repositories"]["fork"]["url"])
    manifest["repositories"]["fork-file-uri"] = {"url": fork_path.as_uri()}
    manifest["components"][0]["source"] = {
        "repository": "fork-file-uri",
        "ref": manifest["integration"]["ref"],
    }

    report = audit_manifest(manifest, repository)

    assert "source_target_same_ref" in finding_codes(report)


def test_prepare_reconstructs_in_new_clone_without_changing_caller_refs(
    audited_repository: tuple[Path, dict], tmp_path: Path,
):
    repository, manifest = audited_repository
    target = tmp_path / "prepared"
    refs_before = run_git(repository, "for-each-ref", "--format=%(refname) %(objectname)")

    receipt = prepare_worktree(manifest, repository, target)

    assert receipt["prepared"] is True
    assert receipt["push_performed"] is False
    assert receipt["caller_checkout_touched"] is False
    assert receipt["applied"][0]["prepared_patch_id"] == manifest["required_patch_ids"][0]
    assert receipt["prepared_head"] == manifest["integration"]["expected_head_commit"]
    assert receipt["publication"]["ref"] == manifest["integration"]["ref"]
    assert receipt["publication"]["expected_old_commit"] == manifest["integration"][
        "expected_head_commit"
    ]
    assert receipt["publication"]["expected_new_commit"] == manifest["integration"][
        "expected_head_commit"
    ]
    assert run_git(target, "status", "--short") == ""
    assert run_git(repository, "for-each-ref", "--format=%(refname) %(objectname)") == refs_before


def test_prepare_records_live_expected_old_publication_sha_without_requiring_new_head(
    audited_repository: tuple[Path, dict], tmp_path: Path,
):
    repository, manifest = audited_repository
    old = manifest["integration"]["expected_base_commit"]
    run_git(
        repository,
        "push",
        "--force",
        "origin",
        f"{old}:refs/heads/fork-integration",
    )

    receipt = prepare_worktree(
        manifest, repository, tmp_path / "prepared-old-receipt", dry_run=True
    )

    assert receipt["publication"] == {
        "repository_identity": canonical_repository_identity(
            manifest["repositories"]["fork"]["url"], base=repository
        ),
        "ref": "refs/heads/fork-integration",
        "expected_old_commit": old,
        "expected_new_commit": manifest["integration"]["expected_head_commit"],
    }


def test_prepare_rejects_unbound_integration_repository_without_contacting_it(
    audited_repository: tuple[Path, dict], tmp_path: Path,
):
    repository, manifest = audited_repository
    foreign = tmp_path / "foreign-integration.git"
    subprocess.run(["git", "init", "--bare", str(foreign)], check=True, capture_output=True)
    manifest["repositories"]["fork"]["url"] = str(foreign)
    probe = GitProbe(repository)
    live_calls: list[tuple[str | None, str | None]] = []
    real_live_ref = probe.live_ref

    def recording_live_ref(url: str | None, ref: str | None) -> dict:
        live_calls.append((url, ref))
        return real_live_ref(url, ref)

    probe.live_ref = recording_live_ref  # type: ignore[method-assign]
    target = tmp_path / "unbound-integration"

    with pytest.raises(PreparationBlocked) as caught:
        prepare_worktree(manifest, repository, target, dry_run=True, probe=probe)

    assert "prepare_integration_repository_unbound" in {
        finding.code for finding in caught.value.findings
    }
    assert all(url != str(foreign) for url, _ref in live_calls)
    assert target.exists() is False


def test_prepare_blocks_malformed_required_integration_identity_structurally(
    audited_repository: tuple[Path, dict], tmp_path: Path,
):
    repository, manifest = audited_repository
    manifest["components"][0]["patches"][0]["integration"] = []
    target = tmp_path / "malformed-final-identity"

    report = audit_manifest(manifest, repository, observe_live=False)
    with pytest.raises(PreparationBlocked) as caught:
        prepare_worktree(manifest, repository, target)

    assert "missing_integration_identity" in finding_codes(report)
    assert "missing_integration_identity" in {
        finding.code for finding in caught.value.findings
    }
    assert target.exists() is False


def test_prepare_timeout_returns_structured_failed_receipt(
    audited_repository: tuple[Path, dict],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repository, manifest = audited_repository
    target = tmp_path / "timed-out-prepare"

    def time_out(command, **_kwargs):
        raise subprocess.TimeoutExpired(command, 120)

    monkeypatch.setattr(prepare_module.subprocess, "run", time_out)

    with pytest.raises(PreparationFailed) as caught:
        prepare_worktree(manifest, repository, target)

    assert str(caught.value) == "git mutation timed out"
    assert caught.value.receipt["prepared"] is False
    assert caught.value.receipt["publication"]["expected_old_commit"] == manifest[
        "integration"
    ]["expected_head_commit"]


def test_review_updater_chain_finalizes_to_verified_replacement_only(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    original_manifest = deepcopy(manifest)
    component = manifest["components"][0]
    old_patch = component["patches"][0]
    component["upstream_status"] = "review_required"
    component["intended_upstream_status"] = "required"
    old_patch["disposition"] = "review_required"
    old_patch["integration"] = {
        "state": "pending",
        "commit": None,
        "stable_patch_id": None,
    }
    manifest["manifest_state"] = "review_required"
    manifest["required_patch_ids"] = []

    run_git(repository, "checkout", "feature/updater")
    replacement_subject = "fix(updater): coherent native lifecycle replacement"
    replacement_source = commit_file(
        repository,
        "coherent-lifecycle.txt",
        "coherent replacement\n",
        replacement_subject,
        "2001-01-04T00:00:00+00:00",
    )
    run_git(
        repository,
        "push",
        "--force",
        "origin",
        "feature/updater:refs/heads/feature/updater",
    )
    base = manifest["integration"]["expected_base_commit"]
    run_git(repository, "checkout", "-b", "replacement-integration", base)
    integration_environment = os.environ.copy()
    integration_environment["GIT_COMMITTER_DATE"] = "2001-01-05T00:00:00+00:00"
    run_git(repository, "cherry-pick", replacement_source, env=integration_environment)
    replacement_final = run_git(repository, "rev-parse", "HEAD")
    run_git(
        repository,
        "branch",
        "--force",
        "fork-integration",
        replacement_final,
    )
    manifest["integration"]["expected_head_commit"] = replacement_final

    finalized = finalize_component_replacement(
        manifest,
        repository,
        component_id="native-windows-updater",
        source_ref="refs/heads/feature/updater",
        replacements=[
            {
                "source_commit": replacement_source,
                "integration_commit": replacement_final,
                "role": "implementation",
            }
        ],
    )

    finalized_component = finalized["components"][0]
    historical, replacement = finalized_component["patches"]
    assert historical["disposition"] == "folded"
    assert historical["integration"] == {
        "state": "not_replayed",
        "commit": None,
        "stable_patch_id": None,
    }
    assert historical["related_to"] == replacement_source
    assert replacement["disposition"] == "required"
    assert replacement["integration"]["state"] == "expected"
    assert replacement["source"]["stable_patch_id"] == replacement["integration"][
        "stable_patch_id"
    ]
    assert finalized["required_patch_ids"] == [
        replacement["source"]["stable_patch_id"]
    ]
    assert old_patch["disposition"] == "review_required"
    assert original_manifest["components"][0]["patches"][0]["disposition"] == "required"


def test_replacement_finalizer_rejects_missing_final_identity_without_guessing(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    component = manifest["components"][0]
    component["upstream_status"] = "review_required"
    component["intended_upstream_status"] = "required"
    component["patches"][0]["disposition"] = "review_required"
    component["patches"][0]["integration"] = {
        "state": "pending",
        "commit": None,
        "stable_patch_id": None,
    }
    manifest["manifest_state"] = "review_required"
    manifest["required_patch_ids"] = []
    source_commit = component["patches"][0]["source"]["commit"]

    with pytest.raises(ReplacementFinalizationBlocked) as caught:
        finalize_component_replacement(
            manifest,
            repository,
            component_id="native-windows-updater",
            source_ref="refs/heads/feature/updater",
            replacements=[
                {
                    "source_commit": source_commit,
                    "integration_commit": None,
                    "role": "implementation",
                }
            ],
        )

    assert "replacement_identity_invalid" in {
        finding.code for finding in caught.value.findings
    }
    assert component["patches"][0]["integration"]["commit"] is None


def test_replacement_finalizer_rejects_integration_lineage_as_source(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    component = manifest["components"][0]
    component["upstream_status"] = "review_required"
    component["intended_upstream_status"] = "required"
    component["patches"][0]["disposition"] = "review_required"
    component["patches"][0]["integration"] = {
        "state": "pending",
        "commit": None,
        "stable_patch_id": None,
    }
    manifest["manifest_state"] = "review_required"
    manifest["required_patch_ids"] = []
    final_commit = manifest["integration"]["expected_head_commit"]

    with pytest.raises(ReplacementFinalizationBlocked) as caught:
        finalize_component_replacement(
            manifest,
            repository,
            component_id="native-windows-updater",
            source_ref=manifest["integration"]["ref"],
            replacements=[
                {
                    "source_commit": final_commit,
                    "integration_commit": final_commit,
                    "role": "implementation",
                }
            ],
        )

    assert "replacement_source_is_integration" in {
        finding.code for finding in caught.value.findings
    }
    assert "replacement_source_final_same" in {
        finding.code for finding in caught.value.findings
    }


def test_replacement_finalizer_rejects_source_ref_at_earlier_integration_commit(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    component = manifest["components"][0]
    component["upstream_status"] = "review_required"
    component["intended_upstream_status"] = "required"
    component["patches"][0]["disposition"] = "review_required"
    component["patches"][0]["integration"] = {
        "state": "pending",
        "commit": None,
        "stable_patch_id": None,
    }
    manifest["manifest_state"] = "review_required"
    manifest["required_patch_ids"] = []
    base = manifest["integration"]["expected_base_commit"]
    final_commit = manifest["integration"]["expected_head_commit"]
    run_git(repository, "branch", "earlier-integration-source", base)

    with pytest.raises(ReplacementFinalizationBlocked) as caught:
        finalize_component_replacement(
            manifest,
            repository,
            component_id="native-windows-updater",
            source_ref="refs/heads/earlier-integration-source",
            replacements=[
                {
                    "source_commit": base,
                    "integration_commit": final_commit,
                    "role": "implementation",
                }
            ],
        )

    codes = {finding.code for finding in caught.value.findings}
    assert "replacement_source_is_integration" in codes
    assert "replacement_source_integration_lineage" in codes


@pytest.mark.parametrize("omission", ["category", "patch"])
def test_prepare_blocks_manifest_omission_before_creating_target(
    audited_repository: tuple[Path, dict], tmp_path: Path, omission: str,
):
    repository, manifest = audited_repository
    target = tmp_path / f"blocked-{omission}"
    refs_before = run_git(repository, "for-each-ref", "--format=%(refname) %(objectname)")
    if omission == "category":
        manifest["components"][0]["category"] = "desktop"
    else:
        manifest["required_patch_ids"].append("f" * 40)

    with pytest.raises(PreparationBlocked) as caught:
        prepare_worktree(manifest, repository, target)

    assert target.exists() is False
    assert run_git(repository, "for-each-ref", "--format=%(refname) %(objectname)") == refs_before
    expected_code = "missing_required_category" if omission == "category" else "required_patch_missing"
    assert expected_code in {finding.code for finding in caught.value.findings}


def test_prepare_blocks_unrelated_required_patches_before_creating_target(
    audited_repository: tuple[Path, dict], tmp_path: Path,
):
    repository, manifest = audited_repository
    run_git(repository, "checkout", "feature/updater")
    unrelated_commit = commit_file(
        repository,
        "scripts/bootstrap-marker.txt",
        "bootstrap\n",
        "fix(bootstrap): pin installer source",
        "2001-01-04T00:00:00+00:00",
    )
    unrelated_patch_id = stable_patch_id(repository, unrelated_commit)
    manifest["required_patch_ids"].append(unrelated_patch_id)
    manifest["components"][0]["patches"].append(
        {
            "subject": "fix(bootstrap): pin installer source",
            "role": "implementation",
            "disposition": "required",
            "source": {
                "commit": unrelated_commit,
                "stable_patch_id": unrelated_patch_id,
            },
            "integration": {
                "state": "expected",
                "commit": None,
                "stable_patch_id": unrelated_patch_id,
            },
        }
    )
    target = tmp_path / "blocked-unrelated"

    with pytest.raises(PreparationBlocked) as caught:
        prepare_worktree(manifest, repository, target)

    assert target.exists() is False
    assert "unrelated_patches_grouped" in {
        finding.code for finding in caught.value.findings
    }


def test_prepare_skips_absorbed_patch_by_stable_identity(
    audited_repository: tuple[Path, dict], tmp_path: Path,
):
    repository, manifest = audited_repository
    base = manifest["integration"]["expected_base_commit"]
    base_patch_id = stable_patch_id(repository, base)
    manifest["components"].append(
        {
            "id": "upstream-absorbed-core",
            "category": "compression",
            "upstream_status": "absorbed",
            "source": {"repository": "upstream", "ref": "refs/heads/main"},
            "tests": ["tests/test_absorbed.py"],
            "patches": [
                {
                    "subject": "chore: base",
                    "role": "implementation",
                    "disposition": "absorbed_upstream",
                    "source": {"commit": base, "stable_patch_id": base_patch_id},
                    "integration": {
                        "state": "not_replayed",
                        "commit": None,
                        "stable_patch_id": None,
                    },
                }
            ],
        }
    )

    refs_before = run_git(repository, "for-each-ref", "--format=%(refname) %(objectname)")
    probe = GitProbe(repository)
    receipt = prepare_worktree(
        manifest,
        repository,
        tmp_path / "dry-run-target",
        dry_run=True,
        probe=probe,
    )

    assert receipt["prepared"] is False
    assert receipt["writes"] == []
    assert receipt["skipped"] == [
        {
            "component": "upstream-absorbed-core",
            "source_commit": base,
            "stable_patch_id": base_patch_id,
            "reason": "absorbed_stable_patch_id_present",
        }
    ]
    assert (tmp_path / "dry-run-target").exists() is False
    assert run_git(repository, "for-each-ref", "--format=%(refname) %(objectname)") == refs_before
    assert all("fetch" not in command for command in probe.commands)


def test_absorbed_patch_proof_rejects_apply_then_revert_current_tree(
    audited_repository: tuple[Path, dict],
):
    repository, manifest = audited_repository
    source_commit = manifest["components"][0]["patches"][0]["source"]["commit"]
    run_git(repository, "checkout", "-b", "reverted-upstream", source_commit)
    run_git(repository, "revert", "--no-edit", source_commit)
    reverted_head = run_git(repository, "rev-parse", "HEAD")

    assert GitProbe(repository).patch_present(reverted_head, source_commit) is False


def test_absorbed_patch_proof_accepts_equivalent_root_commit(
    tmp_path: Path,
):
    repository = tmp_path / "equivalent-root"
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repository)],
        check=True,
        capture_output=True,
    )
    run_git(repository, "config", "user.email", "tests@example.test")
    run_git(repository, "config", "user.name", "Manifest Tests")
    source = commit_file(
        repository,
        "root.txt",
        "root behavior\n",
        "fix(root): retain behavior",
        "2001-01-01T00:00:00+00:00",
    )
    tree = run_git(repository, "rev-parse", f"{source}^{{tree}}")
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_DATE": "2001-01-02T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2001-01-02T00:00:00+00:00",
        }
    )
    equivalent = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "commit-tree",
            tree,
            "-m",
            "fix(root): equivalent root metadata",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    ).stdout.strip()

    probe = GitProbe(repository)
    assert probe.stable_patch_id(source) == probe.stable_patch_id(equivalent)
    assert probe.patch_present(equivalent, source) is True
