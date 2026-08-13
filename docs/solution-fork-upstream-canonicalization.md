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
   PR head and base SHAs. Normally, do not integrate the feature into the fork
   branch until the upstream merge SHA exists. The narrow exception is an
   explicitly approved, pinned unmerged foundation: record its PR, exact head
   SHA, approval, and reason, then use only that immutable head as the first
   integration input. The exception does not authorize newer PR heads, related
   PR variants, or other unmerged work.
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
discarding. The current integration manifest is authoritative for any explicitly
approved pinned unmerged foundation; at the time of writing it records #82832.
PR #84778 is downstream fork work, not that foundation. All other unmerged work
still follows the normal merge-wait rule unless it has its own explicit approval.
Use the manifest's exact approved head, patch identities, and the
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

## Integrated local fix record

The following fork-local fix was integrated from a clean worktree based on the
published `fork-integration` tip:

```yaml
- component: desktop-dropped-frames
  source_worktree: C:\Users\gwmai\git\hermes-agent\.worktrees\fix-desktop-dropped-frames
  source_branch: fix/desktop-dropped-frames
  source_head_sha: 4118ab25b3ab9977b731fee99702dc2f29780016
  integration_sha: 1d337bffceaeb7091a62c0255dcb5c98faf7cb4b
  status: integrated
  behavior: hidden-pane contain-intrinsic-size permanent + content-visibility toggle; reveal catch-up as a transition; ordered text|tool queue on one timer; flush-time sessionInterrupted re-check; terminal/approval events flush-then-apply
  tests:
    - "apps/desktop typecheck: passed"
    - "apps/desktop test:ui: 3895 passed; expected journal defect + 3 known load-flaky files (passed in isolation)"
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
paper over an unmerged upstream PR, a dirty worktree, a stale remote ref, or a
manifest mismatch.

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
- [ ] PR head/base and upstream merge SHA are recorded.
- [ ] Integration starts from the exact published fork-integration SHA.
- [ ] The upstream result is reachable by SHA and no duplicate fork patch was reimplemented.
- [ ] Manifest/tracking metadata and component scope validate.
- [ ] Native tests, build, checksum, and served-SHA checks pass.
- [ ] The readiness switch is explicit for every excluded component.
- [ ] The user receives the exact supported update path and artifact identity.
