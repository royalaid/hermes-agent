from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator, ValidationError

import hermes_mcp_update_gate as gate
from hermes_cli import (
    update_cmd,
    update_deferred_gateway,
    update_quiesce,
    update_readiness,
    update_receipt,
    update_transaction,
)
from hermes_cli.subcommands.update import build_update_parser


TOP_LEVEL_KEYS = {
    "schema_version",
    "mode",
    "ok",
    "ready",
    "blocked",
    "reason",
    "root",
    "venv",
    "processes",
    "mcp_bridges",
    "pausable_gateways",
    "pausable_gateway_processes",
    "git",
    "last_update_receipt",
    "lease",
    "actions",
    "error",
}


def _scan(*, bridges=None, processes=None):
    return {
        "processes": list(processes or []),
        "mcp_bridges": list(bridges or []),
        "pausable_gateways": 0,
        "pausable_gateway_processes": [],
    }


def _bridge(pid: int = 41, *, owner: str = "codex", role: str = "mcp_bridge_worker"):
    actionable = owner in {"codex", "claude"}
    return {
        "pid": pid,
        "name": "python.exe",
        "cmdline": "python.exe -m agent.transports.hermes_tools_mcp_server",
        "created_at": 100.5 + pid,
        "owner": owner,
        "role": role,
        "actionable": actionable,
        "actionability": "exact_mcp_bridge" if actionable else "hard_block",
        "action": "terminate_exact_mcp" if actionable else "refuse",
    }


def _process(pid: int = 51):
    return {
        "pid": pid,
        "name": "python.exe",
        "cmdline": "python.exe -m hermes_cli.main",
        "owner": "unknown",
        "role": "other",
        "actionable": False,
        "actionability": "hard_block",
        "action": "refuse",
    }


def _gateway_process(pid: int = 61):
    return {
        "pid": pid,
        "name": "python.exe",
        "cmdline": "python.exe -m hermes_cli.main gateway run",
        "created_at": 100.5 + pid,
        "owner": "gateway",
        "role": "gateway_run",
        "actionable": False,
        "actionability": "downstream_drainable",
        "action": "pause_downstream",
    }


def _lease(root: Path):
    return {
        "schema_version": 1,
        "lease_id": "lease-readiness-123456",
        "owner_pid": os.getpid(),
        "created_at": 100,
        "expires_at": 220,
        "handoff_grace_until": 190,
        "install_root": os.path.normcase(os.path.realpath(root)),
    }


def _payload(root: Path, *, mode="preflight", ready=True, reason=None, error=None):
    actions = []
    if mode == "drain" and ready:
        actions = [
            {"type": "clear-scan", "sequence": 1},
            {"type": "clear-scan", "sequence": 2},
        ]
    return update_readiness._readiness_payload(
        mode=mode,
        root=root,
        ok=error is None,
        ready=ready,
        reason=reason,
        error=error,
        actions=actions,
        lease=_lease(root) if mode == "drain" and ready else None,
    )


def test_schema_and_runtime_validator_pin_exact_17_key_contract(tmp_path: Path):
    schema_path = Path(update_cmd.__file__).with_name("update_readiness.schema.v1.json")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    payload = _payload(tmp_path)

    assert schema["additionalProperties"] is False
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    assert set(schema["required"]) == TOP_LEVEL_KEYS
    assert set(payload) == TOP_LEVEL_KEYS
    validator.validate(payload)
    assert update_cmd.validate_update_readiness(payload) is payload
    drain = _payload(tmp_path, mode="drain")
    validator.validate(drain)
    assert update_cmd.validate_update_readiness(drain) is drain


def test_schema_keeps_blockers_and_pausable_gateways_in_separate_arrays(
    tmp_path: Path,
):
    schema_path = Path(update_cmd.__file__).with_name("update_readiness.schema.v1.json")
    validator = Draft202012Validator(
        json.loads(schema_path.read_text(encoding="utf-8"))
    )

    gateway_in_processes = _payload(
        tmp_path, ready=False, reason="processes-running"
    )
    gateway_in_processes["processes"] = [_gateway_process()]
    with pytest.raises(ValidationError):
        validator.validate(gateway_in_processes)
    with pytest.raises(ValueError):
        update_cmd.validate_update_readiness(gateway_in_processes)

    process_in_gateways = _payload(
        tmp_path, ready=False, reason="processes-running"
    )
    process_in_gateways["pausable_gateways"] = 1
    process_in_gateways["pausable_gateway_processes"] = [_process()]
    with pytest.raises(ValidationError):
        validator.validate(process_in_gateways)
    with pytest.raises(ValueError):
        update_cmd.validate_update_readiness(process_in_gateways)

    valid_gateway = _payload(tmp_path)
    valid_gateway["pausable_gateways"] = 1
    valid_gateway["pausable_gateway_processes"] = [_gateway_process()]
    validator.validate(valid_gateway)
    assert update_cmd.validate_update_readiness(valid_gateway) is valid_gateway


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["mcp_bridges"].append(_bridge(owner="unknown"))
        or payload["mcp_bridges"][-1].update(
            {"actionable": True, "actionability": "exact_mcp_bridge", "action": "terminate_exact_mcp"}
        ),
        lambda payload: payload.update(
            {
                "mode": "drain",
                "actions": [
                    {"type": "clear-scan", "sequence": 1},
                    {"type": "clear-scan", "sequence": 1},
                ],
            }
        ),
    ],
)
def test_runtime_validator_rejects_incoherent_semantics(tmp_path: Path, mutate):
    payload = _payload(tmp_path)
    mutate(payload)

    with pytest.raises(ValueError):
        update_cmd.validate_update_readiness(payload)


@pytest.mark.parametrize(
    ("payload", "expected_exit"),
    [
        (lambda root: _payload(root), 0),
        (lambda root: _payload(root, ready=False, reason="venv-blocked"), 2),
        (
            lambda root: _payload(
                root,
                ready=False,
                reason="probe-failed",
                error={"code": "probe-failed", "message": "boom"},
            ),
            1,
        ),
    ],
)
def test_preflight_json_is_one_document_with_stable_exit(
    tmp_path: Path, monkeypatch, capsys, payload, expected_exit
):
    document = payload(tmp_path)
    monkeypatch.setattr(
        update_readiness, "_build_update_preflight", lambda *_args, **_kwargs: document
    )
    args = SimpleNamespace(branch="main", json=True)

    with pytest.raises(SystemExit) as raised:
        update_cmd._cmd_update_preflight(args, root=tmp_path)

    assert raised.value.code == expected_exit
    assert json.loads(capsys.readouterr().out) == document


