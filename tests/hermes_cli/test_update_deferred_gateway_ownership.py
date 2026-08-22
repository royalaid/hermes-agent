import sys
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import update_cmd


def test_gateway_ready_record_binds_running_state_pid_generation_and_home(tmp_path):
    profile_home = tmp_path / "profiles" / "work"
    profile_home.mkdir(parents=True)
    identity = update_cmd._GatewayRuntimeIdentity(405, 60.0)
    state_path = profile_home / "gateway_state.json"

    def write_state(**overrides):
        payload = {
            "kind": "hermes-gateway",
            "gateway_state": "running",
            "pid": 405,
            "start_time": 6000,
            "hermes_home": str(profile_home),
            **overrides,
        }
        state_path.write_text(json.dumps(payload), encoding="utf-8")

    write_state(gateway_state="starting")
    assert not update_cmd._gateway_runtime_ready_record_matches(
        profile_home, identity
    )
    write_state(pid=406)
    assert not update_cmd._gateway_runtime_ready_record_matches(
        profile_home, identity
    )
    write_state(start_time=6001)
    assert not update_cmd._gateway_runtime_ready_record_matches(
        profile_home, identity
    )
    write_state(hermes_home=str(tmp_path / "other"))
    assert not update_cmd._gateway_runtime_ready_record_matches(
        profile_home, identity
    )
    write_state()
    assert update_cmd._gateway_runtime_ready_record_matches(profile_home, identity)


def test_gateway_ready_record_rejects_missing_and_malformed_identity_fields(tmp_path):
    profile_home = tmp_path / "profiles" / "work"
    profile_home.mkdir(parents=True)
    identity = update_cmd._GatewayRuntimeIdentity(405, 60.0)
    state_path = profile_home / "gateway_state.json"
    valid = {
        "kind": "hermes-gateway",
        "gateway_state": "running",
        "pid": 405,
        "start_time": 6000,
        "hermes_home": str(profile_home),
    }
    invalid = []
    for key in valid:
        invalid.append({name: value for name, value in valid.items() if name != key})
    invalid.extend(
        [
            {**valid, "kind": "other"},
            {**valid, "kind": None},
            {**valid, "gateway_state": True},
            {**valid, "pid": True},
            {**valid, "pid": "405"},
            {**valid, "start_time": True},
            {**valid, "start_time": "6000"},
            {**valid, "hermes_home": []},
        ]
    )

    for payload in invalid:
        state_path.write_text(json.dumps(payload), encoding="utf-8")
        assert not update_cmd._gateway_runtime_ready_record_matches(
            profile_home, identity
        ), payload

    state_path.write_text("{malformed-json", encoding="utf-8")
    assert not update_cmd._gateway_runtime_ready_record_matches(profile_home, identity)

    state_path.write_text(json.dumps(valid), encoding="utf-8")
    assert update_cmd._gateway_runtime_ready_record_matches(profile_home, identity)


def test_self_lock_deferral_never_bypasses_outer_recovery_owner(monkeypatch):
    resumed = []
    deferred = []
    fake_main = SimpleNamespace(
        _detect_self_loaded_native_modules=lambda: ["locked-extension.pyd"],
        _defer_update_for_self_lock=lambda modules: deferred.append(modules),
        _resume_windows_gateways_after_update=lambda token: resumed.append(token),
    )
    monkeypatch.setattr(update_cmd, "_m", lambda: fake_main)

    with pytest.raises(SystemExit) as exc:
        update_cmd._abort_dependency_sync_if_self_locked()

    assert exc.value.code == 2
    assert deferred == [["locked-extension.pyd"]]
    assert resumed == []


def test_deferred_gateway_spawn_passes_profile_home_without_mutating_parent_env(
    tmp_path, monkeypatch
):
    from hermes_cli import gateway_windows

    default_root = tmp_path / "hermes"
    expected_home = default_root / "profiles" / "work"
    expected_home.mkdir(parents=True)
    process = SimpleNamespace(pid=404)
    calls = []
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "parent-home"))
    monkeypatch.setattr(
        "hermes_constants.get_default_hermes_root", lambda: default_root
    )
    monkeypatch.setattr(
        gateway_windows,
        "_spawn_detached_process",
        lambda *, hermes_home, allow_breakaway: calls.append(
            (hermes_home, allow_breakaway)
        )
        or process,
    )

    assert update_cmd._spawn_deferred_gateway_profile("work") is process
    assert calls == [(expected_home, False)]
    assert update_cmd.os.environ["HERMES_HOME"] == str(tmp_path / "parent-home")


