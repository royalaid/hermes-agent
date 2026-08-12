//! Update orchestration.
//!
//! Driven when the installer is launched as `Hermes-Setup.exe --update` (see
//! `AppMode` in lib.rs). The desktop app hands off to us — it exits, then we:
//!
//!   1. wait for the old Hermes desktop process to fully exit (so both the
//!      venv shim and packaged app.asar are free; otherwise `hermes update`
//!      or repair bootstrap can race locked files),
//!   2. run `hermes update --yes --gateway` (Python/repo update; this does NOT
//!      rebuild apps/desktop by design — see cmd_update in hermes_cli/main.py),
//!   3. run `hermes desktop --build-only` (the rebuild step update skips),
//!   4. launch the freshly-built desktop (reuses bootstrap::launch logic).
//!
//! We reuse the `BootstrapEvent` channel + the existing progress UI by
//! emitting a synthetic multi-stage manifest (handoff → update → rebuild, plus
//! an install stage on macOS). To the frontend an update looks like a short
//! bootstrap, broken into the real operations run_update performs so the user
//! sees discrete steps (with the live log underneath) instead of one bar.
//!
//! Cross-platform note: `hermes update` already handles macOS/Linux (git/pip).
//! The only OS-specific bits here are the managed Python path and
//! the no-window creation flag — both already cfg-gated. Keep new logic
//! OS-agnostic so the mac/linux port stays "fill in the paths".

use std::env;
use std::ffi::OsString;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use anyhow::{anyhow, Result};
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter};
use tokio::io::BufReader;
use tokio::process::Command;

use crate::events::{BootstrapEvent, LogStream, StageInfo, StageState};
use crate::powershell::read_decoded_line;

/// `hermes update` exit code meaning "another hermes process is holding the
/// venv shim open / dirty precondition" — see _cmd_update_impl in
/// hermes_cli/main.py (sys.exit(2)). We surface a targeted message for this.
const UPDATE_EXIT_CONCURRENT: i32 = 2;

/// How long to wait for the old desktop process to release files under the
/// install tree before giving up and letting `hermes update`'s own guard decide.
const DESKTOP_EXIT_WAIT: Duration = Duration::from_secs(20);
const DESKTOP_EXIT_POLL: Duration = Duration::from_millis(500);

const BRIDGE_LEASE_FILENAME: &str = ".hermes-venv-quiesce";
const BRIDGE_LEASE_ID_ENV: &str = "HERMES_UPDATE_BRIDGE_LEASE_ID";
const BRIDGE_LEASE_MAX_SECONDS: u64 = 20 * 60;
const BRIDGE_LEASE_HANDOFF_GRACE_SECONDS: u64 = 90;
const BRIDGE_LEASE_REFRESH_SECONDS: u64 = 30;
const UPDATE_STAGE_TIMEOUT: Duration = Duration::from_secs(60 * 60);
const REBUILD_STAGE_TIMEOUT: Duration = Duration::from_secs(30 * 60);
const OTHER_STAGE_TIMEOUT: Duration = Duration::from_secs(5 * 60);
const CHILD_PIPE_DRAIN_TIMEOUT: Duration = Duration::from_secs(5);
const CHILD_JOB_DRAIN_TIMEOUT: Duration = Duration::from_secs(5);
const DESKTOP_RELAUNCH_ACK_TIMEOUT: Duration = Duration::from_secs(180);
const DEFERRED_GATEWAY_PLAN_MAX_SECONDS: u64 = 66 * 60;

/// Guards against concurrent update runs. The frontend kicks `startUpdate()`
/// from a mount effect, which can fire more than once (React strict-mode
/// double-invokes effects in dev; a window reload or stray re-init can do it
/// in prod). Two `run_update` tasks racing on `git stash` corrupt the working
/// tree — one stashes the changes the other then can't find. Exactly one task
/// may hold this flag at a time.
static UPDATE_RUNNING: AtomicBool = AtomicBool::new(false);

/// Frontend → Rust: kick off the update flow. Mirrors `start_bootstrap`'s
/// fire-and-forget shape; progress arrives on the `bootstrap` event channel.
#[tauri::command]
pub async fn start_update(app: AppHandle) -> Result<(), String> {
    // Re-entrancy guard (see UPDATE_RUNNING). compare_exchange lets exactly one
    // caller flip false→true; any concurrent caller no-ops instead of spawning
    // a second racing update.
    if UPDATE_RUNNING
        .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
        .is_err()
    {
        // Already running: re-emit the manifest so a duplicate startUpdate()
        // call (which resets the frontend store) can recover its stage list.
        let target_app = if cfg!(target_os = "macos") {
            target_app_from_args(std::env::args().skip(1))
        } else {
            None
        };
        emit(
            &app,
            BootstrapEvent::Manifest {
                stages: update_stages(target_app.is_some()),
                protocol_version: None,
            },
        );
        return Ok(());
    }
    tokio::spawn(async move {
        if let Err(err) = run_update(app.clone()).await {
            // run_update already emits a Failed event on the paths that matter;
            // this catches anything that escaped. Emit defensively.
            emit(
                &app,
                BootstrapEvent::Failed {
                    stage: None,
                    error: format!("{err:#}"),
                },
            );
        }
        UPDATE_RUNNING.store(false, Ordering::SeqCst);
    });
    Ok(())
}

/// RAII guard that owns the "update in progress" marker (see
/// `paths::update_in_progress_marker`). Created at the top of `run_update`;
/// its `Drop` removes the marker on EVERY exit path — success, early
/// `return Err`, or a panic that unwinds through `run_update` — so a crashed
/// or aborted updater can never permanently strand the marker and block
/// future desktop launches. The marker payload is `{pid}\n{started_at_unix}`
/// so the desktop's launch gate can reclaim a confirmed-dead owner. A live or
/// unreadable foreign PID remains authoritative regardless of marker age.
///
/// The marker is also the cross-process update lock: `hermes update` claims
/// the same file (see `hermes_cli/update_lock.py`) so a dashboard-spawned
/// update and this updater can't mutate one checkout at the same time.
/// `acquire` therefore REFUSES when a live foreign owner holds it rather than
/// overwriting — the pre-fix clobber is what let a dashboard `hermes update`
/// keep running while install-mode bootstrap rewrote the tree underneath it.
struct UpdateMarkerGuard {
    path: PathBuf,
    started_at: u64,
    /// False when a live foreign updater already owns the marker: we hold no
    /// claim, so `Drop` must not delete their marker.
    owned: bool,
}

/// Cross-runtime age ceiling used by regression fixtures. A syntactically
/// valid marker whose PID is still live remains authoritative after this
/// duration; age alone must never permit concurrent mutation.
#[cfg(test)]
const UPDATE_MARKER_MAX_AGE_SECS: u64 = 20 * 60;

/// The pid + age of a confirmed-live update holding the marker.
struct MarkerOwner {
    pid: u32,
    age_secs: u64,
}

enum UpdateMarkerAcquireError {
    ForeignOwner(MarkerOwner),
    Publish(std::io::Error),
}

/// Read the marker and report a live *foreign* owner, if any. `None` means the
/// bytes are malformed, the PID is confirmed dead, or the PID is **this**
/// process. An unreadable foreign PID blocks because inspection failure is not
/// proof of death. Never panics.
///
/// Self-PID is treated as non-ownership on purpose (#74761): since #50238 the
/// desktop pre-writes this marker with the spawned updater's pid before the
/// updater reaches `acquire`. Without the exclusion, `acquire` sees a live
/// owner that is itself and aborts ("Another Hermes update is already
/// running"), then the desktop relaunches and retries forever. A foreign live
/// pid (e.g. a dashboard-spawned `hermes update`) still blocks.
fn live_marker_owner_from_raw(raw: &[u8]) -> Option<MarkerOwner> {
    let (pid, started_at) = update_marker_identity_from_raw(raw)?;
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let age_secs = now.saturating_sub(started_at);
    if !pid_matches_claim(pid, started_at) {
        return None;
    }
    // Desktop `writeUpdateMarker(hermesHome, child.pid)` races ahead of us;
    // adopt that pre-claim rather than refusing our own marker.
    if pid == std::process::id() {
        return None;
    }
    Some(MarkerOwner { pid, age_secs })
}

fn update_marker_identity_from_raw(raw: &[u8]) -> Option<(u32, u64)> {
    let raw = std::str::from_utf8(raw).ok()?;
    let mut lines = raw.lines();
    let pid = lines.next()?.trim().parse().ok()?;
    let started_at = lines.next()?.trim().parse().ok()?;
    if pid == 0 || started_at == 0 || lines.next().is_some() {
        return None;
    }
    Some((pid, started_at))
}

#[cfg(windows)]
fn unreadable_process_owner_must_block(error_code: Option<i32>) -> bool {
    use windows_sys::Win32::Foundation::ERROR_INVALID_PARAMETER;

    // OpenProcess documents ERROR_INVALID_PARAMETER for a nonexistent PID.
    // Every other failure, including ERROR_ACCESS_DENIED, leaves ownership
    // unknown and therefore must preserve the lock.
    error_code != Some(ERROR_INVALID_PARAMETER as i32)
}

/// True only when `pid` is live or unreadable and its creation time does not
/// prove numeric-PID reuse after `claimed_at`. Access/probe failures remain
/// fail-closed: inability to inspect an owner is not proof that it died.
#[cfg(windows)]
fn pid_matches_claim(pid: u32, claimed_at: u64) -> bool {
    use windows_sys::Win32::Foundation::{CloseHandle, FILETIME, STILL_ACTIVE};
    use windows_sys::Win32::System::Threading::{
        GetExitCodeProcess, GetProcessTimes, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
    };

    unsafe {
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if handle.is_null() {
            // ERROR_INVALID_PARAMETER is Windows' dead/nonexistent PID result.
            // Access denied or any other unreadable-owner outcome must block:
            // inability to inspect a foreign owner is not proof that it died.
            return unreadable_process_owner_must_block(
                std::io::Error::last_os_error().raw_os_error(),
            );
        }
        let mut code: u32 = 0;
        let ok = GetExitCodeProcess(handle, &mut code);
        if ok != 0 && code != STILL_ACTIVE as u32 {
            CloseHandle(handle);
            return false;
        }
        let mut creation: FILETIME = std::mem::zeroed();
        let mut exit: FILETIME = std::mem::zeroed();
        let mut kernel: FILETIME = std::mem::zeroed();
        let mut user: FILETIME = std::mem::zeroed();
        let times_ok = GetProcessTimes(handle, &mut creation, &mut exit, &mut kernel, &mut user);
        CloseHandle(handle);
        if ok == 0 || times_ok == 0 {
            return true;
        }
        let windows_ticks =
            ((creation.dwHighDateTime as u64) << 32) | creation.dwLowDateTime as u64;
        const WINDOWS_TO_UNIX_EPOCH_TICKS: u64 = 116_444_736_000_000_000;
        let Some(unix_ticks) = windows_ticks.checked_sub(WINDOWS_TO_UNIX_EPOCH_TICKS) else {
            return true;
        };
        let created_at = unix_ticks / 10_000_000;
        // The marker is written only after its owner has started. Reaching the
        // next whole second therefore proves this numeric PID was reused.
        created_at < claimed_at.saturating_add(1)
    }
}

#[cfg(not(windows))]
fn pid_matches_claim(pid: u32, _claimed_at: u64) -> bool {
    // signal 0 delivers nothing; it only probes existence/permission.
    // ESRCH => dead. EPERM => alive but owned by another user.
    let rc = unsafe { libc::kill(pid as libc::pid_t, 0) };
    if rc == 0 {
        return true;
    }
    std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
}

impl UpdateMarkerGuard {
    /// Claim the marker, or report the live updater that already owns it.
    ///
    /// Publishing the claim is fail-closed. Electron uses this exact PID claim
    /// as its positive updater acknowledgement; continuing without it would
    /// let mutation begin while the Desktop still owns install-file handles.
    fn acquire(path: PathBuf) -> std::result::Result<Self, UpdateMarkerAcquireError> {
        if ensure_no_recovery_artifacts(&path).is_err() {
            return Err(UpdateMarkerAcquireError::Publish(std::io::Error::new(
                std::io::ErrorKind::Other,
                "an update-marker CAS recovery artifact is still present",
            )));
        }
        let prior = match std::fs::read(&path) {
            Ok(raw) => Some(raw),
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => None,
            Err(err) => return Err(UpdateMarkerAcquireError::Publish(err)),
        };
        if prior
            .as_deref()
            .is_some_and(|raw| update_marker_identity_from_raw(raw).is_none())
        {
            return Err(UpdateMarkerAcquireError::Publish(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "the existing update marker is malformed or incomplete",
            )));
        }
        if let Some(owner) = prior.as_deref().and_then(live_marker_owner_from_raw) {
            return Err(UpdateMarkerAcquireError::ForeignOwner(owner));
        }
        let pid = std::process::id();
        let started_at = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(UpdateMarkerAcquireError::Publish)?;
        }
        publish_update_marker_atomically(
            &path,
            prior.as_deref(),
            format!("{pid}\n{started_at}\n").as_bytes(),
        )
        .map_err(|err| {
            UpdateMarkerAcquireError::Publish(std::io::Error::new(
                std::io::ErrorKind::Other,
                err.to_string(),
            ))
        })?;
        Ok(Self {
            path,
            started_at,
            owned: true,
        })
    }

    /// Release the marker as soon as every mutating stage has completed.
    ///
    /// The updater still owns a Tauri/Cocoa event loop while it relaunches the
    /// desktop, and that loop can outlive `app.exit(0)`. Relying on `Drop`
    /// alone therefore leaves a *successful* update looking active — a live
    /// pid holding a fresh marker — which blocks desktop startup and every
    /// other updater while this PID remains live. Idempotent: `Drop` still runs
    /// and tolerates an already-removed marker.
    fn complete(&mut self) -> Result<()> {
        self.complete_with(|path| std::fs::remove_file(path))
    }

    fn complete_with<F>(&mut self, remove: F) -> Result<()>
    where
        F: FnOnce(&Path) -> std::io::Result<()>,
    {
        if !self.owned {
            return Ok(());
        }
        let tombstone = bridge_lease_sibling(
            &self.path,
            &format!(
                ".cas-release-{}-{}",
                std::process::id(),
                uuid::Uuid::new_v4()
            ),
        )?;
        std::fs::rename(&self.path, &tombstone)
            .map_err(|err| anyhow!("isolating completed update marker: {err}"))?;
        // From this point our claim is represented only by the recovery
        // artifact. Drop must not make an uncorrelated second cleanup attempt.
        self.owned = false;
        let ours = std::fs::read(&tombstone)
            .ok()
            .and_then(|raw| update_marker_identity_from_raw(&raw))
            == Some((std::process::id(), self.started_at));
        if ours {
            remove(&tombstone).map_err(|err| {
                anyhow!(
                    "removing completed update-marker recovery artifact {}: {err}",
                    tombstone.display()
                )
            })?;
            return Ok(());
        }

        // Restore foreign bytes without overwriting a still-newer claimant.
        // A hard link is an atomic create-if-absent operation on every target
        // filesystem supported by the Windows updater.
        if !self.path.exists() {
            match std::fs::hard_link(&tombstone, &self.path) {
                Ok(()) => {
                    if let Err(err) = std::fs::remove_file(&tombstone) {
                        return Err(anyhow!(
                            "restored foreign update marker but could not retire {}: {err}",
                            tombstone.display()
                        ));
                    }
                }
                Err(err) if err.kind() == std::io::ErrorKind::AlreadyExists => {}
                Err(err) => {
                    return Err(anyhow!(
                        "restoring foreign update-marker recovery artifact {}: {err}",
                        tombstone.display()
                    ));
                }
            }
        }
        Err(anyhow!(
            "update-marker ownership changed during terminal cleanup; preserved {}",
            tombstone.display()
        ))
    }
}

impl Drop for UpdateMarkerGuard {
    fn drop(&mut self) {
        if let Err(err) = self.complete() {
            tracing::warn!(path = ?self.path, %err, "update-marker cleanup was not proven");
        }
    }
}

