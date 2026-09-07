# Fork integration rebase: 2026-09-06

## Purpose and sources

Rebase the published fork integration onto upstream, reconcile fork `main` to the resulting integration, and record the branches represented by the final tree. Preserve the upstream-shaped Windows updater changes as the canonical implementation.

The captured published integration was `762862b168d87c059f24eeed305bdbbbb71c3889`. The captured fork `main` was `a8c50eb1d841563eff22bd707d80472e7f1e9c9f`; it had no unique changes to reconcile. The local integration checkout at `756f3157942866b24d3f2cebe000cc9b7058e6ca` was stale and was preserved rather than replayed as a second source.

## Rebase findings

- Follow upstream's module extractions. Gateway behavior formerly in `gateway/run.py` now belongs in focused modules. Apply the behavior at its current owner and update tests to patch the symbol actually called at runtime.
- Preserve commit authorship and record the old-to-new commit mapping. A changed patch ID after a conflict does not establish that a feature was lost; inspect the behavior and document its disposition.
- Keep the smaller updater proposal canonical. Historical local Windows updater implementations are superseded, not additional features to restore after rebasing.
- Refresh merge assertions against the retained tree. Do not preserve a removed implementation solely to satisfy an obsolete string assertion. Empty assertions do not characterize a merge.
- The daily cron job imports its deployed sibling `refresh.py`. Editing the repository copy alone does not update that job. Validate and synchronize the deployed copy when the final integration bookkeeping is ready.

## Updater verification limits

The focused native process suites passed 58 tests. The canonical updater also passed focused Python and Electron checks and the reviewed relaunch tests.

Two attempted end-to-end updates on this host are **not valid isolated proof**. A separate checkout, virtual environment, `HERMES_HOME`, and Electron user-data directory did not isolate process discovery: `find_gateway_pids(all_profiles=True)` scans the host. Those attempts stopped the installed gateway. Further attempts were stopped, the test processes were closed, and the installed gateway was restarted through its existing `Hermes_Gateway` scheduled task at 16:49 PT.

The attempts also exposed a restart failure: replaying a Windows base Python process's command line bypasses its virtual-environment launcher and can fail to import dependencies (`rich` in this run). The automatic retry cold-started a gateway through the correct launcher. That recovery is not a clean-run result.

Do not count these attempts, or the historical run ledger for the older updater, toward the canonical updater's two-run cutover requirement. A truly isolated Windows environment and a pushed known-good rollback reference are still required before live updater cutover.

## Integration verification

- Electron and renderer TypeScript checks passed. The complete Desktop production build passed in the isolated checkout; this validation build was not installed or published.
- The 353 selected Desktop tests were reconciled: 351 passed initially, then the corrected background-sync expectations passed in a 62-test targeted rerun. Scoped ESLint passed.
- Skill lookup and guardrail tests: 75 passed.
- Continuation delivery, crash recovery, and attachment publication: 100 passed after passing the claimed event through upstream's extracted delivery helpers.
- Goal control and adjacent goal storage tests: 84 passed after restoring sorted parent/child cross-process locks around migration preparation, CAS publication, and authoritative read-back.
- Todo hydration and compression tests: 22 passed after stamping trusted snapshots at the extracted database-read boundary.
- TUI queue, orphan-race, and related server checks: 67 passed after removing a history-to-registry lock inversion and adopting queued prompts from dead transports.
- All merge and patch assertions match the final working tree. The fork-refresh tests passed within the initial 193-test integration run; failures in that run were isolated to goal-state tests and the subsequently corrected delivery helper.
- Native scanner and marker source files match the canonical updater PR, including its CI follow-up. The fork additionally retains its managed Windows shortcut refresh.

## Cron handoff

The repository registry now describes the retained modules and canonical updater, and the provenance JSON accounts for all 189 captured commits. Deploy this `refresh.py` beside the cron `job.py` when the integration is published. The currently deployed job consumes the still-published old branch, so replacing its assertions before publication would validate the wrong tree. The findings and completion note are saved alongside the deployed job and in its notepad; no maintenance job was executed to publish implicitly.

## Status

Rebase completed onto upstream `dec1e87833`. Canonical updater merge: `ea1ae5d8b8`. The [provenance file](2026-09-06-fork-integration-provenance.json) records all 189 captured commits and the four canonical updater commits. Local `fork-integration` and `main` are reconciled to the integration containing this record. Their prior tips remain under the `safety/*-20260906` refs. Findings are copied beside the deployed cron job and linked from its notepad. The deployed executable registry must be synchronized when this integration is published.

The canonical updater feature branch is published at `034aa2adec8097bff77fbdf6dd8a471b59faf041` as upstream [draft PR #104687](https://github.com/NousResearch/hermes-agent/pull/104687). Publication of fork `main` and `fork-integration`, and live updater cutover, remain pending two complete updater runs in a disposable Windows environment and a pushed known-good rollback reference. The PR remains a draft while that proof is outstanding.

The first upstream CI run exposed seven failing updater tests. Follow-up commit `034aa2adec` restores lazy process reads in both scanner copies, uses the canonical venv-layout helper and gateway lifecycle matcher, and reclaims impossible POSIX PIDs instead of treating them as unknown live owners. The six affected suites passed 227 tests on Windows through `scripts/run_tests.sh`; Ruff passed. The exact follow-up commit is retained in integration merge `453e075fb6`.

Upstream CI completed for `034aa2adec`: **25 checks passed; 12 skipped or neutral; none failed or pending**. Python, Windows, macOS, JS/TypeScript, Nix, both Docker builds, and the aggregate required-check gate passed. [Every check and its job URL](2026-09-06-updater-pr-ci.json) is recorded separately. Desktop E2E was skipped and does not count toward the two-run cutover requirement.

The updated maintenance registry passed its 29 tests and all 41 committed-tree assertions.
