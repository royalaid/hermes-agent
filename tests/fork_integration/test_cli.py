from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from fork_integration.audit import GitProbe
from fork_integration.cli import main


def run_git(repository: Path, *args: str, env: dict[str, str] | None = None) -> str:
    process_environment = os.environ.copy()
    if env:
        process_environment.update(env)
    completed = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=process_environment,
    )
    return completed.stdout.strip()


def commit_file(
    repository: Path,
    relative_path: str,
    content: str,
    subject: str,
) -> str:
    (repository / relative_path).write_bytes(content.encode("utf-8"))
    run_git(repository, "add", "--", relative_path)
    run_git(repository, "commit", "-m", subject)
    return run_git(repository, "rev-parse", "HEAD")


@pytest.fixture
def finalization_cli_repository(
    tmp_path: Path,
) -> tuple[Path, Path, dict[str, str]]:
    repository = tmp_path / "finalization-repository"
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repository)],
        check=True,
        capture_output=True,
    )
    run_git(repository, "config", "user.email", "tests@example.test")
    run_git(repository, "config", "user.name", "Manifest Tests")
    run_git(repository, "config", "core.autocrlf", "false")
    base = commit_file(repository, "base.txt", "base\n", "chore: base")
    run_git(repository, "checkout", "-b", "feature/updater")
    historical = commit_file(
        repository,
        "historical.txt",
        "historical repair\n",
        "fix(updater): historical repair",
    )
    source = commit_file(
        repository,
        "replacement.txt",
        "coherent replacement\n",
        "fix(updater): coherent lifecycle replacement",
    )
    run_git(repository, "checkout", "-b", "fork-integration", base)
    run_git(repository, "cherry-pick", source)
    integration = run_git(repository, "rev-parse", "HEAD")
    probe = GitProbe(repository)
    historical_patch_id = probe.stable_patch_id(historical)
    assert historical_patch_id is not None
    manifest = {
        "schema_version": 2,
        "manifest_state": "review_required",
        "repositories": {
            "upstream": {"url": "https://example.test/upstream.git"},
            "fork": {"url": "https://example.test/fork.git"},
        },
        "integration": {
            "repository": "fork",
            "ref": "refs/heads/fork-integration",
            "upstream_repository": "upstream",
            "upstream_ref": "refs/heads/main",
            "expected_base_commit": base,
            "expected_head_commit": integration,
        },
        "required_categories": ["updater"],
        "required_patch_ids": [],
        "components": [
            {
                "id": "native-windows-updater-mcp",
                "category": "updater",
                "upstream_status": "review_required",
                "intended_upstream_status": "required",
                "source": {"repository": "fork", "ref": None},
                "tests": ["tests/updater/test_lifecycle.py"],
                "review_notes": "Awaiting coherent immutable replacements.",
                "patches": [
                    {
                        "subject": "fix(updater): historical repair",
                        "role": "implementation",
                        "disposition": "review_required",
                        "source": {
                            "commit": historical,
                            "stable_patch_id": historical_patch_id,
                        },
                        "integration": {
                            "state": "pending",
                            "commit": None,
                            "stable_patch_id": None,
                        },
                    }
                ],
            }
        ],
    }
    manifest_path = tmp_path / "manifest.v2.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return repository, manifest_path, {
        "historical": historical,
        "source": source,
        "integration": integration,
    }


