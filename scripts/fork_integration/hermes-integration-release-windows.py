#!/usr/bin/env python
"""Daily safe maintenance for the published Hermes integration branch.

The job reconstructs ``fork-integration`` from a declared stack: fetched
``origin/main`` is followed by pinned upstream-PR patches (whether or not those
PRs are merged), then ordered fork-only component patches. It validates the bootstrap updater, force-pushes with an
explicit lease, then publishes a GitHub prerelease containing the blue Windows
bootstrap/updater launcher (not an MSI/NSIS versioned installer) plus a
checksum/provenance manifest. It retains the five newest releases created by
this automation (``integration-*`` tags).

All substantive failures leave the branch's pre-run checkout intact.  A rebase
conflict is aborted rather than being left for a later scheduled run to
accidentally continue.  ``--dry-run`` is read-only: it never rebases, builds,
pushes, creates releases, uploads assets, or deletes releases.
"""
from __future__ import annotations

import argparse
import base64
import contextvars
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HOME = Path.home()
HERMES_HOME = HOME / "AppData" / "Local" / "hermes"
SCRIPT_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = SCRIPT_DIR / "hermes-integration-manifest.json"
# U6 (R3): rejected and superseded candidate patch-ids. Lives beside the
# manifest and is tracked by sync.py; an absent file is an empty blocklist.
BLOCKLIST_PATH = SCRIPT_DIR / "fork-integration-blocklist.json"
PROPOSALS_SCRIPT = SCRIPT_DIR / "proposals.py"
WORKTREE = HERMES_HOME / "worktrees" / "openai-native-windows"
FORK_REMOTE = "fork"
RELEASE_PREFIX = "integration-"
RETAIN_RELEASES = 5
LOG_PATH = HERMES_HOME / "logs" / "cron-scripts" / "hermes-integration-release.log"
REVIEW_DIR = HERMES_HOME / "logs" / "cron-scripts" / "integration-release-reviews"
LOCK_PATH = HERMES_HOME / "cron" / "locks" / "hermes-integration-release.lock"
LAUNCHER_PATH = Path("apps/bootstrap-installer/src-tauri/target/release/Hermes-Setup.exe")
GH_EXE = HOME / "scoop" / "apps" / "gh" / "current" / "bin" / "gh.exe"
FLEET_ROOT = HOME / "git" / "fleet"
FLEET_SCRIPT_DIR = FLEET_ROOT / "scripts"
FLEET_JOB_ID = "1ab4c7013fef"
FLEET_JOB_NAME = "Daily Hermes integration rebase + blue updater release"
FAILURE_INVESTIGATOR_SCRIPT = SCRIPT_DIR / "hermes-release-failure-investigator.py"

# The manifest is the component contract.  Its source patches also provide the
# only path-scope authority at validation time; do not maintain a second
# hand-written subject/path list here.
APPROVED_INTEGRATION_PATHS = {
    # desktop-hitch-diagnostics component (manifest id: desktop-hitch-diagnostics)
    "agent/delegation_context.py",
    "apps/desktop/electron/diagnostics-capture.test.ts", "apps/desktop/electron/diagnostics-capture.ts",
    "apps/desktop/electron/diagnostics-classify.test.ts", "apps/desktop/electron/diagnostics-classify.ts",
    "apps/desktop/electron/diagnostics-export.test.ts", "apps/desktop/electron/diagnostics-export.ts",
    "apps/desktop/electron/diagnostics-gateway.test.ts", "apps/desktop/electron/diagnostics-gateway.ts",
    "apps/desktop/electron/preload.ts",
    "apps/desktop/scripts/perf/README.md", "apps/desktop/scripts/perf/lib/launch.mjs",
    "apps/desktop/scripts/perf/run.mjs", "apps/desktop/scripts/perf/scenarios/hitch-classify.mjs",
    "apps/desktop/scripts/perf/scenarios/index.mjs",
    "apps/desktop/src/app/settings/diagnostics-settings.tsx", "apps/desktop/src/app/settings/index.tsx",
    "apps/desktop/src/app/settings/types.ts", "apps/desktop/src/debug/perf-live.ts",
    "apps/desktop/src/diagnostics/arming-bridge.test.ts", "apps/desktop/src/diagnostics/arming-bridge.ts",
    "apps/desktop/src/diagnostics/capture.test.ts", "apps/desktop/src/diagnostics/capture.ts",
    "apps/desktop/src/diagnostics/index.ts", "apps/desktop/src/diagnostics/long-frames.ts",
    "apps/desktop/src/diagnostics/ring-buffer.test.ts", "apps/desktop/src/diagnostics/ring-buffer.ts",
    "apps/desktop/src/diagnostics/stream-delta.test.tsx",
    "apps/desktop/src/global.d.ts", "apps/desktop/src/main.tsx",
    "docs/observability/desktop-hitch-diagnostics.md",
    "docs/plans/2026-08-06-001-fix-desktop-hitching-diagnostics-plan.md",
    "hermes_cli/debug.py", "hermes_cli/diagnostics_diagnose.py", "hermes_cli/diagnostics_ring.py",
    "hermes_cli/main.py", "hermes_cli/subcommands/debug.py", "hermes_cli/web_server.py",
    "plugins/kanban/dashboard/dist/index.js", "plugins/kanban/dashboard/plugin_api.py",
    "tests/hermes_cli/test_diagnostics_diagnose.py", "tests/hermes_cli/test_diagnostics_ring.py",
    "tests/plugins/test_kanban_dashboard_plugin.py", "tests/tools/test_delegate_kanban_isolation.py",
    "tui_gateway/ws.py",
    # end desktop-hitch-diagnostics component
    # gateway-config-offloop component (manifest id: gateway-config-offloop)
    "tests/hermes_cli/test_web_server_config_offloop.py",
    # end gateway-config-offloop component
    # inflight-journal-bounded component (manifest id: inflight-journal-bounded)
    "apps/desktop/src/lib/inflight-turn-journal.ts",
    "apps/desktop/src/lib/inflight-turn-journal.test.ts",
    # end inflight-journal-bounded component
    # gateway-config-offloop follow-up: router off-loop sweep
    "hermes_cli/web_routers/cron.py",
    "hermes_cli/web_routers/mcp.py",
    "hermes_cli/web_routers/skills.py",
    "hermes_cli/web_routers/tools.py",
    # end gateway-config-offloop follow-up
    "agent/agent_init.py", "agent/chat_completion_helpers.py", "agent/codex_runtime.py",
    "README.md", "apps/desktop/electron/main.ts", "apps/desktop/electron/update-branch.ts", "apps/desktop/electron/update-branch.test.ts",
    "apps/bootstrap-installer/src-tauri/Cargo.toml", "apps/bootstrap-installer/src-tauri/build.rs",
    "apps/bootstrap-installer/src-tauri/src/bootstrap.rs", "apps/bootstrap-installer/src-tauri/src/install_script.rs",
    "apps/bootstrap-installer/src-tauri/src/update.rs", "apps/desktop/electron/backend-env.test.ts",
    "apps/desktop/electron/backend-env.ts", "apps/desktop/electron/main.ts",
    "apps/desktop/electron/windows-user-env.test.ts", "apps/desktop/electron/windows-user-env.ts",
    "apps/desktop/harness/reasoning-layout/README.md", "apps/desktop/harness/reasoning-layout/assert.mjs",
    "apps/desktop/harness/reasoning-layout/index.html", "apps/desktop/package.json",
    "apps/desktop/src/app/session/hooks/use-message-stream/delta-flush.test.tsx",
    "apps/desktop/src/app/session/hooks/use-message-stream/gateway-event.ts",
    "apps/desktop/src/app/session/hooks/use-message-stream/index.ts",
    "apps/desktop/src/components/assistant-ui/thread/message-parts.tsx",
    "apps/desktop/src/components/assistant-ui/thread/streaming.test.tsx",
    "apps/desktop/src/dev/reasoning-layout-harness.tsx", "apps/desktop/src/lib/chat-messages.test.ts",
    "apps/desktop/src/lib/chat-messages.ts", "run_agent.py", "scripts/install.ps1", "scripts/install.sh",
    "tests/agent/test_codex_app_server_event_bridge.py", "tests/cli/test_reasoning_command.py",
    "tests/run_agent/test_run_agent_codex_responses.py", "tests/test_install_ps1_repository_pin.py",
    "tests/tools/test_local_env_windows_msys.py", "tests/tui_gateway/test_codex_app_server_live_events.py",
    "tools/environments/local.py", "tui_gateway/server.py",
}


def load_manifest() -> dict[str, Any]:
    """Read the reviewable required-component contract; reject any ambiguity."""
    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"integration manifest is unreadable: {MANIFEST_PATH}: {exc}") from exc
    expected_keys = {"schema", "integration_branch", "upstream", "upstream_foundations", "fork", "components"}
    if set(manifest) != expected_keys or manifest.get("schema") != 3:
        raise RuntimeError("integration manifest has an unsupported schema or keys")
    if not isinstance(manifest["upstream_foundations"], list) or not manifest["upstream_foundations"]:
        raise RuntimeError("integration manifest has no required upstream foundations")
    foundation_ids: set[str] = set()
    for foundation in manifest["upstream_foundations"]:
        if set(foundation) != {"id", "repository", "pull_request", "approved_head", "base_ref", "patches"}:
            raise RuntimeError("integration manifest has malformed upstream foundation metadata")
        foundation_id = foundation.get("id")
        if not isinstance(foundation_id, str) or not foundation_id or foundation_id in foundation_ids:
            raise RuntimeError("integration manifest has invalid or duplicate upstream foundation ids")
        foundation_ids.add(foundation_id)
        if not isinstance(foundation.get("repository"), str) or foundation["repository"].count("/") != 1:
            raise RuntimeError(f"upstream foundation {foundation_id} has an invalid repository")
        if not isinstance(foundation.get("pull_request"), int) or foundation["pull_request"] <= 0:
            raise RuntimeError(f"upstream foundation {foundation_id} has an invalid pull request")
        if not isinstance(foundation.get("approved_head"), str) or not re.fullmatch(r"[0-9a-f]{40}", foundation["approved_head"]):
            raise RuntimeError(f"upstream foundation {foundation_id} has an invalid approved head")
        if not isinstance(foundation.get("base_ref"), str) or not foundation["base_ref"]:
            raise RuntimeError(f"upstream foundation {foundation_id} has an invalid base ref")
        if not isinstance(foundation.get("patches"), list) or not foundation["patches"]:
            raise RuntimeError(f"upstream foundation {foundation_id} has no patches")
        for patch in foundation["patches"]:
            allowed_patch_keys = {
                "commit", "stable_patch_id", "subject", "accepted_output_patch_ids", "reviewed_replacement"
            }
            if not {"commit", "stable_patch_id", "subject"} <= set(patch) or not set(patch) <= allowed_patch_keys:
                raise RuntimeError(f"upstream foundation {foundation_id} has malformed patch metadata")
            if not all(isinstance(patch.get(key), str) for key in ("commit", "stable_patch_id", "subject")):
                raise RuntimeError(f"upstream foundation {foundation_id} has malformed patch metadata")
            accepted = patch.get("accepted_output_patch_ids", [])
            if not isinstance(accepted, list) or not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) for value in accepted):
                raise RuntimeError(f"upstream foundation {foundation_id} has malformed accepted output identities")
            replacement = patch.get("reviewed_replacement")
            if replacement is not None:
                if (
                    not isinstance(replacement, dict)
                    or not {"commit", "stable_patch_id"} <= set(replacement)
                    or not set(replacement) <= {"commit", "stable_patch_id", "source_ref"}
                ):
                    raise RuntimeError(f"upstream foundation {foundation_id} has malformed reviewed replacement")
                if not all(
                    isinstance(replacement.get(key), str)
                    and re.fullmatch(r"[0-9a-f]{40}", replacement[key])
                    for key in ("commit", "stable_patch_id")
                ):
                    raise RuntimeError(f"upstream foundation {foundation_id} has malformed reviewed replacement")
                if "source_ref" in replacement and (
                    not isinstance(replacement["source_ref"], str) or not replacement["source_ref"]
                ):
                    raise RuntimeError(f"upstream foundation {foundation_id} has malformed reviewed replacement")
                if replacement["stable_patch_id"] not in accepted:
                    raise RuntimeError(
                        f"upstream foundation {foundation_id} reviewed replacement identity is not accepted"
                    )
    if not isinstance(manifest["components"], list) or not manifest["components"]:
        raise RuntimeError("integration manifest has no required components")
    ids: set[str] = set()
    for component in manifest["components"]:
        if set(component) != {"id", "source_ref", "patches"} or not isinstance(component.get("id"), str) or component["id"] in ids:
            raise RuntimeError("integration manifest has invalid or duplicate component ids")
        ids.add(component["id"])
        if not isinstance(component.get("source_ref"), str) or not isinstance(component.get("patches"), list) or not component["patches"]:
            raise RuntimeError(f"integration component {component['id']} has no source or patches")
        for patch in component["patches"]:
            allowed_patch_keys = {
                "commit", "stable_patch_id", "subject", "accepted_output_patch_ids", "reviewed_replacement"
            }
            if not {"commit", "stable_patch_id", "subject"} <= set(patch) or not set(patch) <= allowed_patch_keys:
                raise RuntimeError(f"integration component {component['id']} has malformed patch metadata")
            if not all(isinstance(patch.get(key), str) for key in ("commit", "stable_patch_id", "subject")):
                raise RuntimeError(f"integration component {component['id']} has malformed patch metadata")
            accepted = patch.get("accepted_output_patch_ids", [])
            if not isinstance(accepted, list) or not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) for value in accepted):
                raise RuntimeError(f"integration component {component['id']} has malformed accepted output identities")
            replacement = patch.get("reviewed_replacement")
            if replacement is not None:
                if (
                    not isinstance(replacement, dict)
                    or not {"commit", "stable_patch_id"} <= set(replacement)
                    or not set(replacement) <= {"commit", "stable_patch_id", "source_ref"}
                ):
                    raise RuntimeError(f"integration component {component['id']} has malformed reviewed replacement")
                if not all(
                    isinstance(replacement.get(key), str)
                    and re.fullmatch(r"[0-9a-f]{40}", replacement[key])
                    for key in ("commit", "stable_patch_id")
                ):
                    raise RuntimeError(f"integration component {component['id']} has malformed reviewed replacement")
                if "source_ref" in replacement and (
                    not isinstance(replacement["source_ref"], str) or not replacement["source_ref"]
                ):
                    raise RuntimeError(f"integration component {component['id']} has malformed reviewed replacement")
                if replacement["stable_patch_id"] not in accepted:
                    raise RuntimeError(
                        f"integration component {component['id']} reviewed replacement identity is not accepted"
                    )
    component_patch_identities = {
        (patch["commit"], patch["stable_patch_id"])
        for component in manifest["components"]
        for patch in component["patches"]
    }
    for foundation in manifest["upstream_foundations"]:
        for patch in foundation["patches"]:
            replacement = patch.get("reviewed_replacement")
            if replacement and (replacement["commit"], replacement["stable_patch_id"]) not in component_patch_identities:
                raise RuntimeError(
                    f"upstream foundation {foundation['id']} reviewed replacement is not a declared component patch"
                )
    for component in manifest["components"]:
        for patch in component["patches"]:
            replacement = patch.get("reviewed_replacement")
            if replacement and (replacement["commit"], replacement["stable_patch_id"]) not in component_patch_identities:
                raise RuntimeError(
                    f"integration component {component['id']} reviewed replacement is not a declared component patch"
                )
    return manifest


MANIFEST = load_manifest()
BRANCH = MANIFEST["integration_branch"]
UPSTREAM_REMOTE = MANIFEST["upstream"]["remote"]
UPSTREAM_REF = MANIFEST["upstream"]["ref"]
REPOSITORY = MANIFEST["fork"]["repository"]
UPSTREAM_FOUNDATIONS = MANIFEST["upstream_foundations"]
FOUNDATION_PATCHES = [patch for foundation in UPSTREAM_FOUNDATIONS for patch in foundation["patches"]]
REQUIRED_PATCHES = [patch for component in MANIFEST["components"] for patch in component["patches"]]

#: U11 canary entry point (``--canary-manifest``): true for the remainder of
#: THIS PROCESS once a canary manifest has been applied. Never true on the
#: sanctioned scheduler path.
CANARY_MANIFEST_ACTIVE = False


def reset_run_canary_state() -> None:
    global CANARY_MANIFEST_ACTIVE
    CANARY_MANIFEST_ACTIVE = False


def apply_canary_manifest(path: str) -> None:
    """U11 canary entry point: swap ``MANIFEST_PATH`` and every
    manifest-derived global for THIS RUN ONLY, before any gate executes.

    Deliberately a DIFFERENT filename than ``hermes-integration-manifest.json``
    under this same directory, so sync.py's ``TRACKED_SET`` (a fixed tuple of
    filenames, defined independently of this module's ``MANIFEST_PATH``
    variable -- see ``sync.py``) never names it: the run-start integrity gate
    stamp-checks the tracked operational manifest copy exactly as before, and
    never touches this file at all (U11's "outside the tracked set, no stamp
    check" requirement holds structurally, not by special-casing the gate).

    Called once, at the very top of ``main()``, before the integrity gate.
    """
    global MANIFEST_PATH, MANIFEST, BRANCH, UPSTREAM_REMOTE, UPSTREAM_REF, REPOSITORY
    global UPSTREAM_FOUNDATIONS, FOUNDATION_PATCHES, REQUIRED_PATCHES, CANARY_MANIFEST_ACTIVE
    MANIFEST_PATH = Path(path)
    MANIFEST = load_manifest()
    BRANCH = MANIFEST["integration_branch"]
    UPSTREAM_REMOTE = MANIFEST["upstream"]["remote"]
    UPSTREAM_REF = MANIFEST["upstream"]["ref"]
    REPOSITORY = MANIFEST["fork"]["repository"]
    UPSTREAM_FOUNDATIONS = MANIFEST["upstream_foundations"]
    FOUNDATION_PATCHES = [patch for foundation in UPSTREAM_FOUNDATIONS for patch in foundation["patches"]]
    REQUIRED_PATCHES = [patch for component in MANIFEST["components"] for patch in component["patches"]]
    CANARY_MANIFEST_ACTIVE = True
    log(f"CANARY_MANIFEST_ACTIVE path={MANIFEST_PATH}")


def emit_fleet_receipt(
    started_at: datetime,
    *,
    outcome: str,
    changed: bool,
    error: str | None = None,
) -> None:
    """Best-effort receipt: Fleet observes this job but never runs or blocks it."""
    try:
        if not FLEET_SCRIPT_DIR.is_dir():
            raise RuntimeError(f"Fleet scripts unavailable: {FLEET_SCRIPT_DIR}")
        if str(FLEET_SCRIPT_DIR) not in sys.path:
            sys.path.insert(0, str(FLEET_SCRIPT_DIR))
        import fleet_receipt  # type: ignore[import-not-found]

        wrote = fleet_receipt.emit_live(
            job_id=FLEET_JOB_ID,
            job_name=FLEET_JOB_NAME,
            started_at=started_at.isoformat(),
            finished_at=datetime.now().astimezone().isoformat(),
            outcome=outcome,
            stage_reached="done",
            artifact=None,
            counters={"changed": int(changed)},
            timing={"integration_release": {"elapsed_s": (datetime.now().astimezone() - started_at).total_seconds()}},
            mirror_intact=None,
            error={"type": "IntegrationReleaseError", "message_tail": error[-500:]} if error else None,
        )
        if not wrote:
            log("WARNING Fleet receipt writer returned false")
    except Exception as exc:  # Receipt failure must not mask the release outcome.
        log(f"WARNING Fleet receipt unavailable: {type(exc).__name__}: {exc}")


