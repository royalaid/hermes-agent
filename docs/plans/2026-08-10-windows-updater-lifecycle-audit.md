# Native-Windows updater lifecycle audit

Date: 2026-08-10
Author: Royalaid, Hermes Agent
Status: evidence-backed design and acceptance record
Scope: only native-Windows evidence and results are authoritative; WSL
evidence and behavior are excluded

## Executive conclusion

The recurring failure is a lifecycle-coordination defect, not one isolated
installer bug. A Hermes update can involve Desktop, its managed `serve`
backend, gateways, supervised children, Codex-owned Hermes-tools MCP bridges,
the detached update hand-off, the managed virtual environment, and a custom
fork release pipeline. The existing paths do not all make one truthful
transaction from "which process owns this install?" through "the update is
complete and the new build is running."

The retained native-Windows evidence contains at least 13 distinct incident
windows from June 15 through August 9. Within the relevant Desktop evidence,
29 `venv-blocked` records map to 28 update attempts; every one of 15 staged
hand-offs reported success even though a holder remained; one scan reported as
many as 50 blockers. Five of six scheduled release runs from August 5 through
August 10 failed. These are lower-bound observations from the retained corpus,
not estimates of all affected updates.

The safe repair is a narrow CLI contract plus lifecycle fixes in the existing
updater surfaces:

1. Preflight the exact managed install and return versioned JSON.
2. Fail closed for unknown, Desktop, unsupported-harness, or otherwise generic
   holders.
3. Permit bridge interruption only for an exact MCP set attributed to a
   supported Codex or Claude harness and only after explicit user
   authorization, whether it occurs in a standalone pause or atomic update.
4. Keep an updater-owned quiesce lease across the full mutation window and
   prove two clear scans across the respawn interval.
5. Run the ordinary updater, then require a receipt/ref and readiness
   post-check before claiming success.
6. Repair fork provenance so absorbed, retained, regrouped, and unpublished
   patches are represented by commit and stable patch identity rather than
   subject text.

## Evidence boundaries and method

### Evidence classes used

- Retained native-Windows Desktop updater logs and hand-off results.
- Hermes session records that group repeated log lines into user-visible
  update attempts and incident windows.
- Native process-scan evidence with sensitive command-line values redacted.
- Scheduled release run records for August 5-10.
- The local git object graph and stable patch-id comparisons for the upstream
  and published fork snapshots.
- Current source inspection at the observed upstream snapshot, used to verify
  causal seams and distinguish a display-classification bug from a process
  termination bug.
- The legacy local fork-integration manifest, referenced below through its
  environment-neutral `%LOCALAPPDATA%` path, and the repo-owned v2 manifest
  used as the publication target.

Counts were deduplicated at the attempt or incident-window level where stated.
The document does not reproduce raw process command lines, environment values,
tokens, user prompts, PIDs, or large log payloads.

### Corpus reproducibility anchors

The retained logs are mutable, so these SHA-256 values bind the files used for
this observed snapshot. They identify evidence without publishing its sensitive
payload:

The machine-readable, sanitized selection ledger is
[`docs/plans/evidence/2026-08-10-windows-updater-lifecycle.json`](evidence/2026-08-10-windows-updater-lifecycle.json).
It records the snapshot times, log hashes, deduplication rules, qualitative
database queries, incident-window IDs, derived counts, redacted live-process
aggregate, and repair-chain identities. Once committed, that file's Git blob
anchors the selection rules; the source-log hashes continue to anchor the
mutable primary evidence.

| Artifact | Snapshot SHA-256 | Evidence used |
|---|---|---|
| `bootstrap-installer.log` | `44246FA64F4B0591BFF40C7CC876B17B1CF125002B1792E3CF17EA726D855D48` | Bootstrap hand-off and recovery stages |
| `desktop.log` | `7B0157420636F406A7419696226BDB8C73C12885DF760C0C2B7284F3BBB10FE5` | Current Desktop update stages and blocker records |
| `desktop.log.1` | `41EB40B4CEF0000A71A54D3B0A94A013D01D4D593943C9A47E9995EA583DA5A0` | Rotated Desktop history |
| `update.log` | `6FED4E496458EA9B59A38FC25A7B5394D5A414AF0F039272BBD417555D69518F` | Updater terminal states and venv refusals |
| `integration-release.log` | `77E4F5467F242ACBBB40678EC1FDB0C54AFFFC7DADB67D5BBAA179597FD803B4` | Scheduled fork-release terminal states |

The lower-bound incident windows are: June 15; June 16-17; June 29; July 10;
July 20; July 29 at 19:11; July 29 at 23:31 through July 30; July 31; August 3;
August 6; August 7; August 8; and August 9. These are 13 windows, not 13 raw
log lines.

The audit ledger was reconstructed from the two hashed Desktop log snapshots
and cross-checked against retained session rows. It counted one update attempt
per contiguous staged-update sequence, folded the repeated `venv-blocked`
emission within one sequence, and joined a hand-off initiation only to its
subsequent holder result. Scheduled release counts use one terminal state per
scheduled run in the August 5-10 window. This produces 29 blocker records / 28
attempts, 15 qualifying hand-offs, and six scheduled runs without adding
duplicate routed log records. Session identifiers and query parameters are
intentionally omitted. The sanitized ledger is reproducibility metadata, not a
replacement for the hashed primary logs.

### Facts, inferences, and targets

- **Verified fact** means the retained corpus, git object data, or current
  source directly supports the statement.
- **Causal inference** means multiple verified facts form the most direct
  explanation, but the statement is not itself a raw log record.
- **Implementation target** describes behavior that must be added or preserved;
  it is not a claim that the observed snapshot already implements it.

## Observed ref snapshot (not authority)

Observation time: `2026-08-10T11:38:54-07:00`.

| Identity | Observed SHA | Subject |
|---|---|---|
| Upstream `main` | `e5bc6b21868efad57414b1d28abbbb5ce26765c9` | `fix(attribution): correct AI_AGENT id to registry value and carry harness markers into all terminal backends` |
| Published fork-integration | `adafd77fa07f68dcaf58d9b28466c0bdd4cb115c` | `fix(bootstrap): pin installer repository with build ref` |

At this observation point, 23 commits were reachable from the published fork
snapshot and not from the upstream snapshot. Their merge base was
`8359e760be499fd8e804242e7606d81dde931abb`.

These refs move. Branch names, ahead/behind counts, process owners, and remote
tips must be re-read for any later release or recovery. This table is an
observed snapshot for audit reproducibility, not an authority for what should
be installed, replayed, or published.

## Verified incident findings

