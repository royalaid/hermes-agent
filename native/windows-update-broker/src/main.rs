mod protocol;

#[cfg(windows)]
mod windows_broker;

use std::io::{self, Write};

use protocol::{Event, ResultCode};

fn run() -> io::Result<()> {
    let mut stdout = io::stdout().lock();
    let request = match protocol::read_frame(&mut io::stdin().lock()) {
        Ok(Some(frame)) => frame,
        Ok(None) => return Ok(()),
        Err(code) => {
            protocol::write_response(&mut stdout, Event::Rejected, code, None)?;
            return Ok(());
        }
    };
    if request.op != protocol::OP_SUPERVISE_CHILD {
        protocol::write_response(
            &mut stdout,
            Event::Rejected,
            ResultCode::UnknownOperation,
            None,
        )?;
        return Ok(());
    }

    #[cfg(windows)]
    {
        windows_broker::supervise(request.payload, &mut stdout)
    }
    #[cfg(not(windows))]
    {
        let _ = request.payload;
        protocol::write_response(
            &mut stdout,
            Event::Rejected,
            ResultCode::UnsupportedPlatform,
            None,
        )
    }
}

fn main() {
    if run().is_err() {
        let _ = io::stdout().flush();
    }
}