/// Capability-bound lease that keeps the managed venv quiesced while the
/// direct Tauri fallback mutates it. The repo-owned PowerShell handoff adopts
/// the same schema itself; this guard covers the older/no-script fallback.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct BridgeQuiesceLease {
    schema_version: u32,
    lease_id: String,
    owner_pid: u32,
    created_at: u64,
    expires_at: u64,
    handoff_grace_until: u64,
    install_root: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct UpdateReceiptHealth {
    critical_syntax: bool,
    critical_imports: bool,
    dependencies: bool,
    node_dependencies: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct UpdateReceipt {
    schema_version: u32,
    invocation_id: String,
    lease_id: String,
    mode: String,
    root: String,
    remote: Option<String>,
    branch: String,
    target_ref: Option<String>,
    target_sha: Option<String>,
    resulting_head: Option<String>,
    archive_sha: Option<String>,
    timestamp: u64,
    success: bool,
    gateway_resume_deferred: bool,
    health: UpdateReceiptHealth,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct DesktopHandoffAck {
    schema_version: u32,
    attempt_id: String,
    invocation_id: String,
    lease_id: String,
    pid: u32,
    process_started_at: u64,
    root: String,
    executable: String,
    build_id: String,
    build_source: String,
    backend_ready: bool,
    backend_mode: String,
    acknowledged_at: u64,
    error: serde_json::Value,
}

struct WindowsDesktopRelaunchIdentity {
    pid: u32,
    process_started_at: u64,
    executable: PathBuf,
    requested_at: u64,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct DeferredGatewayPlanProfile {
    name: String,
    old_pid: u32,
    created_at: f64,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct DeferredGatewayPlan {
    schema_version: u32,
    invocation_id: String,
    lease_fingerprint: String,
    install_root: String,
    created_at: u64,
    expires_at: u64,
    profiles: Vec<DeferredGatewayPlanProfile>,
    cold_start_if_installed: bool,
    auth: String,
}

struct DeferredGatewayPlanProof {
    pending_path: PathBuf,
    completed_path: PathBuf,
    raw: Vec<u8>,
    started_completed: bool,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct DeferredGatewayAdoptionFrame {
    schema_version: u32,
    event: String,
    invocation_id: String,
    owner_pid: u32,
}

fn is_hex_sha(value: &str, minimum: usize, maximum: usize) -> bool {
    (minimum..=maximum).contains(&value.len()) && value.bytes().all(|byte| byte.is_ascii_hexdigit())
}

fn validate_update_receipt(
    path: &Path,
    expected_invocation_id: &str,
    expected_lease_id: &str,
    install_root: &Path,
    expected_branch: &str,
    not_before: u64,
    now: u64,
) -> Result<UpdateReceipt> {
    let raw = std::fs::read(path)
        .map_err(|err| anyhow!("reading update success receipt {}: {err}", path.display()))?;
    if raw.is_empty() || raw.len() > 64 * 1024 {
        return Err(anyhow!("the update success receipt has an invalid size"));
    }
    let receipt: UpdateReceipt = serde_json::from_slice(&raw).map_err(|_| {
        anyhow!("the update success receipt is invalid JSON or has an unknown field")
    })?;
    if receipt.schema_version != 1
        || !valid_bridge_lease_id(&receipt.invocation_id)
        || receipt.invocation_id != expected_invocation_id
        || !valid_bridge_lease_id(&receipt.lease_id)
        || receipt.lease_id != expected_lease_id
        || !canonical_roots_match(Path::new(&receipt.root), install_root)
        || receipt.branch != expected_branch
        || receipt.timestamp < not_before
        || receipt.timestamp > now.saturating_add(30)
        || !receipt.success
        || !receipt.gateway_resume_deferred
        || !receipt.health.critical_syntax
        || !receipt.health.critical_imports
        || !receipt.health.dependencies
        || !receipt.health.node_dependencies
    {
        return Err(anyhow!(
            "the update child did not produce a fresh, correlated, healthy success receipt"
        ));
    }
    match receipt.mode.as_str() {
        "git" => {
            let remote = receipt
                .remote
                .as_deref()
                .filter(|value| !value.trim().is_empty())
                .ok_or_else(|| anyhow!("the git update receipt is missing its remote"))?;
            let target_ref = receipt
                .target_ref
                .as_deref()
                .ok_or_else(|| anyhow!("the git update receipt is missing its target ref"))?;
            let target_sha = receipt
                .target_sha
                .as_deref()
                .filter(|value| is_hex_sha(value, 40, 40))
                .ok_or_else(|| anyhow!("the git update receipt has an invalid target SHA"))?;
            let resulting_head = receipt
                .resulting_head
                .as_deref()
                .filter(|value| is_hex_sha(value, 40, 40))
                .ok_or_else(|| anyhow!("the git update receipt has an invalid resulting HEAD"))?;
            if target_ref != format!("refs/remotes/{remote}/{expected_branch}")
                || !target_sha.eq_ignore_ascii_case(resulting_head)
                || receipt.archive_sha.is_some()
            {
                return Err(anyhow!("the git update receipt is internally inconsistent"));
            }
        }
        "archive" => {
            if receipt.remote.is_some()
                || receipt.target_ref.is_some()
                || receipt.target_sha.is_some()
                || receipt.resulting_head.is_some()
                || !receipt
                    .archive_sha
                    .as_deref()
                    .is_some_and(|value| is_hex_sha(value, 64, 64))
            {
                return Err(anyhow!(
                    "the archive update receipt is internally inconsistent"
                ));
            }
        }
        _ => return Err(anyhow!("the update receipt has an unsupported mode")),
    }
    Ok(receipt)
}

fn deferred_gateway_plan_proof(
    hermes_home: &Path,
    invocation_id: &str,
    install_root: &Path,
) -> Result<DeferredGatewayPlanProof> {
    let pending_path = hermes_home.join(format!(".hermes-gateway-resume-{invocation_id}.json"));
    let completed_path =
        hermes_home.join(format!(".hermes-gateway-resume-{invocation_id}.completed"));
    let pending_exists = pending_path.is_file();
    let completed_exists = completed_path.is_file();
    if pending_exists == completed_exists {
        return Err(anyhow!(
            "the deferred gateway plan must have exactly one pending or completed record"
        ));
    }
    let pending_name = pending_path
        .file_name()
        .ok_or_else(|| anyhow!("the deferred gateway plan path has no file name"))?
        .to_string_lossy();
    let consume_prefix = format!("{pending_name}.consume-");
    for entry in std::fs::read_dir(hermes_home)
        .map_err(|err| anyhow!("scanning deferred gateway plan artifacts: {err}"))?
    {
        let entry =
            entry.map_err(|err| anyhow!("reading deferred gateway plan artifact: {err}"))?;
        if entry
            .file_name()
            .to_string_lossy()
            .starts_with(&consume_prefix)
        {
            return Err(anyhow!(
                "a deferred gateway plan consume artifact is still present"
            ));
        }
    }
    let selected = if pending_exists {
        &pending_path
    } else {
        &completed_path
    };
    let raw = std::fs::read(selected).map_err(|err| {
        anyhow!(
            "reading deferred gateway plan {}: {err}",
            selected.display()
        )
    })?;
    if raw.is_empty() || raw.len() > 256 * 1024 {
        return Err(anyhow!("the deferred gateway plan has an invalid size"));
    }
    let plan: DeferredGatewayPlan = serde_json::from_slice(&raw).map_err(|_| {
        anyhow!("the deferred gateway plan is invalid JSON or has an unknown field")
    })?;
    let now = unix_time_seconds();
    if plan.schema_version != 1
        || plan.invocation_id != invocation_id
        || !valid_bridge_lease_id(&plan.invocation_id)
        || !canonical_roots_match(Path::new(&plan.install_root), install_root)
        || plan.created_at == 0
        || plan.created_at > now.saturating_add(5)
        || plan.expires_at < now
        || plan.expires_at < plan.created_at
        || plan.expires_at.saturating_sub(plan.created_at) > DEFERRED_GATEWAY_PLAN_MAX_SECONDS
        || !is_hex_sha(&plan.lease_fingerprint, 64, 64)
        || !is_hex_sha(&plan.auth, 64, 64)
    {
        return Err(anyhow!(
            "the deferred gateway plan is expired, malformed, or uncorrelated"
        ));
    }
    let mut profile_names = std::collections::HashSet::new();
    for profile in &plan.profiles {
        if profile.name.is_empty()
            || profile.name.len() > 128
            || !profile
                .name
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte))
            || !profile_names.insert(profile.name.as_str())
            || profile.old_pid == 0
            || !profile.created_at.is_finite()
            || profile.created_at <= 0.0
        {
            return Err(anyhow!(
                "the deferred gateway plan has an invalid profile identity"
            ));
        }
    }
    let _ = plan.cold_start_if_installed;
    Ok(DeferredGatewayPlanProof {
        pending_path,
        completed_path,
        raw,
        started_completed: completed_exists,
    })
}

fn deferred_gateway_plan_consumed(
    hermes_home: &Path,
    invocation_id: &str,
    install_root: &Path,
    initial: &DeferredGatewayPlanProof,
) -> Result<()> {
    let terminal = deferred_gateway_plan_proof(hermes_home, invocation_id, install_root)?;
    if !terminal.started_completed
        || terminal.raw != initial.raw
        || initial.pending_path.exists()
        || !initial.completed_path.is_file()
    {
        return Err(anyhow!(
            "the exact deferred gateway plan was not consumed into its completed record"
        ));
    }
    Ok(())
}

fn validate_deferred_gateway_adoption_frame(
    line: &str,
    expected_invocation_id: &str,
    expected_owner_pid: u32,
) -> Result<()> {
    let frame: DeferredGatewayAdoptionFrame = serde_json::from_str(line).map_err(|_| {
        anyhow!("the gateway-resume child did not emit its exact adoption frame first")
    })?;
    if frame.schema_version != 1
        || frame.event != "deferred-gateway-lease-adopted"
        || frame.invocation_id != expected_invocation_id
        || frame.owner_pid != expected_owner_pid
    {
        return Err(anyhow!(
            "the gateway-resume adoption frame was malformed or did not identify the exact child"
        ));
    }
    Ok(())
}

#[cfg(windows)]
fn child_process_creation_ticks(child: &tokio::process::Child) -> Result<u64> {
    use windows_sys::Win32::Foundation::FILETIME;
    use windows_sys::Win32::System::Threading::GetProcessTimes;

    let handle = child
        .raw_handle()
        .ok_or_else(|| anyhow!("the child process handle is unavailable"))?;
    let mut creation: FILETIME = unsafe { std::mem::zeroed() };
    let mut exit: FILETIME = unsafe { std::mem::zeroed() };
    let mut kernel: FILETIME = unsafe { std::mem::zeroed() };
    let mut user: FILETIME = unsafe { std::mem::zeroed() };
    let ok = unsafe {
        GetProcessTimes(
            handle.cast(),
            &mut creation,
            &mut exit,
            &mut kernel,
            &mut user,
        )
    };
    if ok == 0 {
        return Err(anyhow!(
            "reading the exact child process creation identity: {}",
            std::io::Error::last_os_error()
        ));
    }
    Ok(((creation.dwHighDateTime as u64) << 32) | creation.dwLowDateTime as u64)
}

struct BridgeQuiesceLeaseGuard {
    path: PathBuf,
    lease_id: String,
    install_root: PathBuf,
    owned: bool,
    transferred_to: Option<u32>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ChildLeaseObservation {
    ParentOwned,
    ChildOwned,
    ParentReturned,
}

impl BridgeQuiesceLeaseGuard {
    fn adopt(path: PathBuf, expected_lease_id: Option<&str>, install_root: &Path) -> Result<Self> {
        let expected_lease_id = expected_lease_id.ok_or_else(|| {
            anyhow!("this Desktop update did not receive a bridge-quiesce handoff capability")
        })?;
        if !valid_bridge_lease_id(expected_lease_id) {
            return Err(anyhow!(
                "the bridge-quiesce handoff capability is malformed"
            ));
        }
        ensure_no_recovery_artifacts(&path)?;
        if !path.exists() {
            return Err(anyhow!("the expected bridge-quiesce lease is missing"));
        }

        let mut file = open_bridge_lease_snapshot(&path)?;
        let (mut lease, expected_raw) = read_bridge_lease_snapshot(&mut file)?;
        let now = unix_time_seconds();
        validate_bridge_lease(&lease, expected_lease_id, install_root, now, true)?;
        drop(file);
        renew_bridge_lease(&mut lease, now);
        replace_bridge_lease_atomically(&path, &expected_raw, &lease)?;

        Ok(Self {
            path,
            lease_id: expected_lease_id.to_string(),
            install_root: install_root.to_path_buf(),
            owned: true,
            transferred_to: None,
        })
    }

    fn refresh(&mut self) -> Result<()> {
        if !self.owned {
            return Err(anyhow!("bridge-quiesce lease ownership was lost"));
        }
        ensure_no_recovery_artifacts(&self.path)?;
        let mut file = open_bridge_lease_snapshot(&self.path)?;
        let (mut lease, expected_raw) = read_bridge_lease_snapshot(&mut file)?;
        let now = unix_time_seconds();
        validate_bridge_lease(&lease, &self.lease_id, &self.install_root, now, false)?;
        drop(file);
        renew_bridge_lease(&mut lease, now);
        replace_bridge_lease_atomically(&self.path, &expected_raw, &lease)
    }

    fn observe_child_transfer<F>(
        &mut self,
        child_pid: u32,
        mut allowed_descendant: F,
    ) -> Result<ChildLeaseObservation>
    where
        F: FnMut(u32) -> Result<bool>,
    {
        if child_pid == 0 {
            return Err(anyhow!("the updater child has no verifiable process id"));
        }
        ensure_no_recovery_artifacts(&self.path)?;
        let mut file = open_bridge_lease_snapshot(&self.path)?;
        let (lease, _) = read_bridge_lease_snapshot(&mut file)?;
        validate_bridge_lease_document(
            &lease,
            &self.lease_id,
            &self.install_root,
            unix_time_seconds(),
        )?;
        if lease.owner_pid == std::process::id() {
            if self.owned {
                return Ok(ChildLeaseObservation::ParentOwned);
            }
            if self.transferred_to.is_some() {
                self.owned = true;
                return Ok(ChildLeaseObservation::ParentReturned);
            }
        }
        if lease.owner_pid == child_pid || allowed_descendant(lease.owner_pid)? {
            self.owned = false;
            self.transferred_to = Some(lease.owner_pid);
            return Ok(ChildLeaseObservation::ChildOwned);
        }
        Err(anyhow!(
            "bridge-quiesce lease ownership changed to an unexpected process"
        ))
    }

    fn require_parent_return(&mut self) -> Result<()> {
        if self.transferred_to.is_none() {
            return Err(anyhow!(
                "the exact updater child never acknowledged the bridge-quiesce lease"
            ));
        }
        if !self.path.exists() || has_recovery_artifacts(&self.path)? {
            return Err(anyhow!(
                "the updater child exited without returning the bridge-quiesce lease to its exact parent"
            ));
        }
        let mut file = open_bridge_lease_snapshot(&self.path)?;
        let (lease, _) = read_bridge_lease_snapshot(&mut file)?;
        validate_bridge_lease(
            &lease,
            &self.lease_id,
            &self.install_root,
            unix_time_seconds(),
            false,
        )?;
        if lease.owner_pid != std::process::id() {
            return Err(anyhow!(
                "the updater child exited without returning the bridge-quiesce lease to its exact parent"
            ));
        }
        self.owned = true;
        Ok(())
    }

    fn require_child_cleanup(&mut self) -> Result<()> {
        if self.transferred_to.is_none() {
            return Err(anyhow!(
                "the exact gateway-resume child never acknowledged the bridge-quiesce lease"
            ));
        }
        if self.path.exists() || has_recovery_artifacts(&self.path)? {
            return Err(anyhow!(
                "the gateway-resume child exited without clearing its exact bridge-quiesce lease"
            ));
        }
        self.owned = false;
        Ok(())
    }

    fn recover_after_terminated_child(&mut self) -> Result<()> {
        let expected_owner = self.transferred_to.ok_or_else(|| {
            anyhow!("no exact updater child ever adopted the bridge-quiesce lease")
        })?;
        ensure_no_recovery_artifacts(&self.path)?;
        let mut file = open_bridge_lease_snapshot(&self.path)?;
        let (mut lease, expected_raw) = read_bridge_lease_snapshot(&mut file)?;
        validate_bridge_lease_document(
            &lease,
            &self.lease_id,
            &self.install_root,
            unix_time_seconds(),
        )?;
        if lease.owner_pid != expected_owner {
            return Err(anyhow!(
                "the bridge-quiesce lease is no longer owned by the exact terminated child"
            ));
        }
        if pid_matches_claim(lease.owner_pid, lease.created_at) {
            return Err(anyhow!(
                "the terminated updater lease owner is still live or unreadable"
            ));
        }
        drop(file);
        renew_bridge_lease(&mut lease, unix_time_seconds());
        replace_bridge_lease_atomically(&self.path, &expected_raw, &lease)?;
        self.owned = true;
        Ok(())
    }

    fn complete(&mut self) -> Result<()> {
        if !self.owned {
            if self.path.exists() || has_recovery_artifacts(&self.path)? {
                return Err(anyhow!(
                    "bridge-quiesce state reappeared after the trusted gateway-resume cleanup proof"
                ));
            }
            return Ok(());
        }
        let tombstone = bridge_lease_sibling(
            &self.path,
            &format!(
                ".cas-release-{}-{}",
                std::process::id(),
                uuid::Uuid::new_v4()
            ),
        )?;
        std::fs::rename(&self.path, &tombstone)
            .map_err(|err| anyhow!("isolating bridge-quiesce lease for cleanup: {err}"))?;
        self.owned = false;

        let ours = std::fs::read(&tombstone)
            .ok()
            .and_then(|raw| serde_json::from_slice::<BridgeQuiesceLease>(&raw).ok())
            .map(|lease| {
                lease.schema_version == 1
                    && lease.lease_id == self.lease_id
                    && lease.owner_pid == std::process::id()
                    && canonical_roots_match(Path::new(&lease.install_root), &self.install_root)
            })
            .unwrap_or(false);
        if ours {
            std::fs::remove_file(&tombstone).map_err(|err| {
                anyhow!(
                    "removing completed bridge-quiesce recovery artifact {}: {err}",
                    tombstone.display()
                )
            })?;
        } else if !self.path.exists() {
            std::fs::hard_link(&tombstone, &self.path).map_err(|err| {
                anyhow!(
                    "restoring foreign bridge-quiesce recovery artifact {}: {err}",
                    tombstone.display()
                )
            })?;
            std::fs::remove_file(&tombstone)?;
            return Err(anyhow!(
                "bridge-quiesce lease ownership changed during terminal cleanup"
            ));
        } else {
            return Err(anyhow!(
                "foreign bridge-quiesce lease changed during cleanup; preserved {}",
                tombstone.display()
            ));
        }
        Ok(())
    }
}

impl Drop for BridgeQuiesceLeaseGuard {
    fn drop(&mut self) {
        if let Err(err) = self.complete() {
            tracing::warn!(path = ?self.path, %err, "bridge-quiesce cleanup was not proven");
        }
    }
}

fn unix_time_seconds() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(0)
}

fn valid_bridge_lease_id(value: &str) -> bool {
    (16..=128).contains(&value.len())
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

fn canonical_roots_match(left: &Path, right: &Path) -> bool {
    let Ok(left) = left.canonicalize() else {
        return false;
    };
    let Ok(right) = right.canonicalize() else {
        return false;
    };
    if cfg!(target_os = "windows") {
        left.to_string_lossy()
            .eq_ignore_ascii_case(&right.to_string_lossy())
    } else {
        left == right
    }
}

fn validate_bridge_lease(
    lease: &BridgeQuiesceLease,
    expected_lease_id: &str,
    install_root: &Path,
    now: u64,
    allow_handoff: bool,
) -> Result<()> {
    validate_bridge_lease_document(lease, expected_lease_id, install_root, now)?;
    let owner_matches_claim = pid_matches_claim(lease.owner_pid, lease.created_at);
    let self_owned = lease.owner_pid == std::process::id() && owner_matches_claim;
    let valid_handoff = allow_handoff && (owner_matches_claim || now <= lease.handoff_grace_until);
    if !self_owned && !valid_handoff {
        return Err(anyhow!("the bridge-quiesce lease owner is no longer valid"));
    }
    Ok(())
}

fn validate_bridge_lease_document(
    lease: &BridgeQuiesceLease,
    expected_lease_id: &str,
    install_root: &Path,
    now: u64,
) -> Result<()> {
    if lease.schema_version != 1
        || !valid_bridge_lease_id(&lease.lease_id)
        || lease.lease_id != expected_lease_id
        || lease.owner_pid == 0
    {
        return Err(anyhow!(
            "the bridge-quiesce lease capability does not match"
        ));
    }
    if !canonical_roots_match(Path::new(&lease.install_root), install_root) {
        return Err(anyhow!(
            "the bridge-quiesce lease targets another installation"
        ));
    }
    if lease.created_at == 0
        || lease.created_at > now.saturating_add(30)
        || lease.expires_at <= now
        || lease.expires_at < lease.created_at
        || lease.expires_at.saturating_sub(lease.created_at) > BRIDGE_LEASE_MAX_SECONDS
        || lease.handoff_grace_until < lease.created_at
        || lease.handoff_grace_until > lease.expires_at
        || lease.handoff_grace_until.saturating_sub(lease.created_at)
            > BRIDGE_LEASE_HANDOFF_GRACE_SECONDS
    {
        return Err(anyhow!(
            "the bridge-quiesce lease is expired or outside its safety bounds"
        ));
    }
    Ok(())
}

fn renew_bridge_lease(lease: &mut BridgeQuiesceLease, now: u64) {
    lease.owner_pid = std::process::id();
    lease.created_at = now;
    lease.expires_at = now.saturating_add(BRIDGE_LEASE_MAX_SECONDS);
    lease.handoff_grace_until = now.saturating_add(BRIDGE_LEASE_HANDOFF_GRACE_SECONDS);
}

fn open_bridge_lease_snapshot(path: &Path) -> Result<std::fs::File> {
    let mut options = std::fs::OpenOptions::new();
    options.read(true);
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::fs::OpenOptionsExt;
        use windows_sys::Win32::Storage::FileSystem::{FILE_SHARE_DELETE, FILE_SHARE_READ};
        // Readers remain able to observe the prior complete JSON document,
        // and atomic replacement remains possible while this snapshot is open.
        // Write sharing stays disabled so in-place writers cannot mutate it.
        options.share_mode(FILE_SHARE_READ | FILE_SHARE_DELETE);
    }
    options
        .open(path)
        .map_err(|err| anyhow!("opening bridge-quiesce lease snapshot: {err}"))
}

fn read_bridge_lease_snapshot(file: &mut std::fs::File) -> Result<(BridgeQuiesceLease, Vec<u8>)> {
    if file.metadata()?.len() == 0 || file.metadata()?.len() > 64 * 1024 {
        return Err(anyhow!("the bridge-quiesce lease has an invalid size"));
    }
    let mut raw = Vec::new();
    file.read_to_end(&mut raw)?;
    let lease = serde_json::from_slice(&raw)
        .map_err(|_| anyhow!("the bridge-quiesce lease is invalid JSON"))?;
    Ok((lease, raw))
}

fn bridge_lease_sibling(path: &Path, suffix: &str) -> Result<PathBuf> {
    let name = path
        .file_name()
        .ok_or_else(|| anyhow!("the bridge-quiesce lease path has no file name"))?;
    let mut sibling = name.to_os_string();
    sibling.push(suffix);
    Ok(path.with_file_name(sibling))
}

fn recovery_artifacts(path: &Path) -> Result<Vec<PathBuf>> {
    let parent = path
        .parent()
        .ok_or_else(|| anyhow!("the handoff marker path has no parent"))?;
    let base = path
        .file_name()
        .ok_or_else(|| anyhow!("the handoff marker path has no file name"))?
        .to_string_lossy();
    let prefix = format!("{base}.cas-");
    let mut artifacts = Vec::new();
    let entries = match std::fs::read_dir(parent) {
        Ok(entries) => entries,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Ok(artifacts),
        Err(err) => return Err(anyhow!("scanning handoff recovery artifacts: {err}")),
    };
    for entry in entries {
        let entry = entry.map_err(|err| anyhow!("reading handoff recovery artifact: {err}"))?;
        if entry.file_name().to_string_lossy().starts_with(&prefix) {
            artifacts.push(entry.path());
        }
    }
    Ok(artifacts)
}

fn has_recovery_artifacts(path: &Path) -> Result<bool> {
    Ok(!recovery_artifacts(path)?.is_empty())
}

fn ensure_no_recovery_artifacts(path: &Path) -> Result<()> {
    ensure_no_recovery_artifacts_at(path, unix_time_seconds(), pid_matches_claim)
}

fn ensure_no_recovery_artifacts_at(
    path: &Path,
    now: u64,
    owner_matches_claim: impl Fn(u32, u64) -> bool,
) -> Result<()> {
    for artifact in recovery_artifacts(path)? {
        let raw = std::fs::read(&artifact)
            .map_err(|err| anyhow!("reading handoff recovery artifact: {err}"))?;
        let name = artifact
            .file_name()
            .map(|value| value.to_string_lossy())
            .unwrap_or_default();
        let stale = if let Ok(lease) = serde_json::from_slice::<BridgeQuiesceLease>(&raw) {
            let emergency = name.contains(".cas-emergency-");
            let max_lifetime = if emergency {
                120
            } else {
                BRIDGE_LEASE_MAX_SECONDS
            };
            if lease.schema_version != 1
                || !valid_bridge_lease_id(&lease.lease_id)
                || lease.owner_pid == 0
                || lease.created_at == 0
                || lease.expires_at < lease.created_at
                || lease.expires_at.saturating_sub(lease.created_at) > max_lifetime
                || lease.handoff_grace_until < lease.created_at
                || lease.handoff_grace_until > lease.expires_at
                || lease.handoff_grace_until.saturating_sub(lease.created_at)
                    > BRIDGE_LEASE_HANDOFF_GRACE_SECONDS
            {
                false
            } else if emergency {
                // Emergency recovery is deliberately non-adoptable and blocks
                // through its bounded expiry even when its owner is dead.
                now > lease.expires_at
            } else {
                // Ordinary CAS generations follow the same owner/handoff
                // contract as the primary lease. A dead owner keeps the gate
                // closed through the final grace second, then its exact bytes
                // may be retired without waiting for the full lease lifetime.
                now > lease.expires_at
                    || (now > lease.handoff_grace_until
                        && !owner_matches_claim(lease.owner_pid, lease.created_at))
            }
        } else if let Some((pid, claimed_at)) = update_marker_identity_from_raw(&raw) {
            !pid_matches_claim(pid, claimed_at)
        } else {
            false
        };
        if stale {
            retire_recovery_artifact_exact(path, &artifact, &raw)?;
        } else {
            return Err(anyhow!(
                "a fresh or unreadable handoff CAS recovery artifact is present"
            ));
        }
    }
    Ok(())
}

fn retire_recovery_artifact_exact(path: &Path, artifact: &Path, expected_raw: &[u8]) -> Result<()> {
    let isolated = bridge_lease_sibling(
        path,
        &format!(
            ".cas-release-{}-{}",
            std::process::id(),
            uuid::Uuid::new_v4()
        ),
    )?;
    std::fs::rename(artifact, &isolated)
        .map_err(|err| anyhow!("isolating stale recovery artifact: {err}"))?;
    let isolated_raw = std::fs::read(&isolated)?;
    if isolated_raw != expected_raw {
        if !artifact.exists() {
            let _ = std::fs::hard_link(&isolated, artifact);
        }
        return Err(anyhow!(
            "handoff recovery artifact changed during exact-byte retirement"
        ));
    }
    std::fs::remove_file(&isolated)
        .map_err(|err| anyhow!("retiring stale handoff recovery artifact: {err}"))?;
    Ok(())
}

fn write_new_bridge_lease_file(path: &Path, raw: &[u8]) -> Result<()> {
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)?;
    file.write_all(raw)?;
    file.sync_all()?;
    Ok(())
}

fn publish_update_marker_atomically(
    path: &Path,
    expected_raw: Option<&[u8]>,
    raw: &[u8],
) -> Result<()> {
    let nonce = format!("-{}-{}", std::process::id(), uuid::Uuid::new_v4());
    let temporary = bridge_lease_sibling(path, &format!(".cas-shadow{nonce}"))?;
    write_new_bridge_lease_file(&temporary, raw)?;
    let result = if let Some(expected_raw) = expected_raw {
        replace_file_atomically_with_expected(path, &temporary, expected_raw, raw)
    } else {
        // Linking a fully flushed same-directory temporary into an absent name
        // is an atomic create-if-absent operation on NTFS and Unix filesystems.
        // A concurrent claimant wins with AlreadyExists; it is never replaced.
        std::fs::hard_link(&temporary, path)
            .map_err(|err| anyhow!("atomically publishing update marker: {err}"))
    };
    if result.is_ok() && temporary.exists() {
        std::fs::remove_file(&temporary)
            .map_err(|err| anyhow!("retiring update-marker shadow: {err}"))?;
    }
    result
}

fn replace_bridge_lease_atomically(
    path: &Path,
    expected_raw: &[u8],
    lease: &BridgeQuiesceLease,
) -> Result<()> {
    let raw = serde_json::to_vec(lease)?;
    let nonce = format!("-{}-{}", std::process::id(), uuid::Uuid::new_v4());
    let temporary = bridge_lease_sibling(path, &format!(".cas-shadow{nonce}"))?;
    write_new_bridge_lease_file(&temporary, &raw)?;

    let result = replace_file_atomically_with_expected(path, &temporary, expected_raw, &raw);
    if result.is_ok() && temporary.exists() {
        std::fs::remove_file(&temporary)
            .map_err(|err| anyhow!("retiring bridge-lease shadow: {err}"))?;
    }
    result
}

fn replace_file_atomically_with_expected(
    path: &Path,
    temporary: &Path,
    expected_raw: &[u8],
    renewed_raw: &[u8],
) -> Result<()> {
    let nonce = format!("-{}-{}", std::process::id(), uuid::Uuid::new_v4());
    let predecessor = bridge_lease_sibling(path, &format!(".cas-previous{nonce}"))?;
    std::fs::rename(path, &predecessor)
        .map_err(|err| anyhow!("isolating the handoff marker predecessor: {err}"))?;
    let predecessor_raw = std::fs::read(&predecessor)?;
    if predecessor_raw != expected_raw {
        // Exact-byte CAS failed. Restore only by atomic create-if-absent; a
        // concurrent canonical claimant is never overwritten.
        if !path.exists() {
            let _ = std::fs::hard_link(&predecessor, path);
        }
        return Err(anyhow!(
            "handoff marker ownership changed during atomic replacement"
        ));
    }

    if let Err(err) = std::fs::hard_link(temporary, path) {
        if !path.exists() {
            let _ = std::fs::hard_link(&predecessor, path);
        }
        return Err(anyhow!("publishing the renewed handoff marker: {err}"));
    }
    if std::fs::read(path).ok().as_deref() != Some(renewed_raw) {
        return Err(anyhow!(
            "handoff marker changed immediately after atomic replacement"
        ));
    }
    std::fs::remove_file(&predecessor)
        .map_err(|err| anyhow!("retiring the handoff predecessor: {err}"))?;
    Ok(())
}

async fn run_update(app: AppHandle) -> Result<()> {
    let hermes_home = install_global_hermes_home();
    let install_root = hermes_home.join("hermes-agent");
    let bridge_lease_id = bridge_lease_id_from_args(std::env::args().skip(1));

    #[cfg(target_os = "windows")]
    {
        if ensure_windows_handoff_result_slot_clear(&hermes_home).is_err() {
            let msg = "A previous Desktop update result or its recovery artifact has not been consumed safely. Open Hermes to finish that handoff, then retry; nothing was changed.".to_string();
            emit(
                &app,
                BootstrapEvent::Failed {
                    stage: Some("handoff".into()),
                    error: msg.clone(),
                },
            );
            return Err(anyhow!(msg));
        }
    }

    // Mutual exclusion (#50238): publish an "update in progress" marker for the
    // entire duration of this update. A desktop instance the user relaunches
    // mid-update consults this before spawning its own local backend — without
    // it, that backend re-locks the venv shim, and the relaunch/lock cycle
    // loops. The guard
    // removes the marker on every exit path (incl. early returns / panics).
    //
    // The same marker is the cross-process update lock (hermes_cli/
    // update_lock.py claims it too), so a live foreign owner means another
    // updater — most often a dashboard-spawned `hermes update` — is already
    // mutating this checkout. Refuse instead of running a second one over it.
    let mut update_marker =
        match UpdateMarkerGuard::acquire(hermes_home.join(".hermes-update-in-progress")) {
            Ok(guard) => guard,
            Err(UpdateMarkerAcquireError::ForeignOwner(owner)) => {
                let mins = owner.age_secs / 60;
                let secs = owner.age_secs % 60;
                let elapsed = if mins > 0 {
                    format!("{mins}m {secs}s")
                } else {
                    format!("{secs}s")
                };
                let msg = format!(
                    "Another Hermes update is already running (PID {}, started {} ago). \
                 Wait for it to finish, or close the window or dashboard tab that \
                 started it, then try again.",
                    owner.pid, elapsed
                );
                emit(
                    &app,
                    BootstrapEvent::Failed {
                        stage: None,
                        error: msg.clone(),
                    },
                );
                return Err(anyhow!(msg));
            }
            Err(UpdateMarkerAcquireError::Publish(err)) => {
                let msg = format!(
                "Could not publish the updater handoff marker safely: {err}. Nothing was changed."
            );
                emit(
                    &app,
                    BootstrapEvent::Failed {
                        stage: Some("handoff".into()),
                        error: msg.clone(),
                    },
                );
                return Err(anyhow!(msg));
            }
        };

    let update_branch = update_branch_from_args(std::env::args().skip(1))
        .or_else(|| option_env_string("BUILD_PIN_BRANCH"))
        .unwrap_or_else(|| "main".to_string());
    let target_app = if cfg!(target_os = "macos") {
        target_app_from_args(std::env::args().skip(1))
    } else {
        None
    };

    let hermes_python = resolve_update_python(&install_root).ok_or_else(|| {
        let msg = format!(
            "Could not find the managed Hermes Python under {}. Is Hermes installed? \
             Re-run the installer to repair the install.",
            install_root.display()
        );
        emit(
            &app,
            BootstrapEvent::Failed {
                stage: None,
                error: msg.clone(),
            },
        );
        anyhow!(msg)
    })?;

    // Synthetic manifest so the existing progress UI renders our stages.
    emit(
        &app,
        BootstrapEvent::Manifest {
            stages: update_stages(target_app.is_some()),
            protocol_version: None,
        },
    );

    // ---- stage 1: wait for the old desktop to die ------------------------
    // The desktop exec'd us then called app.exit(), but process teardown is
    // async on Windows. If it still holds the venv shim, `hermes update`
    // aborts with exit 2. If it still holds the packaged app.asar,
    // install.ps1's repair/re-clone path cannot move/remove the install tree.
    // Give both handles a bounded window to clear. Surfaced as its own stage
    // (rather than a silent pre-step) so a slow close / force-kill reads as
    // real progress instead of a frozen first bar.
    let started = Instant::now();
    emit_stage(&app, "handoff", StageState::Running, None, None);
    let mut bridge_lease = match BridgeQuiesceLeaseGuard::adopt(
        hermes_home.join(BRIDGE_LEASE_FILENAME),
        bridge_lease_id.as_deref(),
        &install_root,
    ) {
        Ok(lease) => lease,
        Err(err) => {
            let message = format!("Could not safely adopt the bridge-quiesce handoff: {err}");
            emit_stage(
                &app,
                "handoff",
                StageState::Failed,
                Some(started.elapsed().as_millis() as u64),
                Some(message.clone()),
            );
            emit(
                &app,
                BootstrapEvent::Failed {
                    stage: Some("handoff".into()),
                    error: message,
                },
            );
            return Ok(());
        }
    };
    if let Err(err) = wait_for_install_locks_free(&install_root, &app, "handoff").await {
        let duration_ms = started.elapsed().as_millis() as u64;
        let message = err.to_string();
        emit_stage(
            &app,
            "handoff",
            StageState::Failed,
            Some(duration_ms),
            Some(message.clone()),
        );
        emit(
            &app,
            BootstrapEvent::Failed {
                stage: Some("handoff".into()),
                error: message,
            },
        );
        // The user-facing failure is already emitted with its exact stage.
        // Treat it as a handled terminal outcome so start_update's defensive
        // catch-all does not overwrite it with an unscoped duplicate error.
        return Ok(());
    }
    emit_stage(
        &app,
        "handoff",
        StageState::Succeeded,
        Some(started.elapsed().as_millis() as u64),
        None,
    );

    // ---- stage 2: hermes update -----------------------------------------
    // Pass --branch so `hermes update` targets the branch this installer was
    // built/pinned against (BUILD_PIN_BRANCH), NOT its built-in default of
    // `main`. The install was a detached-HEAD checkout of a specific commit;
    // without --branch, `hermes update` switches the checkout to `main` (a
    // divergent branch that may not even have the desktop CLI command), then
    // reports "already up to date" against the wrong branch. The desktop
    // detected the update against this same branch, so we must update against
    // it too.
    emit_log(
        &app,
        Some("update"),
        LogStream::Stdout,
        &format!("[update] updating against branch {update_branch}"),
    );
    let child_env = update_child_env(&install_root);
    let invocation_id = format!("invocation-{}", uuid::Uuid::new_v4().simple());
    let mut update_args: Vec<String> = vec![
        "-m".into(),
        "hermes_cli.main".into(),
        "update".into(),
        "--yes".into(),
        "--gateway".into(),
        "--defer-gateway-resume".into(),
    ];
    // --force skips `hermes update`'s Windows running-exe guard (which would
    // `sys.exit(2)` and dead-end the handoff). By contract the desktop has
    // already exited and this updater has proved the install targets unlocked.
    // Unknown holders are never killed: the handoff fails before mutation if
    // the final bounded lock probe remains blocked.
    //
    // NOTE: --force does NOT bypass the venv-python holder guard (that needs
    // an explicit `--force-venv`, which we deliberately do not pass). Our lock
    // probe only checks the hermes.exe shim and app.asar, so an external venv
    // python holding a native .pyd (a user terminal, an unmanaged gateway)
    // could still be alive here — mutating the venv under it would strand the
    // install half-updated. If that guard fires, it exits 2 and the match arm
    // below surfaces the correct "close all Hermes windows" message.
    update_args.push("--force".into());
    update_args.push("--branch".into());
    update_args.push(update_branch.clone());
    update_args.push("--invocation-id".into());
    update_args.push(invocation_id.clone());
    update_args.push("--bridge-lease-id".into());
    update_args.push(bridge_lease.lease_id.clone());

    emit_stage(&app, "update", StageState::Running, None, None);
    let started = Instant::now();
    let update_started_at = unix_time_seconds();
    let update = run_streamed(
        &app,
        &hermes_python,
        &update_args,
        &install_root,
        &child_env,
        Some("update"),
        Some(&mut bridge_lease),
    )
    .await;
    let update_ms = started.elapsed().as_millis() as u64;

    let mut verified_update_receipt = None;
    let update_failure = match update {
        Ok(CmdResult { exit_code: Some(0) }) => match validate_update_receipt(
            &hermes_home.join(".hermes-update-receipt.json"),
            &invocation_id,
            &bridge_lease.lease_id,
            &install_root,
            &update_branch,
            update_started_at,
            unix_time_seconds(),
        ) {
            Ok(receipt) => {
                verified_update_receipt = Some(receipt);
                emit_stage(&app, "update", StageState::Succeeded, Some(update_ms), None);
                None
            }
            Err(err) => Some(format!(
                "Hermes exited successfully without a fresh, correlated update receipt: {err}"
            )),
        },
        Ok(CmdResult {
            exit_code: Some(code),
        }) if code == UPDATE_EXIT_CONCURRENT => Some(
            "Hermes is still running. Close all Hermes windows and try the update again."
                .to_string(),
        ),
        Ok(CmdResult { exit_code: other }) => Some(format!(
            "hermes update failed (exit {:?}). See {} for details.",
            other,
            hermes_home.join("logs").join("update.log").display()
        )),
        Err(err) => Some(format!(
            "Hermes update containment failed before a proved terminal state: {err}"
        )),
    };

    // The contained update created an authenticated plan before stopping any
    // gateway and returned the lease after its Job drained. Restore that exact
    // plan immediately, even when update failed. The plan and lease have
    // bounded lifetimes; the later Desktop-only rebuild must not postpone fleet
    // recovery or keep gateways stopped beyond those contracts.
    emit_log(
        &app,
        Some("update"),
        LogStream::Stdout,
        "[update] restoring the verified gateway fleet outside mutation containment",
    );
    let resume_args: Vec<String> = vec![
        "-m".into(),
        "hermes_cli.main".into(),
        "update".into(),
        "--resume-deferred-gateway".into(),
        "--invocation-id".into(),
        invocation_id.clone(),
        "--bridge-lease-id".into(),
        bridge_lease.lease_id.clone(),
        "--root".into(),
        install_root.to_string_lossy().into_owned(),
    ];
    let resume = run_deferred_gateway_resume(
        &app,
        &hermes_python,
        &resume_args,
        &install_root,
        &child_env,
        &invocation_id,
        &install_root,
        &mut bridge_lease,
        OTHER_STAGE_TIMEOUT,
    )
    .await;
    let resume_failure = match resume {
        Ok(CmdResult { exit_code: Some(0) }) => None,
        Ok(CmdResult { exit_code }) => Some(format!(
            "the trusted deferred gateway recovery command failed (exit {:?})",
            exit_code
        )),
        Err(err) => Some(format!(
            "the trusted deferred gateway recovery did not reach a proved terminal state: {err}"
        )),
    };
    if let Some(msg) = resume_failure {
        emit(
            &app,
            BootstrapEvent::Failed {
                stage: Some("handoff".into()),
                error: msg.clone(),
            },
        );
        return Err(anyhow!(msg));
    }
    if let Some(msg) = update_failure {
        emit_stage(
            &app,
            "update",
            StageState::Failed,
            Some(update_ms),
            Some(msg.clone()),
        );
        emit(
            &app,
            BootstrapEvent::Failed {
                stage: Some("update".into()),
                error: msg.clone(),
            },
        );
        return Err(anyhow!(msg));
    }
    let verified_update_receipt = verified_update_receipt.ok_or_else(|| {
        anyhow!("the successful update lost its verified receipt before Desktop relaunch")
    })?;

    // ---- stage 3: hermes desktop --build-only ----------------------------
    // Python source/venv mutation and gateway recovery are already complete.
    // This stage only rebuilds the Desktop artifact and remains contained in a
    // fresh exact-child Job with its own bounded deadline.
    emit_stage(&app, "rebuild", StageState::Running, None, None);
    let started = Instant::now();
    let rebuild_args: Vec<String> = vec![
        "-m".into(),
        "hermes_cli.main".into(),
        "desktop".into(),
        "--force-build".into(),
        "--build-only".into(),
    ];
    // The relaunched Desktop proves it is the artifact produced by this exact
    // mutation receipt. Pin the stamp input for both git (40 hex) and archive
    // (64 hex) updates; never inherit a stale ambient CI GITHUB_SHA.
    let mut rebuild_env = child_env.clone();
    rebuild_env.extend(receipt_build_identity_env(&verified_update_receipt)?);
    let mut rebuild = run_streamed(
        &app,
        &hermes_python,
        &rebuild_args,
        &install_root,
        &rebuild_env,
        Some("rebuild"),
        None,
    )
    .await;

    if matches!(&rebuild, Ok(result) if rebuild_needs_retry(result.exit_code)) {
        emit_log(
            &app,
            Some("rebuild"),
            LogStream::Stdout,
            "[rebuild] first desktop rebuild failed; retrying once (a self-healed \
             Electron download builds clean on the second run)…",
        );
        rebuild = run_streamed(
            &app,
            &hermes_python,
            &rebuild_args,
            &install_root,
            &rebuild_env,
            Some("rebuild"),
            None,
        )
        .await;
    }
    let rebuild_ms = started.elapsed().as_millis() as u64;
    let rebuild_failure = match rebuild {
        Ok(CmdResult { exit_code: Some(0) }) => {
            emit_stage(
                &app,
                "rebuild",
                StageState::Succeeded,
                Some(rebuild_ms),
                None,
            );
            None
        }
        Ok(CmdResult { exit_code }) => Some(format!(
            "Rebuilding the desktop app failed (exit {:?}). The update was applied but the app could not be rebuilt.",
            exit_code
        )),
        Err(err) => Some(format!(
            "The desktop rebuild did not reach a contained terminal state: {err}"
        )),
    };
    if let Some(msg) = rebuild_failure.as_ref() {
        emit_stage(
            &app,
            "rebuild",
            StageState::Failed,
            Some(rebuild_ms),
            Some(msg.clone()),
        );
    }
    if let Some(msg) = rebuild_failure {
        emit(
            &app,
            BootstrapEvent::Failed {
                stage: Some("rebuild".into()),
                error: msg.clone(),
            },
        );
        return Err(anyhow!(msg));
    }

    let launch_target = if let Some(target_app) = target_app {
        let started = Instant::now();
        emit_stage(&app, "install", StageState::Running, None, None);
        match install_macos_app_update(&app, &install_root, &target_app).await {
            Ok(installed_app) => {
                emit_stage(
                    &app,
                    "install",
                    StageState::Succeeded,
                    Some(started.elapsed().as_millis() as u64),
                    None,
                );
                Some(installed_app)
            }
            Err(err) => {
                let msg = format!("{err:#}");
                emit_stage(
                    &app,
                    "install",
                    StageState::Failed,
                    Some(started.elapsed().as_millis() as u64),
                    Some(msg.clone()),
                );
                emit(
                    &app,
                    BootstrapEvent::Failed {
                        stage: Some("install".into()),
                        error: msg.clone(),
                    },
                );
                return Err(anyhow!(msg));
            }
        }
    } else {
        None
    };

    // ---- done: release transaction state, relaunch, then signal complete --
    // Every install-tree mutation is finished. Release the lock BEFORE the
    // relaunch: this process can stay wedged in its native event loop even
    // after a successful app.exit(), and a live pid on a fresh marker would
    // make a completed update look active — blocking desktop startup and
    // every other updater while this updater PID remains live.
    if let Err(err) = bridge_lease.complete() {
        let msg = "Update finished, but the bridge-quiesce lease could not be released safely; refusing to report completion.".to_string();
        emit(
            &app,
            BootstrapEvent::Failed {
                stage: Some("handoff".into()),
                error: msg.clone(),
            },
        );
        return Err(anyhow!("{msg} {err}"));
    }
    if let Err(err) = update_marker.complete() {
        let msg = "Update finished, but the update handoff marker could not be released safely; refusing to report completion.".to_string();
        emit(
            &app,
            BootstrapEvent::Failed {
                stage: Some("handoff".into()),
                error: msg.clone(),
            },
        );
        return Err(anyhow!("{msg} {err}"));
    }

    let relaunch = if let Some(target_app) = launch_target {
        launch_macos_app_and_exit(&app, &target_app).await
    } else if cfg!(target_os = "windows") {
        launch_windows_desktop_with_readiness_proof(
            &hermes_home,
            &install_root,
            &verified_update_receipt,
        )
        .await
    } else {
        crate::bootstrap::launch_hermes_desktop(
            app.clone(),
            install_root.to_string_lossy().into_owned(),
        )
        .await
        .map_err(|err| anyhow!(err))
    };
    if let Err(err) = relaunch {
        let msg = format!(
            "Update finished, but Hermes could not be relaunched: {err}. Launch Hermes manually."
        );
        emit(
            &app,
            BootstrapEvent::Failed {
                stage: None,
                error: msg.clone(),
            },
        );
        return Err(anyhow!(msg));
    }

    emit(
        &app,
        BootstrapEvent::Complete {
            install_root: install_root.to_string_lossy().into_owned(),
            marker: None,
        },
    );

    // Only now have mutation receipt, exact cleanup, and (on Windows) an
    // attempt-correlated, healthy Desktop backend acknowledgment all been
    // proved. Merely spawning or briefly observing a process is never enough.
    #[cfg(target_os = "windows")]
    app.exit(0);

    // The launch helpers normally request exit themselves, but their failure
    // paths must still close a successful updater. A native event loop can
    // ignore that graceful request, so arm a process-exit fallback now that
    // all update state and the marker have been settled.
    exit_after_success(&app);
    Ok(())
}

/// Ask the app to exit, with a hard `process::exit` fallback for a native
/// event loop that ignores the graceful request. Without it a finished updater
/// can linger as a live pid forever.
fn exit_after_success(app: &AppHandle) {
    std::thread::spawn(|| {
        std::thread::sleep(std::time::Duration::from_secs(3));
        tracing::warn!("graceful updater exit timed out; forcing process exit");
        std::process::exit(0);
    });
    app.exit(0);
}

/// Poll until the venv shim AND packaged desktop app bundle are no longer locked
/// (Windows) or a bounded timeout elapses. On non-Windows this is a short fixed
/// grace since file locking isn't the failure mode there.
#[derive(Debug, PartialEq, Eq)]
pub(crate) struct InstallLockWaitError {
    locked_paths: Vec<PathBuf>,
}

impl std::fmt::Display for InstallLockWaitError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "Hermes installation files are still locked after cleanup ({}). Close all Hermes windows and background processes, then try again.",
            format_locked_paths(&self.locked_paths)
        )
    }
}

