use std::collections::BTreeSet;
use std::ffi::{OsString, c_void};
use std::io::{self, Write};
use std::mem::{size_of, zeroed};
use std::os::windows::ffi::{OsStrExt, OsStringExt};
use std::path::{Component, Path, Prefix};
use std::ptr::{null, null_mut};
use std::sync::mpsc::{self, RecvTimeoutError};
use std::thread;
use std::time::{Duration, Instant};

use windows_sys::Win32::Foundation::{CloseHandle, FILETIME, HANDLE, WAIT_OBJECT_0, WAIT_TIMEOUT};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JOBOBJECT_BASIC_ACCOUNTING_INFORMATION, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JobObjectBasicAccountingInformation, JobObjectExtendedLimitInformation,
    QueryInformationJobObject, SetInformationJobObject, TerminateJobObject,
};
use windows_sys::Win32::System::Threading::{
    CREATE_NO_WINDOW, CREATE_SUSPENDED, CREATE_UNICODE_ENVIRONMENT, CreateProcessW,
    GetProcessTimes, OpenProcess, PROCESS_INFORMATION, PROCESS_QUERY_LIMITED_INFORMATION,
    ResumeThread, STARTUPINFOW, TerminateProcess, WaitForSingleObject,
};

use crate::protocol::{self, Event, ProcessIdentity, ResultCode};

const FIELD_TIMEOUT_MS: u16 = 1;
const FIELD_RETAINED_PROCESS: u16 = 2;
const FIELD_EXECUTABLE_UTF16: u16 = 3;
const FIELD_ARGUMENT_UTF16: u16 = 4;
const FIELD_WORKING_DIRECTORY_UTF16: u16 = 5;
const FIELD_ENVIRONMENT_UTF16: u16 = 6;
const FIELD_TOTAL_RUNTIME_MS: u16 = 7;
const FIELD_CONTROL_DECISION_TIMEOUT_MS: u16 = 8;

const MIN_TIMEOUT_MS: u32 = 100;
const MAX_TIMEOUT_MS: u32 = 120_000;
const MIN_TOTAL_RUNTIME_MS: u32 = 1_000;
const MAX_TOTAL_RUNTIME_MS: u32 = 86_400_000;
const MAX_ARGUMENTS: usize = 64;
const MAX_ENVIRONMENT_ENTRIES: usize = 128;
const MAX_COMMAND_LINE_UNITS: usize = 32_766;
const SYNCHRONIZE_ACCESS: u32 = 0x0010_0000;
const BROKER_ABORT_EXIT_CODE: u32 = 0x4855_5044;

struct OwnedHandle(HANDLE);

impl OwnedHandle {
    fn new(handle: HANDLE) -> Option<Self> {
        (!handle.is_null()).then_some(Self(handle))
    }

    fn raw(&self) -> HANDLE {
        self.0
    }
}

impl Drop for OwnedHandle {
    fn drop(&mut self) {
        // SAFETY: OwnedHandle is constructed only from a non-NULL owned Win32 handle.
        unsafe { CloseHandle(self.0) };
    }
}

struct Request {
    abort_drain_timeout_ms: u32,
    control_decision_timeout_ms: u32,
    total_runtime_ms: u32,
    retained_pid: u32,
    retained_creation_time: u64,
    executable: Vec<u16>,
    arguments: Vec<Vec<u16>>,
    working_directory: Option<Vec<u16>>,
    environment: Vec<Vec<u16>>,
}

struct Session {
    job: OwnedHandle,
    child: OwnedHandle,
    retained: OwnedHandle,
    identity: ProcessIdentity,
    abort_drain_timeout_ms: u32,
    control_decision_timeout_ms: u32,
    total_runtime_ms: u32,
}

fn read_u32(value: &[u8]) -> Result<u32, ResultCode> {
    value
        .try_into()
        .map(u32::from_le_bytes)
        .map_err(|_| ResultCode::InvalidPayload)
}

fn read_u64(value: &[u8]) -> Result<u64, ResultCode> {
    value
        .try_into()
        .map(u64::from_le_bytes)
        .map_err(|_| ResultCode::InvalidPayload)
}

