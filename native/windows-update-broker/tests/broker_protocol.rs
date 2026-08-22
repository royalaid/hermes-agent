use std::io::{Read, Write};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

const MAGIC: &[u8; 8] = b"HUPDBRK1";
const VERSION: u16 = 1;
const OP_SUPERVISE_CHILD: u16 = 1;
const EVENT_REJECTED: u16 = 0x8001;
const UNSUPPORTED_VERSION: u16 = 2;
const PAYLOAD_TOO_LARGE: u16 = 4;
const TRAILING_BYTES: u16 = 7;
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

fn request(version: u16, op: u16, declared_len: u32, payload: &[u8]) -> Vec<u8> {
    let mut frame = Vec::with_capacity(16 + payload.len());
    frame.extend_from_slice(MAGIC);
    frame.extend_from_slice(&version.to_le_bytes());
    frame.extend_from_slice(&op.to_le_bytes());
    frame.extend_from_slice(&declared_len.to_le_bytes());
    frame.extend_from_slice(payload);
    frame
}

fn run_exchange(frame: &[u8]) -> Vec<u8> {
    let mut command = Command::new(env!("CARGO_BIN_EXE_hermes-windows-update-broker"));
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    let mut broker = command.spawn().expect("start broker");
    let mut stdin = broker.stdin.take().expect("broker stdin");
    stdin.write_all(frame).expect("write request");
    drop(stdin);

    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if broker.try_wait().expect("poll broker").is_some() {
            break;
        }
        if Instant::now() >= deadline {
            broker.kill().expect("kill stalled broker");
            let _ = broker.wait();
            panic!("broker did not terminate within the test deadline");
        }
        thread::sleep(Duration::from_millis(10));
    }

    let mut response = Vec::new();
    broker
        .stdout
        .take()
        .expect("broker stdout")
        .read_to_end(&mut response)
        .expect("read response");
    response
}

fn assert_fixed_response(response: &[u8], event: u16, code: u16) {
    assert_eq!(response.len(), 18, "one fixed response frame");
    assert_eq!(&response[..8], MAGIC);
    assert_eq!(
        u16::from_le_bytes(response[8..10].try_into().unwrap()),
        VERSION
    );
    assert_eq!(
        u16::from_le_bytes(response[10..12].try_into().unwrap()),
        event
    );
    assert_eq!(u32::from_le_bytes(response[12..16].try_into().unwrap()), 2);
    assert_eq!(
        u16::from_le_bytes(response[16..18].try_into().unwrap()),
        code
    );
}

#[test]
fn wrong_protocol_version_is_rejected_with_a_fixed_response() {
    let response = run_exchange(&request(2, OP_SUPERVISE_CHILD, 0, &[]));
    assert_fixed_response(&response, EVENT_REJECTED, UNSUPPORTED_VERSION);
}

#[test]
fn oversized_payload_is_rejected_before_the_payload_is_read() {
    let response = run_exchange(&request(VERSION, OP_SUPERVISE_CHILD, 65_521, &[]));
    assert_fixed_response(&response, EVENT_REJECTED, PAYLOAD_TOO_LARGE);
}

#[test]
fn incomplete_tlv_tail_is_rejected() {
    let response = run_exchange(&request(VERSION, OP_SUPERVISE_CHILD, 1, &[0]));
    assert_fixed_response(&response, EVENT_REJECTED, TRAILING_BYTES);
}