| Finding | Verified observation | Interpretation |
|---|---|---|
| Incident recurrence | At least 13 incident windows, June 15-August 9 | The issue survived several independent fixes and releases. |
| Desktop blocking | 29 `venv-blocked` records across 28 attempts | One attempt emitted the condition twice; raw lines must not be counted as unique updates. |
| False-positive hand-off completion | 15 of 15 staged hand-offs said success while holders remained | Hand-off success described launch/exit initiation, not verified install release or update completion. |
| Blocker fan-out | A single scan reported up to 50 blockers | Remediation must handle process trees and respawn, not assume one stale PID. |
| Role mislabel | `mcp_server` matched the raw substring `serve` | The affected display path mislabeled a Codex MCP bridge as a Desktop backend. |
| Live ownership | The supplied live scan attributed the active Hermes-tools bridge to Codex | Ownership is available and useful, but it is ephemeral and must be re-proven for each action. |
| Scheduled release reliability | Five of six scheduled releases failed during August 5-10 | Update safety and fork publication were both unreliable in the same operating window. |
| Manifest coverage | A 13-commit repair chain was absent from the schema-1 integration manifest | The published branch could contain operational repairs the release ledger could not explain or reproduce. |
| Native compaction duplication | Upstream native compaction and a fork replay share stable patch-id prefix `5eb223de` | Replaying both duplicates behavior even when commit SHA and subject differ. |

### Direct, sanitized log/session evidence

The following short excerpts are sufficient to identify the event classes
without exposing command lines or user data:

```text
[updates] venv-blocked: <count> process(es) hold the install
[updates] launched repo hand-off script ...; exiting desktop to release venv shim
[bootstrap] handed off ... recovery to updater ...; exiting desktop to release app.asar
```

The Desktop path returned a successful hand-off result in the observed staged
cases. That result establishes that a detached updater was launched; it does
not establish that every holder exited, the virtual environment mutated
successfully, the intended ref was installed, or the new Desktop build
relaunched.

The session ledger joins those event classes into 28 attempts and 15 staged
hand-offs. The sanitized live process scan at
`2026-08-10T12:22:51.6531063-07:00` found 38 exact MCP-module processes: 19
target-venv wrappers and 19 other-base-Python workers. Direct parent names were
the set `{codex, python}`, owner ancestors were `{chatgpt, codex}`, the
sanitized owner classification was Codex, and no Hermes Desktop process was
present. PIDs and raw command lines are deliberately omitted. The literal
`mcp_server` text belongs to the configured module name that triggered the
false substring match, not to the new structured role contract. The live scan
is retained only as the aggregate in the sanitized evidence ledger because its
raw form contained ephemeral process identity and command-line data. The
ownership statement is a point-in-time observation, not reusable
authorization; every interrupting action must re-prove it.

The scheduled-release ledger contains six terminal run states between August 5
and August 10: five failure states and one success state. This aggregate proves
the release reliability finding; it does not imply that all five failures had
the same cause.

### Direct source evidence at the upstream snapshot

Current source confirms the role-label error precisely:

- `hermes_cli/update_cmd.py` builds a display hint with a raw
  `"serve" in cmdline` check.
- Codex is configured to launch
  `agent.transports.hermes_tools_mcp_server`.
- Therefore `mcp_server` satisfies the substring check and receives the
  Desktop-backend hint even though it is a distinct Codex-owned MCP process.

This is a display/diagnosis bug. The later orphan-backend classifier is more
constrained: it also requires the `hermes_cli.main` module and a token-like
` serve` or ` dashboard` shape. The audit does not claim that the generic
orphan reaper terminates MCP bridges merely because the display hint is wrong.

Current source also confirms the Desktop seam:

- Normal in-app update currently waits for `releaseBackendLockForUpdate()` to
  report unlocked and performs one venv-blocker scan before returning
  `{ok: true, handedOff: true}`.
- Bootstrap recovery awaits `releaseBackendLockForUpdate(updateRoot)` but
  ignores its `unlocked` result, performs no equivalent blocker scan, launches
  the updater, logs the hand-off, and returns success.
- A one-scan normal path still has a respawn race; a no-check recovery path has
  a larger fail-open gap.

## Causal chains

### 1. Holder-to-half-update chain

1. Desktop, a gateway, a supervised descendant, or an MCP bridge maps the
   managed runtime or one of its native extensions.
2. The update path releases only the holders it knows it owns, or checks only
   one process-table instant.
3. An external or respawned holder remains after the hand-off is labeled
   successful.
4. The detached updater reaches virtual-environment mutation with locked files,
   or its downstream guard refuses with `venv-blocked`.
5. The user sees an update loop, an old Desktop relaunch, or a partially
   updated environment even though the hand-off itself returned success.

The retained counts and the normal/recovery seam support this chain. They do
not justify indiscriminate process termination.

### 2. Misclassification-to-wrong-guidance chain

1. The exact MCP bridge module ends in `hermes_tools_mcp_server`.
2. A display classifier looks for `serve` anywhere in the command-line string.
3. `mcp_server` matches and is described as a Desktop `serve` backend.
4. The user is told to close Desktop even when Codex owns the live bridge.
5. Closing or restarting Desktop cannot guarantee that Codex releases or stops
   respawning its MCP child, so the next attempt remains blocked.

The remedy is exact argv/module classification plus owner/role attribution,
not another broader substring.

### 3. Hand-off-to-false-success chain

1. Electron successfully spawns the repo-owned PowerShell updater.
2. Electron sets its quit intent and returns `handedOff=true`.
3. The staged script or bootstrap path encounters a holder, build failure,
   stale lease, or relaunch failure later.
4. The initiation result is surfaced as if it were completion.

The upstream hand-off result file and truthful-exit hardening improve this
chain, but every initiating surface still has to enforce the same release,
scan, lease, receipt, and post-check contract.

### 4. Fork-manifest-to-release-drift chain

1. Integration work is replayed, folded, or regrouped as upstream moves.
2. The schema-1 manifest groups by component/subject and omits a later
   13-commit repair chain.
3. A scheduled release reconstructs from incomplete or circular provenance.
4. Already-absorbed behavior may be replayed while live fork-only repairs may
   be dropped.
5. The resulting published ref can regress updater or compaction behavior even
   when the release job itself reaches a git commit.

Stable patch identity and explicit final commit identity are required to break
this chain.

## Superseded and partial fixes

"Superseded" here does not mean the commits should be reverted. It means their
local protection remains useful but their premise is too narrow to serve as
the end-to-end lifecycle contract.

