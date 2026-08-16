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

As of **U2**, `sync.py` is the only path by which the operational copies at
`%HERMES_HOME%\scripts` may change: an automatic post-publish sync (right
after a successful release publish) and a provisional/break-glass
`sync.py deploy` for an in-window investigator or an authorized operator.
Operational copies are verified against this repo's committed git tree
(never against the sync stamp's own recorded hashes — the tree is
authoritative) by `hermes-integration-release-windows.py`'s run-start
integrity gate, which fails closed on a real run and reports (without
blocking) under `--dry-run`. See `sync.py`'s module docstring for the full
mechanics, the tracked-file-set decision (which files sync and why the
in-repo shims/README do not), and `python sync.py --help`.

Note this changes the "byte-pure, do not hand-edit" policy above only for
`sync.py`'s own two call sites inside
`hermes-integration-release-windows.py` (the run-start gate and the
post-publish hook) — everything else in that file, and every other
imported file, is still byte-pure per the U1 import commit.

## Running the tests

```
uv run python -m pytest tests/cron/test_fork_integration_release.py tests/cron/test_fork_integration_sync.py -q
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/tests/test-fork-integration-release.ps1
```

Both also run in CI as the `fork-integration` job in
`.github/workflows/installer-tests.yml`.
