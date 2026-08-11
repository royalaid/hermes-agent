"""Contract checks for the native-Windows updater skill and CLI envelope."""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = (
    REPO_ROOT
    / "optional-skills"
    / "autonomous-ai-agents"
    / "windows-update-readiness"
)
SKILL_PATH = SKILL_DIR / "SKILL.md"
OPENAI_YAML_PATH = SKILL_DIR / "agents" / "openai.yaml"
SCHEMA_PATH = REPO_ROOT / "hermes_cli" / "update_readiness.schema.v1.json"
GENERATED_PAGE_PATH = (
    REPO_ROOT
    / "website"
    / "docs"
    / "user-guide"
    / "skills"
    / "optional"
    / "autonomous-ai-agents"
    / "autonomous-ai-agents-windows-update-readiness.md"
)
CATALOG_PATH = REPO_ROOT / "website" / "docs" / "reference" / "optional-skills-catalog.md"

REQUIRED_SECTIONS = [
    "## When to Use",
    "## Prerequisites",
    "## How to Run",
    "## Quick Reference",
    "## Procedure",
    "## Pitfalls",
    "## Verification",
]
TOP_LEVEL_KEYS = {
    "schema_version",
    "mode",
    "ok",
    "ready",
    "blocked",
    "reason",
    "root",
    "venv",
    "processes",
    "mcp_bridges",
    "pausable_gateways",
    "pausable_gateway_processes",
    "git",
    "last_update_receipt",
    "lease",
    "actions",
    "error",
}
ALLOWED_UPDATE_COMMANDS = {
    "hermes update --preflight --json",
    "hermes update --drain --yes --json",
    "hermes update --yes",
}


def _content() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


def _normalized_content() -> str:
    return " ".join(_content().split())


def _frontmatter_and_body() -> tuple[dict[str, str], str]:
    content = _content()
    assert content.startswith("---"), "SKILL.md must open with frontmatter"
    match = re.search(r"\n---\s*\n", content[3:])
    assert match, "frontmatter must close with ---"
    frontmatter_text = content[3 : match.start() + 3]
    body = content[match.end() + 3 :]
    frontmatter = {}
    for line in frontmatter_text.splitlines():
        field = re.match(r"^(\w[\w-]*):\s*(.*)$", line)
        if field:
            frontmatter[field.group(1)] = field.group(2).strip().strip('"')
    return frontmatter, body


