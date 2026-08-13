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
   PR head and base SHAs. Do not integrate the feature into the fork branch
   until the upstream merge SHA exists.
3. **Integration boundary.** Start from the exact published
   `fork-integration` SHA. Fetch explicit upstream and fork refs, verify
   ancestry and patch identity, and merge the upstream result by commit
   provenance. Do not recreate an equivalent fork-only patch because a title or
   branch name looks similar.
4. **Release boundary.** Update the existing manifest or equivalent tracking
   record, validate the declared component scope, build from the exact
   integration head, and publish only through the sanctioned release path.
   Verify the public artifact checksum and the installed/served version's
   integration SHA before telling the user to update.

## Current canonicalization record

```yaml
repository: royalaid/hermes-agent
integration_branch: fork-integration
upstream_repository: NousResearch/hermes-agent
upstream_main_sha: cc389f81550180ad81bfddaa8b40dd0338d13c6d
fork_main_sha: a8c50eb1d841563eff22bd707d80472e7f1e9c9f
integration_base_sha: cc389f81550180ad81bfddaa8b40dd0338d13c6d
integration_code_head_sha: 27b83dc8fcfee7ae437c614148a389542aa98e2a
upstream_pr: https://github.com/NousResearch/hermes-agent/pull/84778
upstream_pr_head_sha: 45213d7f718ab4e32f0b4b6274a75a004e7604bd
status: rebuilt_pending_release_verification
```

The fork's ordinary `main` is not the current upstream tip. The published
user-facing line is `fork-integration`, which is rebased onto upstream
`upstream/main`.

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
discarding. Use the PR head SHA, patch identity, and the readiness/exclusion
record above.

## Integrated local fix record

The following fork-local fix was integrated from a clean worktree based on the
published `fork-integration` tip:

```yaml
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