def test_exact_gateway_readiness_accepts_only_retained_wrapper_or_descendant(
    monkeypatch,
):
    from hermes_cli import gateway

    class Process:
        def __init__(self, pid):
            self.pid = int(pid)

        def create_time(self):
            return {404: 50.0, 405: 60.0, 999: 70.0}[self.pid]

        def parent(self):
            return Process(404) if self.pid == 405 else None

    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(Process=Process))
    spawned = SimpleNamespace(pid=404, poll=lambda: None)
    candidates = [SimpleNamespace(profile="work", path="C:/profiles/work", pid=999)]
    monkeypatch.setattr(gateway, "find_profile_gateway_processes", lambda: candidates)
    monkeypatch.setattr(
        update_cmd, "_gateway_runtime_ready_record_matches", lambda *_args: True
    )
    monkeypatch.setattr(
        update_cmd,
        "_profile_process_still_matches",
        lambda pid, created_at: (pid, created_at) in {(404, 50.0), (405, 60.0)},
    )

    with pytest.raises(RuntimeError, match="unrecognized gateway runtime"):
        update_cmd._exact_gateway_profile_identity("work", spawned, 50.0)

    candidates[:] = [SimpleNamespace(profile="work", path="C:/profiles/work", pid=405)]
    assert update_cmd._exact_gateway_profile_identity(
        "work", spawned, 50.0
    ) == update_cmd._GatewayRuntimeIdentity(405, 60.0)

    candidates[:] = [SimpleNamespace(profile="work", path="C:/profiles/work", pid=404)]
    assert update_cmd._exact_gateway_profile_identity(
        "work", spawned, 50.0
    ) == update_cmd._GatewayRuntimeIdentity(404, 50.0)

    candidates[:] = [SimpleNamespace(profile="work", path="C:/profiles/work", pid=405)]
    with pytest.raises(RuntimeError, match="unrecognized gateway runtime"):
        update_cmd._exact_gateway_profile_identity("work", spawned, 51.0)


