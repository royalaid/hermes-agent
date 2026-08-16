# fork_integration

Byte-pure import of the operational Hermes fork-integration release system
(`hermes-integration-release-windows.py`, `hermes-release-failure-investigator.py`,
`hermes-integration-manifest.json`, `test_hermes_integration_release_windows.py`)
from `%USERPROFILE%\AppData\Local\hermes\scripts` into this repo, for review
and CI coverage.

## Import + shim policy

- The four imported files are **byte-identical** to their ops-directory
  source (verified by sha256 at import time; see the import commit body).
  `.gitattributes` in this directory forces `-text` on them so git never
  rewrites line endings on checkin/checkout.
- **Do not hand-edit the imported files.** Their original filenames are kept
  (not renamed to `release.py`/`investigator.py`) to avoid breaking
  cross-references from the live ops directory, which remains the
  operational copy.
- `release.py` and `investigator.py` are importlib shims: they load the
  dash-named scripts by path (not importable via a normal `import`
  statement) and re-export their module-level names. Use them as
  `from scripts.fork_integration.release import mod` /
  `from scripts.fork_integration.investigator import mod`.

## Sync contract (summary)

These are currently a **one-time snapshot**, not a live mirror — the
operational copies in the ops directory keep evolving independently.
Automated sync entry points (so this directory tracks ops changes, or so
reviewed changes flow back out) are **U2's** responsibility, not this unit's.

## Running the tests

```
uv run python -m pytest tests/cron/test_fork_integration_release.py -q
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/tests/test-fork-integration-release.ps1
```

Both also run in CI as the `fork-integration` job in
`.github/workflows/installer-tests.yml`.
