#!/usr/bin/env python3
"""Classify a PR's changed files into CI work lanes.

Reads newline-separated changed paths on stdin and writes ``key=value``
booleans (one per lane) to ``$GITHUB_OUTPUT`` and stdout. The
``detect-changes`` composite action consumes them so steps gate on
``if: steps.changes.outputs.<lane> == 'true'``.

Lanes:

* ``python``      — pytest / ruff / ty / footguns.
* ``python_prod`` — Python changes OUTSIDE tests/ — gates jobs that ship or
  run the product (Desktop E2E backend, Docker image) but never import the
  test suite. A tests-only PR keeps ``python`` (pytest must run) while
  skipping those product jobs.
* ``docker_meta`` — Dockerfiles etc.
* ``frontend``    — TS typecheck matrix + desktop build.
* ``site``        — Docusaurus + generated skill docs.
* ``scan``        — supply-chain scan (Python files, .pth, setup hooks).
* ``deps``        — pyproject.toml dependency bounds check.
* ``npm_lock``    — semantic package-lock.json diff PR comment.
* ``installer``   — PowerShell installer tests (Windows runner).
* ``windows_update`` — Native Windows updater lifecycle contracts.
* ``mcp_catalog`` — bundled MCP catalog / installer review.

Docker is not a lane — it builds on push-to-main and release only,
never per-PR.

Contract — *fail open, never closed*. We may run a lane we didn't need, but
must never skip one a change could break:

* An empty diff, any ``.github/`` change, or a change to this classifier runs
  everything.
* ``python`` is a denylist: skipped only when *every* file is provably prose
  or a frontend-only package; an unrecognized path keeps it on.
* ``skills/`` (incl. ``SKILL.md``) is python-relevant — the skill-doc tests
  read that tree, so a doc-looking edit can still break Python.
"""

from __future__ import annotations

import json
import os
import sys

_FRONTEND = ("ui-tui/", "web/", "apps/")  # TS typecheck-matrix packages
_ROOT_NPM = {"package.json", "package-lock.json"}  # shifts every package's tree
_DOCKER_META = ("docker/", ".hadolint.yml", "Dockerfile") # docker setup
_SITE = ("website/", "skills/", "optional-skills/")  # docs site + skill pages
# Prose/frontend trees that can't touch Python. skills/ is excluded on purpose.
_PY_SKIP = ("docs/", "website/") + _FRONTEND

# CI-sensitive files: eslint config, workflow files, composite actions.
# Changes here can influence what code the autofix job executes and pushes to
# main, so they require explicit maintainer review (ci-reviewed label).
#
# package.json is deliberately NOT listed here: npm scripts only execute on the
# unprivileged generate-patch runner (contents: read), never on the privileged
# apply-patch job. The two-job split means a malicious package.json script
# can't get push access — it runs on an ephemeral runner with zero write perms.
_CLASSIFIER_FILE = "scripts/ci/classify_changes.py"
_CI_REVIEW_FILES = {
    ".prettierrc",
    _CLASSIFIER_FILE,
}
_CI_REVIEW_PATHS = (".github/workflows/", ".github/actions/")

# Supply-chain scan: files that can execute code at install/import time.
_SCAN_EXTS = (".py", ".pth")
_SCAN_FILES = {"setup.cfg", "pyproject.toml"}

# MCP catalog files that require explicit security review.
_MCP_CATALOG_PATHS = ("optional-mcps/",)
_MCP_CATALOG_FILES = {"hermes_cli/mcp_catalog.py"}

# Windows installer + its PowerShell tests. These only run on a Windows runner,
# so they get their own lane rather than riding along with ``python``.
_INSTALLER_PATHS = ("scripts/tests/",)
_INSTALLER_FILES = {"scripts/install.ps1", "scripts/install.cmd"}

# Native Windows update lifecycle. Keep this deliberately narrower than the
# whole Desktop/bootstrap trees: this lane installs both npm and Rust
# dependencies, so it should run only when the process/lease/handoff contract
# can change. The workflow itself is covered by the .github fail-open rule.
_WINDOWS_UPDATE_FILES = {
    "scripts/desktop-update.ps1",
    "scripts/tests/fixtures/desktop-update-bridge-lease.json",
    "apps/bootstrap-installer/src-tauri/Cargo.toml",
    "apps/bootstrap-installer/src-tauri/Cargo.lock",
    "apps/bootstrap-installer/src-tauri/src/lib.rs",
    "apps/bootstrap-installer/src-tauri/src/update.rs",
    "agent/__init__.py",
    "hermes_constants.py",
    "hermes_cli/main.py",
    "hermes_cli/subcommands/update.py",
    "hermes_cli/update_cmd.py",
    "hermes_cli/update_transaction.py",
    "hermes_cli/update_lock.py",
    "hermes_cli/update_readiness.py",
    "hermes_cli/update_readiness.schema.v1.json",
    "hermes_cli/update_quiesce.py",
    "hermes_cli/update_deferred_gateway.py",
    "hermes_cli/update_receipt.py",
    "hermes_cli/gateway.py",
    "hermes_cli/gateway_windows.py",
    "hermes_cli/_scan_venv_blockers.py",
    "hermes_mcp_update_gate.py",
    "agent/transports/hermes_tools_mcp_server.py",
    "tests/test_hermes_mcp_update_gate.py",
    "tests/agent/transports/test_hermes_tools_mcp_server.py",
    "tests/hermes_cli/test_scan_venv_blockers.py",
    "tests/hermes_cli/test_update_readiness.py",
    "tests/cli/test_update_command.py",
}
_WINDOWS_UPDATE_SCRIPT_TEST_PREFIX = "scripts/tests/test-desktop-update-"
_WINDOWS_UPDATE_PYTHON_TEST_PREFIX = "tests/hermes_cli/test_update"
_WINDOWS_UPDATE_BOOTSTRAP_SOURCE_PREFIX = "apps/bootstrap-installer/src-tauri/src/"
_WINDOWS_UPDATE_DESKTOP_FILES = {"main.ts"}
_WINDOWS_UPDATE_DESKTOP_PREFIXES = (
    "backend-child.",
    "handoff-",
    "mcp-bridge-quiesce.",
    "pool-backend-lifecycle.",
    "primary-backend-startup.",
    "update-",
    "updater-process.",
    "venv-blocker-scan.",
    "windows-process-identity.",
)

