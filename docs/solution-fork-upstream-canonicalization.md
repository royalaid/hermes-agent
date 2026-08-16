# Solution: fork work to upstream, canonical integration, and published update

## Problem

Fork work often starts in a checkout that already contains unrelated edits or
fork-only release wiring. Treating that checkout as the upstream PR source can
leak local changes into the PR, lose authorship during reimplementation, or
publish an artifact that does not correspond to the reviewed integration
commit.

## Durable pattern

Use four explicit boundaries:

1. **Feature boundary.** Create a clean worktree and branch from an explicit
   upstream SHA. Stage only the generic source, tests, and narrowly relevant
   documentation. Keep fork manifests, release scripts, and unrelated user
   edits out of the upstream branch.
2. **Upstream boundary.** Rebase onto the current upstream base, run the
   focused/native gates, and open the PR against that explicit base. Record the
   PR head and base SHAs. For ordinary fork work, wait for the upstream merge
   SHA before integrating. **Pinned-unmerged-foundation policy (in force):**
   an explicitly approved PR head may be integrated pinned to current upstream
   regardless of merge state — the approval, exact head SHA, and reason are
   recorded in the manifest, and every carried patch is verified by stable
   patch identity (`git patch-id --stable`), not by merge status. The pin does
   not travel: it does not authorize newer PR heads, related PR variants, or
   other unmerged work without their own explicit approval.
3. **Integration boundary.** The published `fork-integration` head is
   canonical. Reconstruct it from the explicit upstream `main` SHA, the pinned
   approved foundation when one exists, and the complete ordered set of fork PR
   branches or patches. Fetch explicit upstream and fork refs, verify ancestry
   and patch identity, and merge by commit provenance. Do not recreate an
   equivalent fork-only patch because a title or branch name looks similar.
4. **Release boundary.** Update the existing manifest or equivalent tracking
   record, validate the declared component scope, build from the exact
   integration head, and publish only through the sanctioned release path.
   Verify the public artifact checksum and the installed/served version's
   integration SHA before telling the user to update.

## Recorded canonicalization snapshot (historical)

```yaml
repository: royalaid/hermes-agent
integration_branch: fork-integration
upstream_repository: NousResearch/hermes-agent
upstream_main_sha: 423f92e607dd51908d23b04758bc0fcd6ec5ff39
fork_main_sha: a8c50eb1d841563eff22bd707d80472e7f1e9c9f
integration_base_sha: 423f92e607dd51908d23b04758bc0fcd6ec5ff39
integration_code_head_sha: 1d337bffceaeb7091a62c0255dcb5c98faf7cb4b
published_input_sha: 7d094ca310a6601271b8846ed291030cee2e3d6a
reconstructed_replay_head_sha: ba65ac08b396e5f268c2435bc086ab09e14ca35f
upstream_pr: https://github.com/NousResearch/hermes-agent/pull/84778
upstream_pr_head_sha: 45213d7f718ab4e32f0b4b6274a75a004e7604bd
status: rebuilt_pending_release_verification
```

These SHAs, PR states, and status values are historical audit snapshots, not
live scheduler or remote state. Resolve current refs and release state before
any new integration or update decision.

The fork's ordinary `main` is not the recorded upstream tip. The published
user-facing line is `fork-integration`, which is reconstructed from upstream
`main`, the approved pinned foundation when recorded, and the ordered fork
patch set. Scheduler-local integration state is disposable and reconstructible;
the published `fork-integration` head is the canonical recovery point.

## Upstream PR audit

The audit was refreshed against `NousResearch/hermes-agent` on 2026-08-13.
Closed PRs remain in-process for this fork and are not treated as disposable.