def test_real_preflight_dispatch_does_not_run_startup_file_cleanup(
    tmp_path: Path,
):
    root = tmp_path / "install"
    home = tmp_path / "home"
    scripts = root / "venv" / "Scripts"
    cache = root / "package" / "__pycache__"
    ref = root / ".git" / "refs" / "heads"
    events = home / "startup-events"
    scripts.mkdir(parents=True)
    cache.mkdir(parents=True)
    ref.mkdir(parents=True)
    events.mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (ref / "main").write_text("a" * 40 + "\n", encoding="utf-8")
    (root / ".bytecode-fingerprint").write_text("old-fingerprint", encoding="utf-8")
    (scripts / "hermes.exe.old.preflight").write_bytes(b"do-not-delete")
    (cache / "stale.pyc").write_bytes(b"do-not-delete")
    (home / "sentinel").write_bytes(b"do-not-touch")

    def snapshot(base: Path) -> dict[str, tuple[str, int, str]]:
        return {
            str(path.relative_to(base)): (
                "file" if path.is_file() else "directory",
                path.stat().st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest()
                if path.is_file()
                else "",
            )
            for path in base.rglob("*")
        }

    before = {"root": snapshot(root), "home": snapshot(home)}
    script = "\n".join(
        [
            "import sys",
            "from pathlib import Path",
            "root = Path(sys.argv[1])",
            "events = Path(sys.argv[2])",
            "def mark(name):",
            "    (events / name).write_text('called', encoding='utf-8')",
            "sys.argv = ['hermes', '--reasoning', 'high', 'update', '--preflight', '--json']",
            "from hermes_cli import _early_recovery",
            "_early_recovery.recover_if_needed = lambda: mark('early-recovery')",
            "from hermes_cli import env_loader",
            "env_loader.load_hermes_dotenv = lambda **_kwargs: mark('dotenv')",
            "import hermes_logging",
            "hermes_logging.setup_logging = lambda **_kwargs: mark('logging')",
            "from hermes_cli import main as m",
            "m.PROJECT_ROOT = root",
            "m._cleanup_quarantined_exes = lambda: mark('quarantine')",
            "m._sweep_stale_bytecode_if_checkout_changed = lambda: mark('bytecode')",
            "m._recover_from_interrupted_install = lambda: mark('recovery')",
            "m.main()",
        ]
    )
    env = {
        **os.environ,
        "HERMES_HOME": str(home),
        "PYTHONDONTWRITEBYTECODE": "1",
        "HERMES_DISABLE_FAST_CHAT_LAUNCH": "1",
    }

    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", script, str(root), str(events)],
        cwd=Path(update_cmd.__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode in {0, 1, 2}, result.stderr
    document = json.loads(result.stdout)
    assert set(document) == TOP_LEVEL_KEYS
    assert document["mode"] == "preflight"
    assert list(events.iterdir()) == []
    assert {"root": snapshot(root), "home": snapshot(home)} == before


@pytest.mark.parametrize(
    "prefix",
    [
        ["--profile", "update"],
        ["--safe-mode", "--profile", "update"],
        ["--profile=update"],
        ["--"],
    ],
)
def test_early_preflight_dispatch_uses_the_operative_update_index(
    monkeypatch, prefix: list[str]
) -> None:
    from hermes_cli import main as cli_main

    argv = ["hermes", *prefix, "update", "--preflight", "--json"]
    dispatched = []
    monkeypatch.setattr(cli_main.sys, "argv", argv)
    monkeypatch.setattr(cli_main, "cmd_update", dispatched.append)

    assert cli_main._try_read_only_update_preflight_launch()
    assert len(dispatched) == 1
    assert dispatched[0].preflight is True
    assert dispatched[0].json is True


def test_early_preflight_value_flags_match_authoritative_top_level_parser() -> None:
    from hermes_cli import main as cli_main
    from hermes_cli._parser import (
        PRE_ARGPARSE_INHERITED_FLAGS,
        build_top_level_parser,
    )

    parser, _subparsers, _chat_parser = build_top_level_parser()
    parser_value_flags = {
        option
        for action in parser._actions
        if action.option_strings and action.nargs != 0
        for option in action.option_strings
    }
    parser_value_flags.update(
        flag for flag, takes_value in PRE_ARGPARSE_INHERITED_FLAGS if takes_value
    )

    assert cli_main._EARLY_TOP_LEVEL_VALUE_FLAGS == parser_value_flags


def test_preflight_fails_closed_when_scanner_probe_fails(tmp_path: Path, monkeypatch):
    import hermes_cli._scan_venv_blockers as scanner

    monkeypatch.setattr(scanner, "scan_venv_blockers", lambda _root: (_ for _ in ()).throw(RuntimeError("denied")))
    monkeypatch.setattr(update_readiness, "_load_update_receipt", lambda _root: None)

    payload = update_readiness._build_update_preflight(tmp_path, "main")

    assert payload["ok"] is False
    assert payload["ready"] is False
    assert payload["reason"] == "probe-failed"
    update_cmd.validate_update_readiness(payload)


def test_foreign_update_lock_is_a_stable_preflight_blocker(tmp_path: Path, monkeypatch):
    import hermes_cli._scan_venv_blockers as scanner
    import hermes_cli.update_lock as update_lock

    holder = update_lock.UpdateHolder(
        pid=999,
        age_seconds=1.0,
        started_at=100.0,
        raw="999\n100\n",
    )
    monkeypatch.setattr(scanner, "scan_venv_blockers", lambda _root: _scan())
    monkeypatch.setattr(update_readiness, "_git_preflight_metadata", lambda *_args: None)
    monkeypatch.setattr(update_readiness, "_load_update_receipt", lambda _root: None)
    monkeypatch.setattr(update_readiness, "_read_update_holder_read_only", lambda: holder)
    monkeypatch.setattr(update_lock, "_handoff_pid", lambda: None)
    monkeypatch.setattr(update_lock, "_is_ancestor_pid", lambda _pid: False)
    monkeypatch.setattr(gate, "live_quiesce_lease", lambda *_args, **_kwargs: None)

    payload = update_readiness._build_update_preflight(tmp_path, "main")

    assert payload["ready"] is False
    assert payload["reason"] == "update-running"
    assert payload["processes"][0]["role"] == "update_lock_holder"
    update_cmd.validate_update_readiness(payload)


def test_independent_preflight_does_not_promise_an_active_lease_can_be_adopted(
    tmp_path: Path, monkeypatch
):
    import hermes_cli._scan_venv_blockers as scanner

    lease = _lease(tmp_path)
    monkeypatch.setattr(scanner, "scan_venv_blockers", lambda _root: _scan())
    monkeypatch.setattr(update_readiness, "_git_preflight_metadata", lambda *_args: None)
    monkeypatch.setattr(update_readiness, "_load_update_receipt", lambda _root: None)
    monkeypatch.setattr(update_readiness, "_read_update_holder_read_only", lambda: None)
    monkeypatch.setattr(gate, "live_quiesce_lease", lambda *_args, **_kwargs: lease)

    payload = update_readiness._build_update_preflight(tmp_path, "main")

    assert payload["ready"] is False
    assert payload["reason"] == "quiesce-lease-active"
    assert payload["lease"] == update_readiness._public_quiesce_lease(lease)
    assert "lease_id" not in payload["lease"]
    update_cmd.validate_update_readiness(payload)


def test_standalone_drain_pause_is_not_an_adoptable_update_handoff(
    tmp_path: Path, monkeypatch
):
    home = tmp_path / ".hermes"
    root = home / "hermes-agent"
    root.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    now = int(time.time())
    dead_owner_pid = 2_000_000_000
    pause = gate.write_quiesce_lease(
        root,
        owner_pid=dead_owner_pid,
        now=now,
        handoff_grace_seconds=90,
    )

    # A separate updater has no capability or ancestry relation to the drain
    # child. The bounded pause is intentionally not a drain-then-apply seam.
    with pytest.raises(RuntimeError, match="another updater owns"):
        update_quiesce._claim_update_quiesce_lease(root)

    expired_pause = {
        **pause,
        "created_at": now - 100,
        "expires_at": now + 100,
        "handoff_grace_until": now - 10,
    }
    gate.marker_path().write_text(
        json.dumps(expired_pause, sort_keys=True), encoding="utf-8"
    )

    claimed = update_quiesce._claim_update_quiesce_lease(root)
    assert claimed["owner_pid"] == os.getpid()
    assert claimed["lease_id"] != pause["lease_id"]
    assert update_quiesce._release_update_quiesce_lease(root, claimed)


def test_preflight_accepts_exact_capability_owned_by_live_ancestor(
    tmp_path: Path, monkeypatch
):
    import hermes_cli._scan_venv_blockers as scanner
    import hermes_cli.update_lock as update_lock

    lease = {**_lease(tmp_path), "owner_pid": 777}
    monkeypatch.setattr(scanner, "scan_venv_blockers", lambda _root: _scan())
    monkeypatch.setattr(update_readiness, "_git_preflight_metadata", lambda *_args: None)
    monkeypatch.setattr(update_readiness, "_load_update_receipt", lambda _root: None)
    monkeypatch.setattr(update_readiness, "_read_update_holder_read_only", lambda: None)
    monkeypatch.setattr(gate, "live_quiesce_lease", lambda *_args, **_kwargs: lease)
    monkeypatch.setattr(update_lock, "_handoff_pid", lambda: None)
    monkeypatch.setattr(update_lock, "_is_ancestor_pid", lambda pid: pid == 777)

    payload = update_readiness._build_update_preflight(
        tmp_path,
        "main",
        expected_lease_id=lease["lease_id"],
    )

    assert payload["ready"] is True
    assert payload["reason"] is None
    assert payload["lease"] is None
    update_cmd.validate_update_readiness(payload)


def test_matching_capability_cannot_be_adopted_from_unrelated_owner(
    tmp_path: Path, monkeypatch
):
    import hermes_cli.update_lock as update_lock

    tmp_path.mkdir(exist_ok=True)
    lease = gate.write_quiesce_lease(
        tmp_path,
        owner_pid=os.getpid() + 1000,
    )
    monkeypatch.setattr(update_lock, "_is_ancestor_pid", lambda _pid: False)

    with pytest.raises(RuntimeError, match="not this process or its live ancestor"):
        update_quiesce._claim_update_quiesce_lease(
            tmp_path, expected_lease_id=lease["lease_id"]
        )


def test_heartbeat_loss_fail_stops_before_another_mutation(
    tmp_path: Path, monkeypatch
):
    home = tmp_path / "home"
    root = tmp_path / "install"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    lease = gate.write_quiesce_lease(root, owner_pid=os.getpid())
    gate.marker_path().write_text("foreign-malformed", encoding="utf-8")
    events: list[str] = []
    heartbeat = update_quiesce._UpdateLeaseHeartbeat(
        root,
        lease,
        fail_stop=lambda reason: events.append(f"fail-stop:{reason}"),
    )

    if heartbeat._renew_once():
        events.append("later-mutation")

    assert heartbeat.lost
    assert events and events[0].startswith("fail-stop:")
    assert "later-mutation" not in events
    assert gate.live_quiesce_lease(
        gate.marker_path(), install_root=root, pid_alive=lambda _pid: False
    ) is not None


def test_heartbeat_probe_exception_fail_stops_before_renewal(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    root = tmp_path / "install"
    root.mkdir()
    lease = {
        "schema_version": 1,
        "lease_id": "heartbeat-probe-lease-1234",
        "owner_pid": os.getpid(),
        "created_at": 100,
        "expires_at": 200,
        "handoff_grace_until": 100,
        "install_root": str(root.resolve()),
    }
    events: list[str] = []
    monkeypatch.setattr(
        gate,
        "read_quiesce_lease",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PermissionError("sharing violation")
        ),
    )
    monkeypatch.setattr(
        gate,
        "write_quiesce_lease",
        lambda *_args, **_kwargs: events.append("renewed"),
    )
    heartbeat = update_quiesce._UpdateLeaseHeartbeat(
        root,
        lease,
        fail_stop=lambda reason: events.append(f"fail-stop:{reason}"),
    )

    assert not heartbeat._renew_once()
    assert heartbeat.lost
    assert events and events[0].startswith("fail-stop:bridge quiesce lease probe failed")
    assert "renewed" not in events


@pytest.mark.windows_only
def test_windows_fail_stop_job_kills_an_already_spawned_mutator(tmp_path: Path):
    sentinel = tmp_path / "orphan-survived.txt"
    child_code = (
        "import pathlib,time; time.sleep(1.5); "
        f"pathlib.Path({str(sentinel)!r}).write_text('survived', encoding='utf-8')"
    )
    helper_code = "\n".join(
        [
            "import subprocess,sys,time",
            "from hermes_cli.update_cmd import _WindowsMutationJob",
            "job = _WindowsMutationJob()",
            f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}])",
            "print(child.pid, flush=True)",
            "time.sleep(0.2)",
            "job.abort('lease lost')",
        ]
    )
    helper = subprocess.Popen(
        [sys.executable, "-c", helper_code],
        cwd=Path(update_cmd.__file__).parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    child_pid_line = helper.stdout.readline().strip()
    stderr = ""
    try:
        helper.wait(timeout=10)
        stderr = helper.stderr.read()
    finally:
        if helper.poll() is None:
            helper.kill()
            helper.wait(timeout=5)
    assert child_pid_line.isdigit(), stderr
    time.sleep(1.8)
    assert not sentinel.exists(), "lease fail-stop orphaned a live mutating child"


@pytest.mark.windows_only
def test_windows_normal_cleanup_refuses_to_disarm_with_lingering_mutator(
    tmp_path: Path,
):
    sentinel = tmp_path / "cleanup-orphan-survived.txt"
    child_code = (
        "import pathlib,time; time.sleep(1.5); "
        f"pathlib.Path({str(sentinel)!r}).write_text('survived', encoding='utf-8')"
    )
    helper_code = "\n".join(
        [
            "import subprocess,sys",
            "from hermes_cli.update_cmd import _WindowsMutationJob",
            "job = _WindowsMutationJob()",
            f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}])",
            "print(child.pid, flush=True)",
            "job.disarm(timeout_seconds=0.2)",
        ]
    )
    helper = subprocess.Popen(
        [sys.executable, "-c", helper_code],
        cwd=Path(update_cmd.__file__).parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    child_pid_line = helper.stdout.readline().strip()
    stderr = ""
    try:
        helper.wait(timeout=10)
        stderr = helper.stderr.read()
    finally:
        if helper.poll() is None:
            helper.kill()
            helper.wait(timeout=5)
    assert child_pid_line.isdigit(), stderr
    time.sleep(1.8)
    assert not sentinel.exists(), "cleanup disarmed containment around a live mutator"


@pytest.mark.windows_only
def test_windows_trusted_gateway_runs_after_mutation_job_disarm(
    tmp_path: Path,
):
    sentinel = tmp_path / "gateway-restarted.txt"
    gateway_code = (
        "import pathlib,time; time.sleep(0.8); "
        f"pathlib.Path({str(sentinel)!r}).write_text('restarted', encoding='utf-8')"
    )
    helper_code = "\n".join(
        [
            "import subprocess,sys",
            "from hermes_cli.update_cmd import _WindowsMutationJob",
            "job = _WindowsMutationJob()",
            "mutator = subprocess.Popen([sys.executable, '-c', 'pass'])",
            "mutator.wait(timeout=5)",
            "job.disarm(timeout_seconds=2)",
            # The pinned containment contract never grants BREAKAWAY_OK. Once
            # the inner mutation Job is drained and closed, the trusted resume
            # uses ordinary detached creation outside that Job.
            f"gateway = subprocess.Popen([sys.executable, '-c', {gateway_code!r}], creationflags=subprocess.CREATE_NO_WINDOW)",
            "gateway.wait(timeout=5)",
            "print(gateway.returncode, flush=True)",
        ]
    )
    helper = subprocess.run(
        [sys.executable, "-c", helper_code],
        cwd=Path(update_cmd.__file__).parents[1],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert helper.returncode == 0, helper.stderr
    assert helper.stdout.strip() == "0", helper.stderr
    assert sentinel.read_text(encoding="utf-8") == "restarted"


def test_preflight_refuses_mismatched_lease_capability(tmp_path: Path, monkeypatch):
    import hermes_cli._scan_venv_blockers as scanner

    lease = _lease(tmp_path)
    monkeypatch.setattr(scanner, "scan_venv_blockers", lambda _root: _scan())
    monkeypatch.setattr(update_readiness, "_git_preflight_metadata", lambda *_args: None)
    monkeypatch.setattr(update_readiness, "_load_update_receipt", lambda _root: None)
    monkeypatch.setattr(update_readiness, "_read_update_holder_read_only", lambda: None)
    monkeypatch.setattr(gate, "live_quiesce_lease", lambda *_args, **_kwargs: lease)

    payload = update_readiness._build_update_preflight(
        tmp_path,
        "main",
        expected_lease_id="different-lease-123456",
    )

    assert payload["ready"] is False
    assert payload["reason"] == "lease-capability-mismatch"


def test_drain_handles_clear_then_respawn_and_requires_fresh_two_scan_proof(
    tmp_path: Path, monkeypatch
):
    import hermes_cli._scan_venv_blockers as scanner

    bridge = _bridge()
    scans = iter([_scan(), _scan(bridges=[bridge]), _scan(bridges=[bridge]), _scan(), _scan()])
    terminated = []
    monkeypatch.setattr(scanner, "scan_venv_blockers", lambda _root: next(scans))
    monkeypatch.setattr(
        scanner,
        "terminate_mcp_bridge",
        lambda _root, *, pid, created_at: terminated.append((pid, created_at)) or True,
    )
    monkeypatch.setattr(update_quiesce, "_git_preflight_metadata", lambda *_args: None)
    monkeypatch.setattr(update_quiesce, "_load_update_receipt", lambda _root: None)
    monkeypatch.setattr(update_quiesce._time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(gate, "write_quiesce_lease", lambda *_args, **_kwargs: _lease(tmp_path))

    payload = update_quiesce._drain_under_update_lease(
        tmp_path,
        _lease(tmp_path),
        branch="main",
        timeout_seconds=5,
    )

    assert payload["ready"] is True
    assert terminated == [(bridge["pid"], bridge["created_at"])]
    assert [
        action["sequence"]
        for action in payload["actions"]
        if action["type"] == "clear-scan"
    ] == [1, 2]
    update_cmd.validate_update_readiness(payload)


def test_drain_acquires_outer_lock_then_lease_before_actionable_scan(
    tmp_path: Path, monkeypatch, capsys
):
    import hermes_cli.update_lock as update_lock

    events = []

    class FakeLock:
        acquired = True
        holder = None
        failure_reason = None
        path = tmp_path / ".hermes-update-in-progress"

        def acquire(self):
            events.append("lock-acquire")
            return True

        def prove_claim(self):
            events.append("lock-prove")
            return True

        def release(self):
            events.append("lock-release")

    lease = _lease(tmp_path)
    monkeypatch.setattr(update_lock, "UpdateLock", FakeLock)
    monkeypatch.setattr(
        update_quiesce,
        "_claim_update_quiesce_lease",
        lambda _root: events.append("lease-acquire") or lease,
    )
    monkeypatch.setattr(
        update_quiesce,
        "_release_update_quiesce_lease",
        lambda *_args: events.append("lease-release") or True,
    )

    def drain(*_args, **_kwargs):
        events.append("first-scan")
        return _payload(tmp_path, mode="drain")

    monkeypatch.setattr(update_quiesce, "_drain_under_update_lease", drain)
    args = SimpleNamespace(yes=True, json=True, branch="main", timeout_seconds=5)

    with pytest.raises(SystemExit) as raised:
        update_cmd._cmd_update_drain(args, root=tmp_path)

    assert raised.value.code == 0
    assert events == [
        "lock-acquire",
        "lock-prove",
        "lease-acquire",
        "first-scan",
        "lock-release",
    ]
    json.loads(capsys.readouterr().out)


def test_drain_cleanup_failure_still_releases_outer_lock(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import hermes_cli.update_lock as update_lock

    events: list[str] = []
    lease = _lease(tmp_path)

    class FakeLock:
        holder = None
        failure_reason = None

        def acquire(self):
            events.append("lock-acquire")
            return True

        def prove_claim(self):
            events.append("lock-prove")
            return True

        def release(self):
            events.append("lock-release")

    monkeypatch.setattr(update_lock, "UpdateLock", FakeLock)
    monkeypatch.setattr(
        update_quiesce,
        "_claim_update_quiesce_lease",
        lambda _root: events.append("lease-acquire") or lease,
    )
    monkeypatch.setattr(
        update_quiesce,
        "_drain_under_update_lease",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("scan failed")
        ),
    )
    monkeypatch.setattr(
        update_quiesce,
        "_release_update_quiesce_lease",
        lambda *_args: (_ for _ in ()).throw(PermissionError("cleanup denied")),
    )
    args = SimpleNamespace(yes=True, json=True, branch="main", timeout_seconds=5)

    with pytest.raises(SystemExit) as raised:
        update_cmd._cmd_update_drain(args, root=tmp_path)

    captured = capsys.readouterr()
    assert raised.value.code == 1
    assert events == [
        "lock-acquire",
        "lock-prove",
        "lease-acquire",
        "lock-release",
    ]
    assert len(captured.out.splitlines()) == 1
    payload = json.loads(captured.out)
    assert payload["reason"] == "lease-failed"
    assert payload["error"] == {
        "code": "lease-cleanup-failed",
        "message": "cleanup denied",
    }


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "0", "-1", "120.1"])
def test_update_parser_rejects_nonfinite_or_out_of_range_timeout(value: str):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_update_parser(subparsers, cmd_update=lambda _args: None)

    with pytest.raises(SystemExit) as raised:
        parser.parse_args(["update", "--drain", "--yes", "--timeout-seconds", value])

    assert raised.value.code == 2


def test_update_help_discloses_windows_mcp_interruption_consent():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_update_parser(subparsers, cmd_update=lambda _args: None)

    help_text = " ".join(
        parser._subparsers._group_actions[0].choices["update"].format_help().split()
    )

    assert "Codex or Claude Hermes MCP bridges" in help_text
    assert "exact, verified" in help_text


def test_real_parser_to_check_path_uses_selected_remote_and_forced_refspec(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    fake_main = SimpleNamespace(PROJECT_ROOT=root)
    monkeypatch.setattr(update_cmd, "_m", lambda: fake_main)
    monkeypatch.setattr("hermes_cli.config.detect_install_method", lambda _root: "git")
    commands = []

    def run(command, **_kwargs):
        commands.append([str(part) for part in command])
        joined = " ".join(str(part) for part in command)
        if "config --get branch.feature.remote" in joined:
            return subprocess.CompletedProcess(command, 0, "fork\n", "")
        if "remote get-url -- fork" in joined:
            return subprocess.CompletedProcess(command, 0, "file:///fork\n", "")
        if "config --includes --show-origin --show-scope --name-only --get-regexp" in joined:
            return subprocess.CompletedProcess(command, 1, "", "")
        if "rev-parse --is-shallow-repository" in joined:
            return subprocess.CompletedProcess(command, 0, "false\n", "")
        if "rev-list" in joined:
            return subprocess.CompletedProcess(command, 0, "0\n", "")
        return subprocess.CompletedProcess(command, 0, "abc1234\n", "")

    monkeypatch.setattr(update_cmd.subprocess, "run", run)

    def handler(args):
        return update_cmd._cmd_update_check(
            branch=args.branch or "main", branch_explicit=args.branch is not None
        )

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_update_parser(subparsers, cmd_update=handler)
    args = parser.parse_args(["update", "--check", "--branch", "feature"])
    args.func(args)

    fetch = next(command for command in commands if "fetch" in command)
    assert fetch[-3:] == [
        "--",
        "fork",
        "+refs/heads/feature:refs/remotes/fork/feature",
    ]
    assert any("HEAD..refs/remotes/fork/feature" in command for command in commands)


def test_malicious_tracking_remote_is_rejected_before_git_uses_it(
    tmp_path: Path, monkeypatch
):
    seen = []

    def run(command, **_kwargs):
        seen.append(command)
        if "config" in command:
            return subprocess.CompletedProcess(command, 0, "--upload-pack=evil\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(update_cmd.subprocess, "run", run)

    with pytest.raises(ValueError, match="invalid update remote"):
        update_readiness._resolve_update_target(["git"], tmp_path, "main")

    assert not any("remote" in command for command in seen)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=update_readiness._sanitized_git_env(),
    )
    return result.stdout.strip()


def test_divergent_fork_uses_same_remote_ref_for_fetch_compare_and_reset(tmp_path: Path):
    origin = tmp_path / "origin.git"
    fork = tmp_path / "fork.git"
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "--bare", str(origin))
    _git(tmp_path, "init", "--bare", str(fork))
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("base", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")
    _git(repo, "branch", "-M", "main")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "remote", "add", "fork", str(fork))
    _git(repo, "push", "origin", "main")
    _git(repo, "push", "fork", "main")

    (repo / "origin.txt").write_text("origin", encoding="utf-8")
    _git(repo, "add", "origin.txt")
    _git(repo, "commit", "-m", "origin")
    origin_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "origin", "main")

    _git(repo, "reset", "--hard", base)
    (repo / "fork.txt").write_text("fork", encoding="utf-8")
    _git(repo, "add", "fork.txt")
    _git(repo, "commit", "-m", "fork")
    fork_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "--force", "fork", "main")
    _git(repo, "reset", "--hard", origin_sha)
    _git(repo, "config", "branch.main.remote", "fork")

    target = update_readiness._resolve_update_target(["git"], repo, "main")
    _git(repo, "fetch", "--", target.remote, target.refspec)
    merge = subprocess.run(
        ["git", "merge", "--ff-only", target.tracking_ref],
        cwd=repo,
        capture_output=True,
        env=update_readiness._sanitized_git_env(),
    )
    assert merge.returncode != 0
    _git(repo, "reset", "--hard", target.tracking_ref)

    assert target.remote == "fork"
    assert target.refspec == "+refs/heads/main:refs/remotes/fork/main"
    assert _git(repo, "rev-parse", "HEAD") == fork_sha
    assert _git(repo, "rev-parse", "refs/remotes/origin/main") == origin_sha


