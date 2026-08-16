"""Tests for U8/KTD8: live NDJSON progress for no_agent cron jobs.

Covers the scheduler/state half of the handoff spec's Part A:

* ``_run_job_script``'s reader-thread ``on_line`` path (deadline/cancel/
  terminate preserved; monitor/prerun callers stay on the byte-for-byte
  ``communicate()`` path).
* ``run_job``'s no_agent branch: NDJSON stage lines are redacted per-line and
  drive ONE in-place-updated assistant progress message; the final result
  line (not a trailing stage line) drives ``doc``/``output``/``_parse_wake_gate``.
* ``SessionDB.update_message`` finalize-in-place semantics via
  ``_record_run`` (role alternation preserved — never two assistant rows).

The live-stream source throughout is the disposable
``scripts/fork_integration/sleep_canary.py`` fixture, driven through
``SLEEP_CANARY_*`` env vars since cron scripts run with no argv.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path

import pytest

_CANARY_SRC = (
    Path(__file__).resolve().parents[2]
    / "scripts" / "fork_integration" / "sleep_canary.py"
)


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME for each test so jobs/scripts don't leak.

    Mirrors ``tests/cron/test_cron_no_agent.py``'s fixture of the same name.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()

    monkeypatch.setenv("HERMES_HOME", str(home))

    import importlib
    import hermes_constants
    importlib.reload(hermes_constants)
    import hermes_state
    importlib.reload(hermes_state)
    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.scheduler
    importlib.reload(cron.scheduler)

    return home


@pytest.fixture
def sleep_canary(hermes_env):
    """Copy the real sleep_canary.py fixture into the isolated scripts dir.

    Returns the script's filename (relative to scripts/), as ``create_job``
    expects.
    """
    dest = hermes_env / "scripts" / "sleep_canary.py"
    shutil.copy(_CANARY_SRC, dest)
    return "sleep_canary.py"


def _job_runs(job_id):
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        return db.list_cron_job_runs(job_id)
    finally:
        db.close()


def _session_messages(session_id):
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        return db.get_messages(session_id)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# One growing, in-place-finalized assistant progress message
# ---------------------------------------------------------------------------


def test_no_agent_stage_lines_grow_and_finalize_one_assistant_message(
    hermes_env, sleep_canary, monkeypatch,
):
    """Every stage line is a real write when un-throttled; role alternation
    stays user -> assistant (never a second assistant row)."""
    from hermes_state import SessionDB
    from cron.jobs import create_job
    import cron.scheduler as scheduler
    from cron.scheduler import run_job

    # Disable throttling so every stage line produces a real DB write —
    # deterministic without needing real >=0.5s spacing between stages.
    monkeypatch.setattr(scheduler, "_NO_AGENT_PROGRESS_UPDATE_MIN_INTERVAL_S", 0.0)
    monkeypatch.setenv("SLEEP_CANARY_STAGES", "3")
    monkeypatch.setenv("SLEEP_CANARY_INTERVAL", "0.02")

    calls: list[tuple] = []
    orig_append = SessionDB.append_message
    orig_update = SessionDB.update_message

    def spy_append(self, session_id, role, content=None, **kwargs):
        msg_id = orig_append(self, session_id, role, content, **kwargs)
        if role == "assistant":
            calls.append(("append", msg_id, content))
        return msg_id

    def spy_update(self, session_id, message_id, content):
        result = orig_update(self, session_id, message_id, content)
        calls.append(("update", message_id, content))
        return result

    monkeypatch.setattr(SessionDB, "append_message", spy_append)
    monkeypatch.setattr(SessionDB, "update_message", spy_update)

    job = create_job(
        prompt=None, schedule="every 5m", script=sleep_canary,
        no_agent=True, deliver="local",
    )
    success, doc, final_response, error = run_job(job)
    assert success is True
    assert error is None

    # Exactly one assistant row was ever CREATED — everything after the
    # first stage line is an UPDATE of that same row.
    assistant_appends = [c for c in calls if c[0] == "append"]
    assert len(assistant_appends) == 1
    progress_id = assistant_appends[0][1]

    updates = [c for c in calls if c[0] == "update"]
    assert len(updates) == 3  # stage 2 update, stage 3 update, finalize update
    assert all(c[1] == progress_id for c in updates)

    # The first three writes (append + first two updates) are the growing
    # live surface: content strictly grows as stages accumulate.
    growing = [assistant_appends[0][2]] + [u[2] for u in updates[:-1]]
    assert all("so far" in text for text in growing)
    assert len(growing[0]) < len(growing[1]) < len(growing[2])

    # The LAST write is _record_run's finalize-in-place call: the terser
    # final doc, not another "N stage(s) so far" progress snapshot.
    final_write = updates[-1][2]
    assert "so far" not in final_write
    assert "3/3 stages, last: verify" in final_write

    # DB state after the run agrees: exactly one user + one assistant row.
    runs = _job_runs(job["id"])
    assert len(runs) == 1
    messages = _session_messages(runs[0]["id"])
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["id"] == progress_id
    assert "3/3 stages, last: verify" in str(messages[1]["content"])

    # run_job's returned doc/final_response carry the same finalized text.
    assert "3/3 stages, last: verify" in doc
    assert "3/3 stages, last: verify" in final_response


def test_no_agent_no_stage_lines_falls_back_to_plain_append(hermes_env):
    """A script with no stage lines never starts a progress message —
    _record_run's fallback append path (unchanged from before KTD8)."""
    from cron.jobs import create_job
    from cron.scheduler import run_job

    script_path = hermes_env / "scripts" / "alert.sh"
    script_path.write_text("#!/bin/bash\necho 'RAM 92% on host'\n")

    job = create_job(
        prompt=None, schedule="every 5m", script="alert.sh",
        no_agent=True, deliver="local",
    )
    success, doc, final_response, error = run_job(job)
    assert success is True
    assert error is None

    runs = _job_runs(job["id"])
    messages = _session_messages(runs[0]["id"])
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert "RAM 92% on host" in str(messages[1]["content"])


