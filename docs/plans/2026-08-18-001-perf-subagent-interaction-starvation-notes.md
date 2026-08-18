# Subagent interaction starvation — operational note

## Goal and authority

- Branch: `perf/subagent-interaction-starvation`
- Base: `6f963921642519789ccb2ecf37e21a846153ed47`
- Scope completed here: deterministic probe-only fanout workload, exact `handleGatewayEventWithPlugins` bridge, report scenario, and read-only CDP watcher.
- No production renderer optimization, provider run, live profile/state/credentials access, CPU profile, baseline update, commit, push, PR, merge, release, or live Desktop control occurred.

## TDD evidence

Initial RED command:

```bash
npm test -- scripts/perf-scenarios.test.mjs src/app/chat/subagent-fanout-workload.test.ts
```

Result: 2 failed. Registry returned `undefined`; workload module import did not exist.

Final focused GREEN command:

```bash
npm test -- scripts/perf-scenarios.test.mjs scripts/perf-watch.test.mjs \
  src/app/chat/subagent-fanout-workload.test.ts \
  src/app/chat/perf-probe-bridge.test.ts \
  src/app/chat/perf-probe.contract.test.ts
```

Result: 5 files passed, 9 tests passed.

## Verification

```bash
npm run build
```

Passed: production renderer, Electron main/preload bundles, staged native dependencies, and `assert-dist-built` all completed successfully.

```bash
npx tsc -p . --noEmit --pretty false
```

Blocked by one pre-existing unrelated error in `src/app/session/hooks/use-session-actions.test.tsx:691`: fixture missing required `discardQueuedStreamState`. Filtering diagnostics showed no errors in the new perf probe, workload, bridge, scenario, or observer files.

```bash
git diff --check
```

Passed.

## Isolated production smoke

Ports `19324` and `15176` were explicitly probed free before each launch. The exact one-worker command was:

```bash
npm run perf -- subagent-fanout --spawn --prod \
  --workers 1 --turns 20 --updates 12 --interval-ms 33 --seed 41 \
  --port 19324 --dev-port 15176 \
  --json scripts/perf/results/subagent-interaction-starvation/subagent-fanout-w1.json
```

The isolated app launched with a disposable perf `HERMES_HOME`/user-data directory and CDP target, and teardown released both ports. No provider credit or live state was used. The isolated run reached the exact mounted gateway reducer (`gatewayDispatches: 14`, `gatewayDispatchFailures: 0`, one terminal worker row) and wrote `subagent-fanout-w1.json`, but its DOM proof was empty (`assistantMessages/codeCards/delegateCards: 0`; transcript/code/pane controls were null). That JSON is therefore **non-authoritative** and not a valid green reproduction. The scenario now rejects this shape explicitly instead of reporting zero-latency controls as success. Iteration also fixed two harness-only defects: unsupported `x/y` fields on `Input.dispatchKeyEvent(type='char')`, and loss of the freshly minted isolated runtime identity between probe calls.

Because the required one-worker smoke lacks DOM/interaction proof, the 2/4/8 scaling matrix was not run. The current blocker is transcript publication through the active runtime/view reconciliation seam, not evidence of the reported production starvation.

## Contamination and cleanup

- No synthetic backend/session 404 noise was accepted: the probe mints an actual session through the isolated gateway and records its runtime id.
- CPU profiling was never enabled.
- Failed isolated runs were torn down by the existing harness; final port probes reported `19324` and `15176` free.
- No observer or serve process was left running.

## Added surfaces

- `subagent-fanout-workload.ts`: bounded deterministic typed lifecycle (`start`, progress/thinking/tool updates, terminal completion).
- `perf-probe-bridge.ts`: narrow registration around the exact mounted `handleGatewayEventWithPlugins` callback, with stale-cleanup protection.
- `perf-probe.tsx`: extends existing `__PERF_DRIVE__`, snapshots/restores transcript, busy state, active runtime, subagent store, and timers; seeds delegate-task/tool and horizontal-code content.
- `scripts/perf/scenarios/subagent-fanout.mjs`: report-tier burst/steady/recovery frame/long-task metrics plus transcript scroll, composer key, pane switch, and code-scroll controls with DOM/store proof.
- `scripts/perf/watch.mjs`: explicit-port, read-only Runtime.evaluate watcher emitting redacted NDJSON; no profiler, input, navigation, mutation, or drive surface.