| Commit(s) | Protection added | Why it did not close the incident class |
|---|---|---|
| `7e55b934` (June 15) | Stop a gateway running from the venv before recreation | Other Desktop, MCP, harness, and descendant holders remained outside that one owner model. |
| `6638199c` (June 24) | Harden the Windows venv-resident process sweep | A sweep can race supervisors and does not establish user consent or truthful hand-off completion. |
| `b14d75f8`, `87ae4ae9` (July 3) | Prevent/self-heal half-updated venvs; split out `--force-venv` | Repair after damage and a bypass flag do not prove pre-mutation readiness. The bypass is prohibited in the new skill. |
| `d358edd9`, `9507f438`, `a31fe8db` | Snapshot venv launchers, pause gateways, stop discovered launchers | Correct for owned gateways, but Codex MCP ownership and respawn require a distinct consented lifecycle. |
| `0b33ee88`, `826bf9b6`, `da3a0a85` (August 8) | Preserve full cmdlines; reap orphaned Desktop backends; make reaping tree-aware | These fix classification and orphan cases but cannot safely classify every generic holder as owned. |
| `79035c62`, `b6d6a083`, `e950fbb0`, `e0573c65`, `deedb3db`, `43022558`, `b028dd5d` plus related fork repairs | Quiesce Codex MCP bridges, gate imports, refresh refs, classify managed-runtime holders | The repair family was not fully represented in the integration manifest, and provenance/lease semantics need consolidation rather than subject-based replay. |
| `92be912d`, `3b08a0f9`, `6495ef82`, `36eda611`, `952f44f8` (August 9 family) | Repo-owned hand-off, visible console/progress, fail-closed gates, result surfacing, relaunch focus | These make staged execution more truthful, but the bootstrap hand-off still lacks parity and one-scan readiness does not prove stability across respawn. |

## Fork-integration manifest and provenance repair

### Schema-1 snapshot

The observed manifest at
`%LOCALAPPDATA%\hermes\scripts\hermes-integration-manifest.json` describes
nine components and 25 source patches. It has three material defects:

1. A 13-commit repair chain on the integration line is not represented.
2. The compaction component groups unrelated diagnostics and bootstrap work
   with native-compaction behavior.
3. Its source branch is a fork integration/rebase branch, creating circular
   provenance instead of naming the independent source and final identity.

The omitted chain and its stable identities are exactly, in source order:

| # | Source commit | Stable patch-id | Patch disposition | Integration state/identity |
|---:|---|---|---|---|
| 1 | `79035c62fa624a9be8ef6214891a41e644c41dcc` | `6db9757f0d97335a2d020a2dabf02d31c7620a98` | `review_required` | `null`; replacement implementation is not yet committed |
| 2 | `b6d6a0838f05a5a2f9d7fc350f006f0998705794` | `50ee09f1e291897c88d2289c80b0f00383f0d4ac` | `review_required` | `null`; replacement implementation is not yet committed |
| 3 | `43022558a040cf03e6f6f4761f5ac178eb737128` | `38fbf024b0be130d4645c5fdef2d9caf73c3ca7b` | `review_required` | `null`; replacement implementation is not yet committed |
| 4 | `e950fbb0fc9c7a0bc96c55e763b709d7b48437cf` | `38899fefccebae7010af17aed85ff844dd0234de` | `review_required` | `null`; replacement implementation is not yet committed |
| 5 | `b028dd5d7f99a3a8e36b345a7796947f18bf77df` | `eb3397df7e2f07bdc649f2814a73c94cd58b27c1` | `review_required` | `null`; replacement implementation is not yet committed |
| 6 | `a68f85c46c1ebdc94fb4b95feb7de9685b34b0b3` | `e01de898a2208e61e79703c8067fe1fc94ef293f` | `review_required` | `null`; replacement implementation is not yet committed |
| 7 | `e0573c650e8d995f9ad2527c276dfe0e2dddf7f5` | `0cc51d321b466467bea1ac2748dcb7f6696b8106` | `review_required` | `null`; replacement implementation is not yet committed |
| 8 | `deedb3db7f0e962b4e79f4c27ab24eaa350bcf2c` | `443aee472da1a3fbf9689935db493fd135aa9d59` | `review_required` | `null`; replacement implementation is not yet committed |
| 9 | `126528a77bda687013c274d8deaf9bcf5e5b97a5` | `2f439b403ed30012bfda1395d42d620843f2683b` | `review_required` | `null`; replacement implementation is not yet committed |
| 10 | `fb3ead63b596d5cd48c3c53d03e16503794b99f9` | `bfa6138d92f63e83a80bf77e0d00cc0cf1351c98` | `review_required` | `null`; replacement implementation is not yet committed |
| 11 | `48cf9e5266a68d6f46cd43969f538a43f39d2aba` | `718a7fb88308a5e74af5b0a3344036778962fbda` | `superseded` | `not_replayed` |
| 12 | `79959e421703fbead19f43d7b7edf3f50470dbb6` | `ecf4c06c7c4799dc48f0a72045919f49dc0f5dde` | `superseded` | `not_replayed` |
| 13 | `28e28682e056952920b94574e5413a981031ebf2` | `0f651f8c1330f7958b04fe3ece9fe99c5498c28d` | `superseded` | `not_replayed` |

The first ten remain `review_required`: their intended behavior is pending a
committed replacement, so assigning a final integration SHA would fabricate
provenance. The final three represent the intentionally dropped forced
handoff-exit direction and are `superseded` / `not_replayed`. Exact commit and
patch identities are necessary; subject text and inferred ranges cannot repair
the ledger reliably.

### Native-compaction duplicate

Upstream commit
`5e1b50115f01cda8f8749a347d6a75aeda03ff18`
(`feat(compression): native OpenAI Responses server-side compaction for
gpt-5.6`) has stable patch-id
`5eb223dec2ffeeeadbccaa143fd7e7e00dbc856e`.

Fork replay `9c12c4946` is patch-identical. Its full 40-character object was not
available in this worktree, so this audit does not invent it. The correct
classification is **absorbed upstream / do not replay**.

### Five retained or misgrouped patches

| Commit | Evidence classification | Deterministic v2 representation | Required manifest action |
|---|---|---|---|
| `8786aeca47be65e767a279e93791d89253a41cb7` — prune stale native replay safely | Retained test-only replay behavior; it must be tied to its implementation | Component `upstream_status=review_required`; patch `disposition=required`; integration `state=expected` with stable patch-id `f86cff4f611c5b5f016260557cdabb57f75180e0` and null final commit | Keep in `native-compaction-replay-safety`, related to `9b82e111...`, not the absorbed core patch. |
| `9b82e111bf4531d6a82bce905a94cbdcab47e647` — bound stateless checkpoint replay | Retain pending upstream | Component `upstream_status=review_required`; patch `disposition=required`; integration `state=expected` with stable patch-id `24e72bb4f36d99211d8adeed737bc79564da9c71` and null final commit | Keep as the implementation in `native-compaction-replay-safety`. |
| `67541cb56afebee4fcbc1fa08dcfa2c1f9183b0c` — preserve legacy reasoning summary boundaries | Retain pending upstream | Component `upstream_status=review_required`; patch `disposition=required`; integration `state=expected` with stable patch-id `0d1a3112d4228bbdf0421ba97a29f074e9d2f130` and null final commit | Keep in the distinct `legacy-reasoning-summary-boundaries` model-protocol component. |
| `f6e3c6081fbe82084ff42605b7d27ab8d0af31f7` — honor requested test-block duration | Retain, but misgrouped outside compaction | Component `upstream_status=review_required`; patch `disposition=required`; integration `state=expected` with stable patch-id `36eea134ff23b4cc3414d98efa6a4ed97e1767a0` and null final commit | Regroup into `diagnostics-test-block-duration`. |
| `adafd77fa07f68dcaf58d9b28466c0bdd4cb115c` — pin installer repository with build ref | Retain, but misgrouped outside compaction | Component `upstream_status=review_required`; patch `disposition=required`; integration `state=expected` with stable patch-id `7e226e7568e9f8cda10123d6ba331c19abcc360e` and null final commit | Regroup into `bootstrap-build-ref-pin`. |