# ---------------------------------------------------------------------------
# Final result line / wake-gate: stage lines never drive the gate
# ---------------------------------------------------------------------------


def test_final_result_line_and_wake_gate_ignore_stage_lines():
    from cron.scheduler import _final_result_line, _parse_wake_gate

    stage_only = (
        '{"ts": "x", "stage": "warmup", "ok": true, "detail": "a"}\n'
        '{"ts": "x", "stage": "probe", "ok": true, "detail": "b"}\n'
    )
    # No real final line was ever printed (the run aborted between a stage
    # line and its conclusion) -- _final_result_line finds nothing, and an
    # empty gate input means "wake" (matches _parse_wake_gate's documented
    # behavior for empty/absent output), never accidentally reads a stage
    # line's JSON as the gate.
    assert _final_result_line(stage_only) == ""
    assert _parse_wake_gate(_final_result_line(stage_only)) is True

    with_gate = stage_only + '{"wakeAgent": false}\n'
    assert _final_result_line(with_gate) == '{"wakeAgent": false}'
    assert _parse_wake_gate(_final_result_line(with_gate)) is False


def test_wake_gate_sees_final_line_when_run_aborts_after_stage_lines(
    hermes_env, sleep_canary, monkeypatch,
):
    """End-to-end: a script that exits 0 after stage lines but WITHOUT ever
    printing its final result line must not be silenced, and its delivered
    text must not be a bare stage-line JSON blob."""
    from cron.jobs import create_job
    from cron.scheduler import run_job, SILENT_MARKER

    monkeypatch.setenv("SLEEP_CANARY_STAGES", "2")
    monkeypatch.setenv("SLEEP_CANARY_INTERVAL", "0.01")
    monkeypatch.setenv("SLEEP_CANARY_OMIT_FINAL", "1")

    job = create_job(
        prompt=None, schedule="every 5m", script=sleep_canary,
        no_agent=True, deliver="local",
    )
    success, doc, final_response, error = run_job(job)

    assert success is True
    assert error is None
    assert final_response != SILENT_MARKER
    assert "2/2 stages, last: probe" in final_response
    assert '"stage"' not in final_response  # never a raw stage-line JSON blob