def test_preflight_git_probe_is_read_only_and_ignores_routing_env(
    tmp_path: Path, monkeypatch
):
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    hostile = tmp_path / "hostile"
    _git(tmp_path, "init", "--bare", str(remote))
    repo.mkdir()
    hostile.mkdir()
    _git(repo, "init")
    _git(hostile, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "file.txt").write_text("data", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "base")
    _git(repo, "branch", "-M", "main")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")

    tracked = [repo / ".git" / "index", repo / ".git" / "refs" / "heads" / "main"]
    before = {
        path: (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in tracked
    }
    objects_before = sorted(path.relative_to(repo / ".git") for path in (repo / ".git" / "objects").rglob("*"))
    monkeypatch.setenv("GIT_DIR", str(hostile / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(hostile))
    monkeypatch.setenv("GIT_INDEX_FILE", str(hostile / "index"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hostile))

    metadata = update_readiness._git_preflight_metadata(repo, "main")

    assert metadata is not None
    assert metadata["head"] == _git(repo, "rev-parse", "HEAD")
    assert metadata["tracking_remote"] == "origin"
    assert not (repo / ".git" / "index.lock").exists()
    assert before == {
        path: (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in tracked
    }
    assert objects_before == sorted(path.relative_to(repo / ".git") for path in (repo / ".git" / "objects").rglob("*"))


def test_mutation_guard_sanitizes_git_routing_for_all_inherited_helpers(
    tmp_path: Path, monkeypatch
):
    hostile = {
        "GIT_DIR": str(tmp_path / "foreign.git"),
        "GIT_WORK_TREE": str(tmp_path / "foreign-tree"),
        "GIT_INDEX_FILE": str(tmp_path / "foreign-index"),
        "GIT_OBJECT_DIRECTORY": str(tmp_path / "foreign-objects"),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": str(tmp_path / "hooks"),
        "GIT_SSH_COMMAND": str(tmp_path / "sentinel-ssh-command"),
        "GIT_SSH": str(tmp_path / "sentinel-ssh"),
        "GIT_ASKPASS": str(tmp_path / "sentinel-askpass"),
        "GIT_PROXY_COMMAND": str(tmp_path / "sentinel-proxy"),
        "GIT_EXTERNAL_DIFF": str(tmp_path / "sentinel-diff"),
        "GIT_PAGER": str(tmp_path / "sentinel-pager"),
    }
    for key, value in hostile.items():
        monkeypatch.setenv(key, value)

    with update_readiness._GitRoutingEnvironmentGuard():
        inherited = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json,os; print(json.dumps({k:v for k,v in os.environ.items() if k.startswith('GIT_')}))",
            ],
            capture_output=True,
            check=True,
            text=True,
        )
        child_env = json.loads(inherited.stdout)
        assert not (set(hostile) & set(child_env))

    assert {key: os.environ.get(key) for key in hostile} == hostile


def test_git_command_disables_repository_hooks() -> None:
    command = update_readiness._git_cmd()

    assert "-c" in command
    assert f"core.hooksPath={os.devnull}" in command
    assert "core.fsmonitor=false" in command
    assert f"core.attributesFile={os.devnull}" in command
    assert "protocol.ext.allow=never" in command


@pytest.mark.parametrize(
    "selector",
    [
        "filter.evil.clean",
        "filter.evil.smudge",
        "filter.evil.process",
        "merge.evil.driver",
        "core.sshCommand",
        "core.gitProxy",
        "credential.helper",
        "url.file:///foreign.insteadOf",
        "url.file:///foreign.pushInsteadOf",
    ],
)
def test_local_executable_git_configuration_is_refused_before_worktree_access(
    tmp_path: Path, selector: str
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", selector, "python sentinel.py")

    with pytest.raises(RuntimeError, match="executable filter/merge"):
        update_readiness._assert_safe_git_configuration(update_readiness._git_cmd(), repo)


def test_worktree_scope_executable_git_configuration_is_refused(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "extensions.worktreeConfig", "true")
    _git(repo, "config", "--worktree", "filter.evil.process", "python sentinel.py")

    with pytest.raises(RuntimeError, match="executable filter/merge"):
        update_readiness._assert_safe_git_configuration(update_readiness._git_cmd(), repo)


@pytest.mark.parametrize(
    "operation",
    [
        ["status", "--porcelain"],
        ["stash", "push", "--include-untracked", "-m", "test"],
        ["checkout", "--", "tracked.txt"],
        ["reset", "--hard", "HEAD"],
        ["restore", "--", "tracked.txt"],
    ],
    ids=["status", "stash", "checkout", "reset", "restore"],
)
def test_hardened_git_commands_do_not_load_global_executable_filters(
    tmp_path: Path, monkeypatch, operation: list[str]
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / ".gitattributes").write_text(
        "tracked.txt filter=evil merge=evil\n", encoding="utf-8"
    )
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".gitattributes", "tracked.txt")
    _git(repo, "commit", "-m", "base")

    sentinel = tmp_path / "git-config-executed"
    driver = tmp_path / "filter_driver.py"
    driver.write_text(
        "import pathlib,sys\n"
        f"pathlib.Path({str(sentinel)!r}).write_text('executed')\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n",
        encoding="utf-8",
    )
    command = f'"{sys.executable}" "{driver}"'
    hostile_global = tmp_path / "hostile.gitconfig"
    hostile_global.write_text(
        "[filter \"evil\"]\n"
        f"\tclean = {command}\n"
        f"\tsmudge = {command}\n"
        f"\tprocess = {command}\n"
        "[merge \"evil\"]\n"
        f"\tdriver = {command}\n"
        "[core]\n"
        f"\tfsmonitor = {command}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile_global))
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")

    result = subprocess.run(
        update_readiness._git_cmd() + operation,
        cwd=repo,
        capture_output=True,
        text=True,
        env=update_readiness._sanitized_git_env(),
    )

    assert result.returncode == 0, result.stderr
    assert not sentinel.exists()


def _successful_receipt(root: Path, *, mode: str) -> dict[str, object]:
    git_mode = mode == "git"
    return {
        "schema_version": 1,
        "invocation_id": "invocation-test-123456",
        "lease_id": "lease-readiness-123456",
        "mode": mode,
        "root": os.path.normcase(os.path.realpath(root)),
        "remote": "origin" if git_mode else None,
        "branch": "main",
        "target_ref": "refs/remotes/origin/main" if git_mode else None,
        "target_sha": "a" * 40 if git_mode else None,
        "resulting_head": "a" * 40 if git_mode else None,
        "archive_sha": None if git_mode else "a" * 64,
        "timestamp": 100,
        "success": True,
        "gateway_resume_deferred": False,
        "health": {
            "critical_syntax": True,
            "critical_imports": True,
            "dependencies": True,
            "node_dependencies": True,
        },
    }


@pytest.mark.parametrize(
    ("mode", "invalid_length"),
    [("git", 39), ("git", 41), ("archive", 63), ("archive", 65)],
)
def test_receipt_sha_lengths_are_exact_negative_boundaries(
    tmp_path: Path,
    mode: str,
    invalid_length: int,
):
    receipt = _successful_receipt(tmp_path, mode=mode)
    if mode == "git":
        receipt["target_sha"] = "a" * invalid_length
        receipt["resulting_head"] = "a" * invalid_length
    else:
        receipt["archive_sha"] = "a" * invalid_length

    assert update_receipt._sanitize_update_receipt(receipt, tmp_path) is None

    payload = _payload(tmp_path)
    payload["last_update_receipt"] = receipt
    schema_path = Path(update_cmd.__file__).with_name("update_readiness.schema.v1.json")
    validator = Draft202012Validator(
        json.loads(schema_path.read_text(encoding="utf-8"))
    )
    with pytest.raises(ValidationError):
        validator.validate(payload)
    with pytest.raises(ValueError):
        update_cmd.validate_update_readiness(payload)


def test_receipt_is_profile_global_and_requires_current_live_lease(
    tmp_path: Path, monkeypatch
):
    home = tmp_path / "home"
    profile = home / "profiles" / "work"
    root = tmp_path / "install"
    root.mkdir()
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    fake_main = SimpleNamespace(PROJECT_ROOT=root)
    monkeypatch.setattr(update_cmd, "_m", lambda: fake_main)
    lease = gate.write_quiesce_lease(root, owner_pid=os.getpid())
    args = SimpleNamespace()
    transaction = update_transaction._UpdateTransaction(
        invocation_id="invocation-test-123456",
        lease=lease,
    )
    health = {
        "critical_syntax": True,
        "critical_imports": True,
        "dependencies": True,
        "node_dependencies": True,
    }

    receipt = update_cmd._record_update_success(
        args,
        transaction=transaction,
        mode="git",
        branch="main",
        remote="origin",
        target_ref="refs/remotes/origin/main",
        target_sha="a" * 40,
        resulting_head="a" * 40,
        archive_sha=None,
        health=health,
    )

    receipt_path = home / update_receipt._UPDATE_RECEIPT_NAME
    assert update_receipt._receipt_path(root) == receipt_path
    assert receipt_path.exists()
    assert receipt["lease_id"] == lease["lease_id"]

    foreign = {**lease, "lease_id": "foreign-lease-123456", "owner_pid": os.getpid() + 1}
    gate.marker_path().write_text(json.dumps(foreign), encoding="utf-8")
    with pytest.raises(RuntimeError, match="ownership was lost"):
        update_cmd._record_update_success(
            args,
            transaction=transaction,
            mode="git",
            branch="main",
            remote="origin",
            target_ref="refs/remotes/origin/main",
            target_sha="b" * 40,
            resulting_head="b" * 40,
            archive_sha=None,
            health=health,
        )
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["target_sha"] == "a" * 40


def test_receipt_refuses_unproven_health(tmp_path: Path, monkeypatch):
    root = tmp_path / "install"
    home = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(update_cmd, "_m", lambda: SimpleNamespace(PROJECT_ROOT=root))
    lease = gate.write_quiesce_lease(root, owner_pid=os.getpid())
    args = SimpleNamespace()
    transaction = update_transaction._UpdateTransaction(
        invocation_id="invocation-test-123456",
        lease=lease,
    )

    with pytest.raises(RuntimeError, match="health proof"):
        update_cmd._record_update_success(
            args,
            transaction=transaction,
            mode="archive",
            branch="main",
            remote=None,
            target_ref=None,
            target_sha=None,
            resulting_head=None,
            archive_sha="a" * 64,
            health={
                "critical_syntax": True,
                "critical_imports": True,
                "dependencies": True,
                "node_dependencies": False,
            },
        )


def test_deferred_receipt_failure_preserves_pre_stop_recovery_plan(
    tmp_path: Path, monkeypatch
):
    home = tmp_path / "home"
    root = home / "hermes-agent"
    root.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(update_cmd, "_m", lambda: SimpleNamespace(PROJECT_ROOT=root))
    lease = gate.write_quiesce_lease(root, owner_pid=os.getpid())
    args = SimpleNamespace(
        defer_gateway_resume=True,
    )
    transaction = update_transaction._UpdateTransaction(
        invocation_id="invocation-receipt-123456",
        lease=lease,
        gateway_resume_plan={
            "resume_needed": False,
            "profiles": {},
            "profile_identities": {},
            "unmapped_pids": [],
            "unmapped": [],
            "cold_start_if_installed": False,
        },
    )
    monkeypatch.setattr(
        update_cmd,
        "_write_update_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("injected receipt publication failure")
        ),
    )

    with pytest.raises(OSError, match="receipt publication"):
        update_cmd._record_update_success(
            args,
            transaction=transaction,
            mode="git",
            branch="main",
            remote="origin",
            target_ref="refs/remotes/origin/main",
            target_sha="a" * 40,
            resulting_head="a" * 40,
            archive_sha=None,
            health={
                "critical_syntax": True,
                "critical_imports": True,
                "dependencies": True,
                "node_dependencies": True,
            },
        )

    plan = update_deferred_gateway._deferred_gateway_plan_path(
        root, transaction.invocation_id
    )
    assert plan.is_file()
    assert update_deferred_gateway._load_deferred_gateway_plan(
        plan,
        root=root,
        invocation_id=transaction.invocation_id,
        lease_id=lease["lease_id"],
    ) is not None


def test_receipt_sanitizer_rejects_false_health_field(tmp_path: Path):
    value = {
        "schema_version": 1,
        "invocation_id": "invocation-test-123456",
        "lease_id": "lease-readiness-123456",
        "mode": "git",
        "root": os.path.normcase(os.path.realpath(tmp_path)),
        "remote": "origin",
        "branch": "main",
        "target_ref": "refs/remotes/origin/main",
        "target_sha": "a" * 40,
        "resulting_head": "a" * 40,
        "archive_sha": None,
        "timestamp": 100,
        "success": True,
        "gateway_resume_deferred": False,
        "health": {
            "critical_syntax": True,
            "critical_imports": True,
            "dependencies": True,
            "node_dependencies": False,
        },
    }

    assert update_receipt._sanitize_update_receipt(value, tmp_path) is None


def test_receipt_sanitizer_rejects_git_head_that_does_not_match_target(
    tmp_path: Path,
):
    value = {
        "schema_version": 1,
        "invocation_id": "invocation-test-123456",
        "lease_id": "lease-readiness-123456",
        "mode": "git",
        "root": os.path.normcase(os.path.realpath(tmp_path)),
        "remote": "origin",
        "branch": "main",
        "target_ref": "refs/remotes/origin/main",
        "target_sha": "a" * 40,
        "resulting_head": "b" * 40,
        "archive_sha": None,
        "timestamp": 100,
        "success": True,
        "gateway_resume_deferred": False,
        "health": {
            "critical_syntax": True,
            "critical_imports": True,
            "dependencies": True,
            "node_dependencies": True,
        },
    }

    assert update_receipt._sanitize_update_receipt(value, tmp_path) is None


def _deferred_transaction(*, token: dict) -> update_transaction._UpdateTransaction:
    return update_transaction._UpdateTransaction(
        invocation_id="invocation-deferred-123456",
        lease={"lease_id": "lease-deferred-123456"},
        gateway_resume_plan=token,
    )


def test_deferred_gateway_plan_is_private_exact_and_multi_profile(
    tmp_path: Path, monkeypatch
):
    home = tmp_path / ".hermes"
    root = home / "hermes-agent"
    root.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    token = {
        "resume_needed": True,
        "profiles": {"default": 101, "work": 202},
        "profile_identities": {
            "default": {"pid": 101, "created_at": 100.25},
            "work": {"pid": 202, "created_at": 200.5},
        },
        "unmapped_pids": [],
        "unmapped": [],
        "cold_start_if_installed": False,
    }
    transaction = _deferred_transaction(token=token)

    path = update_deferred_gateway._write_deferred_gateway_plan(
        root, transaction=transaction
    )
    raw = json.loads(path.read_text(encoding="utf-8"))

    assert path == home / ".hermes-gateway-resume-invocation-deferred-123456.json"
    assert set(raw) == {
        "schema_version",
        "invocation_id",
        "lease_fingerprint",
        "install_root",
        "created_at",
        "expires_at",
        "profiles",
        "cold_start_if_installed",
        "auth",
    }
    assert [entry["name"] for entry in raw["profiles"]] == ["default", "work"]
    assert "lease-deferred-123456" not in path.read_text(encoding="utf-8")
    assert update_deferred_gateway._sanitize_deferred_gateway_plan(
        raw,
        root=root,
        invocation_id=transaction.invocation_id,
        lease_id=transaction.lease["lease_id"],
    ) is not None


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("schema_version", True),
        ("created_at", 100.0),
        ("expires_at", 200.0),
    ],
)
def test_deferred_gateway_plan_rejects_noncanonical_top_level_types(
    tmp_path: Path, monkeypatch, field: str, replacement
):
    home = tmp_path / ".hermes"
    root = home / "hermes-agent"
    root.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    transaction = _deferred_transaction(
        token={
            "resume_needed": False,
            "profiles": {},
            "profile_identities": {},
            "unmapped_pids": [],
            "unmapped": [],
            "cold_start_if_installed": False,
        },
    )
    payload = json.loads(
        update_deferred_gateway._write_deferred_gateway_plan(
            root, transaction=transaction
        ).read_text(encoding="utf-8")
    )
    payload[field] = replacement
    unsigned = {key: value for key, value in payload.items() if key != "auth"}
    payload["auth"] = update_deferred_gateway._gateway_plan_auth(
        unsigned, transaction.lease["lease_id"]
    )

    assert update_deferred_gateway._sanitize_deferred_gateway_plan(
        payload,
        root=root,
        invocation_id=transaction.invocation_id,
        lease_id=transaction.lease["lease_id"],
    ) is None


