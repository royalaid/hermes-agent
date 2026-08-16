"""U9 (KTD5/KTD12, R11/R12/R20): investigator authority, incident integrity,
and session visibility.

Part 1 -- authority token (this file's first half):
  - Window math: ``min(next scheduled fire, issue + 4h)``; no resolvable next
    fire (missing job, disabled job, unparseable stamp) expires immediately.
  - The window is FROZEN at mint: editing the schedule afterwards cannot
    extend a live window.
  - ``verify_authority`` refuses an absent, unparseable, foreign-job,
    expired, wrong-action, forged (digest mismatch), unrecorded, or
    closed-incident token, each with its own named reason.
  - The release script's push and publish call sites gate on it, re-checking
    immediately before EACH privileged action, and the refusal reaches the
    result JSON -- not just the log.
"""

from __future__ import annotations

import json
import subprocess
import sys
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from scripts.fork_integration.investigator import mod as investigator
from scripts.fork_integration.release import mod as release

JOB_ID = release.FLEET_JOB_ID
SIGNATURE = "4370ddbb651aa3907de2b236"


# ── fixtures ────────────────────────────────────────────────────────────────


def _write_jobs_store(home: Path, *, next_run_at: str | None, enabled: bool = True,
                      job_id: str = JOB_ID) -> None:
    job: dict[str, Any] = {"id": job_id, "name": "nightly", "enabled": enabled}
    if next_run_at is not None:
        job["next_run_at"] = next_run_at
    path = home / "cron" / "jobs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"jobs": [job], "updated_at": 0}), encoding="utf-8")


def _write_incident_state(home: Path, *, signature: str = SIGNATURE,
                          token_sha256: str | None = None, closure: Any = None,
                          job_id: str = JOB_ID, closed: bool = False) -> Path:
    entry: dict[str, Any] = {
        "occurrences": 1,
        "stage": "verify_manifest",
        "status": "admitted",
        "session_id": "abc12345",
        "spawned_at": datetime.now(timezone.utc).isoformat(),
        "heartbeat_at": None,
        "token_sha256": token_sha256,
        "closure": closure,
    }
    state: dict[str, Any] = {"schema": 2, "job_id": job_id, "open": {}, "closed": [], "failed": {}}
    if closed:
        state["closed"].append({**entry, "signature": signature,
                                "closure": closure or {"state": "abandoned", "at": "now", "reason": "test"}})
    else:
        state["open"][signature] = entry
    path = home / "cron" / "failure-investigators" / f"{job_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return path


def _minted(home: Path, *, next_run_at: str | None = None, signature: str = SIGNATURE,
            allowed: tuple[str, ...] = ("push", "publish"), now: datetime | None = None,
            record: bool = True) -> dict[str, Any]:
    """Mint a real token and (by default) record its digest in the incident."""
    if next_run_at is None:
        next_run_at = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    _write_jobs_store(home, next_run_at=next_run_at)
    minted = investigator.mint_authority_token(
        home=home, job_id=JOB_ID, signature=signature, now=now, allowed_actions=allowed
    )
    if record:
        _write_incident_state(home, signature=signature, token_sha256=minted["token_sha256"])
    return minted


# ── window math (KTD5) ──────────────────────────────────────────────────────


def test_window_end_is_the_next_scheduled_fire_when_it_is_inside_the_cap(tmp_path: Path) -> None:
    issued = datetime(2026, 8, 15, 22, 0, tzinfo=timezone.utc)
    next_fire = issued + timedelta(hours=1, minutes=30)
    assert investigator.authority_window_end(issued, next_fire) == next_fire


def test_window_end_is_the_four_hour_cap_when_the_next_fire_is_further_out(tmp_path: Path) -> None:
    issued = datetime(2026, 8, 15, 22, 0, tzinfo=timezone.utc)
    next_fire = issued + timedelta(days=1)
    assert investigator.authority_window_end(issued, next_fire) == issued + timedelta(hours=4)


