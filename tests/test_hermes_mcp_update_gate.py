from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import hermes_mcp_update_gate as gate
import pytest


def _lease(root: Path, *, now: float = 100.0, owner_pid: int = 42) -> dict:
    created_at = int(now)
    return {
        "schema_version": 1,
        "lease_id": "lease-test-123456",
        "owner_pid": owner_pid,
        "created_at": created_at,
        "expires_at": created_at + 120,
        "handoff_grace_until": created_at + 60,
        "install_root": str(root.resolve()),
    }


def test_exact_mcp_launch_has_no_module_arguments() -> None:
    module = "agent.transports.hermes_tools_mcp_server"

    assert gate.is_exact_mcp_module_argv(["python.exe", "-X", "utf8", "-m", module])
    assert not gate.is_exact_mcp_module_argv(["python.exe", "-m", module, "--verbose"])
    assert not gate.is_exact_mcp_module_argv(["python.exe", "-m", f"{module}.extra"])
    assert not gate.is_exact_mcp_module_argv(["python.exe", "-m", "hermes_cli.main", "serve"])
    assert not gate.is_exact_mcp_module_argv(["python.exe", "script.py", "-m", module])
    assert not gate.is_exact_mcp_module_argv(["python.exe", "--", "-m", module])
    assert not gate.is_exact_mcp_module_argv(["python.exe", "-c", "pass", "-m", module])


def test_gate_accepts_live_owner_for_the_canonical_target_root(tmp_path: Path) -> None:
    root = tmp_path / "install"
    root.mkdir()
    marker = tmp_path / ".hermes-venv-quiesce"
    marker.write_text(json.dumps(_lease(root)), encoding="utf-8")

    active = gate.live_quiesce_lease(
        marker,
        install_root=root,
        now=110.0,
        pid_alive=lambda pid: pid == 42,
    )

    assert active is not None
    assert active["lease_id"] == "lease-test-123456"


@pytest.mark.parametrize("now", [50.0, 300.0])
def test_exact_live_owner_survives_wall_clock_steps(
    tmp_path: Path, now: float
) -> None:
    root = tmp_path / "install"
    root.mkdir()
    marker = tmp_path / ".hermes-venv-quiesce"
    marker.write_text(json.dumps(_lease(root)), encoding="utf-8")

    active = gate.live_quiesce_lease(
        marker,
        install_root=root,
        now=now,
        pid_alive=lambda pid: pid == 42,
        pid_create_time=lambda pid: 99.0 if pid == 42 else None,
    )

    assert active is not None
    assert active["lease_id"] == "lease-test-123456"


@pytest.mark.parametrize(
    ("now", "pid_alive", "pid_create_time"),
    [
        (300.0, lambda _pid: False, lambda _pid: None),
        (50.0, lambda _pid: True, lambda _pid: 101.0),
    ],
    ids=["dead-owner-after-forward-step", "reused-owner-after-backward-step"],
)
def test_wall_clock_steps_do_not_extend_dead_or_reused_owner_lease(
    tmp_path: Path,
    now: float,
    pid_alive,
    pid_create_time,
) -> None:
    root = tmp_path / "install"
    root.mkdir()
    marker = tmp_path / ".hermes-venv-quiesce"
    marker.write_text(json.dumps(_lease(root)), encoding="utf-8")

    assert gate.live_quiesce_lease(
        marker,
        install_root=root,
        now=now,
        pid_alive=pid_alive,
        pid_create_time=pid_create_time,
    ) is None


def test_expired_emergency_shadow_still_has_a_hard_wall_clock_bound(
    tmp_path: Path,
) -> None:
    root = tmp_path / "install"
    root.mkdir()
    marker = tmp_path / ".hermes-venv-quiesce"
    shadow = tmp_path / f"{marker.name}.cas-emergency-test"
    shadow.write_text(json.dumps(_lease(root)), encoding="utf-8")

    assert gate.live_quiesce_lease(
        marker,
        install_root=root,
        now=300.0,
        pid_alive=lambda pid: pid == 42,
        pid_create_time=lambda pid: 99.0 if pid == 42 else None,
    ) is None