def test_deferred_gateway_plan_writer_requires_matching_profile_pid(
    tmp_path: Path, monkeypatch
):
    home = tmp_path / ".hermes"
    root = home / "hermes-agent"
    root.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    transaction = _deferred_transaction(
        token={
            "resume_needed": True,
            "profiles": {"work": 101},
            "profile_identities": {
                "work": {"pid": 202, "created_at": 100.5}
            },
            "unmapped_pids": [],
            "unmapped": [],
            "cold_start_if_installed": False,
        },
    )

    with pytest.raises(RuntimeError, match="does not match its PID"):
        update_deferred_gateway._write_deferred_gateway_plan(
            root, transaction=transaction
        )


def test_deferred_gateway_plan_refuses_unmapped_or_foreign_bytes(
    tmp_path: Path, monkeypatch
):
    home = tmp_path / ".hermes"
    root = home / "hermes-agent"
    root.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    token = {
        "resume_needed": True,
        "profiles": {},
        "profile_identities": {},
        "unmapped_pids": [303],
        "unmapped": [{"pid": 303, "argv": ["python", "gateway", "run"]}],
        "cold_start_if_installed": False,
    }
    with pytest.raises(RuntimeError, match="unmapped"):
        update_deferred_gateway._write_deferred_gateway_plan(
            root, transaction=_deferred_transaction(token=token)
        )

    token["unmapped_pids"] = []
    token["unmapped"] = []
    transaction = _deferred_transaction(token=token)
    path = update_deferred_gateway._write_deferred_gateway_plan(
        root, transaction=transaction
    )
    foreign = json.loads(path.read_text(encoding="utf-8"))
    foreign["cold_start_if_installed"] = True
    assert update_deferred_gateway._sanitize_deferred_gateway_plan(
        foreign,
        root=root,
        invocation_id=transaction.invocation_id,
        lease_id=transaction.lease["lease_id"],
    ) is None