def test_window_end_is_immediate_when_there_is_no_next_fire() -> None:
    """KTD5: an unresolvable next fire is an immediately expired window, never
    a silent fall-back to the 4h cap."""
    issued = datetime(2026, 8, 15, 22, 0, tzinfo=timezone.utc)
    assert investigator.authority_window_end(issued, None) == issued


def test_window_end_never_precedes_issue_even_for_a_stale_next_fire() -> None:
    issued = datetime(2026, 8, 15, 22, 0, tzinfo=timezone.utc)
    assert investigator.authority_window_end(issued, issued - timedelta(hours=5)) == issued


@pytest.mark.parametrize(
    "store",
    [
        pytest.param({"next_run_at": None}, id="missing-next-run-at"),
        pytest.param({"next_run_at": "not-a-timestamp"}, id="unparseable"),
        pytest.param({"next_run_at": "2026-08-16T02:00:00"}, id="naive-no-offset"),
        pytest.param({"next_run_at": "2026-08-16T02:00:00-07:00", "enabled": False}, id="disabled-job"),
        pytest.param({"next_run_at": "2026-08-16T02:00:00-07:00", "job_id": "ffffffffffff"}, id="other-job"),
    ],
)
def test_unresolvable_next_fire_mints_an_already_expired_token(tmp_path: Path, store: dict[str, Any]) -> None:
    _write_jobs_store(tmp_path, **store)
    minted = investigator.mint_authority_token(home=tmp_path, job_id=JOB_ID, signature=SIGNATURE)
    token = minted["token"]
    assert token["expires_at"] == token["issued_at"]

    _write_incident_state(tmp_path, token_sha256=minted["token_sha256"])
    verdict = investigator.verify_authority(
        token_path=minted["path"], job_id=JOB_ID, action="push", home=tmp_path
    )
    assert verdict["reason"] == "authority_token_expired"


def test_absent_jobs_store_mints_an_already_expired_token(tmp_path: Path) -> None:
    minted = investigator.mint_authority_token(home=tmp_path, job_id=JOB_ID, signature=SIGNATURE)
    assert minted["token"]["expires_at"] == minted["token"]["issued_at"]


def test_a_schedule_edit_after_the_mint_cannot_extend_a_live_window(tmp_path: Path) -> None:
    """The freeze proof (KTD5): ``expires_at`` is computed once, at mint.

    A monotonic clock cannot be shared across the spawner, the session, and
    the release process; the frozen wall-clock stamp is the mechanism that
    delivers KTD5's intent, and this pins it.
    """
    issued = datetime.now(timezone.utc)
    minted = _minted(tmp_path, next_run_at=(issued + timedelta(minutes=30)).isoformat(), now=issued)
    frozen = minted["token"]["expires_at"]

    # Someone pushes the nightly a week out after the investigator spawned.
    _write_jobs_store(tmp_path, next_run_at=(issued + timedelta(days=7)).isoformat())

    reread = json.loads(Path(minted["path"]).read_text(encoding="utf-8"))
    assert reread["expires_at"] == frozen
    verdict = investigator.verify_authority(
        token_path=minted["path"], job_id=JOB_ID, action="push", home=tmp_path,
        now=issued + timedelta(minutes=31),
    )
    assert verdict["reason"] == "authority_token_expired"


def test_replacement_window_is_capped_by_the_abandoned_finishers_window(tmp_path: Path) -> None:
    issued = datetime.now(timezone.utc)
    cap = issued + timedelta(minutes=10)
    _write_jobs_store(tmp_path, next_run_at=(issued + timedelta(hours=3)).isoformat())
    minted = investigator.mint_authority_token(
        home=tmp_path, job_id=JOB_ID, signature=SIGNATURE, now=issued, cap=cap
    )
    assert investigator.parse_timestamp(minted["token"]["expires_at"]) == cap


# ── token verification ──────────────────────────────────────────────────────


def test_a_valid_minted_token_is_granted(tmp_path: Path) -> None:
    minted = _minted(tmp_path)
    verdict = investigator.verify_authority(
        token_path=minted["path"], job_id=JOB_ID, action="publish", home=tmp_path
    )
    assert verdict["ok"] is True
    assert verdict["reason"] == "authority_granted"