def _schema() -> dict[str, object]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _assert_readiness_contract(payload: dict[str, object]) -> None:
    """Assert the safety semantics the skill must apply to a CLI document."""
    from hermes_cli.update_cmd import validate_update_readiness

    schema = _schema()
    assert validate_update_readiness(payload) is payload
    assert set(payload) == set(schema["required"])
    assert payload["schema_version"] == 1
    assert payload["mode"] in {"preflight", "drain"}
    assert all(type(payload[key]) is bool for key in ("ok", "ready", "blocked"))
    assert payload["blocked"] is (not payload["ready"])
    assert payload["reason"] is None or isinstance(payload["reason"], str)
    assert all(
        isinstance(payload[key], str) and os.path.isabs(payload[key])
        for key in ("root", "venv")
    )
    for key in ("processes", "mcp_bridges", "pausable_gateway_processes", "actions"):
        assert isinstance(payload[key], list)
    assert type(payload["pausable_gateways"]) is int
    assert payload["pausable_gateways"] >= 0
    assert payload["pausable_gateways"] == len(payload["pausable_gateway_processes"])

    if payload["ready"]:
        assert payload["ok"] is True
        assert payload["reason"] is None
        assert payload["processes"] == []
        assert payload["mcp_bridges"] == []
        assert payload["error"] is None
    elif payload["ok"]:
        assert isinstance(payload["reason"], str) and payload["reason"]
    else:
        assert payload["ready"] is False
        assert isinstance(payload["error"], dict)

    process_required = set(schema["$defs"]["process"]["required"])
    for process in payload["processes"]:
        assert set(process) in (process_required, process_required | {"created_at"})
        assert type(process["pid"]) is int and process["pid"] > 0
        assert isinstance(process["name"], str)
        assert isinstance(process["cmdline"], str)
        assert process["actionable"] is False
        assert process["actionability"] == "hard_block"
        assert process["action"] == "refuse"

    bridge_required = set(schema["$defs"]["mcpBridge"]["required"])
    actionable_identities = set()
    for bridge in payload["mcp_bridges"]:
        assert set(bridge) in (bridge_required, bridge_required | {"wrapper_pid"})
        assert type(bridge["pid"]) is int and bridge["pid"] > 0
        assert isinstance(bridge["name"], str)
        assert isinstance(bridge["cmdline"], str)
        assert isinstance(bridge["created_at"], (int, float))
        assert math.isfinite(bridge["created_at"]) and bridge["created_at"] > 0
        assert bridge["role"] in {"mcp_bridge_wrapper", "mcp_bridge_worker"}
        if bridge["actionable"]:
            assert bridge["owner"] in {"codex", "claude"}
            assert bridge["actionability"] == "exact_mcp_bridge"
            assert bridge["action"] == "terminate_exact_mcp"
            actionable_identities.add((bridge["pid"], float(bridge["created_at"])))
        else:
            assert bridge["actionability"] == "hard_block"
            assert bridge["action"] == "refuse"

    for gateway in payload["pausable_gateway_processes"]:
        assert gateway["owner"] == "gateway"
        assert gateway["role"] == "gateway_run"
        assert gateway["actionable"] is False
        assert gateway["actionability"] == "downstream_drainable"
        assert gateway["action"] == "pause_downstream"

    for action in payload["actions"]:
        if action["type"] == "terminate-mcp-bridge":
            identity = (action["pid"], float(action["created_at"]))
            assert action["owner"] in {"codex", "claude"}
            assert action["role"] in {"mcp_bridge_wrapper", "mcp_bridge_worker"}
            assert set(action) in (
                {"type", "pid", "created_at", "owner", "role"},
                {"type", "pid", "created_at", "owner", "role", "terminated"},
            )
            if "terminated" not in action:
                assert identity in actionable_identities
            else:
                assert type(action["terminated"]) is bool
                assert payload["mode"] == "drain"
        else:
            assert action["type"] == "clear-scan"
            assert set(action) == {"type", "sequence"}
            assert action["sequence"] in {1, 2}
            assert payload["mode"] == "drain"

    if payload["mode"] == "drain" and payload["ready"]:
        clear_proof = [
            action["sequence"]
            for action in payload["actions"]
            if action["type"] == "clear-scan"
        ]
        assert clear_proof == [1, 2]
        assert payload["actions"][-2:] == [
            {"type": "clear-scan", "sequence": 1},
            {"type": "clear-scan", "sequence": 2},
        ]

    git = payload["git"]
    if git is not None:
        assert set(git) == set(schema["$defs"]["git"]["required"])
        assert type(git["dirty"]) is bool

    lease = payload["lease"]
    if lease is not None:
        assert set(lease) == set(schema["$defs"]["lease"]["required"])
        assert lease["schema_version"] == 1
        assert re.fullmatch(r"[0-9a-f]{64}", lease["lease_fingerprint"])
        assert "lease_id" not in lease
        assert lease["install_root"] == payload["root"]
        assert 0 < lease["created_at"] <= lease["handoff_grace_until"] <= lease["expires_at"]

    receipt = payload["last_update_receipt"]
    if receipt is not None:
        assert set(receipt) == set(schema["$defs"]["receipt"]["required"])
        assert receipt["schema_version"] == 1
        assert receipt["root"] == payload["root"]
        assert receipt["success"] is True
        assert type(receipt["gateway_resume_deferred"]) is bool
        health = receipt["health"]
        assert set(health) == set(schema["$defs"]["health"]["required"])
        assert all(type(value) is bool for value in health.values())
        assert all(health.values())

    error = payload["error"]
    if error is not None:
        assert set(error) == set(schema["$defs"]["error"]["required"])
        assert all(isinstance(error[key], str) for key in ("code", "message"))


