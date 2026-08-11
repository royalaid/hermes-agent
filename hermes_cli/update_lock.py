"""Cross-process mutual exclusion for in-flight Hermes updates.

Three different surfaces can start an update of the same install tree:

* ``hermes update`` from a terminal,
* the dashboard's Update button (``POST /api/hermes/update`` →
  ``_spawn_hermes_action(["update"])``, detached),
* the desktop's Update button, which hands off to the Tauri
  ``hermes-setup --update`` and, on its failure screen, to install-mode
  bootstrap (``install.ps1`` / ``install.sh``).

Until now only the Tauri updater published an "update in progress" marker
(``UpdateMarkerGuard`` in ``apps/bootstrap-installer/src-tauri/src/update.rs``),
and only the Electron desktop consumed it (``electron/update-marker.ts``, to
gate local backend startup). Nothing stopped two *updaters* from running at
once — so a dashboard-spawned ``hermes update`` and an installer-driven
``git checkout`` could mutate the same checkout concurrently, rewriting source
under a live interpreter and leaving the tree half-updated.

This module makes that same marker the single lock for **all** update
entrypoints instead of adding a fourth mechanism. Format and location are
unchanged and remain byte-compatible with the Rust and Electron readers:

    <HERMES_HOME>/.hermes-update-in-progress   body: "<pid>\\n<started_at_unix>"

A marker counts as live while its exact process identity remains live.  The
timestamp bounds malformed/future claims and lets dead claims self-heal, but a
legitimate long dependency rebuild is not stolen merely because it crosses a
wall-clock ceiling.  PID create time prevents a reused numeric PID from
reviving an old claim.

One layering wrinkle: the Tauri updater holds this marker for its WHOLE run and
then spawns ``hermes update`` as a child stage. Without a handoff the child
sees its own parent's live marker and refuses — the GUI update deadlocks
against itself on every attempt ("Hermes is still running", retry forever).
Two mechanisms recognize the orchestrating parent, and either suffices:

* The updater exports :data:`HANDOFF_PID_ENV` naming its own pid, and
  ``acquire`` treats a live holder matching that pid as the lock we are
  already running under. The env var alone grants nothing: the pid must also
  be the live marker owner, so a stale or forged value cannot bypass the lock.
* A live holder that is a *process ancestor* of ours is likewise our own
  orchestrator. This is the load-bearing path for the fleet: the staged
  ``hermes-setup`` binary under ``~/.hermes`` is only refreshed by a full
  installer run (``copy_self_to_hermes_home`` deliberately no-ops during
  ``--update``), so every desktop whose staged updater predates the
  HANDOFF_PID_ENV export runs an old parent against a new child. Without the
  ancestry check those users get exit 2 ("Hermes is still running") on every
  GUI update forever, with no Hermes process actually running.
"""

from __future__ import annotations

import logging
import math
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Keep in sync with UPDATE_MARKER_MAX_AGE_MS in
# apps/desktop/electron/update-marker.ts — the same marker is read by both, and
# a shorter ceiling here would let Python steal a lock Electron still considers
# live. A full update (git pull + uv sync + desktop rebuild) is minutes.
UPDATE_MARKER_MAX_AGE_SECONDS = 20 * 60

MARKER_NAME = ".hermes-update-in-progress"

# Set by an orchestrating updater (the Tauri `hermes-setup --update` flow) to
# its own pid before spawning `hermes update` as a child stage. The parent
# holds the marker for its whole run, so without this the child refuses its
# own parent's lock and the GUI update can never complete. See update_child_env
# in apps/bootstrap-installer/src-tauri/src/update.rs — keep the name in sync.
HANDOFF_PID_ENV = "HERMES_UPDATE_HANDOFF_PID"

# Exit code meaning "another updater/instance owns this install right now".
# Already the de-facto contract: the Windows shim + venv-holder guards in
# _cmd_update_impl exit 2, and the Tauri updater matches on it
# (UPDATE_EXIT_CONCURRENT in apps/bootstrap-installer/src-tauri/src/update.rs)
# to show "Hermes is still running" instead of a generic failure. Naming it
# here keeps the concurrent-update refusal on that same understood contract.
UPDATE_EXIT_CONCURRENT = 2