def windows_subprocess_kwargs() -> dict[str, Any]:
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        "startupinfo": startupinfo,
    }


def log(message: str) -> None:
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    line = f"{stamp} {message}"
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def emit_stage(stage: str, ok: bool = True, detail: str = "") -> None:
    """Emit one flushed NDJSON progress line (U8/KTD8, R10).

    ``{"ts", "stage", "ok", "detail"}`` on stdout, flushed immediately, plus
    the same payload in the durable file log. The scheduler classifies any
    stdout JSON object carrying a ``"stage"`` key as progress
    (``cron.scheduler._classify_ndjson_stage_line``) and keeps it out of the
    delivered brief, so the final result line this script already prints
    stays byte-compatible with every existing consumer.

    Emission point: a stage line is written when the run ENTERS that stage,
    so a run that dies mid-stage still shows how far it got. ``resolve`` is
    the one exception -- it is emitted on completion, because its detail
    (the parked-pin count) is the whole reason the stage is interesting.
    Failures are reported by the caller as a final ``ok=False`` line.
    """
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "stage": stage,
        "ok": bool(ok),
        "detail": redact_process_output(str(detail))[:800],
    }
    line = json.dumps(payload, ensure_ascii=False)
    print(line, flush=True)
    try:
        log(f"STAGE {line}")
    except Exception:
        # Progress reporting must never be able to fail a release.
        pass


_DRY_RUN_INSPECTION = contextvars.ContextVar("dry_run_inspection", default=False)


def redact_process_output(text: str) -> str:
    """Remove credential-shaped values before process output reaches diagnostics."""
    redacted = re.sub(r"://[^\s/@]+@", "://[REDACTED]@", text)
    redacted = re.sub(
        r"(?i)(\bAuthorization\s*:\s*(?:Bearer|Basic)\s+)[^\s,;]+",
        r"\1[REDACTED]",
        redacted,
    )
    redacted = re.sub(r"(?i)(\b(?:Bearer|Basic)\s+)[^\s,;]+", r"\1[REDACTED]", redacted)
    return re.sub(
        r'''(?ix)
        ((?:"|')?\b(?:password|token|secret|api[_-]?key)\b(?:"|')?
        \s*(?:=|:)\s*)
        (?:"(?:\\.|[^"])*"|'(?:\\.|[^'])*'|[^\s,;\}\]]+)
        ''',
        r"\1[REDACTED]",
        redacted,
    )


def _failure_investigator_settings() -> dict[str, str]:
    """Use configured model settings without creating another user-facing env var."""
    settings = {"profile": "", "model": "", "provider": "", "reasoning_effort": ""}
    config_path = HERMES_HOME / "config.yaml"
    try:
        import yaml
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        configured = config.get("release_failure_investigator", {})
        if not isinstance(configured, dict):
            configured = {}
        settings["profile"] = str(configured.get("profile") or "")
        runtime_config = config
        if settings["profile"]:
            try:
                profile_config_path = HERMES_HOME / "profiles" / settings["profile"] / "config.yaml"
                profile_config = yaml.safe_load(profile_config_path.read_text(encoding="utf-8")) or {}
                if not isinstance(profile_config, dict):
                    raise ValueError("investigator profile config is not a mapping")
                runtime_config = profile_config
            except Exception as profile_exc:
                # A broken or renamed profile must never kill agent dispatch:
                # every incident investigation AND parked-commit resolution
                # brief flows through here (audit hole #2, 2026-08-17). Fall
                # back to the default home, loudly, instead of pointing
                # spawns at an unusable profile.
                log(
                    f"WARNING investigator profile {settings['profile']!r} unusable "
                    f"({type(profile_exc).__name__}); dispatching in default home"
                )
                settings["profile"] = ""
                runtime_config = config
        model_config = runtime_config.get("model", {}) if isinstance(runtime_config.get("model"), dict) else {}
        agent_config = runtime_config.get("agent", {}) if isinstance(runtime_config.get("agent"), dict) else {}
        settings["model"] = str(configured.get("model") or model_config.get("default") or "")
        settings["provider"] = str(configured.get("provider") or model_config.get("provider") or "")
        settings["reasoning_effort"] = str(configured.get("reasoning_effort") or agent_config.get("reasoning_effort") or "")
    except Exception:
        pass
    return settings


