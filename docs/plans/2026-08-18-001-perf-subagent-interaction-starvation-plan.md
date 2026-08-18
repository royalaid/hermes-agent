# Desktop Subagent Interaction Starvation Investigation Plan

> **For Hermes:** Follow `diagnosing-bugs` and `software-development/systematic-debugging`. Build a red-capable loop before proposing or changing production renderer behavior.

**Goal:** On current `fork/fork-integration`, add an isolated real-Electron `subagent-fanout` performance scenario that injects mocked concurrent subagent, tool, and reasoning updates through the real renderer state path; reproduce and minimize pane-interaction starvation at or below eight workers without touching the live Desktop profile; establish an empirical baseline and stop with a minimized reproduction and root-cause evidence before any production renderer change.

**Architecture:** Extend the existing `apps/desktop/scripts/perf/` harness and its probe-only automation surfaces. Launch only through the existing isolated Electron path with a disposable `HERMES_HOME`, Electron user-data directory, app lock scope, and private CDP port. Inject deterministic typed events through the production renderer dispatcher/store path; use the mock gateway only if direct dispatcher injection cannot reproduce the symptom.

**Tech stack:** Electron, React, TypeScript, nanostores, Playwright/CDP, Chrome Performance APIs, the existing Desktop performance harness.

---

## Symptom contract

During a large concurrent subagent run, the renderer stopped responding promptly to:

- primary transcript scrolling;
- composer typing;
- session, tab, and pane switching.

The native window remained responsive. Nested code snippets could still scroll. Responsiveness returned after the major subagent run finished. Treat this as active-update pressure first; retained transcript state remains a separate axis.

## Initial evidence

The supplied redacted Chromium/Electron trace report records:

- 37.905 seconds on the primary `CrRendererMain`;
- 177 renderer `RunTask` spans at or above 50 ms, maximum 925.174 ms;
- 1,741 `DroppedFrame` markers;
- 1,201 `PipelineReporter` `STATE_DROPPED` events;
- 431 `UpdateLayoutTree` spans totaling 27,115.621 ms, p95 193.213 ms, maximum 235.014 ms;
- input-associated 925 ms and 842 ms tasks around click, pointer-up, and mouse-up;
- `GestureScrollUpdate` maximum 225.396 ms;
- no safe trace-level distinction between transcript and code-snippet scroll targets;
- CPU-profiler startup overhead of 763.478 ms, so timing baselines must run without profiling.

These facts rank style-tree work, pointer/hover amplification, and store/React fan-out as leads. They do not establish a root cause.

## Authority and base

- Repository source of truth: freshly fetched `fork/fork-integration`.
- Investigation branch: `perf/subagent-interaction-starvation`.
- Initial fetched base: `6f963921642519789ccb2ecf37e21a846153ed47`.
- Re-fetch before authoritative measurements because the aggregate branch moves frequently.
- A Windows case-collision exists under `contributors/emails/`; this worktree excludes that unrelated directory with sparse checkout so the investigation tree can remain clean. Do not modify or commit that directory.

## Safety boundary

Authorized now:

- isolated worktree and branch;
- investigation documents and machine-readable benchmark results;
- harness/scenario changes;
- tests, builds, isolated Electron runs, and visible isolated verification;
- commits, branch push, and a draft investigation PR after the harness and evidence are reviewable.

Not authorized in this phase:

- production renderer behavior changes;
- reuse or symlinking of live `HERMES_HOME`, Electron user data, sessions, or credentials;
- credit-consuming provider runs without a later explicit approval;
- merge/replay into `fork-integration`;
- release, blue launcher, updater, installer, or live Desktop restart.

## Feedback-loop contract

The final Phase 1 command will have this shape:

```bash
npm run perf -- subagent-fanout --spawn --prod \
  --workers 8 --turns <bounded> --updates <bounded> \
  --interval-ms <measured> --seed <fixed> \
  --json <result-path>
```

The command is red-capable only when it exercises the real renderer event/store path and detects the reported interaction starvation, not merely high CPU or generic slow mounting.

## Metrics before gates

Capture burst, steady-state, and two-second recovery windows separately:

- rAF frame-interval p50/p95/p99, average FPS, and worst one-second FPS;
- long-task count, total, and maximum;
- primary transcript scroll-to-paint latency;
- composer keystroke-to-paint latency;
- pane/session switch-to-paint latency;
- nested code-snippet scroll-to-paint latency as a control;
- dropped/partial/presented frame counts when available without heavy tracing;
- render and nanostore attribution;
- heap samples and bounded-run growth;
- final recovery after all workers become terminal.

The current display reports 60 Hz. A 16.7 ms frame budget is descriptive, not a hard gate until the current-head baseline and variance are measured. Higher refresh rates are stretch targets only on a system that reports them.

## Experiment design

Vary one axis at a time and preserve a fixed seed:

1. worker count: 1, 2, 4, 8; exceed 8 only if the observed shape stays green;
2. update interval and coalescing cadence;
3. event mix: start, progress, thinking, tool, reasoning, terminal;
4. retained and visible transcript size;
5. delegate/tool-card depth and payload size;
6. stationary pointer versus synthetic pointer/hover activity;
7. visible versus hidden keep-alive panes;
8. active-update window versus post-completion retained state.

Once a boundary is found, binary-search each load-bearing axis. The minimal reproduction is complete when removing any remaining axis or lowering it past the boundary makes the loop green across repeated runs.

## Phases

### Phase 0 — Current-head baseline

1. Verify the worktree is clean and still based on the fetched aggregate tip.
2. Run existing production-mode `stream`, `transcript`, and `render-churn` scenarios unchanged.
3. Save machine-readable results and exact commands.
4. Do not use `--cpuprofile` for baseline timing.

### Phase 1 — Red-capable scenario

1. Add a registry test that fails because `subagent-fanout` does not exist.
2. Add the smallest scenario registration and watch the test pass.
3. Add a probe API contract test that fails until typed subagent-event injection exists.
4. Implement the smallest probe-only dispatcher seam using the existing production event handler.
5. Exercise actual `$subagentsBySession`, composer-status derivation, delegate-card rendering, transcript tool parts, and terminal settlement.
6. Add interaction probes and deterministic workload configuration.
7. Run the isolated production Electron scenario at 1, 2, 4, and 8 workers.

### Phase 2 — Bound and minimize

1. Find the first workload that reproduces pane-interaction starvation.
2. Re-run it enough times to establish variance and reproduction rate.
3. Binary-search worker count, cadence, content depth, and pointer activity one axis at a time.
4. Capture a lightweight profile only after the timing window is known; compare its hotspot shape with the original trace.
5. Produce a ranked falsification matrix.

### Phase 3 — Prior-art reconciliation

Search public Hermes issues, PRs, discussions, and commits using locally evidenced terms. For each candidate, record exact symptom, proposed mechanism, landed state, and whether local results confirm, contradict, or leave it unresolved. Public reports remain leads, not ground truth.

### Phase 4 — Checkpoint

Stop before production behavior changes. Present:

- exact original and minimized reproduction commands;
- baseline and scaling matrix;
- local symptom match, including the code-scroll control;
- ranked hypotheses and falsification evidence;
- GitHub prior-art reconciliation;
- proposed narrow production boundary and regression seam.

Production changes require Royal's confirmation after this checkpoint.

## 2026-08-18 checkpoint status

Phase 1 is partially proven but not complete. The exact native gateway/reducer path, authoritative retained state, visible session-tile projection, delegate/status/code DOM, and transcript/composer/pane interactions are verified. No single clean W1 artifact contains all four interaction controls because horizontal code-scroll CDP delivery remains unstable when combined with the moving transcript viewport. The old seed-41 artifact and same-port overlapping runs are excluded.

Do not start Phase 2 scaling yet. Split the independently scrollable code-card control into its own strict control scenario/artifact instead of mutating code-card geometry inside the fanout scenario. Then run one clean core W1 and one independent code-control run, each on a fresh verified port pair with a unique result path. Scaling `1 → 2 → 4 → 8` may start only after both artifacts pass their own strict proof.

The explicit-port observer is runtime-validated end to end. `watch.mjs` attached only after the explicit CDP port exposed exactly one page target, emitted three redacted NDJSON aggregate samples with stable safe `{port,targetId,type}` identity, and used no input, navigation, profiler, or mutation commands. Schema validation found no content-derived or sensitive identity fields. The observer does not authorize implicit attachment to a live Desktop or a default CDP port.

### Harness validity gate — `deleg_3d4b9570`

A delayed read-only review of the earlier harness found four validity defects. Current-tree verification retained all four with one timing clarification. These are now fixed source-only and covered by focused tests:

1. **Concurrent cadence:** the old shared event-level timer kept aggregate rate constant as worker count grew. The workload now groups one event per worker into every cadence tick, so increasing workers increases same-window reducer/store pressure without extending lifecycle duration by the same factor.
2. **Reset isolation:** reset now calls the production `discardQueuedStreamState(runtimeId)` seam before restoring subagent/session snapshots. A direct `useMessageStream` fake-timer regression proves a delayed coalescer flush cannot reapply synthetic progress afterward.
3. **Timing origin:** the previous renderer clock began at preparation time, not inside the listener, but it lacked an independent host clock and could not decompose queue wait from post-event paint. Receipts now carry `hostMs`, `rendererWaitMs`, `paintMs`, and `rendererTotalMs`; required top-level interaction metrics use host dispatch-to-paint. Earlier 7.6–87 ms values are old-clock diagnostics, not accepted starvation latency.
4. **Invalid metrics:** required controls now throw on null/non-finite host receipts instead of formatting them as `0 ms`.

The watcher now fails closed unless exactly one page target exists on the explicit port and emits only safe target identity metadata. No title, URL, WebSocket URL, transcript content, tool payload, or credential data is emitted.

Focused receipts after this source-only gate:

