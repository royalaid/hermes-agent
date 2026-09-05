# Windows update refused: "install file locks could not be cleared within the force-release budget" (RCA, 2026-09-04)

Status: root-caused, reproduced live three times, fixed on branch
`fix/windows-update-plugin-service-unit`. Live proof of the fixed scanner and
timing measurements are recorded below.

## The dialog

```
Update aborted: install file locks could not be cleared within the force-release
budget. Still holding the install: PID 54020 python.exe resource=...\venv\Scripts\hermes.exe;
PID 57420 Python resource=...\venv\Scripts\hermes.exe; ...\venv\Scripts\python.exe.
The virtual environment was not modified.
```

desktop.log, 2026-09-05T04:00:50Z → 04:01:54Z (2026-09-04 21:00 PT):

```
04:00:58.011 plugin service PID 52308 (desktop_plugin_worker) stopped before handoff
04:00:58.318 plugin service PID 43264 (desktop_plugin_wrapper) not stopped before handoff
04:01:07–18  stopping Hermes-owned venv holder (operator-serve.pid / argv-identity) PID … ×15
04:01:34.589 install files still locked after 15s (venv\Scripts\python.exe); handing to identity-verified force-release
04:01:54.601 handoff preflight refused (venv-unlock-failed)
```

The same shape hit on 2026-09-03 at 15:26, 15:35 and 16:33 PT (those runs also
listed the Tailscale `hermes serve` tree, which 1ed7573db4 then drained).

## Who holds the install, and who starts them

The two PIDs in the dialog are the **llm-usage-tracker Desktop plugin service**,
respawned ten seconds after the preflight killed it. Launch chain (all outside
the repo):

```
Startup folder  %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Hermes_LLM_Usage_Tracker.cmd
  └─ wscript.exe  %LOCALAPPDATA%\hermes\desktop-plugins\llm-usage-tracker\service-host.vbs   (PID 24692, since 2026-09-02 10:53)
       └─ venv\Scripts\python.exe  service.py      (uv trampoline "wrapper", PID 57420, created 21:01:08)
            └─ .hermes-runtime\...\python.exe service.py   (real interpreter "worker", PID 54020, created 21:01:08)
```

`service-host.vbs` is a supervisor loop: run the service, and if it exits
non-zero, sleep 10 s and run it again, forever. The plugin's own
`service.log` shows one "service started" line per update attempt
(2026-09-03 08:13, 08:26, 08:35, 09:33; 2026-09-04 21:01:08), i.e. every
attempt killed the service and the loop brought it back.

The other holders the log names (serve/kernel/MCP trees, PIDs 19648, 75448,
…) were drained correctly by the 2026-09-03 argv-identity reap. Only the
plugin service survives, because it is the only holder with a supervisor.

## Why the existing code cannot stop it (three defects, all reproduced)

### 1. Worker-first draining leaves the supervisor alive

`runWindowsHandoffPreflight` (main.ts) stopped plugin services per record,
worker first, then wrapper. The scanner's `terminate_desktop_plugin_service`
killed the host **only when called for the wrapper**. But the wrapper is a uv
trampoline that exits the moment its child dies, so by the time the wrapper
call ran (300 ms later) there was no wrapper to prove and the call returned
`terminated: false` ("not stopped before handoff"). The host was never
touched and respawned the service 10 s later, inside the 15 s lock gate.

Reproduced with the shipped carrier against the live service (21:29 PT):

```
T0            host=24692 wrapper=57420 worker=54020
worker call   869 ms → terminated: true     (wrapper vanished with it)
wrapper call  289 ms → terminated: false    (nothing left to prove; host untouched)
t+8 s         host=24692 wrapper=44468 worker=67972   ← respawned
```

### 2. The force-release hands the exact-terminate script a resource it cannot prove