fn utf16_value(value: &[u8], allow_empty: bool) -> Result<Vec<u16>, ResultCode> {
    if !value.len().is_multiple_of(2) || (!allow_empty && value.is_empty()) {
        return Err(ResultCode::InvalidPayload);
    }
    let units = value
        .chunks_exact(2)
        .map(|pair| u16::from_le_bytes([pair[0], pair[1]]))
        .collect::<Vec<_>>();
    if units.contains(&0) || units.len() >= MAX_COMMAND_LINE_UNITS {
        return Err(ResultCode::InvalidPayload);
    }
    Ok(units)
}

fn validate_absolute_canonical_path(units: &[u16], executable: bool) -> Result<(), ResultCode> {
    const BACKSLASH: u16 = b'\\' as u16;
    const FORWARD_SLASH: u16 = b'/' as u16;
    const COLON: u16 = b':' as u16;
    const DOT: u16 = b'.' as u16;
    const SPACE: u16 = b' ' as u16;

    if units.contains(&FORWARD_SLASH)
        || units.starts_with(&[BACKSLASH, BACKSLASH, b'?' as u16, BACKSLASH])
        || units.starts_with(&[BACKSLASH, BACKSLASH, DOT, BACKSLASH])
        || units.starts_with(&[BACKSLASH, b'?' as u16, b'?' as u16, BACKSLASH])
    {
        return Err(ResultCode::InvalidPayload);
    }
    if units
        .split(|unit| *unit == BACKSLASH)
        .any(|component| component == [DOT] || component == [DOT, DOT])
    {
        return Err(ResultCode::InvalidPayload);
    }
    let drive_path = units.len() >= 3
        && ((b'A' as u16..=b'Z' as u16).contains(&units[0])
            || (b'a' as u16..=b'z' as u16).contains(&units[0]))
        && units[1] == COLON
        && units[2] == BACKSLASH;
    let unc_path =
        units.len() >= 5 && units[0] == BACKSLASH && units[1] == BACKSLASH && units[2] != BACKSLASH;
    if !drive_path && !unc_path {
        return Err(ResultCode::InvalidPayload);
    }
    for (index, unit) in units.iter().enumerate() {
        if *unit == COLON && !(drive_path && index == 1) {
            return Err(ResultCode::InvalidPayload);
        }
        if *unit == BACKSLASH
            && index > if unc_path { 1 } else { 2 }
            && units[index - 1] == BACKSLASH
        {
            return Err(ResultCode::InvalidPayload);
        }
    }
    let os_path = OsString::from_wide(units);
    let path = Path::new(&os_path);
    if !path.is_absolute() {
        return Err(ResultCode::InvalidPayload);
    }
    let mut normal_components = 0_usize;
    for component in path.components() {
        match component {
            Component::Prefix(prefix) => match prefix.kind() {
                Prefix::Disk(_) | Prefix::UNC(_, _) => {}
                _ => return Err(ResultCode::InvalidPayload),
            },
            Component::RootDir => {}
            Component::Normal(value) => {
                normal_components += 1;
                let value = value.encode_wide().collect::<Vec<_>>();
                if value.is_empty()
                    || value
                        .last()
                        .is_some_and(|unit| matches!(*unit, DOT | SPACE))
                    || value == [DOT]
                    || value == [DOT, DOT]
                {
                    return Err(ResultCode::InvalidPayload);
                }
            }
            Component::CurDir | Component::ParentDir => return Err(ResultCode::InvalidPayload),
        }
    }
    if executable && (normal_components == 0 || units.last() == Some(&BACKSLASH)) {
        return Err(ResultCode::InvalidPayload);
    }
    if !executable && units.last() == Some(&BACKSLASH) && !(drive_path && units.len() == 3) {
        return Err(ResultCode::InvalidPayload);
    }
    Ok(())
}

