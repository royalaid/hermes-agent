"""U2 (KTD2, R14): the sync boundary between the in-repo release system and
its operational copies at ``%HERMES_HOME%\\scripts``.

Covers ``scripts/fork_integration/sync.py`` directly (staged atomic
``sync()``, tree-authoritative ``verify()``, narrow-attestation
``restamp_manifest()``, and the CLI), plus the two wiring points in
``hermes-integration-release-windows.py``: the run-start integrity gate
(``integration_scripts_integrity_check``) and the post-publish hook
(``sync_operational_copies``, called by the verified-publication helper that
``main()`` reaches only from true publish success -- never on failure or under
``--dry-run``).

Fixtures build small real git repos (mirrors this suite's existing
``test_stable_patch_id_matches_real_git_patch_id`` idiom) rather than
mocking git, since ``sync.py``'s entire job is to be trustworthy against
what git actually has committed.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.fork_integration import sync

SYNC_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "fork_integration" / "sync.py"


# ── fixtures ─────────────────────────────────────────────────────────────


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Fork Integration Sync Test"], cwd=repo, check=True)
    # sync.py compares working-tree bytes against committed blob bytes
    # byte-for-byte (that IS the point of the tool). Disable autocrlf for
    # this fixture repo so a host-level global core.autocrlf=true setting
    # cannot silently normalize what git stores relative to what these
    # tests write and read back -- exactly the class of drift the real
    # operational files guard against via .gitattributes "-text".
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=repo, check=True)
    subprocess.run(["git", "config", "core.safecrlf", "false"], cwd=repo, check=True)
    return repo


def _write_tracked_files(repo: Path, *, variant: str) -> None:
    tracked_dir = repo / "scripts" / "fork_integration"
    tracked_dir.mkdir(parents=True, exist_ok=True)
    for name in sync.TRACKED_SET:
        (tracked_dir / name).write_text(f"# {name} content {variant}\n", encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=repo, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True, capture_output=True, check=True,
    ).stdout.strip()


# ── TRACKED_SET ──────────────────────────────────────────────────────────


def test_tracked_set_matches_the_named_files_plus_sync_py() -> None:
    """The five plan-named files, plus sync.py itself and U6's
    proposals.py/blocklist pair (documented deviations -- see sync.py's
    module docstring). Shims/README are excluded."""
    assert set(sync.TRACKED_SET) == {
        "hermes-integration-release-windows.py",
        "hermes-release-failure-investigator.py",
        "hermes-integration-manifest.json",
        "test_hermes_integration_release_windows.py",
        "overdue_check.py",
        "sync.py",
        "proposals.py",
        "fork-integration-blocklist.json",
    }
    for excluded in ("release.py", "investigator.py", "__init__.py", "README.md"):
        assert excluded not in sync.TRACKED_SET


# ── sync(): exact-set copy + stamp ──────────────────────────────────────


def test_sync_copies_exact_tracked_set_and_writes_stamp(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_tracked_files(repo, variant="v1")
    sha = _commit(repo, "v1")
    dest = tmp_path / "dest"

    stamp = sync.sync(sha, repo, dest)

    on_disk_names = {f.name for f in dest.iterdir() if f.name != sync.SYNC_STAMP_FILENAME}
    assert on_disk_names == set(sync.TRACKED_SET)
    assert stamp["source_sha"] == sha
    assert stamp["release_system_source_sha"] == sha
    assert stamp["published_product_sha"] is None
    assert set(stamp["files"]) == set(sync.TRACKED_SET)
    assert stamp["provisional"] is False
    assert stamp["reason"] is None
    assert stamp["actor"] is None
    assert "synced_at" in stamp

    stamp_on_disk = json.loads((dest / sync.SYNC_STAMP_FILENAME).read_text(encoding="utf-8"))
    assert stamp_on_disk == stamp
    for name in sync.TRACKED_SET:
        assert (dest / name).read_text(encoding="utf-8") == f"# {name} content v1\n"


def test_sync_stamps_release_source_and_published_product_as_distinct_lineage(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path)
    _write_tracked_files(repo, variant="v1")
    release_system_source_sha = _commit(repo, "v1")
    published_product_sha = "b" * 40
    dest = tmp_path / "dest"

    stamp = sync.sync(
        release_system_source_sha,
        repo,
        dest,
        published_product_sha=published_product_sha,
    )

    assert stamp["source_sha"] == release_system_source_sha
    assert stamp["release_system_source_sha"] == release_system_source_sha
    assert stamp["published_product_sha"] == published_product_sha
    assert json.loads(
        (dest / sync.SYNC_STAMP_FILENAME).read_text(encoding="utf-8")
    ) == stamp


# ── sync(): idempotent re-sync ───────────────────────────────────────────


def test_sync_is_idempotent_for_the_same_sha(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_tracked_files(repo, variant="v1")
    sha = _commit(repo, "v1")
    dest = tmp_path / "dest"

    first = sync.sync(sha, repo, dest)
    second = sync.sync(sha, repo, dest)

    assert first["source_sha"] == second["source_sha"] == sha
    assert first["files"] == second["files"]
    assert sync.verify(dest, repo)["ok"] is True


# ── verify(): tree is authoritative, not the stamp ──────────────────────


def test_verify_fails_when_file_and_stamp_are_rewritten_consistently(tmp_path: Path) -> None:
    """A consistently-rewritten file+stamp (self-agreeing, but disagreeing
    with the actual committed tree) must still fail -- the tree is
    authoritative, never the stamp's own recorded per-file hash."""
    repo = _init_repo(tmp_path)
    _write_tracked_files(repo, variant="v1")
    sha = _commit(repo, "v1")
    dest = tmp_path / "dest"
    sync.sync(sha, repo, dest)

    tampered_name = sync.TRACKED_SET[0]
    tampered_bytes = b"tampered content that matches no commit\n"
    (dest / tampered_name).write_bytes(tampered_bytes)
    stamp_path = dest / sync.SYNC_STAMP_FILENAME
    stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
    # Attacker "fixes" the stamp to agree with the tampered file.
    stamp["files"][tampered_name] = hashlib.sha256(tampered_bytes).hexdigest()
    stamp_path.write_text(json.dumps(stamp), encoding="utf-8")

    result = sync.verify(dest, repo)

    assert result["ok"] is False
    problems_by_file = {p["file"]: p["reason"] for p in result["problems"]}
    assert problems_by_file[tampered_name] == "hash_mismatch"