def test_deferred_fleet_partial_retry_does_not_duplicate_started_profile(monkeypatch):
    plan = {
        "profiles": [
            {"name": "default", "old_pid": 101, "created_at": 10.0},
            {"name": "work", "old_pid": 202, "created_at": 20.0},
        ],
        "cold_start_if_installed": False,
    }
    running: dict[str, int] = {}
    starts: list[str] = []
    fail_work = True
    monkeypatch.setattr(update_deferred_gateway, "_running_gateway_profiles", lambda: dict(running))
    monkeypatch.setattr(
        update_deferred_gateway,
        "_profile_process_still_matches",
        lambda _pid, _created: False,
    )

    def spawn(profile: str) -> int:
        nonlocal fail_work
        starts.append(profile)
        if profile == "work" and fail_work:
            fail_work = False
            raise RuntimeError("injected partial resume")
        running[profile] = 900 + len(starts)
        return running[profile]

    monkeypatch.setattr(update_deferred_gateway, "_spawn_deferred_gateway_profile", spawn)
    monkeypatch.setattr(
        update_deferred_gateway,
        "_wait_for_deferred_gateway_profile",
        lambda profile, **_kwargs: profile in running,
    )

    with pytest.raises(RuntimeError, match="partial"):
        update_deferred_gateway._resume_deferred_gateway_fleet(plan)
    update_deferred_gateway._resume_deferred_gateway_fleet(plan)

    assert starts == ["default", "work", "work"]


