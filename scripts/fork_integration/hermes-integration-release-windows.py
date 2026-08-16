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
    settings = {"model": "", "provider": "", "reasoning_effort": ""}
    config_path = HERMES_HOME / "config.yaml"
    try:
        import yaml
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        configured = config.get("release_failure_investigator", {})
        if not isinstance(configured, dict):
            configured = {}
        model_config = config.get("model", {}) if isinstance(config.get("model"), dict) else {}
        agent_config = config.get("agent", {}) if isinstance(config.get("agent"), dict) else {}
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


def resolve_failure_investigator_success() -> None:
    """A real successful return closes all open dedupe keys for the job."""
    try:
        _failure_investigator_module().resolve_success(FLEET_JOB_ID, HERMES_HOME)
    except Exception:
        # Success remains quiet even if a local diagnostic file cannot be updated.
        pass


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
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
    raise SystemExit(code)


def write_reconstruction_review_request(upstream: str, patch: dict[str, str] | None, error: Exception) -> Path:
    """Persist a two-reviewer Opus brief; never auto-resolve a conflict or publish."""
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
    return run(
        "git", "-c", "rerere.enabled=false", "-c", "rerere.autoupdate=false", *args,
        timeout=timeout, check=check, log_failure=log_failure,
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
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Split required patches into patches to apply and exact upstream equivalents.

    An upstream commit is accepted only when both its subject and stable patch
    identity match the manifest source. A matching subject with a different
    patch is an ambiguity, not an absorption: fail closed before mutation,
    unless the manifest declares a reviewed replacement to apply instead.
    """
    to_apply: list[dict[str, str]] = []
    absorbed: list[dict[str, str]] = []
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
        candidates = [
            line for line in git(
                "log", upstream, "--format=%H", "--fixed-strings", f"--grep={patch['subject']}", check=False
            ).splitlines() if line
        ]
        candidate_ids = {candidate: stable_patch_id(candidate) for candidate in candidates}
        equivalent = next((candidate for candidate, patch_id in candidate_ids.items() if patch_id in accepted_ids), None)
        if equivalent:
            absorbed.append({
                "commit": patch["commit"],
                "subject": patch["subject"],
                "upstream_commit": equivalent,
                "output_patch_id": candidate_ids[equivalent],
            })
        elif candidates and not patch.get("reviewed_replacement"):
            raise RuntimeError(
                "upstream has a same-subject but non-equivalent required patch; "
                f"manual reconciliation is required: source={patch['commit']} subject={patch['subject']!r} "
                f"upstream_candidates={candidates}"
            )
        else:
            to_apply.append(patch)
    return to_apply, absorbed


def upstream_patch_resolution(
    upstream: str, *, records: list[dict[str, Any]] | None = None
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Resolve the fork-only overlay patches against upstream."""
    return patch_resolution(upstream, REQUIRED_PATCHES, records=records)


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
            replacement = patch.get("reviewed_replacement")
            applied_commit = replacement["commit"] if replacement else patch["commit"]
            prior_application = next((
                record for record in records
                if record.get("applied_commit", record["source_commit"]) == applied_commit
            ), None)
            if prior_application is not None:
                output_patch_id = prior_application["output_patch_id"]
                if output_patch_id not in _accepted_output_patch_ids(patch):
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
                })
                continue
            try:
                git("cherry-pick", applied_commit, timeout=900)
            except Exception:
                if not _cherry_pick_stopped_empty():
                    raise
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


