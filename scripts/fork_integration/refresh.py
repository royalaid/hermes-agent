#!/usr/bin/env python3
"""Compose fresh upstream with the published fork range, then publish by lease."""
from __future__ import annotations
import argparse, json, os, shutil, subprocess, sys, tempfile, time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true"}
FETCH_RATE_LIMIT_RETRY_DELAYS = (30, 120, 300)
class RefreshError(RuntimeError): pass
@dataclass(frozen=True)
class TreeAssertion:
    path: str
    contains: tuple[str, ...] = ()
    absent: tuple[str, ...] = ()
    ordered: tuple[tuple[str, str], ...] = ()
KILL_ALL_MERGE = "merge: integrate kill-all Windows updater baseline"
HOLDER_DETECTION_MERGE = "Merge fix/update-scanner-carrier-envelope: kernel-proven update holder detection replaces the kill-all"
OWNER_AWARE_SIDEBAR_MERGE = "Merge branch 'feat/owner-aware-sidebar-sessions' into rebuild/fork-integration-20260902"
DURABLE_TODO_MERGE = "Merge draft PR #99644: durable Todo state"
PLUGIN_SERVICE_UNIT_MERGE = "Merge fix/windows-update-plugin-service-unit: stop plugin service units host-first, make force-release able to kill, repair the published tip (#6)"
RUN_LEDGER_1_MERGE = "Merge pull request #14 from royalaid/docs/update-run-ledger"
RUN_LEDGER_2_MERGE = "Merge pull request #15 from royalaid/docs/update-run-ledger-2"
RUN_LEDGER_3_MERGE = "Merge pull request #16 from royalaid/docs/update-run-ledger-3"
CANONICAL_UPDATER_MERGE = "Merge canonical Windows updater PR into rebased fork integration"
UPDATER_CI_MERGE = "Merge upstream updater CI fixes from PR #104687"
RESTORED_UPDATER_DRAIN_MERGE = "Merge pull request #17 from royalaid/restore/fork-updater-drain-20260907"
UPDATER_GUI_SHIM_MERGE = "Merge pull request #18 from royalaid/fix/updater-gui-shim-20260909"
CHARACTERIZED_MERGE_SIDE_SUBJECTS = frozenset({DURABLE_TODO_MERGE, OWNER_AWARE_SIDEBAR_MERGE})
PATCH_ASSERTIONS: dict[str, tuple[TreeAssertion, ...]] = {
    "fix(desktop): do not crash boot when rememberLog runs before hermesLog exists": (
        TreeAssertion("apps/desktop/electron/desktop-log-line.ts", contains=(
            "export const HERMES_LOG_CAP = 300", "export function appendCappedLogLines",
            "if (!Array.isArray(log) || lines.length === 0)",
        )),
        TreeAssertion("apps/desktop/electron/main.ts", contains=(
            "appendCappedLogLines, formatDesktopLogLine",
            "if (!appendCappedLogLines(hermesLog, lines))",
        ), ordered=(("const hermesLog = []", "let poolLimits = readPersistedPoolLimits()"),)),
    ),
    "[verified] fix(desktop): restore wheel scrolling in panes": (TreeAssertion(
        "apps/desktop/src/app/chat/sidebar/index.tsx", contains=("const SCROLL_GUTTER", "GROUP_BODY = 'max-h-none overflow-visible'")),),
    "docs(goals): document model goal control": (TreeAssertion(
        "toolsets.py", contains=('"goal": _ts(', '["goal_control"]')),),
    "feat(gateway): mark release investigators as cron sessions": (TreeAssertion(
        "tui_gateway/methods_session.py", contains=(
            'cron_session = _normalize_cron_session_marker(params.get("cron_session"))',
            '"cron_session": cron_session',
        )),),
    "[verified] feat: add model-callable goal control": (TreeAssertion(
        "hermes_cli/goals.py", contains=(
            "def load_goal_authoritative", "def _goal_generation",
        )),),
    "fix: preserve completed continuation publication ownership": (
        TreeAssertion("gateway/goal_continuation_claims.py", contains=(
            "def stage_completed_result", "completed continuation result still owns publication",
        )),
        TreeAssertion("gateway/run_busy.py", contains=(
            "async def _commit_goal_continuation_result",
            "def _reconcile_completed_goal_continuation_claims",
        )),
        TreeAssertion("gateway/platforms/base.py", contains=("class DeliveryOwnedReply",)),
        TreeAssertion("gateway/delivery_ledger.py", contains=(
            "def record_claimed_result", "def prepare_claimed_result_delivery",
        )),
    ),
    "fix(desktop): hydrate persisted Codex commentary": (TreeAssertion(
        "agent/codex_display_projection.py", contains=(
            "def project_codex_display_items", 'phase not in {"analysis", "commentary", "final", "final_answer"}',
        )),),
    "fix: make goal continuation recovery crash safe": (TreeAssertion(
        "gateway/goal_continuation_claims.py", contains=(
            "CLAIM_VERSION = 1", '"synthetic_head_pending": True',
        )),),
    "fix(desktop): preserve transcript continuity across reconnects": (TreeAssertion(
        "apps/desktop/src/app/contrib/hooks/use-background-sync.ts", contains=(
            "graftRefreshedTailOntoBackfill", "export async function reconcileActiveTranscript",
        )),),
}
MERGE_ASSERTIONS: dict[str, tuple[TreeAssertion, ...]] = {
    RUN_LEDGER_1_MERGE: (TreeAssertion(
        "docs/analysis/2026-09-04-windows-update-plugin-service-respawn-rca.md",
        contains=("| 1 | 18:16 |", "## Run ledger: 2026-09-06"),
    ),),
    RUN_LEDGER_2_MERGE: (TreeAssertion(
        "docs/analysis/2026-09-04-windows-update-plugin-service-respawn-rca.md",
        contains=("| 2 | 18:26 |", "## Run ledger: 2026-09-06"),
    ),),
    RUN_LEDGER_3_MERGE: (TreeAssertion(
        "docs/analysis/2026-09-04-windows-update-plugin-service-respawn-rca.md",
        contains=("| 3 | 18:29 |", "three consecutive clean runs"),
    ),),
    CANONICAL_UPDATER_MERGE: (
        TreeAssertion("apps/desktop/electron/main.ts", contains=(
            "const observed = await scanVenvBlockers(updateRoot)",
            "return getInstallMutationSet(updateRoot)",
            "return runWindowsUpdateForceRelease({",
        ), absent=("function forceKillAllHermesBackendTrees",)),
        TreeAssertion("apps/desktop/electron/install-mutation-set.ts", contains=(
            "export function enumerateInstallMutationSet", "export function getInstallMutationSet",
        )),
        TreeAssertion("hermes_mcp_update_gate.py", contains=(
            "def live_quiesce_lease", "def should_quiesce_mcp_bridge",
        )),
    ),
    UPDATER_CI_MERGE: (
        TreeAssertion("hermes_cli/update_cmd.py", contains=(
            "from gateway.status import looks_like_gateway_runtime_command_line",
            "command = subprocess.list2cmdline(cmdline_list or [])",
        )),
        TreeAssertion("hermes_cli/_scan_venv_blockers.py", contains=(
            'proc_iter = psutil.process_iter(["pid", "exe", "name"])',
            'raw_argv = info["cmdline"] if "cmdline" in info else proc.cmdline()',
        )),
        TreeAssertion("hermes_mcp_update_gate.py", contains=(
            "except (ProcessLookupError, OverflowError):",
        )),
    ),
    RESTORED_UPDATER_DRAIN_MERGE: (
        TreeAssertion("apps/desktop/electron/main.ts", contains=(
            "function killHermesOwnedVenvDaemons", "parseServePidFile",
            "operator-serve.pid", "argv-identity",
            "acquireMcpBridgeQuiesceLease", "handOffMcpBridgeLeaseToStagedUpdater",
        ), absent=("function forceKillAllHermesBackendTrees",)),
        TreeAssertion("apps/desktop/electron/venv-holder-select.ts", contains=(
            "export function isOperatorManagedServeCmdline",
            "export function isHermesOwnedUpdateHolder",
            "hermes_kernel_runner\.py", "hermes_tools_mcp_server",
        )),
        TreeAssertion("apps/desktop/electron/mcp-bridge-quiesce.ts", contains=(
            "export function acquireMcpBridgeQuiesceLease",
            "export async function handOffMcpBridgeLeaseToStagedUpdater",
            "export function revokeMcpBridgeQuiesceLease",
        )),
        TreeAssertion("apps/desktop/resources/update-scanner/scan-venv-blockers.py", contains=(
            "def terminate_venv_holder", '"--terminate-venv-holder"',
        )),
    ),
    UPDATER_GUI_SHIM_MERGE: (
        TreeAssertion("hermes_cli/_scan_venv_blockers.py", contains=(
            "_process_basename(argv[0]).casefold() not in _HERMES_SHIM_BASENAMES",
            "cwd_low.startswith(root_prefix) and is_exact_mcp_module_argv(argv)",
        )),
        TreeAssertion("apps/desktop/resources/update-scanner/scan-venv-blockers.py", contains=(
            "_process_basename(argv[0]).casefold() not in _HERMES_SHIM_BASENAMES",
            "cwd_low.startswith(root_prefix) and is_exact_mcp_module_argv(argv)",
        )),
        TreeAssertion("tui_gateway/session_lifecycle.py", contains=(
            'current.pop("_client_gone_interrupt_requested", None)',
            'current.pop("_client_gone_interrupt_polls", None)',
        )),
        TreeAssertion("agent/codex_display_projection.py", contains=(
            "def project_codex_reply_text", 'item.get("phase") not in ("final", "final_answer")',
        )),
    ),
    "Merge fix/windows-update-handoff-live-log: make the Windows update chain finish (#7)": (
        TreeAssertion("apps/desktop/electron/main.ts", contains=(
            "function releaseDrainUpdateMarker", "probeDesktopBuildNeeded",
        )),
        TreeAssertion("apps/desktop/electron/update-marker.ts", contains=(
            "export function releaseUpdateMarkerIfOwnedBy",
        )),
        TreeAssertion("hermes_cli/update_cmd.py", contains=(
            "def _update_log_append", "def _run_logged_subprocess",
        )),
        TreeAssertion("scripts/desktop-update/windows.ps1", contains=(
            "function Write-StepLines", "function Get-StepHeartbeatLine", "livelog arm",
        )),
    ),
    "Merge fix/handoff-log-sharing: append the hand-off log via a shared FileStream (#8)": (
        TreeAssertion("scripts/desktop-update/windows.ps1", contains=(
            "[System.IO.FileStream]::new",
            "[System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete",
            "if ($SelfTestLog)",
        ), absent=("Add-Content -LiteralPath $LogPath",)),
    ),
    "Merge pull request #9 from royalaid/fix/update-chain-receipt-lease-card": (
        TreeAssertion("apps/desktop/electron/main.ts", contains=(
            "const bundleOutOfSync = await detectRendererSkew()",
            "updateAvailable: behind === null || behind > 0 || bundleOutOfSync",
        )),
        TreeAssertion("hermes_cli/update_receipt.py", contains=(
            "def describe_last_receipt", "Update receipt NOT written",
        )),
        TreeAssertion("scripts/desktop-update/windows.ps1", contains=(
            "function Remove-BridgeLeaseIfOwned", "function Write-UpdateReceiptState",
        )),
    ),
    "Merge pull request #10 from royalaid/fix/handoff-marker-claim-time": (
        TreeAssertion("apps/desktop/electron/main.ts", contains=(
            "function describeHandoffAdoptionState", "getCachedWindowsProcessCreatedAt(pid)",
        )),
        TreeAssertion("scripts/desktop-update/windows.ps1", contains=(
            "Get-Process -Id $PID -ErrorAction Stop",
            "claimed update marker (pid $PID, created $startedAt",
        )),
    ),
    "Merge pull request #11 from royalaid/docs/handoff-handshake-addendum": (
        TreeAssertion("docs/analysis/2026-09-04-windows-update-plugin-service-respawn-rca.md", contains=(
            "## Addendum 2: the hand-off handshake itself",
        )),
        TreeAssertion("docs/plans/2026-09-06-001-refactor-monotonic-update-handoff-plan.md", contains=(
            "# Plan: take wall-clock comparison out of the Windows update hand-off",
            "Acknowledgement is a nonce plus a generation",
        )),
    ),
    PLUGIN_SERVICE_UNIT_MERGE: (
        TreeAssertion("apps/desktop/electron/main.ts", contains=(
            "function killHermesOwnedVenvDaemons",
            "desktopPluginServiceUnits(observed.result.desktopPluginServices)",
            "stopDesktopPluginServiceUnit(updateRoot, service)",
            "relaunchStoppedDesktopPluginHosts('refused preflight')",
        ), absent=("function forceKillAllHermesBackendTrees",)),
        TreeAssertion("apps/desktop/electron/desktop-plugin-host-restore.ts", contains=(
            "export function recordStoppedDesktopPluginHost",
            "export function restoreStoppedDesktopPluginHosts",
        )),
        TreeAssertion("apps/desktop/electron/venv-blocker-scan.ts", contains=(
            "export function desktopPluginServiceUnits",
            "export async function terminateDesktopPluginServiceDetailed",
        )),
        TreeAssertion("apps/desktop/electron/windows-update-force-release.ts", contains=(
            "terminateVia?: 'exact-process' | 'desktop-plugin-service'",
            "export async function runWindowsUpdateForceRelease",
        )),
    ),
    HOLDER_DETECTION_MERGE: (
        TreeAssertion("apps/desktop/electron/main.ts", contains=(
            "const observed = await scanVenvBlockers(updateRoot)",
            "forceReleaseInstallHolders: () => forceReleaseInstallHoldersForUpdate(updateRoot)",
        ), absent=("forceKillAllHermesBackendTrees",)),
        TreeAssertion("apps/desktop/electron/install-mutation-set.ts", contains=(
            "export function enumerateInstallMutationSet", "export function getInstallMutationSet",
        )),
        TreeAssertion("apps/desktop/electron/windows-restart-manager.ts", contains=(
            "export async function listRestartManagerHoldersForResources",
        )),
        TreeAssertion("apps/desktop/electron/windows-update-force-release.ts", contains=(
            "export async function runWindowsUpdateForceRelease", "terminateHolder",
        )),
        TreeAssertion("apps/desktop/resources/update-scanner/scan-venv-blockers.py", contains=(
            "terminate_venv_holder", "--terminate-venv-holder",
        )),
    ),
    OWNER_AWARE_SIDEBAR_MERGE: (
        TreeAssertion("apps/desktop/src/app/contrib/hooks/use-session-tile-delegate.ts", contains=(
            "sessionTileOwnerRoute(storedSessionId)",
            "requestForSessionProfile<T>(owner, requestGateway, method, params, timeoutMs)",
        )),
        TreeAssertion("apps/desktop/src/app/session/hooks/use-session-actions/index.ts", contains=(
            "openRouteTile(item.route, 'center')",
        )),
        TreeAssertion("apps/desktop/src/store/session-states.ts", contains=(
            "export function sessionTileOwnerRoute",
            "ownerRoute: workspaceScope.ownerRoute",
        )),
    ),
    DURABLE_TODO_MERGE: (
        TreeAssertion("agent/message_metadata.py", contains=("def stamp_persisted_todo_snapshot", "def has_persisted_todo_snapshot_provenance")),
        TreeAssertion("hermes_state.py", contains=("def get_todo_state_messages", "_TODO_STATE_LOOKUP_SQL")),
        TreeAssertion("apps/desktop/src/lib/todos.ts", contains=("export function latestSessionTodoState", "export function todosFromSnapshotMetadata")),
        TreeAssertion("tui_gateway/session_history.py", contains=("def _has_structured_todo_snapshot",)),
    ),
    KILL_ALL_MERGE: (
        TreeAssertion(
            "apps/desktop/electron/main.ts",
            contains=(
                "function killHermesOwnedVenvDaemons",
                "isHermesOwnedUpdateHolder",
                "buildVenvHolderListCommand",
            ),
            absent=("function forceKillAllHermesBackendTrees",),
        ),
        TreeAssertion(
            "apps/desktop/electron/venv-holder-select.ts",
            contains=("export function isHermesOwnedUpdateHolder", "isOperatorManagedServeCmdline"),
        ),
    ),
    "merge: integrate live Windows update transport": (
        TreeAssertion("apps/desktop/electron/updater-process.ts", contains=("export function resolveWindowsUpdateTransport",)),
        TreeAssertion("apps/desktop/electron/main.ts", contains=("resolveWindowsUpdateTransport,",
            "const windowsTransport = resolveWindowsUpdateTransport(updateRoot)",
            "windowsTransport.kind === 'manual'", "launchWindowsUpdateTransport("))),
    "merge: integrate Windows updater transport regression coverage": (TreeAssertion(
        "apps/desktop/electron/updater-process.test.ts", contains=(
            "test('resolveWindowsUpdateTransport selects the live checkout script'",
            "test('resolveWindowsUpdateTransport requires a manual update without a live script'")),),
}
PATCH_ASSERTIONS.update({
    "feat(desktop): add full-file tool actions": (TreeAssertion(
        "apps/desktop/src/components/assistant-ui/tool/fallback-model/index.ts",
        contains=("export function fileEditFilesystemPath", "resolved_path"),
    ),),
    "fix(installer): record the installed checkout commit": (TreeAssertion(
        "apps/bootstrap-installer/src-tauri/src/bootstrap.rs",
        contains=("let installed_commit = output", "installed_commit.or_else"),
    ),),
    "fix(compression): prune stale native replay safely": (TreeAssertion(
        "agent/context_compressor.py", contains=(
            "def _prune_stale_reasoning_replay", "compaction",
        ),
    ),),
    "fix(compression): reject custom relays for Codex compaction": (TreeAssertion(
        "agent/native_compaction.py", contains=(
            "def is_direct_openai_route",
            'hostname == "chatgpt.com" and path.startswith("/backend-api/codex")',
        ),
    ),),
    "fix(codex): preserve live reasoning source identity": (TreeAssertion(
        "agent/codex_runtime.py", contains=(
            'callback = getattr(agent, "reasoning_event_callback", None)',
            "on_reasoning_event=getattr(agent,",
        ),
    ),),
    "fix(desktop): hydrate native reasoning summaries": (TreeAssertion(
        "agent/chat_completion_helpers.py", contains=(
            "def _emit_native_codex_reasoning_summaries",
            "if not _emit_native_codex_reasoning_summaries",
        ),
    ),),
    "fix: bind sidebar sessions to exact owners": (TreeAssertion(
        "apps/desktop/src/store/session-states.ts", contains=(
            "export function sessionTileOwnerRoute",
            "export function acceptsSessionRuntimeSource",
        ),
    ),),
    "fix(desktop): coordinate Windows updates through one authenticated marker": (
        *MERGE_ASSERTIONS[RESTORED_UPDATER_DRAIN_MERGE],
    ),
    "fix(rebase): reconcile runtime contracts and CI after composition": (TreeAssertion(
        "hermes_cli/windows_host_path.py", contains=(
            "def read_windows_host_path", "def merge_windows_host_path",
        ),
    ),),
    "fix(ci): pin trigram fixtures to their historical schema": (TreeAssertion(
        "hermes_state_common.py", contains=(
            "FTS_TRIGRAM_SQL", "tokenize='trigram'",
        ),
    ),),
    "fix(desktop): guard stick-to-bottom and clamp measurement for hidden panes": (
        TreeAssertion("apps/desktop/src/components/assistant-ui/thread/pane-scroll-retention.ts", contains=(
            "export function usePaneScrollRetention",
            "parkedScrollTopRef.current = isAtBottom ? null : el.scrollTop",
            "stopScroll()",
        )),
        TreeAssertion("apps/desktop/src/components/assistant-ui/thread/user-message.tsx", contains=(
            "if (fullHeight <= 0)",
        )),
    ),
    "fix(goals): serialize persisted state across processes": (TreeAssertion(
        "hermes_cli/goals.py", contains=(
            "def goal_state_transaction", "def _cross_process_goal_lock",
            "receipt_token = uuid.uuid4().hex",
        ),
    ),),
    "feat(desktop): make sidebar session opens tab-first": (TreeAssertion(
        "apps/desktop/src/app/contrib/sidebar-session-open.ts", contains=(
            "export function openSidebarSession", "openSession(",
            "const placement = intent ?? ($sidebarSessionsOpenInNewTab.get() ? 'tab' : 'main')"
        ),
    ),),
    "fix(desktop): preserve exact sidebar session ownership": (
        *MERGE_ASSERTIONS[OWNER_AWARE_SIDEBAR_MERGE],
        TreeAssertion("apps/desktop/electron/session-windows.ts", contains=(
            "ownerRoute?.connectionId", "ownerTargetProfile",
        )),
    ),
    "fix: preserve goal continuation ordering and provenance": (
        TreeAssertion("gateway/platforms/event.py", contains=("goal_continuation: bool = False",)),
        TreeAssertion("gateway/run_turn.py", contains=(
            "durable_claimed_event: bool = False",
            "defer_result_publication=durable_claimed_event",
        )),
    ),
    "fix: resolve duplicate skills and record guardrail blocks": (
        TreeAssertion("tools/skills_tool.py", contains=("def _candidates_have_same_identity",)),
        TreeAssertion("agent/turn_tool_round.py", contains=(
            "record_kanban_guardrail_halt(decision, task_id=effective_task_id)",
        )),
    ),
    "fix(desktop): restore the fork's holder-drain updater lost in the canonical-PR fold": (
        *MERGE_ASSERTIONS[RESTORED_UPDATER_DRAIN_MERGE],
    ),
    "fix(rebase): restore gateway recovery and desktop contracts": (
        TreeAssertion("gateway/delivery_ledger.py", contains=(
            "class DeliveryObligationConflict", "def mark_claimed_result_delivered",
        )),
        TreeAssertion("gateway/run_notifications.py", contains=(
            "Queued-lane final reconciled by editing message",
            "mark_claimed_result_failed",
        )),
    ),
    "fix(skills): restore the books emoji in tool activity": (TreeAssertion(
        "tools/skills_tool.py", contains=('emoji="' + chr(92) + 'U0001F4DA"',),
    ),),
    "fix(rebase): repair splice damage from the upstream rebase": (
        TreeAssertion("apps/desktop/electron/main.ts", contains=(
            "const outcome = await waitForLocalBackendClearance",
            "clear update preflight did not mint a mutation permit",
            "resolveReadinessProbeAuth",
        )),
        TreeAssertion("apps/desktop/electron/session-windows.ts", contains=(
            "ownerRoute?.connectionId", "ownerTargetProfile",
        )),
    ),
    "fix: enforce authoritative goal persistence boundaries": (
        TreeAssertion("hermes_cli/goals.py", contains=(
            "class GoalMutationOutcomeUnknownError",
            "def load_authoritative(",
            "compare_and_set_meta_many",
        )),
    ),
    "fix(desktop): open plugin routes as closeable tabs": (
        TreeAssertion("apps/desktop/src/app/routes.ts", contains=(
            "export function isContributedRoute",
        )),
        TreeAssertion("apps/desktop/src/store/route-tiles.ts", contains=(
            "export function openRouteTile",
            "revealTreePane(`route-tile:${canonicalPath}`)",
        )),
        TreeAssertion("apps/desktop/src/app/session/hooks/use-session-actions/index.ts", contains=(
            "isContributedRoute(item.route)",
            "openRouteTile(item.route, 'center')",
        )),
    ),
})
MERGE_ASSERTIONS.update({
    "Merge commit 'a1605135e7170ff0958f2ee3edd1b3e7039914a0' into rebuild/fork-integration-final-20260916": PATCH_ASSERTIONS["fix(installer): record the installed checkout commit"],
    "Merge commit '9d9221855b0679dcebe62ad1579a9340066632b5' into rebuild/fork-integration-final-20260916": (TreeAssertion(
        "apps/desktop/electron/windows-user-env.ts", contains=("function readWindowsHostPath", "readWindowsRegistryEnvVar"),
    ),),
    "Merge commit '3b30a67583733895074a2fbfbb79fb8ce7d51bf3' into rebuild/fork-integration-final-20260916": PATCH_ASSERTIONS["fix(desktop): hydrate persisted Codex commentary"],
    "Merge commit '9f84bbcb154e9dec3cb8ca0132d165a89c0148b6' into rebuild/fork-integration-final-20260916": PATCH_ASSERTIONS["[verified] fix(desktop): restore wheel scrolling in panes"],
    "Merge commit '27c43f6698dc759dfc12dcd2581ed1eb1017e741' into rebuild/fork-integration-final-20260916": (TreeAssertion(
        "gateway/kanban_watchers.py", contains=("def _kanban_poll_signature", "boards unchanged; skipping database query"),
    ),),
    "Merge commit '56731d27419fc604389415f20f9d27c183f6ca49' into rebuild/fork-integration-final-20260916": (
        *PATCH_ASSERTIONS["fix: preserve completed continuation publication ownership"],
        TreeAssertion("gateway/run_turn.py", contains=("attachment_snapshot=getattr(", "turn_ctx.claimed_event")),
    ),
    "Merge commit 'f1c0a07ed7dc72f3d432d7f262989b14a259c38d' into rebuild/fork-integration-final-20260916": (TreeAssertion(
        "tools/goal_control_tool.py", contains=("def _validate_acceptance_evidence", "goal_readback"),
    ),),
    "Merge commit 'cd5c8a0033a7de78ef92d12316b4f4c88077f183' into rebuild/fork-integration-final-20260916": PATCH_ASSERTIONS["fix(desktop): open plugin routes as closeable tabs"],
    "Merge commit 'c8a4889708c1e4dbf5aede052a3b17014c2af1a2' into rebuild/fork-integration-final-20260916": (TreeAssertion(
        "apps/desktop/src/store/subagents.ts", contains=("export function promoteDelegateFallbackOwnership", "delegateRowIndex"),
    ),),
    "Merge commit '43aea8469658f1f87b09bc2314fe8aaf3c47299f' into rebuild/fork-integration-final-20260916": (TreeAssertion(
        "apps/desktop/src/app/session/hooks/use-session-actions/restore-pending-clarify.ts", contains=("pendingClarifyRequestFromSnapshot", "preserveCurrentClarifyRequest"),
    ),),
    "Merge commit '4a1777d63f65971253f4c875f0989ae961816d68' into rebuild/fork-integration-final-20260916": PATCH_ASSERTIONS["fix(desktop): preserve transcript continuity across reconnects"],
    "Merge commit 'b60d6c4bfde907f201958cfde585ac1509a22c27' into rebuild/fork-integration-final-20260916": (
        TreeAssertion("agent/message_metadata.py", contains=("def stamp_persisted_todo_snapshot",)),
        TreeAssertion("hermes_state_todo.py", contains=("class SessionTodoMixin", "def get_todo_state_messages")),
        TreeAssertion("apps/desktop/src/lib/todos.ts", contains=("latestSessionTodoState", "todosFromSnapshotMetadata")),
        TreeAssertion("tui_gateway/session_history.py", contains=("def _has_structured_todo_snapshot",)),
    ),
    "Merge commit '6792ddb5651815406b4507d6fe9a83bbeab5c93e' into rebuild/fork-integration-final-20260916": (TreeAssertion(
        "apps/desktop/electron/install-mutation-set.ts", contains=("enumerateInstallMutationSet", "getInstallMutationSet"),
    ),),
    "Merge commit '21569d237668df8c5ebd32aa14c4bb85b43a2ca5' into rebuild/fork-integration-final-20260916": (
        TreeAssertion("apps/desktop/electron/main.ts", contains=(
            "function killHermesOwnedVenvDaemons", "buildVenvHolderListCommand", "parseServePidFile",
        ), absent=("function forceKillAllHermesBackendTrees",)),
        TreeAssertion("apps/desktop/electron/venv-holder-select.ts", contains=(
            "isOperatorManagedServeCmdline", "hermes_kernel_runner\.py", "hermes_tools_mcp_server",
        )),
        TreeAssertion("apps/desktop/electron/windows-update-force-release.ts", contains=(
            "export async function runWindowsUpdateForceRelease", "terminateHolder",
        )),
        TreeAssertion("apps/desktop/resources/update-scanner/scan-venv-blockers.py", contains=(
            "def terminate_venv_holder", '"--terminate-venv-holder"',
        )),
    ),
    "Merge PR #104687 type-safety follow-up": (TreeAssertion(
        "apps/desktop/electron/windows-update-apply.ts", contains=("message: string", "waitPlan.message"),
    ),),
    "Merge PR #98673 E2E import follow-up": (TreeAssertion(
        "apps/desktop/e2e/delegate-card-identity.spec.ts", contains=("../../../tests-js/scripts/mock-server",),
    ),),
    "Merge PR #98451 E2E import follow-up": (TreeAssertion(
        "apps/desktop/e2e/clarify-warm-activation.spec.ts", contains=("../../../tests-js/scripts/mock-server",),
    ),),
    "Merge PR #98331 claimed-event follow-up": (TreeAssertion(
        "gateway/run_turn.py", contains=("claimed_event=claimed_event", "turn_ctx.claimed_event"),
    ),),
    "Merge PR #98456 route-test follow-up": (TreeAssertion(
        "apps/desktop/src/app/routes.workspace-reveal.test.ts", contains=("import { host } from '@/sdk'", "host.navigate(SKILLS_ROUTE)"),
    ),),
})
PATCH_ASSERTIONS["fix(desktop): coordinate Windows updates through one authenticated marker"] = (
    MERGE_ASSERTIONS["Merge commit '21569d237668df8c5ebd32aa14c4bb85b43a2ca5' into rebuild/fork-integration-final-20260916"]
)
def _git(repo: Path, *args: str, check: bool = True, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(GIT_ENV)
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, timeout=timeout,
        creationflags=CREATE_NO_WINDOW, env=env)
    if check and done.returncode: raise RefreshError(f"git {' '.join(args)} failed: {(done.stderr or done.stdout).strip()}")
    return done