def test_patching_the_session_id_does_not_invalidate_the_token(tmp_path: Path) -> None:
    """``session_id`` is patched in after ``session.create`` returns, so it is
    deliberately outside the digested claim."""
    minted = _minted(tmp_path)
    assert investigator.attach_session_to_authority(minted["path"], "sess1234") is True

    reread = json.loads(Path(minted["path"]).read_text(encoding="utf-8"))
    assert reread["session_id"] == "sess1234"
    verdict = investigator.verify_authority(
        token_path=minted["path"], job_id=JOB_ID, action="push", home=tmp_path
    )
    assert verdict["ok"] is True
    assert verdict["session_id"] == "sess1234"


def test_absent_token_is_refused(tmp_path: Path) -> None:
    _write_incident_state(tmp_path, token_sha256="whatever")
    verdict = investigator.verify_authority(token_path=None, job_id=JOB_ID, action="push", home=tmp_path)
    assert verdict == {**verdict, "ok": False, "reason": "authority_token_absent"}


def test_unparseable_token_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "token.json"
    path.write_text("{not json", encoding="utf-8")
    verdict = investigator.verify_authority(token_path=path, job_id=JOB_ID, action="push", home=tmp_path)
    assert verdict["reason"] == "authority_token_unparseable"


def test_missing_token_file_is_refused_as_unparseable(tmp_path: Path) -> None:
    verdict = investigator.verify_authority(
        token_path=tmp_path / "nope.json", job_id=JOB_ID, action="push", home=tmp_path
    )
    assert verdict["reason"] == "authority_token_unparseable"


def test_token_for_another_job_is_refused(tmp_path: Path) -> None:
    minted = _minted(tmp_path)
    verdict = investigator.verify_authority(
        token_path=minted["path"], job_id="ffffffffffff", action="push", home=tmp_path
    )
    assert verdict["reason"] == "authority_token_job_mismatch"


def test_expired_token_is_refused(tmp_path: Path) -> None:
    issued = datetime.now(timezone.utc) - timedelta(hours=2)
    minted = _minted(tmp_path, next_run_at=(issued + timedelta(minutes=5)).isoformat(), now=issued)
    verdict = investigator.verify_authority(
        token_path=minted["path"], job_id=JOB_ID, action="push", home=tmp_path
    )
    assert verdict["reason"] == "authority_token_expired"


def test_action_outside_the_allowed_set_is_refused(tmp_path: Path) -> None:
    minted = _minted(tmp_path, allowed=("push",))
    assert investigator.verify_authority(
        token_path=minted["path"], job_id=JOB_ID, action="push", home=tmp_path
    )["ok"] is True
    verdict = investigator.verify_authority(
        token_path=minted["path"], job_id=JOB_ID, action="publish", home=tmp_path
    )
    assert verdict["reason"] == "authority_action_not_allowed"
    assert verdict["allowed_actions"] == ["push"]


def test_a_forged_token_is_refused_by_digest(tmp_path: Path) -> None:
    """The forgery this actually catches: a token edited (or hand-written)
    after the spawner recorded its digest in the incident -- e.g. a widened
    action set or a stretched expiry."""
    minted = _minted(tmp_path)
    forged = json.loads(Path(minted["path"]).read_text(encoding="utf-8"))
    forged["expires_at"] = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    # Self-consistent forgery: the file's own recorded digest is updated too.
    forged["token_sha256"] = investigator.authority_token_sha256(forged)
    Path(minted["path"]).write_text(json.dumps(forged), encoding="utf-8")

    verdict = investigator.verify_authority(
        token_path=minted["path"], job_id=JOB_ID, action="push", home=tmp_path
    )
    assert verdict["reason"] == "authority_token_sha256_mismatch"


def test_token_without_an_incident_record_is_refused(tmp_path: Path) -> None:
    minted = _minted(tmp_path, record=False)
    verdict = investigator.verify_authority(
        token_path=minted["path"], job_id=JOB_ID, action="push", home=tmp_path
    )
    assert verdict["reason"] == "authority_incident_record_missing"


