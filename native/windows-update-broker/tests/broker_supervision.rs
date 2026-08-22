#![cfg(windows)]

use std::ffi::OsStr;
use std::fs::{self, File};
use std::io::{self, Read, Write};
use std::os::windows::ffi::OsStrExt;
use std::os::windows::io::AsRawHandle;
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use windows_sys::Win32::Foundation::{CloseHandle, FILETIME, HANDLE, WAIT_OBJECT_0, WAIT_TIMEOUT};
use windows_sys::Win32::System::Console::GetConsoleWindow;
use windows_sys::Win32::System::JobObjects::IsProcessInJob;
use windows_sys::Win32::System::Threading::{
    CREATE_NO_WINDOW, GetCurrentProcess, GetCurrentProcessId, GetProcessTimes, OpenProcess,
    WaitForSingleObject,
};

const MAGIC: &[u8; 8] = b"HUPDBRK1";
const VERSION: u16 = 1;
const OP_SUPERVISE_CHILD: u16 = 1;
const OP_ABORT: u16 = 2;
const OP_COMMIT: u16 = 3;
const EVENT_REJECTED: u16 = 0x8001;
const EVENT_READY: u16 = 0x8002;
const EVENT_ABORTED: u16 = 0x8003;
const EVENT_EOF_ABORTED: u16 = 0x8004;
const EVENT_COMMITTED: u16 = 0x8005;
const EVENT_COMMIT_ACCEPTED: u16 = 0x8006;
const EVENT_FAILED: u16 = 0x80ff;
const RESULT_OK: u16 = 0;
const MAX_FRAME_BYTES: usize = 65_536;

const FIELD_TIMEOUT_MS: u16 = 1;
const FIELD_RETAINED_PROCESS: u16 = 2;
const FIELD_EXECUTABLE_UTF16: u16 = 3;
const FIELD_ARGUMENT_UTF16: u16 = 4;
const FIELD_WORKING_DIRECTORY_UTF16: u16 = 5;
const FIELD_ENVIRONMENT_UTF16: u16 = 6;
const FIELD_TOTAL_RUNTIME_MS: u16 = 7;
const FIELD_CONTROL_DECISION_TIMEOUT_MS: u16 = 8;
const INVALID_PAYLOAD: u16 = 6;
const UNKNOWN_FIELD: u16 = 8;
const DUPLICATE_FIELD: u16 = 9;
const RETAINED_IDENTITY_MISMATCH: u16 = 11;
const CONTROL_DECISION_TIMED_OUT: u16 = 24;
const SYNCHRONIZE_ACCESS: u32 = 0x0010_0000;

#[derive(Debug)]
struct Response {
    event: u16,
    code: u16,
    child_pid: Option<u32>,
    child_creation_time: Option<u64>,
    active_processes: Option<u32>,
}

struct BrokerClient {
    child: Child,
    stdin: Option<ChildStdin>,
    responses: Receiver<io::Result<Response>>,
}

impl BrokerClient {
    fn start() -> Self {
        let mut command = Command::new(env!("CARGO_BIN_EXE_hermes-windows-update-broker"));
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .env("BROKER_TEST_TOXIC", "must-not-cross")
            .creation_flags(CREATE_NO_WINDOW);
        let mut child = command.spawn().expect("start broker");
        let stdin = child.stdin.take().expect("broker stdin");
        let mut stdout = child.stdout.take().expect("broker stdout");
        let (send, responses) = mpsc::channel();
        thread::spawn(move || {
            loop {
                let mut header = [0_u8; 16];
                match stdout.read_exact(&mut header) {
                    Ok(()) => {}
                    Err(error) if error.kind() == io::ErrorKind::UnexpectedEof => return,
                    Err(error) => {
                        let _ = send.send(Err(error));
                        return;
                    }
                }
                if &header[..8] != MAGIC {
                    let _ = send.send(Err(io::Error::new(io::ErrorKind::InvalidData, "bad magic")));
                    return;
                }
                let payload_len = u32::from_le_bytes(header[12..16].try_into().unwrap()) as usize;
                if payload_len + header.len() > MAX_FRAME_BYTES {
                    let _ = send.send(Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "oversized response",
                    )));
                    return;
                }
                let mut payload = vec![0_u8; payload_len];
                if let Err(error) = stdout.read_exact(&mut payload) {
                    let _ = send.send(Err(error));
                    return;
                }
                let response = parse_response(
                    u16::from_le_bytes(header[10..12].try_into().unwrap()),
                    &payload,
                );
                if send.send(response).is_err() {
                    return;
                }
            }
        });
        Self {
            child,
            stdin: Some(stdin),
            responses,
        }
    }

    fn send(&mut self, op: u16, payload: &[u8]) {
        self.stdin
            .as_mut()
            .expect("open broker stdin")
            .write_all(&frame(op, payload))
            .expect("write broker frame");
    }

    fn write_raw(&mut self, bytes: &[u8]) {
        self.stdin
            .as_mut()
            .expect("open broker stdin")
            .write_all(bytes)
            .expect("write partial broker frame");
    }

    fn response(&mut self) -> Response {
        match self.responses.recv_timeout(Duration::from_secs(10)) {
            Ok(response) => response.expect("valid broker response"),
            Err(error) => panic!(
                "broker response deadline ({error:?}); process state: {:?}",
                self.child.try_wait().expect("poll timed-out broker")
            ),
        }
    }

    fn assert_no_response(&self, duration: Duration) {
        match self.responses.recv_timeout(duration) {
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => panic!("broker response channel closed"),
            Ok(Ok(response)) => panic!("unexpected broker event {}", response.event),
            Ok(Err(error)) => panic!("invalid broker response: {error}"),
        }
    }

    fn close_input(&mut self) {
        self.stdin.take();
    }

    fn wait_for_exit(&mut self) {
        self.stdin.take();
        self.wait_for_exit_preserving_input();
    }

    fn wait_for_exit_preserving_input(&mut self) {
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            if self.child.try_wait().expect("poll broker").is_some() {
                return;
            }
            if Instant::now() >= deadline {
                self.child.kill().expect("kill stalled broker");
                let _ = self.child.wait();
                panic!("broker did not exit within the test deadline");
            }
            thread::sleep(Duration::from_millis(10));
        }
    }

    fn kill_abruptly(&mut self) {
        self.child.kill().expect("abruptly terminate broker");
        self.child.wait().expect("reap abruptly terminated broker");
        self.stdin.take();
    }
}