## 2026-08-18 evidence checkpoint

### Accepted

- Worktree remains on `perf/subagent-interaction-starvation` at fetched base `6f963921642519789ccb2ecf37e21a846153ed47`; `HEAD...fork/fork-integration` was `0 0` at the final fetch.
- Focused contracts are green:
  - Electron project: `scripts/perf-fixture-contract.test.mjs`, `scripts/perf-scenarios.test.mjs`, `scripts/perf-watch.test.mjs` — 7 tests passed.
  - UI project: `subagent-fanout-workload.test.ts`, `perf-probe-bridge.test.ts`, `perf-probe.contract.test.ts` — 5 tests passed.
- Exact native event path is exercised: disposable runtime creation → authoritative tile/session seeding → `dispatchPerfProbeGatewayEvent` → mounted `handleGatewayEventWithPlugins` → normal reducer/stores.
- The correct projection surface is a visible perf-only session tile (`session-tile:perf-fanout-visible`) plus a second control tile. The primary route is not a valid synthetic transcript projection because normal route/cache reconciliation owns it.
- Strict W1-class runs proved, across uncontaminated attempts:
  - `gatewayDispatches: 122`, `gatewayDispatchFailures: 0`;
  - `messageCount: 42`, `subagentRows: 1`;
  - rendered assistant transcript, code card, delegate card/row, and active status group.
- Earlier composer (7.6–14 ms), pane-switch (71–87 ms), and transcript-scroll (43–50 ms) values are retained only as **old-clock prepare/listener-to-paint diagnostics**. They are not accepted as user input-starvation latency and cannot falsify the 842–925 ms renderer-main input starvation from the source trace.
- The aggregate explicit-port read-only observer runtime is validated. Command:

  ```bash
  node scripts/perf/watch.mjs --port 19360 --samples 3 --interval-ms 250
  ```

  It returned three NDJSON samples containing only aggregate DOM counts, `heapMb`, existing live/frame aggregates, render counters, and scroll geometry. It emitted no transcript text, tool payload, URLs, session identifiers, credentials, profiler data, input, navigation, or mutation. `live: null` is expected on the cold isolated renderer because `__PERF_LIVE__` had no sample.
- Observer artifact: `apps/desktop/scripts/perf/results/subagent-interaction-starvation/watch-isolated-19360-20260818-0833.ndjson`.
- That saved artifact predates target-identity metadata. The corrected identity-bearing runtime receipt is `apps/desktop/scripts/perf/results/subagent-interaction-starvation/watch-isolated-identity-19370-20260818-0913.ndjson`.
- The corrected watcher attached only after `/json/list` proved exactly one page target. Three records carried stable safe identity `{port:19370,targetId,type:'page'}` plus aggregate snapshots. Schema validation found no title, URL, WebSocket URL, transcript, message, tool-payload, session-id, or credential keys.
- The owned launcher exited, but `19370/15220` remained served by untracked children. Ownership was ambiguous, so no PID was killed; those ports are abandoned.
- Observer attach remains explicit-port only. It does not authorize implicit attachment to a live Desktop or default port.

### Harness validity review — `deleg_3d4b9570`

A read-only review dispatched from the earlier 05:33 tree completed after the initial checkpoint. Current-tree verification retained four findings, with one timing nuance:

1. Worker scaling was serialized through one event-level timer. It now uses shared cadence batches: every tick publishes one typed event per worker, so `workers=1→8` increases same-window reducer/store pressure without multiplying lifecycle duration.
2. Reset restored `$subagentsBySession` before invalidating pending production coalescer work. The probe bridge now invokes the existing `discardQueuedStreamState(runtimeId)` before restoring snapshots. A direct `useMessageStream` regression proves a delayed flush cannot reapply synthetic progress after restore.
3. The old renderer clock started at prepare time rather than inside the DOM listener, but it lacked an independent host dispatch clock and did not decompose pre-event wait from post-event paint. Interaction receipts now report `hostMs`, `rendererWaitMs`, `paintMs`, and `rendererTotalMs`. Required top-level interaction metrics use `hostMs`.
4. Null interactions were rejected by the strict DOM gate, but required metric serialization still used a null/non-finite-to-zero formatter. Required interactions now throw on missing/non-finite host receipts; only non-required frame aggregates retain zero fallback.