def _restore_replay_checkout(published_input_head: str) -> tuple[bool, bool, str, Exception | None]:
    """Abort replay and remove all owned tracked/untracked transaction debris."""
    git("cherry-pick", "--abort", check=False)
    restore_error: Exception | None = None
    try:
        git("reset", "--hard", published_input_head, timeout=120)
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

    This helper performs no remote mutation.  A conflict restores the original
    published checkout, leaves no cherry-pick state, and records the exact
    source commit for human reconstruction review.
    """
    base, commits = published_integration_range(published_input_head, upstream_head)
    # An already-published/absorbed branch needs no reconstruction.  Return
    # before comparing patches or resetting so this idempotent case stays
    # read-only at the worktree level.
    if not commits:
        return []
    absorbed_commits = _absorbed_published_commits(upstream_head, published_input_head, base)
    git("reset", "--hard", upstream_head, timeout=120)
    replayed: list[str] = []
    records: list[dict[str, Any]] = []
    failed_patch: dict[str, str] | None = None
    try:
        for commit in commits:
            subject = git("show", "-s", "--format=%s", commit)
            failed_patch = {"commit": commit, "subject": subject}
            parents = _commit_parents(commit)
            is_merge = len(parents) > 1
            if not is_merge and commit in absorbed_commits:
                records.append({
                    "kind": "published",
                    "status": "absorbed_patch_equivalent",
                    "source_commit": commit,
                    "output_commit": upstream_head,
                    "output_patch_id": _commit_patch_id(commit),
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
                git(*cherry_pick_args, commit, timeout=900)
            except Exception:
                if not is_merge or not _cherry_pick_stopped_empty():
                    raise
                # The side-parent commits can already represent the whole
                # first-parent delta.  Resolve Git's empty stop explicitly and
                # retain the merge source in the preservation ledger.
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
            replayed.append(commit)
            output_commit = git("rev-parse", "HEAD")
            records.append({
                "kind": "published",
                "status": "applied_merge_mainline" if is_merge else "applied",
                "source_commit": commit,
                "output_commit": output_commit,
                "output_patch_id": _commit_patch_id(output_commit),
                **({"parent_count": len(parents), "mainline": 1} if is_merge else {}),
            })
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


def cherry_pick_is_cleanly_aborted() -> tuple[bool, bool]:
    """Return whether a failed single cherry-pick left state or changes behind."""
    in_progress = run("git", "rev-parse", "--verify", "-q", "CHERRY_PICK_HEAD", check=False).returncode == 0
    dirty = bool(git("status", "--porcelain"))
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
    return [{
        "kind": "published",
        "status": "exact_reachable",
        "source_commit": commit,
        "output_commit": commit,
        "output_patch_id": _commit_patch_id(commit),
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
                patch_id for commit in commits if (patch_id := _commit_patch_id(commit)) is not None
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
        output_commit = record.get("output_commit", "")
        recorded_patch_id = record.get("output_patch_id")
        accepted = _accepted_output_patch_ids(patch)
        # Do not trust the recorded output_patch_id at face value: recompute
        # it from the reconstructed tree so a record that was stamped once
        # and never re-verified cannot silently drift from what output_commit
        # actually contains.
        recomputed = stable_patch_id(output_commit) if output_commit else None
        if recomputed is None or recomputed not in accepted or recomputed != recorded_patch_id:
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
                if merge_status not in {"applied_merge_mainline", "merge_delta_already_represented"}:
                    raise RuntimeError(f"published merge has invalid preservation status: {published}")
                if merge_status == "applied_merge_mainline" and record.get("output_patch_id") is None:
                    raise RuntimeError(f"published merge delta produced no recorded output identity: {published}")
                continue
            # Direct commits retain the strict stable-patch identity check.  The
            # merge path above is separate because replaying all parents first
            # can reduce a merge's first-parent delta to only its unique
            # conflict-resolution portion (or to an already-represented empty).
            source_patch_id = _commit_patch_id(published)
            if source_patch_id is not None and record.get("output_patch_id") != source_patch_id:
                raise RuntimeError(f"published commit was not preserved by patch identity: {published}")
        return
    commits = _represented_commits(upstream, rebased_head)
    identities = {patch_id for commit in commits if (patch_id := _commit_patch_id(commit)) is not None}
    empty_subjects = {
        git("show", "-s", "--format=%s", commit)
        for commit in commits
        if _commit_patch_id(commit) is None
    }
    for published in published_commits:
        patch_id = _commit_patch_id(published)
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


@contextmanager
def exclusive_lock() -> Any:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        fail(f"another integration-release run holds {LOCK_PATH}")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat()}, handle)
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
    if git("status", "--porcelain"):
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
    if git("status", "--porcelain"):
        fail("integration worktree is dirty; no rebase was attempted")
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
    if git("status", "--porcelain"):
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
    if git("status", "--porcelain"):
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
    abort = git("cherry-pick", "--abort", check=False)
    reset_error: Exception | None = None
    try:
        git("reset", "--hard", published_input_head, timeout=120)
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="inspect only; do not modify GitHub or the worktree")
    args = parser.parse_args()
    # A dry-run must not acquire the persistent lock, fetch (which updates
    # tracking refs), or invoke any normal-path verification that materializes
    # refs.  It intentionally performs only the read-only inspection above.
    if args.dry_run:
        try:
            result = inspect_dry_run()
            print(json.dumps(result))
            return 0
        except Exception as exc:
            print(json.dumps({"ok": False, "error": f"dry-run inspection failed without mutation: {exc}"}, ensure_ascii=False))
            return 1
    started_at = datetime.now().astimezone()
    pre_run_local_head: str | None = None
    published_input_head: str | None = None
    rebased_output_head: str | None = None
    branch_pushed = False
    stage = "prepare"
    with exclusive_lock():
        try:
            stage = "identity"
            pre_run_local_head, _pre_fetch_remote_head = ensure_clean_identity()
            stage = "fetch"
            git("fetch", UPSTREAM_REMOTE, "--prune", timeout=300)
            git("fetch", FORK_REMOTE, "--prune", timeout=300)
            stage = "resolve_refs"
            upstream = git("rev-parse", f"{UPSTREAM_REMOTE}/{UPSTREAM_REF.removeprefix('refs/heads/')}")
            # This is the sole lease authority for this transaction.  Do not
            # overwrite it after the fetch, including after a failed push.
            published_input_head = git("rev-parse", f"refs/remotes/{FORK_REMOTE}/{BRANCH}")
            if not args.dry_run:
                synchronize_to_published_head(pre_run_local_head, published_input_head)
            stage = "verify_foundations"
            verified_foundations = verify_upstream_foundations()
            stage = "verify_manifest"
            verify_manifest_sources()
            stage = "reconstruct"
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

            current_output = git("rev-parse", "HEAD")
            stage = "apply_foundations"
            foundation_to_apply, absorbed_foundations = patch_resolution(
                current_output, FOUNDATION_PATCHES, records=published_records
            )
            foundation_records = _resolution_records(absorbed_foundations, "foundation")
            foundation_records.extend(apply_required_patches(
                foundation_to_apply,
                published_input_head=published_input_head,
                upstream_head=upstream,
                kind="foundation",
            ))

            # Resolve components after foundations are present: a foundation may
            # itself represent a required component patch, and components are
            # required invariants rather than an exclusive published allow-list.
            current_output = git("rev-parse", "HEAD")
            stage = "apply_components"
            patches_to_apply, absorbed_components = upstream_patch_resolution(
                current_output, records=[*published_records, *foundation_records]
            )
            component_records = _resolution_records(absorbed_components, "component")
            component_records.extend(apply_required_patches(
                patches_to_apply,
                published_input_head=published_input_head,
                upstream_head=upstream,
                kind="component",
            ))

            rebased_output_head = git("rev-parse", "HEAD")
            stage = "validate_output"
            validate_required_components(upstream, rebased_output_head, records=component_records)
            validate_required_foundations(upstream, rebased_output_head, records=foundation_records)
            validate_published_commit_preservation(
                published_commits, upstream, rebased_output_head, records=published_records
            )
            if git("status", "--porcelain"):
                raise RuntimeError("worktree is dirty after integration reconstruction/application")
            needs_push = rebased_output_head != published_input_head
            recovering_unchanged_output = recovering_current_output and not needs_push
            stage = "verify_build"
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
                result = {"ok": True, "changed": False, "reason": recovery["reason"], "head": rebased_output_head, "upstream": upstream}
                log(json.dumps(result, sort_keys=True))
                resolve_failure_investigator_success()
                emit_fleet_receipt(started_at, outcome="produced", changed=False)
                print(json.dumps(result))
                return 0
            if not recovering_current_output and not needs_push and existing_release["complete"]:
                result = {"ok": True, "changed": False, "reason": "integration_and_release_already_current", "head": rebased_output_head, "upstream": upstream}
                log(json.dumps(result, sort_keys=True))
                resolve_failure_investigator_success()
                emit_fleet_receipt(started_at, outcome="produced", changed=False)
                print(json.dumps(result))
                return 0

            if needs_push:
                stage = "push"
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
            release_url, removed = publish_release(tag, rebased_output_head, launcher, checksum)
            public_asset = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{urllib.parse.quote(launcher.name)}"
            verify_public_asset(public_asset, checksum)
            stage = "verify_published_release"
            repaired_release = verify_existing_integration_release(rebased_output_head, expected_sha=checksum)
            if not repaired_release["complete"]:
                raise RuntimeError(f"published release integrity verification failed: {repaired_release['reason']}")
            result = {
                "ok": True,
                "changed": True,
                "branch": BRANCH,
                "previous_head": pre_run_local_head,
                "published_input_head": published_input_head,
                "rebased_output_head": rebased_output_head,
                "branch_pushed": branch_pushed,
                "head": rebased_output_head,
                "upstream": upstream,
                "release": release_url,
                "tag": tag,
                "launcher": public_asset,
                "sha256": checksum,
                "retention_deleted": removed,
            }
            log(json.dumps(result, sort_keys=True))
            resolve_failure_investigator_success()
            emit_fleet_receipt(started_at, outcome="produced", changed=True)
            print(json.dumps(result))
            return 0
        except Exception as exc:
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
            launch_failure_investigator(stage=stage, error=message)
            emit_fleet_receipt(started_at, outcome="failed", changed=branch_pushed, error=message)
            fail(message)


if __name__ == "__main__":
    raise SystemExit(main())
