# Windows update aborts: "Desktop could not verify the Hermes installation is free"

Analysis date: 2026-09-02. Host: DESKTOP-O91444G. Installed app: `fork-integration @ 4637b690a0`
(built 08:39Z). Fork tip at analysis time: `0939dc5bbd` (no updater files changed since the install).

## TL;DR

1. **The dialog is not an ownership-tracking failure.** It is the `probe-failure` branch of the
   preflight: the Desktop rejected the scanner's JSON before looking at a single holder. All seven
   "Update now" clicks today (08:56Z to 09:10Z) logged
   `handoff preflight refused (venv-probe-failed): scanner envelope fields are invalid`.
2. **Root cause: two copies of the scanner drifted.** Desktop runs a bundled *carrier* copy
   (`apps/desktop/resources/update-scanner/scan-venv-blockers.py`, fork commit `50b9639990`,
   2026-08-31) with `python -I`, never `hermes_cli/_scan_venv_blockers.py`. The upstream rebase
   (`fdb2e10a8e`, PR #98350) added `deferred_backend_evidence` to the module **and** to the exact-key
   parser in `venv-blocker-scan.ts`; the carrier never got it. 15 keys emitted, 16 demanded,
   `hasExactKeys` fails, and a clean scan is reported as "could not verify".
3. **A second, latent drift was hiding behind the kill-all.** Fork commit `d4e069c1f9` made the
   scanner attach `parent_pid` to every generic record, including pausable-gateway records, but only
   taught the parser to accept it on `processes`. Any scan taken while a gateway is alive is
   rejected with `pausable gateway identity is invalid`. Production never saw it because
   `forceKillAllHermesBackendTrees()` SIGKILLs the gateway *before* the first scan. The moment the
   pre-scan kill-all is removed (the direction this analysis recommends), every update would fail
   here instead.
4. **Each failed click still did damage.** The kill-all runs before the scan, so every click
   SIGKILLed the gateway (two unclean gateway deaths in `gateway-exit-diag.log`, restarted by the
   2-minute watchdog), the Desktop backend, and the `llm-usage-tracker` plugin service host, which is
   only relaunched at login and is still down. No update ran.
5. **The holder-discovery stack proves the wrong resource.** Restart Manager and the exclusive-open
   probe are registered on three files (`venv\Scripts\hermes.exe`, `venv\Scripts\python.exe`,
   `venv\python.exe`). The real interpreter that keeps the `.pyd` files locked runs from
   `.hermes-runtime\python\...` and maps 23 files under `venv\Lib\site-packages`. Restart Manager
   on the shim files does not list it. "Unlocked" therefore proves the trampoline is gone, not that
   the venv can be mutated.

## 1. What failed today: timeline and evidence

| Time (UTC) | Event | Evidence |
| --- | --- | --- |
| 03:20 to 03:31 | Earlier, different failure: "repo hand-off script did not adopt matching bridge and update markers", then repeated `mcp-bridge-quiesce-failed` (stale lease) and "Hermes window (pid 41632) did not exit within 30s". Self-resolved; detached update finished OK at 03:31. | `logs/desktop.log:52652-52832`, handoff log 2026-09-01T20:31-07:00 |
| 08:31 to 08:35 | Three CLI runs `hermes update --branch fork-integration --yes`, all `outcome: refused`, exit 2, `pre_update_backup: disabled or failed`. Each restarted the gateway (`SystemExit 75`, then `gateway.start replace=true`). | `logs/update_receipts/update_20260902_01{3153,3405,3519}_*.json`, `gateway-exit-diag.log` |
| 08:35 to 08:39 | A successful CLI update reached `4637b690a0` and rebuilt the desktop (app.asar 08:39Z, desktop-build-stamp). | `logs/update.log`, `desktop-build-stamp.json` |
| 08:45 | Desktop relaunched on the new build (Hermes.exe pid 45628). | `desktop.log` |
| 08:56:43 to 08:57:57 | Five "Update now" clicks. Each: state.db backup, `venv shim unlocked and N signalled backend PID(s) exited; safe to proceed`, then `handoff preflight refused (venv-probe-failed): scanner envelope fields are invalid`. | `desktop.log:53231-53282` |
| ~08:56 | Gateway pid 37760 died: "exited UNCLEANLY (no exit path ran: SIGKILL / OOM / VM death)". Restarted by watchdog as pid 3540 at 08:59. | `gateway-exit-diag.log`, `gateway.log:4550` |
| 09:09:41, 09:10:14 | Two more clicks, same refusal. Gateway pid 3540 SIGKILLed, restarted as 50960 at 09:11. | `desktop.log:53319-53331`, `gateway.log:4590` |

Reproduction (read-only, safe to rerun):

```powershell
$root = "$env:LOCALAPPDATA\hermes\hermes-agent"
& "$root\venv\Scripts\python.exe" -I "$root\apps\desktop\release\win-unpacked\resources\update-scanner\scan-venv-blockers.py" --root $root
# -> 15 keys, ok=true, blocked=false. Parser demands 16 (deferred_backend_evidence) -> probe-failure.
```

Fed through the exact shipped parser (`parseVenvBlockerScanOutput` from `venv-blocker-scan.ts`, unchanged
between `4637b690a0` and the fork tip):

```
SHIPPED carrier (install):  exit=0 keys=15 -> parser kind=probe-failure error=scanner envelope fields are invalid
PATCHED carrier (worktree): exit=0 keys=16 -> parser kind=probe-failure error=pausable gateway identity is invalid   (before parser fix)
IN-TREE MODULE:             exit=0 keys=16 -> parser kind=probe-failure error=pausable gateway identity is invalid   (before parser fix)
PATCHED carrier + parser:   exit=0 keys=16 -> parser kind=clear blocked=false processes=0 bridges=0 gateways=2
```

## 2. The holder-discovery stack and what each layer can actually see

Six independent mechanisms decide "is the install free". They disagree on what a holder is.

| Layer | Where | Matches on | Blind to |
| --- | --- | --- | --- |
| `forceKillAllHermesBackendTrees` (fork kill-all, runs **before** any scan) | `main.ts:3481` | `Win32_Process.ExecutablePath` under install root, plus `wscript`/`cscript` whose cmdline names `desktop-plugins\...\service-host.vbs`; reduces to ppid roots; `taskkill /T /F` | Anything whose exe is outside the root: uv/uvx trampolines in `hermes\bin`, system or other python running a venv script, editors/AV/indexers holding handles, terminals with cwd inside the root. Kills before verification. |
| `_detect_venv_python_processes(strict=True)` | `hermes_cli/update_cmd.py:5447` | exe under `venv\` or `.hermes-runtime\python\`; venv path in cmdline; `hermes_cli.main` plus root in cmdline or cwd; exact MCP argv with cwd in root | Node or other-language services, handle-only holders, plain-python scripts importing from the venv via `sys.path`. Fail-closed: any `python.exe`/`pythonw.exe`/`hermes.exe` whose identity is unreadable (elevated, other session, SYSTEM task) raises and the whole scan becomes probe-failure. |
| Scanner classification (`_scan_venv_blockers.py` and its carrier copy) | module and `apps/desktop/resources/update-scanner/` | Exact argv/exe/ancestry tuples: MCP bridge wrapper/worker, plugin wrapper/worker plus live WSH host, `gateway run`, ledger-deferred serve/dashboard, else "hard_block/refuse" | Everything not in a named shape is `owner=unknown, action=refuse`. Two copies must agree byte-for-byte on the envelope; no test enforced that until now. |
| Exact-key parser | `venv-blocker-scan.ts:parseVenvBlockerScanOutput` | 16 envelope keys, per-record key sets, tuple consistency, pid uniqueness, create_time > 0, wrapper role graph | Any drift in either scanner copy becomes probe-failure with a message that blames the user ("close other windows"). |
| Restart Manager listing | `windows-restart-manager.ts`, resources from `installLockResources()` | Processes holding **three shim/trampoline files** | The real interpreter under `.hermes-runtime` and its mapped `.pyd` files. Live probe: RM on the shim files lists pid 54468 (trampoline) only; RM on the 23 mapped `.pyd`s lists pid 50960 only. |
| Exclusive-open lock probe (`isAnyInstallResourceLocked`) | `main.ts:3447` | Same three files | Same blind spot. Live probe: `hermes.exe` UNLOCKED, `python.exe` LOCKED, `yaml\_yaml.pyd` LOCKED, `jiter.pyd` LOCKED. Once the trampoline dies the probe says "unlocked" while `.pyd`s can still be mapped. |

Two more actors mutate the same state during the window: the elevated helper
(`scripts/desktop-update/windows-force-release.ps1`, Restart Manager on the same three files, kills only
signed claims) and the CLI updater's own rungs (`_pause_windows_gateways_for_update`, ledger reap/relaunch
for serve/dashboard) that only run after the Desktop hands off.

## 3. Every process touching the install right now, and who restarts it

psutil sweep over exe / cwd / cmdline / open files / mapped modules (73 s, 23 hits):

| Holder | Identity | Restarted by | Seen by kill-all | Seen by scanner |
| --- | --- | --- | --- | --- |
| Desktop tree `Hermes.exe` x5 (main, gpu, network, renderer, audio) | exe and cwd under `apps\desktop\release\win-unpacked`, asar/paks open | User or `desktop-update.ps1` relaunch | Excluded by design (`process.execPath`); helper processes share the path so they match the exclusion too | Not python, ignored |
| Gateway trampoline 54468 `venv\Scripts\python.exe -m hermes_cli.main gateway run` | parent 38088 already gone (orphan) | `Hermes_Gateway_Watchdog` scheduled task every **2 min** (gated by `.hermes-venv-quiesce` / `.hermes-update-in-progress` markers), login fallback `Startup\Hermes_Gateway.cmd` | Yes (SIGKILLed today, twice) | `pausable_gateway` |
| Gateway interpreter 50960 `.hermes-runtime\...\cpython-3.11-...\python.exe` (alias path; maps the `cpython-3.11.15-...` real path plus 23 venv `.pyd`s) | child of 54468 | same | Yes | `pausable_gateway` (via managed prefix) |
| `llm-usage-tracker` service (`wscript service-host.vbs` then `venv\Scripts\python.exe service.py`) | 10-second restart loop inside the VBS | **Only at login** (`Startup\Hermes_LLM_Usage_Tracker.cmd`); currently not running, killed by today's clicks | Yes (host and worker) | `desktop_plugin_*` |
| Kanban worker python 6076/46164 (cwd `hermes\kanban\workspaces\t_38d8c5d0`), node 31040 (cwd `...\t_c7b51153\repo`) | exe outside root | Kanban dispatcher inside the gateway (`default_assignee: implementer`) re-dispatches ready cards | No | No (exe/cwd outside root) |
| `uvx.exe`/`uv.exe` semble MCP servers x3 pairs (`hermes\bin\uv.exe`) | spawned by Claude Code sessions, including the one that wrote this | Claude Code MCP respawn | No (outside root) | No |
| Claude/Codex `agent.transports.hermes_tools_mcp_server` bridges | none alive right now | Codex/Claude reconnect; bridge watchers exit on the quiesce lease | Yes | `mcp_bridge_*`, actionable only if the ancestor chain proves `codex`/`claude` |
| `implementer` profile gateway pid 45808 recorded in receipts at old sha `5b136291f3` | dead now | Kanban/gateway profile launcher | n/a | n/a |

Mid-update respawn sources are therefore: watchdog task (2 min, marker-gated), plugin VBS loop (10 s,
not marker-gated, killed only if `wscript` is visible), Desktop backend pool (gated by
`update-in-flight`), kanban dispatcher and cron (inside the gateway, so paused with it), and external
agents (Claude/Codex) that reconnect their MCP bridge unless the lease is visible to them.

## 4. Are process trees reliable here?

Evidence says no, and the code already knows it:

- **Orphans are the norm.** The live gateway's parent (38088, the watchdog's `cmd.exe`) is gone; the
  updater-spawned processes are launched detached. `taskkill /T` only walks *current* ppid links, so a
  trampoline that exits before its interpreter leaves the interpreter with a dangling ppid outside
  any tree.
- **Ppid is a number, not a proof.** Windows recycles PIDs; the scanner spends ~200 lines defending
  against it (`_process_generation_matches`, create-time tolerance 0.01 s, "wrapper_pid must name a
  wrapper record", ancestry monotonic create-time checks). The elevated helper and Restart Manager use
  a 1.5 s tolerance because RM reports whole seconds. The two proofs do not even agree on identity.
- **Trampolines double every holder.** uv venvs on Windows run `venv\Scripts\python.exe` as a launcher
  that spawns the real interpreter from `.hermes-runtime`. Exe-path rules see one; RM on the shim
  sees the other. The alias directory (`cpython-3.11-...`) vs canonical directory (`cpython-3.11.15-...`)
  split already bricked updates once (`5778682085`, fixed by `bc2c9871d9`).
- **Ownership by ancestry is only as strong as the weakest ancestor.** `_owner_from_ancestry` walks
  parents looking for `codex`/`claude`/`apps\desktop` in names or argv. A bridge whose parent is a
  shell wrapper, a `node-pty` conhost, or a WSL relay is `owner=unknown`, which means `refuse`. That is
  the "python processes used by some backend plugins" case from the PR #92879 thread.

Conclusion: trees are usable for *ordering* (leaf-first) but not as the *authority* for what holds the
install. The authority has to come from the kernel's view of the files themselves.

## 5. The fix series that replaced kill-all, and why it misses this case

Royal's PR #92879 (closed 2026-08-31) stated the invariant ("once the user chooses Update now, the
install must be quiescent") and enforced it with a path-rooted `taskkill /T` sweep. Upstream declined and
shipped instead:

- **#99558** fail-closed identity guard: every kill requires positive `(pid, create_time)` identity.
- **#99724** ledger deferral: serve/dashboard backends with a ledger entry are left for the CLI updater's
  reap/relaunch rungs.
- **#98350** sanitized `deferred_backend_evidence` (the key the carrier lacks).
- **#100928** "successful update no longer credits surviving unmanaged serve runtimes".

The fork kept the kill-all *and* layered the upstream series on top (`forceKillAllHermesBackendTrees`,
then `releaseBackendLockForUpdate`, then `forceReleaseInstallHolders` (Restart Manager, 5 s), then the
scanner, the lease, two clear scans, `terminateMcpBridge`/`terminateDesktopPluginService`/
`terminateVenvHolder`, and finally the elevated helper). Why this stack cannot catch today's case:

1. Every layer is fail-closed on *its own* contract, so the number of ways to reach `probe-failure`
   grows with every commit, while none of them checks the one thing that bricks a venv (mapped
   `.pyd` files). Today's failure is pure contract drift with zero holders present.
2. The fork deliberately duplicated the scanner ("candidate-owned bytes, never a target-checkout
   module") to defend against a stale target, and no test compared the copy to the module or ran its
   output through the parser. The carrier test only checked `schema_version`, `mode`, `ok`, `root`,
   `venv`, `blocked`.
3. The kill-all masked the second drift (gateway `parent_pid`) by removing gateways before the scan,
   so the fork's own test fixtures never exercised a live-gateway envelope end to end.
4. The user-facing message for `probe-failure` is the same text as for real holders ("Close other
   Hermes windows and terminals"), so schema bugs are diagnosed as ownership bugs, which is exactly
   how this thread started.

## 6. What this branch changes (`fix/update-scanner-carrier-envelope`)

### 6a. Contract fix (commit 1)

| File | Change |
| --- | --- |
| `apps/desktop/resources/update-scanner/scan-venv-blockers.py` | Emit `deferred_backend_evidence: []` in `_base_result` and the scan result (carrier never defers, so the evidence list is empty). |
| `apps/desktop/electron/venv-blocker-scan.ts` | Accept optional `parent_pid` on pausable-gateway records (already validated as a positive integer). |
| `apps/desktop/electron/update-scanner-carrier.test.ts` | Run real carrier stdout through `parseVenvBlockerScanOutput`; assert it is never `probe-failure`. This is the contract the preflight actually consumes. |
| `apps/desktop/electron/venv-blocker-scan.test.ts` | Gateway record with `parent_pid` parses as `clear`; `parent_pid: 0` and unknown keys still fail. |

### 6b. Kill-all removed; kernel-proven holder detection (commit 2)

| File | Change |
| --- | --- |
| `apps/desktop/electron/main.ts` | `forceKillAllHermesBackendTrees` deleted. `runWindowsHandoffPreflight` now scans first and aborts with zero side effects on `probe-failure`; exact Desktop plugin services (worker, then wrapper plus its proven WSH host) are stopped through the scanner's revalidated path before the transactional preflight. The release gate, the force-release lock proof, the Restart Manager listing, and the elevated helper all use the mutation set below instead of three shim files. Force-release budget 5 s to 20 s, because discovery alone was never finishing in 5 s. |
| `apps/desktop/electron/install-mutation-set.ts` (new) | Enumerates every `.pyd`/`.dll`/`.exe` under `venv\` (shim files first; cached 30 s; 270 files in ~0.35 s here). `probeInstallResourceLocks` does an exclusive open per file (~25 ms for the set) and reads the hard-link count only for files that refused, splitting them into **definite** (single link: only our link can lock it) and **shared** (uv hard-linked the same wheel into other venvs or its cache). `.hermes-runtime` is excluded on purpose: the updater never rewrites a generation in place, and foreign uv tool venvs borrow its interpreter. |
| `apps/desktop/electron/windows-restart-manager.ts` | Resources travel in a flagged list file (never inline; the command line is capped). Definite files: per-file RM sessions up to 12, else one batch. Shared files: one RM batch, then each holder's module list is checked for a path under `venv\`; only holders mapping *our* link are kept, with that path as their resource. Native shim compiled once into a cached assembly. Default timeout 12 s because `RmGetList` resolves a friendly name per holder (~0.5 s each). |
| `apps/desktop/electron/backend-release-gate.ts` | `isShimLocked` may return a promise so the 15 s gate can use the attributed proof. |
| `apps/desktop/electron/update-preflight.test.ts` | The guard that required the kill-all now requires the opposite: no `taskkill`, no `Win32_Process` sweep, scan before any mutation, plugin services after a non-failing scan, transactional preflight last. |
| `install-mutation-set.test.ts`, `windows-restart-manager.test.ts` (new) | Enumeration, caching, lock split, list-file transport, batch/per-file selection, module attribution, assembly cache, row de-duplication, the array-unrolling regression. |

Why the hard-link split exists: uv links identical wheel files into every venv on the volume. This host's
`tokenizers.pyd` has six links, and Claude Code's three semble MCP servers (running from the uv cache)
were listed by Restart Manager as holders of *Hermes'* venv file. Sharing is per file but deletion is per
link: unlinking our link while another link is mapped succeeds (verified with an exclusive open and with a
`LoadLibrary` mapping; only the mapped link itself refuses deletion). So those processes cannot break the
venv sync and must not block or be killed, while the gateway interpreter that maps `venv\...\_yaml.pyd`
through our path must. Every earlier probe conflated the two.

Verified (this host, install venv python on PATH for the carrier test):

- Electron suites touched by the change: 8 files, 208 tests green before the attribution work; after it
  `install-mutation-set`, `windows-restart-manager`, `backend-release-gate`, `windows-update-force-release`,
  `windows-elevated-force-release`, `update-preflight`: 134/134.
- `tsc -p tsconfig.electron.json --noEmit` and `eslint` on every changed file: clean.
- Live, against the real install with the gateway and a cron `hermes backup` chain running and three semble
  servers alive: mutation set 270 files; locks definite=2 (the launchers), shared=24; attributed holders in
  ~3.4 s: the gateway launcher and interpreter, and the backup chain (all exe under the install), with the
  semble servers correctly absent. The pre-fix Restart Manager listing over the same files took 6.6 s and
  named the semble servers as holders.
- Before the contract fix, feeding the shipped carrier to the shipped parser gave
  `probe-failure: scanner envelope fields are invalid`; after both fixes, `clear` with `gateways=2`.

**Not verified until the install is rebuilt:** the user-visible "Update now" path.

## 7. Forward projection: what breaks next

### 7a. With the current fail-closed stack, even after this fix

1. **Any elevated or foreign-session `python.exe`** (admin terminal, SYSTEM scheduled task, another
   user) makes `_detect_venv_python_processes(strict=True)` raise "identity metadata was unreadable",
   which is a probe-failure. Not reproduced here; derived from `update_cmd.py:5447+` (psutil returns
   `None` for `exe`/`cmdline`/`cwd` on AccessDenied).
2. **Next upstream envelope change** repeats today's outage unless the carrier is regenerated from the
   module in the same commit. The new test catches key drift but not semantic drift (for example a new
   record role).
3. **Watchdog race.** The gateway watchdog fires every 2 minutes and is gated only by marker files that
   the Desktop does not write during preflight (the CLI updater writes them later). A watchdog tick
   between kill-all and the first scan resurrects the gateway. With the parser fix this is now
   `pausable` (fine); without it, probe-failure.
4. **Plugin VBS loop.** If `wscript.exe` is not visible in `Win32_Process` (different session, or the
   plugin is hosted by something other than WSH), the 10-second loop respawns `service.py` mid-update
   and the venv sync dies on a mapped `.pyd`.
5. **"Unlocked" lies.** After the trampoline exits, `isAnyInstallResourceLocked` returns false while an
   orphaned `.hermes-runtime` interpreter still maps `.pyd`s (a Claude/Codex bridge whose wrapper was
   killed first, a plugin worker whose wrapper died). The handoff proceeds and the update fails
   *inside* pip/uv with access-denied, which is the July `brotlicffi/_sodium.pyd` brick.
6. **Lease staleness.** A crashed preflight leaves the MCP quiesce lease behind; every later click
   fails `lease-unavailable` until it expires (seen at 03:21Z today).

### 7b. With a "simple/direct" fix

- **Loosen the parser** (accept unknown keys, or drop `hasExactKeys`): hides the next drift instead of
  surfacing it, and lets a malformed scanner smuggle a blocker past the gate (the #98350 threat model).
- **Path-rooted kill-all as the only mechanism** (PR #92879 shape): kills operator REPLs and user
  terminals running venv python; misses every holder whose exe is outside the root (uv trampolines in
  `hermes\bin`, handle-only holders, cwd holders); and, as today shows, destroys state (gateway
  SIGKILL, plugin service down until login) *before* verifying anything, so a no-op abort still costs
  a restart cycle.
- **Kill by Restart Manager on the shim files only**: never sees the interpreter that actually locks
  the venv; proves the fixture, not the venv.
- **Kill everything under the whole install by handle** (RM on every file): RM sessions over tens of
  thousands of files are slow and RM does not enumerate every handle type; it also kills
  Explorer/editors/AV and ends in the "needs Administrator" dead end.

## 8. Making it robust without killing everything

Principle: **verify against the kernel's view of the files the updater will mutate, order by tree,
authorize by identity, and never destroy state before you know an update can proceed.**

Status after this branch: items 2, 3, 5 and 6 are implemented (section 6b); item 1 is covered by the
round-trip test but the carrier is still a copy; items 4 and 7 remain design work.

1. **One scanner, one contract.** Generate the carrier from `hermes_cli/_scan_venv_blockers.py` at build
   time (or `import` it from the candidate checkout, which is what `-I` plus resources already achieves),
   and keep the new parser round-trip test. Add a Python unit test that asserts the carrier's
   `_base_result` key set equals the module's.
2. **Scan before you kill.** *(done)* The path-rooted kill-all is deleted; the first scan runs before
   anything is stopped and a `probe-failure` aborts with zero side effects. Today's seven clicks would
   then have cost nothing. Own holders are still released, but by identity, further down the same
   transaction (`hermes gateway stop --all` drains the gateway gracefully; the force-release kills only
   Restart-Manager-proven holders after `(pid, create time)` revalidation).
3. **Register the right resources with Restart Manager.** *(done, venv only)* The mutation set is every
   `*.pyd`/`*.dll`/`*.exe` under `venv\`, batched through a list file. `.hermes-runtime` is excluded
   because an update never rewrites a generation in place and foreign uv venvs borrow its interpreter.
   The same set backs the "unlocked" proof, with uv-shared hard links resolved by module attribution so
   that only holders of *our* link count.
4. **Classify RM holders, then act by class, not by kill.**
   - Own supervised processes (gateway, plugin services, backend pool): ask their supervisor to stop
     and hold (marker files the watchdog and the VBS loop honour; the VBS loop currently does not
     check the markers, so add the same `UpdateGateClosed` probe the watchdog has).
   - Agent-owned bridges (Claude/Codex): the quiesce lease already makes bridge watchers exit; extend
     the lease to be visible to the bridge itself so it exits even when ancestry cannot prove the
     owner.
   - Unknown same-user holders: show them with the *resource* they hold (RM gives the app name) and
     offer per-process stop; only on explicit "Force update" terminate them by identity, leaf-first,
     using the existing `terminateWindowsHolderWithinDeadline` path.
   - Foreign-session or elevated holders: elevated helper, unchanged.
5. **Break the trampoline pairing.** *(done by construction)* Both halves now appear in the attributed
   holder list on their own evidence (the launcher locks `venv\Scripts\python.exe`, the interpreter maps
   `venv\Lib\site-packages\*.pyd`), so neither depends on the other's ppid to be found; the existing
   leaf-first ordering and re-scan after termination remain.
6. **Make "unlocked" mean unlocked.** *(done)* `isAnyInstallResourceLocked` is now: any definite locked
   file, or any attributed holder of a shared one. It gates the release wait, the force-release loop, and
   the elevated path.
7. **Gate every respawn source on one marker.** The watchdog task already honours
   `.hermes-update-in-progress`; add the check to the plugin `service-host.vbs`, write the marker from the
   Desktop preflight (not only from the CLI updater), and clear it on abort.
8. **Distinct user-facing errors.** `probe-failure` should say "the updater's self-check failed
   (`<error>`); nothing was stopped" and never suggest closing windows. Keep the holder list message for
   real holders.

Order of work: 1 and 2 are small and remove today's outage and its collateral; 3 and 6 fix the false
"unlocked" proof; 4 and 7 are what lets the kill-all be deleted rather than wrapped.

## Appendix: probe commands used

```powershell
# psutil sweep of every process touching the install (exe/cwd/cmdline/open files/mapped modules)
& "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe" $env:TEMP\hermes-holder-sweep.py

# Restart Manager blind-spot probe: RM on shim files vs RM on the interpreter's mapped .pyd files
# (P/Invoke RmStartSession/RmRegisterResources/RmGetList; results quoted in section 2)

# supervisors
Get-ScheduledTask | ? { $_.TaskName -like '*hermes*' }   # Hermes_Gateway, Hermes_Gateway_Watchdog (PT2M), Hermes_Native_Presence
Get-Content "$env:LOCALAPPDATA\hermes\logs\gateway-exit-diag.log" -Tail 15
```