impl std::error::Error for InstallLockWaitError {}

pub(crate) async fn wait_for_install_locks_free(
    install_root: &Path,
    app: &AppHandle,
    stage: &str,
) -> std::result::Result<(), InstallLockWaitError> {
    let lock_targets = install_lock_probe_paths(install_root);
    let deadline = Instant::now() + DESKTOP_EXIT_WAIT;

    emit_log(
        app,
        Some(stage),
        LogStream::Stdout,
        "[handoff] waiting for Hermes to exit…",
    );

    loop {
        let locked = locked_paths(&lock_targets);
        if locked.is_empty() {
            return Ok(());
        }
        if Instant::now() >= deadline {
            // A backend hermes.exe (or the desktop Hermes.exe itself) is still
            // holding an update-sensitive file. The updater has no proven PID
            // identity for that holder, so image-wide taskkill would risk an
            // unrelated install or user session. Give the OS one final beat,
            // then fail closed instead of mutating under an unknown process.
            emit_log(
                app,
                Some(stage),
                LogStream::Stderr,
                &format!(
                    "[handoff] Hermes still holding install files ({}); refusing unsafe image-wide process cleanup",
                    format_locked_paths(&locked)
                ),
            );
            tokio::time::sleep(Duration::from_millis(800)).await;
            let locked_after_cleanup = locked_paths(&lock_targets);
            if locked_after_cleanup.is_empty() {
                emit_log(
                    app,
                    Some(stage),
                    LogStream::Stdout,
                    "[handoff] install files freed during the final lock check",
                );
            } else {
                let result = lock_cleanup_result(locked_after_cleanup, cfg!(target_os = "windows"));
                if let Err(err) = &result {
                    emit_log(
                        app,
                        Some(stage),
                        LogStream::Stderr,
                        &format!("[handoff] {err}"),
                    );
                } else {
                    emit_log(
                        app,
                        Some(stage),
                        LogStream::Stdout,
                        "[handoff] install-file probe remains advisory on this platform; continuing",
                    );
                }
                return result;
            }
            return Ok(());
        }
        tokio::time::sleep(DESKTOP_EXIT_POLL).await;
    }
}

fn lock_cleanup_result(
    locked_paths: Vec<PathBuf>,
    fail_closed: bool,
) -> std::result::Result<(), InstallLockWaitError> {
    if locked_paths.is_empty() || !fail_closed {
        return Ok(());
    }
    Err(InstallLockWaitError { locked_paths })
}

fn install_lock_probe_paths(install_root: &Path) -> Vec<PathBuf> {
    let mut paths = vec![venv_hermes(install_root)];
    paths.extend(desktop_app_payload_paths(install_root));
    paths
}

fn desktop_app_payload_paths(install_root: &Path) -> Vec<PathBuf> {
    let release = install_root.join("apps").join("desktop").join("release");
    if cfg!(target_os = "windows") {
        vec![
            release
                .join("win-unpacked")
                .join("resources")
                .join("app.asar"),
            release
                .join("win-arm64-unpacked")
                .join("resources")
                .join("app.asar"),
        ]
    } else if cfg!(target_os = "macos") {
        vec![
            release
                .join("mac")
                .join("Hermes.app")
                .join("Contents")
                .join("Resources")
                .join("app.asar"),
            release
                .join("mac-arm64")
                .join("Hermes.app")
                .join("Contents")
                .join("Resources")
                .join("app.asar"),
        ]
    } else {
        vec![release
            .join("linux-unpacked")
            .join("resources")
            .join("app.asar")]
    }
}

fn locked_paths(paths: &[PathBuf]) -> Vec<PathBuf> {
    paths.iter().filter(|p| is_locked(p)).cloned().collect()
}

fn format_locked_paths(paths: &[PathBuf]) -> String {
    paths
        .iter()
        .map(|p| p.display().to_string())
        .collect::<Vec<_>>()
        .join(", ")
}

/// Best-effort lock probe: try to open the file for read+write. On Windows an
/// exclusively-held running .exe refuses the open with a sharing violation.
/// On Unix this almost always succeeds (no mandatory locking), which is fine —
/// the venv-shim contention is a Windows-only problem.
fn is_locked(path: &Path) -> bool {
    if !path.exists() {
        return false;
    }
    match std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(path)
    {
        Ok(_) => false,
        Err(_) => true,
    }
}

/// Whether the `desktop --build-only` rebuild should be retried once. Any
/// non-success exit qualifies: the common cause is a transient first-attempt
/// failure (still-settling tree / self-healed Electron download) that a clean
/// second run resolves.
fn rebuild_needs_retry(exit_code: Option<i32>) -> bool {
    exit_code != Some(0)
}

