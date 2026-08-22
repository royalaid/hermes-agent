# Hermes Windows Update Broker

This crate contains the native Windows process broker for the Hermes Desktop
updater handoff. Its only operation is `SUPERVISE_CHILD`. It creates a private,
unnamed, kill-on-close Job before creating the updater, starts the updater with
`CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT`, assigns it
to the Job, records its exact PID and process-creation `FILETIME`, and only then
resumes it.

This broker does **not** terminate install-root holders, elevate privileges, or
automate UAC. A future `TERMINATE_HOLDER` authority boundary needs its own
identity, image, root, resource, and Restart Manager checks. Do not treat this
crate as closing that separate requirement.

## Protocol v1

The protocol is binary and little-endian over anonymous stdin/stdout pipes. It
does not use JSON, serde, a shell, or a temporary command file. Every frame has
this 16-byte header:

| Offset | Size | Field |
| ---: | ---: | --- |
| 0 | 8 | Magic: ASCII `HUPDBRK1` |
| 8 | 2 | Version: `1` |
| 10 | 2 | Operation or event |
| 12 | 4 | Payload length |

The total frame size is at most 65,536 bytes, so payload length is at most
65,520 bytes. The broker checks that bound before allocating or reading the
payload. Truncated headers and payloads fail with a fixed numeric result code.

### Request operations

| Value | Operation | Payload |
| ---: | --- | --- |
| 1 | `SUPERVISE_CHILD` | Strict TLV sequence below |
| 2 | `ABORT` | Empty |
| 3 | `COMMIT` | Empty |

`SUPERVISE_CHILD` is the first frame. After `READY`, the caller sends exactly
one `ABORT` or `COMMIT` frame. Each TLV has a `u16` field ID, a `u32` byte
length, then that many value bytes. Unknown fields, duplicate singleton fields,
malformed lengths, and leftover bytes are rejected.

| ID | Field | Cardinality and bound |
| ---: | --- | --- |
| 1 | Abort-drain timeout, `u32` milliseconds | Required once; 100..120,000 |
| 2 | Retained Desktop identity | Required once; PID `u32` + creation `FILETIME` `u64` |
| 3 | Executable path, UTF-16LE | Required once |
| 4 | Argument, UTF-16LE | Repeated; at most 64 |
| 5 | Working directory, UTF-16LE | Optional once |
| 6 | Child environment entry, UTF-16LE `NAME=VALUE` | Repeated; at most 128 |
| 7 | Total committed runtime, `u32` milliseconds | Required once; 1,000..86,400,000 |
| 8 | Pre-COMMIT decision timeout, `u32` milliseconds | Required once; 100..120,000 |

Executable and working-directory paths must be absolute lexical Windows paths.
Relative paths, `.`/`..`, device namespaces, alternate data streams, forward
slashes, repeated separators, and non-canonical trailing dot/space components
are rejected before `CreateProcessW`. Executable, argument, and environment
blocks are also bounded to the Windows command-line size.

Environment variable names are ASCII alphanumeric/underscore and unique
case-insensitively. The child receives only the explicitly supplied, sorted,
double-NUL-terminated block. The broker's ambient environment is never copied.

### Response events

| Value | Event |
| ---: | --- |
| 0x8001 | `REJECTED` |
| 0x8002 | `READY` |
| 0x8003 | `ABORTED` |
| 0x8004 | `EOF_ABORTED` |
| 0x8005 | `COMMITTED` |
| 0x8006 | `COMMIT_ACCEPTED` |
| 0x80ff | `FAILED` |

Every response payload starts with a fixed `u16` result code. `READY` and
terminal responses that have a process certificate append PID `u32`, creation
`FILETIME` `u64`, and Job active-process count `u32`, for an 18-byte payload.
Rejections without a process certificate have a two-byte payload. No response
contains an OS error string, path, command line, or child output.

| Code | Meaning |
| ---: | --- |
| 0 | OK |
| 1 | Invalid magic |
| 2 | Unsupported version |
| 3 | Unknown operation |
| 4 | Payload too large |
| 5 | Truncated frame |
| 6 | Invalid payload |
| 7 | Trailing bytes |
| 8 | Unknown field |
| 9 | Duplicate field |
| 10 | Retained process could not be opened/queried |
| 11 | Retained PID/creation identity mismatch |
| 12 | Job creation failed |
| 13 | Job configuration failed |
| 14 | Child creation failed |
| 15 | Child assignment failed |
| 16 | Child resume failed |
| 17 | Job termination failed |
| 18 | Job wait timed out |
| 19 | Job accounting query failed |
| 20 | Job was not empty when zero was required |
| 21 | Unsupported platform |
| 22 | Retained-process wait failed or timed out |
| 23 | Unassigned suspended child termination failed |
| 24 | Pre-COMMIT control decision timed out |

## Terminal states

- `ABORT`, invalid control input, and control-pipe EOF call
  `TerminateJobObject`, wait within the abort-drain timeout, query Job
  accounting, and report success only when `ActiveProcesses == 0`.
- The broker waits only the explicit pre-COMMIT decision timeout for that
  control frame. No frame, a partial header, or a partial payload times out,
  terminates the Job, and proves `ActiveProcesses == 0`. A blocked pipe-reader
  thread cannot keep the broker process alive after that proof.
- `COMMIT` is accepted only as a complete, empty-payload frame while the exact
  FILETIME-validated retained Desktop handle remains live. The broker emits an
  immediate identity-bound `COMMIT_ACCEPTED`; callers do not wait for Desktop
  exit before they may quit. After acceptance, loss of stdin or stdout cannot
  revoke the updater. The broker retains the still-armed Job, waits the exact
  Desktop handle, then observes `ActiveProcesses == 0` naturally within the
  separate total-runtime bound. If stdout remains usable it then emits
  `COMMITTED`. It never disarms `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`.
- A failed attempt to deliver `READY` is pre-commit and causes an explicit
  terminate-and-zero attempt.

The broker reports what the kernel proved within the configured bounds. It
does not claim absolute terminality if the kernel itself stalls or refuses the
required query/termination operation; those cases return `FAILED`, never a
success event.

## Development

The crate pins Rust 1.93.1 and checks in `Cargo.lock`. Its only direct runtime
dependency is `windows-sys`; all process control uses public Win32 APIs.

```powershell
cargo test
cargo fmt --all -- --check
cargo clippy --all-targets --all-features -- -D warnings
```

The Windows integration tests use harmless copies of their own test executable,
always pass `CREATE_NO_WINDOW`, and cover suspended assignment, exact identity,
ambient-environment exclusion, noncooperative children, EOF/ABORT, a nested
delayed writer with a +7.5-second no-mutation check, both COMMIT wait orderings,
pre-COMMIT no-frame/partial-frame deadlines, argument quoting boundaries,
post-COMMIT pipe loss, and broker death after `COMMIT_ACCEPTED`.