fn environment_key(entry: &[u16]) -> Result<Vec<u8>, ResultCode> {
    let separator = entry
        .iter()
        .position(|unit| *unit == u16::from(b'='))
        .ok_or(ResultCode::InvalidPayload)?;
    if separator == 0 {
        return Err(ResultCode::InvalidPayload);
    }
    let mut key = Vec::with_capacity(separator);
    for unit in &entry[..separator] {
        let byte = u8::try_from(*unit).map_err(|_| ResultCode::InvalidPayload)?;
        if !(byte == b'_' || byte.is_ascii_alphanumeric()) {
            return Err(ResultCode::InvalidPayload);
        }
        key.push(byte.to_ascii_lowercase());
    }
    Ok(key)
}

fn parse_request(payload: &[u8]) -> Result<Request, ResultCode> {
    let mut offset = 0_usize;
    let mut timeout_ms = None;
    let mut control_decision_timeout_ms = None;
    let mut total_runtime_ms = None;
    let mut retained = None;
    let mut executable = None;
    let mut arguments = Vec::new();
    let mut working_directory = None;
    let mut environment = Vec::new();
    let mut environment_keys = BTreeSet::new();

    while offset < payload.len() {
        if payload.len() - offset < 6 {
            return Err(ResultCode::TrailingBytes);
        }
        let field = u16::from_le_bytes(payload[offset..offset + 2].try_into().unwrap());
        let length =
            u32::from_le_bytes(payload[offset + 2..offset + 6].try_into().unwrap()) as usize;
        offset += 6;
        let end = offset
            .checked_add(length)
            .filter(|end| *end <= payload.len())
            .ok_or(ResultCode::TrailingBytes)?;
        let value = &payload[offset..end];
        offset = end;
        match field {
            FIELD_TIMEOUT_MS => {
                if timeout_ms.is_some() {
                    return Err(ResultCode::DuplicateField);
                }
                let timeout = read_u32(value)?;
                if !(MIN_TIMEOUT_MS..=MAX_TIMEOUT_MS).contains(&timeout) {
                    return Err(ResultCode::InvalidPayload);
                }
                timeout_ms = Some(timeout);
            }
            FIELD_RETAINED_PROCESS => {
                if retained.is_some() || value.len() != 12 {
                    return Err(if retained.is_some() {
                        ResultCode::DuplicateField
                    } else {
                        ResultCode::InvalidPayload
                    });
                }
                let pid = read_u32(&value[..4])?;
                let creation_time = read_u64(&value[4..])?;
                if pid == 0 || creation_time == 0 {
                    return Err(ResultCode::InvalidPayload);
                }
                retained = Some((pid, creation_time));
            }
            FIELD_EXECUTABLE_UTF16 => {
                if executable.is_some() {
                    return Err(ResultCode::DuplicateField);
                }
                let path = utf16_value(value, false)?;
                validate_absolute_canonical_path(&path, true)?;
                executable = Some(path);
            }
            FIELD_ARGUMENT_UTF16 => {
                if arguments.len() >= MAX_ARGUMENTS {
                    return Err(ResultCode::InvalidPayload);
                }
                arguments.push(utf16_value(value, true)?);
            }
            FIELD_WORKING_DIRECTORY_UTF16 => {
                if working_directory.is_some() {
                    return Err(ResultCode::DuplicateField);
                }
                let path = utf16_value(value, false)?;
                validate_absolute_canonical_path(&path, false)?;
                working_directory = Some(path);
            }
            FIELD_ENVIRONMENT_UTF16 => {
                if environment.len() >= MAX_ENVIRONMENT_ENTRIES {
                    return Err(ResultCode::InvalidPayload);
                }
                let entry = utf16_value(value, false)?;
                if !environment_keys.insert(environment_key(&entry)?) {
                    return Err(ResultCode::DuplicateField);
                }
                environment.push(entry);
            }
            FIELD_TOTAL_RUNTIME_MS => {
                if total_runtime_ms.is_some() {
                    return Err(ResultCode::DuplicateField);
                }
                let timeout = read_u32(value)?;
                if !(MIN_TOTAL_RUNTIME_MS..=MAX_TOTAL_RUNTIME_MS).contains(&timeout) {
                    return Err(ResultCode::InvalidPayload);
                }
                total_runtime_ms = Some(timeout);
            }
            FIELD_CONTROL_DECISION_TIMEOUT_MS => {
                if control_decision_timeout_ms.is_some() {
                    return Err(ResultCode::DuplicateField);
                }
                let timeout = read_u32(value)?;
                if !(MIN_TIMEOUT_MS..=MAX_TIMEOUT_MS).contains(&timeout) {
                    return Err(ResultCode::InvalidPayload);
                }
                control_decision_timeout_ms = Some(timeout);
            }
            _ => return Err(ResultCode::UnknownField),
        }
    }

    let (retained_pid, retained_creation_time) = retained.ok_or(ResultCode::InvalidPayload)?;
    Ok(Request {
        abort_drain_timeout_ms: timeout_ms.ok_or(ResultCode::InvalidPayload)?,
        control_decision_timeout_ms: control_decision_timeout_ms
            .ok_or(ResultCode::InvalidPayload)?,
        total_runtime_ms: total_runtime_ms.ok_or(ResultCode::InvalidPayload)?,
        retained_pid,
        retained_creation_time,
        executable: executable.ok_or(ResultCode::InvalidPayload)?,
        arguments,
        working_directory,
        environment,
    })
}

