"""Cross-process update mutual exclusion (``hermes_cli.update_lock``).

Three surfaces can start an update of one install tree: a terminal ``hermes
update``, the dashboard's Update button (which spawns that same command
detached), and the desktop's Update button (Tauri updater → install-mode
bootstrap on its failure screen). Before the shared lock, two of them could run
concurrently and rewrite source under a live interpreter — observed in the wild
as an installer ``git checkout`` rewinding the checkout ~9k commits while a
dashboard-spawned ``hermes update`` was mid-``npm install``, which then failed
against the rewound tree's manifests.

These exercise the real marker file against a temp home — no mocks — because
the contract that matters is what the Rust updater and the Electron gate see on
disk.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from hermes_cli.update_lock import (
    HANDOFF_PID_ENV,
    UPDATE_MARKER_MAX_AGE_SECONDS,
    UpdateLock,
    _pid_matches_update_owner,
    _restore_tombstone_without_overwrite,
    describe_holder,
    read_live_update,
    update_marker_path,
)

# A pid no live process owns. os.kill(pid, 0) must report it dead so a crashed
# updater can never wedge every future update. Deliberately larger than any
# platform's pid_t so it also covers the corrupt-marker path (OverflowError).
DEAD_PID = 4294967294


@pytest.fixture
def marker(tmp_path):
    return tmp_path / ".hermes-update-in-progress"


def test_marker_path_follows_process_hermes_home(tmp_path, monkeypatch):
    """The lock must land where the Rust updater and Electron gate look.

    All three resolve the *process* HERMES_HOME; a profile-scoped path would
    put the lock somewhere the other two owners never read.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert update_marker_path() == tmp_path / ".hermes-update-in-progress"


def test_acquire_writes_pid_and_start_time(marker):
    lock = UpdateLock(path=marker)

    assert lock.acquire() is True
    assert lock.acquired is True

    lines = marker.read_text(encoding="utf-8").splitlines()
    assert int(lines[0]) == os.getpid(), "the Electron gate probes this pid for liveness"
    assert int(lines[1]) == pytest.approx(time.time(), abs=5)
    assert len(lines) == 2, "wire format is exactly pid + started_at"


def test_second_acquire_is_refused_while_the_first_is_live(marker):
    """The bug: two updaters mutating one checkout at the same time."""
    first = UpdateLock(path=marker)
    assert first.acquire() is True

    second = UpdateLock(path=marker)
    assert second.acquire() is False
    assert second.holder is not None
    assert second.holder.pid == os.getpid()
    assert second.acquired is False


def test_refused_lock_does_not_delete_the_live_owners_marker(marker):
    first = UpdateLock(path=marker)
    first.acquire()

    second = UpdateLock(path=marker)
    second.acquire()
    second.release()

    assert marker.exists(), "a refused claimant must never clear the live owner's lock"

    first.release()
    assert not marker.exists()


def test_release_leaves_a_marker_a_handoff_partner_now_owns(marker):
    """The desktop writes the marker, then the Tauri updater takes ownership.

    Releasing must not delete a marker whose pid is no longer ours — that would
    reopen the gate while the partner is still mid-update.
    """
    lock = UpdateLock(path=marker)
    lock.acquire()

    marker.write_text(f"{DEAD_PID}\n{int(time.time())}\n", encoding="utf-8")
    lock.release()

    assert marker.exists(), "the partner's marker is not ours to remove"


def test_exact_claim_proof_rejects_marker_swap(marker):
    lock = UpdateLock(path=marker)
    assert lock.acquire()
    assert lock.prove_claim()

    marker.write_text(f"{os.getpid() + 1}\n{int(time.time())}\n", encoding="utf-8")

    assert not lock.prove_claim()


def test_dead_owner_is_reclaimed_not_honored(marker):
    marker.write_text(f"{DEAD_PID}\n{int(time.time())}\n", encoding="utf-8")

    lock = UpdateLock(path=marker)
    assert lock.acquire() is True
    assert int(marker.read_text(encoding="utf-8").splitlines()[0]) == os.getpid()


def test_live_owner_past_the_age_ceiling_remains_authoritative(marker, monkeypatch):
    """Long dependency stages must not let a second updater steal the lock."""
    long_ago = int(time.time()) - UPDATE_MARKER_MAX_AGE_SECONDS - 60
    marker.write_text(f"{os.getpid()}\n{long_ago}\n", encoding="utf-8")
    monkeypatch.setattr(
        "hermes_cli.update_lock._pid_matches_update_owner", lambda *_args: True
    )

    lock = UpdateLock(path=marker)
    assert lock.acquire() is False
    assert lock.holder is not None


