"""Tests for scripts/ci/classify_changes.py.

Check some common patterns of file modifications and the CI lanes they should run.
We should always fail open. We may run a lane we didn't need, never skip one a
change could have broken.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "classify_changes.py"
_spec = importlib.util.spec_from_file_location("classify_changes", _PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("Failed to load classify_changes.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
classify = _mod.classify
ci_review_files = _mod.ci_review_files

DEFAULT = {
    "python": True,
    "python_prod": True,
    "frontend": True,
    "docker_meta": True,
    "site": True,
    "scan": True,
    "deps": True,
    "npm_lock": True,
    "installer": True,
    "windows_update": True,
    "mcp_catalog": False,
    "ci_review": True,
}


def _lanes(
    python=False,
    frontend=False,
    site=False,
    scan=False,
    deps=False,
    npm_lock=False,
    installer=False,
    windows_update=False,
    mcp_catalog=False,
    docker_meta=False,
    ci_review=False,
    python_prod=None,
) -> dict[str, bool]:
    # python_prod tracks python except for tests-only diffs; default it to
    # python so the majority of cases don't need to spell it out.
    return {
        "python": python,
        "python_prod": python if python_prod is None else python_prod,
        "frontend": frontend,
        "docker_meta": docker_meta,
        "site": site,
        "scan": scan,
        "deps": deps,
        "npm_lock": npm_lock,
        "installer": installer,
        "windows_update": windows_update,
        "mcp_catalog": mcp_catalog,
        "ci_review": ci_review,
    }


CASES = {
    "docs-only → nothing heavy": (["README.md", "docs/guide.md"], _lanes()),
    "python source → python": (["run_agent.py"], _lanes(python=True, scan=True)),
    "dep manifest → python": (["pyproject.toml"], _lanes(python=True, scan=True, deps=True)),
    "uv.lock → python": (["uv.lock"], _lanes(python=True)),
    "ts package → frontend": (["apps/desktop/src/app.tsx"], _lanes(frontend=True)),
    "ui-tui → frontend": (["ui-tui/src/entry.ts"], _lanes(frontend=True)),
    # Lockfile bump shifts every TS package's tree, but not the Python suite.
    "root lockfile → frontend, not python": (["package-lock.json"], _lanes(frontend=True, npm_lock=True)),
    "nested lockfile → npm_lock": (["website/package-lock.json"], _lanes(site=True, npm_lock=True)),
    "website → site": (["website/docs/intro.md"], _lanes(site=True)),
    # SKILL.md reads like docs, but the skill-doc tests read skills/, so a
    # skill edit must still run Python.
    "skill md → python + site": (["skills/github/SKILL.md"], _lanes(python=True, site=True)),
    "dockerfile → docker meta": (["Dockerfile"], _lanes(docker_meta=True)),
    # install.ps1 is a shell script Python never imports, but it's also not
    # provably prose, so python stays on (fail-open) alongside the Windows lane.
    "install.ps1 → installer": (["scripts/install.ps1"], _lanes(python=True, installer=True)),
    "installer test → installer": (
        ["scripts/tests/test-install-ps1-longpath.ps1"],
        _lanes(python=True, installer=True),
    ),
    "desktop update handoff → native Windows updater": (
        ["scripts/desktop-update.ps1"],
        _lanes(python=True, windows_update=True),
    ),
    "desktop update harness → installer + native Windows updater": (
        ["scripts/tests/test-desktop-update-handoff.ps1"],
        _lanes(python=True, installer=True, windows_update=True),
    ),
    "desktop update lease fixture → installer + native Windows updater": (
        ["scripts/tests/fixtures/desktop-update-bridge-lease.json"],
        _lanes(python=True, installer=True, windows_update=True),
    ),
    "bootstrap update implementation → frontend + native Windows updater": (
        ["apps/bootstrap-installer/src-tauri/src/update.rs"],
        _lanes(frontend=True, windows_update=True),
    ),
    "bootstrap updater manifest → frontend + native Windows updater": (
        ["apps/bootstrap-installer/src-tauri/Cargo.toml"],
        _lanes(frontend=True, windows_update=True),
    ),
    "bootstrap updater dependency → frontend + native Windows updater": (
        ["apps/bootstrap-installer/src-tauri/src/powershell.rs"],
        _lanes(frontend=True, windows_update=True),
    ),
    "Electron update transaction → frontend + native Windows updater": (
        ["apps/desktop/electron/update-preflight.ts"],
        _lanes(frontend=True, windows_update=True),
    ),
    "Electron backend child lifecycle → frontend + native Windows updater": (
        ["apps/desktop/electron/backend-child.ts"],
        _lanes(frontend=True, windows_update=True),
    ),
    "Electron lifecycle owner → frontend + native Windows updater": (
        ["apps/desktop/electron/main.ts"],
        _lanes(frontend=True, windows_update=True),
    ),
    "Electron backend startup gate → frontend + native Windows updater": (
        ["apps/desktop/electron/primary-backend-startup.test.ts"],
        _lanes(frontend=True, windows_update=True),
    ),
    "Electron process identity → frontend + native Windows updater": (
        ["apps/desktop/electron/windows-process-identity.test.ts"],
        _lanes(frontend=True, windows_update=True),
    ),
    "Electron lease bridge → frontend + native Windows updater": (
        ["apps/desktop/electron/mcp-bridge-quiesce.ts"],
        _lanes(frontend=True, windows_update=True),
    ),
    "Electron handoff result → frontend + native Windows updater": (
        ["apps/desktop/electron/handoff-result-orchestration.test.ts"],
        _lanes(frontend=True, windows_update=True),
    ),
    "Python updater lease lifecycle → Python + native Windows updater": (
        ["hermes_cli/update_quiesce.py"],
        _lanes(python=True, scan=True, windows_update=True),
    ),
    "Python updater dispatch → Python + native Windows updater": (
        ["hermes_cli/main.py"],
        _lanes(python=True, scan=True, windows_update=True),
    ),
    "Python updater transaction → Python + native Windows updater": (
        ["hermes_cli/update_transaction.py"],
        _lanes(python=True, scan=True, windows_update=True),
    ),
    "Python updater option parser → Python + native Windows updater": (
        ["hermes_cli/subcommands/update.py"],
        _lanes(python=True, scan=True, windows_update=True),
    ),
    "Python updater readiness schema → Python + native Windows updater": (
        ["hermes_cli/update_readiness.schema.v1.json"],
        _lanes(python=True, windows_update=True),
    ),
    "profile root helper → Python + native Windows updater": (
        ["hermes_constants.py"],
        _lanes(python=True, scan=True, windows_update=True),
    ),
    "deferred gateway runtime → Python + native Windows updater": (
        ["hermes_cli/gateway_windows.py"],
        _lanes(python=True, scan=True, windows_update=True),
    ),
    "MCP update gate → Python + native Windows updater": (
        ["hermes_mcp_update_gate.py"],
        _lanes(python=True, scan=True, windows_update=True),
    ),
    "MCP transport → Python + native Windows updater": (
        ["agent/transports/hermes_tools_mcp_server.py"],
        _lanes(python=True, scan=True, windows_update=True),
    ),
    "native updater test → tests-only Python + native Windows updater": (
        ["tests/hermes_cli/test_update_readiness.py"],
        _lanes(
            python=True,
            python_prod=False,
            scan=True,
            windows_update=True,
        ),
    ),
    "updater family test → tests-only Python + native Windows updater": (
        ["tests/hermes_cli/test_update_lock.py"],
        _lanes(
            python=True,
            python_prod=False,
            scan=True,
            windows_update=True,
        ),
    ),
    "Electron pool lifecycle → frontend + native Windows updater": (
        ["apps/desktop/electron/pool-backend-lifecycle.test.ts"],
        _lanes(frontend=True, windows_update=True),
    ),
    "unrelated Electron source → no native Windows updater": (
        ["apps/desktop/electron/theme.ts"],
        _lanes(frontend=True),
    ),
    "python source alone → no installer lane": (["run_agent.py"], _lanes(python=True, scan=True)),
    # Unknown top-level file keeps Python on rather than risk a silent skip.
    "unknown toplevel → python": (["Makefile"], _lanes(python=True)),
    "mixed docs+python → python": (["README.md", "agent/x.py"], _lanes(python=True, scan=True)),
    "mixed docs+frontend → frontend": (["README.md", "apps/x.tsx"], _lanes(frontend=True)),
    # tests-only diffs: pytest lanes stay ON, product jobs (Desktop E2E,
    # Docker) gate on python_prod and skip.
    "tests-only → python without python_prod": (
        ["tests/agent/test_foo.py", "tests/conftest.py"],
        _lanes(python=True, python_prod=False, scan=True),
    ),
    "tests + prod source → both lanes": (
        ["tests/agent/test_foo.py", "agent/x.py"],
        _lanes(python=True, scan=True),
    ),
    # Runner infrastructure is NOT tests-only — a bad runner edit can mask
    # real failures, so it keeps the conservative full lane set.
    "test runner script → python_prod stays on": (
        ["scripts/run_tests_parallel.py"],
        _lanes(python=True, scan=True),
    ),
    # Supply-chain lanes
    ".pth file → scan": (["evil.pth"], _lanes(python=True, scan=True)),
    "setup.py → scan": (["setup.py"], _lanes(python=True, scan=True)),
    "mcp catalog manifest → mcp_catalog": (
        ["optional-mcps/foo/manifest.yaml"],
        _lanes(python=True, mcp_catalog=True),
    ),
    "mcp_catalog.py → mcp_catalog": (
        ["hermes_cli/mcp_catalog.py"],
        _lanes(python=True, scan=True, mcp_catalog=True),
    ),
    # CI-sensitive files require explicit review label.
    "eslint config → ci_review": (
        ["apps/desktop/eslint.config.mjs"],
        _lanes(frontend=True, ci_review=True),
    ),
    "shared eslint config → ci_review": (
        ["eslint.config.shared.mjs"],
        _lanes(python=True, ci_review=True),
    ),
    "ui-tui eslint config → ci_review": (
        ["ui-tui/eslint.config.mjs"],
        _lanes(frontend=True, ci_review=True),
    ),
    "web eslint config → ci_review": (
        ["web/eslint.config.js"],
        _lanes(frontend=True, ci_review=True),
    ),
    "shared package eslint config → ci_review": (
        ["apps/shared/eslint.config.mjs"],
        _lanes(frontend=True, ci_review=True),
    ),
    "bootstrap-installer eslint config → ci_review": (
        ["apps/bootstrap-installer/eslint.config.mjs"],
        _lanes(frontend=True, ci_review=True),
    ),
    "prettier config → ci_review": (
        [".prettierrc"],
        _lanes(python=True, ci_review=True),
    ),
    "workflow yml → ci_review (also fail-open all)": (
        [".github/workflows/typecheck.yml"],
        DEFAULT,
    ),
    "composite action → ci_review (also fail-open all)": (
        [".github/actions/retry/action.yml"],
        DEFAULT,
    ),
    "classifier implementation → ci_review (also fail-open all)": (
        ["scripts/ci/classify_changes.py"],
        DEFAULT,
    ),
    # Normal desktop source doesn't trigger ci_review.
    "desktop src → no ci_review": (
        ["apps/desktop/src/app.tsx"],
        _lanes(frontend=True),
    ),
    # Fail open: CI-config / empty / blank diffs run everything.
    ".github change → all": ([".github/workflows/tests.yml"], DEFAULT),
    "action change → all": ([".github/actions/detect-changes/action.yml"], DEFAULT),
    "empty diff → all": ([], DEFAULT),
    "blank lines → all": (["", "  "], DEFAULT),
}


@pytest.mark.parametrize("files,expected", CASES.values(), ids=CASES.keys())
def test_classify(files, expected):
    assert classify(files) == expected


def test_ci_review_files_returns_only_sensitive_paths_sorted_and_unique():
    assert ci_review_files([
        "apps/desktop/src/app.tsx",
        ".github/workflows/ci.yml",
        "apps/desktop/eslint.config.mjs",
        ".github/workflows/ci.yml",
        "scripts/ci/classify_changes.py",
    ]) == [
        ".github/workflows/ci.yml",
        "apps/desktop/eslint.config.mjs",
        "scripts/ci/classify_changes.py",
    ]