# ---------------------------------------------------------------------------
# Redaction: a secret in a stage line never reaches the session
# ---------------------------------------------------------------------------


def test_secret_in_stage_line_never_reaches_session_including_split_write(
    hermes_env, sleep_canary, monkeypatch,
):
    from cron.jobs import create_job
    import cron.scheduler as scheduler
    from cron.scheduler import run_job

    monkeypatch.setattr(scheduler, "_NO_AGENT_PROGRESS_UPDATE_MIN_INTERVAL_S", 0.0)

    secret = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    monkeypatch.setenv("SLEEP_CANARY_STAGES", "2")
    monkeypatch.setenv("SLEEP_CANARY_INTERVAL", "0.02")
    monkeypatch.setenv("SLEEP_CANARY_SECRET", secret)
    # The secret-bearing stage line is written in two separate flushed
    # writes, split mid-secret with a pause between them — simulating a
    # line split across two OS-level read chunks. A reader that trusted raw
    # chunk boundaries instead of readline()'s newline-delimited semantics
    # could see (and redact-then-emit) a half-written fragment.
    monkeypatch.setenv("SLEEP_CANARY_SPLIT_SECRET_WRITE", "1")

    # The finalized DB row only ever holds the terse final doc (final line +
    # stage count summary) — the per-stage detail text, where the secret
    # lives, only ever appears in the LIVE progress writes made while the
    # run was in flight. Capture every content string ever written to the
    # assistant row (append + update) to check those too, not just the
    # post-finalize DB state.
    from hermes_state import SessionDB

    live_writes: list[str] = []
    orig_append = SessionDB.append_message
    orig_update = SessionDB.update_message

    def spy_append(self, session_id, role, content=None, **kwargs):
        if role == "assistant":
            live_writes.append(content)
        return orig_append(self, session_id, role, content, **kwargs)

    def spy_update(self, session_id, message_id, content):
        live_writes.append(content)
        return orig_update(self, session_id, message_id, content)

    monkeypatch.setattr(SessionDB, "append_message", spy_append)
    monkeypatch.setattr(SessionDB, "update_message", spy_update)

    job = create_job(
        prompt=None, schedule="every 5m", script=sleep_canary,
        no_agent=True, deliver="local",
    )
    success, doc, final_response, error = run_job(job)
    assert success is True
    assert error is None

    runs = _job_runs(job["id"])
    messages = _session_messages(runs[0]["id"])
    full_text = "\n".join(str(m.get("content") or "") for m in messages)
    everything = full_text + "\n" + "\n".join(w or "" for w in live_writes)

    assert secret not in everything
    assert secret not in (doc or "")
    assert secret not in (final_response or "")
    # The redaction mask (a real, distinct sentinel) is present in one of
    # the LIVE progress writes — proves the secret was actually seen and
    # redacted, not just absent because it never made it into a snapshot.
    assert any("ghp_AB" in (w or "") or "REDACTED" in (w or "") for w in live_writes)


# ---------------------------------------------------------------------------
# monitor / prerun callers: unaffected, no on_line callback path
# ---------------------------------------------------------------------------


def test_monitor_source_script_stays_on_no_callback_path(hermes_env):
    """cron.monitor never passes on_line — _run_job_script's default
    communicate()-based path is exercised exactly as before KTD8."""
    from cron.jobs import create_job
    from cron.monitor import check_monitor

    script_path = hermes_env / "scripts" / "watch.sh"
    script_path.write_text("#!/bin/bash\necho 'v1'\n")

    job = create_job(
        prompt="irrelevant", schedule="every 5m",
        monitor_script="watch.sh", deliver="local",
    )
    outcome = check_monitor(job)
    assert outcome.ok is True
    assert outcome.changed is True  # first observation