def update_marker_path() -> Path:
    """Path of the shared update marker.

    Uses the *process* Hermes home (never the context-local profile override):
    the Rust updater resolves ``$HERMES_HOME`` or the platform default, and the
    desktop pins that same value into the updater's env. A profile-scoped path
    here would put the lock somewhere the other two owners never look.
    """
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / MARKER_NAME


def _pid_alive(pid: int) -> bool:
    """True when a process with ``pid`` currently exists.

    Delegates to :func:`gateway.status._pid_exists`, the project's existing
    no-kill probe. Do NOT hand-roll this with ``os.kill(pid, 0)``: on Windows
    that is not a no-op — CPython routes ``sig=0`` to
    ``GenerateConsoleCtrlEvent``, which Ctrl+C's the target's whole console
    process group (bpo-14484). A liveness check that killed the updater it was
    asking about would be a spectacular way to fix a concurrency bug.

    Any positive pid we cannot evaluate counts as live.  Mutation must stop
    when liveness is unprovable; malformed/non-positive pids are still stale.
    """
    if pid <= 0:
        return False
    try:
        # Stdlib-only and, critically, treats Windows OpenProcess access denial
        # as evidence that the PID exists. A protected updater must block a
        # second mutation; "could not inspect" is not proof of death.
        from hermes_mcp_update_gate import _pid_alive as probe_pid

        return bool(probe_pid(pid))
    except Exception as exc:
        # A probe failure is not evidence that the owner died.  This lock
        # protects source mutation, so an unprovable positive PID must fail
        # closed instead of deleting a possibly-live updater's claim.
        logger.debug("Could not probe pid %s: %s", pid, exc)
        return True


def _pid_matches_update_owner(pid: int, started_at: float) -> bool:
    """Prove the marker predates the same live process, failing closed.

    A definitive missing process or a process created after the marker means
    the numeric PID was reused.  Access denial, a missing optional probe, or
    any other inspection failure is not proof that mutation stopped and must
    continue to block.
    """
    if pid <= 0 or not math.isfinite(started_at) or started_at <= 0:
        return False
    if not _pid_alive(pid):
        return False
    try:
        import psutil

        created_at = float(psutil.Process(pid).create_time())
    except ImportError:
        return True
    except Exception as exc:
        try:
            import psutil

            if isinstance(exc, psutil.NoSuchProcess):
                return False
            if isinstance(exc, psutil.AccessDenied):
                return True
        except Exception:
            pass
        logger.debug("Could not validate create time for pid %s: %s", pid, exc)
        return True
    if not math.isfinite(created_at) or created_at <= 0:
        return True
    # The marker uses whole epoch seconds, so the same owner can have a
    # fractional creation time within that one serialized second. A process
    # created one full second later is a reused PID, not clock skew.
    return created_at < started_at + 1.0