def test_token_whose_incident_recorded_no_digest_is_refused(tmp_path: Path) -> None:
    minted = _minted(tmp_path, record=False)
    _write_incident_state(tmp_path, token_sha256=None)
    verdict = investigator.verify_authority(
        token_path=minted["path"], job_id=JOB_ID, action="push", home=tmp_path
    )
    assert verdict["reason"] == "authority_incident_token_unrecorded"


def test_token_of_a_closed_incident_is_refused(tmp_path: Path) -> None:
    """Closing an incident revokes its finisher's authority immediately --
    the mechanism behind "exactly one replacement" (R11)."""
    minted = _minted(tmp_path, record=False)
    _write_incident_state(tmp_path, token_sha256=minted["token_sha256"], closed=True,
                          closure={"state": "abandoned", "at": "2026-08-15T22:00:00+00:00",
                                   "reason": "stale heartbeat"})
    verdict = investigator.verify_authority(
        token_path=minted["path"], job_id=JOB_ID, action="push", home=tmp_path
    )
    assert verdict["reason"] == "authority_incident_closed"


def test_a_schema_one_state_file_cannot_authorize_anything(tmp_path: Path) -> None:
    """The two legacy open incidents carry no digest, so no token can match
    them even before the v2 migration closes them."""
    minted = _minted(tmp_path, record=False)
    legacy = {"schema": 1, "job_id": JOB_ID, "failed": {},
              "open": {SIGNATURE: {"occurrences": 1, "stage": "verify_manifest", "status": "admitted"}}}
    path = tmp_path / "cron" / "failure-investigators" / f"{JOB_ID}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(legacy, indent=2), encoding="utf-8")

    verdict = investigator.verify_authority(
        token_path=minted["path"], job_id=JOB_ID, action="push", home=tmp_path
    )
    assert verdict["reason"] == "authority_incident_token_unrecorded"


# ── the release script's gate ───────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_authority_state() -> Any:
    release.reset_run_authority_state(None)
    yield
    release.reset_run_authority_state(None)