def _git_fixture() -> dict[str, object]:
    return {
        "head": "a" * 40,
        "branch": "main",
        "dirty": False,
        "tracking_remote": "origin",
        "target_branch": "main",
        "target_ref": "refs/remotes/origin/main",
        "target_sha": "b" * 40,
    }


def _lease_fixture(root: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "lease_id": "lease-readiness-123456",
        "owner_pid": 101,
        "created_at": 100,
        "handoff_grace_until": 200,
        "expires_at": 300,
        "install_root": os.path.normcase(os.path.realpath(root)),
    }


def _receipt_fixture(root: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "invocation_id": "invocation-test-123456",
        "lease_id": "lease-readiness-123456",
        "mode": "git",
        "root": os.path.normcase(os.path.realpath(root)),
        "remote": "origin",
        "branch": "main",
        "target_ref": "refs/remotes/origin/main",
        "target_sha": "b" * 40,
        "resulting_head": "b" * 40,
        "archive_sha": None,
        "timestamp": 100,
        "success": True,
        "gateway_resume_deferred": False,
        "health": {
            "critical_syntax": True,
            "critical_imports": True,
            "dependencies": True,
            "node_dependencies": True,
        },
    }


def _run_real_preflight_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    scan: dict[str, object] | Callable[[Path], dict[str, object]],
    *,
    receipt: dict[str, object] | None = None,
    lease: dict[str, object] | None = None,
) -> tuple[int, dict[str, object]]:
    """Exercise the production preflight builder and JSON-printing CLI seam."""
    from hermes_cli import _scan_venv_blockers, update_cmd, update_readiness
    import hermes_mcp_update_gate

    (tmp_path / "venv").mkdir(exist_ok=True)
    scanner = scan if callable(scan) else lambda _root: scan
    monkeypatch.setattr(_scan_venv_blockers, "scan_venv_blockers", scanner)
    monkeypatch.setattr(
        update_readiness,
        "_git_preflight_metadata",
        lambda _root, _branch: _git_fixture(),
    )
    monkeypatch.setattr(update_readiness, "_load_update_receipt", lambda _root: receipt)
    monkeypatch.setattr(update_readiness, "_read_update_holder_read_only", lambda: None)
    monkeypatch.setattr(hermes_mcp_update_gate, "marker_path", lambda: tmp_path / "lease.json")
    monkeypatch.setattr(
        hermes_mcp_update_gate,
        "live_quiesce_lease",
        lambda _marker, *, install_root: lease,
    )

    with pytest.raises(SystemExit) as stopped:
        update_cmd._cmd_update_preflight(
            SimpleNamespace(branch="main", json=True),
            root=tmp_path,
        )
    stdout = capsys.readouterr().out
    assert stdout.count("\n") == 1
    return int(stopped.value.code), json.loads(stdout)


def test_skill_asset_and_codex_metadata_exist():
    assert SKILL_PATH.is_file()
    assert OPENAI_YAML_PATH.is_file()
    assert SCHEMA_PATH.is_file()


def test_skill_uses_repository_required_h1():
    _, body = _frontmatter_and_body()
    assert body.lstrip().startswith("# Windows Update Readiness Skill\n")


def test_frontmatter_is_windows_only_and_uses_configured_human_first():
    frontmatter, _ = _frontmatter_and_body()
    assert frontmatter["name"] == "windows-update-readiness"
    assert re.fullmatch(
        r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
        r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?",
        frontmatter["version"],
    )
    assert frontmatter["author"] == "Royalaid, Hermes Agent"
    assert frontmatter["platforms"] == "[windows]"