def _fetch(repo: Path, remote: str, ref: str) -> str:
    for delay in (*FETCH_RATE_LIMIT_RETRY_DELAYS, None):
        try:
            _git(repo, "fetch", "--no-tags", "--quiet", remote, ref)
            return _git(repo, "rev-parse", "FETCH_HEAD").stdout.strip()
        except RefreshError as error:
            message = str(error).lower()
            if delay is None or ("429" not in message and "rate limit" not in message and "rate-limit" not in message):
                raise
            time.sleep(delay)
    raise AssertionError("fetch retry loop exhausted without returning or raising")
def _select_upstream(repo: Path, fetched: str, explicit: str | None, hour: int | None,
                     now: datetime | None) -> tuple[str, str | None]:
    if explicit:
        selected = _git(repo, "rev-parse", f"{explicit}^{{commit}}").stdout.strip()
        if _git(repo, "merge-base", "--is-ancestor", selected, fetched, check=False).returncode:
            raise RefreshError("explicit upstream SHA is not in fetched upstream history")
        return selected, None
    if hour is None: return fetched, None
    if not 0 <= hour <= 23: raise RefreshError("upstream cutoff hour must be between 0 and 23 UTC")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = current.replace(hour=hour, minute=0, second=0, microsecond=0)
    if current < cutoff: cutoff -= timedelta(days=1)
    selected = _git(repo, "rev-list", "--first-parent", "-1", f"--before={cutoff.isoformat()}", fetched).stdout.strip()
    if not selected: raise RefreshError(f"no upstream commit exists at or before {cutoff.isoformat()}")
    return selected, cutoff.isoformat()