The watcher now has source-level fail-closed target selection: exactly one page target must exist on the explicit port. Every new NDJSON record includes only safe identity `{port,targetId,type}`; title, URL, WebSocket URL, and content remain excluded. This identity-bearing shape is unit-validated but has not received a new isolated runtime receipt after the source-only fix.

Focused GREEN receipts after these changes:

- Electron project: `perf-watch`, fixture, and scenario contracts — **9 tests passed**.
- UI project: workload, bridge, probe, and coalescer regressions — **17 tests passed**.
- Direct coalescer file: **11 tests passed**, including delayed-flush-after-discard.
- `git diff --check`: passed.
- Full `npm run typecheck`: still exits on the pre-existing unrelated `use-session-actions.test.tsx` fixture missing required `discardQueuedStreamState`; no new-file diagnostic was observed before that existing failure stopped the chained command.

No Electron or scaling run has tested the corrected cadence/timing/reset/identity combination. All prior W1 measurements predate at least one validity fix and are not reproduction baselines.

### Rejected or blocked

- `subagent-fanout-w1.json` / seed 41 is invalid and excluded: zero DOM proof, `messageCount: 0`, and three null controls rendered as zero. Its filename/port provenance was also contaminated by overlapping earlier attempts.
- Seed 89/97 attempts on `19341/15193` overlapped and are excluded.
- No single strict W1 run produced all four interaction records. Horizontal code-scroll CDP input delivery remains the collector blocker. Later bounded attempts identified moving shared-viewport geometry and finally `Position out of bounds` from `Input.synthesizeScrollGesture`.
- This is a collector/input-delivery blocker, not evidence that the production gateway/reducer or rendered workload failed.
- W1 is therefore not fully accepted. W2/W4/W8 scaling, minimization, RCA checkpoint, commit/publication, and draft PR remain blocked.

### Contamination and environment notes

- Never use default CDP `9222`; use a verified-free high port and a unique result filename.
- A disposable `perf:serve` must pin the dependency-complete Python and source root, for example:

  ```bash
  HERMES_DESKTOP_PYTHON='<Hermes managed checkout>/venv/Scripts/python.exe' \
  HERMES_DESKTOP_HERMES_ROOT='<isolated worktree>' \
  PERF_PORT=<explicit-high-port> PERF_DEV_PORT=<explicit-high-port> npm run perf:serve
  ```

  The unpinned interpreter failed with `ModuleNotFoundError: No module named 'rich'`.
- The owned observer launcher was stopped. `19360/15210` remained occupied afterward by an untracked child; ownership was ambiguous, so no PID was killed. Those ports are abandoned.
- Perf-only onboarding suppression is gated by `VITE_PERF_PROBE=1`; no live profile or credentials were copied.
- Full TypeScript validation remains blocked by the pre-existing unrelated `use-session-actions.test.tsx` fixture missing `discardQueuedStreamState`. Focused new-file tests and production build passed.

## 2026-08-18 final reproduction checkpoint

The reproduction and realtime-observer goal is complete. Production optimization remains intentionally out of scope.

### Authoritative observer receipt

- Artifact: `apps/desktop/scripts/perf/results/subagent-interaction-starvation/watch-isolated-identity-19370-20260818-0913.ndjson`.
- The explicit CDP port exposed exactly one page target before attach.
- Three samples carried stable safe identity `{port,targetId,type}` plus aggregate DOM/heap/render/scroll counters.
- Schema validation found no title, URL, WebSocket URL, transcript/message text, tool payload, session id, credentials, input, navigation, profiler, or mutation data.
- The owned launcher exited. Its untracked children retained `19370/15220`; ownership was ambiguous, so those ports were abandoned instead of killing unknown PIDs.

### Independent responsive control

