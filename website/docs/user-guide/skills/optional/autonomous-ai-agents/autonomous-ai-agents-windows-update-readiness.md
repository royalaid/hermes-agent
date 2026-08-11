---
title: "Windows Update Readiness — Check and recover native-Windows Hermes updates"
sidebar_label: "Windows Update Readiness"
description: "Check and recover native-Windows Hermes updates"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Windows Update Readiness

Check and recover native-Windows Hermes updates.

## Skill metadata

| | |
|---|---|
| Source | Optional — install with `hermes skills install official/autonomous-ai-agents/windows-update-readiness` |
| Path | `optional-skills/autonomous-ai-agents\windows-update-readiness` |
| Version | `0.1.0` |
| Author | Royalaid, Hermes Agent |
| License | MIT |
| Platforms | windows |
| Tags | `Windows`, `Updater`, `Preflight`, `Recovery`, `Safety` |
| Related skills | [`hermes-agent`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-hermes-agent) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Windows Update Readiness Skill

Check a native-Windows Hermes install before updating. Let the updater own
process discovery and recovery; this skill only interprets its versioned JSON,
asks for consent when recovery would interrupt an MCP bridge, and verifies the
result.

## When to Use

- A native-Windows update reports a managed-runtime or venv blocker.
- The user asks whether an update is safe.
- Desktop reported a hand-off but the update did not finish.
- Do not use this workflow on Linux, macOS, or WSL.

## Prerequisites

- Run commands in a native-Windows terminal.
- Require a Hermes build that supports the updater flags below. If a flag or
  schema is unsupported, stop; do not reproduce its behavior with shell tools.
- A user request to update authorizes the ordinary update, but not interruption
  of MCP bridges. Obtain fresh, prospective consent before every standalone
  drain and every atomic `--yes` update, even when preflight is initially clear.
- This skill is the authorization trust boundary for `--yes`: invoke it only
  from the user's affirmative answer for this exact attempt, never from prior
  chat context or the update request alone.
- Codex and Claude can share the installed skill through `~/.agents/skills/`;
  Hermes can include that directory with `skills.external_dirs`.

## How to Run

Use `terminal` with a bounded timeout and exactly one of these values in its
`command` argument:

```text
hermes update --preflight --json
hermes update --drain --yes --json
hermes update --yes
```

Always start and finish with preflight. Run drain only after the consent step
and only when the user explicitly asks for a temporary bridge pause. For an
explicit update, do not run drain first: `hermes update --yes` owns its lease,
drain, and mutation in one invocation. Otherwise report readiness or the exact
blocker status and stop.

## Quick Reference

Both JSON modes must print one object on stdout. Treat the bundled
`hermes_cli/update_readiness.schema.v1.json` as normative. Require schema
version `1` and all 17 top-level keys:

```text
schema_version, mode, ok, ready, blocked, reason, root, venv,
processes, mcp_bridges, pausable_gateways, pausable_gateway_processes,
git, last_update_receipt, lease, actions, error
```

Require the documented types, `mode` matching the command, and a coherent
state. Schema v1 permits no extra top-level keys. The three state fields are
booleans; `reason` is a stable string or null; `root` and `venv` are strings;
the three process collections and `actions` are arrays;
`pausable_gateways` is a non-negative integer; and `git`,
`last_update_receipt`, `lease`, and `error` are an object or null. Exit `0`
requires `ok=true`, `ready=true`, `blocked=false`, `reason=null`, empty blocker
arrays, and `error=null`. Generic `processes` always block. Treat gateways as
downstream-drainable information.

Structural schema validation is necessary but not sufficient. Apply the
relational checks below too; in particular, an action must match an actionable
bridge in the same document and a lease must match the same canonical root.

Validate these safety-critical nested shapes:

- An interruptible bridge has `pid`, `created_at`, `owner`, `role`,
  `actionable=true`, `actionability="exact_mcp_bridge"`, and
  `action="terminate_exact_mcp"`. Its preflight action has exactly
  `type="terminate-mcp-bridge"`, the same PID, creation time, owner, and role.
  A drain termination adds boolean `terminated`. Historical drain actions keep
  that owner/role/outcome even after the final blocker arrays are clear.
- A lease has schema version, a 64-character lowercase hexadecimal
  `lease_fingerprint`, positive `owner_pid`, ordered finite
  `created_at <= handoff_grace_until <= expires_at`, and an `install_root`
  matching `root`. The public document never contains the raw `lease_id`
  hand-off capability; never request, infer, or echo it.
- `git` carries `head`, branch/target ref, target SHA, tracking remote, and
  dirty state. A receipt carries a new `invocation_id`, `lease_id`, timestamp,
  success flag, boolean `gateway_resume_deferred`, mode,
  root/branch/target/result identities, and an exact `health` object with
  boolean `critical_syntax`, `critical_imports`, `dependencies`, and
  `node_dependencies` fields. The receipt has exactly 15 fields.