### Required manifest v2 contract

The repo-owned schema v2 separates:

- manifest state (`ready` or `review_required`);
- component/category and component `upstream_status`;
- independent repository/source ref, source commit, and source stable patch-id;
- patch role and disposition (`required`, `absorbed_upstream`, `superseded`,
  `review_required`, or `folded`);
- integration state (`expected`, `not_replayed`, or `pending`), final commit,
  and final stable patch-id;
- acceptance tests; and
- candidate base/head and publication refs/commits.

Validation must reject circular or identical source/integration refs,
duplicate active patch-ids, malformed or missing SHA objects, subject
collisions used as equivalence, incohesive component grouping, and a manifest
that omits updater lifecycle work. Status checks should use remote ref reads
and local git objects; absence of proof is `unknown`, never "absorbed."

## Expected native-Windows CLI contract

This section is the normative target for the companion updater work. The
worktree now contains the schema, builder, and relational validator, but the
observed upstream snapshot did not. The contract becomes release authority
only after the implementation and tests are committed and published together.

### Commands

```text
hermes update --preflight --json
hermes update --drain --yes --json
hermes update --yes
```

Preflight is read-only. Drain requires `--yes`; without it, the command must
refuse without mutation. Drain is a standalone, explicitly requested temporary
bridge pause. Its approximately 90-second grace does not apply an update and
does not authorize a later public update invocation. In particular, a later
`hermes update --yes` has no capability token for the drain process's dead-owner
lease and must refuse it rather than pretend the transition is race-free.

For an explicit update, the skill calls `hermes update --yes` directly after
preflight and any required harness-interruption consent. That one invocation
acquires and owns its lease, drains exact supported bridges, proves stable
clear state, mutates the install, runs health checks, writes its correlated
receipt, and releases the lease. It must fail before mutation if any part of
that transaction cannot be established. The hidden `--bridge-lease-id`
capability is reserved for the Desktop/PowerShell hand-off; the skill must not
call or expose it.

The CLI cannot cryptographically distinguish a general update request from an
agent that passed `--yes` after a separate interruption dialog. For the thin
skill, the caller is therefore the authorization trust boundary: it records a
fresh answer for this attempt and invokes `--yes` only after that answer. For a
human invoking the CLI directly, documented `--yes` semantics are the explicit
prospective interruption consent. A reusable prior answer is never valid.

### Exit meanings

| Code | Preflight | Drain |
|---|---|---|
| `0` | Ready | Drain completed and two clear scans passed |
| `2` | Valid scan, not ready | Safely refused, timed out, or blockers remain |
| `1` | Probe/validation failure | Probe/validation failure |
| other | Unsupported contract; fail closed | Unsupported contract; fail closed |

### Versioned JSON

The canonical schema-v1 top-level fields are:

`schema_version`, `mode`, `ok`, `ready`, `blocked`, `reason`, `root`, `venv`,
`processes`, `mcp_bridges`, `pausable_gateways`,
`pausable_gateway_processes`, `git`, `last_update_receipt`, `lease`, `actions`,
and `error`.

Schema v1 has exactly the 17 listed top-level keys; unknown top-level keys
require a schema-version change and otherwise fail closed. The normative
repository artifact is `hermes_cli/update_readiness.schema.v1.json`; callers
must validate the whole envelope, not only the compatibility keys. Production
`hermes_cli.update_cmd.validate_update_readiness` is the normative relational
validator for invariants JSON Schema cannot express, including action-to-bridge
identity, supported actionable ownership, gateway counts, and lease/root
correlation. Types are:

| Key(s) | Required type/invariant |
|---|---|
| `schema_version` | Integer `1` |
| `mode` | String equal to `preflight` or `drain`, matching the command |
| `ok`, `ready`, `blocked` | Booleans; ready success is exactly `true,true,false` |
| `reason` | Stable machine reason string when non-ready, otherwise null |
| `root`, `venv` | Non-empty absolute path strings for the same canonical install |
| `processes`, `mcp_bridges`, `pausable_gateway_processes`, `actions` | Arrays |
| `pausable_gateways` | Non-negative integer equal to the gateway-process array length |
| `git` | Object or null; when present, contains the inspected ref/commit identity |
| `last_update_receipt` | Object or null; when used as success proof, `invocation_id` and `lease_id` differ from pre-state, timestamp follows this command's start, `gateway_resume_deferred` is boolean, and target/result/health correlate |
| `lease` | Object or null; an actionable drain reports a live root-bound lease and bounded hand-off deadline |
| `error` | Object or null; probe failure includes stable `code` and sanitized `message` strings |

Safety-critical nested shapes are normative in schema v1:

| Value | Required shape |
|---|---|
| `processes[]` | Exactly required `pid`, `name`, redacted `cmdline`, `owner`, `role`, `actionable`, `actionability`, `action`; optional `created_at`; owner enum `codex`, `claude`, `desktop`, `gateway`, `unknown`; role enum `other`, `desktop_backend`, `gateway_run`, `update_lock_holder`, `mcp_bridge_wrapper`, `mcp_bridge_worker`; generic blockers have `actionable=false`, `actionability="hard_block"`, `action="refuse"` |
| `mcp_bridges[]` | Exactly required positive `pid`/`created_at`, string `name`/redacted `cmdline`, enum owner, role `mcp_bridge_wrapper` or `mcp_bridge_worker`, boolean `actionable`, `actionability` in `hard_block`/`exact_mcp_bridge`, and `action` in `refuse`/`terminate_exact_mcp`; optional positive `wrapper_pid` only |
| `pausable_gateway_processes[]` | Generic process identity plus `owner="gateway"`, `role="gateway_run"`, `actionable=false`, `actionability="downstream_drainable"`, and `action="pause_downstream"` |
| Preflight `actions[]` | Exactly `type="terminate-mcp-bridge"`, positive `pid`/finite `created_at`, and supported `owner`/`role`, all matching one actionable bridge |
| Drain `actions[]` | A termination action adds boolean `terminated` and retains owner/role/outcome after the current blocker array clears; successful drain exposes a live root-bound lease and contains exactly one ordered `clear-scan(1)`, `clear-scan(2)` pair as the final two actions, with no earlier clear scan and no missing, duplicate, out-of-order, or trailing-action proof |
| `lease` | Exactly schema version `1`, non-reversible 64-character lowercase hexadecimal `lease_fingerprint`, positive `owner_pid`, finite ordered `created_at <= handoff_grace_until <= expires_at`, and canonical `install_root == root`; the public readiness document never exposes the raw `lease_id` adoption capability |
| `git` | `head`, `branch`, `dirty`, `tracking_remote`, `target_branch`, `target_ref`, and nullable `target_sha` with valid types/SHA forms |
| `last_update_receipt` | Exactly 15 fields: schema version `1`; bounded `invocation_id`/`lease_id`; mode `git` or `archive`; canonical root; remote/branch/ref/target/result SHA identities; positive timestamp; `success=true`; boolean `gateway_resume_deferred`; exact four-boolean health object. A receipt correlated to a live lease is withheld from public preflight until that capability is cleared |
| Receipt `health` | Exactly `critical_syntax`, `critical_imports`, `dependencies`, and `node_dependencies`; all must be true before success is claimed |
| `error` | Exactly stable string `code` and sanitized string `message` |

For exit `0`, `ok=true`, `ready=true`, `blocked=false`, `reason=null`, both
blocker arrays are empty, and `error=null`. Exit `2` is a valid non-ready
result with a non-null reason. Exit `1` is a fail-closed probe/validation result
with an error object. Generic `processes` always block. Gateways are
downstream-drainable and do not make the result blocked by themselves.

Only exact bridge entries owned by `codex` or `claude` may be actionable. Each
requires a positive PID, positive finite creation time, and role
`mcp_bridge_wrapper` or `mcp_bridge_worker`; the updater revalidates PID plus
creation time immediately before interruption. The module-name substring
`mcp_server` is not a role. Unknown/Desktop owners, mixed unsupported-owner
sets, unsupported roles, missing identity, or a scanner/schema mismatch fail
closed.

### Internal Desktop scanner envelopes

The Desktop helper must not accept a legacy partial scan as clear. No standalone
`hermes_cli/venv_blocker_scan.schema.v1.json` exists in this snapshot. The
current executable contract is implemented by Python
`hermes_cli/_scan_venv_blockers.py` (`SCHEMA_VERSION`, `_base_result`,
`_emit_probe_fail`, `scan_venv_blockers`, and `main`) and Electron
`apps/desktop/electron/venv-blocker-scan.ts`
(`parseVenvBlockerScanOutput`, `scanVenvBlockers`, and `terminateMcpBridge`),
with their Python and Vitest fixtures. A standalone shared schema is future
acceptance work; if added, both producers and consumers must validate it rather
than leaving it as an unwired document.

The required version-1 `mode="scan"` acceptance shape has exactly these 13
keys, with no extras:

`schema_version`, `mode`, `ok`, `ready`, `blocked`, `reason`, `root`, `venv`,
`processes`, `mcp_bridges`, `pausable_gateways`,
`pausable_gateway_processes`, and `error`.

Its shared values and nested process records use the same types, enums,
root/venv identity, redaction, gateway-count, owner/actionability, and
ready/blocking invariants as the public envelope. `schema_version` is `1` and
`mode` is exactly `scan`. A valid clear or blocked scan exits `0`; invalid
arguments, root/provenance mismatch, malformed output, or probe failure emits
a non-ready error envelope and exits `1`. Missing MCP/gateway fields and extra
keys fail closed rather than defaulting to empty.

The only mutation form has exactly these nine keys:

`schema_version`, `mode`, `ok`, `terminated`, `pid`, `created_at`, `root`,
`venv`, and `error`.

It requires `schema_version=1`, `mode="terminate_mcp_bridge"`, `ok=true`, a
boolean `terminated`, the requested positive PID/create-time pair, canonical
matching root/venv, and `error=null`. Any invalid or changed identity returns a
probe-failure envelope and exit `1`. Desktop may call it only for an entry that
already passed the supported owner/role/actionable contract, and the Python
action revalidates that entire contract before interruption.

### Desktop handoff result state machine

The CLI mutation receipt and Desktop handoff result are separate. No standalone
`apps/desktop/electron/handoff-result.schema.v2.json` exists in this snapshot;
the executable v2 contract is enforced directly by
`apps/desktop/electron/handoff-result.ts` (`RESULT_KEYS`, `RECEIPT_KEYS`,
`parseReceipt`, `parseHandoffResultValue`, `writeHandoffAck`, and terminal
consume/wait functions), paired with the PowerShell and Rust producers and
their native tests. A standalone schema remains optional future hardening: if
one is added, every producer and consumer must validate it rather than leaving
it as an unwired document.

The strict result-v2 object has exactly these 16 top-level keys:

`schema_version`, `attempt_id`, `state`, `ok`, `exit_code`, `message`, `branch`,
`invocation_id`, `lease_id`, `root`, `receipt`, `cleanup`, `runtime_health`,
`relaunch`, `desktop`, and `finished_at`.

Its embedded receipt is the exact 15-field receipt-v1 object described above;
the detached Desktop path requires `gateway_resume_deferred=true`. `cleanup`
has exactly `update_marker_released` and `bridge_lease_released`. `relaunch`
has exactly `state`, `pid`, `process_started_at`, `executable`, `requested_at`,
and `acknowledged_at`. `desktop` has exactly `build_id`, `build_source`, `root`,
`backend_ready`, and `backend_mode`.

The attempt-scoped acknowledgement is a separate strict version-1 object with
exactly 14 fields: `schema_version`, `attempt_id`, `invocation_id`, `lease_id`,
`pid`, `process_started_at`, `root`, `executable`, `build_id`, `build_source`,
`backend_ready`, `backend_mode`, `acknowledged_at`, and `error`.

Required transitions and actors are:

1. The updater validates the fresh 15-field CLI receipt under its owned lease,
   proves owner-correlated update-marker and bridge-lease cleanup, then
   atomically writes `state="pending"` with a random `attempt_id`, exact
   invocation/lease/root/result/runtime-health correlation, an exact relaunched
   PID/start-time/executable identity, and an empty Desktop proof. Pending has
   `ok=false`, no terminal exit code or finish time, and is never success.
2. The relaunched Desktop accepts only that pending attempt, revalidates its
   exact PID, creation time, executable, canonical root, and installed build
   identity, and proves an authenticated local or remote backend is ready. It
   atomically publishes the separate acknowledgement without changing the
   receipt or result file.