#[cfg(target_os = "windows")]
struct UpdaterJob {
    handle: windows_sys::Win32::Foundation::HANDLE,
}

// A Windows job HANDLE is process-local kernel state and may be moved between
// Tokio worker threads. This wrapper is its sole owner and closes it in Drop.
#[cfg(target_os = "windows")]
unsafe impl Send for UpdaterJob {}
#[cfg(target_os = "windows")]
unsafe impl Sync for UpdaterJob {}

#[cfg(target_os = "windows")]
impl UpdaterJob {
    fn new() -> Result<Self> {
        use windows_sys::Win32::System::JobObjects::{
            CreateJobObjectW, JobObjectExtendedLimitInformation, SetInformationJobObject,
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        };

        let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if handle.is_null() {
            return Err(anyhow!(
                "creating updater containment job: {}",
                std::io::Error::last_os_error()
            ));
        }
        let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
        // Do not grant BREAKAWAY_OK: that limit is job-wide and would let any
        // mutating descendant deliberately escape fail-stop containment. The
        // Python update writes a correlated deferred-resume plan instead; the
        // parent executes that trusted plan only after this Job proves empty.
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let configured = unsafe {
            SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                (&info as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        };
        if configured == 0 {
            let err = std::io::Error::last_os_error();
            unsafe {
                windows_sys::Win32::Foundation::CloseHandle(handle);
            }
            return Err(anyhow!("configuring updater containment job: {err}"));
        }
        Ok(Self { handle })
    }

    fn assign(&self, child: &tokio::process::Child) -> Result<()> {
        use windows_sys::Win32::System::JobObjects::AssignProcessToJobObject;

        let child_handle = child
            .raw_handle()
            .ok_or_else(|| anyhow!("updater child exited before containment"))?;
        let assigned = unsafe { AssignProcessToJobObject(self.handle, child_handle.cast()) };
        if assigned == 0 {
            return Err(anyhow!(
                "assigning updater child to containment job: {}",
                std::io::Error::last_os_error()
            ));
        }
        Ok(())
    }

    fn terminate(&self) -> Result<()> {
        use windows_sys::Win32::System::JobObjects::TerminateJobObject;

        let terminated = unsafe { TerminateJobObject(self.handle, 8) };
        if terminated == 0 {
            return Err(anyhow!(
                "terminating updater containment job: {}",
                std::io::Error::last_os_error()
            ));
        }
        Ok(())
    }

    fn contains_pid(&self, pid: u32) -> Result<bool> {
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::JobObjects::IsProcessInJob;
        use windows_sys::Win32::System::Threading::{
            OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
        };

        let process = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
        if process.is_null() {
            return Err(anyhow!(
                "opening claimed updater lease owner PID {pid}: {}",
                std::io::Error::last_os_error()
            ));
        }
        let mut contained = 0;
        let checked = unsafe { IsProcessInJob(process, self.handle, &mut contained) };
        unsafe { CloseHandle(process) };
        if checked == 0 {
            return Err(anyhow!(
                "checking updater lease owner job membership: {}",
                std::io::Error::last_os_error()
            ));
        }
        Ok(contained != 0)
    }

    fn active_processes(&self) -> Result<u32> {
        use windows_sys::Win32::System::JobObjects::{
            JobObjectBasicAccountingInformation, QueryInformationJobObject,
            JOBOBJECT_BASIC_ACCOUNTING_INFORMATION,
        };

        let mut info: JOBOBJECT_BASIC_ACCOUNTING_INFORMATION = unsafe { std::mem::zeroed() };
        let queried = unsafe {
            QueryInformationJobObject(
                self.handle,
                JobObjectBasicAccountingInformation,
                (&mut info as *mut JOBOBJECT_BASIC_ACCOUNTING_INFORMATION).cast(),
                std::mem::size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>() as u32,
                std::ptr::null_mut(),
            )
        };
        if queried == 0 {
            return Err(anyhow!(
                "querying updater containment job accounting: {}",
                std::io::Error::last_os_error()
            ));
        }
        Ok(info.ActiveProcesses)
    }

    async fn wait_drained(&self, timeout: Duration) -> Result<()> {
        let deadline = Instant::now() + timeout;
        loop {
            let active = self.active_processes()?;
            if active == 0 {
                return Ok(());
            }
            if Instant::now() >= deadline {
                return Err(anyhow!(
                    "the updater child exited with {active} contained descendant(s) still active"
                ));
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    }
}

#[cfg(target_os = "windows")]
impl Drop for UpdaterJob {
    fn drop(&mut self) {
        unsafe {
            windows_sys::Win32::Foundation::CloseHandle(self.handle);
        }
    }
}

#[cfg(target_os = "windows")]
struct WindowsUpdaterStartupGate {
    path: PathBuf,
}

#[cfg(target_os = "windows")]
impl WindowsUpdaterStartupGate {
    fn new() -> Self {
        Self {
            path: env::temp_dir().join(format!(
                "hermes-updater-job-gate-{}-{}",
                std::process::id(),
                uuid::Uuid::new_v4()
            )),
        }
    }

    fn release(&self) -> Result<()> {
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&self.path)
            .map_err(|err| anyhow!("releasing contained updater startup gate: {err}"))?;
        file.write_all(b"ready\n")?;
        file.sync_all()?;
        Ok(())
    }
}

#[cfg(target_os = "windows")]
impl Drop for WindowsUpdaterStartupGate {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.path);
    }
}

#[cfg(target_os = "windows")]
const CONTAINED_UPDATER_WRAPPER: &str = r#"
$ErrorActionPreference = 'Stop'
$gate = $env:HERMES_INTERNAL_UPDATE_JOB_GATE
for ($i = 0; $i -lt 300 -and -not (Test-Path -LiteralPath $gate); $i++) {
    Start-Sleep -Milliseconds 100
}
if (-not (Test-Path -LiteralPath $gate)) { exit 13 }
$program = $env:HERMES_INTERNAL_UPDATE_PROGRAM
$childArgs = @((ConvertFrom-Json -InputObject $env:HERMES_INTERNAL_UPDATE_ARGS_JSON))
& $program @childArgs
if ($null -eq $LASTEXITCODE) { exit 1 }
exit $LASTEXITCODE
"#;

/// Spawn `hermes <args>` from `cwd`, stream stdout/stderr as Log events on the
/// bootstrap channel, and return the exit code. Mirrors powershell::run_script
/// but for an arbitrary command (no install.ps1 -File wrapping).
async fn run_streamed(
    app: &AppHandle,
    program: &Path,
    args: &[String],
    cwd: &Path,
    envs: &[(String, OsString)],
    stage: Option<&str>,
    bridge_lease: Option<&mut BridgeQuiesceLeaseGuard>,
) -> Result<CmdResult> {
    run_streamed_with_timeout(
        app,
        program,
        args,
        cwd,
        envs,
        stage,
        bridge_lease,
        stage_timeout(stage),
    )
    .await
}

fn stage_timeout(stage: Option<&str>) -> Duration {
    match stage {
        Some("update") => UPDATE_STAGE_TIMEOUT,
        Some("rebuild") => REBUILD_STAGE_TIMEOUT,
        _ => OTHER_STAGE_TIMEOUT,
    }
}

async fn run_streamed_with_timeout(
    app: &AppHandle,
    program: &Path,
    args: &[String],
    cwd: &Path,
    envs: &[(String, OsString)],
    stage: Option<&str>,
    mut bridge_lease: Option<&mut BridgeQuiesceLeaseGuard>,
    timeout: Duration,
) -> Result<CmdResult> {
    #[cfg(target_os = "windows")]
    let startup_gate = WindowsUpdaterStartupGate::new();
    #[cfg(target_os = "windows")]
    let mut cmd = {
        let mut command = Command::new("powershell.exe");
        command.args([
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            CONTAINED_UPDATER_WRAPPER,
        ]);
        command.env("HERMES_INTERNAL_UPDATE_JOB_GATE", &startup_gate.path);
        command.env("HERMES_INTERNAL_UPDATE_PROGRAM", program);
        command.env(
            "HERMES_INTERNAL_UPDATE_ARGS_JSON",
            serde_json::to_string(args)?,
        );
        command
    };
    #[cfg(not(target_os = "windows"))]
    let mut cmd = Command::new(program);
    #[cfg(not(target_os = "windows"))]
    cmd.args(args);
    cmd.current_dir(cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    for (key, value) in envs {
        cmd.env(key, value);
    }

    #[cfg(target_os = "windows")]
    let updater_job = UpdaterJob::new()?;

    #[cfg(target_os = "windows")]
    {
        // CREATE_NO_WINDOW = 0x08000000 — no flashing console behind the GUI.
        cmd.creation_flags(0x0800_0000);
    }

    let mut child = cmd.spawn().map_err(|e| {
        anyhow!(
            "spawning {} for the {} stage: {e}",
            program.display(),
            stage.unwrap_or("command")
        )
    })?;
    let child_pid = child
        .id()
        .ok_or_else(|| anyhow!("the updater child exited before its PID could be verified"))?;

    #[cfg(target_os = "windows")]
    if let Err(err) = updater_job.assign(&child) {
        // Assignment failure means descendants cannot be contained. Stop the
        // exact process handle before returning; never fall back to image-name
        // matching that could terminate unrelated Python or Codex processes.
        let _ = child.kill().await;
        return Err(err);
    }
    #[cfg(target_os = "windows")]
    if let Err(err) = startup_gate.release() {
        let _ = updater_job.terminate();
        let _ = child.kill().await;
        return Err(err);
    }

    let stdout = child.stdout.take().expect("stdout piped");
    let stderr = child.stderr.take().expect("stderr piped");
    // Same non-UTF-8-safe decode path as powershell::run_script (#67193).
    let mut out = BufReader::new(stdout);
    let mut err = BufReader::new(stderr);
    let mut out_buf = Vec::new();
    let mut err_buf = Vec::new();

    let stage_owned = stage.map(|s| s.to_string());
    let mut stdout_done = false;
    let mut stderr_done = false;
    let mut lease_poll = tokio::time::interval(Duration::from_millis(50));
    lease_poll.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let mut last_lease_refresh = Instant::now();
    let mut child_cleanup_missing_since: Option<Instant> = None;
    let mut lease_unreadable_since: Option<Instant> = None;
    let mut wait = Box::pin(child.wait());
    let mut lease_error = None;
    let deadline = tokio::time::sleep(timeout);
    tokio::pin!(deadline);
    let status = loop {
        tokio::select! {
            _ = &mut deadline => {
                lease_error = Some(anyhow!(
                    "the {} stage exceeded its bounded {:?} deadline",
                    stage_owned.as_deref().unwrap_or("command"),
                    timeout
                ));
                break None;
            }
            status = &mut wait => {
                break Some(status.map_err(|e| anyhow!("waiting for child: {e}"))?);
            }
            line = read_decoded_line(&mut out, &mut out_buf), if !stdout_done => match line {
                Ok(Some(l)) => emit_log(app, stage_owned.as_deref(), LogStream::Stdout, &l),
                Ok(None) => stdout_done = true,
                Err(e) => { tracing::warn!("stdout read error: {e}"); stdout_done = true; }
            },
            line = read_decoded_line(&mut err, &mut err_buf), if !stderr_done => match line {
                Ok(Some(l)) => emit_log(app, stage_owned.as_deref(), LogStream::Stderr, &l),
                Ok(None) => stderr_done = true,
                Err(e) => { tracing::warn!("stderr read error: {e}"); stderr_done = true; }
            },
            _ = lease_poll.tick(), if bridge_lease.is_some() => {
                let lease = bridge_lease.as_deref_mut().expect("lease presence checked");
                let observation = {
                    #[cfg(target_os = "windows")]
                    {
                        lease.observe_child_transfer(child_pid, |owner_pid| {
                            updater_job.contains_pid(owner_pid)
                        })
                    }
                    #[cfg(not(target_os = "windows"))]
                    {
                        lease.observe_child_transfer(child_pid, |_| Ok(false))
                    }
                };
                match observation {
                    Ok(ChildLeaseObservation::ChildOwned) => {
                        child_cleanup_missing_since = None;
                        lease_unreadable_since = None;
                    }
                    Ok(ChildLeaseObservation::ParentReturned) => {
                        child_cleanup_missing_since = None;
                        lease_unreadable_since = None;
                    }
                    Ok(ChildLeaseObservation::ParentOwned) => {
                        child_cleanup_missing_since = None;
                        lease_unreadable_since = None;
                        if last_lease_refresh.elapsed()
                            >= Duration::from_secs(BRIDGE_LEASE_REFRESH_SECONDS)
                        {
                            if let Err(err) = lease.refresh() {
                                lease_error = Some(anyhow!(
                                    "bridge-quiesce lease lost during {} stage: {err}",
                                    stage_owned.as_deref().unwrap_or("update")
                                ));
                                break None;
                            }
                            last_lease_refresh = Instant::now();
                        }
                    }
                    Err(err) => {
                        // The child removes its lease in its terminal finally
                        // block immediately before process exit. Give that
                        // exact, already-observed owner a short bounded window
                        // for the wait handle to signal; any longer absence or
                        // any foreign rewrite still terminates the whole job.
                        if lease.transferred_to.is_some() && !lease.path.exists() {
                            let missing_since = child_cleanup_missing_since
                                .get_or_insert_with(Instant::now);
                            if missing_since.elapsed() < Duration::from_millis(250) {
                                continue;
                            }
                        }
                        // Authenticated CAS writers deliberately publish a
                        // fresh shadow before moving the primary. A poll can
                        // therefore observe a recovery artifact during a valid
                        // child transfer. Retry only for this short bounded
                        // edge; terminal proof still requires zero artifacts.
                        let unreadable_since = lease_unreadable_since
                            .get_or_insert_with(Instant::now);
                        if unreadable_since.elapsed() < Duration::from_millis(750) {
                            continue;
                        }
                        lease_error = Some(anyhow!(
                            "bridge-quiesce lease lost during {} stage: {err}",
                            stage_owned.as_deref().unwrap_or("update")
                        ));
                        break None;
                    }
                }
            }
        }
    };
    drop(wait);
    if let Some(err) = lease_error {
        // Terminate the exact updater job so shim descendants cannot keep
        // mutating after their lease is gone. Never broaden this into
        // image-name termination: unrelated user/Codex Python must survive.
        #[cfg(target_os = "windows")]
        updater_job.terminate()?;
        let _ = child.kill().await;
        let _ = child.wait().await;
        #[cfg(target_os = "windows")]
        updater_job.wait_drained(CHILD_JOB_DRAIN_TIMEOUT).await?;
        if let Some(lease) = bridge_lease.as_deref_mut() {
            if lease.transferred_to.is_some() && !lease.owned {
                if lease.require_parent_return().is_err() {
                    lease.recover_after_terminated_child().map_err(|recover_err| {
                        anyhow!(
                            "{err}; the exact terminated child lease could not be recovered: {recover_err}"
                        )
                    })?;
                }
            }
        }
        return Err(err);
    }
    let status = status.expect("child completion produces a status");
    if let Some(lease) = bridge_lease.as_deref_mut() {
        lease.require_parent_return().map_err(|err| {
            anyhow!(
                "bridge-quiesce handoff did not complete during {} stage: {err}",
                stage_owned.as_deref().unwrap_or("update")
            )
        })?;
    }
    #[cfg(target_os = "windows")]
    if let Err(err) = updater_job.wait_drained(CHILD_JOB_DRAIN_TIMEOUT).await {
        let _ = updater_job.terminate();
        return Err(err);
    }

    let drain = async {
        while let Ok(Some(l)) = read_decoded_line(&mut out, &mut out_buf).await {
            emit_log(app, stage_owned.as_deref(), LogStream::Stdout, &l);
        }
        while let Ok(Some(l)) = read_decoded_line(&mut err, &mut err_buf).await {
            emit_log(app, stage_owned.as_deref(), LogStream::Stderr, &l);
        }
    };
    tokio::time::timeout(CHILD_PIPE_DRAIN_TIMEOUT, drain)
        .await
        .map_err(|_| anyhow!("the updater child pipes did not close after process exit"))?;

    Ok(CmdResult {
        exit_code: status.code(),
    })
}

/// Resume only the authenticated fleet plan emitted by the contained update
/// child. This command deliberately runs outside the mutation Job: a trusted
/// gateway that survives readiness proof must also survive this updater. The
/// command receives only correlated IDs and the canonical root, never captured
/// argv. Failure cancellation is scoped to the exact process we spawned.
async fn run_deferred_gateway_resume(
    app: &AppHandle,
    program: &Path,
    args: &[String],
    cwd: &Path,
    envs: &[(String, OsString)],
    invocation_id: &str,
    install_root: &Path,
    bridge_lease: &mut BridgeQuiesceLeaseGuard,
    timeout: Duration,
) -> Result<CmdResult> {
    let hermes_home = bridge_lease
        .path
        .parent()
        .map(Path::to_path_buf)
        .ok_or_else(|| anyhow!("the bridge-quiesce lease has no global Hermes home"))?;
    let initial_plan = deferred_gateway_plan_proof(&hermes_home, invocation_id, install_root)?;
    bridge_lease.refresh()?;
    let mut cmd = Command::new(program);
    cmd.args(args)
        .current_dir(cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    for (key, value) in envs {
        cmd.env(key, value);
    }
    #[cfg(target_os = "windows")]
    {
        // CREATE_NO_WINDOW. Do not assign a Job Object and do not grant a
        // breakaway limit: the hidden Python command validates the private
        // fleet plan, proves readiness, and intentionally leaves that fleet
        // outside the already-drained mutation Job.
        cmd.creation_flags(0x0800_0000);
    }

    let mut child = cmd
        .spawn()
        .map_err(|err| anyhow!("spawning the trusted deferred gateway resume command: {err}"))?;
    let child_pid = child.id().ok_or_else(|| {
        anyhow!("the gateway-resume child exited before its PID could be verified")
    })?;
    #[cfg(windows)]
    let child_creation_ticks = match child_process_creation_ticks(&child) {
        Ok(value) => value,
        Err(err) => {
            terminate_exact_uncontained_tree(&mut child, child_pid).await;
            return Err(err);
        }
    };
    let stdout = child.stdout.take().expect("stdout piped");
    let stderr = child.stderr.take().expect("stderr piped");
    let mut out = BufReader::new(stdout);
    let mut err = BufReader::new(stderr);
    let mut out_buf = Vec::new();
    let mut err_buf = Vec::new();
    let mut stdout_done = false;
    let mut stderr_done = false;
    let mut lease_poll = tokio::time::interval(Duration::from_millis(10));
    lease_poll.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let mut last_lease_refresh = Instant::now();
    let mut missing_since: Option<Instant> = None;
    let mut unreadable_since: Option<Instant> = None;
    let mut adoption_frame_seen = false;
    let mut completed_replay_selected = false;
    let mut pre_frame_stderr: Vec<String> = Vec::new();
    let mut pre_frame_stderr_bytes = 0usize;
    let mut wait = Box::pin(child.wait());
    let deadline = tokio::time::sleep(timeout);
    tokio::pin!(deadline);
    let mut terminal_error: Option<anyhow::Error> = None;

    let status = loop {
        tokio::select! {
            _ = &mut deadline => {
                terminal_error = Some(anyhow!(
                    "the trusted gateway-resume stage exceeded its bounded {:?} deadline",
                    timeout
                ));
                break None;
            }
            status = &mut wait => match status {
                Ok(status) => break Some(status),
                Err(err) => {
                    terminal_error = Some(anyhow!("waiting for gateway-resume child: {err}"));
                    break None;
                }
            },
            line = read_decoded_line(&mut out, &mut out_buf), if !stdout_done => match line {
                Ok(Some(line)) => {
                    let was_seen = adoption_frame_seen;
                    if let Err(frame_err) = observe_deferred_gateway_stdout_line(
                        &line,
                        invocation_id,
                        child_pid,
                        initial_plan.started_completed,
                        &mut adoption_frame_seen,
                        &mut completed_replay_selected,
                    ) {
                        terminal_error = Some(frame_err);
                        break None;
                    }
                    if !was_seen && adoption_frame_seen {
                        // The exact trusted child emits this frame immediately
                        // after its atomic lease claim. It is durable proof even
                        // if a fast no-op recovery clears the lease before our
                        // next marker poll.
                        bridge_lease.transferred_to = Some(child_pid);
                        bridge_lease.owned = false;
                        for buffered in pre_frame_stderr.drain(..) {
                            emit_log(app, Some("update"), LogStream::Stderr, &buffered);
                        }
                    }
                    if !(!was_seen && adoption_frame_seen) {
                        emit_log(app, Some("update"), LogStream::Stdout, &line);
                    }
                }
                Ok(None) => stdout_done = true,
                Err(read_err) => {
                    tracing::warn!("gateway-resume stdout read error: {read_err}");
                    stdout_done = true;
                }
            },
            line = read_decoded_line(&mut err, &mut err_buf), if !stderr_done => match line {
                Ok(Some(line)) => {
                    match buffer_deferred_gateway_stderr_before_adoption(
                        &line,
                        initial_plan.started_completed,
                        adoption_frame_seen,
                        &mut pre_frame_stderr,
                        &mut pre_frame_stderr_bytes,
                    ) {
                        Ok(true) => {}
                        Ok(false) => {
                            emit_log(app, Some("update"), LogStream::Stderr, &line);
                        }
                        Err(buffer_err) => {
                            terminal_error = Some(buffer_err);
                            break None;
                        }
                    }
                }
                Ok(None) => stderr_done = true,
                Err(read_err) => {
                    tracing::warn!("gateway-resume stderr read error: {read_err}");
                    stderr_done = true;
                }
            },
            _ = lease_poll.tick() => {
                match bridge_lease.observe_child_transfer(child_pid, |_| Ok(false)) {
                    Ok(ChildLeaseObservation::ChildOwned) => {
                        missing_since = None;
                        unreadable_since = None;
                    }
                    Ok(ChildLeaseObservation::ParentReturned) => {
                        missing_since = None;
                        unreadable_since = None;
                    }
                    Ok(ChildLeaseObservation::ParentOwned) => {
                        missing_since = None;
                        unreadable_since = None;
                        if last_lease_refresh.elapsed()
                            >= Duration::from_secs(BRIDGE_LEASE_REFRESH_SECONDS)
                        {
                            if let Err(refresh_err) = bridge_lease.refresh() {
                                terminal_error = Some(anyhow!(
                                    "bridge-quiesce lease lost before gateway recovery adopted it: {refresh_err}"
                                ));
                                break None;
                            }
                            last_lease_refresh = Instant::now();
                        }
                    }
                    Err(observe_err) => {
                        // Successful recovery clears its exact lease immediately
                        // before exit. Permit only that tiny already-transferred
                        // edge; a live child without a lease is unauthorized.
                        if bridge_lease.transferred_to.is_some() && !bridge_lease.path.exists() {
                            let missing = missing_since.get_or_insert_with(Instant::now);
                            if missing.elapsed() < Duration::from_millis(750) {
                                continue;
                            }
                        }
                        let unreadable = unreadable_since.get_or_insert_with(Instant::now);
                        if unreadable.elapsed() < Duration::from_millis(750) {
                            continue;
                        }
                        terminal_error = Some(anyhow!(
                            "bridge-quiesce lease lost during trusted gateway recovery: {observe_err}"
                        ));
                        break None;
                    }
                }
            }
        }
    };
    drop(wait);

    if let Some(run_err) = terminal_error {
        terminate_exact_uncontained_tree(&mut child, child_pid).await;
        reconcile_resume_lease_after_child_exit(bridge_lease, child_pid).map_err(|recover_err| {
            anyhow!(
                "{run_err}; gateway-resume lease state could not be reconciled after cancellation: {recover_err}"
            )
        })?;
        return Err(run_err);
    }
    let status = status.expect("gateway-resume completion produces a status");

    let drain = async {
        loop {
            if stdout_done && stderr_done {
                return Ok::<(), anyhow::Error>(());
            }
            tokio::select! {
                line = read_decoded_line(&mut out, &mut out_buf), if !stdout_done => match line {
                    Ok(Some(line)) => {
                        let was_seen = adoption_frame_seen;
                        observe_deferred_gateway_stdout_line(
                            &line,
                            invocation_id,
                            child_pid,
                            initial_plan.started_completed,
                            &mut adoption_frame_seen,
                            &mut completed_replay_selected,
                        )?;
                        if !was_seen && adoption_frame_seen {
                            bridge_lease.transferred_to = Some(child_pid);
                            bridge_lease.owned = false;
                            for buffered in pre_frame_stderr.drain(..) {
                                emit_log(app, Some("update"), LogStream::Stderr, &buffered);
                            }
                        }
                        if !(!was_seen && adoption_frame_seen) {
                            emit_log(app, Some("update"), LogStream::Stdout, &line);
                        }
                    }
                    Ok(None) => stdout_done = true,
                    Err(read_err) => return Err(anyhow!("reading gateway-resume stdout: {read_err}")),
                },
                line = read_decoded_line(&mut err, &mut err_buf), if !stderr_done => match line {
                    Ok(Some(line)) => {
                        if !buffer_deferred_gateway_stderr_before_adoption(
                            &line,
                            initial_plan.started_completed,
                            adoption_frame_seen,
                            &mut pre_frame_stderr,
                            &mut pre_frame_stderr_bytes,
                        )? {
                            emit_log(app, Some("update"), LogStream::Stderr, &line);
                        }
                    }
                    Ok(None) => stderr_done = true,
                    Err(read_err) => return Err(anyhow!("reading gateway-resume stderr: {read_err}")),
                },
            }
        }
    };
    let drain_result = match tokio::time::timeout(CHILD_PIPE_DRAIN_TIMEOUT, drain).await {
        Ok(result) => result,
        Err(_) => Err(anyhow!(
            "the trusted gateway-resume pipes did not close after process exit"
        )),
    };
    if let Err(drain_err) = drain_result {
        reconcile_resume_lease_after_child_exit(bridge_lease, child_pid).map_err(|recover_err| {
            anyhow!(
                "{drain_err}; gateway-resume lease state could not be reconciled after pipe failure: {recover_err}"
            )
        })?;
        return Err(drain_err);
    }

    // `wait()` completed the same owned process handle whose creation identity
    // was captured before any output was trusted. Tokio intentionally stops
    // exposing `raw_handle()` after completion, so PID + frame provenance bind
    // to that captured handle without a post-exit numeric-PID reopen race.
    #[cfg(windows)]
    let _exact_child_creation_ticks = child_creation_ticks;

    let terminal_proof = prove_deferred_gateway_terminal(
        status.success(),
        adoption_frame_seen,
        completed_replay_selected,
        &initial_plan,
        &hermes_home,
        invocation_id,
        install_root,
        bridge_lease,
    );
    if let Err(proof_err) = terminal_proof {
        reconcile_resume_lease_after_child_exit(bridge_lease, child_pid).map_err(|recover_err| {
            anyhow!(
                "{proof_err}; gateway-resume lease state could not be reconciled after terminal proof failure: {recover_err}"
            )
        })?;
        return Err(proof_err);
    }

    Ok(CmdResult {
        exit_code: status.code(),
    })
}

fn reconcile_resume_lease_after_child_exit(
    bridge_lease: &mut BridgeQuiesceLeaseGuard,
    child_pid: u32,
) -> Result<()> {
    if bridge_lease.path.exists() {
        match bridge_lease.observe_child_transfer(child_pid, |_| Ok(false))? {
            ChildLeaseObservation::ChildOwned => bridge_lease.recover_after_terminated_child(),
            ChildLeaseObservation::ParentOwned | ChildLeaseObservation::ParentReturned => {
                bridge_lease.refresh()
            }
        }
    } else if has_recovery_artifacts(&bridge_lease.path)? {
        Err(anyhow!(
            "a bridge-quiesce CAS recovery artifact remains after the exact child exited"
        ))
    } else {
        bridge_lease.owned = false;
        Ok(())
    }
}

fn prove_deferred_gateway_terminal(
    status_success: bool,
    adoption_frame_seen: bool,
    completed_replay_selected: bool,
    initial_plan: &DeferredGatewayPlanProof,
    hermes_home: &Path,
    invocation_id: &str,
    install_root: &Path,
    bridge_lease: &mut BridgeQuiesceLeaseGuard,
) -> Result<()> {
    if status_success {
        let completed_replay_proved = initial_plan.started_completed
            && completed_replay_selected
            && bridge_lease.transferred_to.is_none();
        if !adoption_frame_seen && !completed_replay_proved {
            return Err(anyhow!(
                "the gateway-resume child exited successfully without an exact adoption or completed-replay frame"
            ));
        }
        deferred_gateway_plan_consumed(hermes_home, invocation_id, install_root, initial_plan)?;
        if adoption_frame_seen {
            bridge_lease.require_child_cleanup()
        } else if bridge_lease.path.exists() || has_recovery_artifacts(&bridge_lease.path)? {
            Err(anyhow!(
                "the completed gateway-resume replay left bridge-quiesce state behind"
            ))
        } else {
            bridge_lease.owned = false;
            Ok(())
        }
    } else if bridge_lease.transferred_to.is_some() {
        bridge_lease.require_parent_return()
    } else {
        // A refusal before adoption is safe only while the exact parent claim
        // is still present and refreshable.
        bridge_lease.refresh()
    }
}

fn buffer_deferred_gateway_stderr_before_adoption(
    line: &str,
    started_completed: bool,
    adoption_frame_seen: bool,
    buffered: &mut Vec<String>,
    buffered_bytes: &mut usize,
) -> Result<bool> {
    if started_completed || adoption_frame_seen {
        return Ok(false);
    }
    *buffered_bytes = buffered_bytes.saturating_add(line.len());
    if *buffered_bytes > 64 * 1024 {
        return Err(anyhow!(
            "the gateway-resume child exceeded the bounded pre-adoption stderr buffer"
        ));
    }
    buffered.push(line.to_string());
    Ok(true)
}

fn observe_deferred_gateway_stdout_line(
    line: &str,
    expected_invocation_id: &str,
    expected_owner_pid: u32,
    started_completed: bool,
    adoption_frame_seen: &mut bool,
    completed_replay_selected: &mut bool,
) -> Result<()> {
    if !*adoption_frame_seen {
        if *completed_replay_selected {
            if serde_json::from_str::<DeferredGatewayAdoptionFrame>(line).is_ok()
                || line.contains("deferred-gateway-lease-adopted")
            {
                return Err(anyhow!(
                    "the completed gateway replay emitted a late adoption frame"
                ));
            }
            return Ok(());
        }
        if started_completed {
            if serde_json::from_str::<DeferredGatewayAdoptionFrame>(line).is_ok() {
                validate_deferred_gateway_adoption_frame(
                    line,
                    expected_invocation_id,
                    expected_owner_pid,
                )?;
                *adoption_frame_seen = true;
                return Ok(());
            }
            if line.contains("deferred-gateway-lease-adopted") {
                return Err(anyhow!(
                    "the completed gateway replay emitted a malformed adoption frame"
                ));
            }
            // A completed replay with no active lease emits only its ordinary
            // already-complete status. Terminal exact-plan and lease-absence
            // proofs below are authoritative for this one no-frame case.
            if line != "✓ Deferred gateway fleet was already resumed." {
                return Err(anyhow!(
                    "the completed gateway replay emitted an unexpected first line"
                ));
            }
            *completed_replay_selected = true;
            return Ok(());
        }
        validate_deferred_gateway_adoption_frame(line, expected_invocation_id, expected_owner_pid)?;
        *adoption_frame_seen = true;
        return Ok(());
    }

    if serde_json::from_str::<DeferredGatewayAdoptionFrame>(line).is_ok()
        || line.contains("deferred-gateway-lease-adopted")
    {
        return Err(anyhow!(
            "the gateway-resume child emitted a duplicate or malformed late adoption frame"
        ));
    }
    Ok(())
}

async fn terminate_exact_uncontained_tree(child: &mut tokio::process::Child, child_pid: u32) {
    #[cfg(target_os = "windows")]
    if matches!(child.try_wait(), Ok(None)) {
        // `child_pid` came from the still-live process handle above. /T is
        // scoped to that exact owned tree; never match an image name or an
        // arbitrary Python/Codex process.
        let mut taskkill = Command::new("taskkill.exe");
        taskkill.args(["/PID", &child_pid.to_string(), "/T", "/F"]);
        taskkill.creation_flags(0x0800_0000);
        let _ = tokio::time::timeout(Duration::from_secs(5), taskkill.status()).await;
    }
    let _ = child.kill().await;
    let _ = tokio::time::timeout(Duration::from_secs(5), child.wait()).await;
}

struct CmdResult {
    exit_code: Option<i32>,
}

/// Path to the venv hermes shim under an install root, regardless of existence.
fn venv_hermes(install_root: &Path) -> PathBuf {
    if cfg!(target_os = "windows") {
        install_root.join("venv").join("Scripts").join("hermes.exe")
    } else {
        install_root.join("venv").join("bin").join("hermes")
    }
}

/// Resolve the managed interpreter directly. Windows console-script shims
/// spawn a second Python process, so their PID cannot be the lease owner and
/// their pre-job child can escape cancellation. `python -m hermes_cli.main`
/// keeps the updater identity on the exact contained process.
fn resolve_update_python(install_root: &Path) -> Option<PathBuf> {
    let python = if cfg!(target_os = "windows") {
        install_root.join("venv").join("Scripts").join("python.exe")
    } else {
        install_root.join("venv").join("bin").join("python")
    };
    python.exists().then_some(python)
}

fn update_child_env(install_root: &Path) -> Vec<(String, OsString)> {
    let hermes_home = install_root
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(install_global_hermes_home);
    let mut envs = vec![(
        "HERMES_HOME".to_string(),
        hermes_home.as_os_str().to_os_string(),
    )];
    // `hermes update` is a Python CLI writing to a pipe here, so CPython
    // block-buffers its stdout: nothing reaches run_streamed (and the live
    // log UI) until 8 KB accumulate or the process exits. Long quiet steps —
    // the pre-update backup can zip multi-GB archives for minutes — render as
    // a frozen stage, and users cancel a healthy update. Force line-by-line
    // output instead.
    envs.push(("PYTHONUNBUFFERED".to_string(), OsString::from("1")));
    // We hold the update-in-progress marker for this whole run, and the
    // `hermes update` child claims that SAME lock (hermes_cli/update_lock.py).
    // Name our pid so the child recognizes the live holder as its own
    // orchestrator and runs under our claim — without this every GUI update
    // refuses its parent's marker with exit 2 ("Hermes is still running")
    // and no number of retries can ever succeed. Keep the variable name in
    // sync with HANDOFF_PID_ENV in hermes_cli/update_lock.py.
    envs.push((
        "HERMES_UPDATE_HANDOFF_PID".to_string(),
        OsString::from(std::process::id().to_string()),
    ));
    if let Some(path) = path_with_prepended_entries(&[
        hermes_home.join("node").join("bin"),
        venv_bin_dir(install_root),
    ]) {
        envs.push(("PATH".to_string(), path));
    }
    envs
}

/// Resolve process-global update coordination state even if a caller leaked a
/// profile-scoped `HERMES_HOME=<root>/profiles/<name>` into the updater.
/// Profiles own conversation data, never the managed checkout or its locks.
fn install_global_hermes_home() -> PathBuf {
    let configured = crate::paths::hermes_home();
    let global = global_hermes_home_from(&configured);
    global.canonicalize().unwrap_or(global)
}

fn global_hermes_home_from(configured: &Path) -> PathBuf {
    let is_profile_home = configured
        .parent()
        .and_then(Path::file_name)
        .map(|name| name.to_string_lossy().eq_ignore_ascii_case("profiles"))
        .unwrap_or(false);
    let global = if is_profile_home {
        configured
            .parent()
            .and_then(Path::parent)
            .map(Path::to_path_buf)
            .unwrap_or_else(|| configured.to_path_buf())
    } else {
        configured.to_path_buf()
    };
    global
}

fn venv_bin_dir(install_root: &Path) -> PathBuf {
    if cfg!(target_os = "windows") {
        install_root.join("venv").join("Scripts")
    } else {
        install_root.join("venv").join("bin")
    }
}

fn path_with_prepended_entries(entries: &[PathBuf]) -> Option<OsString> {
    let mut parts: Vec<PathBuf> = entries.to_vec();
    if let Some(existing) = env::var_os("PATH") {
        parts.extend(env::split_paths(&existing));
    }
    env::join_paths(parts).ok()
}

fn update_branch_from_args<I, S>(args: I) -> Option<String>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    arg_value_from_args(args, "--branch")
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
}

fn bridge_lease_id_from_args<I, S>(args: I) -> Option<String>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    bridge_lease_id_from_sources(args, env::var(BRIDGE_LEASE_ID_ENV).ok())
}