def test_description_meets_repository_hardline():
    frontmatter, _ = _frontmatter_and_body()
    description = frontmatter["description"]
    assert len(description) <= 60
    assert description.endswith(".")
    assert description.count(".") == 1


def test_required_sections_are_present_in_order():
    _, body = _frontmatter_and_body()
    positions = [body.find(section) for section in REQUIRED_SECTIONS]
    assert all(position >= 0 for position in positions)
    assert positions == sorted(positions)


def test_only_allowed_updater_commands_are_present():
    commands = {
        command.strip()
        for command in re.findall(r"`([^`\r\n]*hermes update[^`\r\n]*)`", _content())
    }
    fenced = {
        line.strip()
        for line in _content().splitlines()
        if line.strip().startswith("hermes update")
    }
    assert commands | fenced == ALLOWED_UPDATE_COMMANDS
    assert "Use `terminal` with a bounded timeout" in _content()


def test_real_ready_preflight_fixture_matches_canonical_schema(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
):
    scan = {
        "processes": [],
        "mcp_bridges": [],
        "pausable_gateways": 0,
        "pausable_gateway_processes": [],
    }
    exit_code, payload = _run_real_preflight_cli(monkeypatch, capsys, tmp_path, scan)
    schema = _schema()

    assert set(schema["required"]) == TOP_LEVEL_KEYS
    assert schema["additionalProperties"] is False
    assert set(payload) == TOP_LEVEL_KEYS
    assert exit_code == 0
    assert payload["mode"] == "preflight"
    assert (payload["ok"], payload["ready"], payload["blocked"]) == (True, True, False)
    _assert_readiness_contract(payload)


def test_real_preflight_blocks_on_active_standalone_drain_lease(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
):
    from hermes_cli import update_cmd

    scan = {
        "processes": [],
        "mcp_bridges": [],
        "pausable_gateways": 0,
        "pausable_gateway_processes": [],
    }
    lease = _lease_fixture(tmp_path)
    exit_code, payload = _run_real_preflight_cli(
        monkeypatch,
        capsys,
        tmp_path,
        scan,
        lease=lease,
    )

    assert exit_code == 2
    assert payload["ready"] is False
    assert payload["reason"] == "quiesce-lease-active"
    from hermes_cli import update_readiness

    assert payload["lease"] == update_readiness._public_quiesce_lease(lease)
    assert "lease_id" not in payload["lease"]
    _assert_readiness_contract(payload)

    overclaim = json.loads(json.dumps(payload))
    overclaim.update(ready=True, blocked=False, reason=None)
    from hermes_cli.update_cmd import validate_update_readiness

    with pytest.raises(ValueError):
        validate_update_readiness(overclaim)


def test_real_preflight_exposes_only_a_valid_all_healthy_receipt(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
):
    from hermes_cli.update_cmd import validate_update_readiness

    scan = {
        "processes": [],
        "mcp_bridges": [],
        "pausable_gateways": 0,
        "pausable_gateway_processes": [],
    }
    receipt = _receipt_fixture(tmp_path)
    exit_code, payload = _run_real_preflight_cli(
        monkeypatch,
        capsys,
        tmp_path,
        scan,
        receipt=receipt,
    )

    assert exit_code == 0
    assert payload["last_update_receipt"] == receipt
    _assert_readiness_contract(payload)

    for field in receipt["health"]:
        unhealthy = json.loads(json.dumps(payload))
        unhealthy["last_update_receipt"]["health"][field] = False
        with pytest.raises(ValueError):
            validate_update_readiness(unhealthy)


def test_skill_names_the_normative_versioned_schema():
    assert "`hermes_cli/update_readiness.schema.v1.json` as normative" in _content()