def test_deferred_fleet_pid_reuse_is_not_treated_as_old_live_gateway(monkeypatch):
    plan = {
        "profiles": [{"name": "work", "old_pid": 202, "created_at": 20.0}],
        "cold_start_if_installed": False,
    }
    running: dict[str, int] = {}
    monkeypatch.setattr(update_deferred_gateway, "_running_gateway_profiles", lambda: dict(running))
    monkeypatch.setattr(
        update_deferred_gateway,
        "_profile_process_still_matches",
        lambda _pid, _created: False,
    )
    monkeypatch.setattr(
        update_deferred_gateway,
        "_spawn_deferred_gateway_profile",
        lambda profile: running.setdefault(profile, 404),
    )
    monkeypatch.setattr(
        update_deferred_gateway,
        "_wait_for_deferred_gateway_profile",
        lambda profile, **_kwargs: profile in running,
    )

    update_deferred_gateway._resume_deferred_gateway_fleet(plan)
    assert running == {"work": 404}

    running.clear()
    monkeypatch.setattr(
        update_deferred_gateway,
        "_profile_process_still_matches",
        lambda _pid, _created: True,
    )
    with pytest.raises(RuntimeError, match="still running"):
        update_deferred_gateway._resume_deferred_gateway_fleet(plan)


def test_deferred_fleet_refuses_exact_old_process_even_if_profile_is_running(
    monkeypatch,
):
    plan = {
        "profiles": [{"name": "work", "old_pid": 202, "created_at": 20.0}],
        "cold_start_if_installed": False,
    }
    monkeypatch.setattr(
        update_deferred_gateway, "_running_gateway_profiles", lambda: {"work": 202}
    )
    monkeypatch.setattr(
        update_deferred_gateway,
        "_profile_process_still_matches",
        lambda _pid, _created: True,
    )

    with pytest.raises(RuntimeError, match="still running"):
        update_deferred_gateway._resume_deferred_gateway_fleet(plan)


def test_deferred_plan_consume_is_exact_and_completed_replay_is_idempotent(
    tmp_path: Path, monkeypatch
):
    home = tmp_path / ".hermes"
    root = home / "hermes-agent"
    root.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    transaction = _deferred_transaction(
        token={
            "resume_needed": True,
            "profiles": {},
            "profile_identities": {},
            "unmapped_pids": [],
            "unmapped": [],
            "cold_start_if_installed": False,
        },
    )
    pending = update_deferred_gateway._write_deferred_gateway_plan(
        root, transaction=transaction
    )
    raw = pending.read_text(encoding="utf-8")

    assert update_deferred_gateway._consume_deferred_gateway_plan(pending, raw) is True
    assert not pending.exists()
    completed = update_deferred_gateway._deferred_gateway_plan_path(
        root, transaction.invocation_id, completed=True
    )
    assert completed.read_text(encoding="utf-8") == raw
    assert update_deferred_gateway._load_deferred_gateway_plan(
        completed,
        root=root,
        invocation_id=transaction.invocation_id,
        lease_id=transaction.lease["lease_id"],
    ) is not None


def test_deferred_plan_consume_unlink_failure_rolls_back_completed_authority(
    tmp_path: Path, monkeypatch
):
    home = tmp_path / ".hermes"
    root = home / "hermes-agent"
    root.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    transaction = _deferred_transaction(
        token={
            "resume_needed": True,
            "profiles": {},
            "profile_identities": {},
            "unmapped_pids": [],
            "unmapped": [],
            "cold_start_if_installed": False,
        },
    )
    pending = update_deferred_gateway._write_deferred_gateway_plan(
        root, transaction=transaction
    )
    raw = pending.read_text(encoding="utf-8")
    completed = update_deferred_gateway._deferred_gateway_plan_path(
        root, transaction.invocation_id, completed=True
    )
    original_unlink = Path.unlink

    def fail_consume_tombstone_unlink(path: Path, *positional, **keywords):
        if ".consume-" in path.name:
            raise OSError("injected tombstone retention")
        return original_unlink(path, *positional, **keywords)

    monkeypatch.setattr(Path, "unlink", fail_consume_tombstone_unlink)

    assert update_deferred_gateway._consume_deferred_gateway_plan(pending, raw) is False
    assert pending.read_text(encoding="utf-8") == raw
    assert not completed.exists()