- Electron contracts: 9 tests passed.
- UI workload/bridge/probe/coalescer contracts: 17 tests passed.
- Direct coalescer file: 11 tests passed.
- `git diff --check`: passed.
- Full typecheck remains blocked by the pre-existing unrelated `use-session-actions.test.tsx` fixture missing required `discardQueuedStreamState`.

## Final Phase 1/2 outcome

The reproduction and realtime-observer goal is complete. The final strict validity chain adds two requirements beyond the earlier checkpoint: interactions must begin with `fanoutActive=true`, and non-terminal gateway dispatch count must increase while the real input probes run.

Authoritative minimized command:

```bash
npm run perf -- subagent-fanout --spawn --prod \
  --workers 1 --turns 1 --updates 12 --interval-ms 33 --seed 113 \
  --port <verified-free-cdp-port> --dev-port <verified-free-dev-port> \
  --json <unique-result-path>
```

Artifact: `apps/desktop/scripts/perf/results/subagent-interaction-starvation/fanout-min-active-updates12-dispatch-progress-seed113-20260818-1001.json`.

The strict receipt proves `fanoutActive=true` at both interaction boundaries, non-terminal dispatch progress `8 → 10` during the three input probes, final dispatch count `14` with zero failures, real reducer/session/subagent state, rendered transcript/delegate/code/status/composer/pane surfaces, finite host and renderer timing, terminal release after the probes through the same production callback, and clean recovery.

Red signature: burst frame p95 `160.5 ms`; steady frame p95 `88.2 ms`; pane switch `134.7 ms`; transcript scroll `42.2 ms`; two burst and two steady long tasks; recovery frame p95 `18.3 ms` with zero long tasks.

A separately strict `code-scroll-control` artifact proves the responsive horizontal-code control on a real code card, and the identity-bearing observer artifact proves safe explicit-port realtime attachment. The fixed-seed worker sweep remains useful amplification evidence, while pre-dispatch-progress fanout artifacts are not authoritative active-update timing.

`updates=12` is the smallest harness-valid active-update case at the fixed 33 ms cadence and current setup floor. Lower values cannot guarantee a non-terminal dispatch advances during the probes; they must not be described as the application’s mathematical threshold.

### Public prior-art reconciliation

Read-only GitHub recon found no exact public report of the local signature. Fork commit [`31065f6`](https://github.com/royalaid/hermes-agent/commit/31065f624de0a9dd24b30ebf607ded54581680fc) coalesces non-terminal subagent/tool progress and is verified as an ancestor of measured `HEAD` `6f963921…`; the accepted reproduction therefore occurs with that mitigation already present. Upstream [PR #80870](https://github.com/NousResearch/hermes-agent/pull/80870) independently confirms per-event store-write/React-commit fan-out but instruments rather than fixes it. [Issue #72799](https://github.com/NousResearch/hermes-agent/issues/72799), [issue #50107](https://github.com/NousResearch/hermes-agent/issues/50107), and landed hidden-pane containment are adjacent cadence/containment evidence, not explanations of the exact incident.

Searches found no artifact naming many subagents plus `UpdateLayoutTree` plus transcript/composer/pane starvation with responsive nested code scrolling; no Hermes report using INP, `DroppedFrame`, or `PipelineReporter STATE_DROPPED` for this class; no proven hover-only whole-transcript style invalidation; and no browser-DOM geometric virtualization equivalent. Remaining production hypotheses stay ranked as visible mounted style-tree cost, store/React subscription fan-out, and pointer/hover amplification.

Post-checkpoint source review found one probe-reset isolation gap: tile cleanup removed the reactive `$sessionStates` projection but not ContribWiring’s authoritative `SessionStateCache` entry. The narrow probe-only cleanup now cancels queued/coalesced work and then deletes that synthetic runtime cache entry before snapshot restoration. Focused bridge/probe/coalescer tests pass **14/14**. Accepted performance artifacts remain valid because measurement completed before reset and each run used a unique runtime ID.

Stop before production renderer changes. The next boundary is a user/maintainer decision among narrow production hypotheses: mounted-subtree style containment, store/React publication fan-out, or pointer/hover amplification. No live profile access, provider credit, production optimization, merge, release, installer, updater, or live Desktop control occurred.

## Stop conditions

Stop and report rather than widening scope if:

- the harness cannot exercise the real subagent event/store path;
- deterministic mocks stay green through the observed eight-worker shape and a bounded higher-load probe;
- reproduction requires copying live state or spending provider credits;
- the worktree base moves and results would be stale;
- a proposed next step changes production renderer behavior;
- the live Desktop profile, running process, or credentials would be touched.

## Verification ladder

At the checkpoint, evidence must include:

1. exact fetched base and clean branch state;
2. existing harness health on current head;
3. deterministic scenario unit/contract tests;
4. isolated production Electron execution;
5. original and minimized workload results across repeated runs;
6. visible/DOM proof that the intended transcript, composer, pane, delegate-card, and code-scroll surfaces were exercised;
7. no production renderer behavior changes in the branch;
8. `npm test -- <affected tests>`, `npm run typecheck`, `npm run build`, and `git diff --check` results.