impl Drop for BrokerClient {
    fn drop(&mut self) {
        if self.child.try_wait().ok().flatten().is_none() {
            if let Some(stdin) = self.stdin.as_mut() {
                let _ = stdin.write_all(&frame(OP_ABORT, &[]));
            }
            self.stdin.take();
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }
}

struct TestDir(PathBuf);

impl TestDir {
    fn new() -> Self {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock")
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "hermes-windows-update-broker-test-{}-{nonce}",
            std::process::id()
        ));
        fs::create_dir(&path).expect("create test directory");
        Self(path)
    }

    fn join(&self, name: &str) -> PathBuf {
        self.0.join(name)
    }
}

struct FixtureProcess(Child);

impl FixtureProcess {
    fn waiting_for(release: &Path) -> Self {
        let mut command = Command::new(std::env::current_exe().expect("test executable"));
        command
            .args([
                "--exact",
                "fixture_waits_for_release",
                "--ignored",
                "--nocapture",
            ])
            .env_clear()
            .env("BROKER_TEST_RELEASE", release)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .creation_flags(CREATE_NO_WINDOW);
        Self(command.spawn().expect("start retained fixture"))
    }

    fn pid(&self) -> u32 {
        self.0.id()
    }

    fn handle(&self) -> HANDLE {
        self.0.as_raw_handle() as HANDLE
    }

    fn wait_for_exit(&mut self) {
        assert_eq!(
            unsafe { WaitForSingleObject(self.handle(), 10_000) },
            WAIT_OBJECT_0,
            "fixture process exit deadline"
        );
        self.0.wait().expect("reap fixture process");
    }
}

impl Drop for FixtureProcess {
    fn drop(&mut self) {
        if self.0.try_wait().ok().flatten().is_none() {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
}

impl Drop for TestDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn frame(op: u16, payload: &[u8]) -> Vec<u8> {
    let mut frame = Vec::with_capacity(16 + payload.len());
    frame.extend_from_slice(MAGIC);
    frame.extend_from_slice(&VERSION.to_le_bytes());
    frame.extend_from_slice(&op.to_le_bytes());
    frame.extend_from_slice(&(payload.len() as u32).to_le_bytes());
    frame.extend_from_slice(payload);
    frame
}

fn tlv(payload: &mut Vec<u8>, field: u16, value: &[u8]) {
    payload.extend_from_slice(&field.to_le_bytes());
    payload.extend_from_slice(&(value.len() as u32).to_le_bytes());
    payload.extend_from_slice(value);
}

fn utf16_bytes(value: &OsStr) -> Vec<u8> {
    value
        .encode_wide()
        .flat_map(u16::to_le_bytes)
        .collect::<Vec<_>>()
}

fn supervise_payload(
    executable: &Path,
    retained_pid: u32,
    retained_creation: u64,
    observation: &Path,
    fixture: &str,
    extra_environment: &[(&str, &Path)],
) -> Vec<u8> {
    let mut payload = Vec::new();
    tlv(&mut payload, FIELD_TIMEOUT_MS, &15_000_u32.to_le_bytes());
    let mut retained = Vec::with_capacity(12);
    retained.extend_from_slice(&retained_pid.to_le_bytes());
    retained.extend_from_slice(&retained_creation.to_le_bytes());
    tlv(&mut payload, FIELD_RETAINED_PROCESS, &retained);
    tlv(
        &mut payload,
        FIELD_EXECUTABLE_UTF16,
        &utf16_bytes(executable.as_os_str()),
    );
    for argument in [
        OsStr::new("--exact"),
        OsStr::new(fixture),
        OsStr::new("--ignored"),
        OsStr::new("--nocapture"),
    ] {
        tlv(&mut payload, FIELD_ARGUMENT_UTF16, &utf16_bytes(argument));
    }
    let mut observation_entry = utf16_bytes(OsStr::new("BROKER_TEST_OBSERVATION="));
    observation_entry.extend_from_slice(&utf16_bytes(observation.as_os_str()));
    tlv(&mut payload, FIELD_ENVIRONMENT_UTF16, &observation_entry);
    for (name, path) in extra_environment {
        let mut entry = utf16_bytes(OsStr::new(&format!("{name}=")));
        entry.extend_from_slice(&utf16_bytes(path.as_os_str()));
        tlv(&mut payload, FIELD_ENVIRONMENT_UTF16, &entry);
    }
    tlv(
        &mut payload,
        FIELD_TOTAL_RUNTIME_MS,
        &30_000_u32.to_le_bytes(),
    );
    tlv(
        &mut payload,
        FIELD_CONTROL_DECISION_TIMEOUT_MS,
        &2_000_u32.to_le_bytes(),
    );
    payload
}

fn commit_supervise_payload(
    executable: &Path,
    retained_pid: u32,
    retained_creation: u64,
    observation: &Path,
    child_release: &Path,
) -> Vec<u8> {
    supervise_payload(
        executable,
        retained_pid,
        retained_creation,
        observation,
        "fixture_waits_for_release",
        &[("BROKER_TEST_RELEASE", child_release)],
    )
}

fn parse_response(event: u16, payload: &[u8]) -> io::Result<Response> {
    if payload.len() != 2 && payload.len() != 18 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "bad response length",
        ));
    }
    let code = u16::from_le_bytes(payload[..2].try_into().unwrap());
    if payload.len() == 2 {
        return Ok(Response {
            event,
            code,
            child_pid: None,
            child_creation_time: None,
            active_processes: None,
        });
    }
    Ok(Response {
        event,
        code,
        child_pid: Some(u32::from_le_bytes(payload[2..6].try_into().unwrap())),
        child_creation_time: Some(u64::from_le_bytes(payload[6..14].try_into().unwrap())),
        active_processes: Some(u32::from_le_bytes(payload[14..18].try_into().unwrap())),
    })
}