def _is_docs(p: str) -> bool:
    if p.startswith(("skills/", "optional-skills/")):
        return False
    return p.endswith((".md", ".mdx")) or p.startswith("docs/") or p.startswith("LICENSE")


def _py_irrelevant(p: str) -> bool:
    return _is_docs(p) or p in _ROOT_NPM or p.startswith(_PY_SKIP) or p.startswith(_DOCKER_META)


def _py_test_only(p: str) -> bool:
    """Is ``p`` inside the test suite (never shipped / imported by the product)?

    Product jobs (Desktop E2E's ``hermes serve`` backend, the Docker image)
    run installed code — nothing under ``tests/`` is packaged or importable
    there. scripts/run_tests.sh and run_tests_parallel.py are deliberately
    NOT test-only: they are runner infrastructure, and a bad edit there can
    mask real failures, so they stay conservative (python_prod=true).
    """
    return p.startswith("tests/")


def _is_scan(p: str) -> bool:
    return p.endswith(_SCAN_EXTS) or p in _SCAN_FILES


def _is_mcp_catalog(p: str) -> bool:
    return p.startswith(_MCP_CATALOG_PATHS) or p in _MCP_CATALOG_FILES


def _is_installer(p: str) -> bool:
    return p.startswith(_INSTALLER_PATHS) or p in _INSTALLER_FILES


def _is_windows_update(p: str) -> bool:
    if (
        p in _WINDOWS_UPDATE_FILES
        or p.startswith(_WINDOWS_UPDATE_SCRIPT_TEST_PREFIX)
        or p.startswith(_WINDOWS_UPDATE_PYTHON_TEST_PREFIX)
        or p.startswith(_WINDOWS_UPDATE_BOOTSTRAP_SOURCE_PREFIX)
    ):
        return True
    desktop_prefix = "apps/desktop/electron/"
    if not p.startswith(desktop_prefix):
        return False
    name = p[len(desktop_prefix):]
    return name in _WINDOWS_UPDATE_DESKTOP_FILES or any(
        name.startswith(prefix) for prefix in _WINDOWS_UPDATE_DESKTOP_PREFIXES
    )


def _is_ci_review(p: str) -> bool:
    if p in _CI_REVIEW_FILES or p.startswith(_CI_REVIEW_PATHS):
        return True
    # Any eslint config file at any path — eslint configs can define custom
    # fix functions that execute arbitrary code, so they all require review.
    return os.path.basename(p).startswith("eslint.config.")


def ci_review_files(files: list[str]) -> list[str]:
    """Return the CI-sensitive paths that need maintainer review."""
    return sorted({f.strip() for f in files if f.strip() and _is_ci_review(f.strip())})


def classify(files: list[str]) -> dict[str, bool]:
    """Map changed paths to ``{lane: should_run}``."""
    files = [f.strip() for f in files if f.strip()]
    ret = {
        "python": any(not _py_irrelevant(f) for f in files),
        "python_prod": any(not _py_irrelevant(f) and not _py_test_only(f) for f in files),
        "docker_meta":  any(f.startswith(_DOCKER_META) for f in files),
        "frontend": any(f.startswith(_FRONTEND) or f in _ROOT_NPM for f in files),
        "site": any(f.startswith(_SITE) for f in files),
        "scan": any(_is_scan(f) for f in files),
        "deps": any(f == "pyproject.toml" for f in files),
        "npm_lock": any(f.split("/")[-1] == "package-lock.json" for f in files),
        "installer": any(_is_installer(f) for f in files),
        "windows_update": any(_is_windows_update(f) for f in files),
        "mcp_catalog": any(_is_mcp_catalog(f) for f in files),
        "ci_review": any(_is_ci_review(f) for f in files),
    }
    if not files or any(
        f.startswith(".github/") or f == _CLASSIFIER_FILE for f in files
    ):
        for lane in ret:
            # MCP catalog review is only needed when its own files change.
            if lane != "mcp_catalog":
                ret[lane] = True
    return ret



def main() -> int:
    files = sys.stdin.read().splitlines()
    lanes = classify(files)
    out = "\n".join([
        *(f"{key}={str(value).lower()}" for key, value in lanes.items()),
        f"ci_review_files={json.dumps(ci_review_files(files))}",
    ])
    if dest := os.environ.get("GITHUB_OUTPUT"):
        with open(dest, "a", encoding="utf-8") as fh:
            fh.write(out + "\n")
    print(out)  # echo for local runs + CI step logs
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