| PR | State | Upstream head | Fork-integration disposition |
|---:|---|---|---|
| [#84778](https://github.com/NousResearch/hermes-agent/pull/84778) | open/draft | `45213d7f718` | Windows force-drain handoff and the first-upgrade Hermes Shell identity repair are integrated. |
| [#80866](https://github.com/NousResearch/hermes-agent/pull/80866) | open/draft | `a38befcfa74f` | Equivalent diagnostics stack is integrated; do not duplicate the PR variant. |
| [#80867](https://github.com/NousResearch/hermes-agent/pull/80867) | open/draft | `69779ff8b2c9` | Gateway off-loop fix is integrated by equivalent provenance. |
| [#80870](https://github.com/NousResearch/hermes-agent/pull/80870) | open/draft | `d53bb4785545` | Multi-thread diagnostics work is represented; retain the PR as upstream-in-process. |
| [#80872](https://github.com/NousResearch/hermes-agent/pull/80872) | open/draft | `66469796469d` | Journal fix is integrated; stacked diagnostic parents are represented without duplication. |
| [#79727](https://github.com/NousResearch/hermes-agent/pull/79727) | open | `b516857eadfd` | Equivalent reasoning-identity work is present; exact upstream head remains in process. |
| [#79726](https://github.com/NousResearch/hermes-agent/pull/79726) | open | `9d9221855b06` | Windows PATH fix is integrated by equivalent provenance. |
| [#79725](https://github.com/NousResearch/hermes-agent/pull/79725) | open | `247152cf83fb` | Integrated as `5463a31d09`; PowerShell installer stage-protocol smoke test passed. |
| [#81544](https://github.com/NousResearch/hermes-agent/pull/81544) | closed, unmerged | `6c5e10a76937` | In process and excluded until the venv-quarantine readiness decision. |
| [#76815](https://github.com/NousResearch/hermes-agent/pull/76815) | closed, unmerged | `dd2f89085050` | In process/superseded; its PATH change is covered by #79726. |

An upstream PR being open or closed does not by itself authorize duplication or
discarding. The current integration manifest is authoritative for which pinned
unmerged foundations are approved — query it directly
(`hermes-integration-manifest.json` → `upstream_foundations[].pull_request` /
`.approved_head`, or run `ledger.py report`) rather than trusting a number
recorded here; at the time of writing it recorded PR #82832 (historical
example, not a live value). PR #84778 is downstream fork work, not a pinned
foundation. Every other unmerged item still requires its own explicit approval
before it may be pinned; absent that, it follows the ordinary merge-wait
default. Use the manifest's exact approved head, patch identities, and the
readiness/exclusion record above.

## 2026-08-14 rebuild provenance

Replay source was `git rev-list --reverse cc389f815..7d094ca31` (82 commits)
onto `upstream/main` `423f92e60`. `git cherry` of the old published tip against
the reconstructed head reported 79 exact patch-id matches and three `+` entries.
None of those three is the excluded stale pair `156931078` /
`f94cb5280`.

```yaml
semantic_or_context_replays:
  - source: b1563f6c9d358e42930d1a5e75aa655b951d05df
    subject: "fix(updater): coordinate native Windows process ownership"
    reason: content conflict with upstream #85539 node-deps repair on the already-current path
    resolution: kept both the fork completion_message assignment and _repair_node_deps_on_current_checkout
  - source: b1b989882a8c53b21a9f86f0ac0364b961997333
    subject: "fix(updater): harden native Windows lifecycle authority"
    reason: content conflict with upstream npx cache warm-up and the rewritten already-current path
    resolution: kept upstream warm-up plus the fork is-False lockfile check; took the incoming health-proof rewrite that supersedes the intermediate else-branch
  - source: 463636dea70f948ca008e560d6c5f6e296aef7e6
    subject: "fix(installer): preserve custom fork remotes"
    reason: context-only patch-id drift after upstream SkipComputerUse hunks moved nearby lines
    resolution: same added lines (REPO_URL_OVERRIDE / fork origin preservation); no behavior change
excluded_stale_pre_rebuild:
  - 156931078  # fix(updater): coordinate native Windows process ownership
  - f94cb5280  # feat(gateway): armed diagnostics ring (U3b)
```

## 2026-08-14 composer-viewport corrective integration

The published integration branch moved while the corrective rebuild was being
verified. The newer published tip was already based on the current upstream
and contained patch-equivalent versions of every commit on the source lineage
except the composer correction. The final reconstruction therefore starts at
that newer published tip and replays only the missing corrective commit.

```yaml
published_input_sha: 644efbd5decd10bfe3e0d6be5b9d818f48e2fb68
published_input_safety_ref: safety/fork-integration-published-input-before-composer-20260814-0030
source_branch: fix/desktop-dropped-frames
source_head_sha: 364ec0167c20483f6b8c0962cb932308e72592f2
source_commit_sha: 364ec0167c20483f6b8c0962cb932308e72592f2
integration_code_head_sha: 6a28e1aee98a7458987ff02314844c1a3d516f48
upstream_main_sha: 7a9634568cdeb8f5363bc99042a24ebff9df0e1c
verified_merge_base_sha: 7a9634568cdeb8f5363bc99042a24ebff9df0e1c
provenance: "git cherry matched every source-lineage patch except 364ec0167; replayed with cherry-pick -x"
status: rebuilt_verified_pending_publish
tests:
  - "apps/desktop typecheck: passed"
  - "apps/desktop targeted ESLint (src/app/chat/index.tsx): passed"
  - "apps/desktop focused pane Vitest: 12 passed"
  - "apps/desktop build: passed; build stamp 6a28e1aee98a"
  - "apps/desktop Electron cold-resume composer viewport E2E: passed"
```

## 2026-08-15 canonical Windows updater integration

The PR #84778 lineage was rebased onto the current upstream base and kept as a
focused safety branch. The ordinary Desktop update path was then reconciled
with the fork's authenticated handoff protocol: the upstream nested PowerShell
script owns the visible updater, while the staged Tauri installer remains only
in the separately bounded packaged bootstrap-recovery path. The temporary
`aac5cb605` direct-installer path is not an ancestor of the candidate.

```yaml
source_branch: fix/windows-updater-handoff
source_head_sha: c912255eeefde7707cf277bab75549b2057a2509
source_upstream_base_sha: 0d1828294ad85657ac945bc10186d0e02d33c4d7
integration_input_sha: 89b0cedaab9d65ffeafb59d9537229dd9e8f7ef4
integration_code_head_sha: 7ede2958e83eb8122dafc7a3512a3930fd62dcaf
rejected_direct_installer_sha: aac5cb6058656b7f5f5c07a2cbbda777f39b5401
rejected_direct_installer_is_ancestor: false
native_canary_sha: 903b78e6242fc7b11f0d8e12e48a067eced5f9af
native_canary_tree_sha: c7dcb18ee87e7361f874c85e0807de03c6d6b100
integration_code_tree_sha: c7dcb18ee87e7361f874c85e0807de03c6d6b100
status: native_verified
tests:
  - "Windows PowerShell 5.1 handoff contract: passed"
  - "PowerShell 7 handoff contract: passed"
  - "focused Python updater suite: 377 passed, 12 skipped"
  - "focused Electron updater suite: 249 passed"
  - "full Electron suite: 1414 passed, 2 skipped; 28 unrelated POSIX-host assumptions failed on Windows"
  - "Electron and E2E TypeScript checks: passed"
  - "updater-branch focused Electron suite: 55 passed"
native_proof:
  - "visible Settings > About > Update now flow launched scripts/desktop-update/windows.ps1"
  - "private receipt target/result SHA matched 903b78e6242fc7b11f0d8e12e48a067eced5f9af"
  - "critical syntax, imports, dependencies, and node dependencies were healthy"
  - "deferred gateway lease was adopted and the gateway fleet resumed"
  - "managed Desktop relaunched from the rebuilt exact target and acknowledged authenticated backend readiness"
```

The canary commit lives only in an isolated local fixture repository. Its tree
matches the integration code commit exactly; it is evidence, not an additional
integration input.

### Live-upstream refresh after the native canary

Upstream advanced after the proof above. Both the focused updater branch and
the full integration line were therefore rebased again before publication. The
canonical nested-script UX did not change in the new upstream commits. Their
new stale-Git-lock recovery and wedged-gateway liveness protections were kept,
with the fork's configured remote, scanner, process identity, lease, receipt,
and deferred-resume gates still authoritative.

```yaml
upstream_main_sha: 45af7a71fcd420b4422d2c074b1ce58b9ce0d048
source_branch: fix/windows-updater-handoff
source_head_sha: 339504f532e340fd1c69af50b4660007c5b5fc43
source_merge_base_sha: 45af7a71fcd420b4422d2c074b1ce58b9ce0d048
source_range_diff: "five exact patch matches from c912255e onto 45af7a71f"
integration_branch: fork-integration
integration_rebased_proof_record_sha: 9f8e13652e5aba80b0ec578ad8b7eec02b873523
integration_validation_head_sha: 4c700aa21db8b22b1d9216edfed5022e2bf6670f
integration_merge_base_sha: 45af7a71fcd420b4422d2c074b1ce58b9ce0d048
rejected_direct_installer_sha: aac5cb6058656b7f5f5c07a2cbbda777f39b5401
rejected_direct_installer_is_ancestor: false
status: ready_for_publication_native_gate
tests:
  - "Windows PowerShell 5.1 handoff contract: passed"
  - "PowerShell 7 handoff contract: passed"
  - "focused Python updater and scanner suites: 366 passed"
  - "focused Electron updater suite: 309 passed"
  - "updater-branch focused Electron suite: 59 passed"
  - "integration Electron and E2E TypeScript checks: passed"
  - "updater-branch Electron and E2E TypeScript checks: passed"
  - "integration ESLint: 0 errors"
  - "updater-branch ESLint: 0 errors"
native_publication_gate:
  - "use the visible Settings > About > Update now flow"
  - "receipt target_sha and resulting_head must equal the observed origin/fork-integration publication head"
  - "receipt health checks, deferred gateway resume, cleanup, exact Desktop relaunch, and authenticated backend readiness must all succeed"
```

## Integrated local fix record

The following fork-local fix was integrated from a clean worktree based on the
published `fork-integration` tip:

```yaml
- component: desktop-dropped-frames
  source_worktree: C:\Users\gwmai\git\hermes-agent\.worktrees\fix-desktop-dropped-frames
  source_branch: fix/desktop-dropped-frames
  source_head_sha: 364ec0167c20483f6b8c0962cb932308e72592f2
  integration_sha: 6a28e1aee98a7458987ff02314844c1a3d516f48
  status: integrated
  behavior: hidden-pane contain-intrinsic-size permanent + content-visibility toggle; reveal catch-up as a transition; ordered text|tool queue on one timer; flush-time sessionInterrupted re-check; terminal/approval events flush-then-apply; chat surface pinned to the pane viewport so a long transcript cannot displace the composer
  tests:
    - "apps/desktop typecheck: passed at 6a28e1aee98a"
    - "apps/desktop build: passed at 6a28e1aee98a"
    - "apps/desktop focused pane Vitest: 12 passed"
    - "apps/desktop Electron cold-resume composer viewport E2E: passed"
  open_item: "R2 scroll retention still needs a real-browser/electron e2e check on a deep scrolled-up transcript"
- component: desktop-session-history-pane
  source_worktree: C:\Users\gwmai\AppData\Local\hermes\worktrees\fix-session-history-pane
  source_branch: fix/desktop-session-history-pane
  source_head_sha: bb1f7e38ee0c9357db7cfaf7e60d576d2347b849
  integration_sha: cdc416a765d4f4e40cd7609e889d36541ad631e7
  status: integrated
  behavior: resume runtime first, then load the authoritative transcript; surface transcript failures for tile retry
  tests:
    - "apps/desktop focused Vitest: 4 passed"
    - "apps/desktop typecheck: passed"
```

## Readiness exclusions

These work items remain deliberately outside `fork-integration` until the
readiness switch is explicitly flipped and the listed gates are rerun:

```yaml
excluded_until_switch:
  - component: native-compaction-stack
    source_worktree: C:\Users\gwmai\AppData\Local\hermes\worktrees\fix-native-reasoning-summary-boundaries
    source_branch: fix/native-reasoning-summary-boundaries
    source_head_sha: e13fd35206df
    status: excluded_until_native_compaction_ready
    reason: in process; not ready for prime time
    activation_switch: native_compaction_ready
    required_before_integration:
      - explicit owner readiness approval
      - clean worktree and exact source SHA
      - conflict/provenance review against fork-integration
      - focused native-compaction tests
      - full integration build and updater verification
  - component: pending-upstream-pr-work
    source_worktree: C:\Users\gwmai\AppData\Local\hermes\worktrees\upstream-pr-work
    source_branch: upstream-pr/desktop-hitch-diagnostics
    source_head_sha: a38befcfa74f
    status: excluded_until_upstream_merge_sha
  - component: upstream-venv-quarantine-pr
    source: https://github.com/NousResearch/hermes-agent/pull/81544
    source_head_sha: 6c5e10a76937
    status: excluded_until_in_process_decision
    reason: closed without merge; retained as in-process per operator policy
  - component: reasoning-duration-investigations
    source_worktrees:
      - C:\Users\gwmai\AppData\Local\hermes\worktrees\reasoning-duration-opus-20260807
      - C:\Users\gwmai\AppData\Local\hermes\worktrees\reasoning-duration-sol-20260807
    status: excluded_while_dirty_or_investigatory
```

Do not integrate an excluded component because its directory exists. Flip the
named readiness switch only after the required gates pass, then update this
record with the new source, merge, and verification SHAs.

## Provenance record

Record at least:

```yaml
upstream:
  repository: "<owner/repository>"
  pr: "<number-or-url>"
  merge_sha: "<upstream-merge-sha>"
fork:
  integration_branch: "fork-integration"
  base_sha: "<published-base-sha>"
  integration_sha: "<tested-head-sha>"
feature:
  commit_sha: "<feature-or-upstream-commit-sha>"
  patch_id: "<stable-content-identity-when-used>"
verification:
  merge_base: "<verified-merge-base>"
  tests: ["<command and result>"]
  artifact_sha256: "<published-artifact-sha256>"
  served_sha: "<version-manifest-or-update-sha>"
  status: "integrated|blocked|rolled_back"
```

When the project's manifest has a stricter schema, extend the existing schema
or add a documented companion record rather than silently adding fields that
the release validator rejects. Preserve existing component identities and
stable patch IDs.

## Hermes release-path gates

The Windows integration release automation is intentionally fail-closed. Its
verified sequence is:

- require a clean `fork-integration` worktree whose local HEAD equals the
  published fork branch;
- fetch upstream and fork refs and validate manifest sources and stable patch
  identities;
- reconstruct the integration from the configured upstream ref plus the
  complete ordered patch set;
- validate required subjects and approved paths;
- run diff, typecheck, native bootstrap tests, and the pinned build;
- embed repository, branch, and integration SHA in the build;
- publish the launcher with `PROVENANCE.json` and `SHA256SUMS.txt`;
- verify the public download checksum and retain the documented release count.

Use the release script's dry-run before any mutation. It must not be used to
paper over a dirty worktree, a stale remote ref, an integration-scripts
integrity-check failure, or a manifest mismatch. A pinned-unmerged foundation
is sanctioned policy (see above), not a condition dry-run is hiding.

## Automated release system (`scripts/fork_integration/`)

The nightly job automating the four boundaries above lives in this repository
at `scripts/fork_integration/` — the system's source of truth, versioned and
tested in CI (`.github/workflows/installer-tests.yml`). `%HERMES_HOME%\scripts`
holds only operational deployments produced by the sync boundary below; it is
never hand-edited. `scripts/fork_integration/README.md` is the operator
quickstart for running and approving it; this section is the durable policy
record behind it.

### (a) Reconciliation proposals & park-and-continue

Upstream rewrites the same logical change repeatedly; `proposals.py` turns
that into a state machine instead of a hard nightly refusal:
`generated -> pending-approval -> approved | rejected | stale-invalidated`,
where `stale-invalidated` regenerates back to `generated` with fresh
evidence. A candidate is eligible only when its subject line exactly equals
the pinned patch's subject (never a substring or body match), it is an
ancestor of the upstream tip fetched that run, it is not a `Revert "..."`,
and its patch-id is not blocklisted; author, committer, and signature state
are recorded with it. A rejected or superseded candidate's patch-id is
appended to the blocklist (`fork-integration-blocklist.json`) and can never
be re-proposed or silently re-absorbed. `refs/pinned/<pin>/<patch-id>`
keep-refs protect evidence from garbage collection. Approval is attributable
(approver identity, timestamp, candidate SHA/patch-id recorded in the
manifest commit message), interactive-channel only, re-verifies the
candidate is still upstream's current form, and re-derives the manifest edit,
requiring byte-equality with the stored artifact; `--lineage` approves by
subject plus changed-file set instead of one SHA for a context-drift re-land,
and three stale-invalidations escalate to `churn-livelock`. Churn alone never
skips a nightly: park-and-continue re-applies the pin's last verified form
and tags output provenance `pin-parked-pending-proposal:<id>`; only a failed
re-apply of that last-good form aborts the run.

### (b) Sync boundary

Operational copies at `%HERMES_HOME%\scripts` change through exactly two
attributable entry points: automatic post-publish sync of the exact
published `fork-integration` SHA, and a provisional/break-glass
`sync.py deploy --from-sha <SHA> --reason <text>` for an authorized operator
or an in-window investigator, stamped `provisional: true` and re-stamped by
the next successful publish. Never mid-run, never on a tick, never a direct
edit. Verification is tree-authoritative: the sync stamp only names *which*
SHA to check; `verify()` always re-reads the committed git tree's blob
hashes, so a file and its stamp edited consistently out of band still fail.
Deployment is a staged atomic swap: every tracked file is staged to a
sibling temp directory, re-hashed after the write, then committed with one
`os.replace` per file, in order; the stamp is written strictly last as the
single commit point, so an interrupted swap leaves the prior generation
intact and provably stale rather than silently torn. The run-start integrity
gate runs before the exclusive lock and before any fetch; a mismatch permits
exactly one action, `sync.py deploy` — never a bypass flag — and a dry-run
folds the mismatch into its report instead of aborting. `sync.py
restamp-file`/`restamp-manifest` re-stamps one approval-mutated tracked file
in place, and only when its current on-disk content byte-equals the approved
hash.

### (c) Run lifecycle & loud failures

The release script's exclusive lock now records holder identity, pid, and
start time; a second entrant gets an explicit "busy, held by X (pid P) since
T" refusal instead of a silent wait, and a dead-pid lock older than the
stale window (6 hours) is reclaimed with the reclaim logged. Scheduler,
investigator, and manual runs all acquire this one lock — no second lease.
An execution reclassified `unknown` is delivered as "unconfirmed", never
"failed", and the reap pass corrects `jobs.json` `last_status` synchronously
in the same pass rather than leaving a stale-green status for hours. A
completion that arrives after `unknown` writes a sibling late-outcome record
instead of overwriting the original `unknown` audit row, and that late
outcome is delivered too. An out-of-process dead-man's switch — independent
of the Hermes Python environment, so it survives the exact failure mode it
watches for — alarms only when the job is overdue *and* the scheduler's own
ticker heartbeat is stale; an overdue job with a fresh heartbeat is read as a
long healthy run, not a dead scheduler. Delivery volume stays bounded: one
aggregated run-summary delivery per run, deliver-on-transition semantics per
class, and an explicit escalation when a condition persists unchanged across
runs rather than repeating the same notice forever.

### (d) Investigator authority

**Status: landed (U9; see the `feat(fork-integration): spawner-minted authority
tokens gate push/publish` and `feat(fork-integration): incident schema v2 -
heartbeat, closures, single finisher` commits).** The design below is the
contract `scripts/fork_integration/investigator.py` and the release script's
push/publish paths enforce in code. Authority is enforced by the
push/publish code path, not by goal text: a spawner-minted, expiring token
(job id, incident signature, session id, frozen expiry, allowed action set)
is required at every privileged call, and a forged, expired, or absent token
is refused in code. The authority window is `min(next scheduled fire of the
job, spawn + 4h hard cap)`, computed from a monotonic clock at spawn and
frozen into the record; an absent or unresolvable next fire means immediate
expiry, a schedule edit after spawn cannot extend a live window, and
remaining authority is re-checked immediately before each privileged action.
One active finisher runs per job per window; a stale-heartbeat or dead
finisher closes its incident `abandoned` and permits exactly one replacement
in the remaining window. Forbidden regardless of token: installer execution,
cron mutation, credentials, gateway restarts, and proposal approval (always
human, interactive-channel only). Honest limit: ambient same-user git/gh
credentials mean this token gate bounds accidents and drift in code, not a
fully hostile in-context agent; branch protection on `origin/fork-integration`
is the outstanding enforcement point outside this host (operational note,
user-owned).

### (e) Provenance

`ledger.py` answers "did carried change X get merged upstream, and in what
form?" derived from ground truth on every run — the manifest's declared
patch identities, live git ancestry/patch-identity search against the
tracked upstream ref, and a best-effort `gh pr view` for components sourced
from a named fork branch — never from a cached or trusted prior verdict.
States: `private-only`, `pr-open`, `absorbed-verbatim`, `absorbed-modified`,
`superseded`, plus the manifest's `excluded_until_*` reason codes.
Persistence is an append-only JSONL history, one record per run, one line per
carried patch; nothing ever prunes a lineage's current state, so `ledger.py
report` can still answer for a change untouched for months. Report, not
gate: the report renders the operator-facing evidence table and flags state
transitions since the previous run, but it never blocks a release — the
manifest validators in the release script remain the sole enforcement layer.
A pin absorbed verbatim for several consecutive runs, still an ancestor of
the live upstream tip, is surfaced as a retirement candidate for a
human-approved retire-pin proposal through (a)'s state machine — the
manifest shrinks by the same mechanism it grows, never by auto-mutation.

## Witnessed verification sequence

An operator can prove the whole loop using only the commands named here, with
no plan document required:

- **Forced-failure canary:** run `hermes-integration-release-windows.py
  --canary-manifest <path>` (a manifest injected outside the sync boundary's
  tracked set on purpose) to trip a real gate failure and witness the
  aggregated delivery plus the visible investigator session.
- **Killed-owner reap:** hold the exclusive lock as a foreground run, kill it,
  then trigger a scheduler reap pass to witness the busy-refusal/stale-reclaim
  path and the "unconfirmed" delivery with its late-outcome record.
- **Overdue alarm:** run `overdue_check.py` (`--dry-run` first, then live)
  against a paused scheduler to witness the alarm firing only once the job is
  overdue *and* the ticker heartbeat is stale.
- **Green nightly:** run `hermes-integration-release-windows.py` with no
  `--dry-run` and no injected failure to prove the loop still completes and
  publishes end to end.

### 2026-08-16 witnessed canary (historical audit snapshot, append-only)

```yaml
witness:
  deploy:
    kind: provisional_break_glass
    source_sha: 32ea9a80e8b9e34053d188ea2cf156b35b81e886
    verified: tree-authoritative, all 8 tracked files, independent + run-start gate
    reachability: runtime clone lacked the unpushed sha (sync_integrity
      unreachable_sha on first dry-run); fixed by local ref fetch and, structurally,
      by deploy --runtime-repo (committed same day)
  forced_failure_canary:
    stages: [integrity_gate ok, fetch ok, verify_manifest ok(real), verify_manifest
      fail(canary pin unavailable), transaction restored]
    incident: 575cf32953fcf07a530bb0ce (schema v2; session_id + token digest recorded)
    investigator_session: 71fb5010 (spawned live)
    authority_token: allowed [push, publish]; expiry computed to the next scheduled
      fire (02:00 PT) per min(next-fire, spawn+4h); revoked by operator after the
      witness was captured
    provenance: derived on the failure path; JSONL history appended; 39 pins mapped
      (journal fix absorbed-modified; gateway-config-offloop absorbed-verbatim)
  overdue_check: healthy verdict, on schedule, read-only
  outstanding:
    - Discord delivery witness rides the next cron-fired run (this canary was manual)
    - killed-owner reap live drill optional (mechanism covered by tests)
    - known nits: duplicate transition entry for one foundation pin in the first
      derivation; history entries do not yet carry the canary flag
```

## Rebase subagent contract

A Terra Medium regular-mode subagent may inspect or rebase only an explicitly
assigned isolated worktree. Give it exact source/target refs, allowed paths,
and required tests. Require a report of status, ahead/behind, merge-base,
commit/patch comparison, conflicts, resulting SHAs, and test outcomes.

The subagent must not publish, push, edit integration manifests, clear locks,
delete worktrees, or resolve conflicts outside the assigned allowlist. The
integrator owns provenance updates, conflict policy, release decisions, and
cleanup. Abort and preserve state when the base is ambiguous, the worktree is
dirty, provenance is missing, or a required gate fails.

## What failed before

- Using a dirty installed checkout risks merging user-owned edits into the
  upstream branch.
- Assuming the current projectless directory contains the repo misses the
  Hermes runtime worktree series.
- Relying on `origin/HEAD` or stale tracking refs makes a successful rebase
  appear current when the published branch has moved.
- Matching commit subjects or branch names does not prove that the upstream
  result is present; use SHA ancestry and stable patch identity.
- A local build alone does not prove that the user-facing artifact is fresh;
  verify the public download and the installed/served version metadata.

## Acceptance checklist

- [ ] Original checkout changes, dirty worktrees, and excluded investigations remain untouched.
- [ ] Upstream branch contains only generic, tested changes.
- [ ] PR head/base SHAs are recorded; the upstream merge SHA is recorded once
      merged, or the approved pinned head is recorded per the
      pinned-unmerged-foundation policy when it is not.
- [ ] Integration starts from the exact published fork-integration SHA.
- [ ] The upstream result is reachable by SHA and no duplicate fork patch was reimplemented.
- [ ] Manifest/tracking metadata and component scope validate.
- [ ] Native tests, build, checksum, and served-SHA checks pass.
- [ ] The readiness switch is explicit for every excluded component.
- [ ] The user receives the exact supported update path and artifact identity.
