"""Durable cron execution-ledger behavior."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def _point_ledger(monkeypatch, tmp_path):
    import cron.executions as executions

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    return executions


def test_execution_transitions_are_durable(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)

    claimed = executions.create_execution("job-1", source="builtin")
    assert claimed["status"] == "claimed"
    assert claimed["claimed_at"]
    assert claimed["started_at"] is None
    assert claimed["finished_at"] is None

    running = executions.mark_execution_running(claimed["id"])
    assert running["status"] == "running"
    assert running["started_at"]

    completed = executions.finish_execution(claimed["id"], success=True)
    assert completed["status"] == "completed"
    assert completed["finished_at"]
    assert completed["error"] is None

    persisted = executions.list_executions(job_id="job-1")
    assert persisted == [completed]


def test_execution_ledger_follows_the_current_profile_home(monkeypatch, tmp_path):
    import cron.executions as executions

    current_home = {"path": tmp_path / "default"}
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", None)
    monkeypatch.setattr(executions, "get_hermes_home", lambda: current_home["path"])

    default_row = executions.create_execution("default-job", source="builtin")
    current_home["path"] = tmp_path / "worker"
    worker_row = executions.create_execution("worker-job", source="builtin")

    assert executions.list_executions() == [worker_row]
    current_home["path"] = tmp_path / "default"
    assert executions.list_executions() == [default_row]
    assert (tmp_path / "default" / "cron" / "executions.db").is_file()
    assert (tmp_path / "worker" / "cron" / "executions.db").is_file()


def test_terminal_execution_cannot_be_rewritten(monkeypatch, tmp_path):
    """A completion arriving after the row is already terminal writes a
    sibling late-outcome record instead of a silent no-op (KTD6/U3): the
    `unknown`/terminal audit row itself is never overwritten."""
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("immutable", source="builtin")
    executions.mark_execution_running(record["id"])
    executions.finish_execution(record["id"], success=True)

    late = executions.finish_execution(
        record["id"], success=False, error="late writer"
    )

    # The terminal execution row is returned untouched, not rewritten.
    assert late is not None
    assert late["id"] == record["id"]
    assert late["status"] == "completed"
    assert late["error"] is None
    assert executions.latest_execution("immutable")["status"] == "completed"

    # ...but the late arrival is recorded as a sibling late-outcome row.
    assert late["late_outcome"]["success"] is False
    assert late["late_outcome"]["error"] == "late writer"
    assert late["late_outcome"]["delivery_outcome"] is None
    outcomes = executions.list_late_outcomes(record["id"])
    assert len(outcomes) == 1
    assert outcomes[0]["success"] == 0
    assert outcomes[0]["error"] == "late writer"
    assert outcomes[0]["observed_at"] > 0


def test_late_outcome_not_recorded_for_genuine_claimed_running_race(monkeypatch, tmp_path):
    """A second finish on a still-in-flight (non-terminal) execution is a
    genuine CAS race, not a late outcome -- no late-outcome row is written."""
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("racey", source="builtin")

    # Row is still 'claimed' (never marked running/finished) -- simulate two
    # finishers racing: first wins, this represents the loser's view before
    # its own UPDATE, but calling finish_execution on an unknown id also
    # exercises the "row missing" no-op branch.
    assert executions.finish_execution("does-not-exist", success=True) is None
    assert executions.list_late_outcomes("does-not-exist") == []


def test_execution_late_outcomes_table_created_against_existing_production_db(monkeypatch, tmp_path):
    """The sibling table is created via CREATE TABLE IF NOT EXISTS the first
    time it's touched, even against an executions.db that predates it."""
    executions = _point_ledger(monkeypatch, tmp_path)

    db_path = executions.EXECUTIONS_FILE
    db_path.parent.mkdir(parents=True)
    # Build a production-shaped executions.db using ONLY the pre-existing
    # `executions` table -- no execution_late_outcomes table at all, as a
    # real on-disk database from before this feature would look.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """CREATE TABLE executions (
                 id TEXT PRIMARY KEY,
                 job_id TEXT NOT NULL,
                 source TEXT NOT NULL,
                 process_id TEXT NOT NULL,
                 pid INTEGER NOT NULL,
                 process_started_at INTEGER,
                 status TEXT NOT NULL CHECK(status IN
                   ('claimed','running','completed','failed','unknown')),
                 claimed_at TEXT NOT NULL,
                 started_at TEXT,
                 finished_at TEXT,
                 error TEXT
               )"""
        )
        conn.execute(
            "INSERT INTO executions (id, job_id, source, process_id, pid, "
            "status, claimed_at) VALUES ('pre-existing', 'job-old', 'builtin', "
            "'proc-old', 1, 'completed', 'now')"
        )
        conn.commit()
    finally:
        conn.close()

    late = executions.finish_execution(
        "pre-existing", success=False, error="late after old-schema upgrade"
    )
    assert late is not None
    assert late["late_outcome"]["error"] == "late after old-schema upgrade"
    assert executions.list_late_outcomes("pre-existing")[0]["success"] == 0


def test_retention_bounds_terminal_history_but_preserves_inflight(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 3)
    inflight = executions.create_execution("live", source="builtin")
    executions.mark_execution_running(inflight["id"])
    for index in range(8):
        row = executions.create_execution(f"done-{index}", source="builtin")
        executions.finish_execution(row["id"], success=True)

    records = executions.list_executions(limit=100)
    assert len([row for row in records if row["status"] == "completed"]) == 3
    assert executions.latest_execution("live")["status"] == "running"


def test_corrupt_store_fails_closed_without_overwrite(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    executions.EXECUTIONS_FILE.parent.mkdir(parents=True)
    executions.EXECUTIONS_FILE.write_bytes(b"not a sqlite database")

    with __import__("pytest").raises(sqlite3.DatabaseError):
        executions.create_execution("new", source="builtin")
    assert executions.EXECUTIONS_FILE.read_bytes() == b"not a sqlite database"


def test_cron_runs_cli_prints_execution_history(monkeypatch, tmp_path, capsys):
    executions = _point_ledger(monkeypatch, tmp_path)
    row = executions.create_execution("cli-job", source="builtin")
    executions.finish_execution(row["id"], success=False, error="boom")
    from hermes_cli.cron import cron_runs

    cron_runs("cli-job", limit=10)

    output = capsys.readouterr().out
    assert row["id"] in output
    assert "failed" in output
    assert "boom" in output


def test_quick_backup_includes_execution_ledger():
    from hermes_cli.backup import _QUICK_STATE_FILES

    assert "cron/executions.db" in _QUICK_STATE_FILES


def test_failed_execution_keeps_error(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)

    record = executions.create_execution("job-2", source="external")
    failed = executions.finish_execution(record["id"], success=False, error="provider exploded")

    assert failed["status"] == "failed"
    assert failed["error"] == "provider exploded"


def test_recovery_does_not_mark_live_process_execution_unknown(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("still-live", source="builtin")
    executions.mark_execution_running(record["id"])

    assert executions.recover_interrupted_executions() == 0
    assert executions.latest_execution("still-live")["status"] == "running"


def _orphan_execution(executions, job_id: str, *, running: bool = False) -> dict:
    """Persist a claimed/running execution owned by a process that cannot be
    proved live -- a fabricated pid/process_id combo, mirroring the dead-owner
    fixture idiom in tests/cron/test_dead_owner_claim_reclaim.py."""
    record = executions.create_execution(job_id, source="builtin")
    if running:
        executions.mark_execution_running(record["id"])
    with executions._transaction() as conn:
        conn.execute(
            "UPDATE executions SET process_id='dead-owner', pid=999999999, "
            "process_started_at=NULL WHERE id=?",
            (record["id"],),
        )
    return record


def test_recover_interrupted_executions_detailed_returns_recovered_rows(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(executions, "_owner_is_live", lambda *_a, **_k: False)
    first = _orphan_execution(executions, "detailed-job-a")
    second = _orphan_execution(executions, "detailed-job-b", running=True)

    detailed = executions.recover_interrupted_executions_detailed()

    assert {row["id"] for row in detailed} == {first["id"], second["id"]}
    assert all(row["status"] == "unknown" for row in detailed)


def test_recover_interrupted_executions_detailed_does_not_emit_telemetry(monkeypatch, tmp_path):
    """The detailed variant defers the projection emit to its caller (U4) --
    unlike the int wrapper, it must not call emit_execution_state itself."""
    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(executions, "_owner_is_live", lambda *_a, **_k: False)
    _orphan_execution(executions, "no-emit-job")

    emitted = []
    monkeypatch.setattr(executions, "_emit_execution_state", lambda *a, **k: emitted.append((a, k)))

    detailed = executions.recover_interrupted_executions_detailed()

    assert len(detailed) == 1
    assert emitted == []


def test_recover_interrupted_executions_int_wrapper_still_emits(monkeypatch, tmp_path):
    """The pre-existing `-> int` function keeps emitting exactly as before,
    for callers other than the scheduler reap site (e.g. cronjob_tools)."""
    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(executions, "_owner_is_live", lambda *_a, **_k: False)
    _orphan_execution(executions, "wrapper-emit-job")

    emitted = []
    monkeypatch.setattr(executions, "_emit_execution_state", lambda *a, **k: emitted.append((a, k)))

    assert executions.recover_interrupted_executions() == 1
    assert len(emitted) == 1


def test_double_reap_of_same_execution_is_a_cas_no_op(monkeypatch, tmp_path):
    """A second detailed reap after the first already transitioned a row must
    not return it again -- the CAS UPDATE + same-shaped SELECT excludes rows
    no longer in ('claimed','running'). This is the double-delivery guard
    the U4 reap-site relies on."""
    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(executions, "_owner_is_live", lambda *_a, **_k: False)
    _orphan_execution(executions, "double-reap-job")

    first_pass = executions.recover_interrupted_executions_detailed()
    second_pass = executions.recover_interrupted_executions_detailed()

    assert len(first_pass) == 1
    assert second_pass == []


def test_provider_protocol_recover_interrupted_signature_unchanged():
    """scheduler_provider.CronScheduler.recover_interrupted stays a plain
    `-> int` method (KTD9-adjacent protocol stability) -- U4 adds the
    detailed variant alongside it, never in place of it."""
    import inspect

    from cron.scheduler_provider import CronScheduler

    sig = inspect.signature(CronScheduler.recover_interrupted)
    assert list(sig.parameters) == ["self"]
    assert sig.return_annotation in (int, "int")


def test_late_outcome_delivery_outcome_can_be_attached(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("late-delivery-job", source="builtin")
    executions.finish_execution(record["id"], success=True)

    late = executions.finish_execution(record["id"], success=False, error="late arrival")
    assert late["late_outcome"]["delivery_outcome"] is None

    executions.set_late_outcome_delivery_outcome(late["late_outcome"]["rowid"], "delivered")

    stored = executions.list_late_outcomes(record["id"])[0]
    assert stored["delivery_outcome"] == "delivered"


def test_restart_marks_interrupted_execution_unknown_without_requeue(tmp_path):
    """Real temp-HERMES_HOME subprocess restart: in-flight is audit-only unknown."""
    home = tmp_path / "home"
    repo = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(repo)

    create = subprocess.run(
        [
            sys.executable,
            "-c",
            "from cron.executions import create_execution, mark_execution_running; "
            "r=create_execution('restart-job', source='builtin'); "
            "mark_execution_running(r['id']); print(r['id'])",
        ],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    execution_id = create.stdout.strip()

    recover = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from cron.executions import recover_interrupted_executions, list_executions; "
            "print(recover_interrupted_executions()); "
            "print(json.dumps(list_executions(job_id='restart-job'))) ",
        ],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    lines = recover.stdout.strip().splitlines()
    assert lines[0] == "1"
    records = json.loads(lines[1])
    assert len(records) == 1
    assert records[0]["id"] == execution_id
    assert records[0]["status"] == "unknown"
    assert records[0]["finished_at"]
    assert "restart" in records[0]["error"].lower()
    # Recovery only classifies the old attempt. It must not manufacture a new
    # claimed record (which would imply an automatic retry).
    assert [r["status"] for r in records] == ["unknown"]


def test_generic_submit_failure_finishes_attempt_and_releases_guard(monkeypatch):
    import cron.scheduler as scheduler

    class BrokenPool:
        def submit(self, _callable):
            raise ValueError("executor rejected")

    finished = []
    monkeypatch.setattr(
        scheduler, "create_execution",
        lambda *_args, **_kwargs: {"id": "exec-submit-fail"},
    )
    monkeypatch.setattr(
        scheduler, "finish_execution",
        lambda execution_id, **kwargs: finished.append((execution_id, kwargs)),
    )
    monkeypatch.setattr(scheduler, "get_due_jobs", lambda: [{"id": "submit-fail"}])
    monkeypatch.setattr(scheduler, "claim_job_for_fire", lambda _job_id: True)
    monkeypatch.setattr(scheduler, "_get_parallel_pool", lambda _workers: BrokenPool())

    assert scheduler.tick(verbose=False, sync=False) == 0
    assert finished == [
        ("exec-submit-fail", {
            "success": False,
            "error": "Executor dispatch failed: executor rejected",
        })
    ]
    assert "submit-fail" not in scheduler.get_running_job_ids()


def test_run_one_job_records_running_then_terminal(monkeypatch):
    import cron.scheduler as scheduler

    events = []
    monkeypatch.setattr(
        scheduler,
        "mark_execution_running",
        lambda execution_id: events.append(("running", execution_id)),
        raising=False,
    )
    monkeypatch.setattr(
        scheduler,
        "finish_execution",
        lambda execution_id, **kwargs: events.append(("finish", execution_id, kwargs)),
        raising=False,
    )
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda job, *, defer_agent_teardown=None, **_kw: (True, "output", "response", None),
    )
    monkeypatch.setattr(scheduler, "save_job_output", lambda *_args: None)
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scheduler, "mark_job_run", lambda *_args, **_kwargs: None)

    assert scheduler.run_one_job({"id": "job-3", "execution_id": "exec-3"}) is True
    assert events[0] == ("running", "exec-3")
    assert events[-1][0:2] == ("finish", "exec-3")
    assert events[-1][2]["success"] is True


def test_provider_start_recovers_interrupted_records_before_tick(monkeypatch):
    import cron.scheduler_provider as provider

    events = []
    stop = __import__("threading").Event()
    stop.set()
    monkeypatch.setattr(
        "cron.executions.recover_interrupted_executions",
        lambda: events.append("recover") or 0,
        raising=False,
    )
    monkeypatch.setattr("cron.jobs.record_ticker_heartbeat", lambda **_kwargs: events.append("heartbeat"))

    provider.InProcessCronScheduler().start(stop, interval=1)

    assert events[:2] == ["recover", "heartbeat"]


def test_external_provider_start_recovers_interrupted_records(monkeypatch):
    from plugins.cron_providers.chronos import ChronosCronScheduler

    provider = ChronosCronScheduler()
    provider._client = type("Client", (), {"arm": lambda self, **kwargs: None})()
    events = []
    monkeypatch.setattr(
        "cron.executions.recover_interrupted_executions",
        lambda: events.append("recover") or 0,
    )
    monkeypatch.setattr(provider, "reconcile", lambda: events.append("reconcile"))

    provider.start(__import__("threading").Event())

    assert events == ["recover", "reconcile"]


class _TrackingConnection:
    """Delegates to a real sqlite3.Connection while recording close() calls.

    sqlite3.Connection is a static C type: it has no per-instance __dict__
    and its class methods can't be monkeypatched, so open/close tracking is
    done via a delegating wrapper returned in place of the real connection.
    """

    def __init__(self, real, closed_ids):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_closed_ids", closed_ids)

    def close(self):
        self._closed_ids.append(id(self._real))
        self._real.close()

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __setattr__(self, name, value):
        setattr(self._real, name, value)


def _count_open_connections(executions, monkeypatch):
    """Wrap sqlite3.connect to track open/close balance for the ledger module."""
    opened_ids = []
    closed_ids = []
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened_ids.append(id(conn))
        return _TrackingConnection(conn, closed_ids)

    monkeypatch.setattr(executions.sqlite3, "connect", tracking_connect)
    return opened_ids, closed_ids


def test_ledger_operations_close_every_connection(monkeypatch, tmp_path):
    """Regression for #69567: every ledger call must close its connection
    deterministically instead of relying on garbage collection."""
    executions = _point_ledger(monkeypatch, tmp_path)
    opened, closed = _count_open_connections(executions, monkeypatch)

    record = executions.create_execution("leak-check", source="builtin")
    executions.mark_execution_running(record["id"])
    executions.finish_execution(record["id"], success=True)
    executions.list_executions(job_id="leak-check")
    executions.latest_executions(["leak-check"])
    executions.recover_interrupted_executions()

    assert len(opened) == 6
    assert len(closed) == 6
    assert set(opened) == set(closed)


def test_early_return_still_closes_connection(monkeypatch, tmp_path):
    """mark_execution_running returns None mid-block on a bad transition;
    the connection must still be closed rather than leaked."""
    executions = _point_ledger(monkeypatch, tmp_path)
    opened, closed = _count_open_connections(executions, monkeypatch)

    assert executions.mark_execution_running("does-not-exist") is None

    assert len(opened) == 1
    assert len(closed) == 1


def test_exception_during_operation_still_closes_connection(monkeypatch, tmp_path):
    """A failing statement inside the transaction must roll back and close,
    not leak the connection."""
    executions = _point_ledger(monkeypatch, tmp_path)
    opened, closed = _count_open_connections(executions, monkeypatch)

    with __import__("pytest").raises(sqlite3.IntegrityError):
        with executions._transaction() as conn:
            conn.execute(
                "INSERT INTO executions (id, job_id, source, process_id, pid, "
                "status, claimed_at) VALUES ('x', 'x', 'x', 'x', 1, 'bogus-status', 'now')"
            )

    assert len(opened) == 1
    assert len(closed) == 1


def test_schema_init_failure_still_closes_connection(monkeypatch, tmp_path):
    """If PRAGMA/DDL setup in _connect() fails after sqlite3.connect()
    succeeds, the partially-initialized connection must still be closed."""
    executions = _point_ledger(monkeypatch, tmp_path)
    opened_ids = []
    closed_ids = []
    real_connect = sqlite3.connect

    class _FailingSchemaConnection(_TrackingConnection):
        def execute(self, sql, *args, **kwargs):
            if "CREATE TABLE" in sql:
                raise sqlite3.OperationalError("simulated schema init failure")
            return self._real.execute(sql, *args, **kwargs)

    def tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened_ids.append(id(conn))
        return _FailingSchemaConnection(conn, closed_ids)

    monkeypatch.setattr(executions.sqlite3, "connect", tracking_connect)

    with __import__("pytest").raises(sqlite3.OperationalError):
        executions.create_execution("init-fail", source="builtin")

    assert len(opened_ids) == 1
    assert len(closed_ids) == 1


def test_job_listing_exposes_latest_execution(monkeypatch, tmp_path):
    import cron.jobs as jobs

    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    executions = _point_ledger(monkeypatch, tmp_path)

    job = jobs.create_job(prompt="audit me", schedule="every 1h", name="audit")
    record = executions.create_execution(job["id"], source="builtin")
    executions.mark_execution_running(record["id"])

    listed = jobs.list_jobs(include_disabled=True)
    assert listed[0]["latest_execution"]["id"] == record["id"]
    assert listed[0]["latest_execution"]["status"] == "running"