fn bridge_lease_id_from_sources<I, S>(args: I, env_value: Option<String>) -> Option<String>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    arg_value_from_args(args, "--bridge-lease-id")
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        // Private environment transport lets new staged binaries prove the
        // capability without sending an unknown argument to months-stale
        // staged binaries. Old binaries ignore this variable safely.
        .or_else(|| {
            env_value
                .map(|value| value.trim().to_string())
                .filter(|value| !value.is_empty())
        })
}

fn target_app_from_args<I, S>(args: I) -> Option<PathBuf>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    arg_value_from_args(args, "--target-app")
        .map(PathBuf::from)
        .filter(|p| p.extension().and_then(|e| e.to_str()) == Some("app"))
}

fn arg_value_from_args<I, S>(args: I, name: &str) -> Option<String>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    let mut iter = args.into_iter().map(|s| s.as_ref().to_string()).peekable();
    while let Some(arg) = iter.next() {
        if arg == name {
            return iter.next();
        }
        if let Some(value) = arg.strip_prefix(&format!("{name}=")) {
            return Some(value.to_string());
        }
    }
    None
}

#[cfg(target_os = "macos")]
async fn install_macos_app_update(
    app: &AppHandle,
    install_root: &Path,
    target_app: &Path,
) -> Result<PathBuf> {
    if target_app.extension().and_then(|e| e.to_str()) != Some("app") {
        return Err(anyhow!(
            "refusing to install update into non-app path: {}",
            target_app.display()
        ));
    }

    let rebuilt_app =
        crate::bootstrap::resolve_hermes_desktop_app(install_root).ok_or_else(|| {
            anyhow!(
                "desktop rebuild succeeded but no Hermes.app was found under {}",
                install_root
                    .join("apps")
                    .join("desktop")
                    .join("release")
                    .display()
            )
        })?;

    let same = match (rebuilt_app.canonicalize(), target_app.canonicalize()) {
        (Ok(a), Ok(b)) => a == b,
        _ => rebuilt_app == target_app,
    };
    if same {
        emit_log(
            app,
            Some("install"),
            LogStream::Stdout,
            &format!(
                "[update] rebuilt app is already the launch target: {}",
                target_app.display()
            ),
        );
        return Ok(target_app.to_path_buf());
    }

    emit_log(
        app,
        Some("install"),
        LogStream::Stdout,
        &format!(
            "[update] installing rebuilt app {} -> {}",
            rebuilt_app.display(),
            target_app.display()
        ),
    );

    if let Some(parent) = target_app.parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    let tmp = PathBuf::from(format!("{}.hermes-update-new", target_app.display()));
    let old = PathBuf::from(format!("{}.hermes-update-old", target_app.display()));
    remove_dir_if_exists(&tmp).await;
    remove_dir_if_exists(&old).await;

    let ditto = Command::new("/usr/bin/ditto")
        .arg(&rebuilt_app)
        .arg(&tmp)
        .current_dir(crate::paths::hermes_home())
        .status()
        .await
        .map_err(|e| anyhow!("running ditto: {e}"))?;
    if !ditto.success() {
        return Err(anyhow!(
            "ditto failed while copying updated app into {}",
            tmp.display()
        ));
    }

    // Atomic-as-possible swap with rollback. Extracted so the invariant
    // (target is never left deleted-with-no-replacement) can be unit-tested
    // without ditto / a real .app bundle.
    swap_in_new_bundle(&tmp, target_app, &old).await?;

    let _ = Command::new("/usr/bin/xattr")
        .arg("-dr")
        .arg("com.apple.quarantine")
        .arg(target_app)
        .current_dir(crate::paths::hermes_home())
        .status()
        .await;

    Ok(target_app.to_path_buf())
}

/// Move a freshly-staged bundle (`tmp`) into place at `target`, parking any
/// existing bundle at `old` so the move can succeed (macOS `rename` won't
/// overwrite a non-empty directory).
///
/// Invariant: on ANY failure path, `target` is left pointing at a working
/// bundle — either the original (rolled back from `old`) or untouched — and we
/// never delete the running app with no replacement in place. The staged `tmp`
/// copy is cleaned up on failure.
async fn swap_in_new_bundle(tmp: &Path, target: &Path, old: &Path) -> Result<()> {
    let moved_old = if target.exists() {
        if let Err(err) = tokio::fs::rename(target, old).await {
            // Could not move the existing app aside. Leave it untouched and
            // bail — a failed update must not brick the install.
            remove_dir_if_exists(tmp).await;
            return Err(anyhow!(
                "could not move existing app aside at {} (leaving it in place): {err}",
                target.display()
            ));
        }
        true
    } else {
        false
    };
    if let Err(err) = tokio::fs::rename(tmp, target).await {
        // Restore the original app from the backup so the user keeps a working
        // install, and clean up the staged copy.
        if moved_old {
            let _ = tokio::fs::rename(old, target).await;
        }
        remove_dir_if_exists(tmp).await;
        return Err(anyhow!(
            "installing updated app at {}: {err}",
            target.display()
        ));
    }
    remove_dir_if_exists(old).await;
    Ok(())
}

#[cfg(not(target_os = "macos"))]
async fn install_macos_app_update(
    _app: &AppHandle,
    _install_root: &Path,
    target_app: &Path,
) -> Result<PathBuf> {
    Ok(target_app.to_path_buf())
}

async fn remove_dir_if_exists(path: &Path) {
    if path.exists() {
        let _ = tokio::fs::remove_dir_all(path).await;
    }
}

#[cfg(target_os = "macos")]
async fn launch_macos_app_and_exit(app: &AppHandle, target_app: &Path) -> Result<()> {
    crate::bootstrap::open_macos_app_detached(target_app)
        .map_err(|e| anyhow!("launching {}: {e}", target_app.display()))?;
    tokio::time::sleep(std::time::Duration::from_millis(150)).await;
    app.exit(0);
    Ok(())
}

#[cfg(not(target_os = "macos"))]
async fn launch_macos_app_and_exit(_app: &AppHandle, _target_app: &Path) -> Result<()> {
    Ok(())
}

#[cfg(target_os = "windows")]
fn filetime_ticks_to_unix_seconds(ticks: u64) -> Result<u64> {
    const WINDOWS_TO_UNIX_SECONDS: u64 = 11_644_473_600;
    let seconds = ticks / 10_000_000;
    seconds
        .checked_sub(WINDOWS_TO_UNIX_SECONDS)
        .ok_or_else(|| anyhow!("the Desktop process creation time predates the Unix epoch"))
}

fn expected_receipt_build_id(receipt: &UpdateReceipt) -> Result<&str> {
    match receipt.mode.as_str() {
        "git" => receipt
            .resulting_head
            .as_deref()
            .ok_or_else(|| anyhow!("the verified git receipt has no resulting build identity")),
        "archive" => receipt
            .archive_sha
            .as_deref()
            .ok_or_else(|| anyhow!("the verified archive receipt has no build identity")),
        _ => Err(anyhow!("the verified receipt mode is unsupported")),
    }
}

fn receipt_build_identity_env(receipt: &UpdateReceipt) -> Result<Vec<(String, OsString)>> {
    Ok(vec![
        (
            "GITHUB_SHA".to_string(),
            OsString::from(expected_receipt_build_id(receipt)?),
        ),
        (
            "GITHUB_REF_NAME".to_string(),
            OsString::from(receipt.branch.clone()),
        ),
    ])
}

#[cfg(target_os = "windows")]
fn windows_handoff_result_raw(
    state: &str,
    exit_code: Option<i32>,
    message: &str,
    attempt_id: &str,
    install_root: &Path,
    receipt: &UpdateReceipt,
    relaunch: &WindowsDesktopRelaunchIdentity,
    desktop_ack: Option<&DesktopHandoffAck>,
) -> Result<Vec<u8>> {
    if !matches!(state, "pending" | "complete" | "failed") {
        return Err(anyhow!("the Desktop handoff result state is invalid"));
    }
    let complete = state == "complete";
    let pending = state == "pending";
    if (pending && exit_code.is_some()) || (!pending && exit_code.is_none()) {
        return Err(anyhow!(
            "the Desktop handoff result exit code is incoherent"
        ));
    }
    if complete != desktop_ack.is_some() {
        return Err(anyhow!(
            "the Desktop handoff result readiness proof is incoherent"
        ));
    }
    let canonical_root = install_root
        .canonicalize()
        .map_err(|err| anyhow!("canonicalizing the Desktop handoff root: {err}"))?;
    let canonical_executable = relaunch
        .executable
        .canonicalize()
        .map_err(|err| anyhow!("canonicalizing the Desktop relaunch executable: {err}"))?;
    let build_id = expected_receipt_build_id(receipt)?;
    let desktop = if let Some(ack) = desktop_ack {
        serde_json::json!({
            "build_id": build_id,
            "build_source": "install-stamp",
            "root": canonical_root.to_string_lossy(),
            "backend_ready": ack.backend_ready,
            "backend_mode": ack.backend_mode,
        })
    } else {
        serde_json::json!({
            "build_id": null,
            "build_source": null,
            "root": null,
            "backend_ready": false,
            "backend_mode": null,
        })
    };
    let relaunch_state = match state {
        "pending" => "pending",
        "complete" => "acknowledged",
        _ => "failed",
    };
    let finished_at = if pending {
        None
    } else {
        Some(
            unix_time_seconds().max(
                desktop_ack
                    .map(|ack| ack.acknowledged_at)
                    .unwrap_or(relaunch.requested_at),
            ),
        )
    };
    let result = serde_json::json!({
        "schema_version": 2,
        "attempt_id": attempt_id,
        "state": state,
        "ok": complete,
        "exit_code": exit_code,
        "message": message,
        "branch": receipt.branch,
        "invocation_id": receipt.invocation_id,
        "lease_id": receipt.lease_id,
        "root": canonical_root.to_string_lossy(),
        "receipt": receipt,
        "cleanup": {
            "update_marker_released": true,
            "bridge_lease_released": true,
        },
        "runtime_health": receipt.health,
        "relaunch": {
            "state": relaunch_state,
            "pid": relaunch.pid,
            "process_started_at": relaunch.process_started_at,
            "executable": canonical_executable.to_string_lossy(),
            "requested_at": relaunch.requested_at,
            "acknowledged_at": desktop_ack.map(|ack| ack.acknowledged_at),
        },
        "desktop": desktop,
        "finished_at": finished_at,
    });
    serde_json::to_vec(&result).map_err(Into::into)
}