def test_emergency_shadow_survives_backward_wall_clock_step(tmp_path: Path) -> None:
    root = tmp_path / "install"
    root.mkdir()
    marker = tmp_path / ".hermes-venv-quiesce"
    shadow = tmp_path / f"{marker.name}.cas-emergency-test"
    shadow.write_text(json.dumps(_lease(root)), encoding="utf-8")

    active = gate.live_quiesce_lease(
        marker,
        install_root=root,
        now=50.0,
        pid_alive=lambda _pid: False,
    )

    assert active is not None
    assert active["lease_id"] == "lease-test-123456"


def test_gate_rejects_a_valid_lease_for_another_install(tmp_path: Path) -> None:
    root = tmp_path / "install"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()
    marker = tmp_path / ".hermes-venv-quiesce"
    marker.write_text(json.dumps(_lease(other)), encoding="utf-8")

    assert gate.live_quiesce_lease(
        marker,
        install_root=root,
        now=110.0,
        pid_alive=lambda _pid: True,
    ) is None


def test_dead_owner_is_accepted_only_during_bounded_handoff_grace(tmp_path: Path) -> None:
    root = tmp_path / "install"
    root.mkdir()
    marker = tmp_path / ".hermes-venv-quiesce"
    marker.write_text(json.dumps(_lease(root)), encoding="utf-8")

    assert gate.live_quiesce_lease(
        marker,
        install_root=root,
        now=159.0,
        pid_alive=lambda _pid: False,
    ) is not None
    assert gate.live_quiesce_lease(
        marker,
        install_root=root,
        now=161.0,
        pid_alive=lambda _pid: False,
    ) is None


def test_lease_expiry_and_max_age_are_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "install"
    root.mkdir()
    marker = tmp_path / ".hermes-venv-quiesce"
    payload = _lease(root)
    payload["expires_at"] = payload["created_at"] + gate.MAX_LEASE_SECONDS + 1
    marker.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="malformed and not stale"):
        gate.live_quiesce_lease(
            marker,
            install_root=root,
            now=110.0,
            pid_alive=lambda _pid: True,
        )


def test_nonfinite_lease_times_and_invalid_ids_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "install"
    root.mkdir()
    marker = tmp_path / ".hermes-venv-quiesce"
    payload = _lease(root)
    payload["lease_id"] = "short"
    marker.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="malformed and not stale"):
        gate.live_quiesce_lease(
            marker, install_root=root, now=110.0, pid_alive=lambda _pid: True
        )

    payload["lease_id"] = "valid-lease-id-1234"
    marker.write_text(json.dumps(payload), encoding="utf-8")
    assert gate.live_quiesce_lease(
        marker, install_root=root, now=math.nan, pid_alive=lambda _pid: True
    ) is None
    for field, value in (("now", math.inf), ("lifetime_seconds", math.nan)):
        kwargs = {field: value}
        with pytest.raises(ValueError):
            gate.write_quiesce_lease(root, marker=marker, **kwargs)


def test_legacy_marker_is_accepted_only_for_default_install_root(tmp_path: Path) -> None:
    home = tmp_path / ".hermes"
    root = home / "hermes-agent"
    root.mkdir(parents=True)
    marker = home / ".hermes-venv-quiesce"
    marker.write_text("42\n100\n", encoding="utf-8")

    assert gate.live_quiesce_lease(
        marker,
        install_root=root,
        now=110.0,
        pid_alive=lambda pid: pid == 42,
    ) is not None
    assert gate.live_quiesce_lease(
        marker,
        install_root=tmp_path / "other",
        now=110.0,
        pid_alive=lambda _pid: True,
    ) is None


@pytest.mark.parametrize("now", [50.0, 1400.0])
def test_legacy_exact_live_owner_survives_wall_clock_steps(
    tmp_path: Path, now: float
) -> None:
    home = tmp_path / ".hermes"
    root = home / "hermes-agent"
    root.mkdir(parents=True)
    marker = home / ".hermes-venv-quiesce"
    marker.write_text("42\n100\n", encoding="utf-8")

    assert gate.live_quiesce_lease(
        marker,
        install_root=root,
        now=now,
        pid_alive=lambda pid: pid == 42,
        pid_create_time=lambda pid: 99.0 if pid == 42 else None,
    ) is not None