def test_real_blocked_preflight_fixture_preserves_supported_bridge_identity(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
):
    bridges = [
        {
            "pid": 101,
            "name": "python.exe",
            "cmdline": "<redacted>",
            "created_at": 1001.25,
            "owner": "codex",
            "role": "mcp_bridge_worker",
            "actionable": True,
            "actionability": "exact_mcp_bridge",
            "action": "terminate_exact_mcp",
            "wrapper_pid": 102,
        },
        {
            "pid": 102,
            "name": "python.exe",
            "cmdline": "<redacted>",
            "created_at": 1001.0,
            "owner": "claude",
            "role": "mcp_bridge_wrapper",
            "actionable": True,
            "actionability": "exact_mcp_bridge",
            "action": "terminate_exact_mcp",
        },
    ]
    scan = {
        "processes": [],
        "mcp_bridges": bridges,
        "pausable_gateways": 0,
        "pausable_gateway_processes": [],
    }
    exit_code, payload = _run_real_preflight_cli(monkeypatch, capsys, tmp_path, scan)
    assert exit_code == 2
    assert payload["reason"] == "mcp-bridges-running"
    assert payload["mcp_bridges"] == bridges
    assert payload["actions"] == [
        {
            "type": "terminate-mcp-bridge",
            "pid": 101,
            "created_at": 1001.25,
            "owner": "codex",
            "role": "mcp_bridge_worker",
        },
        {
            "type": "terminate-mcp-bridge",
            "pid": 102,
            "created_at": 1001.0,
            "owner": "claude",
            "role": "mcp_bridge_wrapper",
        },
    ]
    _assert_readiness_contract(payload)

    incoherent = json.loads(json.dumps(payload))
    incoherent["mcp_bridges"][0]["owner"] = "unknown"
    from hermes_cli.update_cmd import validate_update_readiness

    with pytest.raises(ValueError, match="exact owner/action contract"):
        validate_update_readiness(incoherent)

    mismatched_action = json.loads(json.dumps(payload))
    mismatched_action["actions"][0]["owner"] = "claude"
    with pytest.raises(ValueError):
        validate_update_readiness(mismatched_action)

    invalid_name = json.loads(json.dumps(payload))
    invalid_name["mcp_bridges"][0]["name"] = 123
    with pytest.raises(AssertionError):
        _assert_readiness_contract(invalid_name)

    invalid_cmdline = json.loads(json.dumps(payload))
    invalid_cmdline["mcp_bridges"][0]["cmdline"] = {"redacted": True}
    with pytest.raises(AssertionError):
        _assert_readiness_contract(invalid_cmdline)


def test_real_probe_failure_fixture_is_exit_one_and_schema_valid(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
):
    def fail_probe(_root: Path) -> dict[str, object]:
        raise RuntimeError("sanitized probe failure")

    exit_code, payload = _run_real_preflight_cli(
        monkeypatch, capsys, tmp_path, fail_probe
    )
    assert exit_code == 1
    assert payload["ok"] is False
    assert payload["ready"] is False
    assert payload["blocked"] is True
    assert payload["reason"] == "probe-failed"
    assert payload["error"]["code"] == "probe-failed"
    _assert_readiness_contract(payload)


@pytest.mark.parametrize(
    ("owner", "actionable"),
    [("codex", True), ("claude", True), ("desktop", False), ("unknown", False)],
)
def test_production_scanner_actionability_matches_supported_owner_contract(
    monkeypatch: pytest.MonkeyPatch,
    owner: str,
    actionable: bool,
):
    from hermes_cli import _scan_venv_blockers as scanner

    snapshot = scanner._ProcessSnapshot(
        pid=101,
        ppid=100,
        name="python.exe",
        exe="C:/managed/venv/Scripts/python.exe",
        argv=("python.exe", "-m", "agent.transports.hermes_tools_mcp_server"),
        created_at=1001.0,
        process=object(),
    )
    monkeypatch.setattr(scanner, "_owner_from_ancestry", lambda _snapshot: owner)
    record = scanner._mcp_record(
        snapshot,
        role="mcp_bridge_wrapper",
        wrapper_pid=snapshot.pid,
    )

    assert record["actionable"] is actionable
    if actionable:
        assert record["actionability"] == "exact_mcp_bridge"
        assert record["action"] == "terminate_exact_mcp"
    else:
        assert record["actionability"] == "hard_block"
        assert record["action"] == "refuse"