def test_verify_reports_no_stamp(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_tracked_files(repo, variant="v1")
    _commit(repo, "v1")
    dest = tmp_path / "dest"  # never synced
    dest.mkdir()

    result = sync.verify(dest, repo)

    assert result["ok"] is False
    assert result["reason"] == "no_stamp"


def test_verify_reports_unreachable_stamped_sha(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_tracked_files(repo, variant="v1")
    sha = _commit(repo, "v1")
    dest = tmp_path / "dest"
    sync.sync(sha, repo, dest)

    stamp_path = dest / sync.SYNC_STAMP_FILENAME
    stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
    stamp["source_sha"] = "f" * 40  # never committed
    stamp_path.write_text(json.dumps(stamp), encoding="utf-8")

    result = sync.verify(dest, repo)

    assert result["ok"] is False
    assert result["reason"] == "unreachable_sha"


def test_verify_reports_missing_file(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_tracked_files(repo, variant="v1")
    sha = _commit(repo, "v1")
    dest = tmp_path / "dest"
    sync.sync(sha, repo, dest)

    (dest / sync.TRACKED_SET[0]).unlink()

    result = sync.verify(dest, repo)

    assert result["ok"] is False
    problems_by_file = {p["file"]: p["reason"] for p in result["problems"]}
    assert problems_by_file[sync.TRACKED_SET[0]] == "missing_file"


# ── sync(): a run claiming any repair status cannot use mismatched files;
# only re-sync is permitted (release.py's run-start gate) ───────────────


def test_run_start_check_fails_closed_then_only_resync_clears_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end against the real sync module: release.py's run-start
    integrity gate refuses on a mismatch, and the ONLY thing that clears
    it is re-running sync() from a verified sha -- not a bypass."""
    from scripts.fork_integration.release import mod as release

    repo = _init_repo(tmp_path)
    _write_tracked_files(repo, variant="v1")
    sha = _commit(repo, "v1")
    hermes_home = tmp_path / "hermes-home"
    dest = hermes_home / "scripts"
    sync.sync(sha, repo, dest)

    monkeypatch.setattr(release, "HERMES_HOME", hermes_home)
    monkeypatch.setattr(release, "WORKTREE", repo)
    monkeypatch.setattr(release, "log", lambda message: None)

    def spy_fail(message: str, *, code: int = 1) -> None:
        raise SystemExit(code)

    monkeypatch.setattr(release, "fail", spy_fail)

    healthy = release.integration_scripts_integrity_check(dry_run=False)
    assert healthy["ok"] is True

    (dest / sync.TRACKED_SET[0]).write_text("tampered out of band\n", encoding="utf-8")

    with pytest.raises(SystemExit):
        release.integration_scripts_integrity_check(dry_run=False)

    # Re-sync (the only permitted remedy) clears it.
    sync.sync(sha, repo, dest)
    recovered = release.integration_scripts_integrity_check(dry_run=False)
    assert recovered["ok"] is True


def test_run_start_check_fails_closed_on_real_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolated unit test of the gate function via a monkeypatched sync
    module (complements the end-to-end test above)."""
    from scripts.fork_integration.release import mod as release

    fail_calls: list[str] = []

    def spy_fail(message: str, *, code: int = 1) -> None:
        fail_calls.append(message)
        raise SystemExit(code)

    monkeypatch.setattr(release, "fail", spy_fail)
    monkeypatch.setattr(release, "log", lambda message: None)
    fake_sync = SimpleNamespace(verify=lambda dest, repo: {"ok": False, "reason": "hash_mismatch", "problems": []})
    monkeypatch.setattr(release, "_sync_module", lambda: fake_sync)

    with pytest.raises(SystemExit):
        release.integration_scripts_integrity_check(dry_run=False)

    assert len(fail_calls) == 1
    assert "sync.py deploy" in fail_calls[0]
    assert "no bypass" not in fail_calls[0]  # no bypass flag is ever mentioned as an option


def test_run_start_check_dry_run_continues_and_reports_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.fork_integration.release import mod as release

    monkeypatch.setattr(release, "log", lambda message: None)

    def unexpected_fail(message: str, *, code: int = 1) -> None:
        raise AssertionError("fail() must not be called under --dry-run")

    monkeypatch.setattr(release, "fail", unexpected_fail)
    fake_sync = SimpleNamespace(verify=lambda dest, repo: {"ok": False, "reason": "hash_mismatch", "problems": []})
    monkeypatch.setattr(release, "_sync_module", lambda: fake_sync)

    result = release.integration_scripts_integrity_check(dry_run=True)

    assert result["ok"] is False
    assert result["reason"] == "hash_mismatch"


def test_run_start_check_missing_stamp_is_bootstrap_tolerance_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.fork_integration.release import mod as release

    logs: list[str] = []
    monkeypatch.setattr(release, "log", lambda message: logs.append(message))

    def unexpected_fail(message: str, *, code: int = 1) -> None:
        raise AssertionError("fail() must not be called for a pre-U2 missing stamp")

    monkeypatch.setattr(release, "fail", unexpected_fail)
    fake_sync = SimpleNamespace(verify=lambda dest, repo: {"ok": False, "reason": "no_stamp"})
    monkeypatch.setattr(release, "_sync_module", lambda: fake_sync)

    result = release.integration_scripts_integrity_check(dry_run=False)

    assert result["reason"] == "no_stamp"
    assert any("no sync stamp" in line for line in logs)


# ── sync(): interrupted commit phase leaves a detectable, never-lied-about
# state (prior generation's untouched files + stamp stay consistent) ────


def test_interrupted_sync_leaves_prior_generation_and_stamp_consistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path)
    _write_tracked_files(repo, variant="v1")
    sha1 = _commit(repo, "v1")
    dest = tmp_path / "dest"
    sync.sync(sha1, repo, dest)

    _write_tracked_files(repo, variant="v2")
    sha2 = _commit(repo, "v2")

    real_replace = sync.os.replace
    call_count = {"n": 0}

    def flaky_replace(src: Any, dst: Any) -> Any:
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise OSError("simulated kill between file writes")
        return real_replace(src, dst)

    monkeypatch.setattr(sync.os, "replace", flaky_replace)

    with pytest.raises(OSError):
        sync.sync(sha2, repo, dest)

    # The stamp is written strictly last -- it was never reached this run,
    # so it still names the OLD generation.
    stamp_on_disk = json.loads((dest / sync.SYNC_STAMP_FILENAME).read_text(encoding="utf-8"))
    assert stamp_on_disk["source_sha"] == sha1

    replaced_name = sync.TRACKED_SET[0]  # 1st os.replace call: succeeded
    untouched_name = sync.TRACKED_SET[2]  # loop died on call #2 (index 1); never reached
    assert (dest / replaced_name).read_text(encoding="utf-8") == f"# {replaced_name} content v2\n"
    assert (dest / untouched_name).read_text(encoding="utf-8") == f"# {untouched_name} content v1\n"

    # verify() catches the torn state instead of silently trusting it: the
    # replaced file no longer matches what the still-old stamp implies.
    monkeypatch.setattr(sync.os, "replace", real_replace)
    torn = sync.verify(dest, repo)
    assert torn["ok"] is False
    problems_by_file = {p["file"]: p["reason"] for p in torn["problems"]}
    assert problems_by_file[replaced_name] == "hash_mismatch"
    assert untouched_name not in problems_by_file

    # Recovery: re-sync from the intended sha lands a fully consistent
    # generation again.
    recovered = sync.sync(sha2, repo, dest)
    assert recovered["source_sha"] == sha2
    assert sync.verify(dest, repo)["ok"] is True


# ── sync(): provisional stamps + next non-provisional sync re-stamps ────


def test_provisional_deploy_stamps_provisional_and_next_publish_restamps(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_tracked_files(repo, variant="v1")
    sha1 = _commit(repo, "v1")
    dest = tmp_path / "dest"

    provisional_stamp = sync.sync(
        sha1, repo, dest, provisional=True, reason="publish path broken", actor="operator-x",
    )
    assert provisional_stamp["provisional"] is True
    assert provisional_stamp["reason"] == "publish path broken"
    assert provisional_stamp["actor"] == "operator-x"

    _write_tracked_files(repo, variant="v2")
    sha2 = _commit(repo, "v2")
    published_stamp = sync.sync(sha2, repo, dest)  # normal (non-provisional) post-publish sync

    assert published_stamp["provisional"] is False
    assert published_stamp["reason"] is None
    assert published_stamp["actor"] is None
    assert published_stamp["source_sha"] == sha2


# ── sync(): never fires on a failed or dry-run publish ───────────────────


def test_sync_hook_call_site_is_inside_the_publish_success_branch() -> None:
    """Code-inspection proof (explicitly permitted by the U2 test-scenario
    wording): main() reaches the helper that directly performs post-publish
    sync only after public verification -- never in the exception handler or
    --dry-run branch -- and preserves the release/product SHA boundary."""
    import ast
    import inspect
    import textwrap

    from scripts.fork_integration.release import mod as release

    def function_node(function: Any) -> ast.FunctionDef:
        module = ast.parse(textwrap.dedent(inspect.getsource(function)))
        return next(node for node in module.body if isinstance(node, ast.FunctionDef))

    def named_calls(node: ast.AST, name: str) -> list[ast.Call]:
        return [
            candidate
            for candidate in ast.walk(node)
            if isinstance(candidate, ast.Call)
            and isinstance(candidate.func, ast.Name)
            and candidate.func.id == name
        ]

    main = function_node(release.main)
    helper_calls = named_calls(main, "_finish_verified_publication")
    assert len(helper_calls) == 3, "all three checksum-verified success routes must use the finalizer"

    parents = {
        child: parent
        for parent in ast.walk(main)
        for child in ast.iter_child_nodes(parent)
    }
    outer_try = next(
        node
        for node in ast.walk(main)
        if isinstance(node, ast.Try)
        and any(
            isinstance(handler.type, ast.Name)
            and handler.type.id == "Exception"
            and handler.name == "exc"
            for handler in node.handlers
        )
        and len(named_calls(node, "_finish_verified_publication")) == 3
    )
    for helper_call in helper_calls:
        assert isinstance(parents[helper_call], ast.Return)
    assert all(
        not named_calls(handler, "_finish_verified_publication")
        for handler in outer_try.handlers
    ), "outer real-run failure handling must not finish publication"

    dry_run_if = next(
        node
        for node in ast.walk(main)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Attribute)
        and isinstance(node.test.value, ast.Name)
        and node.test.value.id == "args"
        and node.test.attr == "dry_run"
    )
    assert not any(
        named_calls(statement, "_finish_verified_publication")
        for statement in dry_run_if.body
    ), "dry-run handling must not finish publication"

    verified_assignments = [
        statement
        for statement in ast.walk(ast.Module(body=outer_try.body, type_ignores=[]))
        if isinstance(statement, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "public_integrity_verified"
            for target in statement.targets
        )
        and isinstance(statement.value, ast.Constant)
        and statement.value.value is True
    ]
    assert len(verified_assignments) == 3
    assert all(
        any(assignment.lineno < helper_call.lineno for assignment in verified_assignments)
        for helper_call in helper_calls
    )

    configurations: list[tuple[bool, bool, bool]] = []
    for helper_call in helper_calls:
        keywords = {keyword.arg: keyword.value for keyword in helper_call.keywords}
        release_source = keywords["release_system_source_sha"]
        assert (
            isinstance(release_source, ast.Call)
            and isinstance(release_source.func, ast.Attribute)
            and isinstance(release_source.func.value, ast.Name)
            and release_source.func.value.id == "sync_integrity"
            and release_source.func.attr == "get"
            and len(release_source.args) == 1
            and isinstance(release_source.args[0], ast.Constant)
            and release_source.args[0].value == "source_sha"
        )
        published_product = keywords["published_product_sha"]
        assert isinstance(published_product, ast.Name)
        assert published_product.id == "rebased_output_head"
        assert ast.dump(release_source) != ast.dump(published_product)
        configurations.append(tuple(
            bool(keywords[name].value)
            for name in ("changed", "consume_pin", "perform_sync")
            if isinstance(keywords[name], ast.Constant)
        ))
    assert sorted(configurations) == [(False, False, False), (False, False, False), (True, True, True)]

    helper = function_node(release._finish_verified_publication)
    sync_calls = named_calls(helper, "sync_operational_copies")
    assert len(sync_calls) == 1, "verified-publication helper must directly invoke sync"
    assert [
        argument.id if isinstance(argument, ast.Name) else None
        for argument in sync_calls[0].args
    ] == ["release_system_source_sha", "published_product_sha"]


def test_sync_operational_copies_deploys_verified_release_source_and_records_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The product release SHA is context, never the operational sync source."""
    from scripts.fork_integration.release import mod as release

    release_system_source_sha = "a" * 40
    published_product_sha = "b" * 40
    sync_calls: list[tuple[str, Path, Path, str | None]] = []

    def sync_from_verified_source(
        from_sha: str,
        repo: Path,
        dest: Path,
        *,
        published_product_sha: str | None = None,
    ) -> dict[str, Any]:
        sync_calls.append((from_sha, repo, dest, published_product_sha))
        return {
            "source_sha": from_sha,
            "release_system_source_sha": from_sha,
            "published_product_sha": published_product_sha,
            "files": {},
        }

    monkeypatch.setattr(release, "_sync_module", lambda: SimpleNamespace(sync=sync_from_verified_source))
    monkeypatch.setattr(release, "log", lambda message: None)

    outcome = release.sync_operational_copies(release_system_source_sha, published_product_sha)

    assert sync_calls == [
        (
            release_system_source_sha,
            release.WORKTREE,
            release.HERMES_HOME / "scripts",
            published_product_sha,
        )
    ]
    assert sync_calls[0][0] != published_product_sha
    assert outcome["ok"] is True
    assert outcome["source_sha"] == release_system_source_sha
    assert outcome["published_product_sha"] == published_product_sha


def test_sync_operational_copies_fails_closed_without_verified_release_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing run-start lineage must not fall through to sync.sync()."""
    from scripts.fork_integration.release import mod as release

    sync_calls: list[tuple[Any, ...]] = []
    fake_sync = SimpleNamespace(sync=lambda *args: sync_calls.append(args))
    monkeypatch.setattr(release, "_sync_module", lambda: fake_sync)
    monkeypatch.setattr(release, "log", lambda message: None)

    published_product_sha = "b" * 40
    outcome = release.sync_operational_copies(None, published_product_sha)

    assert outcome["ok"] is False
    assert outcome["source_sha"] is None
    assert outcome["published_product_sha"] == published_product_sha
    assert "verified release-system source SHA" in outcome["error"]
    assert sync_calls == []


def test_sync_operational_copies_never_raises_and_reports_failure_honestly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The post-publish hook wrapper is best-effort: a sync failure must
    not raise back into main() (the release already succeeded), but it
    must be reported truthfully in the returned dict, not swallowed."""
    from scripts.fork_integration.release import mod as release

    logs: list[str] = []
    monkeypatch.setattr(release, "log", lambda message: logs.append(message))

    def broken_sync_module() -> Any:
        raise RuntimeError("sync.py unavailable")

    monkeypatch.setattr(release, "_sync_module", broken_sync_module)

    release_system_source_sha = "f" * 40
    published_product_sha = "e" * 40
    outcome = release.sync_operational_copies(
        release_system_source_sha, published_product_sha
    )

    assert outcome["ok"] is False
    assert "sync.py unavailable" in outcome["error"]
    assert outcome["source_sha"] == release_system_source_sha
    assert outcome["published_product_sha"] == published_product_sha
    assert any("post-publish sync failed" in line for line in logs)


def test_sync_operational_copies_missing_source_survives_logging_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A diagnostic failure cannot retroactively fail a published release."""
    from scripts.fork_integration.release import mod as release

    sync_calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        release,
        "_sync_module",
        lambda: SimpleNamespace(sync=lambda *args: sync_calls.append(args)),
    )
    monkeypatch.setattr(
        release,
        "log",
        lambda _message: (_ for _ in ()).throw(OSError("log unavailable")),
    )

    outcome = release.sync_operational_copies(None, "b" * 40)

    assert outcome["ok"] is False
    assert outcome["source_sha"] is None
    assert outcome["published_product_sha"] == "b" * 40
    assert "verified release-system source SHA" in outcome["error"]
    assert sync_calls == []


def test_sync_operational_copies_sync_failure_survives_logging_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The original sync error remains the result when logging also fails."""
    from scripts.fork_integration.release import mod as release

    monkeypatch.setattr(
        release,
        "_sync_module",
        lambda: (_ for _ in ()).throw(RuntimeError("sync unavailable")),
    )
    monkeypatch.setattr(
        release,
        "log",
        lambda _message: (_ for _ in ()).throw(OSError("log unavailable")),
    )

    outcome = release.sync_operational_copies("a" * 40, "b" * 40)

    assert outcome["ok"] is False
    assert outcome["error"] == "sync unavailable"
    assert outcome["source_sha"] == "a" * 40
    assert outcome["published_product_sha"] == "b" * 40


def test_sync_operational_copies_success_survives_logging_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful sync remains successful when only its diagnostic fails."""
    from scripts.fork_integration.release import mod as release

    monkeypatch.setattr(
        release,
        "_sync_module",
        lambda: SimpleNamespace(
            sync=lambda source, _repo, _dest, **_kwargs: {"source_sha": source, "files": {}}
        ),
    )
    monkeypatch.setattr(
        release,
        "log",
        lambda _message: (_ for _ in ()).throw(OSError("log unavailable")),
    )

    outcome = release.sync_operational_copies("a" * 40, "b" * 40)

    assert outcome["ok"] is True
    assert outcome["source_sha"] == "a" * 40
    assert outcome["published_product_sha"] == "b" * 40


# ── restamp_manifest(): accepts matching fragment, refuses otherwise ────


def test_restamp_manifest_accepts_matching_fragment_refuses_otherwise(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_tracked_files(repo, variant="v1")
    sha = _commit(repo, "v1")
    dest = tmp_path / "dest"
    pre_stamp = sync.sync(sha, repo, dest)

    manifest_path = dest / sync.MANIFEST_FILENAME
    # Simulate the U6 approval flow having already applied an approved
    # edit directly to the operational manifest, out of band.
    manifest_path.write_text('{"approved": "fragment"}', encoding="utf-8")
    approved_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    restamped = sync.restamp_manifest(dest, repo, approved_hash)

    assert restamped["files"][sync.MANIFEST_FILENAME] == approved_hash
    assert restamped["source_sha"] == pre_stamp["source_sha"]
    other_name = sync.TRACKED_SET[1]
    assert restamped["files"][other_name] == pre_stamp["files"][other_name]
    stamp_on_disk = json.loads((dest / sync.SYNC_STAMP_FILENAME).read_text(encoding="utf-8"))
    assert stamp_on_disk == restamped

    # Any further, un-approved delta refuses closed.
    manifest_path.write_text('{"further": "unapproved edit"}', encoding="utf-8")
    with pytest.raises(sync.SyncError):
        sync.restamp_manifest(dest, repo, approved_hash)

    # The refusal did not silently mutate the stamp.
    stamp_after_refusal = json.loads((dest / sync.SYNC_STAMP_FILENAME).read_text(encoding="utf-8"))
    assert stamp_after_refusal == restamped


def test_restamp_file_attests_the_blocklist_and_refuses_untracked_names(tmp_path: Path) -> None:
    """U6 approvals mutate the blocklist as well as the manifest, so the
    generalized primitive must attest any tracked file -- and only those."""
    repo = _init_repo(tmp_path)
    _write_tracked_files(repo, variant="v1")
    sha = _commit(repo, "v1")
    dest = tmp_path / "dest"
    pre_stamp = sync.sync(sha, repo, dest)

    blocklist_path = dest / sync.BLOCKLIST_FILENAME
    blocklist_path.write_text('{"schema": 1, "entries": [{"patch_id": "a"}]}', encoding="utf-8")
    approved_hash = hashlib.sha256(blocklist_path.read_bytes()).hexdigest()

    restamped = sync.restamp_file(dest, repo, sync.BLOCKLIST_FILENAME, approved_hash)

    assert restamped["files"][sync.BLOCKLIST_FILENAME] == approved_hash
    assert restamped["source_sha"] == pre_stamp["source_sha"]
    assert restamped["files"][sync.MANIFEST_FILENAME] == pre_stamp["files"][sync.MANIFEST_FILENAME]

    with pytest.raises(sync.SyncError, match="untracked file"):
        sync.restamp_file(dest, repo, "not-a-tracked-file.json", approved_hash)


def test_restamp_manifest_refuses_without_a_prior_stamp(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_tracked_files(repo, variant="v1")
    _commit(repo, "v1")
    dest = tmp_path / "dest"
    dest.mkdir()
    manifest_path = dest / sync.MANIFEST_FILENAME
    manifest_path.write_text("{}", encoding="utf-8")
    approved_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    with pytest.raises(sync.SyncError, match="no prior sync stamp"):
        sync.restamp_manifest(dest, repo, approved_hash)


# ── sync canary: marker absent before, present exactly once after ───────


def test_sync_canary_sequence(tmp_path: Path) -> None:
    """U2 execution note: commit a trivial marker change to a tracked
    file, run sync() from that sha into a fake dest, and prove the marker
    is absent until the sync, then present exactly once after."""
    repo = _init_repo(tmp_path)
    _write_tracked_files(repo, variant="pre-canary")
    _commit(repo, "pre-canary baseline")

    marker_name = sync.TRACKED_SET[0]
    marker_line = "CANARY-MARKER-2026-08-15"
    tracked_path = repo / "scripts" / "fork_integration" / marker_name
    tracked_path.write_text(tracked_path.read_text(encoding="utf-8") + marker_line + "\n", encoding="utf-8")
    canary_sha = _commit(repo, "sync canary: marker change")

    dest = tmp_path / "fake-operational-dest"
    assert not dest.exists(), "marker must be absent before any sync/deploy"

    stamp = sync.sync(canary_sha, repo, dest)

    marker_text = (dest / marker_name).read_text(encoding="utf-8")
    assert marker_text.count(marker_line) == 1
    assert stamp["source_sha"] == canary_sha
    assert stamp["files"][marker_name] == hashlib.sha256(tracked_path.read_bytes()).hexdigest()
    assert sync.verify(dest, repo)["ok"] is True


# ── CLI ────────────────────────────────────────────────────────────────


class TestCli:
    def test_deploy_then_verify_succeed(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        _write_tracked_files(repo, variant="v1")
        sha = _commit(repo, "v1")
        dest = tmp_path / "dest"

        deploy = subprocess.run(
            [
                sys.executable, str(SYNC_SCRIPT_PATH), "deploy",
                "--from-sha", sha, "--repo", str(repo), "--dest", str(dest),
            ],
            capture_output=True, text=True, check=False,
        )
        assert deploy.returncode == 0, deploy.stderr
        payload = json.loads(deploy.stdout)
        assert payload["ok"] is True
        assert payload["source_sha"] == sha
        assert payload["release_system_source_sha"] == sha
        assert payload["published_product_sha"] is None

        verify_run = subprocess.run(
            [sys.executable, str(SYNC_SCRIPT_PATH), "verify", "--repo", str(repo), "--dest", str(dest)],
            capture_output=True, text=True, check=False,
        )
        assert verify_run.returncode == 0, verify_run.stderr
        assert json.loads(verify_run.stdout)["ok"] is True

    def test_deploy_accepts_explicit_published_product_sha(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        _write_tracked_files(repo, variant="v1")
        sha = _commit(repo, "v1")
        product_sha = "b" * 40
        dest = tmp_path / "dest"

        deploy = subprocess.run(
            [
                sys.executable, str(SYNC_SCRIPT_PATH), "deploy",
                "--from-sha", sha, "--repo", str(repo), "--dest", str(dest),
                "--published-product-sha", product_sha,
            ],
            capture_output=True, text=True, check=False,
        )

        assert deploy.returncode == 0, deploy.stderr
        payload = json.loads(deploy.stdout)
        assert payload["release_system_source_sha"] == sha
        assert payload["published_product_sha"] == product_sha

    def test_verify_exit_code_2_on_mismatch(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        _write_tracked_files(repo, variant="v1")
        sha = _commit(repo, "v1")
        dest = tmp_path / "dest"
        sync.sync(sha, repo, dest)
        (dest / sync.TRACKED_SET[0]).write_text("tampered\n", encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(SYNC_SCRIPT_PATH), "verify", "--repo", str(repo), "--dest", str(dest)],
            capture_output=True, text=True, check=False,
        )
        assert result.returncode == 2
        assert json.loads(result.stdout)["ok"] is False

    def test_provisional_deploy_requires_reason(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        _write_tracked_files(repo, variant="v1")
        sha = _commit(repo, "v1")
        dest = tmp_path / "dest"

        result = subprocess.run(
            [
                sys.executable, str(SYNC_SCRIPT_PATH), "deploy",
                "--from-sha", sha, "--repo", str(repo), "--dest", str(dest), "--provisional",
            ],
            capture_output=True, text=True, check=False,
        )
        assert result.returncode == 2
        assert "reason" in json.loads(result.stdout)["error"]

    def test_restamp_manifest_cli_success_and_exit_code_2_on_refusal(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        _write_tracked_files(repo, variant="v1")
        sha = _commit(repo, "v1")
        dest = tmp_path / "dest"
        sync.sync(sha, repo, dest)
        manifest_path = dest / sync.MANIFEST_FILENAME
        manifest_path.write_text('{"approved": "fragment"}', encoding="utf-8")
        approved_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

        ok_result = subprocess.run(
            [
                sys.executable, str(SYNC_SCRIPT_PATH), "restamp-manifest",
                "--repo", str(repo), "--dest", str(dest), "--approved-fragment", approved_hash,
            ],
            capture_output=True, text=True, check=False,
        )
        assert ok_result.returncode == 0, ok_result.stderr
        assert json.loads(ok_result.stdout)["ok"] is True

        manifest_path.write_text('{"further": "unapproved"}', encoding="utf-8")
        refused = subprocess.run(
            [
                sys.executable, str(SYNC_SCRIPT_PATH), "restamp-manifest",
                "--repo", str(repo), "--dest", str(dest), "--approved-fragment", approved_hash,
            ],
            capture_output=True, text=True, check=False,
        )
        assert refused.returncode == 2
        assert json.loads(refused.stdout)["ok"] is False


def test_deploy_ensures_runtime_reachability(tmp_path: Path) -> None:
    """A provisional deploy from an unpushed commit must leave the deployed
    SHA resolvable in the runtime verifier's clone (observed live
    2026-08-16: unreachable_sha would fail the next real run closed)."""
    repo = _init_repo(tmp_path)
    _write_tracked_files(repo, variant="reach")
    sha = _commit(repo, "reachability baseline")

    (tmp_path / "runtime-holder").mkdir()
    runtime = _init_repo(tmp_path / "runtime-holder")
    (runtime / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(runtime), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(runtime), "commit", "-q", "-m", "seed"],
        check=True, capture_output=True,
    )
    probe = subprocess.run(
        ["git", "-C", str(runtime), "cat-file", "-t", sha], capture_output=True, text=True
    )
    assert probe.returncode != 0, "runtime clone must start without the deploy sha"

    dest = tmp_path / "dest"
    stamp = sync.sync(sha, repo, dest, provisional=True, reason="test", runtime_repo=runtime)

    assert stamp["runtime_reachability"]["action"] == "fetched"
    assert stamp["runtime_reachability"]["reachable"] == "true"
    resolved = subprocess.run(
        ["git", "-C", str(runtime), "cat-file", "-t", sha], capture_output=True, text=True
    )
    assert resolved.stdout.strip() == "commit"

    second = sync.sync(sha, repo, dest, provisional=True, reason="test", runtime_repo=runtime)
    assert second["runtime_reachability"]["action"] == "none"