#[cfg(target_os = "windows")]
fn publish_windows_handoff_result(
    path: &Path,
    expected_raw: Option<&[u8]>,
    raw: &[u8],
) -> Result<()> {
    ensure_no_recovery_artifacts(path)?;
    publish_update_marker_atomically(path, expected_raw, raw)
        .map_err(|err| anyhow!("publishing the exact Desktop handoff result: {err}"))
}

#[cfg(target_os = "windows")]
fn ensure_windows_handoff_result_slot_clear(hermes_home: &Path) -> Result<()> {
    let result_path = hermes_home.join(".hermes-update-result.json");
    let result_absent = matches!(
        std::fs::symlink_metadata(&result_path),
        Err(err) if err.kind() == std::io::ErrorKind::NotFound
    );
    if !result_absent {
        return Err(anyhow!(
            "a prior Desktop handoff result is present or unreadable"
        ));
    }
    ensure_no_recovery_artifacts(&result_path)
}

#[cfg(target_os = "windows")]
fn validate_windows_desktop_ack(
    raw: &[u8],
    attempt_id: &str,
    install_root: &Path,
    receipt: &UpdateReceipt,
    relaunch: &WindowsDesktopRelaunchIdentity,
    now: u64,
) -> Result<DesktopHandoffAck> {
    if raw.is_empty() || raw.len() > 64 * 1024 {
        return Err(anyhow!(
            "the Desktop readiness acknowledgment has an invalid size"
        ));
    }
    let value: serde_json::Value = serde_json::from_slice(raw)
        .map_err(|_| anyhow!("the Desktop readiness acknowledgment is invalid JSON"))?;
    let object = value
        .as_object()
        .ok_or_else(|| anyhow!("the Desktop readiness acknowledgment is not one object"))?;
    const FIELDS: [&str; 14] = [
        "schema_version",
        "attempt_id",
        "invocation_id",
        "lease_id",
        "pid",
        "process_started_at",
        "root",
        "executable",
        "build_id",
        "build_source",
        "backend_ready",
        "backend_mode",
        "acknowledged_at",
        "error",
    ];
    if object.len() != FIELDS.len() || FIELDS.iter().any(|field| !object.contains_key(*field)) {
        return Err(anyhow!(
            "the Desktop readiness acknowledgment has the wrong schema"
        ));
    }
    let ack: DesktopHandoffAck = serde_json::from_value(value)
        .map_err(|_| anyhow!("the Desktop readiness acknowledgment has an invalid field type"))?;
    let expected_build = expected_receipt_build_id(receipt)?;
    if ack.schema_version != 1
        || ack.attempt_id != attempt_id
        || ack.invocation_id != receipt.invocation_id
        || ack.lease_id != receipt.lease_id
        || ack.pid != relaunch.pid
        || ack.process_started_at != relaunch.process_started_at
        || !canonical_roots_match(Path::new(&ack.root), install_root)
        || !canonical_roots_match(Path::new(&ack.executable), &relaunch.executable)
        || !ack.build_id.eq_ignore_ascii_case(expected_build)
        || ack.build_source != "install-stamp"
        || !ack.backend_ready
        || !matches!(ack.backend_mode.as_str(), "local" | "remote")
        || ack.acknowledged_at < relaunch.requested_at
        || ack.acknowledged_at > now.saturating_add(30)
        || !ack.error.is_null()
    {
        return Err(anyhow!(
            "the Desktop readiness acknowledgment is mismatched or unhealthy"
        ));
    }
    Ok(ack)
}

#[cfg(target_os = "windows")]
fn remove_exact_handoff_ack(path: &Path, expected_raw: &[u8]) -> Result<()> {
    remove_exact_handoff_ack_with(path, expected_raw, |target| std::fs::remove_file(target))
}

#[cfg(target_os = "windows")]
fn remove_exact_handoff_ack_with<F>(path: &Path, expected_raw: &[u8], remove: F) -> Result<()>
where
    F: FnOnce(&Path) -> std::io::Result<()>,
{
    let tombstone = bridge_lease_sibling(
        path,
        &format!(
            ".cas-release-{}-{}",
            std::process::id(),
            uuid::Uuid::new_v4()
        ),
    )?;
    std::fs::rename(path, &tombstone)
        .map_err(|err| anyhow!("isolating the Desktop readiness acknowledgment: {err}"))?;
    let isolated_raw = std::fs::read(&tombstone)?;
    if isolated_raw != expected_raw {
        if !path.exists() {
            let _ = std::fs::hard_link(&tombstone, path);
        }
        return Err(anyhow!(
            "the Desktop readiness acknowledgment changed during exact cleanup"
        ));
    }
    remove(&tombstone).map_err(|err| {
        anyhow!(
            "retiring the exact Desktop readiness acknowledgment {}: {err}",
            tombstone.display()
        )
    })
}

#[cfg(target_os = "windows")]
async fn wait_for_windows_desktop_ack(
    child: &mut tokio::process::Child,
    child_creation_ticks: u64,
    ack_path: &Path,
    attempt_id: &str,
    install_root: &Path,
    receipt: &UpdateReceipt,
    relaunch: &WindowsDesktopRelaunchIdentity,
    timeout: Duration,
) -> Result<(DesktopHandoffAck, Vec<u8>)> {
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                return Err(anyhow!(
                    "the relaunched Desktop exited before readiness acknowledgment ({status})"
                ));
            }
            Ok(None) => {}
            Err(err) => return Err(anyhow!("checking the relaunched Desktop state: {err}")),
        }
        let rechecked_ticks = child_process_creation_ticks(child)?;
        if rechecked_ticks != child_creation_ticks {
            return Err(anyhow!(
                "the relaunched Desktop process creation identity changed"
            ));
        }
        ensure_no_recovery_artifacts(ack_path)?;
        if ack_path.exists() {
            let raw = std::fs::read(ack_path)
                .map_err(|err| anyhow!("reading the Desktop readiness acknowledgment: {err}"))?;
            let ack = validate_windows_desktop_ack(
                &raw,
                attempt_id,
                install_root,
                receipt,
                relaunch,
                unix_time_seconds(),
            )?;
            return Ok((ack, raw));
        }
        if Instant::now() >= deadline {
            return Err(anyhow!(
                "the relaunched Desktop did not acknowledge backend readiness within the bounded window"
            ));
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
}

#[cfg(target_os = "windows")]
async fn launch_windows_desktop_with_readiness_proof(
    hermes_home: &Path,
    install_root: &Path,
    receipt: &UpdateReceipt,
) -> Result<()> {
    // This direct staged-binary fallback deliberately does not signal or kill
    // a separately opened Desktop that may own Electron's single-instance
    // lock. The normal repo-script path performs that richer survivor handoff.
    // Here, the exact spawned PID must itself acknowledge readiness; a
    // redirected/early-exit launch fails truthfully and asks the user to close
    // the survivor and retry. It can never produce BootstrapEvent::Complete.
    let exe = crate::bootstrap::resolve_hermes_desktop_exe(install_root).ok_or_else(|| {
        anyhow!(
            "could not find the rebuilt Hermes desktop under {}",
            install_root
                .join("apps")
                .join("desktop")
                .join("release")
                .display()
        )
    })?;
    let exe = exe
        .canonicalize()
        .map_err(|err| anyhow!("canonicalizing the rebuilt Desktop executable: {err}"))?;
    let attempt_id = format!("attempt-{}", uuid::Uuid::new_v4().simple());
    let requested_at = unix_time_seconds();
    let mut command = Command::new(&exe);
    command
        .current_dir(install_root)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    // DETACHED_PROCESS: the Desktop must outlive this updater.
    command.creation_flags(0x0000_0008);
    let mut child = command
        .spawn()
        .map_err(|err| anyhow!("launching {}: {err}", exe.display()))?;
    let child_pid = child
        .id()
        .ok_or_else(|| anyhow!("the relaunched Desktop exited before its PID could be captured"))?;
    let child_creation_ticks = match child_process_creation_ticks(&child) {
        Ok(value) => value,
        Err(err) => {
            let _ = child.kill().await;
            let _ = child.wait().await;
            return Err(err);
        }
    };
    let relaunch = WindowsDesktopRelaunchIdentity {
        pid: child_pid,
        process_started_at: filetime_ticks_to_unix_seconds(child_creation_ticks)?,
        executable: exe,
        requested_at,
    };
    let result_path = hermes_home.join(".hermes-update-result.json");
    let ack_path = hermes_home.join(format!(".hermes-update-ack-{attempt_id}.json"));
    let pending_raw = windows_handoff_result_raw(
        "pending",
        None,
        "Update mutation and cleanup are verified; waiting for Desktop readiness acknowledgment.",
        &attempt_id,
        install_root,
        receipt,
        &relaunch,
        None,
    )?;
    if let Err(err) = publish_windows_handoff_result(&result_path, None, &pending_raw) {
        let _ = child.kill().await;
        let _ = child.wait().await;
        return Err(err);
    }

    let readiness = wait_for_windows_desktop_ack(
        &mut child,
        child_creation_ticks,
        &ack_path,
        &attempt_id,
        install_root,
        receipt,
        &relaunch,
        DESKTOP_RELAUNCH_ACK_TIMEOUT,
    )
    .await;
    let (ack, ack_raw) = match readiness {
        Ok(proof) => proof,
        Err(readiness_err) => {
            let _ = child.kill().await;
            let _ = child.wait().await;
            let failed_raw = windows_handoff_result_raw(
                "failed",
                Some(12),
                &format!(
                    "The relaunched Desktop did not prove readiness: {readiness_err}. Close any surviving Hermes Desktop instance and retry the update."
                ),
                &attempt_id,
                install_root,
                receipt,
                &relaunch,
                None,
            )?;
            publish_windows_handoff_result(&result_path, Some(&pending_raw), &failed_raw).map_err(
                |publish_err| {
                    anyhow!(
                        "{readiness_err}; the pending Desktop result could not be terminalized: {publish_err}"
                    )
                },
            )?;
            return Err(readiness_err);
        }
    };
    if let Err(cleanup_err) = remove_exact_handoff_ack(&ack_path, &ack_raw) {
        let failed_raw = windows_handoff_result_raw(
            "failed",
            Some(11),
            &format!("Desktop became ready, but its acknowledgment could not be retired exactly: {cleanup_err}"),
            &attempt_id,
            install_root,
            receipt,
            &relaunch,
            None,
        )?;
        publish_windows_handoff_result(&result_path, Some(&pending_raw), &failed_raw).map_err(
            |publish_err| {
                anyhow!("{cleanup_err}; failed result publication also failed: {publish_err}")
            },
        )?;
        return Err(cleanup_err);
    }
    let complete_raw = windows_handoff_result_raw(
        "complete",
        Some(0),
        "Update complete.",
        &attempt_id,
        install_root,
        receipt,
        &relaunch,
        Some(&ack),
    )?;
    publish_windows_handoff_result(&result_path, Some(&pending_raw), &complete_raw)?;
    Ok(())
}

#[cfg(not(target_os = "windows"))]
async fn launch_windows_desktop_with_readiness_proof(
    _hermes_home: &Path,
    _install_root: &Path,
    _receipt: &UpdateReceipt,
) -> Result<()> {
    Ok(())
}

// ---------------------------------------------------------------------------
// Event helpers — keep emit shape identical to bootstrap.rs so the UI is reused
// ---------------------------------------------------------------------------

fn stage_info(name: &str, title: &str) -> StageInfo {
    StageInfo {
        name: name.to_string(),
        title: title.to_string(),
        category: "update".to_string(),
        needs_user_input: false,
    }
}

/// The synthetic update manifest. Mirrors the real operations `run_update`
/// performs so the progress UI shows them as discrete steps (with the live log
/// underneath) instead of one monolithic bar. `include_install` adds the macOS
/// app-swap stage. Both the happy path and the re-entrancy guard build the
/// manifest here so the two can never drift apart.
fn update_stages(include_install: bool) -> Vec<StageInfo> {
    let mut stages = vec![
        stage_info("handoff", "Preparing to update"),
        stage_info("update", "Downloading the latest version"),
        stage_info("rebuild", "Rebuilding the desktop app"),
    ];
    if include_install {
        stages.push(stage_info("install", "Installing the update"));
    }
    stages
}

// option_env! only accepts string literals, so the build-time pins are read
// by their literal names here. Mirrors bootstrap.rs's helper of the same name
// (kept local rather than shared because option_env! can't be parameterized).
fn option_env_string(key: &str) -> Option<String> {
    let val = match key {
        "BUILD_PIN_COMMIT" => option_env!("BUILD_PIN_COMMIT"),
        "BUILD_PIN_BRANCH" => option_env!("BUILD_PIN_BRANCH"),
        _ => None,
    };
    val.map(|s| s.to_string())
}

fn emit(app: &AppHandle, event: BootstrapEvent) {
    if let Err(e) = app.emit(BootstrapEvent::CHANNEL, &event) {
        tracing::warn!(?e, "failed to emit update event");
    }
}

fn emit_stage(
    app: &AppHandle,
    name: &str,
    state: StageState,
    duration_ms: Option<u64>,
    error: Option<String>,
) {
    tracing::info!(stage = %name, ?state, ?duration_ms, ?error, "update stage");
    emit(
        app,
        BootstrapEvent::Stage {
            name: name.to_string(),
            state,
            duration_ms,
            result: None,
            error,
        },
    );
}