- Scenario: `code-scroll-control`.
- Artifact: `apps/desktop/scripts/perf/results/subagent-interaction-starvation/code-scroll-control-rehomed-provisional-20260818-0928.json` (the filename says provisional because a refetch was temporarily rate-limited; a later successful refetch confirmed `HEAD == fork/fork-integration`, divergence `0 0`).
- Proof: 5 real code cards; `scrollWidth=4096`, `clientWidth=1032`; hit-tested visible real scroller; one wheel and one scroll event; `scrollLeft 0 → 244.44`; finite `hostMs=51.56`, `rendererWaitMs=45.8`, `paintMs=0`; clean teardown.
- The first two control attempts failed honestly. Diagnostics showed the marked scroller was entirely above the viewport and received zero wheel events. Re-homing the same real card inside the arming step fixed target validity without changing the wheel mechanism.

### Minimized active-update reproduction

Command:

```bash
npm run perf -- subagent-fanout --spawn --prod \
  --workers 1 --turns 1 --updates 12 --interval-ms 33 --seed 113 \
  --port <verified-free-cdp-port> --dev-port <verified-free-dev-port> \
  --json <unique-result-path>
```

Authoritative artifact: `apps/desktop/scripts/perf/results/subagent-interaction-starvation/fanout-min-active-updates12-dispatch-progress-seed113-20260818-1001.json`.

Strict validity proof:

- exact mounted gateway/plugin/reducer path;
- `interactionPhaseStart.fanoutActive=true` and `interactionPhaseEnd.fanoutActive=true`;
- non-terminal dispatch count advanced **8 → 10 during the three real input probes**;
- final dispatches `14`, failures `0`, one reduced subagent row;
- 4 authoritative messages;
- rendered assistant transcript, code card, delegate card/row, active status group, composer, transcript viewport, and control tab;
- finite host/renderer receipts for transcript scroll, composer input, and pane switching;
- terminal batch released only after input probes through the same production callback;
- clean teardown and recovery.

Measured red signature:

- burst frame p95: **160.5 ms**;
- burst long tasks: **2** (`145`, `154` ms);
- steady frame p95: **88.2 ms**;
- steady long tasks: **2** (`96`, `109` ms);
- pane switch host latency: **134.7 ms**;
- transcript scroll host latency: **42.2 ms**;
- composer host latency: **14.0 ms**;
- recovery frame p95: **18.3 ms**;
- recovery long tasks: **0**.

This reproduces the reported shape: interaction starvation while non-terminal subagent updates are arriving, followed by prompt recovery after terminal completion.

`updates=12` is the smallest harness-valid case at the fixed 33 ms cadence and current setup floor. The scenario spends a fixed 300 ms preparing strict DOM/control proof; fewer updates can finish before probes begin. A terminal barrier preserves running-row state, but `updates=1` has no non-terminal dispatch left to advance during probes and therefore is not active-update evidence. Do not describe 12 as the application’s mathematical threshold.

### Amplifiers and falsification

- **Retained transcript depth amplifies but is not required.** Phase-valid `turns 20 → 1` reduced burst p95 `212.7 → 159.7 ms`, steady p95 `70.8 → 52.1 ms`, and pane switch `109.5 → 85.7 ms`; the reproduction remained red with only 4 messages.
- **Worker count amplifies but does not create the floor.** Earlier fixed-seed W1/W2/W4/W8 workload diagnostics showed steady frame p95 `69.5 → 72.1 → 106.2 → 107.8 ms`, pane switch `103.2 → 114.0 → 135.9 → 138.3 ms`, and burst long-task count `2 → 2 → 4 → 5`. Those artifacts predate the dispatch-progress receipt, so retain them as workload-amplification diagnostics, not authoritative active-update timings.
- **Update count amplifies but is not required down to the harness-valid floor.** Phase-valid `updates 120 → 12` remained red and dispatches advanced during probes.
- **Post-completion retained state is not sufficient.** Every accepted run recovered to about 18 ms frame p95 with zero recovery long tasks.
- **Horizontal code scrolling remains a responsive independent control.** It is no longer coupled to the moving fanout transcript viewport.

### Ranked root-cause evidence