`forceReleaseHoldersFromScan` defaulted every scanner holder's `resource` to
the first mutation-set file, `venv\Scripts\hermes.exe`. `mergeInstallHolders`
then joined scanner and Restart Manager evidence into one string
(`"hermes.exe; python.exe"`). `buildExactTerminateScript` re-proves through
Restart Manager that the target *currently holds `resource`* before it
terminates anything; a runtime interpreter does not hold the shim and a joined
string is not a path, so every attempt ended
`TERMINATION_CURRENT_LOCK_OWNERSHIP_MISMATCH` — silently, since per-holder
outcomes were never logged. That is why the dialog names PIDs that are still
alive after the "force" release.

Reproduced with the production module (`terminateWindowsHolderWithinDeadline`)
against the live worker:

| holder resource claim                         | result                                             | time   |
|-----------------------------------------------|----------------------------------------------------|--------|
| `venv\Scripts\hermes.exe` (production default)| `failed: TERMINATION_CURRENT_LOCK_OWNERSHIP_MISMATCH` | 7.5 s |
| the worker's own image path                   | `terminated`                                       | 7.8 s  |

### 3. The 20 s budget cannot fit discovery plus one termination

Measured on this host with the bundled modules:

| step                                        | cost   |
|---------------------------------------------|--------|
| mutation-set enumeration + exclusive-open probe (270 files) | 0.5 s |
| scanner (`scan-venv-blockers.py`)           | 1.2 s  |
| Restart Manager attribution (7 holders)     | 8.1 s  |
| one exact termination (PowerShell boundary, Add-Type) | 7.5 s |

One pass = ~10 s of discovery before the first kill, and Restart Manager ran
again on every pass. Two holders need ~30 s; the loop always ended as
`timeout` with the holders alive.

## The graveyard, and what each attempt actually fixed

| attempt | what it fixed | why the dialog came back |
|---------|---------------|--------------------------|
| upstream #84778, fork blue-installer era | end-to-end handoff | pre-dates holder classification |
| #92879 / fork rebuild-killall (Aug 24) | kill every backend tree | SIGKILLed gateway + plugin services 7× per click; removed 2026-09-02 |
| fork PR #3 (Aug 21) | exact holder terminate, 5 s budget, RM discovery | budget never reached a kill; resource default already a lie |
| fork PR #5 (Aug 31) | rebuilt holder release on current line | explicitly excluded "exact-generation VBS plugin restoration" |
| 6431f7b82d + 3f52d18e8a (Sep 2) | carrier/parser envelope drift; mutation-set + RM attribution replaces kill-all | first attempt where the plugin service became the *only* blocker |
| 1ed7573db4 (Sep 3) | argv-identity drain of serve/kernel/MCP trees | left only the supervised plugin service |
| upstream #101502 (open) | gate + holder naming, no kill paths | not about supervisors |

Each layer was real progress; none addressed the supervisor, and defects 2
and 3 meant the last line of defence could never kill anything the scanner
had found.

## The fix

Python scanner (`hermes_cli/_scan_venv_blockers.py` and the standalone carrier
`apps/desktop/resources/update-scanner/scan-venv-blockers.py`, which inlines
the MCP gate and must be patched separately):

- `terminate_desktop_plugin_service_unit` re-proves the whole unit from either
  member and stops it **top-down: host → wrapper → workers**. A member that
  exits between proof and kill counts as stopped. The stop reports the host's
  launch line (`argv`, `cwd`) in a new optional `host` key of the terminate
  envelope.

Electron:

- `desktopPluginServiceUnits` collapses records to one anchor per unit;
  `runWindowsHandoffPreflight` and `runWindowsUpdatePreflight` issue exactly
  one stop per unit.
- `forceReleaseHoldersFromScan` no longer fabricates a resource;
  `mergeInstallHolders` keeps one Restart Manager path as the proof and lists
  the rest under `resources`; a scanner-only holder is terminated on its
  scanner-proven identity; a plugin-service holder is routed through the unit
  stop instead of the single-PID script.
- Force-release budget 20 s → 60 s, Restart Manager queried once per run
  (re-queried only when the scanner sees nothing), and every discovery pass
  and termination outcome is written to desktop.log.