3. The updater consumes only an acknowledgement with the same attempt,
   invocation, lease, process identity, root, executable, and build identity.
   It compare-and-swap transitions the matching pending result to terminal
   `state="complete"`, copying the authenticated Desktop proof. Only this state
   has `ok=true` and `exit_code=0`.
4. Timeout, child exit, identity mismatch, unhealthy/unauthenticated backend,
   cleanup failure, or transition conflict terminalizes as `state="failed"`
   with `ok=false` and a non-zero exit code. Spawn success, a live PID, Boolean
   coercion, or an uncorrelated/pending record never implies completion.
5. Desktop consumes only a terminal record correlated to its current attempt.
   A legacy result may surface only a failure diagnostic; legacy success is
   never accepted.

## Acceptance criteria

### Preflight and classification

- [ ] Native-Windows preflight prints exactly one versioned JSON object to
  stdout and sends diagnostics to stderr.
- [ ] Exit `0`, `1`, and `2` match the table above and cannot contradict
  `ok`/`ready`/`blocked`.
- [ ] The internal scanner envelope is also versioned and requires its exact
  root, venv, blocker, MCP, and gateway keys. Missing MCP-aware fields from a
  legacy scanner are a probe failure, never an empty/clear default.
- [ ] The scanner binds its interpreter, `root`, and `venv` to the same managed
  install before classifying any process.
- [ ] Exact argv/module parsing distinguishes Desktop `serve`, gateway,
  supported Codex/Claude MCP bridges, harness, and unknown roles. No raw
  `serve` substring is an ownership proof.
- [ ] Unknown/generic processes always refuse. Only exact MCP-only sets owned
  by supported Codex/Claude harnesses can advertise a drain action.
- [ ] Output redacts sensitive flags and never requires the caller to inspect
  raw process command lines.

### Authorization, drain, and leases

- [ ] Drain without `--yes` performs no mutation.
- [ ] The caller names the proven owner and interruption impact, then obtains
  fresh explicit authorization.
- [ ] Before every atomic `hermes update --yes`, the caller obtains prospective
  consent for exact supported bridges that can appear after the preflight and
  before the updater's internal scan. An initially clear scan does not waive
  that consent.
- [ ] Public help states that update `--yes` authorizes this prospective exact
  MCP interruption in addition to ordinary interactive prompts. The skill maps
  it only from the separate consent response, never from a general update
  request.
- [ ] The drain revalidates PID plus creation time immediately before any
  bounded fallback termination.
- [ ] A short-lived, owner-checked quiesce lease gates the proven Codex/Claude
  MCP bridge during its bounded hand-off grace.
- [ ] A standalone drain is never chained into a later public update or called
  race-free; it is reported only as a temporary pause with bounded grace.
- [ ] An ordinary update acquires, drains, mutates, verifies, and receipts under
  one live owned lease; failure or ownership loss refuses/aborts the update.
- [ ] The success receipt has exactly 15 fields, including boolean
  `gateway_resume_deferred`. Public preflight withholds a receipt whose raw
  `lease_id` still matches a live lease and exposes it only after that
  capability is cleared.
- [ ] Two clear scans separated by the measured respawn interval are required;
  successful drain exposes a live root-bound lease and exactly the ordered
  action proof `[1, 2]` as its final two actions, with no earlier, missing,
  duplicate, out-of-order, or trailing-action clear sequence.
- [ ] A bridge that appears after consent is interrupted only when the consent
  covers that supported owner/role category and its live PID plus creation time
  is revalidated. Any unsupported owner, role, or actionability refuses.
- [ ] Lease transfer through a transient command wrapper remains owned until
  the updater finishes; foreign/expired ownership fails closed.
- [ ] No updater recovery path depends on `--force-venv`.
- [ ] No path performs an image-wide `taskkill` or other generic tree kill;
  only an exact consented bridge identity may be terminated.

### Desktop and bootstrap transaction

- [ ] Normal apply and bootstrap recovery both require
  `releaseBackendLockForUpdate().unlocked == true`.
- [ ] Both paths run the same post-release preflight and refuse on probe
  failure.
- [ ] Electron does not return `handedOff=true` until the staged updater owns
  the atomic update marker and proves adoption of the same bridge-lease ID.
  Missing or mismatched adoption logs a refusal and does not quit Desktop.
- [ ] The staged PowerShell process remains alive through legitimate teardown;
  any forced Electron exit fallback is bounded beyond the known four-second
  teardown window.
- [ ] A hand-off result is initiation, not success. Completion requires the
  update receipt, intended ref/commit, healthy runtime, and relaunch/post-check.
- [ ] `.hermes-update-in-progress` acquisition and cleanup are atomic,
  owner-correlated operations; a live foreign marker is conflict, never proof
  of a successful hand-off.
- [ ] `.hermes-update-result.json` uses strict result schema version `2` and an
  attempt-correlated two-phase protocol: the updater can write only a pending
  record after mutation and cleanup; the exact relaunched PID/start/executable
  must publish the strict acknowledgement for the same attempt,
  invocation/lease, root, installed build, and authenticated-ready backend
  before a terminal complete record is produced. This artifact is distinct
  from the CLI `last_update_receipt`.
- [ ] Unsupported preflight/schema behavior fails closed. PowerShell and Rust
  paths do not mutate through a legacy fallback, report `Complete` before
  cleanup/relaunch, or coerce a truthy field into success.
- [ ] Failure results surface once and retain enough sanitized detail for
  diagnosis without exposing secrets.

### Release and provenance

- [ ] Manifest v2 represents all 13 omitted repair-chain entries and records
  each exact patch disposition plus integration state; superseded work is
  explicitly `not_replayed`.
- [ ] The native compaction core patch-id `5eb223de...` appears once as
  absorbed upstream and is not replayed.
- [ ] The five retained/misgrouped patches have the classifications in this
  audit.
- [ ] Source commit, stable patch-id, final commit, tests, and publication SHA
  are separate fields.
- [ ] The release job fails before publication on circular refs, duplicate
  active patch-ids, missing objects, subject collisions, or incomplete updater
  provenance.
- [ ] The scheduled publisher invokes `scripts/fork_integration_release.py`
  against the candidate commit's exact v2 manifest blob. An invalid,
  incomplete, or `review_required` manifest produces zero ref writes and never
  falls back to the machine-local schema-1 file.
- [ ] Replacement finalization uses the supported stdout-only
  `fork_integration` CLI `--finalize-component`, `--source-ref`, and repeatable
  `--replacement SOURCE_SHA:INTEGRATION_SHA:ROLE[:RELATED_TO]` arguments. It validates through
  `finalize_component_replacement`, emits structured JSON success/refusal, and
  performs no file or ref write; the resulting candidate still passes the
  ordinary audit/publication gate.
- [ ] A scheduled-release success is verified against the remote published SHA,
  not the local job exit code alone.

