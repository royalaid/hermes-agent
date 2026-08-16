"""Unconfirmed-run delivery + honest last_status at reap, and late-outcome
delivery (U4, KTD6/KTD14).

Covers:
  - ``cron.scheduler._reap_unconfirmed_runs``: the dead-owner reap site's
    post-recovery jobs.json correction (R8) + one aggregated "unconfirmed"
    delivery per affected job (R9-adjacent aggregation) + exactly one
    ``project_execution_event`` per recovered record.
  - ``cron.scheduler._notify_late_outcome``: the late-outcome delivery hook
    wired at ``run_one_job``'s ``finish_execution`` call site.
  - The reap site's double-reap-safe wiring through ``tick()`` end-to-end,
    relying on ``recover_interrupted_executions_detailed``'s CAS guarantee
    (see tests/cron/test_execution_ledger.py for the ledger-level proof).

Mocking idiom mirrors tests/cron/test_dead_owner_claim_reclaim.py (dead-pid
fixture rows, ``_last_dead_owner_reap_at`` throttle reset) and
tests/cron/test_execution_ledger.py::test_run_one_job_records_running_then_terminal
(monkeypatching scheduler module attributes + local-import targets by their
string module path).
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import cron.scheduler as scheduler_mod


@pytest.fixture()
def cron_home(tmp_path):
    from cron import jobs as cron_jobs

    with cron_jobs.use_cron_store(tmp_path):
        cron_jobs.ensure_dirs()
        yield tmp_path


def _record(job_id: str, execution_id: str = "exec-1", error: str = "owner exited") -> dict:
    return {
        "id": execution_id,
        "job_id": job_id,
        "status": "unknown",
        "claimed_at": "2026-08-15T00:00:00+00:00",
        "error": error,
    }


class TestReapUnconfirmedRuns:
    def test_corrects_last_status_and_delivers_once(self, monkeypatch, cron_home):
        from cron import jobs as cron_jobs

        job = cron_jobs.create_job(
            prompt="p", schedule="every 1h", name="watched", deliver="telegram:-100:1",
        )

        delivered = []
        monkeypatch.setattr(
            scheduler_mod, "_deliver_result",
            lambda job_arg, content, **kw: delivered.append((job_arg["id"], content)) or None,
        )
        monkeypatch.setattr(
            scheduler_mod, "_resolve_delivery_targets",
            lambda job_arg: [{"platform": "telegram", "chat_id": "-100", "thread_id": "1"}],
        )
        emitted = []
        monkeypatch.setattr(
            "agent.monitoring.cron_health.emit_execution_state",
            lambda record, **kw: emitted.append((record["id"], kw.get("delivery_outcome"))),
        )

        scheduler_mod._reap_unconfirmed_runs([_record(job["id"])], adapters=None, loop=None)

        assert len(delivered) == 1
        assert delivered[0][0] == job["id"]
        assert "1 run(s) reclassified unconfirmed" in delivered[0][1]
        assert "unconfirmed" in delivered[0][1]

        updated = cron_jobs.get_job(job["id"])
        assert updated["last_status"] == "unknown"
        assert "unconfirmed" in updated["last_error"]
        # KTD6/plan wording: classify as unconfirmed, never "failed".
        assert "failed" not in updated["last_error"]

        assert emitted == [("exec-1", "delivered")]

    def test_aggregates_multiple_records_for_same_job_into_one_delivery(self, monkeypatch, cron_home):
        from cron import jobs as cron_jobs

        job = cron_jobs.create_job(
            prompt="p", schedule="every 1h", name="multi", deliver="telegram:-100:1",
        )

        delivered = []
        monkeypatch.setattr(
            scheduler_mod, "_deliver_result", lambda j, c, **kw: delivered.append(c) or None,
        )
        monkeypatch.setattr(
            scheduler_mod, "_resolve_delivery_targets",
            lambda j: [{"platform": "telegram", "chat_id": "-100", "thread_id": None}],
        )
        emitted = []
        monkeypatch.setattr(
            "agent.monitoring.cron_health.emit_execution_state",
            lambda record, **kw: emitted.append((record["id"], kw.get("delivery_outcome"))),
        )

        records = [
            _record(job["id"], execution_id="exec-a"),
            _record(job["id"], execution_id="exec-b"),
        ]
        scheduler_mod._reap_unconfirmed_runs(records, adapters=None, loop=None)

        assert len(delivered) == 1, "N reclaimed rows for the SAME job must be one delivery, not N"
        assert "2 run(s) reclassified unconfirmed" in delivered[0]
        assert "exec-a" in delivered[0] and "exec-b" in delivered[0]
        # Every record still gets its own telemetry emit even though delivery was one call.
        assert {row[0] for row in emitted} == {"exec-a", "exec-b"}
        assert all(row[1] == "delivered" for row in emitted)

    def test_not_configured_when_no_delivery_target_resolves(self, monkeypatch, cron_home):
        from cron import jobs as cron_jobs

        job = cron_jobs.create_job(
            prompt="p", schedule="every 1h", name="no-target", deliver="origin",
        )
        deliver_calls = []
        monkeypatch.setattr(
            scheduler_mod, "_deliver_result",
            lambda *a, **k: deliver_calls.append(1) or None,
        )
        monkeypatch.setattr(scheduler_mod, "_resolve_delivery_targets", lambda j: [])

        emitted = []
        monkeypatch.setattr(
            "agent.monitoring.cron_health.emit_execution_state",
            lambda record, **kw: emitted.append(kw.get("delivery_outcome")),
        )

        scheduler_mod._reap_unconfirmed_runs([_record(job["id"])], adapters=None, loop=None)

        assert emitted == ["not_configured"]

    def test_failed_delivery_outcome_does_not_block_the_reap(self, monkeypatch, cron_home):
        from cron import jobs as cron_jobs

        job = cron_jobs.create_job(
            prompt="p", schedule="every 1h", name="broken-target", deliver="telegram:-100:1",
        )
        monkeypatch.setattr(
            scheduler_mod, "_deliver_result", lambda j, c, **kw: "transport exploded",
        )
        monkeypatch.setattr(
            scheduler_mod, "_resolve_delivery_targets",
            lambda j: [{"platform": "telegram", "chat_id": "-100", "thread_id": None}],
        )
        emitted = []
        monkeypatch.setattr(
            "agent.monitoring.cron_health.emit_execution_state",
            lambda record, **kw: emitted.append(kw.get("delivery_outcome")),
        )

        # Must not raise even though delivery failed.
        scheduler_mod._reap_unconfirmed_runs([_record(job["id"])], adapters=None, loop=None)

        assert emitted == ["failed"]
        assert cron_jobs.get_job(job["id"])["last_status"] == "unknown"

    def test_missing_job_skips_delivery_without_raising(self, monkeypatch, cron_home):
        deliver_calls = []
        monkeypatch.setattr(
            scheduler_mod, "_deliver_result", lambda *a, **k: deliver_calls.append(1) or None,
        )
        emitted = []
        monkeypatch.setattr(
            "agent.monitoring.cron_health.emit_execution_state",
            lambda record, **kw: emitted.append(kw.get("delivery_outcome")),
        )

        scheduler_mod._reap_unconfirmed_runs([_record("does-not-exist")], adapters=None, loop=None)

        assert deliver_calls == []
        assert emitted == ["not_configured"]

    def test_telemetry_emit_failure_does_not_raise(self, monkeypatch, cron_home):
        from cron import jobs as cron_jobs

        job = cron_jobs.create_job(prompt="p", schedule="every 1h", name="emit-boom")
        monkeypatch.setattr(scheduler_mod, "_deliver_result", lambda *a, **k: None)

        def _boom(*_a, **_k):
            raise RuntimeError("emitter down")

        monkeypatch.setattr("agent.monitoring.cron_health.emit_execution_state", _boom)

        # Must not raise -- a reap pass completes even if telemetry is broken.
        scheduler_mod._reap_unconfirmed_runs([_record(job["id"])], adapters=None, loop=None)


class TestDoubleReapEndToEnd:
    def test_double_reap_via_tick_does_not_double_deliver(self, monkeypatch, cron_home, tmp_path):
        """A second reap pass (throttle bypassed) after the first already
        transitioned the row must not deliver again -- relies on
        recover_interrupted_executions_detailed only returning rows it
        actually transitions (CAS), proven at the ledger level in
        tests/cron/test_execution_ledger.py::test_double_reap_of_same_execution_is_a_cas_no_op.
        """
        from cron import jobs as cron_jobs
        import cron.executions as executions_mod

        monkeypatch.setattr(executions_mod, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
        job = cron_jobs.create_job(
            prompt="p", schedule="every 1h", name="dead-owner", deliver="telegram:-100:1",
        )

        record = executions_mod.create_execution(job["id"], source="builtin")
        dead_pid = int(
            subprocess.run(
                [sys.executable, "-c", "import os; print(os.getpid())"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
        )
        with executions_mod._transaction() as conn:
            conn.execute(
                "UPDATE executions SET process_id='dead-owner', pid=?, "
                "process_started_at=NULL WHERE id=?",
                (dead_pid, record["id"]),
            )

        delivered = []
        monkeypatch.setattr(
            scheduler_mod, "_deliver_result", lambda j, c, **kw: delivered.append(c) or None,
        )
        monkeypatch.setattr(
            scheduler_mod, "_resolve_delivery_targets",
            lambda j: [{"platform": "telegram", "chat_id": "-100", "thread_id": None}],
        )
        monkeypatch.setattr(scheduler_mod, "get_due_jobs", lambda: [])
        monkeypatch.setattr(
            "tools.mcp_tool._kill_orphaned_mcp_children", lambda: None, raising=False,
        )

        monkeypatch.setattr(scheduler_mod, "_last_dead_owner_reap_at", None)
        scheduler_mod.tick(verbose=False)
        assert len(delivered) == 1

        monkeypatch.setattr(scheduler_mod, "_last_dead_owner_reap_at", None)
        scheduler_mod.tick(verbose=False)
        assert len(delivered) == 1, "second reap must not re-deliver the already-transitioned row"

        assert cron_jobs.get_job(job["id"])["last_status"] == "unknown"


class TestNotifyLateOutcome:
    def _job(self, deliver="telegram:-100:1"):
        return {"id": "job-late", "name": "late job", "deliver": deliver}

    def test_no_op_when_result_has_no_late_outcome(self, monkeypatch):
        called = []
        monkeypatch.setattr(scheduler_mod, "_deliver_result", lambda *a, **k: called.append(1))

        scheduler_mod._notify_late_outcome(None, self._job())
        scheduler_mod._notify_late_outcome({"id": "exec-1"}, self._job())

        assert called == []

    def test_delivers_one_liner_and_records_delivery_outcome(self, monkeypatch):
        delivered = []
        monkeypatch.setattr(
            scheduler_mod, "_deliver_result",
            lambda job_arg, text, **kw: delivered.append(text) or None,
        )
        monkeypatch.setattr(
            scheduler_mod, "_resolve_delivery_targets",
            lambda j: [{"platform": "telegram", "chat_id": "-100", "thread_id": None}],
        )
        recorded = []
        monkeypatch.setattr(
            "cron.executions.set_late_outcome_delivery_outcome",
            lambda rowid, outcome: recorded.append((rowid, outcome)),
        )
        emitted = []
        monkeypatch.setattr(
            "agent.monitoring.cron_health.emit_execution_state",
            lambda record, **kw: emitted.append(kw.get("delivery_outcome")),
        )

        result = {
            "id": "exec-late",
            "status": "completed",
            "late_outcome": {"rowid": 42, "success": True, "error": None, "delivery_outcome": None},
        }
        scheduler_mod._notify_late_outcome(result, self._job(), adapters=None, loop=None)

        assert len(delivered) == 1
        assert "late outcome for run exec-late" in delivered[0]
        assert "succeeded" in delivered[0]
        assert "reclassified unconfirmed" in delivered[0]
        assert recorded == [(42, "delivered")]
        assert emitted == ["delivered"]

    def test_failed_word_used_for_a_failed_late_completion(self, monkeypatch):
        delivered = []
        monkeypatch.setattr(
            scheduler_mod, "_deliver_result", lambda j, text, **kw: delivered.append(text) or None,
        )
        monkeypatch.setattr(
            scheduler_mod, "_resolve_delivery_targets",
            lambda j: [{"platform": "telegram", "chat_id": "-100", "thread_id": None}],
        )
        monkeypatch.setattr("cron.executions.set_late_outcome_delivery_outcome", lambda *a, **k: None)
        monkeypatch.setattr("agent.monitoring.cron_health.emit_execution_state", lambda *a, **k: None)

        result = {
            "id": "exec-late-2",
            "late_outcome": {"rowid": 7, "success": False, "error": "boom", "delivery_outcome": None},
        }
        scheduler_mod._notify_late_outcome(result, self._job())

        assert "failed" in delivered[0]

    def test_delivery_failure_is_recorded_and_swallowed(self, monkeypatch):
        monkeypatch.setattr(scheduler_mod, "_deliver_result", lambda *a, **k: "transport down")
        monkeypatch.setattr(
            scheduler_mod, "_resolve_delivery_targets",
            lambda j: [{"platform": "telegram", "chat_id": "-100", "thread_id": None}],
        )
        recorded = []
        monkeypatch.setattr(
            "cron.executions.set_late_outcome_delivery_outcome",
            lambda rowid, outcome: recorded.append(outcome),
        )
        monkeypatch.setattr("agent.monitoring.cron_health.emit_execution_state", lambda *a, **k: None)

        result = {"id": "exec-late-3", "late_outcome": {"rowid": 1, "success": True, "error": None}}
        scheduler_mod._notify_late_outcome(result, self._job())

        assert recorded == ["failed"]


class TestNotifyLateOutcomeWiredAtFinishExecutionCallSite:
    def test_run_one_job_invokes_late_outcome_notify_with_finish_execution_result(self, monkeypatch):
        """Wiring check: run_one_job's terminal finish_execution call site
        forwards whatever finish_execution returns straight into
        _notify_late_outcome (job, adapters, loop) -- exercised end-to-end
        for a normal completion in
        test_execution_ledger.py::test_run_one_job_records_running_then_terminal;
        here we only prove the wiring calls through, not the delivery detail
        (covered by TestNotifyLateOutcome above)."""
        finish_result = {"id": "exec-wired", "late_outcome": {"rowid": 9, "success": True, "error": None}}

        monkeypatch.setattr(scheduler_mod, "mark_execution_running", lambda *_a, **_k: None, raising=False)
        monkeypatch.setattr(scheduler_mod, "finish_execution", lambda *_a, **_k: finish_result, raising=False)
        monkeypatch.setattr(scheduler_mod, "claim_dispatch", lambda _job_id: True)
        monkeypatch.setattr(
            scheduler_mod, "run_job",
            lambda job, *, defer_agent_teardown=None, **_kw: (True, "output", "response", None),
        )
        monkeypatch.setattr(scheduler_mod, "save_job_output", lambda *_args: None)
        monkeypatch.setattr(scheduler_mod, "_deliver_result", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(scheduler_mod, "mark_job_run", lambda *_args, **_kwargs: None)

        notified = []
        monkeypatch.setattr(
            scheduler_mod, "_notify_late_outcome",
            lambda result, job, **kw: notified.append((result, job.get("id"))),
        )

        assert scheduler_mod.run_one_job({"id": "job-wired", "execution_id": "exec-wired"}) is True
        assert notified == [(finish_result, "job-wired")]