def _remote_head(repo: Path, remote: str, ref: str) -> str | None:
    output = _git(repo, "ls-remote", "--heads", remote, ref).stdout.strip()
    return output.split()[0] if output else None
def _lines(repo: Path, *args: str) -> list[str]: return [x for x in _git(repo, *args).stdout.splitlines() if x]
def _cherry(repo: Path, upstream: str, published: str, base: str) -> dict[str, str]: return {x.split()[1]: x[0] for x in _lines(repo, "cherry", upstream, published, base)}
def _assert_tree(repo: Path, revision: str, assertions: Sequence[TreeAssertion]) -> None:
    for assertion in assertions:
        read = _git(repo, "show", f"{revision}:{assertion.path}", check=False)
        if read.returncode: raise RefreshError(f"merge assertion missing {assertion.path} at {revision}")
        text = read.stdout
        if any(x not in text for x in assertion.contains) or any(x in text for x in assertion.absent): raise RefreshError(f"merge assertion failed for {assertion.path} at {revision}")
        for before, after in assertion.ordered:
            if text.find(before) < 0 or text.find(after) <= text.find(before):
                raise RefreshError(f"merge ordering assertion failed for {assertion.path} at {revision}")
def _run_checks(scratch: Path, checks: Sequence[Sequence[str]]) -> None:
    for command in checks:
        done = subprocess.run(list(command), cwd=scratch, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=1800,
            creationflags=CREATE_NO_WINDOW)
        if done.returncode:
            raise RefreshError(f"focused check failed ({' '.join(command)}): {(done.stderr or done.stdout).strip()}")