def test_deferred_plan_loader_recovers_authenticated_consume_tombstone(
    tmp_path: Path, monkeypatch
):
    home = tmp_path / ".hermes"
    root = home / "hermes-agent"
    root.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    transaction = _deferred_transaction(
        token={
            "resume_needed": False,
            "profiles": {},
            "profile_identities": {},
            "unmapped_pids": [],
            "unmapped": [],
            "cold_start_if_installed": False,
        },
    )
    pending = update_deferred_gateway._write_deferred_gateway_plan(
        root, transaction=transaction
    )
    raw = pending.read_text(encoding="utf-8")
    tombstone = pending.with_name(f"{pending.name}.consume-999-recovery")
    os.replace(pending, tombstone)

    loaded = update_deferred_gateway._load_deferred_gateway_plan(
        pending,
        root=root,
        invocation_id=transaction.invocation_id,
        lease_id=transaction.lease["lease_id"],
    )

    assert loaded is not None
    assert loaded[0] == raw
    assert pending.read_text(encoding="utf-8") == raw


@pytest.mark.windows_only
def test_structured_gateway_pause_publishes_explicit_empty_fleet(
    monkeypatch,
):
    import hermes_cli.gateway as gateway_module
    from hermes_cli import gateway_windows

    monkeypatch.setattr(gateway_module, "find_gateway_pids", lambda **_kwargs: [])
    monkeypatch.setattr(gateway_windows, "is_installed", lambda: False)
    published = []

    token = update_cmd._pause_windows_gateways_for_update(
        require_structured_resume=True,
        before_stop=published.append,
    )

    assert token == {
        "resume_needed": False,
        "profiles": {},
        "profile_identities": {},
        "unmapped_pids": [],
        "unmapped": [],
        "cold_start_if_installed": False,
    }
    assert published == [token]


@pytest.mark.windows_only
def test_structured_gateway_pause_propagates_empty_plan_publication_failure(
    monkeypatch,
):
    import hermes_cli.gateway as gateway_module
    from hermes_cli import gateway_windows

    monkeypatch.setattr(gateway_module, "find_gateway_pids", lambda **_kwargs: [])
    monkeypatch.setattr(gateway_windows, "is_installed", lambda: False)

    with pytest.raises(RuntimeError, match="injected plan publication"):
        update_cmd._pause_windows_gateways_for_update(
            require_structured_resume=True,
            before_stop=lambda _token: (_ for _ in ()).throw(
                RuntimeError("injected plan publication")
            ),
        )


def test_atomic_prepare_releases_initial_lease_when_post_drain_renewal_fails(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "install"
    root.mkdir()
    initial = {"lease_id": "lease-prepare-123456"}
    released = []
    monkeypatch.setattr(
        update_quiesce,
        "_claim_update_quiesce_lease",
        lambda *_args, **_kwargs: initial,
    )
    monkeypatch.setattr(
        update_quiesce,
        "_drain_under_update_lease",
        lambda *_args, **_kwargs: {"ok": True, "ready": True},
    )
    monkeypatch.setattr(
        gate,
        "write_quiesce_lease",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("injected renewal failure")
        ),
    )
    monkeypatch.setattr(
        update_quiesce,
        "_release_update_quiesce_lease",
        lambda _root, lease: released.append(lease) or True,
    )
    args = SimpleNamespace(yes=True, bridge_lease_id=None, timeout_seconds=1.0)
    transaction = update_transaction._UpdateTransaction()

    with pytest.raises(RuntimeError, match="injected renewal failure"):
        update_quiesce._prepare_atomic_windows_update(
            args, root=root, transaction=transaction
        )

    assert released == [initial]


@pytest.mark.windows_only
def test_deferred_update_does_not_kill_gateway_missing_from_frozen_plan(
    monkeypatch,
):
    from hermes_cli import main as cli_main

    frozen = {
        "resume_needed": True,
        "profiles": {"alpha": 101},
        "profile_identities": {
            "alpha": {"pid": 101, "created_at": 10.0}
        },
        "unmapped_pids": [],
        "unmapped": [],
        "cold_start_if_installed": False,
    }
    late = update_cmd._VerifiedProcessIdentity(
        pid=202, created_at=20.0, kind="pausable_gateway"
    )
    monkeypatch.setattr(cli_main, "_venv_scripts_dir", lambda: None)
    monkeypatch.setattr(cli_main, "_run_pre_update_backup", lambda _args: None)
    monkeypatch.setattr(
        cli_main, "_pause_windows_gateways_for_update", lambda **_kwargs: frozen
    )
    monkeypatch.setattr(
        cli_main,
        "_detect_venv_python_processes",
        lambda: [(202, "python.exe", "redacted")],
    )
    monkeypatch.setattr(
        cli_main, "_leftover_pausable_gateway_pids", lambda _holders: [late]
    )
    monkeypatch.setattr(
        cli_main, "_orphaned_desktop_backend_pids", lambda _holders: None
    )
    revalidated = []
    monkeypatch.setattr(
        update_cmd,
        "_revalidate_pausable_gateway_identity",
        lambda identity: revalidated.append(identity) or True,
    )
    args = SimpleNamespace(
        defer_gateway_resume=True,
        force=False,
        force_venv=False,
        yes=True,
    )

    with pytest.raises(SystemExit) as exit_info:
        cli_main._cmd_update_impl(
            args,
            gateway_mode=False,
            transaction=update_transaction._UpdateTransaction(),
        )

    assert exit_info.value.code == 2
    assert revalidated == []


def test_hidden_deferred_resume_emits_exact_first_adoption_frame(
    tmp_path: Path, monkeypatch, capsys
):
    from hermes_cli import update_lock

    root = tmp_path / "install"
    root.mkdir()
    invocation_id = "invocation-resume-123456"
    lease_id = "lease-resume-123456"
    args = SimpleNamespace(
        invocation_id=invocation_id,
        bridge_lease_id=lease_id,
        resume_root=str(root),
    )
    prior = {
        "schema_version": 1,
        "lease_id": lease_id,
        "owner_pid": 4321,
    }

    class FakeLock:
        def acquire(self):
            return True

        def prove_claim(self):
            return True

        def release(self):
            return None

    monkeypatch.setattr(update_lock, "UpdateLock", FakeLock)
    monkeypatch.setattr(gate, "read_quiesce_lease", lambda _path: prior)
    monkeypatch.setattr(
        update_deferred_gateway,
        "_claim_update_quiesce_lease",
        lambda *_args, **_kwargs: {**prior, "owner_pid": os.getpid()},
    )
    monkeypatch.setattr(
        update_deferred_gateway,
        "_load_update_receipt",
        lambda _root: {
            "invocation_id": invocation_id,
            "lease_id": lease_id,
            "gateway_resume_deferred": True,
        },
    )

    def load_plan(path: Path, **_kwargs):
        if path.suffix == ".completed":
            return None
        return "raw-plan", {"profiles": [], "cold_start_if_installed": False}

    monkeypatch.setattr(update_deferred_gateway, "_load_deferred_gateway_plan", load_plan)
    monkeypatch.setattr(update_deferred_gateway, "_resume_deferred_gateway_fleet", lambda _plan: None)
    monkeypatch.setattr(update_deferred_gateway, "_consume_deferred_gateway_plan", lambda *_a: True)
    monkeypatch.setattr(update_deferred_gateway, "_release_update_quiesce_lease", lambda *_a: True)

    with pytest.raises(SystemExit) as exit_info:
        update_cmd._cmd_update_resume_deferred_gateway(args, root=root)

    assert exit_info.value.code == 0
    lines = capsys.readouterr().out.splitlines()
    assert json.loads(lines[0]) == {
        "schema_version": 1,
        "event": "deferred-gateway-lease-adopted",
        "invocation_id": invocation_id,
        "owner_pid": os.getpid(),
    }
    assert sum(line.startswith("{") for line in lines) == 1


def test_completed_deferred_resume_without_lease_emits_no_adoption_frame(
    tmp_path: Path, monkeypatch, capsys
):
    root = tmp_path / "install"
    root.mkdir()
    args = SimpleNamespace(
        invocation_id="invocation-resume-123456",
        bridge_lease_id="lease-resume-123456",
        resume_root=str(root),
    )
    monkeypatch.setattr(
        update_deferred_gateway,
        "_load_deferred_gateway_plan",
        lambda *_args, **_kwargs: ("completed", {}),
    )
    monkeypatch.setattr(gate, "read_quiesce_lease", lambda _path: None)

    with pytest.raises(SystemExit) as exit_info:
        update_cmd._cmd_update_resume_deferred_gateway(args, root=root)

    assert exit_info.value.code == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == ["✓ Deferred gateway fleet was already resumed."]


