# Plan: take wall-clock comparison out of the Windows update hand-off

Status: proposed 2026-09-06, after the first hand-off on the PR #7 Desktop
build aborted with "the repo updater did not acknowledge the protected
handoff" (fixed for the identity leg in PR #10). Not started.

## Why

The hand-off already has a logical acknowledgement: the Desktop mints an
unguessable lease id, the script adopts exactly that id under its own pid,
and the lease module binds a generation with compare-and-swap artifacts. That
part did its job on 2026-09-06. What failed was a *time comparison layered on
top of it*: the marker's `<ts>` was the Desktop's pre-spawn wall clock, and
the identity probe required the script's kernel creation time to be no later
than `<ts>` (+1 s). Any spawn that crossed a one-second boundary failed.

That was one of several places where two processes compare timestamps taken
from different clocks with a tolerance:

| predicate | where | tolerance |
|---|---|---|
| marker owner identity: `processCreatedAt < ts + 1` | `update-marker.ts probePidIdentity`, `hermes_mcp_update_gate._lease_owner_is_live` | 1 s |
| lease not from the future: `created_at <= now + skew` | `mcp-bridge-quiesce.ts`, `hermes_mcp_update_gate.py` | 5 s |
| hand-off grace: `now <= handoff_grace_until` | both | 90 s |
| lease expiry: `now <= expires_at` | both, `windows.ps1` (1200 s on adoption) | 20 min |
| artifact freshness: `mtime` vs `now` | `markerMtimeIsFresh` | seconds |
| marker age: `now - started_at` | `update_lock.py`, `UPDATE_MARKER_MAX_AGE_MS` | 20 min |
| Desktop start-time acceptance | `windows.ps1` (`HERMES_UPDATE_STARTED_AT`, now log-only) | 1200 s |

Each is a place to lose a race, a clock adjustment, or a suspend/resume jump.

## Principles

1. **Identity is a token, not an interval.** A process is identified by
   `(pid, kernel creation time)`. The owner writes its own creation time;
   readers compare with equality against the kernel. PR #10 does this for the
   update marker. Apply the same to the lease's `owner_pid`: add
   `owner_created_at` written by the adopter, and make `_lease_owner_is_live`
   / `syncPidIdentity` compare exactly instead of `< created_at + 1`.
2. **Acknowledgement is a nonce plus a generation.** Keep the lease id and
   the CAS generation as the only proof that *this* hand-off was adopted.
   Remove the "adoption happened within N seconds of my spawn" reasoning; the
   wait loop only needs "lease id matches, owner token matches the kernel,
   marker owner token matches the kernel".
3. **Expiry uses a machine-wide monotonic tick, not the wall clock.** Windows
   exposes uptime that every party reads identically: PowerShell
   `[Environment]::TickCount64`, Node `os.uptime()`, Python
   `time.monotonic()` (QueryPerformanceCounter; convert via a shared boot
   reference) or `ctypes.windll.kernel32.GetTickCount64`. Leases carry
   `boot_id` (from `SystemBootTime` or the kernel boot time rounded to the
   second) and `expires_tick`. A reader on a different boot treats every
   lease as dead, which is right: no owner survived the reboot.
4. **Wall clock is for humans only.** Keep `created_at` for logs and for the
   dashboard; never branch on it.

## Work items

1. `hermes_mcp_update_gate.py` + `mcp-bridge-quiesce.ts`: schema v2 lease
   with `owner_created_at`, `boot_id`, `expires_tick`, `handoff_grace_tick`.
   Readers accept v1 during the transition (v1 keeps today's rules).
2. `windows.ps1 Adopt-McpBridgeLease`: write v2 with its own creation time
   and tick values; `Remove-BridgeLeaseIfOwned` unchanged.
3. `update-marker.ts probePidIdentity` and `update_lock.py`: exact creation
   time match when the marker carries one (PR #10 makes Windows write it);
   POSIX keeps the acquisition-time contract until `posix.sh` follows.
4. `waitForMcpBridgeQuiesceLeaseAdoption`: drop the `resolvedPidIdentity`
   tolerance legs in favour of token equality; keep the 10 s ceiling only as
   a liveness bound on the spawn itself, and log the adoption state on give-up
   (PR #10 adds the line).
5. Tests: property-style checks that adoption succeeds for any spawn latency
   and any wall-clock offset between the two processes; a clock-jump test
   (set `now` backwards by an hour mid-hand-off) that must not revoke a live
   lease; a reboot test (different `boot_id`) that must retire it.
6. Docs: update the marker/lease contract comments in all three readers and
   `docs/analysis/2026-09-04-windows-update-plugin-service-respawn-rca.md`.

## Out of scope

The managed-SSH update path has its own markers on the remote host and is
not part of the local hand-off; the Tauri/Rust reader is legacy and reads
only `<pid>\n<ts>`.
