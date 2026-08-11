"""Contract checks for the native-Windows updater skill and CLI envelope."""

from __future__ import annotations

import json
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


def _markdown_section(heading: str) -> str:
    """Return one Markdown section without coupling tests to exact prose."""
    _, body = _frontmatter_and_body()
    marker = f"{heading}\n"
    start = body.index(marker) + len(marker)
    level = len(heading) - len(heading.lstrip("#"))
    following = body[start:]
    match = re.search(rf"(?m)^#{{1,{level}}}\s", following)
    return following[: match.start() if match else None]


def _schema() -> dict[str, object]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _assert_valid_readiness(payload: dict[str, object]) -> None:
    """Use the production validator instead of maintaining a shadow schema."""
    from hermes_cli.update_readiness import validate_update_readiness

    assert validate_update_readiness(payload) is payload
    assert set(payload) == TOP_LEVEL_KEYS


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
    _assert_valid_readiness(payload)


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
    _assert_valid_readiness(payload)

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
    _assert_valid_readiness(payload)

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
    _assert_valid_readiness(payload)

    incoherent = json.loads(json.dumps(payload))
    incoherent["mcp_bridges"][0]["owner"] = "unknown"
    from hermes_cli.update_cmd import validate_update_readiness

    with pytest.raises(ValueError, match="exact owner/action contract"):
        validate_update_readiness(incoherent)

    mismatched_action = json.loads(json.dumps(payload))
    mismatched_action["actions"][0]["owner"] = "claude"
    with pytest.raises(ValueError):
        validate_update_readiness(mismatched_action)

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
    _assert_valid_readiness(payload)


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
    _assert_valid_readiness(valid)

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


def test_skill_keeps_readiness_pause_and_update_lifecycles_separate():
    classify = _markdown_section("### 1. Preflight and classify").lower()
    operate = _markdown_section("### 3. Follow the requested operation").lower()
    contract = _markdown_section("## Quick Reference").lower()

    assert "readiness-only" in classify
    assert re.search(r"(?:do not|never)\s+drain", classify)
    assert "temporary pause" in operate
    assert "hermes update --drain --yes --json" in operate
    assert "explicit update" in operate
    assert "hermes update --yes" in operate
    assert re.search(r"do not[^.]*drain[^.]*first", operate)
    assert "no update" in operate and "later update" in operate
    assert all(token in contract for token in ("clear-scan(1)", "clear-scan(2)", "final two"))
    assert all(
        failure in contract
        for failure in ("missing", "duplicate", "out-of-order", "trailing-action")
    )


def test_atomic_update_requires_prospective_interruption_consent():
    prerequisites = _markdown_section("## Prerequisites").lower()
    consent = _markdown_section("### 2. Obtain interruption consent").lower()

    assert all(
        token in prerequisites
        for token in ("trust boundary", "--yes", "prospective consent", "preflight")
    )
    assert all(
        token in consent
        for token in (
            "codex",
            "claude",
            "tool calls",
            "launched after preflight",
            "yes/no",
            "initially clear",
            "stop without mutation",
        )
    )


def test_destructive_shortcuts_are_explicitly_forbidden():
    pitfalls = _markdown_section("## Pitfalls")
    prohibitions = [
        line.removeprefix("- ").lower()
        for line in pitfalls.splitlines()
        if line.startswith("- ")
    ]
    assert len(prohibitions) >= 8
    assert all(item.startswith("never ") for item in prohibitions)
    for required_terms in (
        ("process", "direct"),
        ("taskkill", "stop-process"),
        ("marker", "lease"),
        ("--force-venv",),
        ("rebase", "remote"),
        ("mcp_server", "desktop", "substring"),
    ):
        assert any(
            all(term in prohibition for term in required_terms)
            for prohibition in prohibitions
        )


def test_post_check_requires_current_receipt_health_and_relaunch_proof():
    post_check = _markdown_section("### 4. Post-check an update").lower()
    for evidence in (
        "invocation_id",
        "lease_id",
        "timestamp",
        "gateway_resume_deferred",
        "health checks",
        "git",
        "intended target",
        "desktop-driven",
        "build/relaunch",
        "lease=null",
    ):
        assert evidence in post_check
    assert all(
        boundary in post_check
        for boundary in ("readiness", "completion", "cannot claim", "schema v1")
    )


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