- An error object has stable string `code` and sanitized string `message`.

A successful `mode="drain"` document contains a live root-bound lease and
exactly one ordered `clear-scan(1)` followed by one `clear-scan(2)` as the
final two `actions`; no clear scan may appear earlier, and missing, duplicate,
out-of-order, or trailing-action proof fails closed.

Exit codes are:

- `0`: ready; for drain, the updater completed stable-clear checks under its
  temporary lease. Drain-ready is not independent update readiness.
- `2`: valid scan, but safely blocked, refused, or timed out.
- `1`: probe or schema validation failure.
- Any other code: unsupported contract; fail closed.

The JSON and exit code must agree. Do not infer success from either alone.

## Procedure

### 1. Preflight and classify

Run `hermes update --preflight --json`, retain its exit code, and parse exactly
one JSON object. Reject malformed JSON, extra stdout, a missing key, an
unsupported schema, a mode mismatch, or contradictory state. Confirm `root`
and `venv` identify the intended install.

Generic, Desktop, unknown, or unattributed blockers always stop the workflow.
An interruptible set must have no `processes`; every `mcp_bridges` entry must
have a positive integer PID, positive finite creation time, owner `codex` or
`claude`, role `mcp_bridge_wrapper` or `mcp_bridge_worker`, and a matching
action. Never infer ownership from a command-line substring.

For a readiness-only request, report ready or the sanitized blocker status and
stop. Do not drain.

### 2. Obtain interruption consent

For an explicit update or an explicitly requested temporary bridge pause, tell
the user how many supported bridges and which owners are currently observed,
and that active tool calls may fail. Also state that an exact supported Codex
or Claude bridge launched after preflight may be paused during the bounded
transaction. Ask a direct yes/no question for this attempt. A prior answer, a
general update request, or an initially clear scan is not interruption consent.
If the user declines or does not answer clearly, stop without mutation.

### 3. Follow the requested operation

For a temporary pause request, run `hermes update --drain --yes --json` after
consent. Validate the full drain envelope and exit code, then run preflight
again. While the dead-owner hand-off grace remains live, require a coherent
exit-`2` result with reason `quiesce-lease-active` and the same root/lease
identity; it prevents an unrelated updater from claiming the pause. If the
grace already expired, a fresh ready result is allowed, but it grants no
capability. State that the lease has only about 90 seconds of grace, no update
was applied, and neither result authorizes a later update.

For an explicit update, record the pre-update `git` identity, receipt
`invocation_id`/`lease_id`/timestamp, and the update command's start time, then
run `hermes update --yes` directly. Do not call the drain-only command first.
The ordinary updater must acquire, own, and renew its lease while it drains and
mutates in the same invocation; any refusal or loss of ownership is failure. A
Desktop hand-off message is initiation, not completion.

### 4. Post-check an update

After an attempted ordinary update, run `hermes update --preflight --json`
again. Claim update success only when:

- the ordinary updater reached a successful terminal result;
- the final preflight is a coherent exit-`0` ready result;
- `last_update_receipt` has new `invocation_id` and `lease_id` values versus
  pre-state, has a timestamp after this update command began, reports success,
  includes boolean `gateway_resume_deferred`, has all health checks true, and
  its installed identity agrees with `git` and the intended target; and
- a Desktop-driven update includes successful build/relaunch proof from a
  versioned, attempt-correlated updater result.

If the install is ready but receipt/ref evidence is absent or ambiguous, say
that readiness is verified and update completion is not. Public readiness
schema v1 does not expose Desktop build/relaunch proof, so this skill alone
cannot claim completion for a Desktop-driven update under schema v1.
When `gateway_resume_deferred=true`, the flag records the mutation path rather
than proving a gateway or Desktop relaunch. A receipt correlated to an active
lease is withheld from public preflight; require the final ready result with
`lease=null` before using the later historical receipt as update evidence.

## Pitfalls

- Never enumerate processes directly or substitute WMI/CIM/process listings.
- Never run `taskkill`, `Stop-Process`, or any direct termination command.
- Never create, delete, or rewrite updater markers or quiesce leases.
- Never use `--force-venv`.
- Never rebase, change remotes, or repair fork history in this workflow.
- Never expose raw blocker command lines, environment values, or secrets.
- Never classify `mcp_server` as Desktop `serve` by substring.
- Never fall back to ad hoc recovery when a command or schema is unavailable.

## Verification

- The initial and final preflights passed the complete schema and state checks.
- Any drain had fresh consent and was followed by an independent preflight.
- No direct process, marker, bypass, or git-history operation was used.
- The ordinary update ran only when the user explicitly requested it.
- The result distinguishes readiness from proven update completion.