def _restore_tombstone_without_overwrite(tombstone: Path, marker: Path) -> None:
    """Restore moved bytes only when no newer marker occupies the path."""
    # A hard link is an atomic exclusive restore: it either publishes the
    # exact tombstoned bytes or observes a newer claim.  It also avoids the
    # partial-file window of open/write/fsync.  Some filesystems do not
    # support links, so retain a fail-closed exclusive-copy fallback.
    try:
        os.link(tombstone, marker)
    except FileExistsError:
        try:
            tombstone.unlink()
        except OSError:
            pass
        return
    except OSError:
        pass
    else:
        try:
            tombstone.unlink()
        except OSError:
            pass
        return

    try:
        payload = tombstone.read_bytes()
    except OSError:
        return
    descriptor = None
    restored = False
    superseded = False
    try:
        descriptor = os.open(
            marker,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        restored = True
    except FileExistsError:
        superseded = True  # a newer claim is authoritative; never overwrite it
    except OSError as exc:
        # Preserve both the tombstone and any partial exclusive destination.
        # Deleting either after a failed write could erase the only evidence
        # of a live foreign owner; subsequent acquisition therefore fails
        # closed until the condition can be inspected/recovered.
        logger.warning("Could not restore update marker %s: %s", marker, exc)
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if restored or superseded:
            try:
                tombstone.unlink()
            except OSError:
                pass


def _move_marker_if_unchanged(marker: Path, expected: str) -> Path | None:
    """Move *marker* aside only if the moved bytes still match *expected*.

    There is no portable compare-and-unlink primitive. Moving first and then
    inspecting the exact inode prevents cleanup from deleting a foreign
    replacement; a mismatch is restored with exclusive creation so it cannot
    overwrite an even newer claim.
    """
    tombstone = marker.with_name(
        f"{marker.name}.release-{os.getpid()}-{secrets.token_hex(8)}"
    )
    try:
        os.replace(marker, tombstone)
        moved = tombstone.read_text(encoding="utf-8")
    except OSError:
        return None
    if moved != expected:
        _restore_tombstone_without_overwrite(tombstone, marker)
        return None
    return tombstone


def _has_pending_recovery(marker: Path) -> bool:
    """Return whether a recent release tombstone requires fail-closed recovery."""
    try:
        candidates = list(marker.parent.glob(f"{marker.name}.release-*"))
    except OSError:
        return True
    pending = False
    now = time.time()
    for candidate in candidates:
        try:
            age = now - candidate.stat().st_mtime
        except OSError:
            return True
        if not math.isfinite(age) or age <= UPDATE_MARKER_MAX_AGE_SECONDS:
            pending = True
            continue
        try:
            raw = candidate.read_text(encoding="utf-8")
            lines = raw.splitlines()
            pid = int(lines[0].strip())
            started_at = float(lines[1].strip())
        except (OSError, IndexError, TypeError, ValueError):
            pid = -1
            started_at = float("-inf")
        if _pid_matches_update_owner(pid, started_at):
            pending = True
            continue
        try:
            candidate.unlink()
        except OSError:
            pending = True
    return pending


def _handoff_pid() -> int | None:
    """Pid of the orchestrating updater that spawned us, if any.

    Read from :data:`HANDOFF_PID_ENV`. Malformed values count as absent —
    a broken handoff must fall back to the normal refusal, never crash.
    """
    raw = os.environ.get(HANDOFF_PID_ENV, "").strip()
    if not raw:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _is_ancestor_pid(pid: int) -> bool:
    """True when ``pid`` is a live ancestor (parent chain) of this process.

    The orchestrating updater spawns ``hermes update`` as a (grand)child, so a
    live marker owned by one of our ancestors can only be the claim we are
    already running under — an unrelated concurrent updater is never in our
    parent chain. This heals the fleet of staged ``hermes-setup`` binaries
    that predate the HANDOFF_PID_ENV export and can never send it.

    Never includes our own pid, and any failure counts as "not an ancestor":
    an unprovable ancestry must fall back to the normal refusal.
    """
    if pid <= 0:
        return False
    try:
        import psutil

        return any(parent.pid == pid for parent in psutil.Process().parents())
    except Exception as exc:
        logger.debug("Could not walk process ancestry for pid %s: %s", pid, exc)
        return False


@dataclass(frozen=True)
class UpdateHolder:
    """A confirmed-live update currently holding the lock."""

    pid: int
    age_seconds: float
    started_at: float
    raw: str


def read_live_update(*, path: Path | None = None) -> UpdateHolder | None:
    """Return the live update holding the lock, or ``None``.

    Absent, unreadable, malformed, definitively dead, and PID-reused claims
    mean "no live update". A live process remains authoritative past the
    ordinary age ceiling so a long dependency rebuild cannot be stolen.
    Never raises.
    """
    marker = path or update_marker_path()
    try:
        raw = marker.read_text(encoding="utf-8")
    except OSError:
        return None  # absent or unreadable => no live update

    lines = raw.splitlines()
    try:
        pid = int(lines[0].strip())
    except (IndexError, ValueError):
        pid = -1
    try:
        started_at = float(lines[1].strip())
    except (IndexError, ValueError):
        started_at = float("-inf")

    age = time.time() - started_at
    if (
        not math.isfinite(age)
        or age < -5
        or not _pid_matches_update_owner(pid, started_at)
    ):
        tombstone = _move_marker_if_unchanged(marker, raw)
        if tombstone is not None:
            try:
                tombstone.unlink()
            except OSError:
                pass
        return None

    return UpdateHolder(pid=pid, age_seconds=age, started_at=started_at, raw=raw)


def describe_holder(holder: UpdateHolder) -> str:
    """One-line, user-facing explanation of who holds the update lock."""
    minutes, seconds = divmod(int(max(holder.age_seconds, 0)), 60)
    elapsed = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
    return (
        f"✗ Another Hermes update is already running (PID {holder.pid}, "
        f"started {elapsed} ago).\n"
        "\n"
        "  Two updates mutating the same checkout corrupt it: one rewrites\n"
        "  source while the other is mid-install. Wait for it to finish, or\n"
        "  close the window/dashboard tab that started it, then retry."
    )


class UpdateLock:
    """Context manager owning the shared update marker for this process.

    ``acquired`` is False when another live update already holds it — callers
    decide whether that's a hard refusal (CLI/dashboard) or a wait. Releasing
    only removes the marker when *we* still own it, so a marker rewritten by a
    handoff partner (the Tauri updater overwrites it with its own pid) is never
    deleted out from under its new owner.
    """

    def __init__(self, *, path: Path | None = None) -> None:
        self.path = path or update_marker_path()
        self.acquired = False
        self.holder: UpdateHolder | None = None
        self.failure_reason: str | None = None
        self._claim_raw: str | None = None
        self._proof_raw: str | None = None

    def acquire(self) -> bool:
        """Claim the lock. Returns False (and sets ``holder``) if it's taken.

        A live holder whose pid matches :data:`HANDOFF_PID_ENV` — or is a
        process ancestor of ours — is our own orchestrating parent (the Tauri
        updater spawning `hermes update` as a stage): we run under ITS claim
        rather than refusing or re-writing the marker, and ``release`` leaves
        the parent's marker untouched. The ancestry path exists because staged
        updaters older than the HANDOFF_PID_ENV export never send the env var.
        """
        if _has_pending_recovery(self.path):
            self.failure_reason = "marker-recovery-pending"
            return False
        existing = read_live_update(path=self.path)
        if existing is not None:
            if existing.pid == _handoff_pid() or _is_ancestor_pid(existing.pid):
                self._proof_raw = existing.raw
                return True
            self.holder = existing
            self.failure_reason = "live-holder"
            return False
        if _has_pending_recovery(self.path):
            self.failure_reason = "marker-recovery-pending"
            return False
        started_at = int(time.time())
        claim = f"{os.getpid()}\n{started_at}\n"
        descriptor = None
        open_attempted = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            open_attempted = True
            descriptor = os.open(
                self.path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_BINARY", 0),
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                descriptor = None
                stream.write(claim)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            if not open_attempted:
                self.failure_reason = "marker-write-failed"
                return False
            self.holder = read_live_update(path=self.path)
            self.failure_reason = (
                "live-holder" if self.holder is not None else "unverifiable-marker"
            )
            return False
        except OSError as exc:
            logger.debug("Could not write update marker %s: %s", self.path, exc)
            self.failure_reason = "marker-write-failed"
            return False
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        self.acquired = True
        self._claim_raw = claim
        self._proof_raw = claim
        return True

    def prove_claim(self) -> bool:
        """Revalidate the exact own/inherited marker bytes and liveness."""
        expected = self._proof_raw
        if expected is None:
            return False
        try:
            if self.path.read_text(encoding="utf-8") != expected:
                return False
        except OSError:
            return False
        holder = read_live_update(path=self.path)
        if holder is None or holder.raw != expected:
            return False
        try:
            return self.path.read_text(encoding="utf-8") == expected
        except OSError:
            return False

    def release(self) -> None:
        """Drop the marker if this process still owns it. Never raises."""
        if not self.acquired:
            self._proof_raw = None
            return
        self.acquired = False
        self._proof_raw = None
        if self._claim_raw is None:
            return
        tombstone = _move_marker_if_unchanged(self.path, self._claim_raw)
        self._claim_raw = None
        if tombstone is None:
            return
        try:
            tombstone.unlink()
        except OSError:
            pass

    def __enter__(self) -> "UpdateLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()
