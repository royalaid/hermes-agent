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


# ═══ Part 2: incident schema v2, heartbeat, one finisher per window ═════════

# The exact shape of the live state file at
# %HERMES_HOME%\cron\failure-investigators\1ab4c7013fef.json on 2026-08-15,
# with the two incidents that have been open (and unfinishable) since 12:36.
_LEGACY_STATE = {
    "failed": {},
    "job_id": JOB_ID,
    "open": {
        "4370ddbb651aa3907de2b236": {"occurrences": 1, "stage": "verify_manifest", "status": "admitted"},
        "c8d5bd93ea9c315804034bfc": {"occurrences": 1, "stage": "apply_foundations", "status": "admitted"},
    },
    "schema": 1,
}


def _write_legacy_state(home: Path) -> Path:
    path = home / "cron" / "failure-investigators" / f"{JOB_ID}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_LEGACY_STATE, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _record(home: Path, *, stage: str = "verify_manifest", error: str = "boom") -> dict[str, Any]:
    return investigator.record_failure(
        job_id=JOB_ID, stage=stage, error=error, home=home, worktree=home / "worktree",
        script_path=home / "release.py", log_path=home / "log.txt",
        test_path=home / "tests.py", manifest_path=home / "manifest.json",
    )


# ── migration ───────────────────────────────────────────────────────────────


def test_schema_one_open_incidents_migrate_to_superseded_closures() -> None:
    state, migrated = investigator.migrate_incident_state(json.loads(json.dumps(_LEGACY_STATE)), JOB_ID)

    assert migrated is True
    assert state["schema"] == 2
    assert state["open"] == {}
    closures = {entry["signature"]: entry["closure"] for entry in state["closed"]}
    assert set(closures) == {"4370ddbb651aa3907de2b236", "c8d5bd93ea9c315804034bfc"}
    assert all(closure["state"] == "superseded" for closure in closures.values())
    assert all(closure["reason"] == "schema-v2 migration" for closure in closures.values())
    # The legacy facts survive the closure; nothing is invented.
    stages = {entry["signature"]: entry["stage"] for entry in state["closed"]}
    assert stages["4370ddbb651aa3907de2b236"] == "verify_manifest"
    assert stages["c8d5bd93ea9c315804034bfc"] == "apply_foundations"
    assert all(entry["token_sha256"] is None for entry in state["closed"])


def test_migration_is_idempotent() -> None:
    once, _ = investigator.migrate_incident_state(json.loads(json.dumps(_LEGACY_STATE)), JOB_ID)
    twice, migrated_again = investigator.migrate_incident_state(json.loads(json.dumps(once)), JOB_ID)

    assert migrated_again is False
    assert twice == once


def test_recording_a_failure_migrates_the_legacy_file_in_place(tmp_path: Path) -> None:
    state_path = _write_legacy_state(tmp_path)

    result = _record(tmp_path, stage="push", error="new failure")

    written = json.loads(state_path.read_text(encoding="utf-8"))
    assert written["schema"] == 2
    assert list(written["open"]) == [result["signature"]]
    assert {entry["signature"] for entry in written["closed"]} == set(_LEGACY_STATE["open"])
    assert {entry["closure"]["state"] for entry in written["closed"]} == {"superseded"}
    entry = written["open"][result["signature"]]
    assert entry["session_id"] is None and entry["spawned_at"] is None
    assert entry["heartbeat_at"] is None and entry["token_sha256"] is None
    assert entry["closure"] is None


def test_recurrence_of_an_open_signature_counts_occurrences_not_new_incidents(tmp_path: Path) -> None:
    first = _record(tmp_path)
    second = _record(tmp_path)

    assert first["signature"] == second["signature"]
    assert (first["spawn"], second["spawn"]) == (True, False)
    assert second["occurrences"] == 2