def test_clear_requires_matching_lease_and_current_owner(tmp_path: Path) -> None:
    root = tmp_path / "install"
    root.mkdir()
    marker = tmp_path / ".hermes-venv-quiesce"
    marker.write_text(json.dumps(_lease(root)), encoding="utf-8")

    assert not gate.clear_quiesce_lease(
        "wrong", owner_pid=42, marker=marker, install_root=root
    )
    assert not gate.clear_quiesce_lease(
        "lease-test-123456", owner_pid=41, marker=marker, install_root=root
    )
    assert marker.exists()
    assert gate.clear_quiesce_lease(
        "lease-test-123456", owner_pid=42, marker=marker, install_root=root
    )
    assert not marker.exists()


def test_should_quiesce_combines_exact_argv_with_live_lease(tmp_path: Path) -> None:
    root = tmp_path / "install"
    root.mkdir()
    marker = tmp_path / ".hermes-venv-quiesce"
    marker.write_text(json.dumps(_lease(root)), encoding="utf-8")
    module = "agent.transports.hermes_tools_mcp_server"

    assert gate.should_quiesce_mcp_bridge(
        argv=["python.exe", "-m", module],
        marker=marker,
        install_root=root,
        now=110.0,
        pid_alive=lambda _pid: True,
    )
    assert not gate.should_quiesce_mcp_bridge(
        argv=["python.exe", "-m", module, "--extra"],
        marker=marker,
        install_root=root,
        now=110.0,
        pid_alive=lambda _pid: True,
    )


def test_real_module_launch_quiesces_before_jiter_preload(tmp_path: Path) -> None:
    """Prove the parent-package gate runs before the native preload boundary."""
    root = Path(__file__).resolve().parents[1]
    hermes_home = tmp_path / "hermes-home"
    marker = hermes_home / gate.MARKER_NAME
    sentinel = tmp_path / "jiter-imported"
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "jiter.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "Path(os.environ['HERMES_JITER_SENTINEL']).write_text('imported')\n",
        encoding="utf-8",
    )
    gate.write_quiesce_lease(
        root,
        marker=marker,
        owner_pid=os.getpid(),
        lifetime_seconds=60,
        handoff_grace_seconds=30,
    )
    env = os.environ.copy()
    env.update(
        {
            "HERMES_HOME": str(hermes_home),
            "HERMES_JITER_SENTINEL": str(sentinel),
            "PYTHONPATH": str(shadow),
        }
    )

    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "agent.transports.hermes_tools_mcp_server",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not sentinel.exists()


