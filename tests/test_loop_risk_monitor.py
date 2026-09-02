"""Behavioral tests for the read-only Loop Risk Monitor scout."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts.loop_risk_monitor import (
    HFL01_ID,
    HFL03_ID,
    HFL04_ID,
    SessionTrace,
    detect_hfl01,
    detect_hfl03,
    detect_hfl04,
    open_read_only,
    scan_database,
)


def _trace(rows, *, session_id="fixture-session", **metadata):
    return SessionTrace(
        session_id=session_id,
        started_at=metadata.pop("started_at", 100.0),
        ended_at=metadata.pop("ended_at", 200.0),
        metadata=metadata,
        rows=rows,
    )


def _tool_turn(message_id, call_id, command, result, *, timestamp=None):
    timestamp = float(message_id) if timestamp is None else timestamp
    return [
        {
            "id": message_id,
            "role": "assistant",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "terminal",
                        "arguments": json.dumps({"command": command}),
                    },
                }
            ],
            "timestamp": timestamp,
        },
        {
            "id": message_id + 1,
            "role": "tool",
            "tool_call_id": call_id,
            "tool_name": "terminal",
            "content": json.dumps({"error": result, "exit_code": 1}),
            "timestamp": timestamp + 0.1,
        },
    ]


def test_hfl01_fires_for_three_identical_failed_calls_without_new_evidence():
    rows = []
    for index in range(3):
        rows.extend(
            _tool_turn(
                10 + index * 2,
                f"call-{index}",
                "python -m pytest tests/example.py",
                "No module named pytest",
            )
        )

    signals = detect_hfl01(_trace(rows), min_repeats=3)

    assert len(signals) == 1
    signal = signals[0].as_dict()
    assert signal["pattern_id"] == HFL01_ID
    assert signal["session_id"] == "fixture-session"
    assert signal["message_ids"] == [10, 11, 12, 13, 14, 15]
    assert signal["details"]["repeat_count"] == 3
    assert len(signal["evidence_lines"]) == 3
    assert all("No module named" not in line for line in signal["evidence_lines"])


@pytest.mark.parametrize(
    "middle_rows",
    [
        [{"id": 99, "role": "user", "content": "Try a different diagnosis."}],
        [
            {
                "id": 99,
                "role": "tool",
                "tool_call_id": "other",
                "tool_name": "read_file",
                "content": "new file contents",
            }
        ],
    ],
)
def test_hfl01_does_not_fire_after_intervening_evidence(middle_rows):
    rows = _tool_turn(10, "call-0", "python -m pytest tests/example.py", "same failure")
    rows += middle_rows
    rows += _tool_turn(20, "call-1", "python -m pytest tests/example.py", "same failure")
    rows += _tool_turn(30, "call-2", "python -m pytest tests/example.py", "same failure")

    assert detect_hfl01(_trace(rows), min_repeats=3) == []


def test_hfl01_does_not_fire_when_arguments_or_outcome_change():
    rows = _tool_turn(10, "call-0", "python -m pytest tests/example.py", "same failure")
    rows += _tool_turn(20, "call-1", "python -m pytest tests/other.py", "same failure")
    rows += _tool_turn(30, "call-2", "python -m pytest tests/example.py", "fixed")

    assert detect_hfl01(_trace(rows), min_repeats=3) == []


def test_hfl03_detects_both_path_dialect_directions_and_preserves_redaction():
    trace = _trace(
        [
            {
                "id": 1,
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "native",
                        "function": {
                            "name": "terminal",
                            "arguments": json.dumps(
                                {"command": "python /c/Users/example/project.py"}
                            ),
                        },
                    },
                    {
                        "id": "posix",
                        "function": {
                            "name": "terminal",
                            "arguments": json.dumps(
                                {"consumer_runtime": "posix", "command": "grep C:/temp/out.txt"}
                            ),
                        },
                    },
                ],
            }
        ]
    )

    signals = detect_hfl03(trace)

    assert len(signals) == 2
    assert {signal.details["path_dialect"] for signal in signals} == {"msys", "windows"}
    assert all(signal.pattern_id == HFL03_ID for signal in signals)
    assert all("/c/Users" not in line and "C:/temp" not in line for signal in signals for line in signal.evidence_lines)


def test_hfl03_allows_paths_matching_the_consumer_runtime():
    trace = _trace(
        [
            {
                "id": 1,
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "native",
                        "function": {
                            "name": "terminal",
                            "arguments": json.dumps({"command": "python C:/temp/out.txt"}),
                        },
                    },
                    {
                        "id": "posix",
                        "function": {
                            "name": "terminal",
                            "arguments": json.dumps({"command": "grep /tmp/out.txt"}),
                        },
                    },
                ],
            }
        ]
    )

    assert detect_hfl03(trace) == []


def test_hfl03_ignores_context_redirections_and_embedded_code():
    trace = _trace(
        [
            {
                "id": 1,
                "role": "assistant",
                "cwd": "C:/workspace/project",
                "env": {"HERMES_HOME": "/tmp/hermes"},
                "tool_calls": [
                    {
                        "id": "context-only",
                        "function": {
                            "name": "terminal",
                            "arguments": json.dumps(
                                {
                                    "command": "git status >/dev/null 2>&1",
                                    "cwd": "C:/workspace/project",
                                    "env": {"HERMES_HOME": "/tmp/hermes"},
                                }
                            ),
                        },
                    },
                    {
                        "id": "python-code",
                        "function": {
                            "name": "terminal",
                            "arguments": json.dumps(
                                {
                                    "command": "python -c \"from pathlib import Path; print(Path('/c/workspace/project'))\""
                                }
                            ),
                        },
                    },
                    {
                        "id": "perl-code",
                        "function": {
                            "name": "terminal",
                            "arguments": json.dumps(
                                {"command": "perl -ne \"print if /C:/workspace/\""}
                            ),
                        },
                    },
                ],
            }
        ]
    )

    assert detect_hfl03(trace) == []


def test_hfl03_ignores_shell_switches_and_msys_virtual_devices():
    trace = _trace(
        [
            {
                "id": 1,
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "switches",
                        "function": {
                            "name": "terminal",
                            "arguments": json.dumps(
                                {
                                    "command": "cmd.exe /d /s /c \"echo ok\""
                                }
                            ),
                        },
                    },
                    {
                        "id": "devices",
                        "function": {
                            "name": "terminal",
                            "arguments": json.dumps(
                                {"command": "git diff --no-index -- /dev/null /dev/stdin"}
                            ),
                        },
                    },
                ],
            }
        ]
    )

    assert detect_hfl03(trace) == []


def test_hfl03_ignores_url_literals_and_keeps_grep_options_as_arguments():
    trace = _trace(
        [
            {
                "id": 1,
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "url",
                        "function": {
                            "name": "terminal",
                            "arguments": json.dumps(
                                {"command": "gh api https://tmp.example.invalid/result"}
                            ),
                        },
                    },
                    {
                        "id": "grep-options",
                        "function": {
                            "name": "terminal",
                            "arguments": json.dumps(
                                {
                                    "consumer_runtime": "posix",
                                    "command": "grep -ne pattern C:/workspace/result.txt",
                                }
                            ),
                        },
                    },
                ],
            }
        ]
    )

    signals = detect_hfl03(trace)

    assert len(signals) == 1
    assert signals[0].details["path_dialect"] == "windows"


def test_hfl04_detects_overlapping_writers_on_one_branch():
    first = _trace(
        [
            {
                "id": 10,
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "a",
                        "function": {"name": "patch", "arguments": '{"path":"src/a.py"}'},
                    }
                ],
                "timestamp": 120,
            }
        ],
        session_id="writer-a",
        git_branch="feature/shared",
        started_at=100,
        ended_at=160,
        pid=101,
    )
    second = _trace(
        [
            {
                "id": 20,
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "b",
                        "function": {"name": "write_file", "arguments": '{"path":"src/b.py"}'},
                    }
                ],
                "timestamp": 130,
            }
        ],
        session_id="writer-b",
        git_branch="feature/shared",
        started_at=140,
        ended_at=180,
        pid=202,
    )

    signals = detect_hfl04([first, second])

    assert len(signals) == 1
    signal = signals[0].as_dict()
    assert signal["pattern_id"] == HFL04_ID
    assert signal["session_id"] == "writer-a"
    assert signal["details"]["related_session_ids"] == ["writer-b"]
    assert signal["details"]["workstream_kind"] == "branch"
    assert signal["message_ids"] == [10, 20]


def test_hfl04_ignores_sequential_or_different_branch_writers():
    base_rows = [
        {
            "id": 10,
            "role": "assistant",
            "tool_calls": [
                {"id": "a", "function": {"name": "patch", "arguments": "{}"}}
            ],
            "timestamp": 120,
        }
    ]
    first = _trace(base_rows, session_id="first", git_branch="one", started_at=100, ended_at=120)
    sequential = _trace(base_rows, session_id="second", git_branch="one", started_at=121, ended_at=140)
    unrelated = _trace(base_rows, session_id="third", git_branch="two", started_at=105, ended_at=130)

    assert detect_hfl04([first, sequential, unrelated]) == []


def test_hfl04_deduplicates_a_pair_that_shares_branch_and_task():
    first = _trace(
        [
            {
                "id": 10,
                "role": "assistant",
                "tool_calls": [
                    {"id": "a", "function": {"name": "patch", "arguments": "{}"}}
                ],
                "timestamp": 120,
            }
        ],
        session_id="writer-a",
        git_branch="feature/shared",
        task_id="t_shared",
        started_at=100,
        ended_at=160,
    )
    second = _trace(
        [
            {
                "id": 20,
                "role": "assistant",
                "tool_calls": [
                    {"id": "b", "function": {"name": "write_file", "arguments": "{}"}}
                ],
                "timestamp": 130,
            }
        ],
        session_id="writer-b",
        git_branch="feature/shared",
        task_id="t_shared",
        started_at=140,
        ended_at=180,
    )

    signals = detect_hfl04([first, second])

    assert len(signals) == 1
    assert signals[0].details["workstream_kind"] == "branch"


def test_hfl04_detects_mutating_git_after_global_options():
    first = _trace(
        [
            {
                "id": 10,
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "a",
                        "function": {
                            "name": "terminal",
                            "arguments": '{"command":"git -C C:/workspace/repo commit -am save"}',
                        },
                    }
                ],
                "timestamp": 120,
            }
        ],
        session_id="writer-a",
        git_branch="feature/shared",
        started_at=100,
        ended_at=160,
    )
    second = _trace(
        [
            {
                "id": 20,
                "role": "assistant",
                "tool_calls": [
                    {"id": "b", "function": {"name": "patch", "arguments": "{}"}}
                ],
                "timestamp": 130,
            }
        ],
        session_id="writer-b",
        git_branch="feature/shared",
        started_at=140,
        ended_at=180,
    )

    assert len(detect_hfl04([first, second])) == 1


def test_hfl04_redacts_path_like_writer_ids():
    first = _trace(
        [
            {
                "id": 10,
                "role": "assistant",
                "tool_calls": [
                    {"id": "a", "function": {"name": "patch", "arguments": "{}"}}
                ],
                "timestamp": 120,
            }
        ],
        session_id="writer-a",
        git_branch="feature/shared",
        writer_id="C:/Users/example/private/writer-a",
        started_at=100,
        ended_at=160,
    )
    second = _trace(
        [
            {
                "id": 20,
                "role": "assistant",
                "tool_calls": [
                    {"id": "b", "function": {"name": "write_file", "arguments": "{}"}}
                ],
                "timestamp": 130,
            }
        ],
        session_id="writer-b",
        git_branch="feature/shared",
        writer_id="/tmp/private/writer-b",
        started_at=140,
        ended_at=180,
    )

    signal = detect_hfl04([first, second])[0].as_dict()
    serialized = json.dumps(signal, sort_keys=True)

    assert "C:/Users/example" not in serialized
    assert "/tmp/private" not in serialized
    assert signal["details"]["writer_pids"] == [None, None]


def test_read_only_scan_uses_sqlite_mode_ro_and_reads_tool_rows(tmp_path):
    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT,
                started_at REAL,
                ended_at REAL,
                title TEXT,
                git_branch TEXT,
                model_config TEXT,
                origin_json TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                session_id TEXT,
                role TEXT,
                content TEXT,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_name TEXT,
                timestamp REAL,
                active INTEGER DEFAULT 1
            );
            """
        )
        connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("db-session", "cli", 100, 200, "Example", "main", None, None),
        )
        rows = _tool_turn(1, "db-call-0", "python -m pytest tests/example.py", "same failure")
        rows += _tool_turn(3, "db-call-1", "python -m pytest tests/example.py", "same failure")
        rows += _tool_turn(5, "db-call-2", "python -m pytest tests/example.py", "same failure")
        for row in rows:
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    row["id"],
                    "db-session",
                    row["role"],
                    row.get("content"),
                    row.get("tool_call_id"),
                    json.dumps(row["tool_calls"]) if row.get("tool_calls") else None,
                    row.get("tool_name"),
                    row.get("timestamp", row["id"]),
                ),
            )

    with open_read_only(db_path) as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE should_not_exist(value TEXT)")

    result = scan_database(db_path, since=50, min_repeats=3)
    assert result.stats["sessions_scanned"] == 1
    assert result.stats["message_rows_scanned"] == 6
    assert [signal.pattern_id for signal in result.signals] == [HFL01_ID]