def test_production_validator_requires_exact_successful_drain_clear_proof(
    tmp_path: Path,
):
    from hermes_cli import update_cmd, update_readiness

    historical_termination = {
        "type": "terminate-mcp-bridge",
        "pid": 101,
        "created_at": 1001.0,
        "owner": "codex",
        "role": "mcp_bridge_worker",
        "terminated": True,
    }

    def payload_for(actions: list[dict[str, object]]) -> dict[str, object]:
        return update_readiness._readiness_payload(
            mode="drain",
            root=tmp_path,
            lease=_lease_fixture(tmp_path),
            ok=True,
            ready=True,
            actions=actions,
        )

    clear_proof = [
        {"type": "clear-scan", "sequence": 1},
        {"type": "clear-scan", "sequence": 2},
    ]
    valid = payload_for([historical_termination, *clear_proof])
    assert update_cmd.validate_update_readiness(valid) is valid
    _assert_readiness_contract(valid)

    invalid_actions = [
        [],
        [{"type": "clear-scan", "sequence": 1}],
        [
            {"type": "clear-scan", "sequence": 2},
            {"type": "clear-scan", "sequence": 1},
        ],
        [
            {"type": "clear-scan", "sequence": 1},
            {"type": "clear-scan", "sequence": 1},
            {"type": "clear-scan", "sequence": 2},
        ],
        [
            {"type": "clear-scan", "sequence": 1},
            {"type": "clear-scan", "sequence": 2},
            {"type": "clear-scan", "sequence": 2},
        ],
        [*clear_proof, historical_termination],
    ]
    for invalid in invalid_actions:
        with pytest.raises(ValueError):
            update_cmd.validate_update_readiness(payload_for(invalid))

    missing_lease = update_readiness._readiness_payload(
        mode="drain",
        root=tmp_path,
        ok=True,
        ready=True,
        actions=clear_proof,
    )
    with pytest.raises(ValueError):
        update_cmd.validate_update_readiness(missing_lease)


def test_production_validator_rejects_drain_only_actions_in_preflight(
    tmp_path: Path,
):
    from hermes_cli import update_cmd, update_readiness

    drain_only_actions = [
        {"type": "clear-scan", "sequence": 1},
        {
            "type": "terminate-mcp-bridge",
            "pid": 101,
            "created_at": 1001.0,
            "owner": "claude",
            "role": "mcp_bridge_wrapper",
            "terminated": True,
        },
    ]
    for action in drain_only_actions:
        payload = update_readiness._readiness_payload(
            mode="preflight",
            root=tmp_path,
            ok=True,
            ready=True,
            actions=[action],
        )
        with pytest.raises(ValueError):
            update_cmd.validate_update_readiness(payload)