def test_run_job_script_default_on_line_none_is_unaffected(hermes_env):
    """Direct sanity check: on_line=None keeps _run_job_script's original
    two-value return contract with no reader threads involved."""
    from cron.scheduler import _run_job_script

    script_path = hermes_env / "scripts" / "quick.sh"
    script_path.write_text("#!/bin/bash\necho 'hello'\n")

    ok, output = _run_job_script("quick.sh")
    assert ok is True
    assert output == "hello"


# ---------------------------------------------------------------------------
# Deadline / cancel / terminate preserved under the reader-thread path
# ---------------------------------------------------------------------------


class _BlockingStream:
    """A fake Popen stdout/stderr that blocks in readline() until closed —
    mirrors a real pipe to a process that never writes and never exits."""

    def __init__(self):
        self._closed = threading.Event()

    def readline(self):
        self._closed.wait()
        return ""

    def close(self):
        self._closed.set()


class _NeverFinishingProcess:
    """Minimal Popen double: pipes block forever until _terminate closes
    them (same idiom as test_cron_no_agent.py's timeout test)."""

    returncode = None
    pid = 0

    def __init__(self, *_args, **_kwargs):
        self.stdout = _BlockingStream()
        self.stderr = _BlockingStream()

    def poll(self):
        return None

    def communicate(self, timeout=None):  # pragma: no cover - on_line path never calls this
        raise subprocess.TimeoutExpired(cmd="never", timeout=timeout)

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired(cmd="never", timeout=timeout)

    def kill(self):
        self.returncode = -9


def test_run_job_script_on_line_path_honors_deadline_and_terminate(
    hermes_env, monkeypatch,
):
    import cron.scheduler as scheduler
    from cron.scheduler import _run_job_script

    def _fake_terminate(proc):
        proc.returncode = -15
        proc.stdout.close()
        proc.stderr.close()

    monkeypatch.setattr(scheduler.subprocess, "Popen", _NeverFinishingProcess)
    monkeypatch.setattr(scheduler, "_get_script_timeout", lambda: 1)
    monkeypatch.setattr(scheduler, "_terminate_cron_script_process", _fake_terminate)

    (hermes_env / "scripts" / "never.py").write_text("import time; time.sleep(999)\n")

    seen_lines: list[tuple[str, str]] = []
    ok, output = _run_job_script(
        "never.py", on_line=lambda name, line: seen_lines.append((name, line)),
    )
    assert ok is False
    assert "timed out" in output.lower()
    assert seen_lines == []  # no output was ever produced before the deadline


def test_run_job_script_on_line_path_honors_cancel_event(hermes_env, monkeypatch):
    import cron.scheduler as scheduler
    from cron.scheduler import _run_job_script

    def _fake_terminate(proc):
        proc.returncode = -15
        proc.stdout.close()
        proc.stderr.close()

    monkeypatch.setattr(scheduler.subprocess, "Popen", _NeverFinishingProcess)
    monkeypatch.setattr(scheduler, "_get_script_timeout", lambda: 3600)
    monkeypatch.setattr(scheduler, "_terminate_cron_script_process", _fake_terminate)

    (hermes_env / "scripts" / "never2.py").write_text("import time; time.sleep(999)\n")

    class _AlreadyCancelled:
        def is_set(self):
            return True

    ok, output = _run_job_script(
        "never2.py", cancel_event=_AlreadyCancelled(), on_line=lambda *_a: None,
    )
    assert ok is False
    assert "cancelled" in output.lower()


def test_run_job_script_on_line_path_captures_real_output_and_reaps(hermes_env):
    """Happy-path reader-thread run: multi-line output is captured
    identically to the communicate() path, and every stdout line is
    delivered to on_line in order."""
    from cron.scheduler import _run_job_script

    script_path = hermes_env / "scripts" / "multi.sh"
    script_path.write_text("#!/bin/bash\necho line1\necho line2\necho line3\n")

    seen: list[str] = []
    ok, output = _run_job_script(
        "multi.sh", on_line=lambda name, line: seen.append(line.strip()) if name == "stdout" else None,
    )
    assert ok is True
    assert output == "line1\nline2\nline3"
    assert seen == ["line1", "line2", "line3"]
