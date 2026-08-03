"""Windows installer must update from the repository selected by -Repository."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")

pytestmark = pytest.mark.skipif(
    POWERSHELL is None or shutil.which("git") is None,
    reason="needs Windows PowerShell and git",
)


def _extract_resolver() -> str:
    text = INSTALL_PS1.read_text(encoding="utf-8")
    match = re.search(
        r"(?ms)^function ConvertTo-RepositorySlug \{.*?^\}\s*^function Resolve-RepositoryRemote \{.*?^\}",
        text,
    )
    assert match is not None, "Resolve-RepositoryRemote function not found"
    return match.group(0)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_existing_checkout_uses_remote_matching_requested_repository(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(checkout, "init", "-q")
    _git(checkout, "remote", "add", "origin", "https://github.com/NousResearch/hermes-agent.git")
    _git(checkout, "remote", "add", "fork", "https://github.com/royalaid/hermes-agent.git")

    checkout_ps = str(checkout).replace("'", "''")
    script = "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            "function Write-Info { param([string]$Message) }",
            _extract_resolver(),
            f"Set-Location -LiteralPath '{checkout_ps}'",
            "$RepoUrlSsh = 'git@github.com:royalaid/hermes-agent.git'",
            "$RepoUrlHttps = 'https://github.com/royalaid/hermes-agent.git'",
            "Resolve-RepositoryRemote",
        ]
    )
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip().splitlines()[-1] == "fork"


def test_custom_repository_adds_managed_remote_without_rewriting_origin(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(checkout, "init", "-q")
    official = "https://github.com/NousResearch/hermes-agent.git"
    custom = "https://github.com/royalaid/hermes-agent.git"
    _git(checkout, "remote", "add", "origin", official)

    checkout_ps = str(checkout).replace("'", "''")
    script = "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            "function Write-Info { param([string]$Message) }",
            _extract_resolver(),
            f"Set-Location -LiteralPath '{checkout_ps}'",
            "$Repository = 'royalaid/hermes-agent'",
            "$RepoUrlSsh = 'git@github.com:royalaid/hermes-agent.git'",
            f"$RepoUrlHttps = '{custom}'",
            "Resolve-RepositoryRemote",
        ]
    )
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip().splitlines()[-1] == "hermes-source"
    origin = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    managed = subprocess.run(
        ["git", "remote", "get-url", "hermes-source"],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert origin == official
    assert managed == custom