def test_reused_pid_does_not_revive_an_old_claim(marker, monkeypatch):
    marker.write_text(f"{os.getpid()}\n{int(time.time())}\n", encoding="utf-8")
    monkeypatch.setattr(
        "hermes_cli.update_lock._pid_matches_update_owner", lambda *_args: False
    )

    lock = UpdateLock(path=marker)
    assert lock.acquire() is True


def test_pid_created_one_second_after_marker_is_reused(monkeypatch):
    import psutil

    started_at = 1_000.0
    monkeypatch.setattr("hermes_cli.update_lock._pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        psutil,
        "Process",
        lambda _pid: type("Process", (), {"create_time": lambda self: 1_001.0})(),
    )

    assert not _pid_matches_update_owner(1234, started_at)


@pytest.mark.parametrize(
    "body",
    ["", "not-a-pid\n123\n", "\n\n", "12345"],
    ids=["empty", "garbage-pid", "blank-lines", "no-start-time"],
)
def test_malformed_markers_never_block_an_update(marker, body):
    marker.write_text(body, encoding="utf-8")

    assert read_live_update(path=marker) is None
    assert UpdateLock(path=marker).acquire() is True


def test_stale_marker_is_removed_on_read(marker):
    marker.write_text(f"{DEAD_PID}\n{int(time.time())}\n", encoding="utf-8")

    assert read_live_update(path=marker) is None
    assert not marker.exists(), "whoever notices a stale marker clears it"


def test_absent_marker_reports_no_live_update(marker):
    assert read_live_update(path=marker) is None


def test_context_manager_releases_even_on_exception(marker):
    with pytest.raises(RuntimeError):
        with UpdateLock(path=marker) as lock:
            assert lock.acquired is True
            raise RuntimeError("update blew up mid-flight")

    assert not marker.exists(), "a crashed update must not strand the lock"


def test_describe_holder_names_the_pid_and_elapsed_time(marker):
    lock = UpdateLock(path=marker)
    lock.acquire()

    holder = read_live_update(path=marker)
    assert holder is not None
    message = describe_holder(holder)

    assert str(os.getpid()) in message, "the user needs the pid to find the other update"
    assert "already running" in message


def test_unwritable_marker_location_fails_closed(tmp_path):
    """Never mutate without a provable, durable exclusive claim."""
    lock = UpdateLock(path=tmp_path / "nonexistent-file" / "marker")
    (tmp_path / "nonexistent-file").write_text("i am a file, not a dir", encoding="utf-8")

    assert lock.acquire() is False
    assert lock.acquired is False, "nothing was written, so there is nothing to release"
    assert lock.failure_reason == "marker-write-failed"


@pytest.mark.parametrize("started_at", ["nan", "inf", "-inf"])
def test_non_finite_marker_timestamp_is_never_live(marker, started_at):
    marker.write_text(f"{os.getpid()}\n{started_at}\n", encoding="utf-8")

    assert read_live_update(path=marker) is None


def test_pid_probe_failure_is_treated_as_live(marker, monkeypatch):
    marker.write_text(f"{os.getpid()}\n{int(time.time())}\n", encoding="utf-8")

    def inaccessible(_pid):
        raise PermissionError("access denied")

    monkeypatch.setattr("hermes_mcp_update_gate._pid_alive", inaccessible)

    holder = read_live_update(path=marker)
    assert holder is not None
    assert holder.pid == os.getpid()
    assert marker.exists()


def test_concurrent_claim_has_exactly_one_owner(marker):
    barrier = threading.Barrier(2)
    results = []
    locks = [UpdateLock(path=marker), UpdateLock(path=marker)]

    def claim(lock):
        barrier.wait()
        results.append(lock.acquire())

    threads = [threading.Thread(target=claim, args=(lock,)) for lock in locks]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert sorted(results) == [False, True]
    assert sum(lock.acquired for lock in locks) == 1
    for lock in locks:
        lock.release()