- `desktop-plugin-host-restore.ts`: the stopped supervisor is recorded in
  `%LOCALAPPDATA%\hermes\update-stopped-plugin-hosts.json` and relaunched
  (detached, shape-checked: wscript/cscript + `.vbs` under `desktop-plugins`)
  on every update abort path and on the first boot after an update.

Also repaired on the branch: the published fork-integration tip 1447280627 has
a truncated line in `session-windows.ts` and a duplicated rememberLog state
block in `main.ts`; neither compiles. The installed checkout 97f6bae828 does
not have those defects.

## Live proof of the fixed scanner (22:04 PT)

```
scan: 2 plugin records; unit stop called with the WORKER pid 76556
UNIT call 894 ms → terminated: true, host: {pid 24692, wscript.exe service-host.vbs, cwd …}
after-unit  procs=[]        t+4 s []   t+8 s []   t+12 s []   t+16 s []
second scan: plugin records = 0
```

The supervisor was relaunched by hand afterwards (`service started` at
22:05:18) so the tracker kept working while the Desktop build was pending.

## Acceptance

The packaged app never enables CDP, so the "Update now" click is the
acceptance test. Expected desktop.log lines on the rebuilt install:

```
[updates] plugin service unit anchored at PID <wrapper> (desktop_plugin_wrapper) stopped before handoff
[updates] force-release discovery pass 0: scanner=… restart-manager=… (…ms)      (only if the 15 s gate still fails)
[updates] force-release PID … -> terminated (…ms)
[updates] plugin service hosts after update boot: relaunched=1 skipped=0        (on the relaunched app)
```


## Addendum: the first real run after the fix (2026-09-05 01:11 PT) and what it took to finish the chain

The Update click on the rebuilt Desktop got past every step this document is
about: the plugin service unit stopped in one call, the remaining holders were
drained, and the hand-off launched. It then failed further down the chain, and
the same session fixed each of those in PR #7:

| step | what the run showed | fix |
|------|---------------------|-----|
| desktop rebuild | `hermes update` ran the build for ~10 min with nothing on stdout and nothing in `logs/update.log` (the whole build buffered until exit; `_log_only_write` is a no-op in `--gateway` mode); the hand-off's 600 s idle watchdog killed it two minutes after the build had finished | `_run_logged_subprocess` streams into update.log and prints a progress line every 30 s |
| hand-off window | one `running:` line for the whole update; a viewer holding the log made every `Add-Content` fail silently | live step lines, `still running` heartbeat, retrying log writes that report dropped lines |
| gateway watchdog | `gateway stop --all` at 08:10:49Z, `Hermes_Gateway_Watchdog.vbs` relaunched it at 08:11:19Z inside the lock gate (it honors `.hermes-update-in-progress`, which the script claims later) | the Desktop holds the marker under its own pid for the drain and hands it to the script |
| force-release | `PID 58196 -> failed BOUNDARY_FAILED TREE_ASSIGN_FAILED win32=5` (holder already inside a job whose hierarchy refuses ours) | degraded containment: suspend, terminate by handle, report `CONTAINMENT_DEGRADED` |
| relaunch | the "Hermes update did not finish" dialog was synchronous: no backend, no log flush, no plugin-host relaunch until it was dismissed | async dialog |
| version state | install-stamp named the merged commit while `app.asar` was still the previous build; git-only skew detection said "in sync" because the stamp commit was unrelated to HEAD after the reset | `hermes desktop --build-needed` and the Desktop consulting it when git cannot place the stamp |

Two things the run also proved right: the plugin-host ledger relaunched the
tracker on the next boot (`plugin service hosts after update boot:
relaunched=1`), and the updater's up-to-date path still rebuilds a stale
bundle through `_rebuild_desktop_after_update`.

Known remaining gap: the Update card compares git HEAD with origin only, so a
current checkout with a stale bundle reports "You're all set" and never offers
the rebuild. The version IPC (`bundleOutOfSync`) knows; the card should use it.