def test_resolve_success_closes_open_incidents_as_resolved(tmp_path: Path) -> None:
    result = _record(tmp_path)
    state_path = Path(result["state_path"])

    investigator.resolve_success(JOB_ID, tmp_path)

    written = json.loads(state_path.read_text(encoding="utf-8"))
    assert written["open"] == {}
    assert written["closed"][-1]["signature"] == result["signature"]
    assert written["closed"][-1]["closure"]["state"] == "resolved"


# ── heartbeat ───────────────────────────────────────────────────────────────


def test_heartbeat_stamps_the_open_incident(tmp_path: Path) -> None:
    result = _record(tmp_path)

    outcome = investigator.heartbeat(home=tmp_path, job_id=JOB_ID, signature=result["signature"])

    assert outcome["ok"] is True
    state = json.loads(Path(result["state_path"]).read_text(encoding="utf-8"))
    assert state["open"][result["signature"]]["heartbeat_at"] == outcome["heartbeat_at"]
    artifact = json.loads(Path(result["artifact_path"]).read_text(encoding="utf-8"))
    assert artifact["heartbeat_at"] == outcome["heartbeat_at"]


def test_heartbeat_refuses_an_incident_that_is_not_open(tmp_path: Path) -> None:
    _write_legacy_state(tmp_path)

    outcome = investigator.heartbeat(home=tmp_path, job_id=JOB_ID, signature="4370ddbb651aa3907de2b236")

    assert outcome == {"ok": False, "reason": "incident_not_open", "job_id": JOB_ID,
                       "signature": "4370ddbb651aa3907de2b236"}


def test_heartbeat_cli_reports_json_and_an_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = _record(tmp_path)

    code = investigator.main([
        "heartbeat", "--job", JOB_ID, "--signature", result["signature"], "--home", str(tmp_path),
    ])

    assert code == 0
    assert json.loads(capsys.readouterr().out.strip())["ok"] is True
    assert investigator.main([
        "heartbeat", "--job", JOB_ID, "--signature", "0" * 24, "--home", str(tmp_path),
    ]) == 1


# ── one finisher per window ─────────────────────────────────────────────────


def _launch(home: Path, *, signature: str, now: datetime) -> dict[str, Any]:
    return investigator.plan_investigator_launch(
        home=home, job_id=JOB_ID, signature=signature, now=now,
    )