def test_failed_tombstone_restore_blocks_subsequent_claim(marker, monkeypatch):
    tombstone = marker.with_name(f"{marker.name}.release-test")
    tombstone.write_text(f"{os.getpid()}\n{int(time.time())}\n", encoding="utf-8")

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "link", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("no links")))
        scoped.setattr(os, "open", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk error")))
        _restore_tombstone_without_overwrite(tombstone, marker)

    assert tombstone.exists(), "failed recovery must retain the live-owner evidence"
    lock = UpdateLock(path=marker)
    assert lock.acquire() is False
    assert lock.failure_reason == "marker-recovery-pending"


class TestHandoffFromOrchestratingUpdater:
    """The Tauri updater holds the marker, then spawns ``hermes update``.

    The regression: the child saw its own parent's live marker and exited 2,
    so every GUI update failed with "Hermes is still running" and retrying
    just re-ran the same self-deadlock. The parent names its pid in
    HANDOFF_PID_ENV; a live holder matching it is our own orchestrator.
    """

    def test_child_runs_under_the_parents_live_claim(self, marker, monkeypatch):
        # Stand in for the parent updater with our own (live) pid.
        marker.write_text(f"{os.getpid()}\n{int(time.time())}\n", encoding="utf-8")
        monkeypatch.setenv(HANDOFF_PID_ENV, str(os.getpid()))

        lock = UpdateLock(path=marker)
        assert lock.acquire() is True
        assert lock.acquired is False, "the parent's claim is not ours to own"

        lock.release()
        assert marker.exists(), "the parent still needs its marker after our stage ends"
        assert int(marker.read_text(encoding="utf-8").splitlines()[0]) == os.getpid()

    def test_handoff_pid_that_is_not_the_live_holder_grants_nothing(self, marker, monkeypatch):
        """The env var alone must not bypass the lock."""
        marker.write_text(f"{os.getpid()}\n{int(time.time())}\n", encoding="utf-8")
        monkeypatch.setenv(HANDOFF_PID_ENV, str(os.getpid() + 1))

        lock = UpdateLock(path=marker)
        assert lock.acquire() is False
        assert lock.holder is not None

    @pytest.mark.parametrize("value", ["", "not-a-pid", "-1", "0"], ids=["empty", "garbage", "negative", "zero"])
    def test_malformed_handoff_values_fall_back_to_refusal(self, marker, monkeypatch, value):
        marker.write_text(f"{os.getpid()}\n{int(time.time())}\n", encoding="utf-8")
        monkeypatch.setenv(HANDOFF_PID_ENV, value)

        assert UpdateLock(path=marker).acquire() is False

    def test_handoff_env_with_no_marker_claims_normally(self, marker, monkeypatch):
        """A handoff pid must not stop us writing our own claim when unlocked."""
        monkeypatch.setenv(HANDOFF_PID_ENV, str(os.getpid()))

        lock = UpdateLock(path=marker)
        assert lock.acquire() is True
        assert lock.acquired is True
        assert int(marker.read_text(encoding="utf-8").splitlines()[0]) == os.getpid()


class TestAncestryHandoff:
    """Staged updaters older than the HANDOFF_PID_ENV export never send it.

    ``hermes-setup`` under ``~/.hermes`` is only refreshed by a full installer
    run, so an updated checkout (new lock) driven by a pre-handoff staged
    updater (old parent) deadlocks on exit 2 forever unless the child also
    recognizes a live holder that is its own process ancestor.

    ``_pid_alive`` is pinned True here because the hermetic conftest guards
    ``os.kill`` probes of pids outside the test subtree (our ppid included);
    liveness has its own coverage above — ancestry is what's under test.
    """

    @pytest.fixture(autouse=True)
    def _liveness_pinned_true(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.update_lock._pid_alive", lambda pid: True)
        monkeypatch.setattr(
            "hermes_cli.update_lock._pid_matches_update_owner", lambda *_args: True
        )

    def test_marker_owned_by_our_parent_process_is_our_orchestrator(self, marker):
        marker.write_text(f"{os.getppid()}\n{int(time.time())}\n", encoding="utf-8")

        lock = UpdateLock(path=marker)
        assert lock.acquire() is True, "a live ancestor's claim is the one we run under"
        assert lock.acquired is False, "the parent's claim is not ours to own"

        lock.release()
        assert marker.exists(), "the parent still needs its marker after our stage ends"
        assert int(marker.read_text(encoding="utf-8").splitlines()[0]) == os.getppid()

    def test_live_non_ancestor_holder_is_still_refused(self, marker):
        """Ancestry must not open the lock to unrelated concurrent updaters."""
        marker.write_text(f"{DEAD_PID}\n{int(time.time())}\n", encoding="utf-8")

        lock = UpdateLock(path=marker)
        assert lock.acquire() is False
        assert lock.holder is not None
        assert lock.holder.pid == DEAD_PID