### Skill and operator workflow

- [ ] The optional skill runs only the two versioned readiness/recovery
  commands, plus ordinary `hermes update --yes` when the user explicitly asks
  for the update.
- [ ] It never enumerates or terminates processes, writes markers, uses
  `--force-venv`, or changes git history/remotes.
- [ ] It requests explicit authorization before either a standalone drain or
  an atomic update would interrupt supported harness bridges.
- [ ] It asks for prospective bridge-interruption consent before every atomic
  `--yes` update, including when the preceding preflight is clear.
- [ ] It never drains for a readiness-only request; the short lease is
  used only for an explicitly requested temporary pause.
- [ ] It never calls standalone drain as a precursor to ordinary update; the
  explicitly requested normal updater owns its own atomic drain/mutation
  transaction.
- [ ] A general update request authorizes only the ordinary update, never MCP
  interruption.
- [ ] It never claims success without a fresh post-update preflight plus a new
  current-invocation success receipt whose invocation/lease IDs differ from
  pre-state and whose timestamp, intended ref, result identity, and runtime
  health correlate. Desktop build/relaunch proof remains a separate artifact.

## Implementation mapping

| Concern | Primary implementation surface | Required proof |
|---|---|---|
| CLI flags, exit codes, receipts, git/install identity | `hermes_cli/update_readiness.schema.v1.json`; primary logic in `hermes_cli/update_readiness.py` and `hermes_cli/update_receipt.py`; integration orchestration in `hermes_cli/update_cmd.py`; parser wiring in `hermes_cli/subcommands/update.py`; invocation in `hermes_cli/main.py` | Native-Windows CLI tests for schema structure and relational rejection, ready, blocked, probe failure, drain refusal, drain success, atomic apply, and post-receipt state |
| Exact holder scan and owner attribution | Current executable contract in `hermes_cli/_scan_venv_blockers.py` (`_base_result`, `_emit_probe_fail`, `scan_venv_blockers`, `main`) and `apps/desktop/electron/venv-blocker-scan.ts` (`parseVenvBlockerScanOutput`, `scanVenvBlockers`, `terminateMcpBridge`), plus Codex/Claude runtime attribution surfaces; a standalone shared schema is future acceptance work | Real subprocess/psutil tests on Windows and matched Python/Vitest fixtures; reject legacy/defaulted fields, and if a standalone schema is added, prove every producer and consumer validates it |
| Desktop JSON parsing and consent UX | `apps/desktop/electron/venv-blocker-scan.ts`, `apps/desktop/electron/update-preflight.ts` (`runWindowsUpdatePreflight` and `exactMcpOnly`), `apps/desktop/electron/main.ts` | Vitest for missing/legacy internal scanner keys, mismatched root/venv, malformed schema, unsupported owner/role/actionability refusal, owner wording, clear-scan prospective consent, declined consent, respawn, and two-clear-scan requirement |
| Normal/bootstrap parity | Desktop apply and `handOffWindowsBootstrapRecovery` paths | One shared transaction helper or behavior-parity tests proving identical gates |
| Lease transfer and updater ownership | `apps/desktop/electron/mcp-bridge-quiesce.ts` (`waitForMcpBridgeQuiesceLeaseAdoption`, `handOffMcpBridgeLeaseToStagedUpdater`), `apps/desktop/electron/updater-process.ts` (`stagedUpdaterEnvironment` / `HERMES_UPDATE_BRIDGE_LEASE_ID`), `apps/desktop/electron/update-marker.ts` | Race tests for atomic marker claim, exact lease adoption, foreign/expired owner refusal, and owner-correlated cleanup |
| Bootstrap child lease adoption | `apps/bootstrap-installer/src-tauri/src/update.rs` at the staged updater's lease adoption, `update_child_env`, and `hermes update` child construction | Native Rust/process test proves the adopted capability reaches the exact Python child, that the child adopts the same lease ID, and that omission or mismatch refuses before mutation |
| Deferred gateway finalizer | Hidden internal CLI `hermes update --resume-deferred-gateway` in `hermes_cli/subcommands/update.py`, primary implementation in `hermes_cli/update_deferred_gateway.py`, direct dispatch import in `hermes_cli/update_cmd.py`, and native PowerShell/Rust callers | Native process tests patch the leaf owner and prove exact invocation/root/lease correlation, authenticated plan consumption, update-lock ownership, child adoption frame, gateway readiness, receipt correlation, and fail-closed lease cleanup; this command is not a public skill action |
| Detached updater result | Current strict v2 executable contract in `scripts/desktop-update.ps1`, `apps/bootstrap-installer/src-tauri/src/update.rs`, and `apps/desktop/electron/handoff-result.ts` (`RESULT_KEYS`, `RECEIPT_KEYS`, `parseReceipt`, `parseHandoffResultValue`, `writeHandoffAck`, terminal consume/wait); a standalone schema file is future hardening only | Native PowerShell and Rust subprocess plus Vitest scenarios for atomic pending result, exact relaunched PID/start/executable, attempt/invocation/lease/root/build correlation, authenticated backend ACK, compare-and-swap terminal complete, timeout/failure/cleanup, and legacy-failure-only handling; never treat this artifact as the CLI receipt |
| Bootstrap installer transaction | `apps/bootstrap-installer/src-tauri/src/` plus the strict result-v2/ACK protocol | Rust tests for handler lifetime, child ownership, exact lease propagation, pending/ACK/terminal transitions, timeout, and receipt correlation |
| Fork manifest v2, replacement finalizer, and release checks | Canonical `fork_integration/hermes-fork-manifest.v2.json`, `fork_integration/manifest.schema.v2.json`, audit/prepare/publish modules, `fork_integration/finalize.py`, supported `fork_integration/cli.py --finalize-component ...` consumer, live publisher `scripts/fork_integration_release.py`, and scheduled-launcher/deployment wiring; legacy `%LOCALAPPDATA%\hermes\scripts\hermes-integration-manifest.json` is migration input only | Validate the exact canonical manifest blob at the candidate publication SHA; prove the finalizer emits structured candidate JSON with no writes; E2E prove invalid/incomplete/`review_required` v2 yields zero ref writes and no legacy fallback; then fixture-driven and remote-ref/local-object status checks |
| Optional operator skill | `optional-skills/autonomous-ai-agents/windows-update-readiness/SKILL.md` | `tests/skills/test_windows_update_readiness_skill.py` and skill validator |

## Native validation matrix

These are separate, observed native-Windows results from the uncommitted
integration worktree. They are not a timeless test-count contract, are not
summed, and do not make the snapshot SHAs release authority. WSL-derived
results are excluded.