def test_profile_homes_share_one_install_global_marker(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "hermes-home"
    first = root / "profiles" / "work"
    second = root / "profiles" / "personal"
    monkeypatch.setenv("HERMES_HOME", str(first))

    assert gate.marker_path() == root / gate.MARKER_NAME
    assert gate.marker_path(first) == root / gate.MARKER_NAME
    assert gate.marker_path(second) == root / gate.MARKER_NAME


def test_default_marker_path_uses_canonical_global_home_helper(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "custom-hermes-root"
    monkeypatch.setattr(gate, "get_default_hermes_root", lambda _home=None: root)

    assert gate.marker_path() == root / gate.MARKER_NAME


def test_explicit_marker_home_does_not_follow_process_environment(
    tmp_path: Path, monkeypatch
) -> None:
    process_root = tmp_path / "process"
    explicit_root = tmp_path / "explicit"
    monkeypatch.setenv("HERMES_HOME", str(process_root / "profiles" / "work"))

    assert gate.marker_path(explicit_root) == explicit_root / gate.MARKER_NAME


def test_exclusive_new_claim_never_overwrites_live_or_malformed_marker(
    tmp_path: Path,
) -> None:
    root = tmp_path / "install"
    root.mkdir()
    marker = tmp_path / gate.MARKER_NAME
    live = gate.write_quiesce_lease(root, marker=marker, owner_pid=os.getpid())

    with pytest.raises(RuntimeError, match="another updater"):
        gate.write_quiesce_lease(root, marker=marker, owner_pid=os.getpid())
    assert json.loads(marker.read_text(encoding="utf-8"))["lease_id"] == live["lease_id"]

    marker.write_text("malformed-but-possibly-foreign", encoding="utf-8")
    with pytest.raises(RuntimeError, match="malformed"):
        gate.write_quiesce_lease(root, marker=marker, owner_pid=os.getpid())
    assert marker.read_text(encoding="utf-8") == "malformed-but-possibly-foreign"


def test_malformed_marker_has_bounded_stale_recovery(tmp_path: Path) -> None:
    root = tmp_path / "install"
    root.mkdir()
    marker = tmp_path / gate.MARKER_NAME
    marker.write_text("old-malformed", encoding="utf-8")
    old = time.time() - gate.MAX_LEASE_SECONDS - 10
    os.utime(marker, (old, old))

    lease = gate.write_quiesce_lease(root, marker=marker, owner_pid=os.getpid())

    assert json.loads(marker.read_text(encoding="utf-8"))["lease_id"] == lease["lease_id"]


def test_renewal_cas_preserves_foreign_replacement(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "install"
    root.mkdir()
    marker = tmp_path / gate.MARKER_NAME
    lease = gate.write_quiesce_lease(root, marker=marker, owner_pid=os.getpid())
    foreign = {**lease, "lease_id": "foreign-lease-123456", "owner_pid": os.getpid() + 1}
    original_move = gate._move_lease_if_unchanged

    def replace_before_compare(path, expected_raw):
        path.write_text(json.dumps(foreign, sort_keys=True), encoding="utf-8")
        return original_move(path, expected_raw)

    monkeypatch.setattr(gate, "_move_lease_if_unchanged", replace_before_compare)
    with pytest.raises(RuntimeError, match="compare-and-swap"):
        gate.write_quiesce_lease(
            root,
            marker=marker,
            lease_id=lease["lease_id"],
            owner_pid=os.getpid(),
        )
    assert json.loads(marker.read_text(encoding="utf-8")) == foreign


def test_renewal_shadow_keeps_gate_visible_while_primary_is_moved(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "install"
    root.mkdir()
    marker = tmp_path / gate.MARKER_NAME
    lease = gate.write_quiesce_lease(root, marker=marker, owner_pid=os.getpid())
    original_move = gate._move_lease_if_unchanged
    observed = []

    def move_and_observe(path, expected_raw):
        tombstone = original_move(path, expected_raw)
        assert tombstone is not None and not path.exists()
        observed.append(
            gate.live_quiesce_lease(
                path,
                install_root=root,
                pid_alive=lambda pid: pid == os.getpid(),
            )
        )
        return tombstone

    monkeypatch.setattr(gate, "_move_lease_if_unchanged", move_and_observe)
    renewed = gate.write_quiesce_lease(
        root,
        marker=marker,
        lease_id=lease["lease_id"],
        owner_pid=os.getpid(),
    )

    assert observed and observed[0] is not None
    assert observed[0]["lease_id"] == lease["lease_id"]
    assert json.loads(marker.read_text(encoding="utf-8"))["lease_id"] == renewed["lease_id"]
    assert not list(tmp_path.glob(f"{gate.MARKER_NAME}.cas-*"))


def test_renewal_temp_write_never_exposes_partial_recovery_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "install"
    root.mkdir()
    marker = tmp_path / gate.MARKER_NAME
    lease = gate.write_quiesce_lease(root, marker=marker, owner_pid=os.getpid())
    wrote_temp = threading.Event()
    release = threading.Event()
    original = gate._write_unpublished_exclusive

    def pause_after_temp(path: Path, raw: str, **kwargs) -> None:
        original(path, raw, **kwargs)
        if path.name.startswith(".hermes-lease-pending-") and not wrote_temp.is_set():
            wrote_temp.set()
            assert release.wait(timeout=5)

    monkeypatch.setattr(gate, "_write_unpublished_exclusive", pause_after_temp)
    result: list[object] = []

    def renew() -> None:
        try:
            result.append(
                gate.write_quiesce_lease(
                    root,
                    marker=marker,
                    lease_id=lease["lease_id"],
                    owner_pid=os.getpid(),
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            result.append(exc)

    thread = threading.Thread(target=renew)
    thread.start()
    assert wrote_temp.wait(timeout=5)
    try:
        assert not list(tmp_path.glob(f"{gate.MARKER_NAME}.cas-*"))
        assert gate.live_quiesce_lease(
            marker,
            install_root=root,
            pid_alive=lambda pid: pid == os.getpid(),
        ) is not None
    finally:
        release.set()
        thread.join(timeout=5)

    assert len(result) == 1 and isinstance(result[0], dict)


def test_custom_atomic_publication_cleans_failed_temporary(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "gateway-plan.json"
    observed: list[tuple[Path, str]] = []

    def fail_after_create(
        path: Path,
        _raw: str,
        *,
        short_write_message: str,
    ) -> None:
        path.touch()
        observed.append((path, short_write_message))
        raise OSError(short_write_message)

    monkeypatch.setattr(gate, "_write_unpublished_exclusive", fail_after_create)

    with pytest.raises(OSError, match="short write while publishing gateway resume plan"):
        gate._publish_exclusive_atomic(
            target,
            "payload",
            temporary_prefix=".hermes-gateway-plan-",
            short_write_message="short write while publishing gateway resume plan",
        )

    assert len(observed) == 1
    assert observed[0][0].name.startswith(".hermes-gateway-plan-")
    assert not target.exists()
    assert not list(tmp_path.glob(".hermes-gateway-plan-*"))


def test_clear_cas_preserves_foreign_replacement(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "install"
    root.mkdir()
    marker = tmp_path / gate.MARKER_NAME
    lease = gate.write_quiesce_lease(root, marker=marker, owner_pid=os.getpid())
    foreign = {**lease, "lease_id": "foreign-lease-123456", "owner_pid": os.getpid() + 1}
    original_move = gate._move_lease_if_unchanged

    def replace_before_compare(path, expected_raw):
        path.write_text(json.dumps(foreign, sort_keys=True), encoding="utf-8")
        return original_move(path, expected_raw)

    monkeypatch.setattr(gate, "_move_lease_if_unchanged", replace_before_compare)
    assert not gate.clear_quiesce_lease(
        lease["lease_id"], owner_pid=os.getpid(), marker=marker, install_root=root
    )
    assert json.loads(marker.read_text(encoding="utf-8")) == foreign


def test_concurrent_new_claim_has_one_winner(tmp_path: Path) -> None:
    root = tmp_path / "install"
    root.mkdir()
    marker = tmp_path / gate.MARKER_NAME
    barrier = threading.Barrier(2)
    results: list[str] = []

    def claim() -> None:
        barrier.wait()
        try:
            gate.write_quiesce_lease(root, marker=marker, owner_pid=os.getpid())
        except RuntimeError:
            results.append("refused")
        else:
            results.append("claimed")

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert sorted(results) == ["claimed", "refused"]


def test_positive_pid_probe_api_failure_is_fail_closed(monkeypatch) -> None:
    monkeypatch.setattr(
        gate.ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("api unavailable")),
    )

    assert gate._pid_alive(12345)


def test_windows_pid_probe_treats_only_invalid_parameter_as_definitive_exit() -> None:
    assert gate._windows_open_process_error_is_definitive_exit(87)
    assert not gate._windows_open_process_error_is_definitive_exit(5)
    assert not gate._windows_open_process_error_is_definitive_exit(123)


@pytest.mark.windows_only
def test_native_windows_pid_probe_live_dead_and_active_zero_grace_lease(
    tmp_path: Path,
) -> None:
    assert gate._pid_alive(os.getpid())

    exited = subprocess.Popen([sys.executable, "-c", "pass"])
    exited.wait(timeout=10)
    assert not gate._pid_alive(exited.pid)

    root = tmp_path / "install"
    root.mkdir()
    marker = tmp_path / gate.MARKER_NAME
    now = int(time.time())
    lease = gate.write_quiesce_lease(
        root,
        marker=marker,
        owner_pid=os.getpid(),
        now=now,
        handoff_grace_seconds=0,
    )
    assert lease["handoff_grace_until"] == lease["created_at"]
    assert gate.live_quiesce_lease(
        marker,
        install_root=root,
        now=now + 1,
    ) is not None


def test_reused_owner_pid_does_not_inherit_old_lease(tmp_path: Path) -> None:
    root = tmp_path / "install"
    root.mkdir()
    marker = tmp_path / gate.MARKER_NAME
    payload = _lease(root, now=100.0)
    payload["handoff_grace_until"] = 100
    marker.write_text(json.dumps(payload), encoding="utf-8")

    assert gate.live_quiesce_lease(
        marker,
        install_root=root,
        now=110.0,
        pid_alive=lambda _pid: True,
        pid_create_time=lambda _pid: 101.0,
    ) is None


@pytest.mark.parametrize("payload", [{}, {"schema_version": 2}])
def test_fresh_structurally_invalid_json_primary_is_fail_closed(
    tmp_path: Path, payload: dict
) -> None:
    root = tmp_path / "install"
    root.mkdir()
    marker = tmp_path / gate.MARKER_NAME
    marker.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="malformed and not stale"):
        gate.live_quiesce_lease(marker, install_root=root)


def test_unknown_v1_fields_never_authorize_read_renew_adopt_or_clear(
    tmp_path: Path,
) -> None:
    root = tmp_path / "install"
    root.mkdir()
    marker = tmp_path / gate.MARKER_NAME
    payload = _lease(root, now=100.0, owner_pid=os.getpid())
    payload["future_semantics"] = "not-understood"
    marker.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="unknown or invalid schema"):
        gate.read_quiesce_lease(marker)
    with pytest.raises(RuntimeError, match="malformed and not stale"):
        gate.live_quiesce_lease(marker, install_root=root, now=100.0)
    with pytest.raises(RuntimeError, match="identity changed"):
        gate.write_quiesce_lease(
            root,
            marker=marker,
            lease_id=payload["lease_id"],
            owner_pid=os.getpid(),
            now=100.0,
        )
    with pytest.raises(RuntimeError, match="malformed and not stale"):
        gate.adopt_quiesce_lease(
            root,
            marker=marker,
            lease_id=payload["lease_id"],
            owner_pid=os.getpid(),
            now=100.0,
        )
    assert gate.clear_quiesce_lease(
        payload["lease_id"],
        owner_pid=os.getpid(),
        marker=marker,
        install_root=root,
    ) is False


@pytest.mark.parametrize(
    "field", ["created_at", "expires_at", "handoff_grace_until"]
)
def test_v1_epoch_fields_are_exact_integer_seconds(
    tmp_path: Path, field: str
) -> None:
    root = tmp_path / "install"
    root.mkdir()
    marker = tmp_path / gate.MARKER_NAME
    payload = _lease(root, owner_pid=os.getpid())
    payload[field] = float(payload[field])
    marker.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="malformed and not stale"):
        gate.live_quiesce_lease(marker, install_root=root, now=100.0)


def test_bounded_stale_structurally_invalid_json_primary_is_inactive(
    tmp_path: Path,
) -> None:
    root = tmp_path / "install"
    root.mkdir()
    marker = tmp_path / gate.MARKER_NAME
    marker.write_text('{"schema_version": 2}', encoding="utf-8")
    old = time.time() - gate.MAX_LEASE_SECONDS - 10
    os.utime(marker, (old, old))

    assert gate.live_quiesce_lease(marker, install_root=root) is None


def test_fresh_stable_malformed_primary_is_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "install"
    root.mkdir()
    marker = tmp_path / gate.MARKER_NAME
    marker.write_text("malformed-in-progress", encoding="utf-8")

    with pytest.raises(RuntimeError, match="malformed and not stale"):
        gate.live_quiesce_lease(marker, install_root=root)


def test_bounded_stale_malformed_primary_is_inactive(tmp_path: Path) -> None:
    root = tmp_path / "install"
    root.mkdir()
    marker = tmp_path / gate.MARKER_NAME
    marker.write_text("malformed-abandoned", encoding="utf-8")
    old = time.time() - gate.MAX_LEASE_SECONDS - 10
    os.utime(marker, (old, old))

    assert gate.live_quiesce_lease(marker, install_root=root) is None


def test_unreadable_primary_marker_is_fail_closed(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "install"
    root.mkdir()
    marker = tmp_path / gate.MARKER_NAME
    marker.write_text("occupied", encoding="utf-8")
    original = Path.read_text

    def deny_target(path, *args, **kwargs):
        if path == marker:
            raise PermissionError("sharing violation")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_target)

    with pytest.raises(RuntimeError, match="unreadable"):
        gate.live_quiesce_lease(marker, install_root=root)


def test_unreadable_recovery_directory_is_fail_closed(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "install"
    root.mkdir()
    marker = tmp_path / gate.MARKER_NAME
    original = Path.glob

    def deny_recovery(path, pattern):
        if path == marker.parent and pattern.startswith(marker.name):
            raise PermissionError("directory access denied")
        return original(path, pattern)

    monkeypatch.setattr(Path, "glob", deny_recovery)

    with pytest.raises(RuntimeError, match="recovery directory"):
        gate.live_quiesce_lease(marker, install_root=root)


def test_reader_rereads_new_primary_when_listed_shadow_disappears(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "install"
    root.mkdir()
    marker = tmp_path / gate.MARKER_NAME
    shadow = tmp_path / f"{gate.MARKER_NAME}.cas-shadow-race"
    now = int(time.time())
    lease = {
        "schema_version": 1,
        "lease_id": "lease-read-race-123456",
        "owner_pid": os.getpid(),
        "created_at": now,
        "expires_at": now + 120,
        "handoff_grace_until": now,
        "install_root": gate._canonical(root),
    }
    raw = json.dumps(lease, sort_keys=True)
    shadow.write_text(raw, encoding="utf-8")
    original = Path.read_text

    def complete_renewal_before_shadow_open(path, *args, **kwargs):
        if path == shadow and shadow.exists():
            marker.write_text(raw, encoding="utf-8")
            shadow.unlink()
            raise FileNotFoundError(str(shadow))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", complete_renewal_before_shadow_open)

    active = gate.live_quiesce_lease(
        marker,
        install_root=root,
        now=now + 1,
        pid_alive=lambda pid: pid == os.getpid(),
    )
    assert active is not None and active["lease_id"] == lease["lease_id"]


def test_explicit_renewal_never_recreates_a_disappeared_capability(
    tmp_path: Path,
) -> None:
    root = tmp_path / "install"
    root.mkdir()
    marker = tmp_path / gate.MARKER_NAME
    lease = gate.write_quiesce_lease(root, marker=marker)
    marker.unlink()

    with pytest.raises(RuntimeError, match="disappeared"):
        gate.write_quiesce_lease(
            root,
            marker=marker,
            lease_id=lease["lease_id"],
            owner_pid=os.getpid(),
        )


def test_expired_emergency_shadow_does_not_wedge_retry_for_twenty_minutes(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "install"
    home = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    shadow = gate.write_emergency_quiesce_shadow(
        root,
        lease_id="emergency-lease-123456",
        owner_pid=os.getpid(),
        now=100,
    )
    assert shadow.exists()

    lease = gate.write_quiesce_lease(
        root,
        marker=gate.marker_path(),
        owner_pid=os.getpid(),
        now=100 + gate.EMERGENCY_LEASE_SECONDS + 1,
    )

    assert not shadow.exists()
    assert lease["owner_pid"] == os.getpid()


@pytest.mark.parametrize(
    ("pid_alive", "pid_create_time", "expected_pending"),
    [
        (lambda _pid: True, lambda _pid: 99.0, True),
        (lambda _pid: False, lambda _pid: None, False),
        (lambda _pid: True, lambda _pid: 101.0, False),
    ],
    ids=["exact-live-owner", "dead-owner", "reused-owner"],
)
def test_expired_recovery_cleanup_checks_exact_owner_before_wall_clock(
    tmp_path: Path,
    monkeypatch,
    pid_alive,
    pid_create_time,
    expected_pending: bool,
) -> None:
    root = tmp_path / "install"
    root.mkdir()
    marker = tmp_path / gate.MARKER_NAME
    shadow = tmp_path / f"{gate.MARKER_NAME}.cas-shadow-test"
    shadow.write_text(json.dumps(_lease(root)), encoding="utf-8")
    os.utime(shadow, (100.0, 100.0))
    monkeypatch.setattr(gate, "_pid_alive", pid_alive)
    monkeypatch.setattr(gate, "_pid_create_time", pid_create_time)

    assert gate._pending_lease_recovery(marker, now=1400.0) is expected_pending
    assert shadow.exists() is expected_pending