1. **Mounted transcript/delegate/status style and render work creates a large worker-independent floor.** Supported by the original trace’s ~27.1 s `UpdateLayoutTree` versus ~0.624 s physical layout, and by a strong W1 red signal with one worker and four messages.
2. **Store/React publication fan-out amplifies the floor.** Supported by same-cadence worker scaling and prior render-churn evidence (`$sessionStates` notifications, listener calls, wasted renders); W4/W8 workload diagnostics raise steady frame and pane-switch costs.
3. **Input/pointer handling is delayed by renderer-main work rather than native-window failure.** Supported by host/renderer timing, original 842–925 ms input tasks, and clean native-window/recovery behavior.
4. **Retained transcript depth is secondary.** Red survives at one turn, though deeper content increases burst/style cost.
5. **Provider latency, live credentials, and gateway IPC are weak explanations.** The reproduction uses deterministic mocked events, zero provider calls, and exact local dispatch with zero failures.

### Public prior-art reconciliation

Read-only GitHub recon completed after the local checkpoint. Local trace and controlled artifacts remain ground truth.

- **Landed but incomplete:** fork commit [`31065f6`](https://github.com/royalaid/hermes-agent/commit/31065f624de0a9dd24b30ebf607ded54581680fc) coalesces non-terminal `subagent.*` and tool-progress publishes. `git merge-base --is-ancestor` proves it is an ancestor of measured `HEAD` `6f963921…`. Starvation still reproduces, so do not duplicate or describe coalescing as a complete fix.
- **Confirmed mechanism, not a fix:** upstream [PR #80870](https://github.com/NousResearch/hermes-agent/pull/80870) attributes multi-thread long frames to per-event subagent/tool-row store writes and React commits outside batching. It independently supports publication fan-out and adds instrumentation rather than remediation.
- **Adjacent:** [issue #72799](https://github.com/NousResearch/hermes-agent/issues/72799) shows adaptive flushing measures queue/store work rather than later React commit cost; [issue #50107](https://github.com/NousResearch/hermes-agent/issues/50107) supports stream-cadence pressure. Existing fork hidden-pane containment is also already present. None establishes this incident’s exact signature.
- **Search gaps:** no public artifact names “many subagents → `UpdateLayoutTree` → transcript/composer/pane starvation while nested code scrolling remains responsive”; no indexed Hermes report uses INP, `DroppedFrame`, or `PipelineReporter STATE_DROPPED` terminology for this class; no proven report isolates hover-style invalidation across the whole transcript; no browser-DOM geometric virtualization equivalent surfaced.

Public prior art therefore narrows—not replaces—the local RCA: visible mounted style-tree cost, hover/pointer amplification, and store/React subscription fan-out remain the unfixed production seams.

### Verification and boundaries

- Focused contracts after all validity fixes: Electron **10 passed**, UI **17 passed**.
- Direct coalescer regression proves delayed buffered progress cannot overwrite restored snapshots.
- Post-checkpoint cleanup hardening now cancels queued/coalesced work and then deletes the synthetic runtime from ContribWiring’s authoritative `SessionStateCache` before restoring snapshots. Current-tree inspection showed tile cleanup alone removed only the reactive `$sessionStates` mirror. This did not invalidate accepted measurements, which completed before reset and used unique runtime IDs, but it closes a real cross-run/cache-reconciliation risk. Focused bridge/probe/coalescer verification: **14 passed**.
- `git diff --check`: passed.
- Repeated successful refetch checks confirmed `HEAD == fork/fork-integration`, divergence `0 0` at the final runtime experiments.
- Full typecheck remains blocked by the pre-existing unrelated `use-session-actions.test.tsx` fixture missing required `discardQueuedStreamState`.
- No live profile, credentials, provider credits, live Desktop control, production renderer optimization, merge, release, installer, or updater action occurred.

## Next boundary

Stop before changing production renderer behavior. The next authorized decision is the RCA checkpoint: choose a narrow production seam to test (style containment/mounted subtree, store publication fan-out, or pointer/hover amplification) and add a regression against the minimized command. A draft PR can contain the isolated harness, observer, artifacts, and evidence without claiming a production fix.