def test_deferred_resume_cleanup_failure_returns_lease_and_releases_lock(
    tmp_path: Path, monkeypatch
):
    from hermes_cli import update_lock

    root = tmp_path / "install"
    root.mkdir()
    invocation_id = "invocation-cleanup-123456"
    lease_id = "lease-cleanup-123456"
    args = SimpleNamespace(
        invocation_id=invocation_id,
        bridge_lease_id=lease_id,
        resume_root=str(root),
    )
    prior = {"schema_version": 1, "lease_id": lease_id, "owner_pid": 4321}
    released_locks = []
    transfers = []

    class FakeLock:
        def acquire(self):
            return True

        def prove_claim(self):
            return True

        def release(self):
            released_locks.append(True)

    monkeypatch.setattr(update_lock, "UpdateLock", FakeLock)
    monkeypatch.setattr(gate, "read_quiesce_lease", lambda _path: prior)
    monkeypatch.setattr(
        update_deferred_gateway,
        "_claim_update_quiesce_lease",
        lambda *_args, **_kwargs: {**prior, "owner_pid": os.getpid()},
    )
    monkeypatch.setattr(
        update_deferred_gateway,
        "_load_deferred_gateway_plan",
        lambda *_args, **_kwargs: ("completed", {}),
    )
    monkeypatch.setattr(
        update_deferred_gateway,
        "_load_update_receipt",
        lambda _root: {
            "invocation_id": invocation_id,
            "lease_id": lease_id,
            "gateway_resume_deferred": True,
        },
    )
    monkeypatch.setattr(
        update_deferred_gateway, "_release_update_quiesce_lease", lambda *_args: False
    )
    monkeypatch.setattr(
        update_deferred_gateway,
        "_transfer_update_quiesce_lease",
        lambda _root, lease, *, new_owner_pid: transfers.append(
            (lease, new_owner_pid)
        )
        or lease,
    )

    with pytest.raises(SystemExit) as exit_info:
        update_cmd._cmd_update_resume_deferred_gateway(args, root=root)

    assert exit_info.value.code == 1
    assert transfers and transfers[0][1] == prior["owner_pid"]
    assert released_locks == [True]


def test_hidden_deferred_update_parser_requires_structured_capability() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    build_update_parser(subparsers, cmd_update=lambda _args: None)
    args = parser.parse_args(
        [
            "update",
            "--gateway",
            "--defer-gateway-resume",
            "--bridge-lease-id",
            "lease-parser-123456",
            "--invocation-id",
            "invocation-parser-123456",
        ]
    )

    update_cmd._validate_deferred_update_request(args)
    args.gateway = False
    with pytest.raises(ValueError, match="requires --gateway"):
        update_cmd._validate_deferred_update_request(args)


@pytest.mark.parametrize(
    "mode_flag",
    ["check", "preflight", "drain", "resume_deferred_gateway"],
)
def test_deferred_mode_conflicts_exit_before_lifecycle_dispatch(
    monkeypatch, mode_flag
):
    from hermes_cli import main as cli_main

    args = SimpleNamespace(
        defer_gateway_resume=True,
        gateway=True,
        bridge_lease_id="lease-parser-123456",
        invocation_id="invocation-parser-123456",
        check=False,
        preflight=False,
        drain=False,
        resume_deferred_gateway=False,
    )
    setattr(args, mode_flag, True)
    dispatched = []
    monkeypatch.setattr(
        cli_main,
        "_cmd_update_preflight",
        lambda *_args, **_kwargs: dispatched.append("preflight"),
    )
    monkeypatch.setattr(
        cli_main,
        "_cmd_update_drain",
        lambda *_args, **_kwargs: dispatched.append("drain"),
    )
    monkeypatch.setattr(
        cli_main,
        "_cmd_update_resume_deferred_gateway",
        lambda *_args, **_kwargs: dispatched.append("resume"),
    )

    with pytest.raises(SystemExit) as exit_info:
        cli_main.cmd_update(args)

    assert exit_info.value.code == 2
    assert dispatched == []


def test_malformed_invocation_id_exits_before_any_bridge_stop(monkeypatch):
    from hermes_cli import main as cli_main

    args = SimpleNamespace(
        defer_gateway_resume=True,
        gateway=True,
        bridge_lease_id="lease-parser-123456",
        invocation_id="../not-an-invocation",
        check=False,
        preflight=False,
        drain=False,
        resume_deferred_gateway=False,
    )
    lifecycle_calls = []
    monkeypatch.setattr(
        cli_main,
        "_prepare_atomic_windows_update",
        lambda *_args, **_kwargs: lifecycle_calls.append("drain"),
    )
    monkeypatch.setattr(
        cli_main,
        "_pause_windows_gateways_for_update",
        lambda *_args, **_kwargs: lifecycle_calls.append("pause"),
    )

    with pytest.raises(SystemExit) as exit_info:
        cli_main.cmd_update(args)

    assert exit_info.value.code == 2
    assert lifecycle_calls == []


@pytest.mark.parametrize(
    ("body_fails", "cleanup_outcome", "expected_error"),
    [
        pytest.param(False, "released", None, id="success"),
        pytest.param(True, "released", "body", id="body-failure"),
        pytest.param(False, "unproven", "cleanup", id="cleanup-unproven"),
        pytest.param(False, "raises", "cleanup-exception", id="cleanup-exception"),
    ],
)
@pytest.mark.windows_only
def test_windows_cmd_update_orders_all_transaction_cleanup(
    monkeypatch,
    body_fails: bool,
    cleanup_outcome: str,
    expected_error: str | None,
) -> None:
    """Pin wrapper cleanup order and make lease-cleanup failure observable."""
    from hermes_cli import config as config_module
    from hermes_cli import main as cli_main
    from hermes_cli import update_lock

    events: list[str] = []
    lease = _lease(Path(cli_main.PROJECT_ROOT))
    resume_plan = {"resume_needed": True, "profiles": {}}

    class BodyFailure(RuntimeError):
        pass

    class FakeLock:
        holder = None

        def acquire(self):
            events.append("lock-acquire")
            return True

        def prove_claim(self):
            events.append("lock-prove")
            return True

        def release(self):
            events.append("lock-release")

    class FakeJob:
        def __init__(self):
            events.append("job-init")

        def abort(self, _reason=""):
            events.append("job-abort")

        def disarm(self):
            events.append("job-disarm")

    class FakeHeartbeat:
        lost = False
        loss_reason = None

        def __init__(self, _root, _lease, *, fail_stop):
            assert callable(fail_stop)
            events.append("heartbeat-init")

        def start(self):
            events.append("heartbeat-start")

        def stop(self):
            events.append("heartbeat-stop")

    def body(args, *, gateway_mode, transaction):
        assert gateway_mode is False
        events.append("body")
        transaction.gateway_resume_plan = resume_plan
        if body_fails:
            raise BodyFailure("injected update body failure")

    def prepare(_args, *, root, transaction):
        assert root == cli_main.PROJECT_ROOT
        events.append("prepare")
        transaction.lease = lease
        transaction.invocation_id = "invocation-order-123456"

    monkeypatch.setattr(config_module, "is_managed", lambda: False)
    monkeypatch.setattr(
        config_module, "detect_install_method", lambda _root: "git"
    )
    monkeypatch.setattr(update_lock, "UpdateLock", FakeLock)
    monkeypatch.setattr(
        cli_main,
        "_install_hangup_protection",
        lambda *, gateway_mode: events.append("output-install") or {"installed": False},
    )
    monkeypatch.setattr(
        cli_main,
        "_finalize_update_output",
        lambda _state: events.append("output-finalize"),
    )
    monkeypatch.setattr(
        cli_main,
        "_prepare_atomic_windows_update",
        prepare,
    )
    monkeypatch.setattr(cli_main, "_WindowsMutationJob", FakeJob)
    monkeypatch.setattr(cli_main, "_UpdateLeaseHeartbeat", FakeHeartbeat)
    monkeypatch.setattr(cli_main, "_cmd_update_impl", body)
    monkeypatch.setattr(
        cli_main,
        "_resume_windows_gateways_after_update",
        lambda plan: events.append("gateway-resume")
        if plan is resume_plan
        else pytest.fail("wrong gateway resume plan"),
    )
    def release_lease(root, value):
        events.append("lease-release")
        assert root == cli_main.PROJECT_ROOT
        assert value is lease
        if cleanup_outcome == "raises":
            raise PermissionError("injected lease cleanup failure")
        return cleanup_outcome == "released"

    monkeypatch.setattr(cli_main, "_release_update_quiesce_lease", release_lease)
    args = SimpleNamespace(
        preflight=False,
        drain=False,
        resume_deferred_gateway=False,
        check=False,
        gateway=False,
        defer_gateway_resume=False,
    )

    if expected_error == "body":
        with pytest.raises(BodyFailure, match="injected update body failure"):
            cli_main.cmd_update(args)
    elif expected_error == "cleanup":
        with pytest.raises(
            RuntimeError, match="bridge lease cleanup could not be proven"
        ):
            cli_main.cmd_update(args)
    elif expected_error == "cleanup-exception":
        with pytest.raises(PermissionError, match="injected lease cleanup failure"):
            cli_main.cmd_update(args)
    else:
        cli_main.cmd_update(args)

    assert events == [
        "output-install",
        "lock-acquire",
        "lock-prove",
        "prepare",
        "job-init",
        "heartbeat-init",
        "heartbeat-start",
        "body",
        "heartbeat-stop",
        "job-disarm",
        "gateway-resume",
        "lease-release",
        "lock-release",
        "output-finalize",
    ]
    assert not {
        "_update_invocation_id",
        "_update_quiesce_lease",
        "_update_handoff_owner_pid",
        "_windows_gateway_resume_plan",
        "_deferred_gateway_plan_written",
    }.intersection(vars(args))