def test_canonical_schema_defines_safety_critical_nested_contracts():
    schema = _schema()
    definitions = schema["$defs"]
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert set(definitions["mcpBridge"]["required"]) == {
        "pid",
        "name",
        "cmdline",
        "created_at",
        "owner",
        "role",
        "actionable",
        "actionability",
        "action",
    }
    assert definitions["mcpBridge"]["properties"]["name"] == {"type": "string"}
    assert definitions["mcpBridge"]["properties"]["cmdline"] == {"type": "string"}
    assert set(definitions["lease"]["required"]) == {
        "schema_version",
        "lease_fingerprint",
        "owner_pid",
        "created_at",
        "expires_at",
        "handoff_grace_until",
        "install_root",
    }
    assert set(definitions["receipt"]["required"]) == {
        "schema_version",
        "invocation_id",
        "lease_id",
        "mode",
        "root",
        "remote",
        "branch",
        "target_ref",
        "target_sha",
        "resulting_head",
        "archive_sha",
        "timestamp",
        "success",
        "gateway_resume_deferred",
        "health",
    }
    assert definitions["receipt"]["properties"]["gateway_resume_deferred"] == {
        "type": "boolean"
    }
    assert set(definitions["health"]["required"]) == {
        "critical_syntax",
        "critical_imports",
        "dependencies",
        "node_dependencies",
    }
    terminate_action, clear_action = definitions["action"]["oneOf"]
    assert set(terminate_action["required"]) == {
        "type",
        "pid",
        "created_at",
        "owner",
        "role",
    }
    assert set(clear_action["required"]) == {"type", "sequence"}


def test_skill_keeps_readiness_pause_and_update_lifecycles_separate():
    content = _normalized_content()
    assert "For a readiness-only request" in content
    assert "stop. Do not drain." in content
    assert "temporary pause request" in content
    assert "no update was applied" in content
    assert "Do not call the drain-only command first" in content
    assert "run `hermes update --yes` directly" in content
    assert "final two `actions`" in content
    assert "missing, duplicate, out-of-order, or trailing-action proof fails closed" in content


def test_atomic_update_requires_prospective_interruption_consent():
    content = _normalized_content()
    assert "This skill is the authorization trust boundary for `--yes`" in content
    assert "prospective consent before every standalone" in content
    assert "even when preflight is initially clear" in content
    assert "launched after preflight may be paused" in content
    assert "an initially clear scan is not interruption consent" in content


def test_destructive_shortcuts_are_explicitly_forbidden():
    content = _content()
    for forbidden in (
        "Never enumerate processes directly",
        "Never run `taskkill`, `Stop-Process`",
        "Never create, delete, or rewrite updater markers or quiesce leases",
        "Never use `--force-venv`",
        "Never rebase, change remotes",
        "Never classify `mcp_server` as Desktop `serve` by substring",
    ):
        assert forbidden in content


def test_post_check_requires_current_receipt_health_and_relaunch_proof():
    content = _normalized_content()
    assert "new `invocation_id` and `lease_id` values versus pre-state" in content
    assert "timestamp after this update command began" in content
    assert "all health checks true" in content
    assert "boolean `gateway_resume_deferred`" in content
    assert "require the final ready result with `lease=null`" in content
    assert "installed identity agrees with `git` and the intended target" in content
    assert "successful build/relaunch proof" in content
    assert "readiness is verified and update completion is not" in content
    assert "this skill alone cannot claim completion for a Desktop-driven update" in content


def test_shared_agent_layout_and_codex_prompt_are_declared():
    content = _content()
    metadata = OPENAI_YAML_PATH.read_text(encoding="utf-8")
    assert "~/.agents/skills/" in content
    assert "skills.external_dirs" in content
    assert 'display_name: "Windows Update Readiness"' in metadata
    assert "$windows-update-readiness" in metadata


def test_generated_page_and_catalog_are_scoped_and_current():
    _, body = _frontmatter_and_body()
    page = GENERATED_PAGE_PATH.read_text(encoding="utf-8")
    second_heading = page.find("# Windows Update Readiness", page.find("# Windows Update Readiness") + 1)
    assert second_heading >= 0
    assert page[second_heading:].strip() == body.strip()
    route = "autonomous-ai-agents/autonomous-ai-agents-windows-update-readiness"
    assert route in CATALOG_PATH.read_text(encoding="utf-8")


def test_skill_has_no_process_manipulation_helpers():
    assert not (SKILL_DIR / "scripts").exists()
    assert not (SKILL_DIR / "references").exists()
    assert not (SKILL_DIR / "assets").exists()