def _quiet(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Silence every side channel that would write under the real
    HERMES_HOME / Fleet root during a gate test."""
    lines: list[str] = []
    monkeypatch.setattr(release, "log", lines.append)
    monkeypatch.setattr(release, "emit_fleet_receipt", lambda *args, **kwargs: None)
    monkeypatch.setattr(release, "launch_failure_investigator", lambda **kwargs: None)
    monkeypatch.setattr(release, "resolve_failure_investigator_success", lambda: None)
    monkeypatch.setattr(release, "integration_scripts_integrity_check", lambda *, dry_run: {"ok": True})
    return lines


def test_scheduler_holder_needs_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The nightly run is the sanctioned automated path (R20)."""
    lines = _quiet(monkeypatch)
    monkeypatch.setattr(
        release, "_failure_investigator_module",
        lambda: (_ for _ in ()).throw(AssertionError("scheduler path must not consult the verifier")),
    )

    grant = release.require_authority("push", holder="scheduler", token_path=None)

    assert grant["reason"] == "scheduler_holder"
    assert release.AUTHORITY_REFUSALS == []
    assert any("AUTHORITY_OK action=push holder=scheduler" in line for line in lines)


def test_non_scheduler_holder_without_a_token_is_refused_in_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _quiet(monkeypatch)
    monkeypatch.setattr(release, "HERMES_HOME", tmp_path)

    with pytest.raises(release.AuthorityRefused) as excinfo:
        release.require_authority("publish", holder="investigator-4370ddbb", token_path=None)

    assert excinfo.value.refusal["reason"] == "authority_token_absent"
    assert release.AUTHORITY_REFUSALS[0]["action"] == "publish"
    assert release.AUTHORITY_REFUSALS[0]["holder"] == "investigator-4370ddbb"


def test_a_broken_verifier_is_a_refusal_not_a_bypass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _quiet(monkeypatch)
    monkeypatch.setattr(release, "HERMES_HOME", tmp_path)
    monkeypatch.setattr(
        release, "_failure_investigator_module",
        lambda: (_ for _ in ()).throw(RuntimeError("investigator module is unavailable")),
    )

    with pytest.raises(release.AuthorityRefused) as excinfo:
        release.require_authority("push", holder="investigator-x", token_path=tmp_path / "t.json")

    assert excinfo.value.refusal["reason"] == "authority_verifier_unavailable"


def test_a_real_minted_token_passes_the_release_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _quiet(monkeypatch)
    monkeypatch.setattr(release, "HERMES_HOME", tmp_path)
    minted = _minted(tmp_path)

    grant = release.require_authority("push", holder="investigator-4370ddbb", token_path=str(minted["path"]))

    assert grant["ok"] is True
    assert release.AUTHORITY_REFUSALS == []


def test_fail_folds_the_refusal_into_the_result_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """R20/KTD14: a refusal that only reaches the log is not delivered."""
    _quiet(monkeypatch)
    monkeypatch.setattr(release, "HERMES_HOME", tmp_path)
    with pytest.raises(release.AuthorityRefused):
        release.require_authority("push", holder="investigator-x", token_path=None)
    capsys.readouterr()

    with pytest.raises(SystemExit):
        release.fail("privileged action refused by authority gate: authority_token_absent")

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["authority_refusals"][0]["reason"] == "authority_token_absent"
    assert payload["authority_refusals"][0]["action"] == "push"


# ── the gate on the real main() path ────────────────────────────────────────

_PUBLISHED = "published0000"
_REBASED = "rebased0000"


def _drive_main(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    argv: list[str],
) -> dict[str, list[Any]]:
    """Stub main() down to just its push/publish transaction.

    Everything the transaction needs is faked with the recipe the operational
    suite already uses (``test_main_wires_post_push_recovery_...``); what is
    left real is the ordering of the two ``require_authority`` calls against
    ``push_rebased_output`` and ``publish_release``.
    """
    calls: dict[str, list[Any]] = {"push": [], "publish": []}

    def fake_git(*args: str, **_kwargs: Any) -> str:
        if args[:2] == ("rev-parse", f"{release.UPSTREAM_REMOTE}/main"):
            return "upstream0000"
        if args[:2] == ("rev-parse", f"refs/remotes/{release.FORK_REMOTE}/{release.BRANCH}"):
            return _PUBLISHED
        if args[:2] == ("rev-parse", "HEAD"):
            return _REBASED
        if args[:2] == ("ls-remote", release.FORK_REMOTE):
            return f"{_REBASED}\trefs/heads/{release.BRANCH}"
        return ""

    launcher = tmp_path / "Hermes-Setup.exe"
    launcher.write_bytes(b"x" * 1_000_001)

    class _Response:
        status = 200

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

    _quiet(monkeypatch)
    monkeypatch.setattr(release, "HERMES_HOME", tmp_path)
    monkeypatch.setattr(release, "git", fake_git)
    monkeypatch.setattr(release, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout="", stderr=""))
    monkeypatch.setattr(release, "exclusive_lock", lambda holder="scheduler": nullcontext())
    monkeypatch.setattr(release, "ensure_clean_identity", lambda: (_PUBLISHED, _PUBLISHED))
    monkeypatch.setattr(release, "synchronize_to_published_head", lambda local, published: published)
    monkeypatch.setattr(release, "verify_upstream_foundations", lambda: [])
    monkeypatch.setattr(release, "verify_manifest_sources", lambda: None)
    monkeypatch.setattr(release, "published_integration_range", lambda published, upstream: ("base", []))
    monkeypatch.setattr(release, "output_is_already_based_on_current_upstream", lambda *args: False)
    monkeypatch.setattr(release, "replay_published_integration_range", lambda *args, **kwargs: [])
    monkeypatch.setattr(release, "patch_resolution", lambda *args, **kwargs: ([], []))
    monkeypatch.setattr(release, "upstream_patch_resolution", lambda *args, **kwargs: ([], []))
    monkeypatch.setattr(release, "apply_required_patches", lambda *args, **kwargs: [])
    monkeypatch.setattr(release, "validate_required_components", lambda *args, **kwargs: None)
    monkeypatch.setattr(release, "validate_required_foundations", lambda *args, **kwargs: None)
    monkeypatch.setattr(release, "validate_published_commit_preservation", lambda *args, **kwargs: None)
    monkeypatch.setattr(release, "resolve_built_launcher", lambda: launcher)
    monkeypatch.setattr(release, "sha256", lambda path: "checksum")
    monkeypatch.setattr(
        release, "verify_existing_integration_release",
        lambda commit, expected_sha=None: {"candidate": False, "complete": True, "reason": "release_complete"},
    )
    monkeypatch.setattr(release, "verify_public_asset", lambda url, expected: None)
    monkeypatch.setattr(release, "sync_operational_copies", lambda sha: {"ok": True})
    monkeypatch.setattr(release, "restore_pre_push_checkout", lambda head: None)
    monkeypatch.setattr(release.urllib.request, "urlopen", lambda *args, **kwargs: _Response())
    monkeypatch.setattr(
        release, "push_rebased_output",
        lambda published, rebased: calls["push"].append((published, rebased)),
    )
    monkeypatch.setattr(
        release, "publish_release",
        lambda tag, commit, launcher_path, checksum: calls["publish"].append(tag) or ("https://example.invalid/r", []),
    )
    monkeypatch.setattr(sys, "argv", ["hermes-integration-release-windows.py", *argv])
    return calls