def test_gateway_readiness_requires_stable_identity_and_final_recheck(monkeypatch):
    runtime = update_cmd._GatewayRuntimeIdentity(405, 60.0)
    scans = []
    sleeps = []

    def scan(*_args):
        scans.append("scan")
        return runtime

    monkeypatch.setattr(update_cmd, "_exact_gateway_profile_identity", scan)
    monkeypatch.setattr(update_cmd._time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(update_cmd._time, "sleep", sleeps.append)

    result = update_cmd._wait_for_deferred_gateway_profile(
        "work",
        SimpleNamespace(pid=404, poll=lambda: None),
        50.0,
        timeout=1.0,
    )

    assert result == update_cmd._GatewayReadinessResult(True, runtime)
    assert scans == ["scan", "scan", "scan"]
    assert sleeps == [0.2]


def test_gateway_readiness_failure_retains_last_authenticated_writer(monkeypatch):
    runtime = update_cmd._GatewayRuntimeIdentity(405, 60.0)
    scans = iter([runtime, None])
    clock = iter([0.0, 0.0, 1.0])
    monkeypatch.setattr(
        update_cmd, "_exact_gateway_profile_identity", lambda *_args: next(scans)
    )
    monkeypatch.setattr(update_cmd._time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(update_cmd._time, "sleep", lambda _delay: None)

    result = update_cmd._wait_for_deferred_gateway_profile(
        "work",
        SimpleNamespace(pid=404, poll=lambda: None),
        50.0,
        timeout=0.1,
    )

    assert result == update_cmd._GatewayReadinessResult(False, runtime)


def test_gateway_readiness_retains_changed_final_identity_for_cleanup(monkeypatch):
    runtime_a = update_cmd._GatewayRuntimeIdentity(405, 60.0)
    runtime_b = update_cmd._GatewayRuntimeIdentity(406, 61.0)
    scans = iter([runtime_a, runtime_a, runtime_b, None])
    clock = iter([0.0, 0.0, 0.0, 1.0])
    monkeypatch.setattr(
        update_cmd, "_exact_gateway_profile_identity", lambda *_args: next(scans)
    )
    monkeypatch.setattr(update_cmd._time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(update_cmd._time, "sleep", lambda _delay: None)

    result = update_cmd._wait_for_deferred_gateway_profile(
        "work",
        SimpleNamespace(pid=404, poll=lambda: None),
        50.0,
        timeout=0.1,
    )

    assert result == update_cmd._GatewayReadinessResult(False, runtime_b)


@pytest.mark.parametrize(
    "probe_error",
    [
        RuntimeError("unrecognized gateway runtime"),
        RuntimeError("multiple gateway runtimes were reported"),
        PermissionError("gateway identity access denied"),
        RuntimeError("gateway runtime identity changed"),
    ],
    ids=["unrecognized", "multiple", "access-denied", "identity-changed"],
)
def test_gateway_readiness_identity_probe_error_is_terminal_and_drains_once(
    monkeypatch, probe_error
):
    runtime = update_cmd._GatewayRuntimeIdentity(405, 60.0)
    scans = iter([probe_error, runtime, runtime, runtime])
    cleaned = []
    process = SimpleNamespace(pid=404)

    def scan(*_args):
        result = next(scans)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(
        update_cmd, "_spawn_deferred_gateway_profile", lambda _profile: process
    )
    monkeypatch.setattr(
        update_cmd, "_spawned_gateway_created_at", lambda _process: 50.0
    )
    monkeypatch.setattr(update_cmd, "_exact_gateway_profile_identity", scan)
    monkeypatch.setattr(update_cmd._time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(update_cmd._time, "sleep", lambda _delay: None)
    monkeypatch.setattr(
        update_cmd,
        "_terminate_unready_gateway_generation",
        lambda stopped_process, stopped_runtime=None: cleaned.append(
            (stopped_process.pid, stopped_runtime)
        ),
    )

    with pytest.raises(type(probe_error)) as raised:
        update_cmd._spawn_and_verify_deferred_gateway_profile("work")

    assert raised.value is probe_error
    assert cleaned == [(404, None)]


def test_gateway_readiness_failure_drains_writer_before_retained_launcher(
    monkeypatch,
):
    events = []

    class Spawned:
        pid = 404

        def poll(self):
            return None

        def terminate(self):
            events.append("launcher-terminate")

        def kill(self):
            raise AssertionError("graceful retained-handle termination should suffice")

        def wait(self):
            events.append("launcher-wait")
            return 1

    runtime = update_cmd._GatewayRuntimeIdentity(405, 60.0)
    monkeypatch.setattr(
        update_cmd, "_spawn_deferred_gateway_profile", lambda _profile: Spawned()
    )
    monkeypatch.setattr(
        update_cmd, "_spawned_gateway_created_at", lambda _process: 50.0
    )
    monkeypatch.setattr(
        update_cmd,
        "_wait_for_deferred_gateway_profile",
        lambda *_args, **_kwargs: update_cmd._GatewayReadinessResult(False, runtime),
    )
    monkeypatch.setattr(
        update_cmd,
        "_terminate_exact_gateway_runtime",
        lambda identity: events.append(f"writer:{identity.pid}"),
    )
    monkeypatch.setattr(
        update_cmd, "_gateway_runtime_is_still_alive", lambda _identity: False
    )

    with pytest.raises(RuntimeError, match="did not become ready"):
        update_cmd._spawn_and_verify_deferred_gateway_profile("work")

    assert events == ["writer:405", "launcher-terminate", "launcher-wait"]


def test_readiness_exception_drains_spawned_launcher_before_return(monkeypatch):
    events = []

    class Spawned:
        pid = 404

    process = Spawned()
    monkeypatch.setattr(
        update_cmd, "_spawn_deferred_gateway_profile", lambda _profile: process
    )
    monkeypatch.setattr(
        update_cmd, "_spawned_gateway_created_at", lambda _process: 50.0
    )
    monkeypatch.setattr(
        update_cmd,
        "_wait_for_deferred_gateway_profile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("readiness probe failed")
        ),
    )
    monkeypatch.setattr(
        update_cmd,
        "_terminate_unready_gateway_generation",
        lambda stopped_process, runtime=None: events.append(
            (stopped_process.pid, runtime)
        ),
    )

    with pytest.raises(RuntimeError, match="readiness probe failed"):
        update_cmd._spawn_and_verify_deferred_gateway_profile("work")

    assert events == [(404, None)]


@pytest.mark.parametrize(
    "failure",
    [KeyboardInterrupt("interrupted"), SystemExit(23)],
    ids=["keyboard-interrupt", "system-exit"],
)
def test_readiness_baseexception_cleans_exact_generation_once_and_reraises_same(
    monkeypatch, failure
):
    process = SimpleNamespace(pid=404)
    cleaned = []
    monkeypatch.setattr(
        update_cmd, "_spawn_deferred_gateway_profile", lambda _profile: process
    )
    monkeypatch.setattr(
        update_cmd, "_spawned_gateway_created_at", lambda _process: 50.0
    )

    def raise_failure(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(
        update_cmd, "_wait_for_deferred_gateway_profile", raise_failure
    )
    monkeypatch.setattr(
        update_cmd,
        "_terminate_unready_gateway_generation",
        lambda stopped_process, runtime=None: cleaned.append(
            (stopped_process.pid, runtime)
        ),
    )

    with pytest.raises(type(failure)) as raised:
        update_cmd._spawn_and_verify_deferred_gateway_profile("work")

    assert raised.value is failure
    assert cleaned == [(404, None)]


def test_multi_profile_failure_drains_every_earlier_startup_attempt(monkeypatch):
    plan = {
        "profiles": [
            {"name": "default", "old_pid": 101, "created_at": 10.0},
            {"name": "work", "old_pid": 202, "created_at": 20.0},
        ],
        "cold_start_if_installed": False,
    }
    process = SimpleNamespace(pid=401)
    runtime = update_cmd._GatewayRuntimeIdentity(402, 40.0)
    attempt = SimpleNamespace(profile="default", process=process, runtime=runtime)
    calls = []

    def spawn(profile):
        calls.append(f"spawn:{profile}")
        if profile == "work":
            raise RuntimeError("second profile failed")
        return attempt

    monkeypatch.setattr(update_cmd, "_running_gateway_profiles", lambda: {})
    monkeypatch.setattr(
        update_cmd, "_profile_process_still_matches", lambda *_args: False
    )
    monkeypatch.setattr(update_cmd, "_spawn_and_verify_deferred_gateway_profile", spawn)
    monkeypatch.setattr(
        update_cmd,
        "_terminate_unready_gateway_generation",
        lambda stopped_process, stopped_runtime: calls.append(
            f"drain:{stopped_process.pid}:{stopped_runtime.pid}"
        ),
    )

    with pytest.raises(RuntimeError, match="second profile failed"):
        update_cmd._resume_deferred_gateway_fleet(plan)

    assert calls == ["spawn:default", "spawn:work", "drain:401:402"]


def test_third_profile_failure_drains_first_two_in_reverse_despite_cleanup_error(
    monkeypatch,
):
    plan = {
        "profiles": [
            {"name": "default", "old_pid": 101, "created_at": 10.0},
            {"name": "work", "old_pid": 202, "created_at": 20.0},
            {"name": "third", "old_pid": 303, "created_at": 30.0},
        ],
        "cold_start_if_installed": False,
    }
    attempts = {
        "default": update_cmd._GatewayStartupAttempt(
            "default",
            SimpleNamespace(pid=401),
            update_cmd._GatewayRuntimeIdentity(501, 40.0),
        ),
        "work": update_cmd._GatewayStartupAttempt(
            "work",
            SimpleNamespace(pid=402),
            update_cmd._GatewayRuntimeIdentity(502, 41.0),
        ),
    }
    calls = []
    monkeypatch.setattr(update_cmd, "_running_gateway_profiles", lambda: {})
    monkeypatch.setattr(
        update_cmd, "_profile_process_still_matches", lambda *_args: False
    )

    def spawn(profile):
        calls.append(f"spawn:{profile}")
        if profile == "third":
            raise RuntimeError("third start failed")
        return attempts[profile]

    def terminate(process, runtime):
        calls.append(f"drain:{process.pid}:{runtime.pid}")
        if runtime.pid == 502:
            raise RuntimeError("work cleanup failed")

    monkeypatch.setattr(update_cmd, "_spawn_and_verify_deferred_gateway_profile", spawn)
    monkeypatch.setattr(update_cmd, "_terminate_unready_gateway_generation", terminate)

    with pytest.raises(RuntimeError, match="fleet cleanup could not be proven"):
        update_cmd._resume_deferred_gateway_fleet(plan)

    assert calls == [
        "spawn:default",
        "spawn:work",
        "spawn:third",
        "drain:402:502",
        "drain:401:501",
    ]


def test_readiness_refuses_mixed_authenticated_and_unrecognized_candidates(
    monkeypatch,
):
    from hermes_cli import gateway

    spawned = SimpleNamespace(pid=404, poll=lambda: None)
    candidates = [
        SimpleNamespace(profile="work", path="C:/profiles/work", pid=405),
        SimpleNamespace(profile="work", path="C:/profiles/work", pid=999),
    ]
    monkeypatch.setattr(gateway, "find_profile_gateway_processes", lambda: candidates)
    monkeypatch.setattr(
        update_cmd, "_gateway_runtime_ready_record_matches", lambda *_args: True
    )
    monkeypatch.setattr(
        update_cmd,
        "_authenticated_gateway_writer_created_at",
        lambda pid, *_args: 60.0 if pid == 405 else None,
    )
    monkeypatch.setattr(
        update_cmd,
        "_profile_process_still_matches",
        lambda pid, created_at: (pid, created_at) == (405, 60.0),
    )

    with pytest.raises(RuntimeError, match="unrecognized gateway runtime"):
        update_cmd._exact_gateway_profile_identity("work", spawned, 50.0)


def test_fleet_resume_checks_exact_old_generation_before_different_profile_pid(
    monkeypatch,
):
    plan = {
        "profiles": [{"name": "work", "old_pid": 202, "created_at": 20.0}],
        "cold_start_if_installed": False,
    }
    monkeypatch.setattr(
        update_cmd, "_running_gateway_profiles", lambda: {"work": 999}
    )
    monkeypatch.setattr(
        update_cmd,
        "_profile_process_still_matches",
        lambda pid, created_at: (pid, created_at) == (202, 20.0),
    )
    monkeypatch.setattr(
        update_cmd,
        "_spawn_and_verify_deferred_gateway_profile",
        lambda _profile: pytest.fail("must not spawn while the exact old gateway lives"),
    )

    with pytest.raises(RuntimeError, match="prior gateway profile 'work' is still running"):
        update_cmd._resume_deferred_gateway_fleet(plan)


def test_fleet_resume_refuses_unrecorded_same_profile_process(monkeypatch):
    plan = {
        "profiles": [{"name": "work", "old_pid": 202, "created_at": 20.0}],
        "cold_start_if_installed": False,
    }
    monkeypatch.setattr(
        update_cmd, "_running_gateway_profiles", lambda: {"work": 999}
    )
    monkeypatch.setattr(
        update_cmd, "_profile_process_still_matches", lambda *_args: False
    )
    monkeypatch.setattr(
        update_cmd,
        "_spawn_and_verify_deferred_gateway_profile",
        lambda _profile: pytest.fail("must not duplicate an unrecorded gateway"),
    )

    with pytest.raises(RuntimeError, match="unrecognized running gateway profile 'work'"):
        update_cmd._resume_deferred_gateway_fleet(plan)


def test_cold_start_refuses_unrecorded_default_profile(monkeypatch):
    plan = {"profiles": [], "cold_start_if_installed": True}
    monkeypatch.setattr(
        update_cmd, "_running_gateway_profiles", lambda: {"default": 999}
    )
    monkeypatch.setattr(
        update_cmd,
        "_spawn_and_verify_deferred_gateway_profile",
        lambda _profile: pytest.fail("must not duplicate an unrecorded gateway"),
    )

    with pytest.raises(RuntimeError, match="unrecognized running gateway profile 'default'"):
        update_cmd._resume_deferred_gateway_fleet(plan)


def test_completed_plan_requires_stable_operational_runtime_identity(
    tmp_path, monkeypatch
):
    from hermes_cli import gateway

    profile_home = tmp_path / "profiles" / "work"
    profile_home.mkdir(parents=True)
    candidate = SimpleNamespace(profile="work", path=profile_home, pid=405)
    candidates = [candidate]
    created_times = iter([60.0, 60.0])

    class Process:
        def __init__(self, pid):
            assert pid == 405

        def create_time(self):
            return next(created_times)

    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(Process=Process))
    monkeypatch.setattr(
        gateway, "find_profile_gateway_processes", lambda: list(candidates)
    )
    monkeypatch.setattr(
        update_cmd, "_profile_process_still_matches", lambda *_args: True
    )
    monkeypatch.setattr(
        update_cmd, "_gateway_runtime_ready_record_matches", lambda *_args: True
    )
    plan = {
        "profiles": [{"name": "work", "old_pid": 202, "created_at": 20.0}],
        "cold_start_if_installed": False,
    }

    assert update_cmd._completed_deferred_gateway_fleet_is_operational(plan)

    candidates.clear()
    assert not update_cmd._completed_deferred_gateway_fleet_is_operational(plan)


def test_plan_consume_failure_drains_uncommitted_fleet_before_return(
    tmp_path, monkeypatch
):
    from hermes_cli import update_lock
    import hermes_mcp_update_gate as gate

    root = tmp_path / "install"
    root.mkdir()
    invocation_id = "invocation-drain-123456"
    lease_id = "lease-drain-123456"
    args = SimpleNamespace(
        invocation_id=invocation_id,
        bridge_lease_id=lease_id,
        resume_root=str(root),
    )
    prior = {"schema_version": 1, "lease_id": lease_id, "owner_pid": 4321}
    process = SimpleNamespace(pid=401)
    runtime = update_cmd._GatewayRuntimeIdentity(402, 40.0)
    attempt = update_cmd._GatewayStartupAttempt("default", process, runtime)
    drained = []
    startup_gate = tmp_path / "startup.gate"
    startup_gate.write_text("armed", encoding="utf-8")
    monkeypatch.setenv(
        "HERMES_DEFERRED_GATEWAY_STARTUP_GATE", str(startup_gate)
    )

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
        update_cmd,
        "_claim_update_quiesce_lease",
        lambda *_args, **_kwargs: {**prior, "owner_pid": update_cmd.os.getpid()},
    )
    monkeypatch.setattr(
        update_cmd,
        "_load_update_receipt",
        lambda _root: {
            "invocation_id": invocation_id,
            "lease_id": lease_id,
            "gateway_resume_deferred": True,
        },
    )

    def load_plan(path, **_kwargs):
        if path.suffix == ".completed":
            return None
        return "raw-plan", {"profiles": [], "cold_start_if_installed": False}

    monkeypatch.setattr(update_cmd, "_load_deferred_gateway_plan", load_plan)
    monkeypatch.setattr(
        update_cmd, "_resume_deferred_gateway_fleet", lambda _plan: [attempt]
    )
    monkeypatch.setattr(
        update_cmd, "_consume_deferred_gateway_plan", lambda *_args: False
    )
    monkeypatch.setattr(
        update_cmd,
        "_drain_gateway_startup_attempts",
        lambda attempts: drained.extend(attempts),
    )
    monkeypatch.setattr(
        update_cmd,
        "_transfer_update_quiesce_lease",
        lambda _root, lease, *, new_owner_pid: lease,
    )

    with pytest.raises(SystemExit) as exit_info:
        update_cmd._cmd_update_resume_deferred_gateway(args, root=root)

    assert exit_info.value.code == 1
    assert drained == [attempt]


@pytest.mark.parametrize("failure_stage", ["manifest-remove", "plan-restore"])
def test_prepared_state_restoration_failure_stays_failed_and_drains_fleet(
    tmp_path, monkeypatch, failure_stage
):
    from hermes_cli import update_lock
    import hermes_mcp_update_gate as gate

    root = tmp_path / "install"
    root.mkdir()
    invocation_id = "invocation-restore-fail-123456"
    lease_id = "lease-restore-fail-123456"
    args = SimpleNamespace(
        invocation_id=invocation_id,
        bridge_lease_id=lease_id,
        resume_root=str(root),
    )
    gate_path = tmp_path / "startup.gate"
    gate_path.write_text("armed", encoding="utf-8")
    monkeypatch.setenv("HERMES_DEFERRED_GATEWAY_STARTUP_GATE", str(gate_path))
    prior = {"schema_version": 1, "lease_id": lease_id, "owner_pid": 4321}
    attempt = update_cmd._GatewayStartupAttempt(
        "default",
        SimpleNamespace(pid=401),
        update_cmd._GatewayRuntimeIdentity(402, 40.0),
    )
    drained = []

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
        update_cmd,
        "_claim_update_quiesce_lease",
        lambda *_args, **_kwargs: {**prior, "owner_pid": update_cmd.os.getpid()},
    )
    monkeypatch.setattr(update_cmd, "_load_update_receipt", lambda _root: None)

    def load_plan(path, **_kwargs):
        if path.suffix == ".completed":
            return None
        return "raw-plan", {"profiles": [], "cold_start_if_installed": False}

    monkeypatch.setattr(update_cmd, "_load_deferred_gateway_plan", load_plan)
    monkeypatch.setattr(
        update_cmd, "_resume_deferred_gateway_fleet", lambda _plan: [attempt]
    )
    monkeypatch.setattr(
        update_cmd, "_consume_deferred_gateway_plan", lambda *_args: True
    )
    manifest = tmp_path / "runtime-manifest.json"
    monkeypatch.setattr(
        update_cmd,
        "_write_prepared_gateway_runtime_manifest",
        lambda *_args, **_kwargs: (manifest, "manifest-raw"),
    )
    monkeypatch.setattr(
        update_cmd, "_release_update_quiesce_lease", lambda *_args: False
    )
    monkeypatch.setattr(
        update_cmd,
        "_remove_private_exact",
        lambda *_args: failure_stage != "manifest-remove",
    )
    restored = []
    monkeypatch.setattr(
        update_cmd,
        "_restore_consumed_deferred_gateway_plan",
        lambda *_args: restored.append(True) or False,
    )
    monkeypatch.setattr(
        update_cmd,
        "_drain_gateway_startup_attempts",
        lambda attempts: drained.extend(attempts),
    )
    monkeypatch.setattr(
        update_cmd,
        "_transfer_update_quiesce_lease",
        lambda _root, lease, *, new_owner_pid: lease,
    )

    with pytest.raises(SystemExit) as exit_info:
        update_cmd._cmd_update_resume_deferred_gateway(args, root=root)

    assert exit_info.value.code == 1
    assert drained == [attempt]
    assert restored == ([] if failure_stage == "manifest-remove" else [True])


def test_lease_cleanup_failure_restores_plan_before_drain_and_retry_restarts_fleet(
    tmp_path, monkeypatch
):
    from hermes_cli import update_lock
    import hermes_mcp_update_gate as gate

    root = tmp_path / "install"
    root.mkdir()
    invocation_id = "invocation-retry-123456"
    lease_id = "lease-retry-123456"
    args = SimpleNamespace(
        invocation_id=invocation_id,
        bridge_lease_id=lease_id,
        resume_root=str(root),
    )
    pending = tmp_path / "gateway-plan.json"
    completed = pending.with_suffix(".completed")
    raw = "authenticated-plan-bytes"
    pending.write_text(raw, encoding="utf-8")
    prior = {"schema_version": 1, "lease_id": lease_id, "owner_pid": 4321}
    released_locks = []

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
        update_cmd,
        "_deferred_gateway_plan_path",
        lambda _root, _invocation_id, *, completed=False: (
            pending.with_suffix(".completed") if completed else pending
        ),
    )
    monkeypatch.setattr(
        update_cmd,
        "_claim_update_quiesce_lease",
        lambda *_args, **_kwargs: {**prior, "owner_pid": update_cmd.os.getpid()},
    )
    monkeypatch.setattr(
        update_cmd,
        "_load_update_receipt",
        lambda _root: {
            "invocation_id": invocation_id,
            "lease_id": lease_id,
            "gateway_resume_deferred": True,
        },
    )

    plan = {"profiles": [], "cold_start_if_installed": False}

    def load_plan(path, **_kwargs):
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8"), plan

    monkeypatch.setattr(update_cmd, "_load_deferred_gateway_plan", load_plan)
    attempts = []

    def resume(_plan):
        sequence = len(attempts) + 1
        attempt = update_cmd._GatewayStartupAttempt(
            "default",
            SimpleNamespace(pid=400 + sequence),
            update_cmd._GatewayRuntimeIdentity(500 + sequence, 40.0 + sequence),
        )
        attempts.append(attempt)
        return [attempt]

    monkeypatch.setattr(update_cmd, "_resume_deferred_gateway_fleet", resume)
    manifest = pending.with_suffix(".prepared-runtime.json")

    def write_manifest(*_args, **_kwargs):
        manifest_raw = "authenticated-runtime-manifest"
        manifest.write_text(manifest_raw, encoding="utf-8")
        return manifest, manifest_raw

    monkeypatch.setattr(
        update_cmd, "_write_prepared_gateway_runtime_manifest", write_manifest
    )
    releases = iter([False, True])
    monkeypatch.setattr(
        update_cmd,
        "_release_update_quiesce_lease",
        lambda *_args: next(releases),
    )
    drained = []
    monkeypatch.setattr(
        update_cmd,
        "_drain_gateway_startup_attempts",
        lambda startup_attempts: drained.extend(startup_attempts),
    )
    monkeypatch.setattr(
        update_cmd,
        "_transfer_update_quiesce_lease",
        lambda _root, lease, *, new_owner_pid: lease,
    )

    def run_once(expected_code):
        startup_gate = tmp_path / f"startup-{len(released_locks)}.gate"
        startup_gate.write_text("armed", encoding="utf-8")
        monkeypatch.setenv(
            "HERMES_DEFERRED_GATEWAY_STARTUP_GATE", str(startup_gate)
        )
        with pytest.raises(SystemExit) as exit_info:
            update_cmd._cmd_update_resume_deferred_gateway(args, root=root)
        assert exit_info.value.code == expected_code

    run_once(1)

    assert pending.read_text(encoding="utf-8") == raw
    assert not completed.exists()
    assert not manifest.exists()
    assert drained == [attempts[0]]

    run_once(0)

    assert len(attempts) == 2
    assert drained == [attempts[0]]
    assert not pending.exists()
    assert pending.with_suffix(".prepared").read_text(encoding="utf-8") == raw
    assert not completed.exists()
    assert manifest.read_text(encoding="utf-8") == "authenticated-runtime-manifest"
    assert released_locks == [True, True]