fn filetime_value(value: FILETIME) -> u64 {
    (u64::from(value.dwHighDateTime) << 32) | u64::from(value.dwLowDateTime)
}

fn process_creation_time(handle: HANDLE) -> Result<u64, ResultCode> {
    let mut created = FILETIME::default();
    let mut exited = FILETIME::default();
    let mut kernel = FILETIME::default();
    let mut user = FILETIME::default();
    // SAFETY: output pointers are valid and handle is a live process handle.
    if unsafe { GetProcessTimes(handle, &mut created, &mut exited, &mut kernel, &mut user) } == 0 {
        return Err(ResultCode::RetainedOpenFailed);
    }
    Ok(filetime_value(created))
}

fn open_retained(request: &Request) -> Result<OwnedHandle, ResultCode> {
    // SAFETY: requested access is query + synchronize and PID is validated nonzero.
    let handle = unsafe {
        OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE_ACCESS,
            0,
            request.retained_pid,
        )
    };
    let handle = OwnedHandle::new(handle).ok_or(ResultCode::RetainedOpenFailed)?;
    if process_creation_time(handle.raw())? != request.retained_creation_time {
        return Err(ResultCode::RetainedIdentityMismatch);
    }
    Ok(handle)
}

fn create_job() -> Result<OwnedHandle, ResultCode> {
    // SAFETY: NULL security attributes and NULL name create a private unnamed Job.
    let job = unsafe { CreateJobObjectW(null(), null()) };
    let job = OwnedHandle::new(job).ok_or(ResultCode::JobCreateFailed)?;
    let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    // SAFETY: the information buffer matches JobObjectExtendedLimitInformation exactly.
    if unsafe {
        SetInformationJobObject(
            job.raw(),
            JobObjectExtendedLimitInformation,
            (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
            size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        )
    } == 0
    {
        return Err(ResultCode::JobConfigureFailed);
    }
    Ok(job)
}

fn quote_argument(argument: &[u16]) -> Vec<u16> {
    let needs_quotes = argument.is_empty()
        || argument
            .iter()
            .any(|unit| matches!(*unit, 0x09 | 0x20 | 0x22));
    if !needs_quotes {
        return argument.to_vec();
    }
    let mut quoted = vec![u16::from(b'"')];
    let mut slashes = 0_usize;
    for unit in argument {
        if *unit == u16::from(b'\\') {
            slashes += 1;
            continue;
        }
        if *unit == u16::from(b'"') {
            quoted.extend(std::iter::repeat_n(u16::from(b'\\'), slashes * 2 + 1));
            quoted.push(*unit);
        } else {
            quoted.extend(std::iter::repeat_n(u16::from(b'\\'), slashes));
            quoted.push(*unit);
        }
        slashes = 0;
    }
    quoted.extend(std::iter::repeat_n(u16::from(b'\\'), slashes * 2));
    quoted.push(u16::from(b'"'));
    quoted
}

fn command_line(request: &Request) -> Result<Vec<u16>, ResultCode> {
    let mut command = quote_argument(&request.executable);
    for argument in &request.arguments {
        command.push(u16::from(b' '));
        command.extend(quote_argument(argument));
    }
    if command.len() >= MAX_COMMAND_LINE_UNITS {
        return Err(ResultCode::InvalidPayload);
    }
    command.push(0);
    Ok(command)
}

fn nul_terminated(value: &[u16]) -> Vec<u16> {
    let mut result = value.to_vec();
    result.push(0);
    result
}

fn environment_block(entries: &[Vec<u16>]) -> Result<Vec<u16>, ResultCode> {
    let mut sorted = entries
        .iter()
        .map(|entry| Ok((environment_key(entry)?, entry)))
        .collect::<Result<Vec<_>, ResultCode>>()?;
    sorted.sort_by(|left, right| left.0.cmp(&right.0));
    let units = sorted
        .iter()
        .map(|(_, entry)| entry.len() + 1)
        .sum::<usize>()
        + 1;
    if units > MAX_COMMAND_LINE_UNITS {
        return Err(ResultCode::InvalidPayload);
    }
    let mut block = Vec::with_capacity(units.max(2));
    for (_, entry) in sorted {
        block.extend_from_slice(entry);
        block.push(0);
    }
    block.push(0);
    if block.len() == 1 {
        block.push(0);
    }
    Ok(block)
}

fn query_active_processes(job: HANDLE) -> Result<u32, ResultCode> {
    let mut accounting = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION::default();
    // SAFETY: buffer and length match JobObjectBasicAccountingInformation.
    if unsafe {
        QueryInformationJobObject(
            job,
            JobObjectBasicAccountingInformation,
            (&mut accounting as *mut JOBOBJECT_BASIC_ACCOUNTING_INFORMATION).cast(),
            size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>() as u32,
            null_mut(),
        )
    } == 0
    {
        return Err(ResultCode::JobQueryFailed);
    }
    Ok(accounting.ActiveProcesses)
}

fn terminate_unassigned_child(child: HANDLE, timeout_ms: u32) -> Result<(), ResultCode> {
    // SAFETY: child is the suspended process handle returned by CreateProcessW.
    if unsafe { TerminateProcess(child, BROKER_ABORT_EXIT_CODE) } == 0
        || unsafe { WaitForSingleObject(child, timeout_ms) } != WAIT_OBJECT_0
    {
        return Err(ResultCode::ChildTerminationFailed);
    }
    Ok(())
}

fn terminate_job_and_prove_zero(job: HANDLE, timeout_ms: u32) -> Result<u32, ResultCode> {
    // SAFETY: job is live and configured to contain all supervised descendants.
    if unsafe { TerminateJobObject(job, BROKER_ABORT_EXIT_CODE) } == 0 {
        return Err(ResultCode::JobTerminateFailed);
    }
    // SAFETY: a Job signals after all active members have terminated.
    if unsafe { WaitForSingleObject(job, timeout_ms) } != WAIT_OBJECT_0 {
        return Err(ResultCode::JobWaitTimedOut);
    }
    let active = query_active_processes(job)?;
    if active != 0 {
        return Err(ResultCode::JobNotEmpty);
    }
    Ok(active)
}

fn create_session(request: Request) -> Result<Session, ResultCode> {
    let retained = open_retained(&request)?;
    let job = create_job()?;
    let application = nul_terminated(&request.executable);
    let mut command = command_line(&request)?;
    let environment = environment_block(&request.environment)?;
    let working_directory = request
        .working_directory
        .as_ref()
        .map(|value| nul_terminated(value));
    let mut startup: STARTUPINFOW = unsafe { zeroed() };
    startup.cb = size_of::<STARTUPINFOW>() as u32;
    let mut process: PROCESS_INFORMATION = unsafe { zeroed() };
    // SAFETY: strings are NUL-terminated, command is writable, environment is
    // double-NUL-terminated, and output structs live through the call.
    if unsafe {
        CreateProcessW(
            application.as_ptr(),
            command.as_mut_ptr(),
            null(),
            null(),
            0,
            CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT,
            environment.as_ptr().cast::<c_void>(),
            working_directory
                .as_ref()
                .map_or(null(), |value| value.as_ptr()),
            &startup,
            &mut process,
        )
    } == 0
    {
        return Err(ResultCode::ChildCreateFailed);
    }
    let child = OwnedHandle::new(process.hProcess).ok_or(ResultCode::ChildCreateFailed)?;
    let thread = OwnedHandle::new(process.hThread).ok_or(ResultCode::ChildCreateFailed)?;
    let creation_time = match process_creation_time(child.raw()) {
        Ok(value) => value,
        Err(_) => {
            terminate_unassigned_child(child.raw(), request.abort_drain_timeout_ms)?;
            return Err(ResultCode::ChildCreateFailed);
        }
    };

    // SAFETY: both handles are live; the child remains suspended until assignment succeeds.
    if unsafe { AssignProcessToJobObject(job.raw(), child.raw()) } == 0 {
        terminate_unassigned_child(child.raw(), request.abort_drain_timeout_ms)?;
        return Err(ResultCode::ChildAssignFailed);
    }
    let active_processes = match query_active_processes(job.raw()) {
        Ok(1) => 1,
        Ok(_) => {
            terminate_job_and_prove_zero(job.raw(), request.abort_drain_timeout_ms)?;
            return Err(ResultCode::JobNotEmpty);
        }
        Err(code) => {
            terminate_job_and_prove_zero(job.raw(), request.abort_drain_timeout_ms)?;
            return Err(code);
        }
    };
    // SAFETY: thread is the primary suspended thread returned by CreateProcessW.
    if unsafe { ResumeThread(thread.raw()) } == u32::MAX {
        terminate_job_and_prove_zero(job.raw(), request.abort_drain_timeout_ms)?;
        return Err(ResultCode::ChildResumeFailed);
    }
    drop(thread);
    Ok(Session {
        job,
        child,
        retained,
        identity: ProcessIdentity {
            pid: process.dwProcessId,
            creation_time,
            active_processes,
        },
        abort_drain_timeout_ms: request.abort_drain_timeout_ms,
        control_decision_timeout_ms: request.control_decision_timeout_ms,
        total_runtime_ms: request.total_runtime_ms,
    })
}

fn terminate_and_prove_zero(session: &Session) -> Result<ProcessIdentity, ResultCode> {
    let active_processes =
        terminate_job_and_prove_zero(session.job.raw(), session.abort_drain_timeout_ms)?;
    Ok(ProcessIdentity {
        active_processes,
        ..session.identity
    })
}

fn remaining_wait_ms(deadline: Instant) -> Result<u32, ResultCode> {
    let remaining = deadline
        .checked_duration_since(Instant::now())
        .ok_or(ResultCode::RetainedWaitFailed)?;
    let millis = remaining.as_millis().max(1);
    u32::try_from(millis).map_err(|_| ResultCode::RetainedWaitFailed)
}

fn wait_for_committed_zero(session: &Session) -> Result<ProcessIdentity, ResultCode> {
    let deadline = Instant::now() + Duration::from_millis(u64::from(session.total_runtime_ms));
    // SAFETY: retained is the exact process handle opened and FILETIME-validated before spawn.
    if unsafe { WaitForSingleObject(session.retained.raw(), remaining_wait_ms(deadline)?) }
        != WAIT_OBJECT_0
    {
        return Err(ResultCode::RetainedWaitFailed);
    }
    let active_processes = loop {
        let active = query_active_processes(session.job.raw())?;
        if active == 0 {
            break active;
        }
        if Instant::now() >= deadline {
            return Err(ResultCode::JobWaitTimedOut);
        }
        std::thread::sleep(Duration::from_millis(10));
    };
    Ok(ProcessIdentity {
        active_processes,
        ..session.identity
    })
}

fn commit_acceptance_identity(session: &Session) -> Result<ProcessIdentity, ResultCode> {
    // SAFETY: retained is the exact process handle opened and FILETIME-validated before spawn.
    if unsafe { WaitForSingleObject(session.retained.raw(), 0) } != WAIT_TIMEOUT {
        return Err(ResultCode::RetainedWaitFailed);
    }
    Ok(ProcessIdentity {
        active_processes: query_active_processes(session.job.raw())?,
        ..session.identity
    })
}

fn write_terminal_failure<W: Write>(
    output: &mut W,
    session: &Session,
    code: ResultCode,
) -> io::Result<()> {
    match terminate_and_prove_zero(session) {
        Ok(identity) => protocol::write_response(output, Event::Failed, code, Some(identity)),
        Err(terminal_code) => protocol::write_response(output, Event::Failed, terminal_code, None),
    }
}

pub fn supervise<W: Write>(payload: Vec<u8>, output: &mut W) -> io::Result<()> {
    let request = match parse_request(&payload) {
        Ok(request) => request,
        Err(code) => return protocol::write_response(output, Event::Rejected, code, None),
    };
    let session = match create_session(request) {
        Ok(session) => session,
        Err(code) => return protocol::write_response(output, Event::Failed, code, None),
    };
    let _retained = session.retained.raw();
    let _child = session.child.raw();
    if let Err(error) =
        protocol::write_response(output, Event::Ready, ResultCode::Ok, Some(session.identity))
    {
        let _ = terminate_and_prove_zero(&session);
        return Err(error);
    }

    let (send, receive) = mpsc::sync_channel(1);
    thread::spawn(move || {
        let result = protocol::read_frame(&mut io::stdin().lock());
        let _ = send.send(result);
    });
    let control = match receive.recv_timeout(Duration::from_millis(u64::from(
        session.control_decision_timeout_ms,
    ))) {
        Ok(Ok(Some(frame))) => frame,
        Ok(Ok(None)) => {
            return match terminate_and_prove_zero(&session) {
                Ok(identity) => protocol::write_response(
                    output,
                    Event::EofAborted,
                    ResultCode::Ok,
                    Some(identity),
                ),
                Err(code) => protocol::write_response(output, Event::Failed, code, None),
            };
        }
        Ok(Err(code)) => return write_terminal_failure(output, &session, code),
        Err(RecvTimeoutError::Timeout) => {
            return write_terminal_failure(output, &session, ResultCode::ControlDecisionTimedOut);
        }
        Err(RecvTimeoutError::Disconnected) => {
            return write_terminal_failure(output, &session, ResultCode::TruncatedFrame);
        }
    };
    if !control.payload.is_empty() {
        return write_terminal_failure(output, &session, ResultCode::TrailingBytes);
    }
    match control.op {
        protocol::OP_ABORT => match terminate_and_prove_zero(&session) {
            Ok(identity) => {
                protocol::write_response(output, Event::Aborted, ResultCode::Ok, Some(identity))
            }
            Err(code) => protocol::write_response(output, Event::Failed, code, None),
        },
        protocol::OP_COMMIT => {
            let accepted_identity = match commit_acceptance_identity(&session) {
                Ok(identity) => identity,
                Err(code) => return write_terminal_failure(output, &session, code),
            };
            // Acceptance is irrevocable after the complete identity-bound COMMIT frame.
            // Delivery failure cannot turn a valid updater into a pre-commit abort.
            let _ = protocol::write_response(
                output,
                Event::CommitAccepted,
                ResultCode::Ok,
                Some(accepted_identity),
            );
            match wait_for_committed_zero(&session) {
                Ok(identity) => protocol::write_response(
                    output,
                    Event::Committed,
                    ResultCode::Ok,
                    Some(identity),
                ),
                Err(code) => write_terminal_failure(output, &session, code),
            }
        }
        _ => write_terminal_failure(output, &session, ResultCode::UnknownOperation),
    }
}