def test_main_refuses_to_push_for_a_non_scheduler_holder_without_a_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _drive_main(monkeypatch, tmp_path, argv=["--holder", "investigator-4370ddbb"])

    with pytest.raises(SystemExit) as exited:
        release.main()

    assert exited.value.code == 1
    assert calls["push"] == [] and calls["publish"] == []
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["authority_refusals"][0]["reason"] == "authority_token_absent"
    assert payload["authority_refusals"][0]["action"] == "push"


def test_main_publishes_under_a_valid_token_and_records_the_grants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    minted = _minted(home)
    calls = _drive_main(monkeypatch, tmp_path, argv=[
        "--holder", "investigator-4370ddbb", "--authority-token", str(minted["path"]),
    ])
    monkeypatch.setattr(release, "HERMES_HOME", home)

    assert release.main() == 0

    assert len(calls["push"]) == 1 and len(calls["publish"]) == 1
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert [grant["action"] for grant in payload["authority"]] == ["push", "publish"]


def test_main_rechecks_authority_immediately_before_each_privileged_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A window that closes between the push and the publish refuses the
    publish: the gate is per-action, not once per run (KTD5)."""
    calls = _drive_main(monkeypatch, tmp_path, argv=[
        "--holder", "investigator-4370ddbb", "--authority-token", str(tmp_path / "token.json"),
    ])
    verdicts = [
        {"ok": True, "reason": "authority_granted"},
        {"ok": False, "reason": "authority_token_expired"},
    ]

    class _Verifier:
        @staticmethod
        def verify_authority(**kwargs: Any) -> dict[str, Any]:
            return verdicts.pop(0)

    monkeypatch.setattr(release, "_failure_investigator_module", lambda: _Verifier())

    with pytest.raises(SystemExit) as exited:
        release.main()

    assert exited.value.code == 1
    assert len(calls["push"]) == 1, "the first action was authorized and must have run"
    assert calls["publish"] == [], "the expired window must refuse the second action"
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["authority_refusals"][0]["action"] == "publish"
    assert payload["authority_refusals"][0]["reason"] == "authority_token_expired"


def test_main_scheduler_path_pushes_and_publishes_without_any_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _drive_main(monkeypatch, tmp_path, argv=[])

    assert release.main() == 0

    assert len(calls["push"]) == 1 and len(calls["publish"]) == 1
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is True
    # The nightly brief keeps its shape: no authority key on the sanctioned path.
    assert "authority" not in payload