fn emit_log(app: &AppHandle, stage: Option<&str>, stream: LogStream, line: &str) {
    match stage {
        Some(s) => tracing::info!(target: "bootstrap.log", stage = %s, "{line}"),
        None => tracing::info!(target: "bootstrap.log", "{line}"),
    }
    emit(
        app,
        BootstrapEvent::Log {
            stage: stage.map(|s| s.to_string()),
            line: line.to_string(),
            stream,
        },
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn venv_hermes_is_under_install_root() {
        let root = Path::new("/x/hermes-agent");
        let shim = venv_hermes(root);
        assert!(shim.starts_with(root));
        assert!(shim.to_string_lossy().contains("venv"));
    }

    #[test]
    fn missing_file_is_not_locked() {
        assert!(!is_locked(Path::new("/nonexistent/does/not/exist/xyz")));
    }

    #[test]
    fn update_child_env_forces_unbuffered_python() {
        let envs = update_child_env(Path::new("/x/hermes-agent"));
        assert!(
            envs.iter()
                .any(|(k, v)| k == "PYTHONUNBUFFERED" && v.to_str() == Some("1")),
            "update children must run unbuffered so long steps stream to the live log"
        );
    }

    #[test]
    fn update_child_env_names_our_pid_for_the_lock_handoff() {
        let envs = update_child_env(Path::new("/x/hermes-agent"));
        assert!(
            envs.iter().any(|(k, v)| k == "HERMES_UPDATE_HANDOFF_PID"
                && v.to_str() == Some(std::process::id().to_string().as_str())),
            "the hermes update child claims the same marker we hold; without our pid \
             it refuses its own parent's lock and every GUI update dead-ends on exit 2"
        );
    }

    #[test]
    fn update_coordination_uses_install_global_home_not_profile_home() {
        let global = PathBuf::from("root");
        let profile = global.join("profiles").join("research");

        assert_eq!(global_hermes_home_from(&profile), global);
        assert_eq!(
            global_hermes_home_from(Path::new("standalone-hermes-home")),
            PathBuf::from("standalone-hermes-home")
        );
    }

    #[cfg(windows)]
    #[test]
    fn unreadable_update_marker_owner_blocks_acquisition() {
        use windows_sys::Win32::Foundation::{ERROR_ACCESS_DENIED, ERROR_INVALID_PARAMETER};

        assert!(unreadable_process_owner_must_block(Some(
            ERROR_ACCESS_DENIED as i32
        )));
        assert!(unreadable_process_owner_must_block(None));
        assert!(!unreadable_process_owner_must_block(Some(
            ERROR_INVALID_PARAMETER as i32
        )));
    }

    #[test]
    fn lock_probe_paths_include_desktop_app_payload() {
        let root = Path::new("/x/hermes-agent");
        let probes = install_lock_probe_paths(root);

        assert!(
            probes.iter().any(|p| p == &venv_hermes(root)),
            "venv shim remains part of the update lock probe"
        );
        assert!(
            // Windows/Linux payloads live under `resources/`, the macOS bundle
            // under `Contents/Resources/` — Path::ends_with is case-sensitive.
            probes.iter().any(|p| {
                p.ends_with(Path::new("resources/app.asar"))
                    || p.ends_with(Path::new("Resources/app.asar"))
            }),
            "packaged app.asar must be probed so repair/re-clone waits for the old desktop to exit"
        );
    }

    #[test]
    fn locked_paths_ignores_missing_payloads() {
        let root = Path::new("/nonexistent/hermes-agent");
        let probes = install_lock_probe_paths(root);

        assert!(locked_paths(&probes).is_empty());
    }

    #[test]
    fn locked_after_cleanup_is_a_hard_error_when_required() {
        let locked = vec![PathBuf::from("C:/Hermes/venv/Scripts/hermes.exe")];

        let err = lock_cleanup_result(locked.clone(), true)
            .expect_err("native Windows handoff must fail while an install target remains locked");

        assert_eq!(err.locked_paths, locked);
        assert!(
            err.to_string().contains("still locked after cleanup"),
            "the handoff failure should explain why mutation was refused"
        );
    }

    #[test]
    fn advisory_lock_probe_preserves_non_windows_compatibility() {
        let locked = vec![PathBuf::from("/opt/hermes/venv/bin/hermes")];

        assert!(
            lock_cleanup_result(locked, false).is_ok(),
            "platforms without mandatory executable locks retain the legacy advisory probe"
        );
    }

    #[test]
    fn shared_bridge_lease_fixture_matches_rust_schema() {
        let lease: BridgeQuiesceLease = serde_json::from_str(include_str!(
            "../../../../scripts/tests/fixtures/desktop-update-bridge-lease.json"
        ))
        .expect("shared bridge lease fixture must deserialize in Rust");

        assert_eq!(lease.schema_version, 1);
        assert!(valid_bridge_lease_id(&lease.lease_id));
        assert_eq!(
            lease.expires_at - lease.created_at,
            BRIDGE_LEASE_MAX_SECONDS
        );
        assert_eq!(
            lease.handoff_grace_until - lease.created_at,
            BRIDGE_LEASE_HANDOFF_GRACE_SECONDS
        );
    }

    #[test]
    fn bridge_lease_adoption_renews_and_cleans_only_its_capability() {
        let dir = unique_tmp_dir("bridge-lease-adopt");
        let install_root = dir.join("hermes-agent");
        std::fs::create_dir_all(&install_root).unwrap();
        let marker = dir.join(BRIDGE_LEASE_FILENAME);
        let lease_id = "lease-0123456789abcdef";
        let now = unix_time_seconds();
        let lease = BridgeQuiesceLease {
            schema_version: 1,
            lease_id: lease_id.to_string(),
            owner_pid: std::process::id(),
            created_at: now,
            expires_at: now + BRIDGE_LEASE_MAX_SECONDS,
            handoff_grace_until: now + BRIDGE_LEASE_HANDOFF_GRACE_SECONDS,
            install_root: install_root.to_string_lossy().into_owned(),
        };
        std::fs::write(&marker, serde_json::to_vec(&lease).unwrap()).unwrap();

        let mut guard =
            BridgeQuiesceLeaseGuard::adopt(marker.clone(), Some(lease_id), &install_root)
                .expect("matching live lease should be adoptable");
        let adopted: BridgeQuiesceLease =
            serde_json::from_str(&std::fs::read_to_string(&marker).unwrap()).unwrap();
        assert_eq!(adopted.lease_id, lease_id);
        assert_eq!(adopted.owner_pid, std::process::id());
        assert_eq!(
            adopted.expires_at - adopted.created_at,
            BRIDGE_LEASE_MAX_SECONDS
        );
        guard.complete().unwrap();
        assert!(!marker.exists(), "owned lease should clear on completion");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn bridge_lease_cleanup_preserves_a_foreign_rewrite() {
        let dir = unique_tmp_dir("bridge-lease-foreign");
        let install_root = dir.join("hermes-agent");
        std::fs::create_dir_all(&install_root).unwrap();
        let marker = dir.join(BRIDGE_LEASE_FILENAME);
        let lease_id = "lease-0123456789abcdef";
        let now = unix_time_seconds();
        let initial = BridgeQuiesceLease {
            schema_version: 1,
            lease_id: lease_id.to_string(),
            owner_pid: std::process::id(),
            created_at: now,
            expires_at: now + BRIDGE_LEASE_MAX_SECONDS,
            handoff_grace_until: now + BRIDGE_LEASE_HANDOFF_GRACE_SECONDS,
            install_root: install_root.to_string_lossy().into_owned(),
        };
        std::fs::write(&marker, serde_json::to_vec(&initial).unwrap()).unwrap();
        let mut guard =
            BridgeQuiesceLeaseGuard::adopt(marker.clone(), Some(lease_id), &install_root).unwrap();

        let mut foreign = initial;
        foreign.lease_id = "lease-fedcba9876543210".to_string();
        std::fs::write(&marker, serde_json::to_vec(&foreign).unwrap()).unwrap();
        assert!(guard.complete().is_err());

        let preserved: BridgeQuiesceLease =
            serde_json::from_str(&std::fs::read_to_string(&marker).unwrap()).unwrap();
        assert_eq!(preserved.lease_id, foreign.lease_id);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn bridge_lease_capability_requires_its_marker() {
        let dir = unique_tmp_dir("bridge-lease-missing");
        let install_root = dir.join("hermes-agent");
        std::fs::create_dir_all(&install_root).unwrap();

        let result = BridgeQuiesceLeaseGuard::adopt(
            dir.join(BRIDGE_LEASE_FILENAME),
            Some("lease-0123456789abcdef"),
            &install_root,
        );
        assert!(result.is_err(), "missing expected lease must fail closed");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn desktop_update_requires_a_bridge_lease_capability() {
        let dir = unique_tmp_dir("bridge-lease-required");
        let install_root = dir.join("hermes-agent");
        std::fs::create_dir_all(&install_root).unwrap();

        let result =
            BridgeQuiesceLeaseGuard::adopt(dir.join(BRIDGE_LEASE_FILENAME), None, &install_root);
        assert!(
            result.is_err(),
            "a Desktop update may not mutate without its lease capability"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn update_marker_guard_writes_then_removes_on_drop() {
        let dir = unique_tmp_dir("marker-guard");
        std::fs::create_dir_all(&dir).unwrap();
        let marker = dir.join(".hermes-update-in-progress");

        {
            let _g = UpdateMarkerGuard::acquire(marker.clone())
                .unwrap_or_else(|_| panic!("no live owner => acquire must succeed"));
            assert!(marker.exists(), "marker must exist while the guard is held");
            let body = std::fs::read_to_string(&marker).unwrap();
            let pid_line = body.lines().next().unwrap();
            assert_eq!(
                pid_line.trim().parse::<u32>().unwrap(),
                std::process::id(),
                "marker records our pid so the desktop can probe liveness"
            );
            assert_eq!(body.lines().count(), 2, "marker is pid + started_at lines");
        }

        assert!(
            !marker.exists(),
            "Drop must remove the marker on every exit path (incl. early return / panic unwind)"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn update_marker_guard_drop_is_quiet_when_already_gone() {
        let dir = unique_tmp_dir("marker-guard-gone");
        std::fs::create_dir_all(&dir).unwrap();
        let marker = dir.join(".hermes-update-in-progress");

        let guard = UpdateMarkerGuard::acquire(marker.clone())
            .unwrap_or_else(|_| panic!("no live owner => acquire must succeed"));
        // Simulate an external cleanup (e.g. the desktop pruned a marker it
        // judged stale) before our guard drops — Drop must not panic.
        std::fs::remove_file(&marker).unwrap();
        drop(guard);

        assert!(!marker.exists());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn acquire_refuses_and_preserves_malformed_or_partial_marker_bytes() {
        for (case, raw) in [
            ("partial", b"424242\n".as_slice()),
            ("malformed", b"not-a-pid\n1700000000\n".as_slice()),
        ] {
            let dir = unique_tmp_dir(&format!("marker-{case}"));
            std::fs::create_dir_all(&dir).unwrap();
            let marker = dir.join(".hermes-update-in-progress");
            std::fs::write(&marker, raw).unwrap();

            let error = UpdateMarkerGuard::acquire(marker.clone())
                .err()
                .expect("an existing marker with invalid bytes must fail closed");
            match error {
                UpdateMarkerAcquireError::Publish(err) => {
                    assert_eq!(err.kind(), std::io::ErrorKind::InvalidData);
                }
                UpdateMarkerAcquireError::ForeignOwner(owner) => {
                    panic!("invalid marker unexpectedly named live owner {}", owner.pid)
                }
            }
            assert_eq!(
                std::fs::read(&marker).unwrap(),
                raw,
                "{case} marker bytes must remain unchanged"
            );
            let _ = std::fs::remove_dir_all(&dir);
        }
    }

    /// Spawn a short-lived sibling process whose pid stands in for a foreign
    /// updater. Same-process double-acquire no longer models contention: since
    /// #74761 `live_marker_owner` treats our own pid as adoptable (desktop
    /// pre-writes it), so a second acquire in *this* process would succeed.
    fn spawn_foreign_holder() -> std::process::Child {
        #[cfg(windows)]
        {
            // `timeout /t` exits immediately when its stdin is redirected by
            // a test runner, which made this live-owner fixture racey. The
            // inbox PowerShell sleep has no console-input dependency.
            std::process::Command::new("powershell.exe")
                .args([
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "Start-Sleep -Seconds 30",
                ])
                .stdin(std::process::Stdio::null())
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::null())
                .spawn()
                .expect("spawn foreign marker holder")
        }
        #[cfg(not(windows))]
        {
            std::process::Command::new("sleep")
                .arg("30")
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::null())
                .spawn()
                .expect("spawn foreign marker holder")
        }
    }

    #[test]
    fn acquire_refuses_while_a_live_updater_owns_the_marker() {
        let dir = unique_tmp_dir("marker-contended");
        std::fs::create_dir_all(&dir).unwrap();
        let marker = dir.join(".hermes-update-in-progress");

        // A live *foreign* updater holds it. We must NOT clobber the marker and
        // run concurrently over the same checkout — that race is what let a
        // dashboard `hermes update` and install-mode bootstrap mutate one tree
        // at once. Own-pid markers are adoptable (#74761), so the foreign pid
        // must be a real sibling process.
        let mut foreign = spawn_foreign_holder();
        let foreign_pid = foreign.id();
        let started_at = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        std::fs::write(&marker, format!("{foreign_pid}\n{started_at}")).unwrap();

        let error = UpdateMarkerGuard::acquire(marker.clone())
            .err()
            .expect("acquire must be refused while a foreign updater is live");
        let owner = match error {
            UpdateMarkerAcquireError::ForeignOwner(owner) => owner,
            UpdateMarkerAcquireError::Publish(err) => {
                panic!("marker publication failed unexpectedly: {err}")
            }
        };
        assert_eq!(owner.pid, foreign_pid);

        // The refused guard must not delete the live owner's marker.
        assert!(
            marker.exists(),
            "refused acquire must leave the marker intact"
        );
        let _ = foreign.kill();
        let _ = foreign.wait();
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(windows)]
    #[test]
    fn acquire_reclaims_a_reused_live_pid_identity() {
        let dir = unique_tmp_dir("marker-reused-live-pid");
        std::fs::create_dir_all(&dir).unwrap();
        let marker = dir.join(".hermes-update-in-progress");
        let mut foreign = spawn_foreign_holder();

        // A process created in 2026 cannot own a marker allegedly claimed at
        // Unix second 1. Numeric liveness alone would false-block forever.
        std::fs::write(&marker, format!("{}\n1\n", foreign.id())).unwrap();
        let guard = UpdateMarkerGuard::acquire(marker.clone()).unwrap_or_else(|_| {
            panic!("a live PID whose process postdates the claim is reused, not the owner")
        });
        drop(guard);

        let _ = foreign.kill();
        let _ = foreign.wait();
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn acquire_refuses_live_owner_even_past_marker_age_ceiling() {
        let dir = unique_tmp_dir("marker-live-past-ceiling");
        std::fs::create_dir_all(&dir).unwrap();
        let marker = dir.join(".hermes-update-in-progress");
        #[cfg(windows)]
        let foreign_pid = 4u32; // System: older than this marker or unreadable => fail closed.
        #[cfg(not(windows))]
        let foreign_pid = 1u32;
        let long_ago = unix_time_seconds().saturating_sub(UPDATE_MARKER_MAX_AGE_SECS + 60);
        std::fs::write(&marker, format!("{foreign_pid}\n{long_ago}\n")).unwrap();

        let result = UpdateMarkerGuard::acquire(marker.clone());
        assert!(
            matches!(result, Err(UpdateMarkerAcquireError::ForeignOwner(_))),
            "a long update stays exclusive while its marker PID is still live"
        );
        assert!(marker.exists(), "the live owner's old marker is preserved");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn acquire_adopts_a_marker_prewritten_with_our_own_pid() {
        // #74761: desktop writeUpdateMarker(hermesHome, child.pid) races ahead
        // of UpdateMarkerGuard::acquire. The marker names US; refusing it made
        // every in-app desktop update loop forever. Adopt and rewrite.
        let dir = unique_tmp_dir("marker-own-pid");
        std::fs::create_dir_all(&dir).unwrap();
        let marker = dir.join(".hermes-update-in-progress");

        let started_at = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0)
            .saturating_sub(2);
        std::fs::write(&marker, format!("{}\n{started_at}", std::process::id())).unwrap();

        let guard = UpdateMarkerGuard::acquire(marker.clone())
            .unwrap_or_else(|_| panic!("own-pid pre-write must be adoptable"));
        assert!(marker.exists(), "adopted guard must own the marker");
        let body = std::fs::read_to_string(&marker).unwrap();
        assert_eq!(
            body.lines().next().unwrap().trim().parse::<u32>().unwrap(),
            std::process::id(),
            "acquire rewrites the marker with our pid + fresh started_at"
        );
        drop(guard);
        assert!(
            !marker.exists(),
            "Drop must still clear the marker we adopted"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn acquire_reclaims_a_marker_owned_by_a_dead_pid() {
        let dir = unique_tmp_dir("marker-dead-pid");
        std::fs::create_dir_all(&dir).unwrap();
        let marker = dir.join(".hermes-update-in-progress");

        // pid 1 exists everywhere, so fabricate a dead one: a very large pid
        // that no live process owns. A crashed updater must never wedge every
        // future update.
        let started_at = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        std::fs::write(&marker, format!("4294967294\n{started_at}")).unwrap();

        let guard = UpdateMarkerGuard::acquire(marker.clone())
            .unwrap_or_else(|_| panic!("a dead owner must not block acquisition"));
        let body = std::fs::read_to_string(&marker).unwrap();
        assert_eq!(
            body.lines().next().unwrap().trim().parse::<u32>().unwrap(),
            std::process::id(),
            "reclaiming rewrites the marker with our pid"
        );
        drop(guard);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn acquire_adopts_own_pid_marker_even_when_old() {
        let dir = unique_tmp_dir("marker-stale-age");
        std::fs::create_dir_all(&dir).unwrap();
        let marker = dir.join(".hermes-update-in-progress");

        // Desktop may prewrite this updater's exact PID before Rust starts.
        // It remains an adoptable self-claim even when its timestamp is old;
        // this exception never applies to a foreign live owner.
        let long_ago = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0)
            .saturating_sub(UPDATE_MARKER_MAX_AGE_SECS + 60);
        std::fs::write(&marker, format!("{}\n{long_ago}", std::process::id())).unwrap();

        let guard = UpdateMarkerGuard::acquire(marker.clone())
            .unwrap_or_else(|_| panic!("an own-PID marker must be adoptable"));
        drop(guard);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn completed_update_releases_marker_before_guard_drop() {
        let dir = unique_tmp_dir("marker-complete");
        std::fs::create_dir_all(&dir).unwrap();
        let marker = dir.join(".hermes-update-in-progress");

        let mut guard = UpdateMarkerGuard::acquire(marker.clone())
            .unwrap_or_else(|_| panic!("no live owner => acquire must succeed"));
        guard.complete().unwrap();

        assert!(
            !marker.exists(),
            "a successful update must unblock desktop startup before relaunch/exit"
        );
        drop(guard);
        assert!(!marker.exists(), "Drop stays idempotent after completion");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn completed_update_preserves_a_foreign_marker_rewrite() {
        let dir = unique_tmp_dir("marker-foreign-rewrite");
        std::fs::create_dir_all(&dir).unwrap();
        let marker = dir.join(".hermes-update-in-progress");
        let mut guard = UpdateMarkerGuard::acquire(marker.clone())
            .unwrap_or_else(|_| panic!("no live owner => acquire must succeed"));

        std::fs::write(&marker, "424242\n1700000000\n").unwrap();
        assert!(guard.complete().is_err());

        assert_eq!(
            std::fs::read_to_string(&marker).unwrap(),
            "424242\n1700000000\n",
            "cleanup must restore a marker rewritten by another owner"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn completed_update_preserves_same_pid_marker_with_foreign_timestamp() {
        let dir = unique_tmp_dir("marker-same-pid-foreign-timestamp");
        std::fs::create_dir_all(&dir).unwrap();
        let marker = dir.join(".hermes-update-in-progress");
        let mut guard = UpdateMarkerGuard::acquire(marker.clone())
            .unwrap_or_else(|_| panic!("no live owner => acquire must succeed"));
        let foreign_started_at = guard.started_at.saturating_add(1);

        std::fs::write(
            &marker,
            format!("{}\n{foreign_started_at}\n", std::process::id()),
        )
        .unwrap();
        assert!(guard.complete().is_err());

        assert_eq!(
            std::fs::read_to_string(&marker).unwrap(),
            format!("{}\n{foreign_started_at}\n", std::process::id()),
            "cleanup requires the exact pid + start timestamp claim"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn parses_update_branch_from_space_or_equals_args() {
        assert_eq!(
            update_branch_from_args(["--update", "--branch", "bb/test"]),
            Some("bb/test".to_string())
        );
        assert_eq!(
            update_branch_from_args(["--update", "--branch=main"]),
            Some("main".to_string())
        );
        assert_eq!(update_branch_from_args(["--update"]), None);
    }

    #[test]
    fn bridge_lease_capability_uses_private_env_without_unknown_args() {
        assert_eq!(
            bridge_lease_id_from_sources(
                ["--update", "--branch", "main"],
                Some("lease-env-0123456789abcdef".to_string()),
            ),
            Some("lease-env-0123456789abcdef".to_string())
        );
        assert_eq!(
            bridge_lease_id_from_sources(
                [
                    "--update",
                    "--bridge-lease-id",
                    "lease-arg-0123456789abcdef"
                ],
                Some("lease-env-0123456789abcdef".to_string()),
            ),
            Some("lease-arg-0123456789abcdef".to_string()),
            "an explicit argument remains deterministic when both transports exist"
        );
    }

    #[test]
    fn deferred_gateway_adoption_frame_is_first_exact_and_single() {
        let invocation_id = "invocation-frame-0123456789abcdef";
        let child_pid = 4242;
        let valid = serde_json::json!({
            "schema_version": 1,
            "event": "deferred-gateway-lease-adopted",
            "invocation_id": invocation_id,
            "owner_pid": child_pid,
        })
        .to_string();

        let mut adopted = false;
        let mut replay = false;
        observe_deferred_gateway_stdout_line(
            &valid,
            invocation_id,
            child_pid,
            false,
            &mut adopted,
            &mut replay,
        )
        .expect("the exact first frame authorizes the trusted child");
        assert!(adopted);
        assert!(observe_deferred_gateway_stdout_line(
            &valid,
            invocation_id,
            child_pid,
            false,
            &mut adopted,
            &mut replay,
        )
        .is_err());

        for invalid in [
            "ordinary output".to_string(),
            serde_json::json!({
                "schema_version": 1,
                "event": "deferred-gateway-lease-adopted",
                "invocation_id": invocation_id,
                "owner_pid": child_pid + 1,
            })
            .to_string(),
            serde_json::json!({
                "schema_version": 1,
                "event": "deferred-gateway-lease-adopted",
                "invocation_id": invocation_id,
                "owner_pid": child_pid,
                "extra": true,
            })
            .to_string(),
        ] {
            let mut adopted = false;
            let mut replay = false;
            assert!(observe_deferred_gateway_stdout_line(
                &invalid,
                invocation_id,
                child_pid,
                false,
                &mut adopted,
                &mut replay,
            )
            .is_err());
        }

        let mut adopted = false;
        let mut replay = false;
        observe_deferred_gateway_stdout_line(
            "✓ Deferred gateway fleet was already resumed.",
            invocation_id,
            child_pid,
            true,
            &mut adopted,
            &mut replay,
        )
        .expect("an exact completed replay may use its durable plan proof instead");
        assert!(replay && !adopted);
        assert!(observe_deferred_gateway_stdout_line(
            &valid,
            invocation_id,
            child_pid,
            true,
            &mut adopted,
            &mut replay,
        )
        .is_err());
    }

    #[test]
    fn deferred_gateway_plan_requires_exact_pending_to_completed_bytes() {
        let home = unique_tmp_dir("deferred-gateway-plan");
        let install_root = home.join("hermes-agent");
        std::fs::create_dir_all(&install_root).unwrap();
        let invocation_id = "invocation-plan-0123456789abcdef";
        let now = unix_time_seconds();
        let raw = serde_json::to_vec(&serde_json::json!({
            "schema_version": 1,
            "invocation_id": invocation_id,
            "lease_fingerprint": "a".repeat(64),
            "install_root": install_root.to_string_lossy(),
            "created_at": now,
            "expires_at": now + DEFERRED_GATEWAY_PLAN_MAX_SECONDS,
            "profiles": [],
            "cold_start_if_installed": true,
            "auth": "b".repeat(64),
        }))
        .unwrap();
        let pending = home.join(format!(".hermes-gateway-resume-{invocation_id}.json"));
        let completed = home.join(format!(".hermes-gateway-resume-{invocation_id}.completed"));
        std::fs::write(&pending, &raw).unwrap();

        let proof = deferred_gateway_plan_proof(&home, invocation_id, &install_root)
            .expect("fresh exact pending plan is valid");
        assert!(!proof.started_completed);
        std::fs::rename(&pending, &completed).unwrap();
        deferred_gateway_plan_consumed(&home, invocation_id, &install_root, &proof)
            .expect("identical completed bytes prove exact consumption");

        std::fs::write(&completed, [raw.as_slice(), b" "].concat()).unwrap();
        assert!(
            deferred_gateway_plan_consumed(&home, invocation_id, &install_root, &proof).is_err()
        );
        std::fs::write(&completed, &raw).unwrap();
        let consume = home.join(format!(
            ".hermes-gateway-resume-{invocation_id}.json.consume-1-deadbeef"
        ));
        std::fs::write(&consume, &raw).unwrap();
        assert!(deferred_gateway_plan_proof(&home, invocation_id, &install_root).is_err());
        let _ = std::fs::remove_dir_all(&home);
    }

    fn write_test_deferred_gateway_plan(
        home: &Path,
        install_root: &Path,
        invocation_id: &str,
    ) -> DeferredGatewayPlanProof {
        let now = unix_time_seconds();
        let raw = serde_json::to_vec(&serde_json::json!({
            "schema_version": 1,
            "invocation_id": invocation_id,
            "lease_fingerprint": "a".repeat(64),
            "install_root": install_root.to_string_lossy(),
            "created_at": now,
            "expires_at": now + DEFERRED_GATEWAY_PLAN_MAX_SECONDS,
            "profiles": [],
            "cold_start_if_installed": true,
            "auth": "b".repeat(64),
        }))
        .unwrap();
        let pending = home.join(format!(".hermes-gateway-resume-{invocation_id}.json"));
        std::fs::write(&pending, raw).unwrap();
        deferred_gateway_plan_proof(home, invocation_id, install_root).unwrap()
    }

    #[test]
    fn deferred_gateway_stream_inversion_buffers_stderr_until_exact_stdout_frame() {
        let invocation_id = "invocation-inversion-0123456789abcdef";
        let child_pid = 4242;
        let frame = serde_json::json!({
            "schema_version": 1,
            "event": "deferred-gateway-lease-adopted",
            "invocation_id": invocation_id,
            "owner_pid": child_pid,
        })
        .to_string();
        let mut buffered = Vec::new();
        let mut buffered_bytes = 0;
        assert!(buffer_deferred_gateway_stderr_before_adoption(
            "stderr selected first",
            false,
            false,
            &mut buffered,
            &mut buffered_bytes,
        )
        .expect("cross-pipe scheduling does not determine protocol order"));
        let mut adopted = false;
        let mut replay = false;
        observe_deferred_gateway_stdout_line(
            &frame,
            invocation_id,
            child_pid,
            false,
            &mut adopted,
            &mut replay,
        )
        .expect("the first stdout line remains authoritative after earlier stderr delivery");
        assert!(adopted);
        assert_eq!(buffered, ["stderr selected first"]);
    }

    #[test]
    fn deferred_gateway_fast_adopt_clear_survives_status_before_pipe_drain() {
        let home = unique_tmp_dir("deferred-fast-adopt-clear");
        let install_root = home.join("hermes-agent");
        std::fs::create_dir_all(&install_root).unwrap();
        let invocation_id = "invocation-fast-0123456789abcdef";
        let plan = write_test_deferred_gateway_plan(&home, &install_root, invocation_id);
        std::fs::rename(&plan.pending_path, &plan.completed_path).unwrap();
        let mut guard = BridgeQuiesceLeaseGuard {
            path: home.join(BRIDGE_LEASE_FILENAME),
            lease_id: "lease-fast-0123456789abcdef".into(),
            install_root: install_root.clone(),
            owned: false,
            // The exact stdout adoption frame supplies this durable transfer
            // proof even when adoption+clear happens between marker polls.
            transferred_to: Some(4242),
        };
        let status_arrived_before_stdout_drain = true;
        let mut adopted = false;
        let mut replay = false;
        let frame = serde_json::json!({
            "schema_version": 1,
            "event": "deferred-gateway-lease-adopted",
            "invocation_id": invocation_id,
            "owner_pid": 4242,
        })
        .to_string();
        observe_deferred_gateway_stdout_line(
            &frame,
            invocation_id,
            4242,
            false,
            &mut adopted,
            &mut replay,
        )
        .expect("buffered stdout is drained after process status");
        assert!(status_arrived_before_stdout_drain && adopted);
        prove_deferred_gateway_terminal(
            true,
            adopted,
            replay,
            &plan,
            &home,
            invocation_id,
            &install_root,
            &mut guard,
        )
        .expect("exact frame + completed plan + absent lease prove fast success");
        let _ = std::fs::remove_dir_all(&home);
    }

    #[test]
    fn deferred_gateway_nonzero_after_adoption_requires_exact_parent_return() {
        let home = unique_tmp_dir("deferred-nonzero-return");
        let install_root = home.join("hermes-agent");
        std::fs::create_dir_all(&install_root).unwrap();
        let invocation_id = "invocation-return-0123456789abcdef";
        let plan = write_test_deferred_gateway_plan(&home, &install_root, invocation_id);
        let marker = home.join(BRIDGE_LEASE_FILENAME);
        let lease_id = "lease-return-0123456789abcdef";
        let now = unix_time_seconds();
        let returned = BridgeQuiesceLease {
            schema_version: 1,
            lease_id: lease_id.into(),
            owner_pid: std::process::id(),
            created_at: now,
            expires_at: now + BRIDGE_LEASE_MAX_SECONDS,
            handoff_grace_until: now + BRIDGE_LEASE_HANDOFF_GRACE_SECONDS,
            install_root: install_root.to_string_lossy().into_owned(),
        };
        std::fs::write(&marker, serde_json::to_vec(&returned).unwrap()).unwrap();
        let mut guard = BridgeQuiesceLeaseGuard {
            path: marker.clone(),
            lease_id: lease_id.into(),
            install_root: install_root.clone(),
            owned: false,
            transferred_to: Some(4242),
        };
        prove_deferred_gateway_terminal(
            false,
            true,
            false,
            &plan,
            &home,
            invocation_id,
            &install_root,
            &mut guard,
        )
        .expect("a nonzero adopted child is safe only after exact parent return");
        assert!(guard.owned);

        std::fs::remove_file(&marker).unwrap();
        guard.owned = false;
        assert!(prove_deferred_gateway_terminal(
            false,
            true,
            false,
            &plan,
            &home,
            invocation_id,
            &install_root,
            &mut guard,
        )
        .is_err());
        let _ = std::fs::remove_dir_all(&home);
    }

    fn write_test_update_receipt(
        path: &Path,
        install_root: &Path,
        invocation_id: &str,
        lease_id: &str,
        timestamp: u64,
        healthy: bool,
    ) {
        let sha = "0123456789abcdef0123456789abcdef01234567";
        let receipt = serde_json::json!({
            "schema_version": 1,
            "invocation_id": invocation_id,
            "lease_id": lease_id,
            "mode": "git",
            "root": install_root.to_string_lossy(),
            "remote": "origin",
            "branch": "main",
            "target_ref": "refs/remotes/origin/main",
            "target_sha": sha,
            "resulting_head": sha,
            "archive_sha": null,
            "timestamp": timestamp,
            "success": true,
            "gateway_resume_deferred": true,
            "health": {
                "critical_syntax": healthy,
                "critical_imports": true,
                "dependencies": true,
                "node_dependencies": true
            }
        });
        std::fs::write(path, serde_json::to_vec(&receipt).unwrap()).unwrap();
    }

    fn test_update_receipt(install_root: &Path, timestamp: u64) -> UpdateReceipt {
        let path = install_root
            .parent()
            .unwrap()
            .join(".test-update-receipt.json");
        write_test_update_receipt(
            &path,
            install_root,
            "invocation-ack-0123456789abcdef",
            "lease-ack-0123456789abcdef",
            timestamp,
            true,
        );
        validate_update_receipt(
            &path,
            "invocation-ack-0123456789abcdef",
            "lease-ack-0123456789abcdef",
            install_root,
            "main",
            timestamp,
            timestamp,
        )
        .unwrap()
    }

    #[test]
    fn archive_rebuild_stamp_uses_exact_64_hex_receipt_identity() {
        let home = unique_tmp_dir("archive-build-stamp");
        let install_root = home.join("hermes-agent");
        std::fs::create_dir_all(&install_root).unwrap();
        let mut receipt = test_update_receipt(&install_root, unix_time_seconds());
        let archive_sha = "b".repeat(64);
        receipt.mode = "archive".into();
        receipt.remote = None;
        receipt.target_ref = None;
        receipt.target_sha = None;
        receipt.resulting_head = None;
        receipt.archive_sha = Some(archive_sha.clone());
        let env = receipt_build_identity_env(&receipt).unwrap();
        assert_eq!(env[0], ("GITHUB_SHA".into(), OsString::from(archive_sha)));
        assert_eq!(env[1], ("GITHUB_REF_NAME".into(), OsString::from("main")));
        let _ = std::fs::remove_dir_all(&home);
    }

    #[cfg(windows)]
    #[tokio::test]
    async fn windows_complete_requires_exact_durable_desktop_ack() {
        let hermes_home = unique_tmp_dir("desktop-durable-ack");
        let install_root = hermes_home.join("hermes-agent");
        std::fs::create_dir_all(&install_root).unwrap();
        let now = unix_time_seconds();
        let receipt = test_update_receipt(&install_root, now);
        let executable =
            PathBuf::from(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
                .canonicalize()
                .unwrap();
        let requested_at = unix_time_seconds();
        let mut child = Command::new(&executable)
            .args([
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Start-Sleep -Seconds 30",
            ])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let creation_ticks = child_process_creation_ticks(&child).unwrap();
        let relaunch = WindowsDesktopRelaunchIdentity {
            pid: child.id().unwrap(),
            process_started_at: filetime_ticks_to_unix_seconds(creation_ticks).unwrap(),
            executable: executable.clone(),
            requested_at,
        };
        let attempt_id = "attempt-ack-0123456789abcdef";
        let result_path = hermes_home.join(".hermes-update-result.json");
        let ack_path = hermes_home.join(format!(".hermes-update-ack-{attempt_id}.json"));
        let pending_raw = windows_handoff_result_raw(
            "pending",
            None,
            "pending",
            attempt_id,
            &install_root,
            &receipt,
            &relaunch,
            None,
        )
        .unwrap();
        publish_windows_handoff_result(&result_path, None, &pending_raw).unwrap();
        let build_id = expected_receipt_build_id(&receipt).unwrap();
        let ack_raw = serde_json::to_vec(&serde_json::json!({
            "schema_version": 1,
            "attempt_id": attempt_id,
            "invocation_id": receipt.invocation_id,
            "lease_id": receipt.lease_id,
            "pid": relaunch.pid,
            "process_started_at": relaunch.process_started_at,
            "root": install_root.canonicalize().unwrap().to_string_lossy(),
            "executable": executable.to_string_lossy(),
            "build_id": build_id,
            "build_source": "install-stamp",
            "backend_ready": true,
            "backend_mode": "local",
            "acknowledged_at": unix_time_seconds(),
            "error": null,
        }))
        .unwrap();
        std::fs::write(&ack_path, &ack_raw).unwrap();
        let (ack, observed_raw) = wait_for_windows_desktop_ack(
            &mut child,
            creation_ticks,
            &ack_path,
            attempt_id,
            &install_root,
            &receipt,
            &relaunch,
            Duration::from_secs(1),
        )
        .await
        .expect("exact live PID + creation + healthy backend ACK prove readiness");
        remove_exact_handoff_ack(&ack_path, &observed_raw).unwrap();
        let complete_raw = windows_handoff_result_raw(
            "complete",
            Some(0),
            "complete",
            attempt_id,
            &install_root,
            &receipt,
            &relaunch,
            Some(&ack),
        )
        .unwrap();
        publish_windows_handoff_result(&result_path, Some(&pending_raw), &complete_raw).unwrap();
        let terminal: serde_json::Value =
            serde_json::from_slice(&std::fs::read(&result_path).unwrap()).unwrap();
        assert_eq!(terminal.as_object().unwrap().len(), 16);
        assert_eq!(terminal["receipt"].as_object().unwrap().len(), 15);
        assert_eq!(terminal["receipt"]["gateway_resume_deferred"], true);
        assert_eq!(terminal["state"], "complete");
        assert_eq!(terminal["ok"], true);
        assert_eq!(terminal["relaunch"]["state"], "acknowledged");
        assert_eq!(terminal["desktop"]["backend_ready"], true);
        let _ = child.kill().await;
        let _ = child.wait().await;
        let _ = std::fs::remove_dir_all(&hermes_home);
    }

    #[cfg(windows)]
    #[test]
    fn windows_stale_result_or_recovery_artifact_blocks_before_mutation() {
        let hermes_home = unique_tmp_dir("desktop-stale-result");
        std::fs::create_dir_all(&hermes_home).unwrap();
        ensure_windows_handoff_result_slot_clear(&hermes_home)
            .expect("an absent result generation is clear");
        let result_path = hermes_home.join(".hermes-update-result.json");
        std::fs::write(&result_path, b"{}").unwrap();
        assert!(ensure_windows_handoff_result_slot_clear(&hermes_home).is_err());
        std::fs::remove_file(&result_path).unwrap();
        let artifact =
            bridge_lease_sibling(&result_path, ".cas-shadow-1234-0123456789abcdef").unwrap();
        std::fs::write(&artifact, b"{}").unwrap();
        assert!(ensure_windows_handoff_result_slot_clear(&hermes_home).is_err());
        let _ = std::fs::remove_dir_all(&hermes_home);
    }

    #[cfg(windows)]
    #[tokio::test]
    async fn windows_desktop_exit_without_ack_never_proves_complete() {
        let hermes_home = unique_tmp_dir("desktop-no-ack");
        let install_root = hermes_home.join("hermes-agent");
        std::fs::create_dir_all(&install_root).unwrap();
        let receipt = test_update_receipt(&install_root, unix_time_seconds());
        let executable = PathBuf::from("cmd.exe");
        let requested_at = unix_time_seconds();
        let mut child = Command::new(&executable)
            .args(["/d", "/s", "/c", "exit /b 0"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let creation_ticks = child_process_creation_ticks(&child).unwrap();
        let relaunch = WindowsDesktopRelaunchIdentity {
            pid: child.id().unwrap(),
            process_started_at: filetime_ticks_to_unix_seconds(creation_ticks).unwrap(),
            executable,
            requested_at,
        };
        assert!(wait_for_windows_desktop_ack(
            &mut child,
            creation_ticks,
            &hermes_home.join(".hermes-update-ack-attempt-none.json"),
            "attempt-none-0123456789abcdef",
            &install_root,
            &receipt,
            &relaunch,
            Duration::from_secs(1),
        )
        .await
        .is_err());
        let _ = child.wait().await;
        let _ = std::fs::remove_dir_all(&hermes_home);
    }

    #[cfg(windows)]
    #[test]
    fn desktop_ack_tombstone_deletion_failure_withholds_completion() {
        let hermes_home = unique_tmp_dir("desktop-ack-delete-failure");
        std::fs::create_dir_all(&hermes_home).unwrap();
        let ack_path = hermes_home.join(".hermes-update-ack-attempt.json");
        let raw = br#"{"schema_version":1}"#;
        std::fs::write(&ack_path, raw).unwrap();
        let err = remove_exact_handoff_ack_with(&ack_path, raw, |_| {
            Err(std::io::Error::new(
                std::io::ErrorKind::PermissionDenied,
                "simulated deletion refusal",
            ))
        })
        .expect_err("a failed exact tombstone deletion must withhold completion");
        assert!(err.to_string().contains("retiring the exact Desktop"));
        assert!(has_recovery_artifacts(&ack_path).unwrap());
        let _ = std::fs::remove_dir_all(&hermes_home);
    }

    #[test]
    fn update_success_requires_fresh_correlated_healthy_receipt() {
        let dir = unique_tmp_dir("receipt-proof");
        let install_root = dir.join("hermes-agent");
        std::fs::create_dir_all(&install_root).unwrap();
        let receipt_path = dir.join(".hermes-update-receipt.json");
        let now = unix_time_seconds();
        let invocation_id = "invocation-0123456789abcdef";
        let lease_id = "lease-receipt-0123456789abcdef";

        write_test_update_receipt(
            &receipt_path,
            &install_root,
            invocation_id,
            lease_id,
            now,
            true,
        );
        validate_update_receipt(
            &receipt_path,
            invocation_id,
            lease_id,
            &install_root,
            "main",
            now,
            now,
        )
        .expect("fresh exact receipt proves mutation success");

        assert!(validate_update_receipt(
            &receipt_path,
            invocation_id,
            "lease-other-0123456789abcdef",
            &install_root,
            "main",
            now,
            now,
        )
        .is_err());
        write_test_update_receipt(
            &receipt_path,
            &install_root,
            invocation_id,
            lease_id,
            now.saturating_sub(1),
            true,
        );
        assert!(validate_update_receipt(
            &receipt_path,
            invocation_id,
            lease_id,
            &install_root,
            "main",
            now,
            now,
        )
        .is_err());
        write_test_update_receipt(
            &receipt_path,
            &install_root,
            invocation_id,
            lease_id,
            now,
            false,
        );
        assert!(validate_update_receipt(
            &receipt_path,
            invocation_id,
            lease_id,
            &install_root,
            "main",
            now,
            now,
        )
        .is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn update_marker_cleanup_failure_withholds_completion_and_preserves_recovery() {
        let dir = unique_tmp_dir("marker-cleanup-failure");
        std::fs::create_dir_all(&dir).unwrap();
        let marker = dir.join(".hermes-update-in-progress");
        let mut guard = UpdateMarkerGuard::acquire(marker.clone())
            .unwrap_or_else(|_| panic!("marker acquisition must succeed"));
        let result = guard.complete_with(|_| {
            Err(std::io::Error::new(
                std::io::ErrorKind::PermissionDenied,
                "simulated deletion refusal",
            ))
        });
        assert!(result.is_err(), "cleanup failure must be terminal");
        assert!(has_recovery_artifacts(&marker).unwrap());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn bridge_handoff_grace_accepts_90_seconds_and_rejects_91() {
        let dir = unique_tmp_dir("bridge-grace-boundary");
        let install_root = dir.join("hermes-agent");
        std::fs::create_dir_all(&install_root).unwrap();
        let now = unix_time_seconds();
        let mut lease = BridgeQuiesceLease {
            schema_version: 1,
            lease_id: "lease-grace-0123456789abcdef".into(),
            owner_pid: std::process::id(),
            created_at: now,
            expires_at: now + BRIDGE_LEASE_MAX_SECONDS,
            handoff_grace_until: now + 90,
            install_root: install_root.to_string_lossy().into_owned(),
        };
        validate_bridge_lease_document(&lease, &lease.lease_id, &install_root, now)
            .expect("90-second final handoff window is the cross-runtime maximum");
        lease.handoff_grace_until = now + 91;
        assert!(
            validate_bridge_lease_document(&lease, &lease.lease_id, &install_root, now,).is_err()
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn dead_owner_recovery_blocks_at_90_seconds_and_retires_at_91() {
        let dir = unique_tmp_dir("bridge-dead-owner-recovery");
        let install_root = dir.join("hermes-agent");
        std::fs::create_dir_all(&install_root).unwrap();
        let marker = dir.join(BRIDGE_LEASE_FILENAME);
        let lease = BridgeQuiesceLease {
            schema_version: 1,
            lease_id: "lease-dead-owner-0123456789abcdef".into(),
            owner_pid: 4242,
            created_at: 100_000,
            expires_at: 101_200,
            handoff_grace_until: 100_090,
            install_root: install_root.to_string_lossy().into_owned(),
        };
        let artifact = bridge_lease_sibling(&marker, ".cas-shadow-4242-0123456789abcdef").unwrap();
        std::fs::write(&artifact, serde_json::to_vec(&lease).unwrap()).unwrap();

        assert!(
            ensure_no_recovery_artifacts_at(&marker, 100_090, |_, _| false).is_err(),
            "dead-owner recovery still blocks through the final grace second"
        );
        assert!(artifact.exists());
        assert!(
            ensure_no_recovery_artifacts_at(&marker, 100_091, |_, _| true).is_err(),
            "a matching owner keeps ordinary recovery active after handoff grace"
        );
        assert!(artifact.exists());
        ensure_no_recovery_artifacts_at(&marker, 100_091, |_, _| false)
            .expect("dead-owner recovery retires after handoff grace by exact-byte CAS");
        assert!(!artifact.exists());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn emergency_recovery_blocks_until_its_bounded_expiry_then_retires() {
        let dir = unique_tmp_dir("bridge-emergency");
        let install_root = dir.join("hermes-agent");
        std::fs::create_dir_all(&install_root).unwrap();
        let marker = dir.join(BRIDGE_LEASE_FILENAME);
        let lease = BridgeQuiesceLease {
            schema_version: 1,
            lease_id: "lease-emergency-0123456789abcdef".into(),
            owner_pid: 4,
            created_at: 100_000,
            expires_at: 100_120,
            handoff_grace_until: 100_090,
            install_root: install_root.to_string_lossy().into_owned(),
        };
        let emergency = bridge_lease_sibling(&marker, ".cas-emergency-1-0123456789abcdef").unwrap();
        std::fs::write(&emergency, serde_json::to_vec(&lease).unwrap()).unwrap();
        assert!(
            ensure_no_recovery_artifacts_at(&marker, 100_120, |_, _| false).is_err(),
            "emergency recovery blocks through its bounded expiry"
        );
        assert!(emergency.exists());
        ensure_no_recovery_artifacts_at(&marker, 100_121, |_, _| false)
            .expect("expired bounded emergency artifact retires by exact-byte CAS");
        assert!(!emergency.exists());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(windows)]
    #[tokio::test]
    async fn updater_job_terminates_its_exact_child() {
        let job = UpdaterJob::new().expect("create kill-on-close updater job");
        let mut child = Command::new("cmd.exe")
            .args(["/d", "/s", "/c", "ping -n 30 127.0.0.1 >nul"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn contained test child");
        job.assign(&child)
            .expect("assign exact child to updater job");
        job.terminate().expect("terminate contained updater job");

        let status = tokio::time::timeout(Duration::from_secs(5), child.wait())
            .await
            .expect("contained child must exit promptly")
            .expect("wait for contained child");
        assert!(!status.success(), "job termination must not report success");
    }

    #[cfg(windows)]
    #[tokio::test]
    async fn bridge_lease_transfers_to_contained_python_then_returns_to_parent() {
        let dir = unique_tmp_dir("bridge-lease-child-transfer");
        let install_root = dir.join("hermes-agent");
        std::fs::create_dir_all(&install_root).unwrap();
        let marker = dir.join(BRIDGE_LEASE_FILENAME);
        let lease_id = "lease-child-0123456789abcdef";
        let now = unix_time_seconds();
        let initial = BridgeQuiesceLease {
            schema_version: 1,
            lease_id: lease_id.to_string(),
            owner_pid: std::process::id(),
            created_at: now,
            expires_at: now + BRIDGE_LEASE_MAX_SECONDS,
            handoff_grace_until: now + BRIDGE_LEASE_HANDOFF_GRACE_SECONDS,
            install_root: install_root.to_string_lossy().into_owned(),
        };
        std::fs::write(&marker, serde_json::to_vec(&initial).unwrap()).unwrap();
        let mut guard =
            BridgeQuiesceLeaseGuard::adopt(marker.clone(), Some(lease_id), &install_root)
                .expect("parent adopts the incoming lease");

        let child_script = dir.join("adopt-and-clean.ps1");
        std::fs::write(
            &child_script,
            r#"param([string]$Marker)
$lease = [System.IO.File]::ReadAllText($Marker) | ConvertFrom-Json
$parentPid = [int]$lease.owner_pid
$now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$lease.owner_pid = $PID
$lease.created_at = $now
$lease.expires_at = $now + 1200
$lease.handoff_grace_until = $now + 90
$json = $lease | ConvertTo-Json -Compress
[System.IO.File]::WriteAllText($Marker, $json, (New-Object System.Text.UTF8Encoding($false)))
Start-Sleep -Milliseconds 1500
$lease.owner_pid = $parentPid
$now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$lease.created_at = $now
$lease.expires_at = $now + 1200
$lease.handoff_grace_until = $now + 90
$json = $lease | ConvertTo-Json -Compress
[System.IO.File]::WriteAllText($Marker, $json, (New-Object System.Text.UTF8Encoding($false)))
"#,
        )
        .unwrap();
        let updater_job = UpdaterJob::new().expect("create contained updater job");
        let startup_gate = WindowsUpdaterStartupGate::new();
        let descendant_args = vec![
            "-NoProfile".to_string(),
            "-ExecutionPolicy".to_string(),
            "Bypass".to_string(),
            "-File".to_string(),
            child_script.to_string_lossy().into_owned(),
            "-Marker".to_string(),
            marker.to_string_lossy().into_owned(),
        ];
        let mut child = Command::new("powershell.exe")
            .args([
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                CONTAINED_UPDATER_WRAPPER,
            ])
            .env("HERMES_INTERNAL_UPDATE_JOB_GATE", &startup_gate.path)
            .env("HERMES_INTERNAL_UPDATE_PROGRAM", "powershell.exe")
            .env(
                "HERMES_INTERNAL_UPDATE_ARGS_JSON",
                serde_json::to_string(&descendant_args).unwrap(),
            )
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn gated updater wrapper");
        updater_job
            .assign(&child)
            .expect("assign wrapper before releasing its child gate");
        startup_gate
            .release()
            .expect("release contained startup gate");
        let wrapper_pid = child.id().expect("wrapper pid");
        let deadline = Instant::now() + Duration::from_secs(5);
        let mut observed = false;
        while Instant::now() < deadline {
            match guard.observe_child_transfer(wrapper_pid, |owner_pid| {
                updater_job.contains_pid(owner_pid)
            }) {
                Ok(ChildLeaseObservation::ChildOwned) => {
                    observed = true;
                    break;
                }
                Ok(ChildLeaseObservation::ParentOwned) => {
                    tokio::time::sleep(Duration::from_millis(25)).await;
                }
                Ok(ChildLeaseObservation::ParentReturned) => break,
                Err(_) => tokio::time::sleep(Duration::from_millis(25)).await,
            }
        }
        assert!(
            observed,
            "the contained descendant must positively adopt the lease"
        );
        assert_ne!(
            guard.transferred_to,
            Some(wrapper_pid),
            "the fixture models a console launcher whose Python-like child owns the lease"
        );
        let status = child
            .wait()
            .await
            .expect("wait for contained updater wrapper");
        assert!(status.success());
        let deadline = Instant::now() + Duration::from_secs(3);
        while Instant::now() < deadline {
            if matches!(
                guard.observe_child_transfer(wrapper_pid, |owner_pid| {
                    updater_job.contains_pid(owner_pid)
                }),
                Ok(ChildLeaseObservation::ParentReturned)
            ) {
                break;
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
        guard
            .require_parent_return()
            .expect("the contained child must return its lease before success");
        guard.complete().expect("parent releases returned lease");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn update_manifest_leads_with_handoff_and_gates_install() {
        let base = update_stages(false);
        assert_eq!(
            base.first().map(|s| s.name.as_str()),
            Some("handoff"),
            "the lock-wait must surface as the first visible step"
        );
        assert!(
            base.iter().any(|s| s.name == "update") && base.iter().any(|s| s.name == "rebuild"),
            "update + rebuild remain distinct stages"
        );
        assert!(
            base.iter().all(|s| s.name != "install"),
            "no app-swap stage unless an install target was passed"
        );

        let with_install = update_stages(true);
        assert_eq!(
            with_install.last().map(|s| s.name.as_str()),
            Some("install"),
            "the macOS app-swap is the final stage when present"
        );
        assert_eq!(
            with_install.len(),
            base.len() + 1,
            "include_install adds exactly one stage"
        );
    }

    #[test]
    fn rebuild_retries_only_on_failure() {
        assert!(
            !rebuild_needs_retry(Some(0)),
            "a clean rebuild must not retry"
        );
        assert!(
            rebuild_needs_retry(Some(1)),
            "a failed rebuild retries once"
        );
        assert!(
            rebuild_needs_retry(None),
            "a killed/signalled rebuild (no exit code) retries once"
        );
    }

    #[test]
    fn parses_only_app_targets() {
        assert_eq!(
            target_app_from_args(["--update", "--target-app", "/Applications/Hermes.app"]),
            Some(PathBuf::from("/Applications/Hermes.app"))
        );
        assert_eq!(
            target_app_from_args(["--target-app", "/tmp/not-an-app"]),
            None
        );
    }

    // Helpers for the swap tests: make a throwaway dir tree we can rename.
    fn unique_tmp_dir(tag: &str) -> PathBuf {
        let base = std::env::temp_dir().join(format!(
            "hermes-swap-test-{tag}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&base).unwrap();
        base
    }

    fn write_marker(dir: &Path, contents: &str) {
        std::fs::create_dir_all(dir).unwrap();
        std::fs::write(dir.join("marker.txt"), contents).unwrap();
    }

    #[tokio::test]
    async fn swap_installs_new_bundle_and_cleans_up() {
        let base = unique_tmp_dir("ok");
        let target = base.join("Hermes.app");
        let tmp = base.join("Hermes.app.hermes-update-new");
        let old = base.join("Hermes.app.hermes-update-old");
        write_marker(&target, "OLD");
        write_marker(&tmp, "NEW");

        swap_in_new_bundle(&tmp, &target, &old).await.unwrap();

        // New bundle is now at target; staging + backup dirs are gone.
        assert_eq!(
            std::fs::read_to_string(target.join("marker.txt")).unwrap(),
            "NEW"
        );
        assert!(!tmp.exists(), "staged copy should be cleaned up");
        assert!(!old.exists(), "backup should be cleaned up on success");
        let _ = std::fs::remove_dir_all(&base);
    }

    #[tokio::test]
    async fn swap_failure_never_leaves_target_missing() {
        // Regression guard for the catastrophic path: the move-aside of the
        // existing app fails AND the staged bundle can't be installed. The
        // buggy version deleted `target` when move-aside failed and then
        // skipped rollback, bricking the install. The fixed version must leave
        // the original app intact on disk.
        //
        // Trigger both failures deterministically:
        //  - `old` is a NON-EMPTY dir  -> rename(target, old) fails
        //  - `tmp` does not exist       -> rename(tmp, target) fails
        let base = unique_tmp_dir("fail");
        let target = base.join("Hermes.app");
        let tmp = base.join("Hermes.app.hermes-update-new"); // intentionally absent
        let old = base.join("Hermes.app.hermes-update-old");
        write_marker(&target, "OLD");
        write_marker(&old, "OCCUPIED"); // non-empty => rename(target,old) fails

        let result = swap_in_new_bundle(&tmp, &target, &old).await;

        assert!(
            result.is_err(),
            "swap should fail when neither move can complete"
        );
        assert!(
            target.exists(),
            "original app must NOT be deleted on failure"
        );
        assert_eq!(
            std::fs::read_to_string(target.join("marker.txt")).unwrap(),
            "OLD",
            "original app contents must be intact after a failed swap"
        );
        let _ = std::fs::remove_dir_all(&base);
    }

    #[tokio::test]
    async fn swap_rolls_back_when_install_step_fails() {
        // Move-aside succeeds but installing the staged bundle fails (tmp
        // absent). The original must be rolled back from `old` to `target`.
        let base = unique_tmp_dir("rollback");
        let target = base.join("Hermes.app");
        let tmp = base.join("Hermes.app.hermes-update-new"); // absent
        let old = base.join("Hermes.app.hermes-update-old");
        write_marker(&target, "OLD");

        let result = swap_in_new_bundle(&tmp, &target, &old).await;

        assert!(result.is_err());
        assert!(
            target.exists(),
            "original must be restored after failed install"
        );
        assert_eq!(
            std::fs::read_to_string(target.join("marker.txt")).unwrap(),
            "OLD"
        );
        assert!(
            !old.exists(),
            "backup should be rolled back, not left behind"
        );
        let _ = std::fs::remove_dir_all(&base);
    }
}