def _rerere_autoupdated_paths(done: subprocess.CompletedProcess[str]) -> set[str]:
    prefix, suffix = "Staged '", "' using previous resolution."
    return {line[len(prefix):-len(suffix)] for line in f"{done.stdout}\n{done.stderr}".splitlines()
            if line.startswith(prefix) and line.endswith(suffix)}
def _rerere_paths_accounted_for(repo: Path, rerere_paths: set[str], staged_paths: set[str]) -> bool:
    if not rerere_paths:
        return False
    return all(path in staged_paths or not _git(repo, "diff", "HEAD", "--quiet", "--", path, check=False).returncode
               for path in rerere_paths)
def _scratch_rebase(repo: Path, published: str, upstream: str, base: str,
                    assertions: Sequence[TreeAssertion], checks: Sequence[Sequence[str]]) -> tuple[str, set[str], set[str]]:
    scratch = Path(tempfile.mkdtemp(prefix="hermes-fork-refresh-"))
    added, candidate, stopped, rerere_resolved = False, "", set(), set()
    try:
        _git(repo, "worktree", "add", "--quiet", "--detach", str(scratch), published)
        added = True
        done = _git(scratch, "rebase", "--rebase-merges", "--reapply-cherry-picks", "--empty=stop",
                    "--no-update-refs", "--no-autostash", "--onto", upstream, base, check=False, timeout=1800)
        while done.returncode:
            conflicts = _git(scratch, "diff", "--name-only", "--diff-filter=U").stdout.strip()
            head = _git(scratch, "rev-parse", "--verify", "REBASE_HEAD", check=False)
            explicit_resolution = False
            if (
                not head.returncode
                and head.stdout.strip() == "d34b405d2a0e543d45c40876a008b3c26bf4d9ee"
                and conflicts == "apps/desktop/scripts/dev-mock.mjs"
            ):
                # Upstream moved the shared mock to tests-js; this launcher-only edit has no target.
                _git(scratch, "rm", "--", conflicts)
                conflicts = ""
                explicit_resolution = True
            if conflicts or head.returncode:
                raise RefreshError(f"rebase conflict: {(done.stderr or done.stdout).strip()}")
            unstaged = _git(scratch, "diff", "--name-only").stdout.strip()
            untracked = _git(scratch, "ls-files", "--others", "--exclude-standard").stdout.strip()
            staged = _git(scratch, "diff", "--cached", "--name-only").stdout.strip()
            if unstaged or untracked:
                raise RefreshError(f"rebase conflict: {(done.stderr or done.stdout).strip()}")
            if staged:
                staged_paths = set(staged.splitlines())
                rerere_paths = _rerere_autoupdated_paths(done)
                if not explicit_resolution and not _rerere_paths_accounted_for(scratch, rerere_paths, staged_paths):
                    raise RefreshError(f"staged rebase state lacks rerere autoupdate provenance: staged={sorted(staged_paths)!r}, rerere={sorted(rerere_paths)!r}")
                resolved_head = head.stdout.strip()
                continued = _git(scratch, "rebase", "--continue", check=False, timeout=1800)
                if continued.returncode:
                    next_head = _git(scratch, "rev-parse", "--verify", "REBASE_HEAD", check=False)
                    if next_head.returncode or next_head.stdout.strip() == resolved_head:
                        raise RefreshError(
                            f"rebase continuation made no progress: {(continued.stderr or continued.stdout).strip()}"
                        )
                rerere_resolved.add(resolved_head)
                done = continued
                continue
            stopped.add(head.stdout.strip())
            done = _git(scratch, "rebase", "--skip", check=False, timeout=1800)
        candidate = _git(scratch, "rev-parse", "HEAD").stdout.strip()
        if _git(scratch, "merge-base", "--is-ancestor", upstream, candidate, check=False).returncode:
            raise RefreshError("candidate does not contain fetched upstream")
        _assert_tree(scratch, candidate, assertions)
        _run_checks(scratch, checks)
    finally:
        cleanup_error, primary_error = None, sys.exc_info()[1]
        if added:
            _git(scratch, "rebase", "--abort", check=False)
            removed = _git(repo, "worktree", "remove", "--force", str(scratch), check=False)
            cleanup_error = (removed.stderr or removed.stdout).strip() if removed.returncode else None
        if scratch.exists() and not cleanup_error:
            shutil.rmtree(scratch)
        if cleanup_error:
            raise RefreshError(f"{primary_error}; scratch cleanup failed: {cleanup_error}" if primary_error else f"scratch cleanup failed: {cleanup_error}") from primary_error
    return candidate, stopped, rerere_resolved