def test_schema_1_cli_migration_prints_only_and_does_not_create_output(
    tmp_path: Path, capsys,
):
    source_commit = "1" * 40
    source_patch = "2" * 40
    legacy = tmp_path / "legacy.json"
    legacy.write_text(
        json.dumps(
            {
                "schema": 1,
                "integration_branch": "fork-integration",
                "upstream": {"remote": "origin", "ref": "refs/heads/main"},
                "fork": {"remote": "fork", "repository": "owner/repository"},
                "components": [
                    {
                        "id": "legacy-feature",
                        "source_ref": "fork/feature",
                        "patches": [
                            {
                                "commit": source_commit,
                                "stable_patch_id": source_patch,
                                "subject": "fix(feature): retain behavior",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    exit_code = main(["--migrate-schema-1", str(legacy), "--json"])
    draft = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert draft["manifest_state"] == "review_required"
    assert draft["components"][0]["patches"][0]["integration"]["commit"] is None
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before


def test_release_launcher_disables_repo_bytecode_writes(tmp_path: Path):
    repository_root = Path(__file__).resolve().parents[2]
    isolated_root = tmp_path / "isolated-tool"
    package = isolated_root / "fork_integration"
    scripts = isolated_root / "scripts"
    shutil.copytree(
        repository_root / "fork_integration",
        package,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    scripts.mkdir(parents=True)
    shutil.copy2(
        repository_root / "scripts" / "fork_integration_release.py",
        scripts / "fork_integration_release.py",
    )
    legacy = isolated_root / "legacy.json"
    legacy.write_text(
        json.dumps(
            {
                "schema": 1,
                "components": [],
                "upstream": {},
                "fork": {},
            }
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(scripts / "fork_integration_release.py"),
            "--migrate-schema-1",
            str(legacy),
            "--json",
        ],
        cwd=isolated_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stderr
    assert not list(isolated_root.rglob("__pycache__"))
    assert not list(isolated_root.rglob("*.pyc"))


@pytest.mark.parametrize(
    "override_args",
    [
        ["--offline"],
        ["--manifest", "alternate.json"],
        ["--installed-repo", "installed"],
    ],
)
def test_release_mode_rejects_manifest_offline_and_second_repo_overrides(
    tmp_path: Path, capsys, override_args: list[str],
):
    exit_code = main(
        [
            "--release-candidate",
            "1" * 40,
            "--repo",
            str(tmp_path),
            "--json",
            *override_args,
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert output["ready"] is False
    assert "strict release mode forbids override options" in output["error"]


def test_status_json_reports_malformed_collection_without_traceback(
    tmp_path: Path, capsys,
):
    repository_root = Path(__file__).resolve().parents[2]
    manifest = json.loads(
        (
            repository_root
            / "fork_integration"
            / "hermes-fork-manifest.v2.json"
        ).read_text(encoding="utf-8")
    )
    manifest["components"][0]["patches"] = 42
    malformed = tmp_path / "malformed.json"
    malformed.write_text(json.dumps(manifest), encoding="utf-8")

    exit_code = main(
        [
            "--status",
            "--manifest",
            str(malformed),
            "--repo",
            str(repository_root),
            "--offline",
            "--json",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert output["ready"] is False
    assert "missing_patches" in {
        finding["code"] for finding in output["findings"]
    }


def test_finalize_component_cli_outputs_verified_manifest_without_writing(
    finalization_cli_repository: tuple[Path, Path, dict[str, str]], capsys,
):
    repository, manifest_path, identities = finalization_cli_repository
    manifest_before = manifest_path.read_bytes()
    refs_before = run_git(
        repository, "for-each-ref", "--format=%(refname) %(objectname)"
    )

    exit_code = main(
        [
            "--finalize-component",
            "native-windows-updater-mcp",
            "--manifest",
            str(manifest_path),
            "--repo",
            str(repository),
            "--source-ref",
            "refs/heads/feature/updater",
            "--replacement",
            f"{identities['source']}:{identities['integration']}:implementation",
        ]
    )
    captured = capsys.readouterr()
    finalized = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    component = finalized["components"][0]
    historical, replacement = component["patches"]
    assert component["upstream_status"] == "required"
    assert component["source"]["ref"] == "refs/heads/feature/updater"
    assert historical["disposition"] == "folded"
    assert historical["integration"]["state"] == "not_replayed"
    assert replacement["source"]["commit"] == identities["source"]
    assert replacement["integration"]["commit"] == identities["integration"]
    assert finalized["required_patch_ids"] == [
        replacement["source"]["stable_patch_id"]
    ]
    assert manifest_path.read_bytes() == manifest_before
    assert (
        run_git(repository, "for-each-ref", "--format=%(refname) %(objectname)")
        == refs_before
    )


def test_finalize_component_cli_refuses_non_equivalent_immutable_identities(
    finalization_cli_repository: tuple[Path, Path, dict[str, str]], capsys,
):
    repository, manifest_path, identities = finalization_cli_repository

    exit_code = main(
        [
            "--finalize-component",
            "native-windows-updater-mcp",
            "--manifest",
            str(manifest_path),
            "--repo",
            str(repository),
            "--source-ref",
            "refs/heads/feature/updater",
            "--replacement",
            f"{identities['historical']}:{identities['integration']}:implementation",
        ]
    )
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 1
    assert captured.err == ""
    assert "replacement_patch_identity_mismatch" in {
        finding["code"] for finding in report["findings"]
    }


def test_finalize_component_cli_returns_json_for_malformed_identity_tuple(
    finalization_cli_repository: tuple[Path, Path, dict[str, str]], capsys,
):
    repository, manifest_path, _identities = finalization_cli_repository

    exit_code = main(
        [
            "--finalize-component",
            "native-windows-updater-mcp",
            "--manifest",
            str(manifest_path),
            "--repo",
            str(repository),
            "--source-ref",
            "refs/heads/feature/updater",
            "--replacement",
            "not-an-identity-tuple",
        ]
    )
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 2
    assert captured.err == ""
    assert report["ready"] is False
    assert "SOURCE_SHA:INTEGRATION_SHA:ROLE" in report["error"]


def test_finalize_component_cli_refuses_circular_integration_lineage(
    finalization_cli_repository: tuple[Path, Path, dict[str, str]], capsys,
):
    repository, manifest_path, identities = finalization_cli_repository

    exit_code = main(
        [
            "--finalize-component",
            "native-windows-updater-mcp",
            "--manifest",
            str(manifest_path),
            "--repo",
            str(repository),
            "--source-ref",
            "refs/heads/fork-integration",
            "--replacement",
            f"{identities['integration']}:{identities['integration']}:implementation",
        ]
    )
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    codes = {finding["code"] for finding in report["findings"]}

    assert exit_code == 1
    assert captured.err == ""
    assert "replacement_source_is_integration" in codes
    assert "replacement_source_final_same" in codes


@pytest.mark.parametrize(
    ("components", "expected_error"),
    [
        (None, "schema-1 components must be an array"),
        (42, "schema-1 components must be an array"),
        (["lost-component"], "schema-1 components[0] must be an object"),
        ([None], "schema-1 components[0] must be an object"),
        ([42], "schema-1 components[0] must be an object"),
        ([{"id": "broken", "patches": None}], "patches must be an array"),
        ([{"id": "broken", "patches": 42}], "patches must be an array"),
        (
            [{"id": "broken", "patches": ["lost-patch"]}],
            "schema-1 components[0].patches[0] must be an object",
        ),
        (
            [{"id": "broken", "patches": [None]}],
            "schema-1 components[0].patches[0] must be an object",
        ),
        (
            [{"id": "broken", "patches": [42]}],
            "schema-1 components[0].patches[0] must be an object",
        ),
    ],
)
def test_schema_1_cli_migration_rejects_malformed_collections_as_json(
    tmp_path: Path,
    capsys,
    components,
    expected_error: str,
):
    legacy = tmp_path / "malformed-legacy.json"
    legacy.write_text(
        json.dumps(
            {
                "schema": 1,
                "integration_branch": "fork-integration",
                "upstream": {},
                "fork": {},
                "components": components,
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(["--migrate-schema-1", str(legacy), "--json"])
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 2
    assert captured.err == ""
    assert report["ready"] is False
    assert expected_error in report["error"]
    assert "Traceback" not in captured.out