fn read_one_response(mut stdout: ChildStdout) -> io::Result<(ChildStdout, Response)> {
    let mut header = [0_u8; 16];
    stdout.read_exact(&mut header)?;
    if &header[..8] != MAGIC {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "bad magic"));
    }
    let payload_len = u32::from_le_bytes(header[12..16].try_into().unwrap()) as usize;
    if payload_len + header.len() > MAX_FRAME_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "oversized response",
        ));
    }
    let mut payload = vec![0_u8; payload_len];
    stdout.read_exact(&mut payload)?;
    let response = parse_response(
        u16::from_le_bytes(header[10..12].try_into().unwrap()),
        &payload,
    )?;
    Ok((stdout, response))
}

fn read_one_response_before_deadline(
    stdout: ChildStdout,
    broker: &mut Child,
    context: &str,
) -> (ChildStdout, Response) {
    let (send, receive) = mpsc::channel();
    thread::spawn(move || {
        let _ = send.send(read_one_response(stdout));
    });
    match receive.recv_timeout(Duration::from_secs(10)) {
        Ok(Ok(value)) => value,
        other => {
            let _ = broker.kill();
            let _ = broker.wait();
            panic!("raw broker {context} failed: {other:?}");
        }
    }
}

fn filetime_value(value: FILETIME) -> u64 {
    (u64::from(value.dwHighDateTime) << 32) | u64::from(value.dwLowDateTime)
}

fn process_creation_time(handle: HANDLE) -> u64 {
    let mut created = FILETIME::default();
    let mut exited = FILETIME::default();
    let mut kernel = FILETIME::default();
    let mut user = FILETIME::default();
    // SAFETY: every output points to a valid FILETIME and the caller supplies a live process handle.
    assert_ne!(
        unsafe { GetProcessTimes(handle, &mut created, &mut exited, &mut kernel, &mut user) },
        0
    );
    filetime_value(created)
}

fn wait_for_file(path: &Path) {
    let deadline = Instant::now() + Duration::from_secs(10);
    while !path.is_file() {
        assert!(
            Instant::now() < deadline,
            "fixture did not write observation"
        );
        thread::sleep(Duration::from_millis(10));
    }
}

#[test]
fn supervised_child_starts_inside_job_hidden_and_abort_proves_zero() {
    let temp = TestDir::new();
    let observation = temp.join("observation.txt");
    // SAFETY: pseudo-handle and PID describe this live test process.
    let retained_handle = unsafe { GetCurrentProcess() };
    let retained_pid = unsafe { GetCurrentProcessId() };
    let retained_creation = process_creation_time(retained_handle);
    let executable = std::env::current_exe().expect("test executable");

    let mut broker = BrokerClient::start();
    broker.send(
        OP_SUPERVISE_CHILD,
        &supervise_payload(
            &executable,
            retained_pid,
            retained_creation,
            &observation,
            "fixture_reports_job_before_first_write",
            &[],
        ),
    );

    let ready = broker.response();
    assert_eq!(ready.event, EVENT_READY);
    assert_eq!(ready.code, RESULT_OK);
    assert_eq!(ready.active_processes, Some(1));
    wait_for_file(&observation);
    let observed = fs::read_to_string(&observation).expect("read observation");
    let fields = observed.trim().split(':').collect::<Vec<_>>();
    assert_eq!(fields.len(), 5);
    assert_eq!(fields[0].parse::<u32>().unwrap(), ready.child_pid.unwrap());
    assert_eq!(
        fields[1].parse::<u64>().unwrap(),
        ready.child_creation_time.unwrap()
    );
    assert_eq!(fields[2], "in-job");
    assert_eq!(fields[3], "no-console");
    assert_eq!(fields[4], "ambient-env-absent");

    broker.send(OP_ABORT, &[]);
    let aborted = broker.response();
    assert_eq!(aborted.event, EVENT_ABORTED);
    assert_eq!(aborted.code, RESULT_OK);
    assert_eq!(aborted.child_pid, ready.child_pid);
    assert_eq!(aborted.child_creation_time, ready.child_creation_time);
    assert_eq!(aborted.active_processes, Some(0));
    broker.wait_for_exit();
}