def _push_with_lease(repo: Path, remote: str, ref: str, captured: str, candidate: str) -> bool:
    current = _remote_head(repo, remote, ref)
    if current == candidate: return False
    if current != captured: raise RefreshError(f"lease rejected: remote moved to {current}")
    pushed = _git(repo, "push", "--porcelain", f"--force-with-lease={ref}:{captured}",
                  remote, f"{candidate}:{ref}", check=False)
    if pushed.returncode:
        if _remote_head(repo, remote, ref) == candidate:
            return False
        raise RefreshError(f"lease rejected: {(pushed.stderr or pushed.stdout).strip()}")
    if _remote_head(repo, remote, ref) != candidate: raise RefreshError("push reported success but remote does not equal candidate")
    return True
def compose(repo: str | Path, *, upstream_remote: str = "upstream", published_remote: str = "origin",
            upstream_ref: str = "refs/heads/main", published_ref: str = "refs/heads/fork-integration",
            dry_run: bool = True, checks: Sequence[Sequence[str]] = (), upstream_sha: str | None = None,
            upstream_cutoff_hour: int | None = None, now_utc: datetime | None = None,
            merge_assertions: dict[str, tuple[TreeAssertion, ...]] | None = None) -> dict:
    repo = Path(repo).resolve()
    fetched = _fetch(repo, upstream_remote, upstream_ref)
    upstream, cutoff = _select_upstream(repo, fetched, upstream_sha, upstream_cutoff_hour, now_utc)
    published = _fetch(repo, published_remote, published_ref)
    bases = _lines(repo, "merge-base", "--all", upstream, published)
    if len(bases) != 1: raise RefreshError(f"expected one merge base, found {len(bases)}")
    base = bases[0]
    result = {"fetched_upstream_head": fetched, "upstream_sha": upstream, "upstream_cutoff": cutoff,
              "captured_upstream": upstream, "captured_published": published, "merge_base": base,
              "candidate": published, "status": "already_current", "dispositions": [], "pushed": False}
    if not _git(repo, "merge-base", "--is-ancestor", upstream, published, check=False).returncode:
        return result
    rows = [line.split() for line in _lines(repo, "rev-list", "--reverse", "--topo-order", "--parents", published, f"^{base}")]
    commits, parents = [row[0] for row in rows], {row[0]: row[1:] for row in rows}
    merges = [sha for sha in commits if len(parents[sha]) > 1]
    merge_set = set(merges)
    registry = MERGE_ASSERTIONS if merge_assertions is None else merge_assertions
    subject_lines = _lines(repo, "log", "--no-walk=unsorted", "--format=%H%x00%s", *commits) if commits else []
    subjects = dict(line.split("\x00", 1) for line in subject_lines)
    resolved = {sha: registry.get(sha) or registry.get(subjects[sha]) for sha in merges}
    missing = [sha for sha in merges if resolved[sha] is None]
    if missing: raise RefreshError(f"uncharacterized merge(s): {', '.join(missing)}")
    non_merges = [sha for sha in commits if sha not in merge_set]
    resolved_patches = {sha: PATCH_ASSERTIONS.get(sha) or PATCH_ASSERTIONS.get(subjects[sha]) for sha in non_merges}
    assertions = tuple(item for sha in merges for item in resolved[sha]) + tuple(
        item for sha in non_merges if resolved_patches[sha] for item in resolved_patches[sha]
    )
    _assert_tree(repo, published, assertions)
    before = _cherry(repo, upstream, published, base)
    if set(before) != set(non_merges): raise RefreshError("captured non-merge range lacks stable patch dispositions")
    candidate, stopped, rerere_resolved = _scratch_rebase(repo, published, upstream, base, assertions, checks)
    after = _cherry(repo, candidate, published, base)
    failed, represented = [sha for sha in non_merges if after.get(sha) != "-"], {}
    patch_represented = {sha for sha in failed if resolved_patches[sha]}
    for merge_sha in merges:
        if subjects[merge_sha] not in CHARACTERIZED_MERGE_SIDE_SUBJECTS:
            continue
        first_parent, side_parent = parents[merge_sha][:2]
        for sha in failed:
            if (not _git(repo, "merge-base", "--is-ancestor", sha, side_parent, check=False).returncode
                    and _git(repo, "merge-base", "--is-ancestor", sha, first_parent, check=False).returncode):
                represented[sha] = merge_sha
    kill_merge = next((sha for sha in merges if subjects[sha] == KILL_ALL_MERGE), None)
    kill_side = parents[kill_merge][1] if kill_merge else None
    if kill_side in failed: represented[kill_side] = kill_merge
    failed = [sha for sha in failed if sha not in represented and sha not in patch_represented and sha not in rerere_resolved]
    if failed: raise RefreshError(f"fork changes missing from candidate: {', '.join(failed)}")
    result["candidate"] = candidate
    dispositions = {sha: {"commit": sha, "status": "represented_by_merge_assertion" if sha in represented
        else "represented_by_patch_assertion" if sha in patch_represented
        else "rerere_resolved" if sha in rerere_resolved else "empty" if sha in stopped else "replayed",
        "represented_by": represented.get(sha) or (sha if sha in patch_represented else None),
        "preexisting_patch": before[sha] == "-"} for sha in non_merges}
    result["dispositions"] = [{"commit": sha, "status": "characterized_merge_assertion",
        "assertion_count": len(resolved[sha])} if sha in merge_set else dispositions[sha] for sha in commits]
    if dry_run:
        result["status"] = "candidate_ready"
        return result
    result["pushed"] = _push_with_lease(repo, published_remote, published_ref, published, candidate)
    result["status"] = "published" if result["pushed"] else "already_published"
    return result
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="."); parser.add_argument("--upstream-remote", default="upstream")
    parser.add_argument("--published-remote", default="origin")
    parser.add_argument("--upstream-ref", default="refs/heads/main")
    parser.add_argument("--published-ref", default="refs/heads/fork-integration")
    parser.add_argument("--publish", action="store_true"); parser.add_argument("--check", action="append", default=[])
    parser.add_argument("--wake-agent-on-failure", action="store_true")
    parser.add_argument("--upstream-sha")
    parser.add_argument("--upstream-cutoff-hour", type=int)
    args = parser.parse_args(argv)
    try:
        decoded_checks = [json.loads(value) for value in args.check]
        if any(not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command) for command in decoded_checks): raise RefreshError("each --check must be a non-empty JSON array of strings")
        result = compose(args.repo, upstream_remote=args.upstream_remote, published_remote=args.published_remote,
            upstream_ref=args.upstream_ref, published_ref=args.published_ref, dry_run=not args.publish,
            upstream_sha=args.upstream_sha, upstream_cutoff_hour=args.upstream_cutoff_hour,
            checks=tuple(tuple(command) for command in decoded_checks))
        if args.wake_agent_on_failure:
            result["wakeAgent"] = False
        print(json.dumps(result, sort_keys=True))
        return 0
    except (RefreshError, subprocess.TimeoutExpired, OSError, TypeError, ValueError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, sort_keys=True))
        return 1
if __name__ == "__main__":
    raise SystemExit(main())