def test_first_failure_spawns_one_finisher_with_a_token(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    _write_jobs_store(tmp_path, next_run_at=(now + timedelta(hours=2)).isoformat())
    result = _record(tmp_path)

    decision = _launch(tmp_path, signature=result["signature"], now=now)

    assert decision["action"] == "spawn"
    assert decision["reason"] == "no_live_finisher"
    entry = json.loads(Path(result["state_path"]).read_text(encoding="utf-8"))["open"][result["signature"]]
    assert entry["token_sha256"] == decision["token_sha256"]
    assert entry["spawned_at"] == now.isoformat()
    # The evidence artifact points at the window the finisher was given.
    artifact = json.loads(Path(result["artifact_path"]).read_text(encoding="utf-8"))
    assert artifact["authority"]["token_path"] == decision["token_path"]
    assert artifact["authority"]["allowed_actions"] == ["push", "publish"]


def test_a_second_failure_inside_a_live_window_attaches_without_spawning(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    _write_jobs_store(tmp_path, next_run_at=(now + timedelta(hours=2)).isoformat())
    first = _record(tmp_path, stage="verify_manifest", error="first")
    _launch(tmp_path, signature=first["signature"], now=now)
    second = _record(tmp_path, stage="apply_foundations", error="second, different failure")

    decision = _launch(tmp_path, signature=second["signature"], now=now + timedelta(minutes=5))

    assert decision["action"] == "attach"
    assert decision["attached_to"] == first["signature"]
    state = json.loads(Path(first["state_path"]).read_text(encoding="utf-8"))
    assert state["open"][first["signature"]]["attached"][0]["signature"] == second["signature"]
    # No second token was minted for the attached signature.
    assert state["open"][second["signature"]]["token_sha256"] is None


def test_a_stale_heartbeat_closes_abandoned_and_spawns_exactly_one_replacement(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    _write_jobs_store(tmp_path, next_run_at=(now + timedelta(hours=3)).isoformat())
    first = _record(tmp_path, stage="verify_manifest", error="first")
    original = _launch(tmp_path, signature=first["signature"], now=now)
    later = now + timedelta(minutes=25)  # past the 20 minute heartbeat window
    second = _record(tmp_path, stage="push", error="second failure, finisher is gone")

    decision = _launch(tmp_path, signature=second["signature"], now=later)

    assert decision["action"] == "spawn"
    assert decision["reason"] == "replacement_for_abandoned_finisher"
    assert decision["closed"] == [{"signature": first["signature"], "state": "abandoned"}]

    state = json.loads(Path(first["state_path"]).read_text(encoding="utf-8"))
    closed = [entry for entry in state["closed"] if entry["signature"] == first["signature"]][0]
    assert closed["closure"]["state"] == "abandoned"
    assert "heartbeat stale" in closed["closure"]["reason"]
    # Exactly one finisher exists afterwards.
    finishers = [sig for sig, entry in state["open"].items() if entry.get("token_sha256")]
    assert finishers == [second["signature"]]
    # The replacement inherits the abandoned finisher's window end: a
    # die-and-replace cycle cannot walk the window forward.
    assert decision["expires_at"] == original["expires_at"]
    # And the abandoned finisher's own token is dead the moment it closed.
    assert investigator.verify_authority(
        token_path=original["token_path"], job_id=JOB_ID, action="push", home=tmp_path, now=later,
    )["reason"] == "authority_incident_closed"


def test_a_fresh_replacement_absorbs_the_next_failure_instead_of_spawning_again(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    _write_jobs_store(tmp_path, next_run_at=(now + timedelta(hours=3)).isoformat())
    first = _record(tmp_path, stage="verify_manifest", error="first")
    _launch(tmp_path, signature=first["signature"], now=now)
    later = now + timedelta(minutes=25)
    second = _record(tmp_path, stage="push", error="second")
    _launch(tmp_path, signature=second["signature"], now=later)
    third = _record(tmp_path, stage="publish", error="third")

    decision = _launch(tmp_path, signature=third["signature"], now=later + timedelta(minutes=1))

    assert decision["action"] == "attach"
    assert decision["attached_to"] == second["signature"]


def test_a_replacement_cannot_itself_be_replaced_while_its_heartbeat_is_fresh(tmp_path: Path) -> None:
    """The heartbeat window is what damps a die-and-replace cycle: a fresh
    replacement absorbs every subsequent failure for at least 20 minutes, so
    no separate spawn-rate limiter is needed (or shipped)."""
    now = datetime.now(timezone.utc)
    _write_jobs_store(tmp_path, next_run_at=(now + timedelta(hours=3)).isoformat())
    first = _record(tmp_path, stage="verify_manifest", error="first")
    _launch(tmp_path, signature=first["signature"], now=now)
    stale_moment = now + timedelta(minutes=21)
    second = _record(tmp_path, stage="push", error="second")
    _launch(tmp_path, signature=second["signature"], now=stale_moment)

    spawns = []
    for minute in range(1, 20):
        third = _record(tmp_path, stage="publish", error=f"failure {minute}")
        decision = _launch(tmp_path, signature=third["signature"], now=stale_moment + timedelta(minutes=minute))
        spawns.append(decision["action"])

    assert set(spawns) == {"attach"}


def test_an_ended_window_closes_expired_and_refuses_a_powerless_replacement(
    tmp_path: Path,
) -> None:
    now = datetime.now(timezone.utc)
    _write_jobs_store(tmp_path, next_run_at=(now + timedelta(minutes=10)).isoformat())
    first = _record(tmp_path, stage="verify_manifest", error="first")
    _launch(tmp_path, signature=first["signature"], now=now)
    after_window = now + timedelta(minutes=11)
    second = _record(tmp_path, stage="push", error="second, after the window")

    decision = _launch(tmp_path, signature=second["signature"], now=after_window)

    # A window that simply ran out closes `expired`, not `abandoned`.
    assert decision["closed"] == [{"signature": first["signature"], "state": "expired"}]
    # The schedule (now in the past) offers no new window, so replacing the
    # finisher would only produce a powerless session on every later failure.
    assert decision["action"] == "skip"
    assert decision["reason"] == "window_unavailable"


def test_an_ended_window_is_replaced_when_the_schedule_offers_a_new_one(tmp_path: Path) -> None:
    """The normal nightly case: the window ended at the scheduled fire, the
    run failed, and the next fire is a day out -- a fresh finisher spawns."""
    now = datetime.now(timezone.utc)
    _write_jobs_store(tmp_path, next_run_at=(now + timedelta(minutes=10)).isoformat())
    first = _record(tmp_path, stage="verify_manifest", error="first")
    _launch(tmp_path, signature=first["signature"], now=now)
    after_window = now + timedelta(minutes=11)
    _write_jobs_store(tmp_path, next_run_at=(after_window + timedelta(days=1)).isoformat())
    second = _record(tmp_path, stage="push", error="second, after the window")

    decision = _launch(tmp_path, signature=second["signature"], now=after_window)

    assert decision["closed"] == [{"signature": first["signature"], "state": "expired"}]
    assert decision["action"] == "spawn"
    # Capped at 4h by KTD5, not stretched to tomorrow's fire.
    assert investigator.parse_timestamp(decision["expires_at"]) == after_window + timedelta(hours=4)


def test_maybe_launch_investigator_spawns_detached_with_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_jobs_store(tmp_path, next_run_at=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat())
    result = _record(tmp_path)
    spawned: list[list[str]] = []
    monkeypatch.setattr(investigator, "spawn_detached", spawned.append)

    decision = investigator.maybe_launch_investigator(result)

    assert decision["action"] == "spawn"
    assert len(spawned) == 1
    assert spawned[0][-4:] == ["--artifact", result["artifact_path"],
                               "--authority-token", decision["token_path"]]


def test_maybe_launch_investigator_attaches_without_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_jobs_store(tmp_path, next_run_at=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat())
    first = _record(tmp_path, stage="verify_manifest", error="first")
    monkeypatch.setattr(investigator, "spawn_detached", lambda argv: None)
    investigator.maybe_launch_investigator(first)
    second = _record(tmp_path, stage="apply_foundations", error="second")
    monkeypatch.setattr(
        investigator, "spawn_detached",
        lambda argv: (_ for _ in ()).throw(AssertionError("a live finisher must absorb the failure")),
    )

    decision = investigator.maybe_launch_investigator(second)

    assert decision["action"] == "attach"
    assert decision["attached_to"] == first["signature"]


# ── the goal contract (R12) ─────────────────────────────────────────────────


def _goal_for(tmp_path: Path) -> str:
    artifact = json.loads(Path(_record(tmp_path)["artifact_path"]).read_text(encoding="utf-8"))
    return investigator.build_goal(artifact, authority_token_path=tmp_path / "token.json")


@pytest.mark.parametrize(
    "phrase",
    [
        "orphan evidence",
        "before ANY mutation",
        "release lock",
        "cron progress transcript",
        "smallest local fix",
        "sync.py deploy --from-sha <committed sha> --provisional",
        "--authority-token",
        "REAL RECONSTRUCTION CONFLICT",
        "force-with-lease push, prerelease publish, public checksum verification",
        "re-checked in code immediately before each privileged action",
        "heartbeat --job",
        "stale heartbeat closes this incident as abandoned",
    ],
)
def test_goal_contract_states_the_allowed_path(tmp_path: Path, phrase: str) -> None:
    assert phrase in _goal_for(tmp_path)


@pytest.mark.parametrize(
    "phrase",
    [
        "running any installer",
        "changing cron jobs, schedules, or creating any cron job (no recursive scheduling)",
        "changing credentials",
        "restarting the gateway or the Desktop app",
        "deleting any release that is not an integration-* prerelease",
        "approving a reconciliation proposal",
        "refuses a non-TTY caller",
        "PROPOSALS_ALLOW_NONINTERACTIVE",
        "editing the operational copies under the Hermes scripts directory directly",
    ],
)
def test_goal_contract_forbids_the_out_of_scope_actions(tmp_path: Path, phrase: str) -> None:
    assert phrase in _goal_for(tmp_path)


def test_goal_and_prompt_carry_the_minted_token_path(tmp_path: Path) -> None:
    _write_jobs_store(tmp_path, next_run_at=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat())
    result = _record(tmp_path)
    decision = investigator.plan_investigator_launch(
        home=tmp_path, job_id=JOB_ID, signature=result["signature"],
        artifact_path=Path(result["artifact_path"]),
    )
    artifact = json.loads(Path(result["artifact_path"]).read_text(encoding="utf-8"))

    assert decision["token_path"] in investigator.build_goal(artifact)
    assert decision["token_path"] in investigator.build_prompt(artifact)


def test_the_prompt_still_mandates_orphan_evidence_before_mutation(tmp_path: Path) -> None:
    artifact = json.loads(Path(_record(tmp_path)["artifact_path"]).read_text(encoding="utf-8"))
    prompt = investigator.build_prompt(artifact)

    assert "orphan evidence" in prompt
    assert "before any mutation" in prompt
    assert "do not approve reconciliation proposals" in prompt


# ── session identity + linkage (R11) ────────────────────────────────────────


class _FakeProcess:
    def __init__(self) -> None:
        self.stopped = False

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        self.stopped = True

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        self.stopped = True


class _FakeTransport:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((method, params))
        if method == "session.create":
            return {"result": {"session_id": "sess1234", "stored_session_id": "20260815_investigator"}}
        if method == "slash.exec" and str(params.get("command", "")).startswith("goal status"):
            return {"result": {"status": "done"}}
        if method == "slash.exec":
            return {"result": {"type": "send"}}
        return {"result": {}}


def test_session_create_params_carry_the_investigator_identity(tmp_path: Path) -> None:
    artifact = json.loads(Path(_record(tmp_path)["artifact_path"]).read_text(encoding="utf-8"))

    params = investigator.session_create_params(artifact)

    assert params["source"] == "desktop"
    assert params["cron_session"] == JOB_ID
    assert params["close_on_disconnect"] is False
    assert params["title"] == f"Release investigator · {JOB_ID} · {artifact['signature'][:8]}"


def test_run_artifact_links_the_created_session_to_the_token_and_the_incident(
    tmp_path: Path,
) -> None:
    _write_jobs_store(tmp_path, next_run_at=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat())
    result = _record(tmp_path)
    decision = investigator.plan_investigator_launch(
        home=tmp_path, job_id=JOB_ID, signature=result["signature"],
        artifact_path=Path(result["artifact_path"]),
    )
    transport = _FakeTransport()

    assert investigator.run_artifact(
        Path(result["artifact_path"]),
        authority_token_path=Path(decision["token_path"]),
        transport_factory=lambda: (_FakeProcess(), transport),
        lifecycle_seconds=0.0,
    ) is True

    created = dict(transport.requests[0][1])
    assert transport.requests[0][0] == "session.create"
    assert created["source"] == "desktop"
    assert created["title"].startswith("Release investigator · ")
    # The session id the spawner could not know at mint is patched into both
    # the authority record and the incident.
    token = json.loads(Path(decision["token_path"]).read_text(encoding="utf-8"))
    assert token["session_id"] == "sess1234"
    state = json.loads(Path(result["state_path"]).read_text(encoding="utf-8"))
    assert state["open"][result["signature"]]["session_id"] == "sess1234"
    # Patching the session id leaves the token valid (digest is claim-only).
    assert investigator.verify_authority(
        token_path=decision["token_path"], job_id=JOB_ID, action="publish", home=tmp_path,
    )["ok"] is True
    goal_command = transport.requests[1][1]["command"]
    assert goal_command.startswith("goal Finish or fail-closed")
    assert decision["token_path"] in goal_command
