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
post-publish hook), plus U6's park-and-continue changes described below —
everything else in that file, and every other imported file, is still
byte-pure per the U1 import commit.

## Reconciliation proposals (U6)

`proposals.py` owns upstream-churn reconciliation. When upstream carries a
same-subject, non-patch-equivalent rewrite of a pinned patch, the run no
longer refuses: it writes a hash-stamped proposal artifact under
`%HERMES_HOME%\review-artifacts\fork-integration-proposals\` (evidence:
candidate SHAs + patch-ids + author/committer/signature, the full candidate
diff in a sibling `.diff`, an interdiff summary, and a ready-to-apply
manifest fragment), records the parked pin, and **continues** with the pin's
last verified form, tagging output provenance
`pin-parked-pending-proposal:<id>`. Only a failed re-apply of that last-good
form aborts a run.

Operator commands (approval is interactive-only and is on the investigator's
forbidden list):

```
python proposals.py list
python proposals.py approve <id> --artifact-hash <sha256> [--lineage]
python proposals.py reject  <id> --reason "<why>"
```

`approve` re-verifies the candidate against current upstream, re-derives the
manifest edit and requires byte-equality with the stored fragment (else
`stale-invalidated`), edits `hermes-integration-manifest.json` in place
(surgically — the hand formatting is preserved), appends superseded
patch-ids to `fork-integration-blocklist.json`, commits both with the
approver/candidate/proposal recorded in the message, writes a
`refs/pinned/<pin>/<patch-id>` keep-ref for the outgoing commit, and prints
the `sync.py restamp-file` commands that carry the approved edit across the
U2 sync boundary. `--lineage` approves the lineage (subject + changed-file
set) rather than one SHA — the escape hatch for a context-drift re-land and
for the `churn_livelock` escalation after three regenerations.

Blocklisted patch-ids never count as equivalent and are never re-proposed.

## Investigator finish authority (U9)

When a run fails, `hermes-release-failure-investigator.py` records a
sanitized incident and — for the first failure with no live finisher —
**mints an authority record** at
`%HERMES_HOME%\cron\authority\<job>-<signature>.json` before spawning the
investigator session. The record carries the job id, the incident
signature, the session id (patched in after `session.create` returns),
`issued_at`, a **frozen** `expires_at` = `min(next scheduled fire, issue +
4h)`, and the allowed action set (`push`, `publish`).

`hermes-integration-release-windows.py --authority-token <path>` validates
it in code immediately before **each** privileged action: any `--holder`
other than `scheduler` must present a token that parses, names this job, is
unexpired at that instant, allows that action, and whose recomputed digest
matches the digest the spawner recorded in the still-open incident. Every
failure is a named refusal (`authority_token_expired`,
`authority_token_sha256_mismatch`, `authority_incident_closed`, …) that
lands in the result JSON as well as the log, and exits nonzero. The nightly
scheduler run is the sanctioned automated path and needs no token.

**Honest limit (R20).** Ambient same-user git/gh credentials mean these
file-based controls bound accidents and drift, not a fully hostile
in-context agent: nothing here stops a process running as this account from
passing `--holder scheduler` or writing its own incident record. What the
gate does buy is that an investigator following its contract cannot push or
publish outside a window a non-agent process opened for the incident it was
spawned for, and that a superseded or abandoned finisher loses its
authority the moment its incident closes. No HMAC is used — a shared secret
readable by the same account would be theater. The enforcement point
outside this host is GitHub branch protection on `origin/fork-integration`
(deferred, user-owned).

**Deviation from KTD5's wording**, recorded deliberately: KTD5 asks for a
monotonic timestamp. No monotonic clock is shared across the spawner
process, the investigator session, and the release process. The mechanism
that delivers KTD5's intent — a schedule edit after the spawn cannot extend
a live window — is the frozen wall-clock `expires_at`, computed once at
mint and never recomputed by a reader.

Incident state (`%HERMES_HOME%\cron\failure-investigators\<job>.json`) is
schema 2: entries carry `session_id`, `spawned_at`, `heartbeat_at`,
`token_sha256`, `token_expires_at` and a `closure` (`resolved`, `expired`,
`abandoned`, `superseded`); `open` holds only live incidents and closed ones
move to `closed`. Schema-1 files migrate on first write, closing any open
legacy incident `superseded`. At most one finisher runs per job per window:
a live finisher (heartbeat < 20 min) absorbs further failures, a stale one
is closed `abandoned` and replaced exactly once within the same window end.
The session beats with:

```
python hermes-release-failure-investigator.py heartbeat --job <id> --signature <sig>
```

Investigator sessions are created with `source="desktop"` and the title
`Release investigator · <job> · <sig8>`, so they appear in the Desktop
sidebar's recents; nightly `source="cron"` run sessions stay out of recents
as before. The `cron_session` marker is never persisted and nothing filters
on it — the visibility contract is keyed on the session's own source and
title (proved by the gateway-layer tests in
`tests/cron/test_fork_integration_investigator.py`).

## Running the tests

```
uv run python -m pytest tests/cron/ -k fork_integration -q
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/tests/test-fork-integration-release.ps1
```

Both also run in CI as the `fork-integration` job in
`.github/workflows/installer-tests.yml`.