def _failure_investigator_module() -> Any:
    spec = importlib.util.spec_from_file_location("hermes_release_failure_investigator", FAILURE_INVESTIGATOR_SCRIPT)
    if not spec or not spec.loader:
        raise RuntimeError("release failure investigator is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def launch_failure_investigator(*, stage: str, error: str) -> None:
    """Best-effort only: no investigator error may alter release failure truth."""
    try:
        investigator = _failure_investigator_module()
        result = investigator.record_failure(
            job_id=FLEET_JOB_ID,
            stage=stage,
            error=redact_process_output(error),
            canary=CANARY_MANIFEST_ACTIVE,
            home=HERMES_HOME,
            worktree=WORKTREE,
            script_path=Path(__file__).resolve(),
            log_path=LOG_PATH,
            test_path=SCRIPT_DIR / "test_hermes_integration_release_windows.py",
            manifest_path=MANIFEST_PATH,
            investigator=_failure_investigator_settings(),
        )
        investigator.maybe_launch_investigator(result)
        log(f"FAILURE_INVESTIGATOR signature={result['signature']} spawned={str(bool(result['spawn'])).lower()} occurrences={result['occurrences']}")
    except Exception as investigator_exc:
        log(f"WARNING failure investigator unavailable: {type(investigator_exc).__name__}")


# ── U9/KTD12/R20: privileged-action authority gate ──────────────────────────

#: Refusals recorded by ``require_authority()`` this run. ``fail()`` folds
#: them into the result JSON so the refusal reaches the delivered brief and
#: the cron run doc, not just the log.
AUTHORITY_REFUSALS: list[dict[str, Any]] = []
#: Granted privileged actions (audit trail in the log + result JSON).
AUTHORITY_GRANTS: list[dict[str, Any]] = []
#: The token path this run was started with (``--authority-token``).
AUTHORITY_TOKEN_PATH: str | None = None


class AuthorityRefused(RuntimeError):
    """A privileged action was refused by the authority gate.

    Raised (not ``fail()``ed) so the normal failure path still runs: the
    pre-push restoration, the incident record, and the Fleet receipt all
    behave exactly as they do for any other refusal.
    """

    def __init__(self, refusal: dict[str, Any]) -> None:
        super().__init__(f"privileged action refused by authority gate: {refusal.get('reason')}")
        self.refusal = refusal


def reset_run_authority_state(token_path: str | None = None) -> None:
    global AUTHORITY_TOKEN_PATH
    AUTHORITY_REFUSALS.clear()
    AUTHORITY_GRANTS.clear()
    AUTHORITY_TOKEN_PATH = token_path


def require_authority(action: str, *, holder: str, token_path: str | None) -> dict[str, Any]:
    """Gate one privileged action (``push`` / ``publish``) in code (R20).

    Called immediately before EACH privileged action, never once per run, so
    a window that expires between the push and the publish refuses the
    publish.

    - ``--holder scheduler`` (the default, i.e. the nightly cron run) is the
      sanctioned automated path and needs no token.
    - Any other holder -- an investigator session, a manual operator run --
      must present a spawner-minted token that parses, names this job, is
      unexpired at THIS moment, allows THIS action, and whose recomputed
      digest matches the digest the spawner recorded in the (still open)
      incident entry.  Every failure is a named refusal.

    HONEST LIMIT (R20, same wording as the plan): ambient same-user git/gh
    credentials mean file-based controls bound accidents and drift, not a
    fully hostile in-context agent -- nothing here stops a process on this
    account from passing ``--holder scheduler`` or writing its own incident
    record.  What it does buy: an investigator that follows its contract
    cannot push or publish outside a window a NON-AGENT process opened for
    the incident it was spawned for, and a stale or superseded finisher's
    authority dies the moment its incident is closed.  The enforcement point
    outside this host is GitHub branch protection on the published branch
    (deferred, user-owned).
    """
    holder_label = (holder or "scheduler").strip() or "scheduler"
    if holder_label == "scheduler":
        grant = {"ok": True, "action": action, "holder": holder_label, "reason": "scheduler_holder"}
        AUTHORITY_GRANTS.append(grant)
        log(f"AUTHORITY_OK action={action} holder={holder_label} reason=scheduler_holder")
        return grant
    try:
        verdict = dict(_failure_investigator_module().verify_authority(
            token_path=token_path,
            job_id=FLEET_JOB_ID,
            action=action,
            home=HERMES_HOME,
        ))
    except Exception as exc:
        # The verifier itself failing is a refusal, never a bypass.
        verdict = {"ok": False, "reason": "authority_verifier_unavailable", "detail": type(exc).__name__}
    verdict.update({"action": action, "holder": holder_label})
    if token_path:
        verdict.setdefault("token_path", str(token_path))
    if not verdict.get("ok"):
        AUTHORITY_REFUSALS.append(verdict)
        log("AUTHORITY_REFUSED " + json.dumps(verdict, sort_keys=True, default=str))
        emit_stage(f"authority_{action}", ok=False, detail=str(verdict.get("reason")))
        raise AuthorityRefused(verdict)
    AUTHORITY_GRANTS.append(verdict)
    log("AUTHORITY_OK " + json.dumps(verdict, sort_keys=True, default=str))
    return verdict


def resolve_failure_investigator_success() -> None:
    """A real successful return closes all open dedupe keys for the job."""
    try:
        _failure_investigator_module().resolve_success(FLEET_JOB_ID, HERMES_HOME)
    except Exception:
        # Success remains quiet even if a local diagnostic file cannot be updated.
        pass


def _sync_module() -> Any:
    """Load sync.py by path (U2/KTD2): same pattern as
    ``_failure_investigator_module()`` -- the operational directory is flat
    (no package ``__init__``), so a normal import statement cannot reach a
    sibling module there. ``sync.py`` itself is a tracked file (see its
    module docstring's TRACKED_SET rationale), so this always resolves
    alongside this script in both the in-repo and operational locations.
    """
    spec = importlib.util.spec_from_file_location("fork_integration_sync", SCRIPT_DIR / "sync.py")
    if not spec or not spec.loader:
        raise RuntimeError("sync module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def integration_scripts_integrity_check(*, dry_run: bool) -> dict[str, Any]:
    """Run-start integrity gate (U2/KTD2, R14).

    Verifies the operational copies at ``HERMES_HOME/scripts`` against their
    release-system source tree (via the ``WORKTREE`` clone) BEFORE mutation --
    called immediately after argument parsing, before the exclusive lock,
    before any fetch.

    - No stamp at all (pre-U2 bootstrap: sync.py has never deployed
      anything into this environment yet) is tolerated with a single
      warning, not a failure -- there is no prior verified generation to
      have drifted from.
    - Any other ``verify()`` failure (hash mismatch, unreachable stamped
      sha, a tracked file missing from disk or from the tree) fails closed
      on a real run: refuse before touching the worktree, the lock, or
      GitHub. The only remedy is ``python sync.py deploy ...``; there is
      deliberately no bypass flag (KTD2: "never a bypass assertion").
    - Under ``--dry-run`` the mismatch is folded into the dry-run report
      instead of raising, since dry-run must stay read-only and must not
      abort the rest of the (harmless) inspection.
    """
    try:
        result = _sync_module().verify(HERMES_HOME / "scripts", WORKTREE)
    except Exception as exc:
        # The gate itself must fail closed, not silently pass, if it
        # cannot even run (e.g. sync.py missing or broken).
        result = {"ok": False, "reason": "integrity_check_unavailable", "error": str(exc)}
    if not result.get("ok") and result.get("reason") == "no_stamp":
        log("WARNING operational copies have no sync stamp yet (pre-U2 bootstrap); continuing without integrity verification")
        return result
    if not result.get("ok") and not dry_run:
        fail(
            "operational copies fail integrity check; run `sync.py deploy` "
            f"to restore a verified generation: {json.dumps(result, sort_keys=True, default=str)}"
        )
    return result


def _best_effort_sync_log(message: str) -> None:
    """Keep post-publish diagnostics from changing the release outcome."""
    try:
        log(message)
    except Exception:
        pass


def sync_operational_copies(
    release_system_source_sha: str | None, published_product_sha: str,
) -> dict[str, Any]:
    """Deploy the verified release-system source after a product publish.

    ``release_system_source_sha`` identifies the tree containing the tracked
    operational files; ``published_product_sha`` is distinct release context.

    Best-effort by design: the release itself has already succeeded, been
    pushed, and had its checksum verified by the time this runs, so a sync
    failure here must not retroactively fail (or roll back) a genuinely
    successful release. It is instead reported honestly in the result
    JSON's ``"sync"`` key and logged loudly -- the next run's
    ``integration_scripts_integrity_check()`` fails closed on the stale
    operational copies until a human runs ``sync.py deploy`` (or the next
    successful publish supersedes it).
    """
    if not release_system_source_sha:
        error = "verified release-system source SHA unavailable"
        _best_effort_sync_log(
            "WARNING post-publish sync refused: "
            f"source_sha={release_system_source_sha} "
            f"published_product_sha={published_product_sha} error={error}"
        )
        return {
            "ok": False,
            "error": error,
            "source_sha": release_system_source_sha,
            "published_product_sha": published_product_sha,
        }
    try:
        outcome = _sync_module().sync(
            release_system_source_sha, WORKTREE, HERMES_HOME / "scripts"
        )
        _best_effort_sync_log(
            "POST_PUBLISH_SYNC ok=true "
            f"source_sha={release_system_source_sha} "
            f"published_product_sha={published_product_sha}"
        )
        return {
            "ok": True,
            **outcome,
            "source_sha": release_system_source_sha,
            "published_product_sha": published_product_sha,
        }
    except Exception as exc:
        _best_effort_sync_log(
            "WARNING post-publish sync failed: "
            f"source_sha={release_system_source_sha} "
            f"published_product_sha={published_product_sha} "
            f"{type(exc).__name__}: {exc}"
        )
        return {
            "ok": False,
            "error": str(exc),
            "source_sha": release_system_source_sha,
            "published_product_sha": published_product_sha,
        }


# ── U6: reconciliation proposals, blocklist, park-and-continue ──────────────

_PROPOSALS_MODULE: Any = None
#: Pins parked by this run: ``{"pin_id", "pin_kind", "proposal_id",
#: "churn_livelock", "subject", ...}``. Reset at run start by
#: ``reset_run_reconciliation_state()`` and reported in the result JSON.
PARKED_PINS: list[dict[str, Any]] = []
_BLOCKLIST_CACHE: set[str] | None = None
PARKED_PROVENANCE_PREFIX = "pin-parked-pending-proposal"


def _proposals_module() -> Any:
    """Load proposals.py by path and cache it (same flat-directory reason as
    ``_sync_module()``; ``proposals.py`` is a tracked file)."""
    global _PROPOSALS_MODULE
    if _PROPOSALS_MODULE is None:
        spec = importlib.util.spec_from_file_location("fork_integration_proposals", PROPOSALS_SCRIPT)
        if not spec or not spec.loader:
            raise RuntimeError("proposals module is unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _PROPOSALS_MODULE = module
    return _PROPOSALS_MODULE


def reset_run_reconciliation_state() -> None:
    """Clear per-run proposal state so one process never reports another
    run's parked pins (and re-reads a blocklist edited between runs)."""
    global _BLOCKLIST_CACHE
    PARKED_PINS.clear()
    _BLOCKLIST_CACHE = None


def blocklisted_patch_ids() -> set[str]:
    """Patch identities that may never count as equivalent or be proposed (R3).

    Fails closed on a corrupt blocklist: silently treating it as empty would
    let a human-rejected rewrite be absorbed on the next run. An absent file
    is legitimately empty.
    """
    global _BLOCKLIST_CACHE
    if _BLOCKLIST_CACHE is None:
        _BLOCKLIST_CACHE = set(_proposals_module().blocklisted_patch_ids(BLOCKLIST_PATH))
    return _BLOCKLIST_CACHE


def pin_identity(patch: dict[str, Any]) -> tuple[str, str]:
    """Map a flat manifest patch back to its ``(kind, container id)`` pin.

    ``patch_resolution`` receives foundation/component patches as one flat
    list, but a proposal must name the pin a human would edit. Resolved
    live against the loaded manifest so tests that swap
    ``REQUIRED_PATCHES``/``FOUNDATION_PATCHES`` still get a stable answer.
    """
    key = (str(patch.get("commit")), str(patch.get("stable_patch_id")))
    index = _proposals_module().manifest_pin_index(MANIFEST)
    return index.get(key, ("component", "unindexed"))


def park_pin_for_churn(
    patch: dict[str, Any], *, search_ref: str, upstream_tip: str,
) -> dict[str, Any] | None:
    """Generate-or-refresh this pin's proposal and record the park (KTD13).

    Returns the parked-pin note, or ``None`` when nothing qualifies. NEVER
    raises into the release path: a proposal is evidence for a human, and
    evidence machinery that failed must not be able to skip a nightly (R19).
    The park note itself is still recorded on failure, honestly carrying the
    error, because the run genuinely did continue past a churn signal.
    """
    kind, pin_id = "component", "unindexed"
    pin = {
        "kind": kind,
        "id": pin_id,
        "commit": str(patch["commit"]),
        "stable_patch_id": str(patch["stable_patch_id"]),
        "subject": str(patch["subject"]),
    }
    try:
        kind, pin_id = pin_identity(patch)
        pin.update({"kind": kind, "id": pin_id})
        proposals = _proposals_module()
        git_repo = proposals.Git(WORKTREE)
        detected = proposals.detect_candidates(
            git_repo, pin,
            search_ref=search_ref,
            upstream_tip=upstream_tip,
            blocked=blocklisted_patch_ids(),
            accepted_patch_ids=_accepted_output_patch_ids(patch),
            patch_id_of=stable_patch_id,
        )
        if not detected["candidates"]:
            return None
        artifact = proposals.generate_or_refresh(
            proposals.ProposalStore(), git_repo, pin=pin,
            candidates=detected["candidates"],
            low_confidence=detected["low_confidence"],
            upstream_ref=f"{UPSTREAM_REMOTE}/{UPSTREAM_REF.removeprefix('refs/heads/')}",
            upstream_tip=upstream_tip,
        )
        note = {
            "pin_id": pin_id,
            "pin_kind": kind,
            "source_commit": pin["commit"],
            "subject": pin["subject"],
            "proposal_id": artifact["id"],
            "state": artifact["state"],
            "evidence": artifact["evidence"],
            "regen_count": artifact["regen_count"],
            "churn_livelock": bool(artifact["churn_livelock"]),
            "low_confidence": bool(artifact["low_confidence"]),
            "artifact_sha256": artifact["artifact_sha256"],
        }
        log(
            f"PIN_PARKED pin={pin_id} proposal={artifact['id']} state={artifact['state']} "
            f"evidence={artifact['evidence']} regen={artifact['regen_count']} "
            f"churn_livelock={str(note['churn_livelock']).lower()}"
        )
    except Exception as exc:
        note = {
            "pin_id": pin_id,
            "pin_kind": kind,
            "source_commit": pin["commit"],
            "subject": pin["subject"],
            "proposal_id": None,
            "state": "unavailable",
            "churn_livelock": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        log(f"WARNING proposal generation failed for pin={pin_id}: {type(exc).__name__}: {exc}")
    PARKED_PINS.append(note)
    return note


def parked_pin_provenance(patch: dict[str, Any]) -> str | None:
    """``pin-parked-pending-proposal:<id>`` for a pin parked by this run."""
    commit = str(patch.get("commit"))
    for note in PARKED_PINS:
        if note.get("source_commit") == commit and note.get("proposal_id"):
            return f"{PARKED_PROVENANCE_PREFIX}:{note['proposal_id']}"
    return None


def parked_pin_summary() -> list[dict[str, Any]]:
    """The result-JSON shape for this run's parked pins (R19)."""
    return [
        {
            "pin_id": note.get("pin_id"),
            "proposal_id": note.get("proposal_id"),
            "churn_livelock": bool(note.get("churn_livelock")),
        }
        for note in PARKED_PINS
    ]


def open_proposal_summary() -> list[dict[str, Any]]:
    """Parked pins as the ARTIFACT STORE currently sees them.

    Used by the read-only dry-run report, which never runs resolution and so
    has no per-run parked list of its own. Best-effort: an unreadable store
    must not fail an otherwise clean inspection.
    """
    try:
        artifacts = _proposals_module().ProposalStore().list_open()
    except Exception:
        return []
    return [
        {
            "pin_id": artifact.get("pin", {}).get("id"),
            "proposal_id": artifact.get("id"),
            "churn_livelock": bool(artifact.get("churn_livelock")),
        }
        for artifact in artifacts
    ]


# ── U7/R7/KTD14: every-run provenance wiring + retirement bridge (U6) ───────

#: This run's fetched/resolved upstream ref (a SHA once ``upstream`` in
#: main() is set; otherwise the local tracking ref, best-effort). Read by
#: ``_fold_provenance()``/``generate_retirement_proposals()`` so provenance
#: derivation can use "this run's fetched upstream when available" without
#: threading it through every call site (R7's own wording; ``derive()``
#: tolerates a stale/unresolvable ref -- see ledger.py's module docstring).
_RUN_UPSTREAM_REF: str | None = None

_LEDGER_MODULE: Any = None


def _ledger_module() -> Any:
    """Load ledger.py by path and cache it (same flat-directory reason as
    ``_sync_module()``/``_proposals_module()``; ``ledger.py`` is a tracked
    file)."""
    global _LEDGER_MODULE
    if _LEDGER_MODULE is None:
        spec = importlib.util.spec_from_file_location("fork_integration_ledger", SCRIPT_DIR / "ledger.py")
        if not spec or not spec.loader:
            raise RuntimeError("ledger module is unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _LEDGER_MODULE = module
    return _LEDGER_MODULE


def reset_run_provenance_state() -> None:
    global _RUN_UPSTREAM_REF
    _RUN_UPSTREAM_REF = None


def record_run_upstream_ref(ref: str) -> None:
    """Called once ``main()`` resolves this run's fetched upstream SHA."""
    global _RUN_UPSTREAM_REF
    _RUN_UPSTREAM_REF = ref


def _fallback_upstream_ref() -> str:
    """Best-effort upstream ref when this run never got far enough to fetch
    (an early integrity-gate/identity failure): the local tracking ref from
    the manifest, unresolved and possibly stale. ``ledger.derive()`` degrades
    gracefully on every git failure this can cause (see its module
    docstring) -- it never raises, so a stale ref only means fewer resolved
    candidates this run, never a broken run."""
    return f"{UPSTREAM_REMOTE}/{UPSTREAM_REF.removeprefix('refs/heads/')}"


# ── U6 (retirement bridge): a pin absorbed-verbatim for several consecutive
# runs, still an ancestor of the live upstream tip, retires through the SAME
# reviewed state machine churn proposals use (R4's "never a parallel
# mechanism", applied to removal). ──────────────────────────────────────────


def generate_retirement_proposals(*, upstream_ref: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """For each ``ledger.retirement_candidates()`` pin id, generate (or
    refresh) a retire-pin proposal via ``proposals.generate_or_refresh_retirement``.

    Best-effort and NEVER raises into the release path (KTD7: provenance and
    its retirement bridge report, they do not gate) -- one bad pin's failure
    is logged and skipped, never breaking the rest.
    """
    generated: list[dict[str, Any]] = []
    try:
        ledger = _ledger_module()
        history_path = ledger.default_history_path()
        candidate_pin_ids = ledger.retirement_candidates(history_path, repo_dir=WORKTREE, upstream_ref=upstream_ref)
        if not candidate_pin_ids:
            return generated
        records_by_pin = {record["pin_id"]: record for record in records}
        proposals = _proposals_module()
        store = proposals.ProposalStore()
        git_repo = proposals.Git(WORKTREE)
        for pin_id in candidate_pin_ids:
            record = records_by_pin.get(pin_id)
            if not record:
                continue
            try:
                kind, container_id, _commit = pin_id.split(":", 2)
            except ValueError:
                log(f"WARNING retirement bridge: unparseable pin_id={pin_id!r}")
                continue
            pin = {
                "kind": kind, "id": container_id, "commit": record["commit"],
                "stable_patch_id": record["stable_patch_id"], "subject": record["subject"],
            }
            try:
                artifact = proposals.generate_or_refresh_retirement(
                    store, git_repo, pin=pin, evidence=record.get("evidence") or {},
                    upstream_ref=upstream_ref, upstream_tip=upstream_ref,
                )
                generated.append({
                    "pin_id": pin_id, "proposal_id": artifact["id"], "state": artifact["state"],
                    "refreshed": bool(artifact.get("refreshed")),
                })
                log(f"RETIREMENT_PROPOSED pin={pin_id} proposal={artifact['id']} state={artifact['state']}")
            except Exception as exc:
                log(f"WARNING retirement proposal generation failed for pin={pin_id}: {type(exc).__name__}: {exc}")
    except Exception as exc:
        log(f"WARNING retirement bridge unavailable: {type(exc).__name__}: {exc}")
    return generated


def _fold_provenance(payload: dict[str, Any], *, dry_run: bool) -> None:
    """R7 (every run, any outcome) / KTD14 (folded into the single aggregated
    run-summary): derive ground-truth provenance and fold it into the
    result JSON. NEVER lets a ledger failure break the run -- caught,
    logged, and reported as ``provenance: {"error": ...}`` (KTD7: report,
    never gate).

    Dry-run derives (so the report is visible) but never appends to the
    JSONL history and never generates retirement proposals -- both are
    mutations, and dry-run stays read-only.
    """
    try:
        ledger = _ledger_module()
        upstream_ref = _RUN_UPSTREAM_REF or _fallback_upstream_ref()
        history_path = ledger.default_history_path()
        records = ledger.derive(
            manifest_path=MANIFEST_PATH, repo_dir=WORKTREE, upstream_ref=upstream_ref, blocklist_path=BLOCKLIST_PATH,
        )
        transitions = ledger.diff_vs_previous(records, history_path)
        if not dry_run:
            try:
                manifest_sha256 = sha256(MANIFEST_PATH)
            except OSError:
                manifest_sha256 = None
            ledger.append_history(records, history_path, manifest_sha256=manifest_sha256, upstream_tip=upstream_ref)
        retiring = ledger.retirement_candidates(history_path, repo_dir=WORKTREE, upstream_ref=upstream_ref)
        provenance: dict[str, Any] = {
            "states": {record["pin_id"]: record["state"] for record in records},
            "transitions": transitions,
            "retirement_candidates": retiring,
        }
        if not dry_run and retiring:
            provenance["retirement_proposals"] = generate_retirement_proposals(
                upstream_ref=upstream_ref, records=records,
            )
        payload["provenance"] = provenance
    except Exception as exc:
        payload["provenance"] = {"error": f"{type(exc).__name__}: {exc}"}
        log(f"WARNING provenance derivation failed: {type(exc).__name__}: {exc}")


def _fold_canary_flag(payload: dict[str, Any]) -> None:
    if CANARY_MANIFEST_ACTIVE:
        payload["canary"] = True


def _emit_result(payload: dict[str, Any], *, dry_run: bool, exit_code: int = 0) -> int:
    """Single choke point for every SUCCESSFUL (non-``fail()``) exit from
    ``main()`` -- the dry-run report and every non-dry-run success return.
    Folds provenance (R7) and the canary flag (U11) into the result JSON
    before printing, so every outcome carries them (``fail()`` does the same
    for the failure path, below)."""
    _fold_provenance(payload, dry_run=dry_run)
    _fold_canary_flag(payload)
    print(json.dumps(payload, ensure_ascii=False))
    return exit_code


@contextmanager
def dry_run_inspection_logging_disabled() -> Any:
    """Prevent checked command failures during dry-run inspection from being logged."""
    token = _DRY_RUN_INSPECTION.set(True)
    try:
        yield
    finally:
        _DRY_RUN_INSPECTION.reset(token)


def fail(message: str, *, code: int = 1) -> None:
    log(f"ERROR {message}")
    payload: dict[str, Any] = {"ok": False, "error": message}
    # U9/R20: a refused privileged action must be visible wherever the run's
    # outcome is read (delivered brief, cron run doc, Fleet receipt consumer),
    # not only in the durable log. The signature is deliberately unchanged so
    # every existing caller and test spy keeps working.
    if AUTHORITY_REFUSALS:
        payload["authority_refusals"] = list(AUTHORITY_REFUSALS)
    # R7/KTD7: every run, any outcome, including every failure/abort path --
    # fail() is the single choke point every one of them already passes
    # through. dry_run=False is always correct here: fail() is never called
    # from the dry-run branch (integration_scripts_integrity_check() only
    # calls it when dry_run is False; the dry-run branch composes and
    # returns its own payload without going through fail()).
    _fold_provenance(payload, dry_run=False)
    _fold_canary_flag(payload)
    print(json.dumps(payload, ensure_ascii=False))
    raise SystemExit(code)


def write_reconstruction_review_request(upstream: str, patch: dict[str, str] | None, error: Exception) -> Path:
    """Persist a two-reviewer Opus brief for a conflict the job could not prove-resolve."""
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    failed = patch or {"commit": "unknown", "subject": "unknown"}
    review_path = REVIEW_DIR / f"reconstruction-{datetime.now():%Y%m%d-%H%M%S}.json"
    prompt = (
        "Read-only adversarial review of a Hermes integration reconstruction conflict. "
        "Do not edit, reset, cherry-pick, push, create releases, or run other mutating commands. "
        "Compare the upstream base, the failed source patch, its regression tests, and relevant runtime behavior. "
        "State whether the patch is already equivalent upstream, a minimal safe resolution, or must remain blocked. "
        "This is one of two independent reviews; a human must reconcile them before any release."
    )
    review_path.write_text(json.dumps({
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "review_required",
        "upstream": upstream,
        "failed_patch": failed,
        "error": str(error),
        "worktree": str(WORKTREE),
        "reviewer_instruction": prompt,
        "suggested_claude_command": (
            f'claude -p {json.dumps(prompt)} --model opus --effort high '
            '--allowedTools "Read,Bash" --max-turns 8 --output-format json'
        ),
    }, indent=2) + "\n", encoding="utf-8")
    log(f"RECONSTRUCTION_REVIEW_REQUIRED path={review_path} failed_patch={failed['commit']}")
    return review_path


def resolve_executable(name: str) -> str:
    """Resolve Windows command shims even when the scheduler has a minimal PATH."""
    explicit = Path(name)
    if explicit.is_file():
        return str(explicit)
    if name == "npm" and sys.platform == "win32":
        candidate = Path(os.environ.get("ProgramFiles", r"C:\\Program Files")) / "nodejs" / "npm.cmd"
        if candidate.is_file():
            return str(candidate)
    if name == "cargo" and sys.platform == "win32":
        candidate = HOME / ".cargo" / "bin" / "cargo.exe"
        if candidate.is_file():
            return str(candidate)
    discovered = shutil.which(name)
    if discovered:
        return discovered
    raise FileNotFoundError(f"required executable is unavailable: {name}")


def run(
    *args: str,
    cwd: Path = WORKTREE,
    timeout: int = 900,
    check: bool = True,
    extra_env: dict[str, str] | None = None,
    log_failure: bool | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [resolve_executable(args[0]), *args[1:]]
    env = os.environ.copy()
    if _DRY_RUN_INSPECTION.get() and args[0] == "git":
        env["GIT_OPTIONAL_LOCKS"] = "0"
    if sys.platform == "win32":
        cargo_bin = str(HOME / ".cargo" / "bin")
        env["PATH"] = cargo_bin + os.pathsep + env.get("PATH", "")
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        command,
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=env,
        **windows_subprocess_kwargs(),
    )
    if check and result.returncode:
        # Keep reports useful but never print a credential-bearing command.
        # The final lines usually hold compiler/test failures; persist them in
        # the durable cron log so the one-line scheduler receipt stays short.
        raw_detail = ((result.stdout or "") + "\n" + (result.stderr or "")).strip().replace("\r", "")
        redacted_detail = redact_process_output(raw_detail)
        detail = redacted_detail.replace("\n", " ")[-1200:]
        if log_failure is None:
            log_failure = not _DRY_RUN_INSPECTION.get()
        if log_failure:
            log(f"COMMAND_FAILURE executable={args[0]} exit={result.returncode} tail={redacted_detail[-6000:]}")
        raise RuntimeError(f"{args[0]} failed ({result.returncode}): {detail}")
    return result


def git(*args: str, timeout: int = 900, check: bool = True, log_failure: bool | None = None) -> str:
    # Never replay a previous manual conflict resolution in this automated
    # force-push/release workflow.
    command = ("git", "-c", "rerere.enabled=false", "-c", "rerere.autoupdate=false", *args)
    try:
        return run(
            *command, timeout=timeout, check=check, log_failure=log_failure,
        ).stdout.strip()
    except RuntimeError as exc:
        detail = str(exc)
        if (
            not check
            or "index.lock" not in detail
            or "File exists" not in detail
            or _read_lock_info().get("pid") != os.getpid()
        ):
            raise
        # A completed git child cannot still own its failed lock.  Retrying is
        # safe only while this process owns the release lock, which excludes
        # every other sanctioned user of the scheduler worktree.
        _clear_stale_worktree_index_lock()
        log(f"retrying git command after reclaiming stale index.lock: {args[0] if args else '<none>'}")
        return run(
            *command, timeout=timeout, check=check, log_failure=log_failure,
        ).stdout.strip()


def public_github_json(url: str) -> dict[str, Any]:
    """Read public GitHub metadata without exposing or requiring credentials."""
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "hermes-integration-foundation-check"},
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not verify upstream foundation metadata at {url}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"upstream foundation metadata was not an object: {url}")
    return payload


def verify_upstream_foundations() -> list[dict[str, Any]]:
    """Fetch and verify pinned upstream PRs without requiring merge status."""
    verified: list[dict[str, Any]] = []
    for foundation in UPSTREAM_FOUNDATIONS:
        repository = foundation["repository"]
        number = foundation["pull_request"]
        payload = public_github_json(f"https://api.github.com/repos/{repository}/pulls/{number}")
        actual_head = payload.get("head", {}).get("sha")
        actual_base_repo = payload.get("base", {}).get("repo", {}).get("full_name")
        actual_base_ref = payload.get("base", {}).get("ref")
        if actual_head != foundation["approved_head"]:
            raise RuntimeError(
                f"upstream foundation {foundation['id']} PR #{number} head changed: "
                f"approved={foundation['approved_head']}, actual={actual_head}"
            )
        if actual_base_repo != repository or actual_base_ref != foundation["base_ref"]:
            raise RuntimeError(
                f"upstream foundation {foundation['id']} PR #{number} targets "
                f"{actual_base_repo}:{actual_base_ref}, expected {repository}:{foundation['base_ref']}"
            )
        local_ref = f"refs/remotes/{UPSTREAM_REMOTE}/pr/{number}"
        git("fetch", UPSTREAM_REMOTE, f"+refs/pull/{number}/head:{local_ref}", timeout=300)
        fetched_head = git("rev-parse", local_ref)
        if fetched_head != foundation["approved_head"]:
            raise RuntimeError(
                f"upstream foundation {foundation['id']} PR #{number} fetched head changed: "
                f"approved={foundation['approved_head']}, fetched={fetched_head}"
            )
        for patch in foundation["patches"]:
            commit = patch["commit"]
            if git("cat-file", "-t", commit, check=False) != "commit":
                raise RuntimeError(f"upstream foundation patch is unavailable: {foundation['id']} ({commit})")
            if stable_patch_id(commit) != patch["stable_patch_id"]:
                raise RuntimeError(f"upstream foundation patch identity changed: {foundation['id']} ({commit})")
            if run("git", "merge-base", "--is-ancestor", commit, fetched_head, timeout=60, check=False).returncode:
                raise RuntimeError(f"upstream foundation patch is not reachable from PR head: {foundation['id']} ({commit})")
        verified.append({
            "id": foundation["id"],
            "pull_request": number,
            "approved_head": actual_head,
            "state": payload.get("state"),
            "merged": bool(payload.get("merged_at")),
        })
    return verified


def stable_patch_id(ref: str) -> str:
    """Return Git's whitespace-insensitive identity for one required patch."""
    patch = run("git", "show", "--format=", "--binary", ref, timeout=120).stdout
    result = subprocess.run(
        [resolve_executable("git"), "patch-id", "--stable"],
        input=patch,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
        **windows_subprocess_kwargs(),
    )
    if result.returncode or not result.stdout.strip():
        raise RuntimeError(f"could not calculate stable patch identity for {ref}")
    return result.stdout.split()[0]


def source_proves_patch(source_ref: str, patch: dict[str, str]) -> bool:
    """Accept an exact source commit or its same-subject stable-patch rewrite."""
    commit = patch["commit"]
    if not run("git", "merge-base", "--is-ancestor", commit, source_ref, timeout=300, check=False).returncode:
        return True
    candidates = [
        candidate for candidate in git(
            "log", source_ref, "--format=%H", "--fixed-strings", f"--grep={patch['subject']}", check=False
        ).splitlines() if candidate
    ]
    return any(stable_patch_id(candidate) == patch["stable_patch_id"] for candidate in candidates)


def verify_manifest_sources() -> None:
    """Fail before mutation if a mandatory source ref no longer proves its patches."""
    for component in MANIFEST["components"]:
        source_ref = component["source_ref"]
        if source_ref.startswith("fork/"):
            remote_ref = "refs/heads/" + source_ref.removeprefix("fork/")
            resolved = git("ls-remote", FORK_REMOTE, remote_ref).split()
            if len(resolved) < 2:
                raise RuntimeError(f"mandatory component source is unavailable: {component['id']} ({source_ref})")
            # The scheduler clone is intentionally shallow/partial; materialize
            # the declared source ref before asking Git ancestry questions.
            git("fetch", FORK_REMOTE, f"+{remote_ref}:refs/remotes/{source_ref}", timeout=300)
        for patch in component["patches"]:
            commit = patch["commit"]
            if git("cat-file", "-t", commit, check=False) != "commit":
                raise RuntimeError(f"mandatory component patch is unavailable: {component['id']} ({commit})")
            if stable_patch_id(commit) != patch["stable_patch_id"]:
                raise RuntimeError(f"mandatory component patch identity changed: {component['id']} ({commit})")
            if source_ref.startswith("fork/") and not source_proves_patch(source_ref, patch):
                raise RuntimeError(f"mandatory patch is not reachable from its source: {component['id']} ({commit})")
            replacement = patch.get("reviewed_replacement")
            if replacement is not None:
                replacement_commit = replacement["commit"]
                if git("cat-file", "-t", replacement_commit, check=False) != "commit":
                    raise RuntimeError(
                        f"reviewed component replacement patch is unavailable: {component['id']} ({replacement_commit})"
                    )
                if stable_patch_id(replacement_commit) != replacement["stable_patch_id"]:
                    raise RuntimeError(
                        f"reviewed component replacement patch identity changed: {component['id']} ({replacement_commit})"
                    )
                replacement_source = replacement.get("source_ref", source_ref)
                if replacement_source.startswith("fork/"):
                    remote_ref = "refs/heads/" + replacement_source.removeprefix("fork/")
                    resolved = git("ls-remote", FORK_REMOTE, remote_ref).split()
                    if len(resolved) < 2:
                        raise RuntimeError(
                            f"reviewed component replacement source is unavailable: {component['id']} ({replacement_source})"
                        )
                    git("fetch", FORK_REMOTE, f"+{remote_ref}:refs/remotes/{replacement_source}", timeout=300)
                if replacement_source.startswith("fork/") and not source_proves_patch(replacement_source, replacement):
                    raise RuntimeError(
                        f"reviewed component replacement is not reachable from its source: "
                        f"{component['id']} ({replacement_commit})"
                    )

    for foundation in UPSTREAM_FOUNDATIONS:
        for patch in foundation["patches"]:
            replacement = patch.get("reviewed_replacement")
            if replacement is None:
                continue
            commit = replacement["commit"]
            if git("cat-file", "-t", commit, check=False) != "commit":
                raise RuntimeError(
                    f"reviewed foundation replacement patch is unavailable: {foundation['id']} ({commit})"
                )
            if stable_patch_id(commit) != replacement["stable_patch_id"]:
                raise RuntimeError(
                    f"reviewed foundation replacement patch identity changed: {foundation['id']} ({commit})"
                )


def patch_resolution(
    upstream: str,
    patches: list[dict[str, str]],
    *,
    records: list[dict[str, Any]] | None = None,
    upstream_tip: str | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Split required patches into patches to apply and exact upstream equivalents.

    An upstream commit is accepted as absorbed only when both its subject and
    stable patch identity match the manifest source, and only when that
    identity is not blocklisted (R3: a human-rejected or superseded rewrite
    never counts as equivalent again).

    A matching subject with a DIFFERENT patch used to be a hard refusal
    ("manual reconciliation is required"), which meant upstream churn alone
    skipped the nightly. It is now park-and-continue (KTD13/R19): the run
    generates or refreshes a reconciliation proposal for a human, records the
    parked pin, and then resolves exactly as if no candidate existed -- the
    pin's existing manifest form goes to ``apply_required_patches`` as
    before. If that last-good form no longer applies, the unchanged
    conflict-abort path in ``apply_required_patches`` is what fails the run.
    That is the ONLY remaining churn abort.

    ``upstream_tip`` is the upstream head fetched by THIS run. Candidate
    eligibility (R1) requires ancestry from it, so a caller that cannot name
    it gets no detection at all rather than a proposal built on an
    unverifiable ancestry claim.
    """
    to_apply: list[dict[str, str]] = []
    absorbed: list[dict[str, str]] = []
    blocked = blocklisted_patch_ids()
    recorded_by_patch_id = {
        record.get("output_patch_id"): record
        for record in (records or [])
        if record.get("output_patch_id") and record.get("output_commit")
    }
    for patch in patches:
        accepted_ids = _accepted_output_patch_ids(patch)
        recorded = next((recorded_by_patch_id[patch_id] for patch_id in accepted_ids if patch_id in recorded_by_patch_id), None)
        if recorded is not None:
            absorbed.append({
                "commit": patch["commit"],
                "subject": patch["subject"],
                "upstream_commit": str(recorded["output_commit"]),
                "output_patch_id": str(recorded["output_patch_id"]),
            })
            continue
        # `--grep` is the cheap pre-filter only; `%s` carries the subject so
        # eligibility can demand EXACT subject equality (R1) instead of the
        # substring/body match `--grep` actually performs.
        candidate_rows = [
            line.partition("\x00") for line in git(
                "log", upstream, "--format=%H%x00%s", "--fixed-strings", f"--grep={patch['subject']}", check=False
            ).splitlines() if line
        ]
        candidates = [row[0] for row in candidate_rows]
        candidate_ids = {candidate: stable_patch_id(candidate) for candidate in candidates}
        equivalent = next((
            candidate for candidate, patch_id in candidate_ids.items()
            if patch_id in accepted_ids and patch_id not in blocked
        ), None)
        if equivalent:
            absorbed.append({
                "commit": patch["commit"],
                "subject": patch["subject"],
                "upstream_commit": equivalent,
                "output_patch_id": candidate_ids[equivalent],
            })
            continue
        if upstream_tip:
            park_pin_for_churn(patch, search_ref=upstream, upstream_tip=upstream_tip)
        to_apply.append(patch)
    return to_apply, absorbed


def upstream_patch_resolution(
    upstream: str, *, records: list[dict[str, Any]] | None = None, upstream_tip: str | None = None
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Resolve the fork-only overlay patches against upstream."""
    return patch_resolution(upstream, REQUIRED_PATCHES, records=records, upstream_tip=upstream_tip)


def _ignore_review_dir_inside_worktree() -> None:
    """Keep required review evidence from making a restored checkout dirty."""
    review_root = REVIEW_DIR.resolve()
    repository_root = Path(git("rev-parse", "--show-toplevel")).resolve()
    try:
        review_relative = review_root.relative_to(repository_root).as_posix().rstrip("/")
    except ValueError:
        return
    if not review_relative:
        return
    exclude = repository_root / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    pattern = f"/{review_relative}/"
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if pattern not in existing.splitlines():
        with exclude.open("a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write(pattern + "\n")


def apply_required_patches(
    patches: list[dict[str, Any]],
    *,
    published_input_head: str,
    upstream_head: str,
    kind: str,
) -> list[dict[str, str]]:
    """Apply a bounded required series and record each source-to-output identity.

    Any conflict aborts the entire transaction locally, restores the immutable
    published input, writes review evidence, and propagates a blocking error.
    """
    records: list[dict[str, str]] = []
    failed_patch: dict[str, Any] | None = None
    try:
        for patch in patches:
            failed_patch = patch
            # R19: a pin parked behind a pending proposal still applies its
            # last verified form -- the output just carries who parked it.
            provenance = parked_pin_provenance(patch)
            parked = {"provenance": provenance} if provenance else {}
            replacement = patch.get("reviewed_replacement")
            applied_commit = replacement["commit"] if replacement else patch["commit"]
            prior_application = next((
                record for record in records
                if record.get("applied_commit", record["source_commit"]) == applied_commit
            ), None)
            if prior_application is not None:
                output_patch_id = prior_application["output_patch_id"]
                if output_patch_id not in _accepted_output_patch_ids(patch) and (
                    prior_application.get("status") not in {
                        "applied_in_job_resolution", "resolved_as_already_present", "parked_unresolved",
                    }
                ):
                    raise RuntimeError(
                        f"prior {kind} application identity was not approved: "
                        f"source={patch['commit']} applied={applied_commit}"
                    )
                records.append({
                    "kind": kind,
                    "status": "represented_by_prior_application",
                    "source_commit": patch["commit"],
                    **({"applied_commit": applied_commit} if replacement else {}),
                    "output_commit": prior_application["output_commit"],
                    "output_patch_id": output_patch_id,
                    **parked,
                })
                continue
            try:
                _git_write_with_lock_retry("cherry-pick", applied_commit)
            except Exception:
                if _cherry_pick_stopped_empty():
                    # Git has applied no delta and reports a clean index/worktree.
                    # This is not a conflict resolution to invent: it proves the
                    # required patch is already represented by the current tree.
                    git("cherry-pick", "--skip", timeout=120)
                    records.append({
                        "kind": kind,
                        "status": "already_present_after_empty_cherry_pick",
                        "source_commit": patch["commit"],
                        "output_commit": git("rev-parse", "HEAD"),
                        "output_patch_id": patch["stable_patch_id"],
                        **parked,
                    })
                    continue
                resolution = attempt_in_job_conflict_resolution(
                    applied_commit, str(patch.get("subject", "")), kind=kind,
                )
                if resolution is None:
                    # Content never halts the reconstruction (user directive
                    # 2026-08-17): park the pin, dispatch it to an agent, and
                    # keep applying everything else.
                    parked_pin = park_unresolvable_required_patch(patch, kind)
                    if parked_pin is None:
                        raise  # parking machinery broke: infrastructure
                    records.append({**parked_pin, **parked})
                    continue
                # The resolved output identity is by definition not in the pin's
                # accepted set; the resolution artifact IS the approval evidence
                # (user directive 2026-08-16: reconcile in-job instead of
                # stopping the nightly). The record carries the artifact path so
                # a follow-up proposal can bless the identity permanently and
                # end the per-run re-resolution of the same pin conflict.
                records.append({
                    "kind": kind,
                    "status": "applied_in_job_resolution",
                    "source_commit": patch["commit"],
                    **({"applied_commit": applied_commit} if replacement else {}),
                    "output_commit": resolution["output_commit"],
                    "output_patch_id": resolution["output_patch_id"],
                    "conflicted_files": resolution["conflicted_files"],
                    "resolution_artifact": resolution["resolution_artifact"],
                    **parked,
                })
                continue
            output_commit = git("rev-parse", "HEAD")
            output_patch_id = stable_patch_id(output_commit)
            if output_patch_id not in _accepted_output_patch_ids(patch):
                raise RuntimeError(
                    f"applied {kind} output identity was not approved: source={patch['commit']} output={output_commit}"
                )
            records.append({
                "kind": kind,
                "status": "applied_reviewed_replacement" if replacement else "applied",
                "source_commit": patch["commit"],
                **({"applied_commit": applied_commit} if replacement else {}),
                "output_commit": output_commit,
                "output_patch_id": output_patch_id,
                **parked,
            })
    except Exception as exc:
        git("cherry-pick", "--abort", check=False)
        _ignore_review_dir_inside_worktree()
        review_path = write_reconstruction_review_request(upstream_head, failed_patch, exc)
        git("reset", "--hard", published_input_head, timeout=120)
        in_progress, dirty = cherry_pick_is_cleanly_aborted()
        actual_head = git("rev-parse", "HEAD")
        if in_progress or dirty or actual_head != published_input_head:
            raise RuntimeError(
                f"required {kind} application failed and restoration was incomplete: "
                f"expected_head={published_input_head}, actual_head={actual_head}, dirty={dirty}, "
                f"cherry_pick_in_progress={in_progress}"
            ) from exc
        source = failed_patch["commit"] if failed_patch else "unknown"
        raise RuntimeError(
            f"required {kind} patch conflicted; source={source}; review_request={review_path}: {exc}"
        ) from exc
    return records


def published_integration_range(published_head: str, upstream_head: str) -> tuple[str, list[str]]:
    """Return the unique published-only commit range in chronological order.

    A published head that is already contained in upstream is an explicit,
    idempotent no-range case.  Multiple merge bases are rejected because an
    automated reconstruction must not choose an arbitrary ancestry path.
    """
    bases = [line for line in git("merge-base", "--all", published_head, upstream_head, check=False).splitlines() if line]
    if len(bases) != 1:
        raise RuntimeError(
            "published integration and upstream do not have a unique merge base: "
            f"published={published_head}, upstream={upstream_head}, merge_bases={bases}"
        )
    base = bases[0]
    if published_head == upstream_head or run(
        "git", "merge-base", "--is-ancestor", published_head, upstream_head, timeout=60, check=False
    ).returncode == 0:
        return base, []
    # Topological order is required when the published range contains merges:
    # replay every parent before applying the merge's first-parent delta.
    commits = [
        line for line in git("rev-list", "--reverse", "--topo-order", f"{base}..{published_head}").splitlines()
        if line
    ]
    return base, commits


def _absorbed_published_commits(upstream_head: str, published_head: str, base: str) -> set[str]:
    """Return published commits whose stable patches already exist upstream.

    ``git cherry`` performs the patch-equivalence comparison in one bounded
    operation.  Do not calculate a patch ID for every commit in upstream: the
    upstream repository has tens of thousands of commits and that turns each
    release into an hours-long preflight.
    """
    absorbed: set[str] = set()
    for line in git("cherry", upstream_head, published_head, base, timeout=900).splitlines():
        marker, separator, commit = line.partition(" ")
        if separator and marker == "-":
            absorbed.add(commit.strip())
    return absorbed


def _commit_parents(commit: str) -> list[str]:
    """Return a commit's parents in authored order."""
    fields = git("rev-list", "--parents", "-n", "1", commit).split()
    if not fields or fields[0] != commit:
        raise RuntimeError(f"could not inspect published commit parents: {commit}")
    return fields[1:]


def _cherry_pick_stopped_empty() -> bool:
    """Identify a cherry-pick that stopped only because its delta is present."""
    if run("git", "rev-parse", "--verify", "-q", "CHERRY_PICK_HEAD", check=False).returncode:
        return False
    return (
        run("git", "diff", "--quiet", check=False).returncode == 0
        and run("git", "diff", "--cached", "--quiet", check=False).returncode == 0
    )


# ── in-job replay-conflict reconciliation (user directive 2026-08-16) ────────
# A published-commit replay conflict is reconciled inside the job when the
# result can be proven mechanically; anything unprovable falls back to the
# fail-closed review-request stop. The proof belongs to the job, never to the
# resolution backend: only both-modified files may change, no conflict marker
# may survive, Python sources must still compile, and nothing outside the
# conflicted set may be touched.
IN_JOB_RECONCILIATION = True
RESOLUTION_BACKEND = None  # tests inject a fake; None selects the claude backend


#: Resolution cache: every proven in-job resolution is pinned and reusable,
#: so a failed run's resolver work is never re-derived from scratch by the
#: next attempt (user directive 2026-08-17: "why is it rerunning?!").
RESOLUTION_CACHE_PATH = HERMES_HOME / "cron" / "fork-integration-resolution-cache.json"


def _load_resolution_cache() -> dict[str, Any]:
    try:
        data = json.loads(RESOLUTION_CACHE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        pass
    except Exception as exc:
        log(f"WARNING resolution cache unreadable, starting fresh: {exc}")
    return {}


def _record_cached_resolution(source_commit: str, resolved_commit: str) -> None:
    """Best-effort: a cache write failure must never taint a real resolution."""
    try:
        keep_ref = f"refs/pinned/resolved/{source_commit[:12]}"
        git("update-ref", keep_ref, resolved_commit)
        cache = _load_resolution_cache()
        cache[source_commit] = {
            "resolved_commit": resolved_commit,
            "keep_ref": keep_ref,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        RESOLUTION_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESOLUTION_CACHE_PATH.write_text(json.dumps(cache, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        log(f"WARNING could not record cached resolution for {source_commit}: {exc}")


def _try_cached_resolution(commit: str, kind: str) -> dict[str, Any] | None:
    """Replay a prior proven resolution of this exact commit, if one exists.

    Called from inside a stopped cherry-pick. On cache miss or a cache pick
    that no longer applies, the original conflicted state is re-created so
    the backend path proceeds exactly as before. Never raises.
    """
    try:
        entry = _load_resolution_cache().get(commit)
        if not isinstance(entry, dict):
            return None
        resolved = str(entry.get("resolved_commit", ""))
        if git("cat-file", "-t", resolved, check=False) != "commit":
            return None
        git("cherry-pick", "--abort", check=False)
        try:
            _git_write_with_lock_retry("cherry-pick", "--allow-empty", resolved)
        except Exception:
            # The cached resolution drifted (upstream moved again): restore
            # the original conflict and let the live resolver take over.
            git("cherry-pick", "--abort", check=False)
            _git_write_with_lock_retry("reset", "--hard", "HEAD", timeout=120)
            git("clean", "-fd", timeout=120)
            try:
                git("cherry-pick", "--allow-empty", commit, timeout=900)
            except Exception:
                pass  # conflicted again, as expected — backend path resumes
            return None
        output_commit = git("rev-parse", "HEAD")
        log(f"RESOLUTION_CACHE_HIT commit={commit} resolved={resolved}")
        return {
            "kind": kind,
            "status": "applied_in_job_resolution",
            "source_commit": commit,
            "output_commit": output_commit,
            "output_patch_id": _commit_patch_id(output_commit),
            "conflicted_files": [],
            "resolution_artifact": str(entry.get("keep_ref", "")),
        }
    except Exception as exc:
        log(f"WARNING cached-resolution replay failed for {commit}: {exc}")
        return None


#: Machine-generated files never reach the LLM resolver (witnessed 2026-08-17:
#: a resolver burned its whole turn budget on a uv.lock conflict). They are
#: pre-resolved to the reconstruction base's side and regenerated afterwards.
MACHINE_GENERATED_CONFLICTS = ("uv.lock",)
#: Regenerate uv.lock via ``uv lock`` after a resolution that pre-resolved it.
#: Module-level so tests can disable the subprocess.
UV_LOCK_REGEN = True


def _claude_resolution_backend(prompt: str, conflicted: list[str]) -> str:
    """Run a bounded, non-interactive resolver inside the job's worktree.

    GIT_OPTIONAL_LOCKS=0: the agent harness runs its own git introspection
    in its cwd (the scheduler worktree), and an optional index.lock from a
    straggling child collided with the run's next cherry-pick (witnessed
    2026-08-17 03:48, one pick after a successful resolution).
    """
    result = run(
        "claude", "-p", prompt,
        "--model", "opus", "--effort", "high",
        "--allowedTools", "Read,Edit,Grep,Glob",
        "--permission-mode", "acceptEdits",
        "--max-turns", "60", "--output-format", "json",
        timeout=1800,
        extra_env={"GIT_OPTIONAL_LOCKS": "0"},
    )
    return result.stdout


def _conflict_markers_remain(paths: list[str]) -> bool:
    """True when any listed file is missing or still carries git conflict markers."""
    for path in paths:
        file_path = WORKTREE / path
        if not file_path.is_file():
            return True
        text = file_path.read_text(encoding="utf-8", errors="replace")
        if any(
            line.startswith(("<<<<<<<", ">>>>>>>")) or line.rstrip() == "======="
            for line in text.splitlines()
        ):
            return True
    return False


def _conflicted_paths() -> list[str]:
    return [line for line in git("diff", "--name-only", "--diff-filter=U").splitlines() if line]


def attempt_in_job_conflict_resolution(commit: str, subject: str, kind: str = "published") -> dict[str, Any] | None:
    """Reconcile one replay conflict in-job; ``None`` means fall back closed.

    Never raises: on ``None`` the caller re-raises the original cherry-pick
    error and the existing restoration path cleans the checkout, so every
    internal failure here degrades to exactly the old fail-closed behavior.
    """
    if not IN_JOB_RECONCILIATION:
        return None
    try:
        conflicted = _conflicted_paths()
        if not conflicted:
            return None  # not a content conflict: a real git failure stays fatal
        # Only both-modified content conflicts are provable here. Delete and
        # rename conflicts need human intent and stay on the review path.
        states_snapshot = _porcelain_lines()
        states = {line[3:]: line[:2] for line in states_snapshot if line}
        if any(states.get(path) != "UU" for path in conflicted):
            return None
        # A prior attempt may already have proven this exact resolution:
        # replay it in seconds instead of re-deriving it.
        cached = _try_cached_resolution(commit, kind)
        if cached is not None:
            return cached
        # Machine-generated files never reach the resolver: take the
        # reconstruction base's side now; regenerate after the semantic
        # resolution lands.
        generated = [path for path in conflicted if path in MACHINE_GENERATED_CONFLICTS]
        editable = [path for path in conflicted if path not in MACHINE_GENERATED_CONFLICTS]
        for path in generated:
            git("checkout", "--ours", "--", path)
            git("add", "--", path)
        transcript = ""
        if editable:
            prompt = (
                "You are resolving one git cherry-pick conflict inside an automated "
                f"Hermes integration release. Conflicted commit: {commit} ({subject}). "
                f"Conflicted files (both-modified): {', '.join(editable)}. "
                "Your ONLY tools are Read, Edit, Grep, and Glob. Bash, PowerShell, "
                "and every other tool are unavailable — a denied call only wastes a "
                "turn, so never attempt one. Each conflicted file contains standard "
                "inline git conflict markers (<<<<<<< HEAD / ======= / >>>>>>>): "
                "Read the file, reconcile, and Edit it in place. Keep both the "
                "upstream intent (HEAD side) and the fork intent (patch side) "
                "wherever both apply — a union resolution. Never delete either "
                "side's behavior just to make the conflict disappear. Do not stage, "
                "commit, or touch any other file. If the sides are genuinely "
                "irreconcilable, change nothing and say so."
            )
            backend = RESOLUTION_BACKEND or _claude_resolution_backend
            before = _porcelain_lines()
            for attempt in (1, 2):
                transcript = backend(prompt, list(editable))
                # Job-owned proof, part 1: the backend touched nothing beyond
                # editing the conflicted files in place (their porcelain lines
                # stay "UU").
                after = _porcelain_lines()
                if set(after) - set(before):
                    return None
                if not _conflict_markers_remain(editable):
                    break
                if attempt == 2:
                    return None
                prompt += (
                    " NOTE: a previous attempt left unresolved conflict markers in "
                    "at least one listed file — finish resolving every marker."
                )
            for path in editable:
                if path.endswith(".py") and run(
                    sys.executable, "-m", "py_compile", str(WORKTREE / path), check=False
                ).returncode:
                    return None
            git("add", "--", *editable)
        if _conflicted_paths():
            return None
        if run("git", "diff", "--cached", "--check", check=False).returncode:
            return None
        uv_lock_regenerated: bool | None = None
        if generated and UV_LOCK_REGEN:
            regen = run("uv", "lock", check=False, timeout=600)
            uv_lock_regenerated = regen.returncode == 0
            if uv_lock_regenerated:
                git("add", "--", *generated)
            else:
                # Publish the base side rather than fail the nightly; the
                # build gate downstream is the real check for a stale lock.
                log(
                    "WARNING uv.lock regeneration failed after conflict resolution: "
                    + ((regen.stdout or "") + (regen.stderr or "")).strip().replace("\n", " ")[-400:]
                )
        if run("git", "diff", "--cached", "--quiet", check=False).returncode == 0:
            # The proven resolution collapsed to the current tree: the patch
            # is already represented (witnessed live 2026-08-17 05:52 —
            # `cherry-pick --continue` refuses the now-empty pick). Conclude
            # as a skip and record presence, not failure.
            git("cherry-pick", "--skip", timeout=120)
            log(f"RECONSTRUCTION_RESOLVED_AS_PRESENT commit={commit} kind={kind}")
            return {
                "kind": kind,
                "status": "resolved_as_already_present",
                "source_commit": commit,
                "output_commit": git("rev-parse", "HEAD"),
                "output_patch_id": None,
                "conflicted_files": conflicted,
                "resolution_artifact": None,
            }
        run(
            "git", "-c", "rerere.enabled=false", "-c", "core.editor=true",
            "cherry-pick", "--continue",
            timeout=300, extra_env={"GIT_EDITOR": "true"},
        )
        output_commit = git("rev-parse", "HEAD")
        _record_cached_resolution(commit, output_commit)
        REVIEW_DIR.mkdir(parents=True, exist_ok=True)
        artifact_path = REVIEW_DIR / f"reconstruction-resolution-{datetime.now():%Y%m%d-%H%M%S}.json"
        artifact_path.write_text(json.dumps({
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "resolved_in_job",
            "kind": kind,
            "source_commit": commit,
            "subject": subject,
            "conflicted_files": conflicted,
            "generated_files": generated,
            "uv_lock_regenerated": uv_lock_regenerated,
            "output_commit": output_commit,
            "resolved_delta": git("show", "--format=%H %s", output_commit, "--", *conflicted)[-20000:],
            "backend_transcript_tail": (transcript or "")[-8000:],
            "worktree": str(WORKTREE),
        }, indent=2) + "\n", encoding="utf-8")
        log(
            f"RECONSTRUCTION_RESOLVED_IN_JOB commit={commit} "
            f"files={','.join(conflicted)} artifact={artifact_path}"
        )
        return {
            "kind": kind,
            "status": "applied_in_job_resolution",
            "source_commit": commit,
            "output_commit": output_commit,
            "output_patch_id": _commit_patch_id(output_commit),
            "conflicted_files": conflicted,
            "resolution_artifact": str(artifact_path),
        }
    except Exception as exc:
        log(f"WARNING in-job reconciliation failed for {commit}: {exc}")
        return None


def _git_write_with_lock_retry(*args: str, timeout: int = 900) -> str:
    """Run a mutating git command, retrying once after clearing our own debris.

    Under the exclusive cron lock every index.lock in the worktree is either
    a dead run's leftover or a straggler from a resolver child that already
    exited — both clearable. One bounded retry converts that race from a
    run-killer into a log line (witnessed 2026-08-17 03:48).
    """
    try:
        return git(*args, timeout=timeout)
    except Exception as exc:
        if "index.lock" not in str(exc):
            raise
        time.sleep(2)
        _clear_stale_worktree_index_lock()
        return git(*args, timeout=timeout)


# ── park-and-continue for unresolvable published commits (2026-08-17) ───────
# A content conflict must never halt the rebase: an unprovable resolution
# parks the one commit (object kept alive under refs/pinned/parked/) and the
# reconstruction keeps going. Every later run retries the parked queue —
# upstream drift routinely dissolves these without any human touch.
PARKED_COMMITS_PATH = HERMES_HOME / "cron" / "fork-integration-parked-commits.json"


def _load_parked_commits() -> dict[str, Any]:
    try:
        data = json.loads(PARKED_COMMITS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("entries"), list):
            return data
    except FileNotFoundError:
        pass
    except Exception as exc:
        log(f"WARNING parked-commit ledger unreadable, starting fresh: {exc}")
    return {"schema": 1, "entries": []}


def _save_parked_commits(data: dict[str, Any]) -> None:
    PARKED_COMMITS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PARKED_COMMITS_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def park_unresolvable_published_commit(commit: str, subject: str) -> dict[str, Any] | None:
    """Skip one unresolvable commit and keep the rebase moving.

    Returns ``None`` only when the parking machinery itself fails — the
    caller treats that as infrastructure breakage and fails closed. Content
    never fails closed.
    """
    try:
        keep_ref = f"refs/pinned/parked/{commit[:12]}"
        # Abort the stopped pick and scrub any debris a refused resolver
        # attempt left behind (edits, stray files): the replay must continue
        # from a pristine pre-pick tree.
        git("cherry-pick", "--abort", check=False)
        _git_write_with_lock_retry("reset", "--hard", "HEAD", timeout=120)
        git("clean", "-fd", timeout=120)
        git("update-ref", keep_ref, commit)
        data = _load_parked_commits()
        now = datetime.now(timezone.utc).isoformat()
        entry = next((item for item in data["entries"] if item.get("commit") == commit), None)
        if entry is None:
            entry = {
                "commit": commit, "subject": subject, "first_parked_at": now,
                "attempts": 0, "keep_ref": keep_ref, "status": "open",
            }
            data["entries"].append(entry)
        entry.update({
            "last_parked_at": now,
            "attempts": int(entry.get("attempts", 0)) + 1,
            "status": "open",
        })
        _save_parked_commits(data)
        log(
            f"PUBLISHED_COMMIT_PARKED commit={commit} attempts={entry['attempts']} "
            f"subject={subject!r} ledger={PARKED_COMMITS_PATH}"
        )
        # The parked commit goes to an agent for out-of-band resolution: the
        # rebase keeps moving, the investigator session reviews the conflict
        # in its own workspace and writes `resolved_commit` back into the
        # ledger entry; the next run's retry loop consumes it automatically.
        launch_failure_investigator(
            stage="parked_commit_resolution",
            error=(
                f"Published commit {commit} ({subject!r}) was parked after in-job "
                f"resolution could not be proven; the rebase continued without it. "
                f"AGENT BRIEF: resolve this commit against current upstream in your "
                f"OWN worktree (never the scheduler worktree at {WORKTREE}), commit "
                f"the resolved result there, make it reachable (push a ref or fetch "
                f"it into the scheduler clone), then set 'resolved_commit' on the "
                f"matching entry in {PARKED_COMMITS_PATH} (status stays 'open'). "
                f"The original object is pinned at {keep_ref}. The next release run "
                f"applies your resolution automatically."
            ),
        )
        return {
            "kind": "published",
            "status": "parked_unresolved",
            "source_commit": commit,
            "output_commit": git("rev-parse", "HEAD"),
            "output_patch_id": None,
            "keep_ref": keep_ref,
            "parked_ledger": str(PARKED_COMMITS_PATH),
        }
    except Exception as exc:
        log(f"WARNING parking machinery failed for {commit}: {exc}")
        return None


def park_unresolvable_required_patch(patch: dict[str, Any], kind: str) -> dict[str, Any] | None:
    """Skip one unresolvable pin application and keep the reconstruction moving.

    Pins need no retry ledger: every run re-applies the manifest, so a parked
    pin is retried nightly by construction. The dispatch below hands the
    conflict to an agent for out-of-band resolution (reviewed_replacement or
    accepted-identity proposal).
    """
    try:
        git("cherry-pick", "--abort", check=False)
        _git_write_with_lock_retry("reset", "--hard", "HEAD", timeout=120)
        git("clean", "-fd", timeout=120)
        log(
            f"REQUIRED_PATCH_PARKED kind={kind} commit={patch['commit']} "
            f"subject={str(patch.get('subject', ''))!r}"
        )
        launch_failure_investigator(
            stage="parked_pin_resolution",
            error=(
                f"Required {kind} patch {patch['commit']} "
                f"({str(patch.get('subject', ''))!r}) was parked after in-job "
                f"resolution could not be proven; the reconstruction continued "
                f"without it and every nightly retries it by construction. "
                f"AGENT BRIEF: resolve the conflict against current upstream in "
                f"your OWN worktree (never the scheduler worktree at {WORKTREE}), "
                f"then either add a reviewed_replacement for this pin or extend "
                f"its accepted_output_patch_ids via the manifest approval flow."
            ),
        )
        return {
            "kind": kind,
            "status": "parked_unresolved",
            "source_commit": patch["commit"],
            "output_commit": git("rev-parse", "HEAD"),
            "output_patch_id": None,
        }
    except Exception as exc:
        log(f"WARNING pin parking machinery failed for {patch['commit']}: {exc}")
        return None


def retry_parked_commits(exclude: set[str] | None = None) -> list[dict[str, Any]]:
    """Re-attempt open parked commits at the end of a replay.

    ``exclude`` carries the commits of the range that just replayed: an
    entry parked seconds ago by THIS run must not be immediately re-fought
    (it would re-run the resolver and re-create whatever debris parked it).
    """
    exclude = exclude or set()
    data = _load_parked_commits()
    records: list[dict[str, Any]] = []
    changed = False
    for entry in data["entries"]:
        if entry.get("status") != "open":
            continue
        commit = str(entry.get("commit", ""))
        if commit in exclude:
            continue
        subject = str(entry.get("subject", ""))
        # An agent may have resolved this out-of-band: its resolved commit
        # takes precedence over re-fighting the original conflict.
        resolved = str(entry.get("resolved_commit", "") or "")
        pick_target = next(
            (
                candidate for candidate in (resolved, commit)
                if candidate and git("cat-file", "-t", candidate, check=False) == "commit"
            ),
            None,
        )
        if pick_target is None:
            continue
        try:
            _git_write_with_lock_retry("cherry-pick", "--allow-empty", pick_target)
            resolution: dict[str, Any] | None = None
        except Exception:
            resolution = attempt_in_job_conflict_resolution(commit, subject)
            if resolution is None:
                # Scrub exactly like parking does: no conflict state or
                # resolver debris may leak into the rest of the run.
                git("cherry-pick", "--abort", check=False)
                _git_write_with_lock_retry("reset", "--hard", "HEAD", timeout=120)
                git("clean", "-fd", timeout=120)
                entry.update({
                    "last_parked_at": datetime.now(timezone.utc).isoformat(),
                    "attempts": int(entry.get("attempts", 0)) + 1,
                })
                changed = True
                log(f"PARKED_COMMIT_STILL_UNRESOLVED commit={commit} attempts={entry['attempts']}")
                continue
        entry.update({"status": "applied", "applied_at": datetime.now(timezone.utc).isoformat()})
        changed = True
        output_commit = git("rev-parse", "HEAD")
        records.append(resolution or {
            "kind": "published",
            "status": "applied_from_parked",
            "source_commit": commit,
            "output_commit": output_commit,
            "output_patch_id": _commit_patch_id(output_commit),
        })
        log(f"PARKED_COMMIT_APPLIED commit={commit}")
    if changed:
        _save_parked_commits(data)
    return records


def _ensure_pristine_tree(context: str) -> None:
    """Self-heal external interference with OUR worktree (exclusive lock held).

    Something on this host intermittently deletes or edits checked-out files
    mid-run (witnessed three times on contributors/emails/*.local paths, run
    killers on 2026-08-17). Under the exclusive cron lock every unexplained
    change is interference, not work: restore tracked paths, remove
    untracked debris, log loudly, and fail only if the tree still cannot be
    made pristine.
    """
    porcelain = _porcelain_lines()
    if not porcelain:
        return
    if not _real_dirt(porcelain):
        log(f"NTFS_CASE_PHANTOMS tolerated context={context} entries={porcelain[:6]!r}")
        return
    log(f"WORKTREE_INTERFERENCE context={context} entries={porcelain[:10]!r}")
    _git_write_with_lock_retry("reset", "--hard", "HEAD", timeout=120)
    git("clean", "-fd", timeout=120)
    remaining = _real_dirt(_porcelain_lines())
    if remaining:
        raise RuntimeError(
            f"worktree is dirty after integration reconstruction: {str(remaining)[:300]}"
        )
    if git("status", "--porcelain"):
        log(f"NTFS_CASE_PHANTOMS tolerated context={context} after-reset")


def _restore_replay_checkout(published_input_head: str) -> tuple[bool, bool, str, Exception | None]:
    """Abort replay and remove all owned tracked/untracked transaction debris."""
    _clear_stale_worktree_index_lock()
    git("cherry-pick", "--abort", check=False)
    restore_error: Exception | None = None
    try:
        _git_write_with_lock_retry("reset", "--hard", published_input_head, timeout=120)
        git("clean", "-fd", timeout=120)
    except Exception as exc:
        restore_error = exc
    in_progress, dirty = cherry_pick_is_cleanly_aborted()
    return in_progress, dirty, git("rev-parse", "HEAD", check=False), restore_error


def replay_published_integration_range(
    published_input_head: str,
    upstream_head: str,
    *,
    return_records: bool = False,
) -> list[Any]:
    """Rebuild upstream from every published-only commit, preserving direct work.

    This helper performs no remote mutation.  A conflict is first offered to
    the in-job reconciliation path (proof-carrying, see
    ``attempt_in_job_conflict_resolution``); when that cannot prove a
    resolution the original published checkout is restored, no cherry-pick
    state is left behind, and the exact source commit is recorded for human
    reconstruction review.
    """
    base, commits = published_integration_range(published_input_head, upstream_head)
    # An already-published/absorbed branch needs no reconstruction.  Return
    # before comparing patches or resetting so this idempotent case stays
    # read-only at the worktree level.
    if not commits:
        return []
    absorbed_commits = _absorbed_published_commits(upstream_head, published_input_head, base)
    # One metadata pass for the whole range: per-commit subject/parent
    # subprocesses added ~4s x 100+ commits of pure spawn overhead to every
    # replay on Windows (user question 2026-08-17: "why is a cache hit
    # taking so long?").
    metadata: dict[str, tuple[list[str], str]] = {}
    for chunk_start in range(0, len(commits), 400):
        chunk = commits[chunk_start:chunk_start + 400]
        for line in git(
            "log", "--no-walk=unsorted", "--format=%H%x00%P%x00%s", *chunk, timeout=300
        ).splitlines():
            parts = line.split("\x00")
            if len(parts) == 3:
                metadata[parts[0]] = ([p for p in parts[1].split() if p], parts[2])
    absorbed_ids = _batch_patch_ids([c for c in commits if c in absorbed_commits])
    git("reset", "--hard", upstream_head, timeout=120)
    replayed: list[str] = []
    records: list[dict[str, Any]] = []
    failed_patch: dict[str, str] | None = None
    try:
        for commit in commits:
            if commit in metadata:
                parents, subject = metadata[commit]
            else:
                subject = git("show", "-s", "--format=%s", commit)
                parents = _commit_parents(commit)
            failed_patch = {"commit": commit, "subject": subject}
            is_merge = len(parents) > 1
            if not is_merge and commit in absorbed_commits:
                records.append({
                    "kind": "published",
                    "status": "absorbed_patch_equivalent",
                    "source_commit": commit,
                    "output_commit": upstream_head,
                    "output_patch_id": absorbed_ids.get(commit),
                })
                continue
            # --allow-empty preserves authored marker/empty direct commits.  A
            # direct commit that becomes empty still stops for review; merge
            # deltas get the explicit already-represented handling below.
            cherry_pick_args = ["cherry-pick", "--allow-empty"]
            if is_merge:
                # Published merge commits are never omitted.  Replaying their
                # first-parent delta preserves merge-only conflict resolutions
                # after all parent commits have been replayed topologically.
                cherry_pick_args.extend(["-m", "1"])
            try:
                _git_write_with_lock_retry(*cherry_pick_args, commit)
            except Exception:
                if is_merge and _cherry_pick_stopped_empty():
                    # The side-parent commits can already represent the whole
                    # first-parent delta.  Resolve Git's empty stop explicitly
                    # and retain the merge source in the preservation ledger.
                    git("cherry-pick", "--skip", timeout=120)
                    output_commit = git("rev-parse", "HEAD")
                    records.append({
                        "kind": "published",
                        "status": "merge_delta_already_represented",
                        "source_commit": commit,
                        "output_commit": output_commit,
                        "output_patch_id": None,
                        "parent_count": len(parents),
                        "mainline": 1,
                    })
                    continue
                resolution = attempt_in_job_conflict_resolution(commit, subject)
                if resolution is None:
                    # A content conflict must NEVER halt the rebase (user
                    # directive 2026-08-17): park this one commit — keep its
                    # object alive, record it loudly, skip it — and keep
                    # rebuilding everything else. Fail-closed is reserved
                    # for infrastructure failures, not content.
                    parked = park_unresolvable_published_commit(commit, subject)
                    if parked is None:
                        raise  # parking machinery itself broke: infrastructure
                    records.append({
                        **parked,
                        **({"parent_count": len(parents), "mainline": 1} if is_merge else {}),
                    })
                    continue
                replayed.append(commit)
                records.append({
                    **resolution,
                    **({"parent_count": len(parents), "mainline": 1} if is_merge else {}),
                })
                continue
            replayed.append(commit)
            output_commit = git("rev-parse", "HEAD")
            records.append({
                "kind": "published",
                "status": "applied_merge_mainline" if is_merge else "applied",
                "source_commit": commit,
                "output_commit": output_commit,
                "output_patch_id": None,
                "_pending_patch_id": True,
                **({"parent_count": len(parents), "mainline": 1} if is_merge else {}),
            })
        # Batch-fill applied outputs' identities in one pipeline instead of a
        # subprocess pair per pick.
        pending = [record for record in records if record.pop("_pending_patch_id", False)]
        if pending:
            output_ids = _batch_patch_ids([record["output_commit"] for record in pending])
            for record in pending:
                record["output_patch_id"] = output_ids.get(record["output_commit"])
        # After the range replays, re-attempt anything parked by earlier
        # runs — including agent-resolved commits waiting in the ledger.
        records.extend(retry_parked_commits(exclude=set(commits)))
    except Exception as exc:
        in_progress, dirty, actual_head, restore_error = _restore_replay_checkout(published_input_head)
        # Operational review records may be configured inside a fixture/worktree.
        # Keep them local-only ignored so recording the required review does not
        # falsely make source checkout restoration appear dirty.
        review_root = REVIEW_DIR.resolve()
        repository_root = Path(git("rev-parse", "--show-toplevel")).resolve()
        try:
            review_relative = review_root.relative_to(repository_root).as_posix().rstrip("/")
        except ValueError:
            review_relative = ""
        if review_relative:
            exclude = repository_root / ".git" / "info" / "exclude"
            exclude.parent.mkdir(parents=True, exist_ok=True)
            pattern = f"/{review_relative}/"
            existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
            if pattern not in existing.splitlines():
                with exclude.open("a", encoding="utf-8") as handle:
                    if existing and not existing.endswith("\n"):
                        handle.write("\n")
                    handle.write(pattern + "\n")
        review_path = write_reconstruction_review_request(upstream_head, failed_patch, exc)
        if restore_error or in_progress or dirty or actual_head != published_input_head:
            raise RuntimeError(
                "published integration replay failed and restoration was incomplete: "
                f"restore_error={restore_error}, cherry_pick_in_progress={in_progress}, dirty={dirty}, "
                f"expected_head={published_input_head}, actual_head={actual_head}"
            ) from exc
        where = (
            f"{failed_patch['commit']} ({failed_patch['subject']})"
            if failed_patch else "unknown"
        )
        raise RuntimeError(
            "published integration replay conflicted; no remote mutation was attempted; "
            f"conflicting_commit={where}; review_request={review_path}: {exc}"
        ) from exc
    return records if return_records else replayed


def _porcelain_lines() -> list[str]:
    """RAW porcelain lines with status columns intact.

    ``git()`` strips the whole stdout, which eats the FIRST line's leading
    status column: ``" M <file>"`` became ``"M <file>"`` and ``line[3:]``
    parsed the path as ``"ntributors/..."`` — so a single-entry porcelain
    (always the first line) always mis-parsed (witnessed 2026-08-17 12:59,
    phantom judged as real dirt). Never parse porcelain through git().
    """
    return [
        line for line in run("git", "status", "--porcelain").stdout.splitlines()
        if line.strip()
    ]


def _ntfs_phantom_paths() -> set[str]:
    """Index paths that collide case-insensitively with another entry.

    A case-insensitive filesystem can materialize only one casing, so git
    perpetually reports the loser as modified or deleted — dirt that no
    reset can ever clear. Witnessed 2026-08-17: upstream main carries BOTH
    contributors/emails/agent@Agents-Mac-mini.local and
    contributors/emails/agent@agents-Mac-mini.local. These are filesystem
    phantoms, never evidence of interference or loss.
    """
    seen: dict[str, str] = {}
    phantoms: set[str] = set()
    for path in git("ls-files").splitlines():
        key = path.lower()
        if key in seen:
            phantoms.add(path)
            phantoms.add(seen[key])
        else:
            seen[key] = path
    return phantoms


def _real_dirt(porcelain_lines: list[str]) -> list[str]:
    """Porcelain entries that are NOT case-collision phantoms."""
    entries = [line for line in porcelain_lines if line]
    if not entries:
        return []
    phantoms = _ntfs_phantom_paths()
    return [line for line in entries if line[3:].strip('"') not in phantoms]


def cherry_pick_is_cleanly_aborted() -> tuple[bool, bool]:
    """Return whether a failed single cherry-pick left state or changes behind."""
    in_progress = run("git", "rev-parse", "--verify", "-q", "CHERRY_PICK_HEAD", check=False).returncode == 0
    dirty = bool(_real_dirt(_porcelain_lines()))
    return in_progress, dirty


def _represented_commits(upstream: str, rebased_head: str) -> list[str]:
    """Return commits whose patches can prove the reconstructed integration tip."""
    refs = [upstream]
    if run("git", "merge-base", "--is-ancestor", rebased_head, upstream, check=False).returncode:
        refs.append(f"{upstream}..{rebased_head}")
    commits: list[str] = []
    for ref in refs:
        commits.extend(line for line in git("rev-list", "--reverse", ref).splitlines() if line)
    return commits


def _commit_patch_id(commit: str) -> str | None:
    """Return None only for an authored empty commit that has no patch identity."""
    try:
        return stable_patch_id(commit)
    except RuntimeError:
        return None


def _batch_patch_ids(commits: list[str]) -> dict[str, str | None]:
    """Stable patch-ids for many commits in one git pipeline per chunk.

    The per-commit ``git show | git patch-id`` pair costs seconds of process
    spawn on Windows; across a 500-commit validation scan that added up to a
    ~45-minute stall (witnessed 2026-08-16). ``git log --no-walk -p`` emits
    every requested commit in one stream and ``git patch-id --stable`` keys
    each diff to its ``commit <sha>`` header, so one subprocess pair covers a
    whole chunk. Empty commits produce no diff and stay ``None``, matching
    ``_commit_patch_id``.
    """
    ids: dict[str, str | None] = {commit: None for commit in commits}
    unique = list(dict.fromkeys(commits))
    # 400 x 40-char SHAs stays far under the Windows 32k command-line limit.
    for start in range(0, len(unique), 400):
        chunk = unique[start:start + 400]
        stream = run(
            "git", "log", "--no-walk=unsorted", "-p", "--binary",
            "--format=commit %H", *chunk, timeout=900,
        ).stdout
        result = subprocess.run(
            [resolve_executable("git"), "patch-id", "--stable"],
            input=stream,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
            **windows_subprocess_kwargs(),
        )
        if result.returncode:
            raise RuntimeError(f"batch patch identity computation failed: {(result.stderr or '')[-400:]}")
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] in ids:
                ids[parts[1]] = parts[0]
    return ids


def _accepted_output_patch_ids(patch: dict[str, Any]) -> set[str]:
    """Return source plus explicitly reviewed conflict-resolution identities."""
    return {patch["stable_patch_id"], *patch.get("accepted_output_patch_ids", [])}


def _resolution_records(absorbed: list[dict[str, str]], kind: str) -> list[dict[str, str]]:
    """Translate bounded resolution results into validator application records."""
    return [{
        "kind": kind,
        "status": "represented",
        "source_commit": item["commit"],
        "output_commit": item["upstream_commit"],
        "output_patch_id": item["output_patch_id"],
    } for item in absorbed]


def _exact_published_records(published_commits: list[str]) -> list[dict[str, Any]]:
    """Record authoritative published commits retained by exact ancestry."""
    patch_ids = _batch_patch_ids(published_commits)
    return [{
        "kind": "published",
        "status": "exact_reachable",
        "source_commit": commit,
        "output_commit": commit,
        "output_patch_id": patch_ids[commit],
    } for commit in published_commits]


def _matching_patch_by_subject(patch: dict[str, Any], upstream: str, rebased_head: str) -> tuple[bool, bool]:
    """Return (equivalent, same_subject_seen) without scanning full history."""
    candidates: list[str] = []
    for ref in (upstream, f"{upstream}..{rebased_head}"):
        if ref != upstream and run("git", "merge-base", "--is-ancestor", rebased_head, upstream, check=False).returncode == 0:
            continue
        candidates.extend(
            line for line in git("log", ref, "--format=%H", "--fixed-strings", f"--grep={patch['subject']}", check=False).splitlines()
            if line
        )
    candidates = list(dict.fromkeys(candidates))
    accepted_ids = _accepted_output_patch_ids(patch)
    return (
        any(_commit_patch_id(candidate) in accepted_ids for candidate in candidates),
        bool(candidates),
    )


def _required_patch_statuses(
    patches: list[dict[str, str]], upstream: str, rebased_head: str
) -> list[tuple[dict[str, str], bool, bool]]:
    """Resolve required patches in one pass, with a bounded fallback scan."""
    statuses: list[tuple[dict[str, str], bool, bool]] = []
    unresolved: list[tuple[dict[str, str], bool]] = []
    for patch in patches:
        equivalent, same_subject_seen = _matching_patch_by_subject(patch, upstream, rebased_head)
        if equivalent or same_subject_seen:
            statuses.append((patch, equivalent, same_subject_seen))
        else:
            unresolved.append((patch, same_subject_seen))

    if unresolved:
        commits = _represented_commits(upstream, rebased_head)
        if len(commits) <= 500:
            identities = {
                patch_id for patch_id in _batch_patch_ids(commits).values() if patch_id is not None
            }
            for patch, same_subject_seen in unresolved:
                statuses.append((patch, bool(_accepted_output_patch_ids(patch) & identities), same_subject_seen))
        else:
            statuses.extend((patch, False, same_subject_seen) for patch, same_subject_seen in unresolved)
    return statuses


def _validate_required_records(
    patches: list[dict[str, str]], rebased_head: str, records: list[dict[str, str]], label: str
) -> None:
    """Validate a bounded source-to-output ledger without walking repository history."""
    by_source: dict[str, list[dict[str, str]]] = {}
    for record in records:
        by_source.setdefault(record["source_commit"], []).append(record)
    for patch in patches:
        matches = by_source.get(patch["commit"], [])
        if len(matches) != 1:
            raise RuntimeError(
                f"missing or ambiguous required {label} application record: source={patch['commit']} records={len(matches)}"
            )
        record = matches[0]
        if record.get("status") in {"parked_unresolved", "resolved_as_already_present"}:
            # Parked by directive (honestly absent, retried every run) or
            # proven already-represented by a collapsed-empty resolution:
            # neither carries a fresh identity to verify.
            continue
        output_commit = record.get("output_commit", "")
        recorded_patch_id = record.get("output_patch_id")
        accepted = _accepted_output_patch_ids(patch)
        # Do not trust the recorded output_patch_id at face value: recompute
        # it from the reconstructed tree so a record that was stamped once
        # and never re-verified cannot silently drift from what output_commit
        # actually contains.
        recomputed = stable_patch_id(output_commit) if output_commit else None
        # An in-job conflict resolution's identity is by definition outside
        # the accepted set; its recorded artifact is the approval evidence
        # (user directive 2026-08-16). Internal consistency (recomputed ==
        # recorded) is still mandatory for every record.
        in_job_resolution = (
            record.get("status") == "applied_in_job_resolution"
            and bool(record.get("resolution_artifact"))
        )
        if recomputed is None or recomputed != recorded_patch_id or (
            recomputed not in accepted and not in_job_resolution
        ):
            raise RuntimeError(
                f"required {label} application record identity does not match reconstructed output: "
                f"source={patch['commit']} output={output_commit} recorded={recorded_patch_id} recomputed={recomputed}"
            )
        if not output_commit or run(
            "git", "merge-base", "--is-ancestor", output_commit, rebased_head, timeout=60, check=False
        ).returncode:
            raise RuntimeError(
                f"required {label} application record is not reachable from output: "
                f"source={patch['commit']} output={output_commit}"
            )


def validate_required_components(
    upstream: str, rebased_head: str, *, records: list[dict[str, str]] | None = None
) -> None:
    """Require every manifest patch to be represented by stable patch identity.

    Components are invariants, not an exclusive allow-list: direct published
    commits may add subjects and paths.  A same-subject non-equivalent patch is
    nevertheless a review blocker, never evidence that a component survived.
    """
    if records is not None:
        _validate_required_records(REQUIRED_PATCHES, rebased_head, records, "component")
        return
    for patch, equivalent, same_subject_seen in _required_patch_statuses(REQUIRED_PATCHES, upstream, rebased_head):
        if equivalent:
            continue
        if same_subject_seen:
            raise RuntimeError(
                "same-subject but non-equivalent required component; manual reconciliation is required: "
                f"source={patch['commit']} subject={patch['subject']!r}"
            )
        raise RuntimeError(
            "missing required component patch from upstream/rebased output: "
            f"source={patch['commit']} subject={patch['subject']!r}"
        )


def validate_required_foundations(
    upstream: str, rebased_head: str, *, records: list[dict[str, str]] | None = None
) -> None:
    """Require each pinned upstream foundation patch in the reconstructed output."""
    if records is not None:
        _validate_required_records(FOUNDATION_PATCHES, rebased_head, records, "foundation")
        return
    for patch, equivalent, same_subject_seen in _required_patch_statuses(FOUNDATION_PATCHES, upstream, rebased_head):
        if equivalent:
            continue
        if same_subject_seen:
            raise RuntimeError(
                "same-subject but non-equivalent required foundation; manual reconciliation is required: "
                f"source={patch['commit']} subject={patch['subject']!r}"
            )
        raise RuntimeError(
            "missing required foundation patch from upstream/rebased output: "
            f"source={patch['commit']} subject={patch['subject']!r}"
        )


def validate_published_commit_preservation(
    published_commits: list[str],
    upstream: str,
    rebased_head: str,
    *,
    records: list[dict[str, str]] | None = None,
) -> None:
    """Prove every published input survives upstream absorption or replay.

    Non-empty commits are matched only by stable patch ID.  For authored empty
    commits Git provides no patch identity, so exact reachability is preferred;
    otherwise an empty output commit with the recorded subject is required.
    """
    if records is not None:
        by_source = {record["source_commit"]: record for record in records}
        # One pipeline instead of a per-commit subprocess pair: the identity
        # walk over ~100 records cost 20+ minutes serially (2026-08-17).
        source_ids = _batch_patch_ids(published_commits)
        for published in published_commits:
            record = by_source.get(published)
            if record is None:
                raise RuntimeError(f"published commit has no preservation record: {published}")
            output_commit = record.get("output_commit", "")
            if not output_commit or run(
                "git", "merge-base", "--is-ancestor", output_commit, rebased_head, timeout=60, check=False
            ).returncode:
                raise RuntimeError(
                    f"published commit preservation output is not reachable: source={published} output={output_commit}"
                )
            parents = _commit_parents(published)
            if len(parents) > 1:
                if record.get("parent_count") != len(parents) or record.get("mainline") != 1:
                    raise RuntimeError(f"published merge has invalid mainline preservation record: {published}")
                merge_status = record.get("status")
                if merge_status not in {
                    "applied_merge_mainline", "merge_delta_already_represented",
                    "applied_in_job_resolution", "parked_unresolved",
                    "resolved_as_already_present",
                }:
                    raise RuntimeError(f"published merge has invalid preservation status: {published}")
                if merge_status == "applied_merge_mainline" and record.get("output_patch_id") is None:
                    raise RuntimeError(f"published merge delta produced no recorded output identity: {published}")
                continue
            # Direct commits retain the strict stable-patch identity check.  The
            # merge path above is separate because replaying all parents first
            # can reduce a merge's first-parent delta to only its unique
            # conflict-resolution portion (or to an already-represented empty).
            source_patch_id = source_ids[published]
            if source_patch_id is not None and record.get("output_patch_id") != source_patch_id:
                # An in-job conflict resolution legitimately changes the
                # patch identity; its artifact is the preservation evidence
                # (user directive 2026-08-16/17 — reconcile in-job, do not
                # stop the nightly for a mechanical identity mismatch). A
                # parked commit is honestly absent by directive: the ledger
                # entry plus keep-ref is its preservation evidence and the
                # retry loop is its road back in.
                in_job = (
                    record.get("status") == "applied_in_job_resolution"
                    and record.get("resolution_artifact")
                )
                parked = (
                    record.get("status") == "parked_unresolved"
                    and record.get("parked_ledger")
                )
                already_present = record.get("status") == "resolved_as_already_present"
                # A cleanly applied cherry-pick's patch-id legitimately drifts
                # when earlier in-job resolutions changed its context lines in
                # the same files; git either applies a change or stops — the
                # identity mismatch is not evidence of loss for an applied
                # record (witnessed 2026-08-17 09:32: a41bdf0cd drifted after
                # 13 resolutions touched its neighborhood). Reachability was
                # already enforced above; log the drift, never fail on it.
                applied_with_drift = record.get("status") in {"applied", "applied_merge_mainline"}
                if applied_with_drift:
                    log(
                        f"PRESERVATION_CONTEXT_DRIFT commit={published} "
                        f"output={record.get('output_commit')}"
                    )
                if not (in_job or parked or already_present or applied_with_drift):
                    raise RuntimeError(f"published commit was not preserved by patch identity: {published}")
        return
    commits = _represented_commits(upstream, rebased_head)
    commit_ids = _batch_patch_ids(commits)
    identities = {patch_id for patch_id in commit_ids.values() if patch_id is not None}
    empty_subjects = {
        git("show", "-s", "--format=%s", commit)
        for commit, patch_id in commit_ids.items()
        if patch_id is None
    }
    published_ids = _batch_patch_ids(published_commits)
    for published in published_commits:
        patch_id = published_ids[published]
        if patch_id is not None:
            if patch_id not in identities:
                raise RuntimeError(f"published commit was not preserved by patch identity: {published}")
            continue
        if run("git", "merge-base", "--is-ancestor", published, rebased_head, check=False).returncode == 0:
            continue
        subject = git("show", "-s", "--format=%s", published)
        if subject not in empty_subjects:
            raise RuntimeError(
                "authored empty published commit was not preserved by exact reachability or empty-commit record: "
                f"{published} subject={subject!r}"
            )


def resolve_built_launcher() -> Path:
    """Resolve the blue bootstrap/updater app, never its NSIS wrapper."""
    return WORKTREE / LAUNCHER_PATH


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _lock_holder_pid_alive(pid: int) -> bool:
    """Cross-platform "is this PID alive" check that never sends a real signal.

    ``os.kill(pid, 0)`` is NOT a safe existence probe on Windows: CPython's
    Windows implementation collides ``sig=0`` with ``CTRL_C_EVENT`` and
    routes it through ``GenerateConsoleCtrlEvent``, which can terminate the
    target process (and other processes sharing its console group) instead
    of merely checking it -- see ``gateway/status.py``'s ``_pid_exists`` and
    this repo's ``psutil`` dependency comment in ``pyproject.toml`` for the
    same footgun. ``psutil`` is a hard project dependency specifically to
    replace that idiom; only fall back to a read-only Windows ``OpenProcess``
    probe (never ``TerminateProcess``) if psutil is somehow unavailable.
    """
    try:
        import psutil  # type: ignore

        return bool(psutil.pid_exists(int(pid)))
    except ImportError:
        pass
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            ERROR_INVALID_PARAMETER = 87
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return kernel32.GetLastError() != ERROR_INVALID_PARAMETER
            kernel32.CloseHandle(handle)
            return True
        except (OSError, AttributeError):
            return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_lock_info() -> dict[str, Any]:
    """Best-effort parse of the lock file; corrupt/empty content is tolerated
    and reported as an unknown holder rather than raising."""
    try:
        data = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("lock payload was not a JSON object")
    except Exception:
        return {"holder": "unknown holder", "pid": None, "started_at": None}
    holder = data.get("holder")
    return {
        "holder": holder if isinstance(holder, str) and holder else "unknown holder",
        "pid": data.get("pid"),
        "started_at": data.get("started_at"),
    }


def _lock_is_reclaimable(info: dict[str, Any]) -> bool:
    """Dead holder PID = dead lock; reclaim immediately.

    User directive 2026-08-17 after two runs were blocked by locks whose
    holders had died: the PID in the lock is the sole liveness authority.
    A lock without a parseable PID (corrupt/legacy payload) has no provable
    owner and is equally reclaimable. The old ``LOCK_STALE_AFTER`` age gate
    is gone — it turned every crashed run into a six-hour outage.
    """
    pid = info.get("pid")
    if not isinstance(pid, int):
        return True
    return not _lock_holder_pid_alive(pid)


# ── upstream base pin: single-shot, TTL-bounded, always-measured ────────────
UPSTREAM_PIN_PATH = HERMES_HOME / "cron" / "fork-integration-upstream-pin.json"


def _resolve_upstream_base(upstream_tip: str) -> str:
    """Return the base for this run: a live, unexpired pin or the tip.

    The pin's liveness rules mirror the exclusive lock's dead-PID doctrine:
    - env pin (HERMES_UPSTREAM_PIN) is ephemeral by construction — it dies
      with the operator's shell and can never reach the cron;
    - the file pin is single-shot (deleted by a successful publish) and
      TTL-bounded — an expired file is ignored loudly and removed, so a
      crashed retry loop cannot wedge the base beyond its window.
    """
    env_pin = os.environ.get("HERMES_UPSTREAM_PIN", "").strip()
    if env_pin:
        if git("cat-file", "-t", env_pin, check=False) != "commit":
            raise RuntimeError(f"HERMES_UPSTREAM_PIN is not a known commit: {env_pin}")
        log(f"UPSTREAM_PINNED source=env pin={env_pin} tip_was={upstream_tip}")
        return env_pin
    try:
        payload = json.loads(UPSTREAM_PIN_PATH.read_text(encoding="utf-8"))
        sha = str(payload.get("sha", "")).strip()
        expires_at = datetime.fromisoformat(str(payload.get("expires_at")))
    except FileNotFoundError:
        return upstream_tip
    except Exception as exc:
        log(f"WARNING unreadable upstream pin ignored and removed: {exc}")
        UPSTREAM_PIN_PATH.unlink(missing_ok=True)
        return upstream_tip
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= expires_at:
        log(f"UPSTREAM_PIN_EXPIRED ignored and removed: sha={sha} expired_at={expires_at.isoformat()}")
        UPSTREAM_PIN_PATH.unlink(missing_ok=True)
        return upstream_tip
    if git("cat-file", "-t", sha, check=False) != "commit":
        log(f"WARNING upstream pin names unknown commit, ignored and removed: {sha}")
        UPSTREAM_PIN_PATH.unlink(missing_ok=True)
        return upstream_tip
    log(f"UPSTREAM_PINNED source=file pin={sha} expires_at={expires_at.isoformat()} tip_was={upstream_tip}")
    return sha


def _consume_upstream_pin_on_publish() -> None:
    """Single-shot: a successful publish retires the pin that produced it."""
    try:
        if UPSTREAM_PIN_PATH.exists():
            UPSTREAM_PIN_PATH.unlink()
            log("UPSTREAM_PIN_CONSUMED by successful publish")
    except OSError as exc:
        log(f"WARNING could not consume upstream pin: {exc}")


def _clear_stale_worktree_index_lock() -> None:
    """Remove an index lock left in OUR worktree by a dead release run.

    Only ever called while holding the exclusive cron lock, which makes this
    process the sole sanctioned user of the scheduler worktree — a surviving
    ``index.lock`` at that point is by definition debris from a killed run,
    not a live competitor.
    """
    lock = WORKTREE / ".git" / "index.lock"
    try:
        if lock.exists():
            lock.unlink()
            log(f"removed stale git lock left by a dead run: {lock}")
    except OSError as exc:
        # A removal failure surfaces on the next git write anyway; log the
        # earlier, clearer story now.
        log(f"WARNING could not remove stale git lock {lock}: {exc}")


@contextmanager
def exclusive_lock(holder: str = "scheduler") -> Any:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        info = _read_lock_info()
        if _lock_is_reclaimable(info):
            log(
                f"reclaiming stale lock held by {info['holder']} pid {info['pid']} "
                f"since {info['started_at']}"
            )
            try:
                LOCK_PATH.unlink()
            except FileNotFoundError:
                pass
            try:
                fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                info = _read_lock_info()
                fail(f"busy, held by {info['holder']} (pid {info['pid']}) since {info['started_at']}")
        else:
            fail(f"busy, held by {info['holder']} (pid {info['pid']}) since {info['started_at']}")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {"holder": holder, "pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat()},
                handle,
            )
        yield
    finally:
        try:
            LOCK_PATH.unlink()
        except FileNotFoundError:
            pass


def _sanitized_command_tail(text: str) -> str:
    """Keep a short redacted diagnostic tail for exception and receipt paths."""
    return redact_process_output(text).replace("\r", "").replace("\n", " ").strip()[-600:]


def remote_ref_head(remote: str, ref: str) -> str:
    """Read exactly one remote ref without updating local tracking refs."""
    result = run("git", "ls-remote", remote, ref, timeout=120, check=False)
    if result.returncode:
        detail = _sanitized_command_tail((result.stdout or "") + "\n" + (result.stderr or ""))
        raise RuntimeError(
            f"git ls-remote failed for remote ref: remote={remote} ref={ref} "
            f"exit={result.returncode} tail={detail or '<no output>'}"
        )
    lines = [line.split() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"zero matching remote refs: remote={remote} ref={ref}")
    if len(lines) != 1:
        raise RuntimeError(f"multiple matching remote refs: remote={remote} ref={ref} count={len(lines)}")
    fields = lines[0]
    if len(fields) != 2 or not re.fullmatch(r"[0-9a-f]{40}", fields[0]) or fields[1] != ref:
        sample = _sanitized_command_tail(result.stdout)
        raise RuntimeError(
            f"malformed remote ref output: remote={remote} ref={ref} "
            f"tail={sample or '<no output>'}"
        )
    return fields[0]


def _without_dry_run_failure_logging(function: Any) -> Any:
    """Run the dry-run inspector with durable checked-failure logging disabled."""
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with dry_run_inspection_logging_disabled():
            return function(*args, **kwargs)
    return wrapped


@_without_dry_run_failure_logging
def inspect_dry_run() -> dict[str, Any]:
    """Describe a reconstruction using only read-only Git operations.

    Remote tips come from ``ls-remote`` rather than ``fetch``.  Git can only
    calculate patch/range provenance when the matching remote object is already
    present locally; an advanced remote is explicitly reported as deferred.
    """
    if not WORKTREE.is_dir():
        raise RuntimeError(f"integration worktree is absent: {WORKTREE}")
    if git("branch", "--show-current") != BRANCH:
        raise RuntimeError(f"worktree is not on {BRANCH}")
    if _real_dirt(_porcelain_lines()):
        raise RuntimeError("integration worktree is dirty; dry-run made no changes")
    pre_run_local_head = git("rev-parse", "HEAD")
    published_ref = f"refs/heads/{BRANCH}"
    upstream_ref = UPSTREAM_REF if UPSTREAM_REF.startswith("refs/") else f"refs/heads/{UPSTREAM_REF}"
    published_input_head = remote_ref_head(FORK_REMOTE, published_ref)
    current_upstream = remote_ref_head(UPSTREAM_REMOTE, upstream_ref)
    local_upstream_ref = f"refs/remotes/{UPSTREAM_REMOTE}/{upstream_ref.removeprefix('refs/heads/')}"
    local_upstream = git("rev-parse", "--verify", "-q", local_upstream_ref, check=False)
    upstream_available = local_upstream == current_upstream
    provenance = "local_tracking_matches_remote" if upstream_available else "remote_ahead_of_local_tracking_range_deferred"
    result: dict[str, Any] = {
        "ok": True,
        "dry_run": True,
        "pre_run_local_head": pre_run_local_head,
        "published_input_head": published_input_head,
        "published_head": published_input_head,  # backwards-compatible alias
        "local_matches_published": pre_run_local_head == published_input_head,
        "local_would_sync_to_published": pre_run_local_head != published_input_head,
        "current_upstream": current_upstream,
        "upstream": current_upstream,  # backwards-compatible alias
        "upstream_provenance": provenance,
        "old_upstream_base": None,
        "published_commit_count": 0,
        "absorbed_commit_count": 0,
        "commits_to_replay": [],
        "required_components": [component["id"] for component in MANIFEST["components"]],
        # Read-only: the open proposals currently parking a pin (R19). A
        # dry-run never resolves patches, so this reports the store's state
        # rather than this invocation's -- and never writes to it.
        "parked_pins": open_proposal_summary(),
        "would_rebase_complete_published_range": False,
        "recovery_current_upstream": False,
        "push_lease_head": published_input_head,
        "push_lease_provenance": "published_input_head",
    }
    if not upstream_available:
        return result
    # These operations inspect only existing objects; do not materialize sources,
    # verify PRs, or contact GitHub because those normal-path checks fetch refs.
    if run("git", "cat-file", "-e", f"{published_input_head}^{{commit}}", check=False).returncode:
        result["inspection_deferred_reason"] = "published_remote_object_not_available_locally"
        return result
    old_base, published_commits = published_integration_range(published_input_head, current_upstream)
    absorbed_commits = (
        _absorbed_published_commits(current_upstream, published_input_head, old_base)
        if published_commits else set()
    )
    commits_to_replay = [commit for commit in published_commits if commit not in absorbed_commits]
    absorbed = len(published_commits) - len(commits_to_replay)
    result.update({
        "old_upstream_base": old_base,
        "published_commit_count": len(published_commits),
        "absorbed_commit_count": absorbed,
        "commits_to_replay": commits_to_replay,
        "would_rebase_complete_published_range": True,
        "recovery_current_upstream": output_is_already_based_on_current_upstream(published_input_head, current_upstream),
    })
    return result


def ensure_clean_identity() -> tuple[str, str]:
    if not WORKTREE.is_dir():
        fail(f"integration worktree is absent: {WORKTREE}")
    if git("branch", "--show-current") != BRANCH:
        fail(f"worktree is not on {BRANCH}")
    # Under the exclusive cron lock the scheduler worktree never carries
    # human work — only dead runs' debris (e.g. run 12's case-collision
    # aftermath left a tracked file deleted on disk). Self-heal instead of
    # refusing; _ensure_pristine_tree still fails on anything it cannot
    # restore.
    _ensure_pristine_tree("run_start")
    old_head = git("rev-parse", "HEAD")
    # This checkout uses a deliberately narrow fork fetch refspec. Materialize
    # the configured integration branch explicitly so a branch rename does not
    # make identity verification depend on a stale local tracking ref.
    git("fetch", FORK_REMOTE, f"+refs/heads/{BRANCH}:refs/remotes/{FORK_REMOTE}/{BRANCH}", timeout=300)
    old_remote = git("rev-parse", f"refs/remotes/{FORK_REMOTE}/{BRANCH}")
    return old_head, old_remote


def synchronize_to_published_head(local_head: str, published_head: str) -> str:
    """Adopt the fetched fork tip, retaining any displaced local tip safely.

    The caller supplies immutable anchors captured around its fetch.  Re-read
    the checkout and tracking ref here so this mutating operation cannot reset
    an unexpected worktree or trust a missing/stale published ref.
    """
    published_ref = f"refs/remotes/{FORK_REMOTE}/{BRANCH}"
    if git("branch", "--show-current") != BRANCH:
        raise RuntimeError(f"checked out branch must be {BRANCH}")
    # Phantom-aware: since the 2026-08-17 publish the published tree itself
    # carries upstream's case-collision pair, so every NTFS checkout is
    # permanently "dirty" by one phantom line. Only real dirt refuses.
    if _real_dirt(_porcelain_lines()):
        raise RuntimeError("dirty working tree; refusing to synchronize published head")

    resolved_published = git("rev-parse", "--verify", published_ref, check=False)
    if not resolved_published:
        raise RuntimeError(f"missing published tracking ref: {published_ref}")
    if resolved_published != published_head:
        raise RuntimeError(
            f"published tracking ref changed: {published_ref}={resolved_published}, expected {published_head}"
        )

    current_head = git("rev-parse", "HEAD")
    if current_head != local_head:
        raise RuntimeError(f"local integration HEAD changed: {current_head}, expected {local_head}")
    if local_head == published_head:
        return published_head

    safe_branch = BRANCH.replace("/", "-").replace(" ", "-")
    safety_ref = f"safety/{safe_branch}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S-%f}-{local_head[:12]}"
    git("branch", safety_ref, local_head)
    git("reset", "--hard", published_head, timeout=120)
    if _real_dirt(_porcelain_lines()):
        raise RuntimeError("worktree is dirty after synchronizing published head")
    synchronized_head = git("rev-parse", "HEAD")
    if synchronized_head != published_head:
        raise RuntimeError(
            f"published-head synchronization failed: expected {published_head}, got {synchronized_head}"
        )
    return published_head


def restore_pre_push_checkout(published_input_head: str) -> None:
    """Leave a failed pre-push transaction exactly at its fetched input tip.

    The input tip is deliberately distinct from the scheduler's pre-run local
    tip: any divergent local tip was retained under a safety ref when the
    transaction adopted the authoritative published branch.
    """
    # The caller captured this input after ensure_clean_identity().  Re-check
    # the checkout before destructive cleanup so a changed worktree/branch is
    # never cleaned by a failed transaction.
    if git("branch", "--show-current") != BRANCH:
        raise RuntimeError(f"pre-push restoration refused outside {BRANCH}")
    _clear_stale_worktree_index_lock()
    abort = git("cherry-pick", "--abort", check=False)
    reset_error: Exception | None = None
    try:
        _git_write_with_lock_retry("reset", "--hard", published_input_head, timeout=120)
        # This transaction owns only the known integration worktree and has
        # already verified its branch above.  Remove *untracked* build/replay
        # debris, but deliberately retain ignored caches (no -x).
        git("clean", "-fd", timeout=120)
    except Exception as exc:
        reset_error = exc
    in_progress, dirty = cherry_pick_is_cleanly_aborted()
    actual_head = git("rev-parse", "HEAD", check=False)
    if reset_error or in_progress or dirty or actual_head != published_input_head:
        details = (
            f"abort_exit={abort!r}, reset_error={reset_error}, cherry_pick_in_progress={in_progress}, "
            f"dirty={dirty}, expected_head={published_input_head}, actual_head={actual_head}"
        )
        raise RuntimeError(f"pre-push restoration was incomplete: {details}") from reset_error


def push_rebased_output(published_input_head: str, rebased_output_head: str) -> None:
    """Publish once with the immutable SHA captured by the canonical fetch."""
    git(
        "push",
        f"--force-with-lease=refs/heads/{BRANCH}:{published_input_head}",
        FORK_REMOTE,
        f"{rebased_output_head}:refs/heads/{BRANCH}",
        timeout=600,
    )


def release_recovery_decision(
    *,
    published_input_head: str,
    rebased_output_head: str,
    branch_is_current_output: bool,
    release_exists: bool,
) -> dict[str, bool | str]:
    """Choose the no-rewrite path for a previously pushed output tip."""
    if branch_is_current_output:
        if release_exists:
            return {"replay": False, "push": False, "publish_release": False, "reason": "integration_and_release_already_current"}
        return {"replay": False, "push": False, "publish_release": True, "reason": "release_missing_for_current_output"}
    return {"replay": True, "push": True, "publish_release": True, "reason": "reconstruction_required"}


def output_is_already_based_on_current_upstream(published_input_head: str, upstream: str) -> bool:
    """True only when the published tip is already a descendant of this upstream."""
    return run(
        "git", "merge-base", "--is-ancestor", upstream, published_input_head, timeout=60, check=False
    ).returncode == 0


def github_token() -> str:
    # Git Credential Manager owns the secret; no token is logged or printed.
    result = subprocess.run(
        [resolve_executable("git"), "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        **windows_subprocess_kwargs(),
    )
    if result.returncode:
        raise RuntimeError("GitHub credential lookup failed")
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    token = values.get("password", "")
    if not token:
        raise RuntimeError("no GitHub credential is available for release publishing")
    return token


def github_request(method: str, url: str, token: str, data: bytes | None = None, content_type: str = "application/json") -> Any:
    request = urllib.request.Request(
        url,
        method=method,
        data=data,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": content_type,
            "User-Agent": "hermes-integration-release-windows",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            body = response.read()
            return json.loads(body.decode("utf-8")) if body else None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"GitHub API {method} failed ({exc.code}): {body}") from exc


def public_github_payload(url: str) -> Any:
    """Read public GitHub release data/assets without credentials."""
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "hermes-integration-release-windows"})
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.loads(response.read().decode("utf-8"))


def public_release_asset_bytes(url: str) -> bytes:
    """Download a public release asset for the same verification users receive."""
    with urllib.request.urlopen(url, timeout=180) as response:
        return response.read()


def verify_existing_integration_release(commit: str, expected_sha: str | None = None) -> dict[str, Any]:
    """Inspect public metadata and assets; only a cryptographically complete release is reusable.

    Without an expected local installer checksum this can identify a candidate,
    never declare a release complete.  Call again after the build before a no-op.
    """
    payload = public_github_payload(f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=100")
    if not isinstance(payload, list):
        raise RuntimeError("public GitHub releases metadata was not a list")
    suffix = f"-{commit[:12]}"
    release_item = next((item for item in payload if isinstance(item, dict)
        and str(item.get("tag_name", "")).startswith(RELEASE_PREFIX)
        and str(item.get("tag_name", "")).endswith(suffix)), None)
    if release_item is None:
        return {"candidate": False, "complete": False, "reason": "release_missing"}
    if str(release_item.get("target_commitish", "")) != commit:
        return {"candidate": True, "complete": False, "reason": "release_target_commit_mismatch", "release": release_item}
    assets = {str(asset.get("name")): str(asset.get("browser_download_url", ""))
              for asset in release_item.get("assets", []) if isinstance(asset, dict)}
    needed = {"Hermes-Setup.exe", "SHA256SUMS.txt", "PROVENANCE.json"}
    if not needed <= assets.keys() or not all(assets[name] for name in needed):
        return {"candidate": True, "complete": False, "reason": "release_assets_incomplete", "release": release_item}
    if expected_sha is None:
        return {"candidate": True, "complete": False, "reason": "expected_installer_checksum_unknown", "release": release_item}
    sums = public_release_asset_bytes(assets["SHA256SUMS.txt"]).decode("utf-8", errors="replace")
    provenance = json.loads(public_release_asset_bytes(assets["PROVENANCE.json"]).decode("utf-8"))
    expected_sum_line = f"{expected_sha}  Hermes-Setup.exe"
    if expected_sum_line not in sums or not isinstance(provenance, dict):
        return {"candidate": True, "complete": False, "reason": "release_checksum_manifest_invalid", "release": release_item}
    if any(provenance.get(key) != value for key, value in {
        "repository": REPOSITORY, "branch": BRANCH, "commit": commit,
        "launcher": "Hermes-Setup.exe", "sha256": expected_sha,
    }.items()):
        return {"candidate": True, "complete": False, "reason": "release_provenance_invalid", "release": release_item}
    actual_sha = hashlib.sha256(public_release_asset_bytes(assets["Hermes-Setup.exe"])).hexdigest()
    if actual_sha != expected_sha:
        return {"candidate": True, "complete": False, "reason": "release_public_installer_checksum_invalid", "release": release_item}
    return {"candidate": True, "complete": True, "reason": "release_complete", "release": release_item}


def publish_release(tag: str, commit: str, launcher: Path, checksum: str) -> tuple[str, list[str]]:
    if not GH_EXE.is_file():
        raise RuntimeError(f"authenticated GitHub CLI is unavailable: {GH_EXE}")
    manifest = {
        "repository": REPOSITORY,
        "branch": BRANCH,
        "commit": commit,
        "launcher": launcher.name,
        "sha256": checksum,
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    with tempfile.TemporaryDirectory(prefix="hermes-integration-release-") as temp_dir:
        stage = Path(temp_dir)
        sums = stage / "SHA256SUMS.txt"
        provenance = stage / "PROVENANCE.json"
        sums.write_text(f"{checksum}  {launcher.name}\n", encoding="utf-8")
        provenance.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        create = run(
            str(GH_EXE), "release", "create", tag,
            "--repo", REPOSITORY,
            "--target", commit,
            "--title", f"Hermes integration {commit[:12]}",
            "--notes", "Automated integration prerelease. Blue Windows bootstrap/updater launcher (not an MSI/NSIS versioned installer); see PROVENANCE.json and SHA256SUMS.txt.",
            "--prerelease",
            timeout=900, check=False,
        )
        # A timeout/error after tag creation leaves a partial release.  Repair
        # that known tag idempotently, replacing assets rather than blind-create.
        if create.returncode:
            run(str(GH_EXE), "release", "view", tag, "--repo", REPOSITORY, timeout=120)
        run(str(GH_EXE), "release", "upload", tag, str(launcher), str(sums), str(provenance),
            "--repo", REPOSITORY, "--clobber", timeout=900)
    view = run(str(GH_EXE), "release", "view", tag, "--repo", REPOSITORY, "--json", "url", "--jq", ".url", timeout=120).stdout.strip()
    releases = json.loads(run(str(GH_EXE), "release", "list", "--repo", REPOSITORY, "--limit", "100", "--json", "tagName,createdAt", timeout=120).stdout)
    automated = sorted(
        (item for item in releases if str(item.get("tagName", "")).startswith(RELEASE_PREFIX)),
        key=lambda item: item.get("createdAt", ""),
        reverse=True,
    )
    removed: list[str] = []
    for old in automated[RETAIN_RELEASES:]:
        old_tag = str(old["tagName"])
        run(str(GH_EXE), "release", "delete", old_tag, "--repo", REPOSITORY, "--yes", timeout=180)
        removed.append(old_tag)
    return view, removed


def verify_public_asset(url: str, expected_sha: str) -> None:
    # The fork is intentionally public.  Verify the user-facing download path,
    # not merely the authenticated upload response.
    with urllib.request.urlopen(url, timeout=180) as response:
        digest = hashlib.sha256()
        for block in iter(lambda: response.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != expected_sha:
        raise RuntimeError("public GitHub download checksum did not match uploaded installer")


def _finish_verified_publication(
    result: dict[str, Any],
    *,
    started_at: datetime,
    release_system_source_sha: str | None,
    published_product_sha: str,
    changed: bool,
    consume_pin: bool,
    perform_sync: bool,
) -> int:
    """Finish local work after public integrity verification has committed success.

    A complete public release is the irreversible transaction boundary. Every
    operation here is local bookkeeping, diagnostics, or reporting; an
    ``Exception`` from any one is retained as a structured warning and cannot
    retroactively turn the verified publication into a failed run. Deliberate
    process-control exceptions remain outside this boundary.
    """
    warnings: list[dict[str, str]] = []

    def record_warning(operation: str, exc: Exception) -> None:
        warning = {
            "operation": operation,
            "error": redact_process_output(f"{type(exc).__name__}: {exc}"),
        }
        warnings.append(warning)
        result["warnings"] = warnings
        try:
            log(
                "WARNING verified publication post-commit operation failed: "
                f"operation={operation} error={warning['error']}"
            )
        except Exception:
            pass

    def best_effort(operation: str, action: Any, default: Any = None) -> Any:
        try:
            return action()
        except Exception as exc:
            record_warning(operation, exc)
            return default

    if consume_pin:
        best_effort("consume_upstream_pin", _consume_upstream_pin_on_publish)
    if perform_sync:
        best_effort("emit_sync_stage", lambda: emit_stage("sync"))
        result["sync"] = best_effort(
            "sync_operational_copies",
            lambda: sync_operational_copies(release_system_source_sha, published_product_sha),
            {
                "ok": False,
                "error": "post-publication sync raised before returning an outcome",
                "source_sha": release_system_source_sha,
                "published_product_sha": published_product_sha,
            },
        )
    result["parked_pins"] = best_effort("parked_pin_summary", parked_pin_summary, [])
    best_effort("result_log", lambda: log(json.dumps(result, sort_keys=True)))
    best_effort("resolve_failure_investigator_success", resolve_failure_investigator_success)
    best_effort(
        "emit_fleet_receipt",
        lambda: emit_fleet_receipt(started_at, outcome="produced", changed=changed),
    )
    try:
        return _emit_result(result, dry_run=False)
    except Exception as exc:
        record_warning("emit_result", exc)
        # _emit_result may have failed while deriving provenance, serializing,
        # or writing stdout. Make one minimal structured-output attempt; if no
        # reporting channel remains, the verified publication still exits 0.
        try:
            print(json.dumps(result, ensure_ascii=False, default=str))
        except Exception:
            pass
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="inspect only; do not modify GitHub or the worktree")
    parser.add_argument(
        "--holder", default="scheduler",
        help="identity recorded on the exclusive lock, for busy/stale-reclaim diagnostics (default: scheduler)",
    )
    parser.add_argument(
        "--authority-token", default=None, dest="authority_token",
        help=(
            "path to a spawner-minted authority record (U9/KTD12). Required for push and "
            "publish whenever --holder is not 'scheduler'; ignored on the scheduler path."
        ),
    )
    parser.add_argument(
        "--canary-manifest", default=None, dest="canary_manifest",
        help=(
            "U11 witnessed-canary entry point: resolve MANIFEST_PATH from this file instead of the "
            "tracked manifest, for this run only. Outside sync.py's TRACKED_SET (no stamp check); the "
            "tracked operational manifest copy is still verified. Result JSON gains \"canary\": true."
        ),
    )
    args = parser.parse_args()
    reset_run_reconciliation_state()
    reset_run_authority_state(args.authority_token)
    reset_run_provenance_state()
    reset_run_canary_state()
    if args.canary_manifest:
        apply_canary_manifest(args.canary_manifest)
    # Run-start integrity gate (U2/KTD2, R14): before ANY mutation -- before
    # the lock, before any fetch, even before the read-only dry-run
    # inspection below -- the operational copies must match the published
    # git tree. fail()s (raising SystemExit) on a real-run mismatch.
    emit_stage("integrity_gate")
    sync_integrity = integration_scripts_integrity_check(dry_run=args.dry_run)
    # A dry-run must not acquire the persistent lock, fetch (which updates
    # tracking refs), or invoke any normal-path verification that materializes
    # refs.  It intentionally performs only the read-only inspection above.
    if args.dry_run:
        emit_stage("dry_run_gate")
        try:
            result = inspect_dry_run()
            result["sync_integrity"] = sync_integrity
            return _emit_result(result, dry_run=True)
        except Exception as exc:
            return _emit_result(
                {
                    "ok": False,
                    "error": f"dry-run inspection failed without mutation: {exc}",
                    "sync_integrity": sync_integrity,
                },
                dry_run=True, exit_code=1,
            )
    started_at = datetime.now().astimezone()
    pre_run_local_head: str | None = None
    published_input_head: str | None = None
    rebased_output_head: str | None = None
    branch_pushed = False
    public_integrity_verified = False
    publication_result: dict[str, Any] | None = None
    stage = "prepare"
    with exclusive_lock(args.holder):
        try:
            stage = "identity"
            pre_run_local_head, _pre_fetch_remote_head = ensure_clean_identity()
            stage = "fetch"
            emit_stage("fetch")
            git("fetch", UPSTREAM_REMOTE, "--prune", timeout=300)
            git("fetch", FORK_REMOTE, "--prune", timeout=300)
            stage = "resolve_refs"
            upstream_tip = git("rev-parse", f"{UPSTREAM_REMOTE}/{UPSTREAM_REF.removeprefix('refs/heads/')}")
            # Retry-loop determinism (2026-08-17): chasing upstream's tip
            # across retries reshuffles conflicts and invalidates the
            # resolution cache. A pin freezes the base until a publish lands.
            # Liveness discipline mirrors the lock's dead-PID rule: the file
            # pin is single-shot (deleted on successful publish), TTL-bounded
            # (expired pins are ignored, loudly, and removed), and every run
            # measures how far behind the live tip its base sits.
            upstream = _resolve_upstream_base(upstream_tip)
            upstream_behind_by = git(
                "rev-list", "--count", f"{upstream}..{upstream_tip}", check=False
            ) or "0"
            if upstream != upstream_tip:
                log(f"UPSTREAM_BASE_BEHIND_TIP behind_by={upstream_behind_by} base={upstream} tip={upstream_tip}")
            # R7: "this run's fetched upstream when available" for provenance
            # derivation -- recorded as soon as it is resolved so a failure
            # anywhere downstream (including inside fail()) still derives
            # against the freshest available upstream.
            record_run_upstream_ref(upstream)
            # This is the sole lease authority for this transaction.  Do not
            # overwrite it after the fetch, including after a failed push.
            published_input_head = git("rev-parse", f"refs/remotes/{FORK_REMOTE}/{BRANCH}")
            if not args.dry_run:
                synchronize_to_published_head(pre_run_local_head, published_input_head)
            stage = "reconstruct"
            emit_stage("reconstruct")
            published_base, published_commits = published_integration_range(published_input_head, upstream)
            # If the published line already descends from fetched upstream, retain
            # it exactly and append only newly required invariants.  Otherwise
            # reconstruct all published-only work first.  In both cases direct
            # published commits remain authoritative and are tracked explicitly.
            recovering_current_output = output_is_already_based_on_current_upstream(published_input_head, upstream)
            if recovering_current_output:
                published_records = _exact_published_records(published_commits)
            else:
                git("branch", f"safety/{BRANCH.replace('/', '-')}-{datetime.now():%Y%m%d-%H%M%S}", published_input_head)
                published_records = replay_published_integration_range(
                    published_input_head, upstream, return_records=True
                )

            rebased_output_head = git("rev-parse", "HEAD")
            # The branch is the single source of truth (2026-08-17 directive:
            # "upstream with our changes on top" is the entire contract).
            # Retired = patch already absorbed upstream, no longer monitored;
            # everything else replayed, resolved in-job, or parked to an
            # agent. One tally line tells the whole story.
            tally: dict[str, int] = {}
            for record in published_records:
                tally[str(record["status"])] = tally.get(str(record["status"]), 0) + 1
            emit_stage(
                "retire",
                detail=" ".join(
                    f"{status}={count}" for status, count in sorted(tally.items())
                ) or "nothing_to_replay",
            )
            for record in published_records:
                if record["status"] == "absorbed_patch_equivalent":
                    log(f"RETIRED commit={record['source_commit']} reason=already_upstream")

            stage = "validate_output"
            emit_stage("validate")
            validate_published_commit_preservation(
                published_commits, upstream, rebased_output_head, records=published_records
            )
            _ensure_pristine_tree("post_reconstruction")
            needs_push = rebased_output_head != published_input_head
            recovering_unchanged_output = recovering_current_output and not needs_push
            stage = "verify_build"
            emit_stage("build")
            run("git", "diff", "--check", timeout=120)
            run("npm", "--workspace", "apps/bootstrap-installer", "run", "typecheck", timeout=900)
            # This test deliberately spawns Windows `timeout /t 30`.  In the
            # noninteractive cron service, timeout exits immediately and makes
            # the sibling-PID contention assertion false.  Run the remaining
            # real bootstrap tests; preserve the excluded test's failure as an
            # explicit logged platform limitation rather than claiming 53/53.
            run(
                "cargo",
                "test",
                "--manifest-path",
                "apps/bootstrap-installer/src-tauri/Cargo.toml",
                "--",
                "--skip",
                "update::tests::acquire_refuses_while_a_live_updater_owns_the_marker",
                timeout=1200,
            )
            run(
                "npm",
                "--workspace", "apps/bootstrap-installer", "run", "tauri:build", "--", "--no-bundle",
                timeout=2700,
                # These are baked into the binary by build.rs.  The release must
                # fetch this fork/commit, never silently fall back to upstream.
                extra_env={
                    "HERMES_BUILD_PIN_REPOSITORY": REPOSITORY,
                    "HERMES_BUILD_PIN_COMMIT": rebased_output_head,
                    "HERMES_BUILD_PIN_BRANCH": BRANCH,
                },
            )
            launcher = resolve_built_launcher()
            if not launcher.is_file() or launcher.stat().st_size < 1_000_000:
                raise RuntimeError(f"expected bootstrap/updater launcher was not built: {launcher}")
            checksum = sha256(launcher)

            stage = "verify_existing_release"
            existing_release = verify_existing_integration_release(rebased_output_head, expected_sha=checksum)
            recovery = release_recovery_decision(
                published_input_head=published_input_head,
                rebased_output_head=rebased_output_head,
                branch_is_current_output=recovering_unchanged_output,
                release_exists=bool(existing_release["complete"]),
            )
            if recovering_unchanged_output and not recovery["publish_release"]:
                publication_result = {"ok": True, "changed": False, "reason": recovery["reason"], "head": rebased_output_head, "upstream": upstream}
                public_integrity_verified = True
                return _finish_verified_publication(
                    publication_result,
                    started_at=started_at,
                    release_system_source_sha=sync_integrity.get("source_sha"),
                    published_product_sha=rebased_output_head,
                    changed=False,
                    consume_pin=False,
                    perform_sync=False,
                )
            if not recovering_current_output and not needs_push and existing_release["complete"]:
                publication_result = {"ok": True, "changed": False, "reason": "integration_and_release_already_current", "head": rebased_output_head, "upstream": upstream}
                public_integrity_verified = True
                return _finish_verified_publication(
                    publication_result,
                    started_at=started_at,
                    release_system_source_sha=sync_integrity.get("source_sha"),
                    published_product_sha=rebased_output_head,
                    changed=False,
                    consume_pin=False,
                    perform_sync=False,
                )

            if needs_push:
                stage = "push"
                # Re-checked here, immediately before the action (R20/KTD5).
                require_authority("push", holder=args.holder, token_path=args.authority_token)
                push_rebased_output(published_input_head, rebased_output_head)
                branch_pushed = True
            stage = "verify_pushed_branch"
            remote_after = git("ls-remote", FORK_REMOTE, f"refs/heads/{BRANCH}").split()[0]
            if remote_after != rebased_output_head:
                raise RuntimeError(f"fork branch verification failed: expected {rebased_output_head}, got {remote_after}")
            raw = f"https://raw.githubusercontent.com/{REPOSITORY}/{rebased_output_head}/scripts/install.ps1"
            with urllib.request.urlopen(raw, timeout=90) as response:
                if response.status != 200:
                    raise RuntimeError(f"pinned installer script returned HTTP {response.status}")

            # Repair a partial tag for this exact output rather than minting a
            # second tag after a timeout/error.  A fresh tag is used only when
            # public metadata has no candidate for this commit.
            existing_tag = str(existing_release.get("release", {}).get("tag_name", ""))
            tag = existing_tag if existing_release.get("candidate") and existing_tag else (
                f"{RELEASE_PREFIX}{datetime.now().astimezone():%Y%m%d}-{rebased_output_head[:12]}"
            )
            stage = "publish_release"
            # A second, independent check: a window that expired between the
            # push and here refuses the publish (KTD5's per-action re-check).
            require_authority("publish", holder=args.holder, token_path=args.authority_token)
            emit_stage("publish", detail=f"tag={tag}")
            release_url, removed = publish_release(tag, rebased_output_head, launcher, checksum)
            public_asset = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{urllib.parse.quote(launcher.name)}"
            verify_public_asset(public_asset, checksum)
            stage = "verify_published_release"
            # GitHub's API is read-after-write laggy: run 15 published a
            # complete release at :07/:09 and the :13 probe still saw
            # release_missing, failing a fully successful run. Give
            # propagation up to a minute before believing a negative.
            repaired_release: dict[str, Any] = {"complete": False, "reason": "unverified"}
            for attempt in range(5):
                repaired_release = verify_existing_integration_release(rebased_output_head, expected_sha=checksum)
                if repaired_release["complete"]:
                    break
                log(
                    f"PUBLISH_VERIFY_RETRY attempt={attempt + 1} "
                    f"reason={repaired_release['reason']} (API propagation grace)"
                )
                time.sleep(15)
            if not repaired_release["complete"]:
                raise RuntimeError(f"published release integrity verification failed: {repaired_release['reason']}")
            public_integrity_verified = True

            # A checksum-backed public verification is the irreversible commit
            # boundary. Nothing local or diagnostic after this point may
            # retroactively report the published release as failed.
            publication_result = {
                "ok": True,
                "changed": True,
                "branch": BRANCH,
                "previous_head": pre_run_local_head,
                "published_input_head": published_input_head,
                "rebased_output_head": rebased_output_head,
                "branch_pushed": branch_pushed,
                "head": rebased_output_head,
                "upstream": upstream,
                "upstream_behind_by": upstream_behind_by,
                "release": release_url,
                "tag": tag,
                "launcher": public_asset,
                "sha256": checksum,
                "retention_deleted": removed,
            }
            if args.holder != "scheduler":
                # Audit trail for a token-gated finish; absent (not empty) on
                # the scheduler path so the nightly brief keeps its shape.
                publication_result["authority"] = list(AUTHORITY_GRANTS)
            return _finish_verified_publication(
                publication_result,
                started_at=started_at,
                release_system_source_sha=sync_integrity.get("source_sha"),
                published_product_sha=rebased_output_head,
                changed=True,
                consume_pin=True,
                perform_sync=True,
            )
        except Exception as exc:
            if public_integrity_verified:
                # Belt-and-suspenders around the entire post-commit region:
                # even an unexpected bug in its coordinator cannot fall
                # through to restoration, failure investigation, a failed
                # Fleet receipt, or fail().
                message = redact_process_output(f"{type(exc).__name__}: {exc}")
                committed = publication_result or {
                    "ok": True,
                    "changed": True,
                    "branch": BRANCH,
                    "head": rebased_output_head,
                    "release_integrity_verified": True,
                }
                committed.setdefault("warnings", []).append({
                    "operation": "post_commit_boundary",
                    "error": message,
                })
                try:
                    log(
                        "WARNING verified publication post-commit coordinator failed: "
                        f"{message}"
                    )
                except Exception:
                    pass
                try:
                    return _emit_result(committed, dry_run=False)
                except Exception:
                    return 0

            restoration_error = ""
            if published_input_head and not branch_pushed:
                try:
                    restore_pre_push_checkout(published_input_head)
                    restoration_error = " (local pre-push transaction restored to published input)"
                except Exception as restore_exc:
                    restoration_error = f" (CRITICAL: local restoration failed: {restore_exc})"
            elif branch_pushed:
                restoration_error = (
                    " (branch_pushed=true; no local reset or backward force-push was attempted; "
                    f"rebased_output_head={rebased_output_head}; post-push release/validation failure)"
                )
            message = redact_process_output(f"{exc}{restoration_error}")
            emit_stage(stage, ok=False, detail=message)
            launch_failure_investigator(stage=stage, error=message)
            emit_fleet_receipt(started_at, outcome="failed", changed=branch_pushed, error=message)
            fail(message)


if __name__ == "__main__":
    raise SystemExit(main())
