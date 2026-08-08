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

When the project’s manifest has a stricter schema, extend the existing schema
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

Use the release script’s dry-run before any mutation. It must not be used to
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
- Relying on `origin/HEAD` or stale tracking refs makes a successful rebase
  appear current when the published branch has moved.
- Matching commit subjects or branch names does not prove that the upstream
  result is present; use SHA ancestry and stable patch identity.
- A local build alone does not prove that the user-facing artifact is fresh;
  verify the public download and the installed/served version metadata.

## Acceptance checklist

- [ ] Original checkout changes and untracked evidence remain untouched.
- [ ] Upstream branch contains only generic, tested changes.
- [ ] PR head/base and upstream merge SHA are recorded.
- [ ] Integration starts from the exact published fork-integration SHA.
- [ ] The upstream result is reachable by SHA and no duplicate fork patch was
      reimplemented.
- [ ] Manifest/tracking metadata and component scope validate.
- [ ] Native tests, build, checksum, and served-SHA checks pass.
- [ ] The user receives the exact supported update path and artifact identity.