| Surface | Native command/scope | Observed result |
|---|---|---|
| Python affected native lifecycle set | `tests/test_hermes_mcp_update_gate.py`, constants, MCP-server transport, scanner, readiness, `cmd_update`, autostash, yes-flag, and stale-dashboard tests | 283 passed, 18 skipped in 94.63 seconds |
| Python broad updater matrix before the behavior-preserving extraction | All `tests/hermes_cli/test_update*.py`, `test_cmd_update.py`, `test_cmd_update_docker.py`, and `tests/cli/test_update_command.py` | 320 passed, 10 skipped in 115.24 seconds |
| Python combined updater family, scanner, and optional skill after extraction | Native matrix against the four directly owned leaf modules and coordinator integration | 378 passed, 10 skipped |
| Python readiness plus optional-skill integration after extraction | Focused readiness and skill contract matrix | 113 passed |
| Python focused post-extraction lifecycle slice | Focused native lifecycle matrix | 271 passed, 1 skipped |
| Fork manifest, finalizer, audit, and publication | `.\.venv\Scripts\python.exe -m pytest tests\fork_integration -q` | 156 passed in 386.40 seconds |
| Windows-only fork-audit markers | `.\.venv\Scripts\python.exe -m pytest tests\fork_integration\test_audit.py -q -m windows_only`; `.\.venv\Scripts\python.exe -m pytest tests\ci\test_list_os_marked_tests.py -q` | 3 passed/73 deselected; 9 marker-discovery tests passed |
| Electron updater transaction | Electron's Node runtime running the 14 focused `electron/*.test.ts` update, marker, identity, lease, preflight, handoff-result, Desktop-proof, orchestration, and relaunch-exit files | 207 of 207 passed |
| PowerShell updater handoff, PowerShell 7 | `pwsh` running `scripts/tests/test-desktop-update-handoff.ps1` sequentially | 103 of 103 passed |
| PowerShell updater handoff, Windows PowerShell 5.1 | `powershell.exe` running the same harness sequentially | 103 of 103 passed |
| Rust staged updater | `cargo test --offline update::tests --lib` | 50 passed, 0 failed, 31 filtered |
| Optional readiness skill | Native Git-for-Windows runner invoking `scripts/run_tests.sh tests/skills/test_windows_update_readiness_skill.py -q` | 26 passed |
| Repository skill-authoring standards | Native Git-for-Windows runner invoking `scripts/run_tests.sh tests/skills/test_authoring_standards.py -q` | 1,166 passed |

The focused Electron TypeScript/ESLint checks, PowerShell parser checks,
`rustfmt` check, and owned production diff checks also passed. The full
Electron TypeScript build still reports an unrelated, pre-existing missing
`get-windows` type in `window-below.ts`; it is not counted as a focused updater
failure. The generic skill-creator quick validator rejects the repository's
required `author`, `platforms`, and `version` frontmatter extensions, while the
repository's authoritative authoring suite passes them.

## Maintainability follow-up

The Python portion of the god-module follow-up is implemented in this
worktree. A behavior-preserving extraction reduced
`hermes_cli/update_cmd.py` from 9,026 to 6,796 lines and moved the receipt,
readiness, quiesce, and deferred-gateway seams into
`hermes_cli/update_receipt.py`, `hermes_cli/update_readiness.py`,
`hermes_cli/update_quiesce.py`, and `hermes_cli/update_deferred_gateway.py`.
None of those leaves imports `hermes_cli.main` or `hermes_cli.update_cmd`.
The coordinator imports the leaf-owned APIs directly, tests patch the leaf
owners, and the final simplification removes the interim injected-collaborator
APIs and wrapper shims. The post-extraction matrix above, compile/import
checks, Ruff, and owned diff checks passed. This
extraction is still uncommitted at the matrix observation and is not release
authority by itself.

The remaining explicit maintainability follow-up is behavior-preserving
decomposition of the Desktop Electron orchestration, PowerShell handoff, and
Rust updater after their safety contracts are frozen. Keep the public
CLI/schema and result-v2/ACK contracts unchanged, move tests with the extracted
seams, and do not combine that cleanup with further lifecycle behavior changes.

## Known limitations and unresolved uncertainty

- Incident counts are bounded by retained logs/session rows. "At least 13" is
  intentional; missing/rotated evidence may contain more windows.
- The `29` records / `28` attempts and `15/15` hand-offs describe the supplied
  native-Windows corpus, not a population-wide failure rate.
- The five failed scheduled releases are not asserted to share one root cause.
  Release provenance gaps and updater lifecycle failures overlap in time, but
  each run still needs its own terminal evidence.
- Live Codex ownership is an ephemeral observation. Any later interrupting
  drain or update must re-establish owner, role, PID, and creation time.
- The fork replay `9c12c4946` was available by short identity and stable
  patch-id comparison, but its full object was absent from this worktree.
- The seeded manifest ledger remains deliberately `review_required`: offline
  audit still reports 20 unavailable legacy source objects, 21 subject-only
  integration ambiguities, and five expected patches absent from the current
  local integration history. Those are publication blockers, not evidence to
  guess equivalence; a passing focused-test count is not publication authority.
- Branch tips and ahead/behind counts move; the SHA table is not an update
  target or replay instruction.
- The CLI schema and validator are present in this uncommitted integration
  worktree but absent from the observed upstream snapshot. Older or published
  builds that do not provide the exact contract must fail closed rather than
  have the skill emulate it.
- The internal scanner and Desktop handoff-result standalone schema files named
  as future hardening targets above do not exist in this snapshot. Today the
  checked-in Python/TypeScript parsers and their tests are the executable
  contract. Exact no-default scanner parsing remains acceptance work; strict
  result-v2/ACK parsing is executable but has no separate schema artifact.
- The direct Rust staged-binary relaunch fallback intentionally does not signal
  or terminate a separately opened Desktop that owns Electron's
  single-instance lock. It requires the exact spawned process to acknowledge;
  a redirected survivor launch fails closed, cannot emit `Complete`, and asks
  the user to close the survivor and retry. The richer survivor handoff remains
  available only through the normal repo-script path.
- The optional skill cannot prove GUI binary replacement merely because the
  backend ref changed. Public readiness schema v1 exposes the CLI update
  receipt and runtime health but no Desktop build/relaunch proof. Therefore the
  skill must report Desktop-driven completion as ambiguous until a versioned,
  attempt-correlated result is exposed through an allowed updater command.
- WSL evidence and behavior are excluded. Only the native-Windows results in
  the evidence manifest and final validation matrix are authoritative; POSIX
  lock and process semantics are outside this audit.

## Durable decision record

The updater owns enumeration, exact classification, leases, and bounded
termination. The skill owns interpretation, consent, sequencing, and honest
reporting. Desktop and bootstrap own the hand-off user experience. The fork
release pipeline owns provenance and publication verification. None of these
surfaces may claim the next surface succeeded merely because its own hand-off
returned successfully.
