"""Fixtures shared across hermes_cli kanban tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    """Pretend every assignee maps to a real Hermes profile.

    Most dispatcher tests use synthetic assignees ("alice", "bob") that
    don't correspond to actual profile directories on disk. Without this
    patch, the dispatcher's profile-exists guard (PR #20105) routes
    those tasks into ``skipped_nonspawnable`` instead of spawning, which
    would break tests that assert spawn behavior.
    """
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@pytest.fixture(autouse=True)
def _suppress_concurrent_hermes_gate(request, monkeypatch):
    """Default ``_detect_concurrent_hermes_instances`` to ``[]`` for every test.

    The Windows update path now refuses to proceed when another
    ``hermes.exe`` is detected (issue #26670). On a developer's Windows
    machine running the test suite via ``hermes`` itself, this would
    flag the running agent as a concurrent instance and abort every
    ``cmd_update`` test. Tests that want to exercise the gate explicitly
    re-patch ``_detect_concurrent_hermes_instances`` with their own
    return value — autouse here gives a clean default without touching
    the rest of the suite.

    Tests that need to call the REAL function (e.g. unit tests for the
    helper itself) opt out with ``@pytest.mark.real_concurrent_gate``.
    """
    if request.node.get_closest_marker("real_concurrent_gate"):
        return
    try:
        from hermes_cli import main as _cli_main
    except Exception:
        return
    # raising=False: under pytest's per-test spawn isolation, a concurrent
    # xdist worker importing a module that transitively touches hermes_cli.main
    # can briefly expose a partially-initialized module object here — one where
    # _detect_concurrent_hermes_instances isn't defined yet. A bare setattr
    # would raise AttributeError and error the (unrelated) test. The attribute
    # always exists once main.py finishes importing, so a no-op when it's
    # transiently absent is the correct, race-free default.
    monkeypatch.setattr(
        _cli_main,
        "_detect_concurrent_hermes_instances",
        lambda *_a, **_k: [],
        raising=False,
    )


@pytest.fixture
def platform_neutral_update_lifecycle(monkeypatch):
    """Neutralize updater side effects without pretending to be another OS.

    Git/config-focused tests still enter the real host branch.  On native
    Windows this fixture replaces only the outer process-coordination effects
    (lock, bridge lease, Job, gateway pause, and holder scan) with inert test
    doubles.  That preserves truthful platform selection while preventing a
    unit test from touching live Hermes processes or install-global markers.
    """
    from hermes_cli import main as cli_main
    from hermes_cli import update_lock

    class _TestUpdateLock:
        holder = None

        def acquire(self):
            return True

        def prove_claim(self):
            return True

        def release(self):
            return None

    class _TestMutationJob:
        def abort(self, _reason=""):
            raise AssertionError("test updater unexpectedly lost its lease")

        def disarm(self):
            return None

    class _TestLeaseHeartbeat:
        lost = False
        loss_reason = None

        def __init__(self, _root, _lease, *, fail_stop):
            self.fail_stop = fail_stop

        def start(self):
            return None

        def stop(self):
            return None

    monkeypatch.setattr(update_lock, "UpdateLock", _TestUpdateLock)
    monkeypatch.setattr(
        cli_main,
        "_prepare_atomic_windows_update",
        lambda _args, *, root: (None, "test-invocation-123456"),
    )
    monkeypatch.setattr(cli_main, "_WindowsMutationJob", _TestMutationJob)
    monkeypatch.setattr(cli_main, "_UpdateLeaseHeartbeat", _TestLeaseHeartbeat)
    monkeypatch.setattr(
        cli_main, "_pause_windows_gateways_for_update", lambda **_kwargs: None
    )
    monkeypatch.setattr(cli_main, "_detect_venv_python_processes", lambda: [])
    monkeypatch.setattr(cli_main, "_venv_scripts_dir", lambda: None)