#[test]
fn relative_device_ads_executable_and_relative_working_directory_are_rejected_before_creation() {
    let temp = TestDir::new();
    let observation = temp.join("must-not-be-written.txt");
    let retained_handle = unsafe { GetCurrentProcess() };
    let retained_pid = unsafe { GetCurrentProcessId() };
    let retained_creation = process_creation_time(retained_handle);

    let relative_executable = Path::new("relative-child.exe");
    let mut broker = BrokerClient::start();
    broker.send(
        OP_SUPERVISE_CHILD,
        &supervise_payload(
            relative_executable,
            retained_pid,
            retained_creation,
            &observation,
            "fixture_reports_job_before_first_write",
            &[],
        ),
    );
    let rejected = broker.response();
    assert_eq!(rejected.event, EVENT_REJECTED);
    assert_eq!(rejected.code, INVALID_PAYLOAD);
    broker.wait_for_exit();

    for forbidden in [
        r"\\?\C:\child.exe",
        r"C:\child.exe:stream",
        r"C:\safe\.\child.exe",
        r"C:\safe\..\child.exe",
    ] {
        let mut broker = BrokerClient::start();
        broker.send(
            OP_SUPERVISE_CHILD,
            &supervise_payload(
                Path::new(forbidden),
                retained_pid,
                retained_creation,
                &observation,
                "fixture_reports_job_before_first_write",
                &[],
            ),
        );
        let rejected = broker.response();
        assert_eq!(rejected.event, EVENT_REJECTED);
        assert_eq!(rejected.code, INVALID_PAYLOAD);
        broker.wait_for_exit();
    }

    let executable = std::env::current_exe().expect("test executable");
    for forbidden in ["relative-directory", r"C:\safe\.\work", r"C:\safe\..\work"] {
        let mut payload = supervise_payload(
            &executable,
            retained_pid,
            retained_creation,
            &observation,
            "fixture_reports_job_before_first_write",
            &[],
        );
        tlv(
            &mut payload,
            FIELD_WORKING_DIRECTORY_UTF16,
            &utf16_bytes(OsStr::new(forbidden)),
        );
        let mut broker = BrokerClient::start();
        broker.send(OP_SUPERVISE_CHILD, &payload);
        let rejected = broker.response();
        assert_eq!(rejected.event, EVENT_REJECTED);
        assert_eq!(rejected.code, INVALID_PAYLOAD);
        broker.wait_for_exit();
    }
    assert!(!observation.exists());
}

#[test]
fn unknown_and_duplicate_tlv_fields_are_rejected_with_fixed_codes() {
    let mut unknown = Vec::new();
    tlv(&mut unknown, 0xffff, &[]);
    let mut broker = BrokerClient::start();
    broker.send(OP_SUPERVISE_CHILD, &unknown);
    let rejected = broker.response();
    assert_eq!(rejected.event, EVENT_REJECTED);
    assert_eq!(rejected.code, UNKNOWN_FIELD);
    broker.wait_for_exit();

    let mut duplicate = Vec::new();
    tlv(&mut duplicate, FIELD_TIMEOUT_MS, &15_000_u32.to_le_bytes());
    tlv(&mut duplicate, FIELD_TIMEOUT_MS, &15_000_u32.to_le_bytes());
    let mut broker = BrokerClient::start();
    broker.send(OP_SUPERVISE_CHILD, &duplicate);
    let rejected = broker.response();
    assert_eq!(rejected.event, EVENT_REJECTED);
    assert_eq!(rejected.code, DUPLICATE_FIELD);
    broker.wait_for_exit();
}

#[test]
fn case_insensitive_environment_duplicates_are_rejected() {
    let temp = TestDir::new();
    let observation = temp.join("must-not-be-written.txt");
    let retained_handle = unsafe { GetCurrentProcess() };
    let retained_pid = unsafe { GetCurrentProcessId() };
    let retained_creation = process_creation_time(retained_handle);
    let executable = std::env::current_exe().expect("test executable");
    let mut payload = supervise_payload(
        &executable,
        retained_pid,
        retained_creation,
        &observation,
        "fixture_reports_job_before_first_write",
        &[],
    );
    tlv(
        &mut payload,
        FIELD_ENVIRONMENT_UTF16,
        &utf16_bytes(OsStr::new("broker_test_observation=duplicate")),
    );

    let mut broker = BrokerClient::start();
    broker.send(OP_SUPERVISE_CHILD, &payload);
    let rejected = broker.response();
    assert_eq!(rejected.event, EVENT_REJECTED);
    assert_eq!(rejected.code, DUPLICATE_FIELD);
    broker.wait_for_exit();
    assert!(!observation.exists());
}