def test_deferred_resume_requires_containment_before_plan_access(
    tmp_path, monkeypatch
):
    root = tmp_path / "install"
    root.mkdir()
    args = SimpleNamespace(
        invocation_id="invocation-gate-123456",
        bridge_lease_id="lease-gate-123456",
        resume_root=str(root),
    )
    monkeypatch.delenv("HERMES_DEFERRED_GATEWAY_STARTUP_GATE", raising=False)
    plan_reads = []
    monkeypatch.setattr(
        update_cmd,
        "_load_deferred_gateway_plan",
        lambda *_args, **_kwargs: plan_reads.append(True),
    )

    with pytest.raises(RuntimeError, match="containment gate"):
        update_cmd._cmd_update_resume_deferred_gateway(args, root=root)

    assert plan_reads == []


def test_deferred_resume_containment_gate_requires_explicit_armed_state(
    tmp_path, monkeypatch
):
    gate = tmp_path / "startup.gate"
    gate.write_text("wait", encoding="utf-8")
    monkeypatch.setenv("HERMES_DEFERRED_GATEWAY_STARTUP_GATE", str(gate))
    clock = iter([0.0, 0.0, 11.0])
    monkeypatch.setattr(update_cmd._time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(update_cmd._time, "sleep", lambda _delay: None)

    with pytest.raises(RuntimeError, match="was not armed"):
        update_cmd._await_parent_gateway_containment()

    assert "HERMES_DEFERRED_GATEWAY_STARTUP_GATE" not in update_cmd.os.environ


def test_deferred_resume_containment_gate_rejects_malformed_state(
    tmp_path, monkeypatch
):
    gate = tmp_path / "startup.gate"
    gate.write_text("unknown", encoding="utf-8")
    monkeypatch.setenv("HERMES_DEFERRED_GATEWAY_STARTUP_GATE", str(gate))

    with pytest.raises(RuntimeError, match="invalid state"):
        update_cmd._await_parent_gateway_containment()


def test_deferred_resume_containment_gate_accepts_exact_armed_state(
    tmp_path, monkeypatch
):
    gate = tmp_path / "startup.gate"
    gate.write_text("armed", encoding="utf-8")
    monkeypatch.setenv("HERMES_DEFERRED_GATEWAY_STARTUP_GATE", str(gate))

    update_cmd._await_parent_gateway_containment()

    assert gate.read_text(encoding="utf-8") == "armed"
    assert "HERMES_DEFERRED_GATEWAY_STARTUP_GATE" not in update_cmd.os.environ


def test_deferred_resume_containment_gate_retries_transient_replace_gap(
    tmp_path, monkeypatch
):
    gate = tmp_path / "startup.gate"
    gate.write_text("armed", encoding="utf-8")
    monkeypatch.setenv("HERMES_DEFERRED_GATEWAY_STARTUP_GATE", str(gate))
    original_open = Path.open
    attempts = 0

    def transient_open(path, *args, **kwargs):
        nonlocal attempts
        if path == gate and attempts == 0:
            attempts += 1
            raise FileNotFoundError("atomic gate replacement in progress")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", transient_open)
    monkeypatch.setattr(update_cmd._time, "sleep", lambda _delay: None)

    update_cmd._await_parent_gateway_containment()

    assert attempts == 1
    assert gate.read_text(encoding="utf-8") == "armed"
    assert "HERMES_DEFERRED_GATEWAY_STARTUP_GATE" not in update_cmd.os.environ


def test_deferred_resume_containment_gate_persistent_gap_fails_bounded(
    tmp_path, monkeypatch
):
    gate = tmp_path / "startup.gate"
    monkeypatch.setenv("HERMES_DEFERRED_GATEWAY_STARTUP_GATE", str(gate))
    clock = iter([0.0, 0.0, 2.3])
    monkeypatch.setattr(update_cmd._time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(update_cmd._time, "sleep", lambda _delay: None)

    with pytest.raises(RuntimeError, match="gate is unreadable"):
        update_cmd._await_parent_gateway_containment()

    assert "HERMES_DEFERRED_GATEWAY_STARTUP_GATE" not in update_cmd.os.environ


def test_deferred_resume_containment_gate_does_not_retry_malformed_state(
    tmp_path, monkeypatch
):
    gate = tmp_path / "startup.gate"
    gate.write_text("armed ", encoding="utf-8")
    monkeypatch.setenv("HERMES_DEFERRED_GATEWAY_STARTUP_GATE", str(gate))

    with pytest.raises(RuntimeError, match="invalid state"):
        update_cmd._await_parent_gateway_containment()
