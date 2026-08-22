import logging
import json
import os
from types import SimpleNamespace

import pytest

from hermes_cli import update_diagnostics


def _hostile_error() -> OSError:
    private = (
        r"C:\Users\royal\AppData\Local\hermes\.hermes-runtime\venv-candidate-nonce"
        " invocation-private-123456 lease-private-123456 "
        "https://user:password@example.invalid/private.git\n"
        "\x1b[31mPRIVATE-SENTINEL\x00"
    )
    return OSError(5, private + ("X" * (1024 * 1024)))


def test_failure_record_is_one_fixed_bounded_ascii_line_without_private_input(
    caplog,
):
    error = _hostile_error()

    with caplog.at_level(logging.ERROR, logger="hermes.update-test"):
        line = update_diagnostics.log_update_failure(
            logging.getLogger("hermes.update-test"),
            code="HDU201",
            stage="candidate-staging",
            error=error,
        )

    assert line == (
        "schema=1 event=desktop-update-failure code=HDU201 "
        "stage=candidate-staging kind=io os_code=5"
    )
    encoded = line.encode("ascii")
    assert len(encoded) <= 192
    assert b"\n" not in encoded and b"\r" not in encoded
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.getMessage() == line
    assert record.exc_info is None
    assert record.exc_text is None
    combined = line + caplog.text
    for forbidden in (
        "PRIVATE-SENTINEL",
        "venv-candidate-nonce",
        "invocation-private",
        "lease-private",
        "password",
        "example.invalid",
        "royal",
        "OSError",
        "Traceback",
    ):
        assert forbidden not in combined


def test_receipt_replaces_hostile_stop_reason_with_fixed_code(tmp_path, monkeypatch):
    from hermes_cli import update_receipt

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: home)
    update_receipt._current = None
    update_receipt.begin_update_receipt()
    hostile = str(_hostile_error())

    path = update_receipt.finalize_pending_update_receipt(1, hostile)

    assert path is not None
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["stop_reason"] == "HDU999:command-boundary"
    serialized = json.dumps(payload, sort_keys=True)
    assert "PRIVATE-SENTINEL" not in serialized
    assert "password" not in serialized
    update_receipt._current = None


def test_heartbeat_probe_logs_one_fixed_record_and_passes_only_fixed_reason(
    tmp_path, monkeypatch, caplog
):
    import hermes_mcp_update_gate as gate
    from hermes_cli import update_cmd

    root = tmp_path / "install"
    root.mkdir()
    lease = {
        "schema_version": 1,
        "lease_id": "lease-heartbeat-redaction-123456",
        "owner_pid": os.getpid(),
        "created_at": 100,
        "expires_at": 200,
        "handoff_grace_until": 100,
        "install_root": str(root.resolve()),
    }
    monkeypatch.setattr(gate, "read_quiesce_lease", lambda *_: (_ for _ in ()).throw(_hostile_error()))
    monkeypatch.setattr(gate, "write_emergency_quiesce_shadow", lambda *_args, **_kwargs: None)
    reasons = []
    heartbeat = update_cmd._UpdateLeaseHeartbeat(
        root,
        lease,
        fail_stop=reasons.append,
    )

    with caplog.at_level(logging.ERROR, logger="hermes_cli.update_cmd"):
        assert heartbeat._renew_once() is False

    records = [
        record
        for record in caplog.records
        if "desktop-update-failure" in record.getMessage()
    ]
    assert len(records) == 1
    assert records[0].getMessage() == (
        "schema=1 event=desktop-update-failure code=HDU401 "
        "stage=heartbeat-probe kind=io os_code=5"
    )
    assert records[0].exc_info is None
    assert records[0].exc_text is None
    assert reasons == ["HDU401:heartbeat-probe"]
    assert "PRIVATE-SENTINEL" not in caplog.text


def test_provider_proof_converts_hostile_failure_without_cause_or_log(
    monkeypatch, caplog
):
    from hermes_cli import config, update_cmd

    monkeypatch.setattr(config, "load_config", lambda: (_ for _ in ()).throw(_hostile_error()))

    with pytest.raises(update_diagnostics.UpdateDiagnosticError) as failure:
        update_cmd._active_memory_provider_specs()

    assert failure.value.code == "HDU101"
    assert failure.value.stage == "provider-proof"
    assert failure.value.__cause__ is None
    assert "PRIVATE-SENTINEL" not in str(failure.value)
    assert caplog.records == []


@pytest.mark.parametrize(
    ("preclassified", "expected_code", "expected_stage"),
    [
        (False, "HDU999", "command-boundary"),
        (True, "HDU201", "candidate-staging"),
    ],
)
def test_update_command_boundary_exits_without_private_traceback_and_receipt_leak(
    tmp_path,
    monkeypatch,
    caplog,
    capsys,
    preclassified,
    expected_code,
    expected_stage,
):
    from hermes_cli import config, main, update_lock, update_receipt

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: home)
    monkeypatch.setattr(config, "is_managed", lambda: False)
    monkeypatch.setattr(config, "detect_install_method", lambda _root: "git")
    monkeypatch.setattr(main, "_validate_deferred_update_request", lambda _args: None)
    monkeypatch.setattr(main, "_is_windows", lambda: False)
    monkeypatch.setattr(
        main,
        "_install_hangup_protection",
        lambda *, gateway_mode: {"installed": False},
    )
    monkeypatch.setattr(main, "_finalize_update_output", lambda _state: None)

    class FakeLock:
        holder = None

        def acquire(self):
            return True

        def prove_claim(self):
            return True

        def release(self):
            pass

    monkeypatch.setattr(update_lock, "UpdateLock", FakeLock)

    def fail_update(*_args, **_kwargs):
        update_receipt.begin_update_receipt()
        error = _hostile_error()
        if preclassified:
            raise update_diagnostics.diagnostic_error(
                error,
                code="HDU201",
                stage="candidate-staging",
            ) from None
        raise error

    monkeypatch.setattr(main, "_cmd_update_impl", fail_update)
    args = SimpleNamespace(
        preflight=False,
        drain=False,
        resume_deferred_gateway=False,
        plan=False,
        check=False,
        gateway=False,
        defer_gateway_resume=False,
    )

    with caplog.at_level(logging.ERROR, logger="hermes_cli.main"):
        with pytest.raises(SystemExit) as exit_info:
            main.cmd_update(args)

    assert exit_info.value.code == 1
    output = capsys.readouterr()
    combined = output.out + output.err + caplog.text
    assert "PRIVATE-SENTINEL" not in combined
    assert "password" not in combined
    assert "Traceback" not in combined
    records = [
        record
        for record in caplog.records
        if "desktop-update-failure" in record.getMessage()
    ]
    assert len(records) == 1
    assert records[0].getMessage() == (
        "schema=1 event=desktop-update-failure "
        f"code={expected_code} stage={expected_stage} kind=io os_code=5"
    )
    assert records[0].exc_info is None and records[0].exc_text is None
    receipt = update_receipt.read_latest_receipt()
    assert receipt is not None
    assert receipt["stop_reason"] == f"{expected_code}:{expected_stage}"
