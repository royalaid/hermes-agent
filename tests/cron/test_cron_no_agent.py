"""Tests for cronjob no_agent mode — script-driven jobs that skip the LLM.

Covers:

* ``create_job(no_agent=True)`` shape, validation, and serialization.
* ``cronjob(action='create', no_agent=True)`` tool-level validation.
* ``cronjob(action='update')`` flipping no_agent on/off.
* ``scheduler.run_job`` short-circuit path: success/silent/failure.
* Shell script support in ``_run_job_script`` (.sh runs via bash).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME for each test so jobs/scripts don't leak."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()

    monkeypatch.setenv("HERMES_HOME", str(home))

    # Reload modules that cache get_hermes_home() at import time.
    import importlib
    import hermes_constants
    importlib.reload(hermes_constants)
    import hermes_state
    importlib.reload(hermes_state)  # DEFAULT_DB_PATH binds at import time
    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.scheduler
    importlib.reload(cron.scheduler)

    return home


# ---------------------------------------------------------------------------
# create_job / update_job: data-layer semantics
# ---------------------------------------------------------------------------


def test_create_job_no_agent_requires_script(hermes_env):
    from cron.jobs import create_job

    with pytest.raises(ValueError, match="no_agent=True requires a script"):
        create_job(prompt=None, schedule="every 5m", no_agent=True)


def test_update_job_roundtrips_no_agent_flag(hermes_env):
    from cron.jobs import create_job, update_job, get_job

    script_path = hermes_env / "scripts" / "w.sh"
    script_path.write_text("echo hi\n")
    job = create_job(prompt=None, schedule="every 5m", script="w.sh", no_agent=True, deliver="local")

    update_job(job["id"], {"no_agent": False})
    reloaded = get_job(job["id"])
    assert reloaded["no_agent"] is False

    update_job(job["id"], {"no_agent": True})
    reloaded = get_job(job["id"])
    assert reloaded["no_agent"] is True


# ---------------------------------------------------------------------------
# cronjob tool: API-layer validation
# ---------------------------------------------------------------------------


def test_cronjob_tool_create_no_agent_without_script_errors(hermes_env):
    from tools.cronjob_tools import cronjob

    result = json.loads(
        cronjob(action="create", schedule="every 5m", no_agent=True, deliver="local")
    )
    assert result.get("success") is False
    assert "no_agent=True requires a script" in result.get("error", "")


# ---------------------------------------------------------------------------
# scheduler.run_job: short-circuit behavior
# ---------------------------------------------------------------------------


def test_run_job_no_agent_success_returns_script_stdout(hermes_env):
    """Happy path: script exits 0 with output, delivered verbatim."""
    from cron.jobs import create_job
    from cron.scheduler import run_job

    script_path = hermes_env / "scripts" / "alert.sh"
    script_path.write_text("#!/bin/bash\necho 'RAM 92% on host'\n")

    job = create_job(
        prompt=None, schedule="every 5m", script="alert.sh", no_agent=True, deliver="local"
    )
    success, doc, final_response, error = run_job(job)
    assert success is True
    assert error is None
    assert "RAM 92% on host" in final_response
    assert "RAM 92% on host" in doc


# ---------------------------------------------------------------------------
# run_job: no_agent runs are recorded as run-history sessions (#44080)
# ---------------------------------------------------------------------------


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


def test_run_job_no_agent_success_records_run_session(hermes_env):
    """A successful no_agent run must appear in the job's run history."""
    from cron.jobs import create_job
    from cron.scheduler import run_job

    script_path = hermes_env / "scripts" / "alert.sh"
    script_path.write_text("#!/bin/bash\necho 'RAM 92% on host'\n")

    job = create_job(
        prompt=None, schedule="every 5m", script="alert.sh", no_agent=True, deliver="local"
    )
    success, _, _, _ = run_job(job)
    assert success is True

    runs = _job_runs(job["id"])
    assert len(runs) == 1
    run = runs[0]
    assert run["id"].startswith(f"cron_{job['id']}_")
    assert run["source"] == "cron"
    # The run finished, so the GUI must not show it as still active.
    assert run["ended_at"] is not None
    assert run["end_reason"] == "cron_complete"

    # Script output is persisted so the GUI can show what the run produced.
    messages = _session_messages(run["id"])
    roles = [m["role"] for m in messages]
    assert "user" in roles and "assistant" in roles
    assistant_text = " ".join(
        str(m.get("content") or "") for m in messages if m["role"] == "assistant"
    )
    assert "RAM 92% on host" in assistant_text


def test_run_job_no_agent_failure_records_run_session(hermes_env):
    """A failed script run must be visible in run history, not silent."""
    from cron.jobs import create_job
    from cron.scheduler import run_job

    script_path = hermes_env / "scripts" / "broken.sh"
    script_path.write_text("#!/bin/bash\necho oops >&2\nexit 3\n")

    job = create_job(
        prompt=None, schedule="every 5m", script="broken.sh", no_agent=True, deliver="local"
    )
    success, _, _, _ = run_job(job)
    assert success is False

    runs = _job_runs(job["id"])
    assert len(runs) == 1
    assert runs[0]["ended_at"] is not None

    messages = _session_messages(runs[0]["id"])
    assistant_text = " ".join(
        str(m.get("content") or "") for m in messages if m["role"] == "assistant"
    )
    assert "script failed" in assistant_text


def test_run_job_no_agent_silent_run_records_run_session(hermes_env):
    """Silent runs (empty stdout) still leave a run record."""
    from cron.jobs import create_job
    from cron.scheduler import run_job, SILENT_MARKER

    script_path = hermes_env / "scripts" / "quiet.sh"
    script_path.write_text("#!/bin/bash\n# nothing to say\n")

    job = create_job(
        prompt=None, schedule="every 5m", script="quiet.sh", no_agent=True, deliver="local"
    )
    success, _, final_response, _ = run_job(job)
    assert success is True
    assert final_response == SILENT_MARKER

    runs = _job_runs(job["id"])
    assert len(runs) == 1
    assert runs[0]["ended_at"] is not None


def test_run_job_no_agent_survives_broken_session_store(hermes_env):
    """Run recording is best-effort: a broken state store must not break runs."""
    from cron.jobs import create_job
    from cron.scheduler import run_job

    script_path = hermes_env / "scripts" / "alert.sh"
    script_path.write_text("#!/bin/bash\necho 'still works'\n")

    job = create_job(
        prompt=None, schedule="every 5m", script="alert.sh", no_agent=True, deliver="local"
    )

    with patch("hermes_state.SessionDB", side_effect=RuntimeError("db locked")):
        success, _, final_response, error = run_job(job)

    assert success is True
    assert error is None
    assert "still works" in final_response


# ---------------------------------------------------------------------------
# _run_job_script: shell-script support
# ---------------------------------------------------------------------------


def test_run_job_script_path_traversal_still_blocked(hermes_env):
    """Security regression: shell-script support must NOT loosen containment."""
    from cron.scheduler import _run_job_script

    # Absolute path outside the scripts dir should be rejected.
    ok, output = _run_job_script("/etc/passwd")
    assert ok is False
    assert "Blocked" in output or "outside" in output