#[test]
fn quoted_trailing_backslash_and_empty_arguments_round_trip() {
    let temp = TestDir::new();
    let observation = temp.join("argument-boundaries.txt");
    let retained_handle = unsafe { GetCurrentProcess() };
    let retained_pid = unsafe { GetCurrentProcessId() };
    let retained_creation = process_creation_time(retained_handle);
    let executable = std::env::current_exe().expect("test executable");
    let mut payload = supervise_payload(
        &executable,
        retained_pid,
        retained_creation,
        &observation,
        "fixture_records_argument_boundaries",
        &[],
    );
    for argument in [
        OsStr::new(""),
        OsStr::new("contains \"quote"),
        OsStr::new(r"trailing\"),
    ] {
        tlv(&mut payload, FIELD_ARGUMENT_UTF16, &utf16_bytes(argument));
    }

    let mut broker = BrokerClient::start();
    broker.send(OP_SUPERVISE_CHILD, &payload);
    let ready = broker.response();
    assert_eq!(ready.event, EVENT_READY);
    wait_for_file(&observation);
    assert_eq!(
        fs::read_to_string(&observation).expect("read argument observation"),
        "|contains \"quote|trailing\\"
    );

    broker.send(OP_ABORT, &[]);
    let aborted = broker.response();
    assert_eq!(aborted.event, EVENT_ABORTED);
    assert_eq!(aborted.code, RESULT_OK);
    assert_eq!(aborted.active_processes, Some(0));
    broker.wait_for_exit();
}

#[test]
fn wrong_retained_creation_filetime_prevents_child_creation() {
    let temp = TestDir::new();
    let observation = temp.join("must-not-be-written.txt");
    let retained_handle = unsafe { GetCurrentProcess() };
    let retained_pid = unsafe { GetCurrentProcessId() };
    let retained_creation = process_creation_time(retained_handle);
    let executable = std::env::current_exe().expect("test executable");

    let mut broker = BrokerClient::start();
    broker.send(
        OP_SUPERVISE_CHILD,
        &supervise_payload(
            &executable,
            retained_pid,
            retained_creation.wrapping_add(1),
            &observation,
            "fixture_reports_job_before_first_write",
            &[],
        ),
    );
    let failed = broker.response();
    assert_eq!(failed.event, EVENT_FAILED);
    assert_eq!(failed.code, RETAINED_IDENTITY_MISMATCH);
    assert_eq!(failed.child_pid, None);
    broker.wait_for_exit();
    assert!(!observation.exists());
}

#[test]
fn closing_control_pipe_aborts_noncooperative_child_and_proves_zero() {
    let temp = TestDir::new();
    let observation = temp.join("eof-observation.txt");
    let retained_handle = unsafe { GetCurrentProcess() };
    let retained_pid = unsafe { GetCurrentProcessId() };
    let retained_creation = process_creation_time(retained_handle);
    let executable = std::env::current_exe().expect("test executable");
    let mut broker = BrokerClient::start();
    broker.send(
        OP_SUPERVISE_CHILD,
        &supervise_payload(
            &executable,
            retained_pid,
            retained_creation,
            &observation,
            "fixture_reports_job_before_first_write",
            &[],
        ),
    );
    let ready = broker.response();
    assert_eq!(ready.event, EVENT_READY);
    wait_for_file(&observation);

    broker.close_input();
    let aborted = broker.response();
    assert_eq!(aborted.event, EVENT_EOF_ABORTED);
    assert_eq!(aborted.code, RESULT_OK);
    assert_eq!(aborted.child_pid, ready.child_pid);
    assert_eq!(aborted.child_creation_time, ready.child_creation_time);
    assert_eq!(aborted.active_processes, Some(0));
    broker.wait_for_exit();
}

#[test]
fn open_control_pipe_without_a_decision_times_out_and_proves_zero() {
    let temp = TestDir::new();
    let observation = temp.join("decision-timeout-observation.txt");
    let retained_handle = unsafe { GetCurrentProcess() };
    let retained_pid = unsafe { GetCurrentProcessId() };
    let retained_creation = process_creation_time(retained_handle);
    let executable = std::env::current_exe().expect("test executable");
    let mut broker = BrokerClient::start();
    broker.send(
        OP_SUPERVISE_CHILD,
        &supervise_payload(
            &executable,
            retained_pid,
            retained_creation,
            &observation,
            "fixture_reports_job_before_first_write",
            &[],
        ),
    );
    let ready = broker.response();
    assert_eq!(ready.event, EVENT_READY);
    wait_for_file(&observation);

    let failed = broker.response();
    assert_eq!(failed.event, EVENT_FAILED);
    assert_eq!(failed.code, CONTROL_DECISION_TIMED_OUT);
    assert_eq!(failed.child_pid, ready.child_pid);
    assert_eq!(failed.active_processes, Some(0));
    broker.wait_for_exit_preserving_input();
}

#[test]
fn partial_control_header_with_pipe_open_times_out_and_proves_zero() {
    let temp = TestDir::new();
    let observation = temp.join("partial-header-observation.txt");
    let retained_handle = unsafe { GetCurrentProcess() };
    let retained_pid = unsafe { GetCurrentProcessId() };
    let retained_creation = process_creation_time(retained_handle);
    let executable = std::env::current_exe().expect("test executable");
    let mut broker = BrokerClient::start();
    broker.send(
        OP_SUPERVISE_CHILD,
        &supervise_payload(
            &executable,
            retained_pid,
            retained_creation,
            &observation,
            "fixture_reports_job_before_first_write",
            &[],
        ),
    );
    let ready = broker.response();
    assert_eq!(ready.event, EVENT_READY);
    wait_for_file(&observation);
    broker.write_raw(&frame(OP_ABORT, &[])[..8]);

    let failed = broker.response();
    assert_eq!(failed.event, EVENT_FAILED);
    assert_eq!(failed.code, CONTROL_DECISION_TIMED_OUT);
    assert_eq!(failed.child_pid, ready.child_pid);
    assert_eq!(failed.active_processes, Some(0));
    broker.wait_for_exit_preserving_input();
}

#[test]
fn partial_control_payload_with_pipe_open_times_out_and_proves_zero() {
    let temp = TestDir::new();
    let observation = temp.join("partial-payload-observation.txt");
    let retained_handle = unsafe { GetCurrentProcess() };
    let retained_pid = unsafe { GetCurrentProcessId() };
    let retained_creation = process_creation_time(retained_handle);
    let executable = std::env::current_exe().expect("test executable");
    let mut broker = BrokerClient::start();
    broker.send(
        OP_SUPERVISE_CHILD,
        &supervise_payload(
            &executable,
            retained_pid,
            retained_creation,
            &observation,
            "fixture_reports_job_before_first_write",
            &[],
        ),
    );
    let ready = broker.response();
    assert_eq!(ready.event, EVENT_READY);
    wait_for_file(&observation);
    let partial = frame(OP_ABORT, &[0_u8; 10]);
    broker.write_raw(&partial[..17]);

    let failed = broker.response();
    assert_eq!(failed.event, EVENT_FAILED);
    assert_eq!(failed.code, CONTROL_DECISION_TIMED_OUT);
    assert_eq!(failed.child_pid, ready.child_pid);
    assert_eq!(failed.active_processes, Some(0));
    broker.wait_for_exit_preserving_input();
}

#[test]
fn decision_timeout_kills_nested_delayed_writer_and_prevents_late_mutation() {
    let temp = TestDir::new();
    let observation = temp.join("decision-nested-observation.txt");
    let delayed_marker = temp.join("decision-must-not-exist.txt");
    let retained_handle = unsafe { GetCurrentProcess() };
    let retained_pid = unsafe { GetCurrentProcessId() };
    let retained_creation = process_creation_time(retained_handle);
    let executable = std::env::current_exe().expect("test executable");
    let mut broker = BrokerClient::start();
    broker.send(
        OP_SUPERVISE_CHILD,
        &supervise_payload(
            &executable,
            retained_pid,
            retained_creation,
            &observation,
            "fixture_spawns_delayed_writer",
            &[("BROKER_TEST_DELAYED_MARKER", &delayed_marker)],
        ),
    );
    let ready = broker.response();
    assert_eq!(ready.event, EVENT_READY);
    wait_for_file(&observation);

    let failed = broker.response();
    assert_eq!(failed.event, EVENT_FAILED);
    assert_eq!(failed.code, CONTROL_DECISION_TIMED_OUT);
    assert_eq!(failed.active_processes, Some(0));
    broker.wait_for_exit_preserving_input();
    thread::sleep(Duration::from_millis(7_500));
    assert!(
        !delayed_marker.exists(),
        "a descendant mutated disk after decision-timeout Job-zero proof"
    );
}

#[test]
fn abort_kills_nested_delayed_writer_and_no_mutation_occurs_after_seven_seconds() {
    let temp = TestDir::new();
    let observation = temp.join("nested-observation.txt");
    let delayed_marker = temp.join("must-not-exist.txt");
    let retained_handle = unsafe { GetCurrentProcess() };
    let retained_pid = unsafe { GetCurrentProcessId() };
    let retained_creation = process_creation_time(retained_handle);
    let executable = std::env::current_exe().expect("test executable");
    let mut broker = BrokerClient::start();
    broker.send(
        OP_SUPERVISE_CHILD,
        &supervise_payload(
            &executable,
            retained_pid,
            retained_creation,
            &observation,
            "fixture_spawns_delayed_writer",
            &[("BROKER_TEST_DELAYED_MARKER", &delayed_marker)],
        ),
    );
    let ready = broker.response();
    assert_eq!(ready.event, EVENT_READY);
    wait_for_file(&observation);
    assert_eq!(
        fs::read_to_string(&observation).expect("read nested observation"),
        "nested-started"
    );

    broker.send(OP_ABORT, &[]);
    let aborted = broker.response();
    assert_eq!(aborted.event, EVENT_ABORTED);
    assert_eq!(aborted.code, RESULT_OK);
    assert_eq!(aborted.active_processes, Some(0));
    broker.wait_for_exit();
    thread::sleep(Duration::from_millis(7_500));
    assert!(
        !delayed_marker.exists(),
        "a contained descendant mutated disk after terminal Job-zero proof"
    );
}

#[test]
fn commit_waits_for_exact_retained_process_after_supervised_job_is_empty() {
    let temp = TestDir::new();
    let observation = temp.join("commit-retained-observation.txt");
    let child_release = temp.join("release-supervised-child");
    let retained_release = temp.join("release-retained-child");
    let mut retained = FixtureProcess::waiting_for(&retained_release);
    let retained_creation = process_creation_time(retained.handle());
    let executable = std::env::current_exe().expect("test executable");
    let mut broker = BrokerClient::start();
    broker.send(
        OP_SUPERVISE_CHILD,
        &commit_supervise_payload(
            &executable,
            retained.pid(),
            retained_creation,
            &observation,
            &child_release,
        ),
    );
    let ready = broker.response();
    assert_eq!(ready.event, EVENT_READY);
    wait_for_file(&observation);
    broker.send(OP_COMMIT, &[]);
    let accepted = broker.response();
    assert_eq!(accepted.event, EVENT_COMMIT_ACCEPTED);
    assert_eq!(accepted.code, RESULT_OK);
    assert_eq!(accepted.child_pid, ready.child_pid);
    assert_eq!(accepted.child_creation_time, ready.child_creation_time);
    broker.close_input();

    fs::write(&child_release, b"release").expect("release supervised child");
    let child_handle = unsafe { OpenProcess(SYNCHRONIZE_ACCESS, 0, ready.child_pid.unwrap()) };
    assert!(!child_handle.is_null(), "open supervised process");
    assert_eq!(
        unsafe { WaitForSingleObject(child_handle, 10_000) },
        WAIT_OBJECT_0,
        "supervised process natural exit deadline"
    );
    unsafe { CloseHandle(child_handle) };
    broker.assert_no_response(Duration::from_secs(2));

    fs::write(&retained_release, b"release").expect("release retained child");
    retained.wait_for_exit();
    let committed = broker.response();
    assert_eq!(committed.event, EVENT_COMMITTED);
    assert_eq!(committed.code, RESULT_OK);
    assert_eq!(committed.child_pid, ready.child_pid);
    assert_eq!(committed.child_creation_time, ready.child_creation_time);
    assert_eq!(committed.active_processes, Some(0));
    broker.wait_for_exit();
}

#[test]
fn commit_waits_for_natural_job_zero_after_exact_retained_process_exits() {
    let temp = TestDir::new();
    let observation = temp.join("commit-job-observation.txt");
    let child_release = temp.join("release-supervised-child");
    let retained_release = temp.join("release-retained-child");
    let mut retained = FixtureProcess::waiting_for(&retained_release);
    let retained_creation = process_creation_time(retained.handle());
    let executable = std::env::current_exe().expect("test executable");
    let mut broker = BrokerClient::start();
    broker.send(
        OP_SUPERVISE_CHILD,
        &commit_supervise_payload(
            &executable,
            retained.pid(),
            retained_creation,
            &observation,
            &child_release,
        ),
    );
    let ready = broker.response();
    assert_eq!(ready.event, EVENT_READY);
    wait_for_file(&observation);
    broker.send(OP_COMMIT, &[]);
    let accepted = broker.response();
    assert_eq!(accepted.event, EVENT_COMMIT_ACCEPTED);
    assert_eq!(accepted.code, RESULT_OK);
    assert_eq!(accepted.child_pid, ready.child_pid);
    assert_eq!(accepted.child_creation_time, ready.child_creation_time);
    broker.close_input();

    fs::write(&retained_release, b"release").expect("release retained child");
    retained.wait_for_exit();
    broker.assert_no_response(Duration::from_secs(2));

    fs::write(&child_release, b"release").expect("release supervised child");
    let committed = broker.response();
    assert_eq!(committed.event, EVENT_COMMITTED);
    assert_eq!(committed.code, RESULT_OK);
    assert_eq!(committed.child_pid, ready.child_pid);
    assert_eq!(committed.child_creation_time, ready.child_creation_time);
    assert_eq!(committed.active_processes, Some(0));
    broker.wait_for_exit();
}

#[test]
fn committed_child_survives_control_and_output_pipe_loss_until_natural_zero() {
    let temp = TestDir::new();
    let observation = temp.join("commit-pipe-loss-observation.txt");
    let child_release = temp.join("release-supervised-child");
    let retained_release = temp.join("release-retained-child");
    let mut retained = FixtureProcess::waiting_for(&retained_release);
    let retained_creation = process_creation_time(retained.handle());
    let executable = std::env::current_exe().expect("test executable");

    let mut command = Command::new(env!("CARGO_BIN_EXE_hermes-windows-update-broker"));
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .creation_flags(CREATE_NO_WINDOW);
    let mut broker = command.spawn().expect("start raw broker");
    let mut stdin = broker.stdin.take().expect("raw broker stdin");
    let stdout = broker.stdout.take().expect("raw broker stdout");
    stdin
        .write_all(&frame(
            OP_SUPERVISE_CHILD,
            &commit_supervise_payload(
                &executable,
                retained.pid(),
                retained_creation,
                &observation,
                &child_release,
            ),
        ))
        .expect("write supervise frame");
    let (stdout, ready) = read_one_response_before_deadline(stdout, &mut broker, "READY");
    assert_eq!(ready.event, EVENT_READY);
    wait_for_file(&observation);
    let child_handle = unsafe { OpenProcess(SYNCHRONIZE_ACCESS, 0, ready.child_pid.unwrap()) };
    assert!(!child_handle.is_null(), "open supervised process");

    stdin
        .write_all(&frame(OP_COMMIT, &[]))
        .expect("write commit frame");
    let (stdout, accepted) =
        read_one_response_before_deadline(stdout, &mut broker, "COMMIT_ACCEPTED");
    assert_eq!(accepted.event, EVENT_COMMIT_ACCEPTED);
    assert_eq!(accepted.code, RESULT_OK);
    assert_eq!(accepted.child_pid, ready.child_pid);
    assert_eq!(accepted.child_creation_time, ready.child_creation_time);
    drop(stdin);
    drop(stdout);
    fs::write(&retained_release, b"release").expect("release retained child");
    retained.wait_for_exit();
    assert_eq!(
        unsafe { WaitForSingleObject(child_handle, 2_000) },
        WAIT_TIMEOUT,
        "pipe loss after COMMIT must not terminate the valid updater child"
    );

    fs::write(&child_release, b"release").expect("release supervised child");
    assert_eq!(
        unsafe { WaitForSingleObject(child_handle, 10_000) },
        WAIT_OBJECT_0,
        "supervised process natural exit deadline"
    );
    unsafe { CloseHandle(child_handle) };
    let deadline = Instant::now() + Duration::from_secs(10);
    let status = loop {
        if let Some(status) = broker.try_wait().expect("poll raw broker") {
            break status;
        }
        assert!(Instant::now() < deadline, "raw broker exit deadline");
        thread::sleep(Duration::from_millis(10));
    };
    assert!(status.success());
}

#[test]
fn broker_death_after_commit_acceptance_kills_nested_delayed_writer() {
    let temp = TestDir::new();
    let observation = temp.join("accepted-crash-observation.txt");
    let delayed_marker = temp.join("accepted-crash-must-not-exist.txt");
    let retained_handle = unsafe { GetCurrentProcess() };
    let retained_pid = unsafe { GetCurrentProcessId() };
    let retained_creation = process_creation_time(retained_handle);
    let executable = std::env::current_exe().expect("test executable");
    let mut broker = BrokerClient::start();
    broker.send(
        OP_SUPERVISE_CHILD,
        &supervise_payload(
            &executable,
            retained_pid,
            retained_creation,
            &observation,
            "fixture_spawns_delayed_writer",
            &[("BROKER_TEST_DELAYED_MARKER", &delayed_marker)],
        ),
    );
    let ready = broker.response();
    assert_eq!(ready.event, EVENT_READY);
    wait_for_file(&observation);

    broker.send(OP_COMMIT, &[]);
    let accepted = broker.response();
    assert_eq!(accepted.event, EVENT_COMMIT_ACCEPTED);
    assert_eq!(accepted.code, RESULT_OK);
    assert_eq!(accepted.child_pid, ready.child_pid);
    assert_eq!(accepted.child_creation_time, ready.child_creation_time);
    broker.kill_abruptly();

    thread::sleep(Duration::from_millis(7_500));
    assert!(
        !delayed_marker.exists(),
        "kill-on-close must remain armed after COMMIT_ACCEPTED"
    );
}

#[test]
#[ignore = "real child fixture invoked by the broker integration tests"]
fn fixture_reports_job_before_first_write() {
    let mut in_job = 0;
    // SAFETY: the process pseudo-handle and result pointer are valid; NULL asks about any Job.
    assert_ne!(
        unsafe { IsProcessInJob(GetCurrentProcess(), std::ptr::null_mut(), &mut in_job) },
        0
    );
    assert_ne!(
        in_job, 0,
        "child must be assigned before its first instruction"
    );
    let pid = unsafe { GetCurrentProcessId() };
    let creation = process_creation_time(unsafe { GetCurrentProcess() });
    // SAFETY: GetConsoleWindow has no preconditions.
    let no_console = unsafe { GetConsoleWindow() }.is_null();
    let observation = std::env::var_os("BROKER_TEST_OBSERVATION").expect("observation path");
    let ambient_absent = std::env::var_os("BROKER_TEST_TOXIC").is_none();
    let mut file = File::create(observation).expect("create observation");
    writeln!(
        file,
        "{pid}:{creation}:{}:{}:{}",
        if in_job != 0 { "in-job" } else { "not-in-job" },
        if no_console {
            "no-console"
        } else {
            "has-console"
        },
        if ambient_absent {
            "ambient-env-absent"
        } else {
            "ambient-env-leaked"
        }
    )
    .expect("write observation");
    file.flush().expect("flush observation");
    loop {
        thread::park_timeout(Duration::from_secs(60));
    }
}

#[test]
#[ignore = "real nested child fixture invoked by the broker integration tests"]
fn fixture_spawns_delayed_writer() {
    let executable = std::env::current_exe().expect("test executable");
    let mut command = Command::new(executable);
    command
        .args([
            "--exact",
            "fixture_delayed_writer",
            "--ignored",
            "--nocapture",
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .creation_flags(CREATE_NO_WINDOW);
    let mut nested = command.spawn().expect("start delayed nested writer");
    let observation = std::env::var_os("BROKER_TEST_OBSERVATION").expect("observation path");
    fs::write(observation, "nested-started").expect("write nested observation");
    nested.wait().expect("wait for delayed nested writer");
}

#[test]
#[ignore = "real delayed writer fixture invoked by the broker integration tests"]
fn fixture_delayed_writer() {
    let mut in_job = 0;
    assert_ne!(
        unsafe { IsProcessInJob(GetCurrentProcess(), std::ptr::null_mut(), &mut in_job) },
        0
    );
    assert_ne!(in_job, 0, "nested writer must inherit Job membership");
    thread::sleep(Duration::from_secs(2));
    let marker = std::env::var_os("BROKER_TEST_DELAYED_MARKER").expect("delayed marker path");
    fs::write(marker, "escaped").expect("write delayed marker");
}

#[test]
#[ignore = "real release-controlled fixture invoked by the broker integration tests"]
fn fixture_waits_for_release() {
    if let Some(observation) = std::env::var_os("BROKER_TEST_OBSERVATION") {
        fs::write(observation, "waiting").expect("write waiting observation");
    }
    let release = PathBuf::from(std::env::var_os("BROKER_TEST_RELEASE").expect("release path"));
    while !release.is_file() {
        thread::sleep(Duration::from_millis(10));
    }
}

#[test]
#[ignore = "real argument probe invoked by the broker integration tests"]
fn fixture_records_argument_boundaries() {
    let arguments = std::env::args().skip(5).collect::<Vec<_>>();
    let observation = std::env::var_os("BROKER_TEST_OBSERVATION").expect("observation path");
    fs::write(observation, arguments.join("|")).expect("write argument observation");
}
