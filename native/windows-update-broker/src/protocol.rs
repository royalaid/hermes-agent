use std::io::{self, Read, Write};

pub const MAGIC: &[u8; 8] = b"HUPDBRK1";
pub const VERSION: u16 = 1;
pub const HEADER_BYTES: usize = 16;
pub const MAX_FRAME_BYTES: usize = 65_536;
pub const MAX_PAYLOAD_BYTES: usize = MAX_FRAME_BYTES - HEADER_BYTES;

pub const OP_SUPERVISE_CHILD: u16 = 1;
pub const OP_ABORT: u16 = 2;
pub const OP_COMMIT: u16 = 3;

#[derive(Clone, Copy)]
#[repr(u16)]
pub enum Event {
    Rejected = 0x8001,
    Ready = 0x8002,
    Aborted = 0x8003,
    EofAborted = 0x8004,
    Committed = 0x8005,
    CommitAccepted = 0x8006,
    Failed = 0x80ff,
}

#[derive(Clone, Copy)]
#[repr(u16)]
pub enum ResultCode {
    Ok = 0,
    InvalidMagic = 1,
    UnsupportedVersion = 2,
    UnknownOperation = 3,
    PayloadTooLarge = 4,
    TruncatedFrame = 5,
    InvalidPayload = 6,
    TrailingBytes = 7,
    UnknownField = 8,
    DuplicateField = 9,
    RetainedOpenFailed = 10,
    RetainedIdentityMismatch = 11,
    JobCreateFailed = 12,
    JobConfigureFailed = 13,
    ChildCreateFailed = 14,
    ChildAssignFailed = 15,
    ChildResumeFailed = 16,
    JobTerminateFailed = 17,
    JobWaitTimedOut = 18,
    JobQueryFailed = 19,
    JobNotEmpty = 20,
    #[cfg(not(windows))]
    UnsupportedPlatform = 21,
    RetainedWaitFailed = 22,
    ChildTerminationFailed = 23,
    ControlDecisionTimedOut = 24,
}

pub struct Frame {
    pub op: u16,
    pub payload: Vec<u8>,
}

#[derive(Clone, Copy)]
pub struct ProcessIdentity {
    pub pid: u32,
    pub creation_time: u64,
    pub active_processes: u32,
}

pub fn read_frame<R: Read>(reader: &mut R) -> Result<Option<Frame>, ResultCode> {
    let mut header = [0_u8; HEADER_BYTES];
    match reader.read(&mut header[..1]) {
        Ok(0) => return Ok(None),
        Ok(1) => {}
        Ok(_) => unreachable!("single-byte read returned more than one byte"),
        Err(_) => return Err(ResultCode::TruncatedFrame),
    }
    reader
        .read_exact(&mut header[1..])
        .map_err(|_| ResultCode::TruncatedFrame)?;
    if &header[..8] != MAGIC {
        return Err(ResultCode::InvalidMagic);
    }
    let version = u16::from_le_bytes(header[8..10].try_into().expect("version field"));
    if version != VERSION {
        return Err(ResultCode::UnsupportedVersion);
    }
    let payload_len =
        u32::from_le_bytes(header[12..16].try_into().expect("payload length field")) as usize;
    if payload_len > MAX_PAYLOAD_BYTES {
        return Err(ResultCode::PayloadTooLarge);
    }
    let mut payload = vec![0_u8; payload_len];
    reader
        .read_exact(&mut payload)
        .map_err(|_| ResultCode::TruncatedFrame)?;
    Ok(Some(Frame {
        op: u16::from_le_bytes(header[10..12].try_into().expect("operation field")),
        payload,
    }))
}

pub fn write_response<W: Write>(
    writer: &mut W,
    event: Event,
    code: ResultCode,
    identity: Option<ProcessIdentity>,
) -> io::Result<()> {
    let payload_len = if identity.is_some() { 18_u32 } else { 2_u32 };
    let mut frame = Vec::with_capacity(HEADER_BYTES + payload_len as usize);
    frame.extend_from_slice(MAGIC);
    frame.extend_from_slice(&VERSION.to_le_bytes());
    frame.extend_from_slice(&(event as u16).to_le_bytes());
    frame.extend_from_slice(&payload_len.to_le_bytes());
    frame.extend_from_slice(&(code as u16).to_le_bytes());
    if let Some(identity) = identity {
        frame.extend_from_slice(&identity.pid.to_le_bytes());
        frame.extend_from_slice(&identity.creation_time.to_le_bytes());
        frame.extend_from_slice(&identity.active_processes.to_le_bytes());
    }
    writer.write_all(&frame)?;
    writer.flush()
}
