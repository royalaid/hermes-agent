from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install.ps1"
INSTALLER_SH = ROOT / "scripts" / "install.sh"


def test_repository_pin_controls_clone_update_and_zip_fallback_sources():
    script = INSTALLER.read_text(encoding="utf-8-sig")

    assert '[string]$Repository = "NousResearch/hermes-agent"' in script
    assert '$RepoUrlHttps = "https://github.com/$Repository.git"' in script
    assert 'remote set-url origin $RepoUrlHttps' in script

    expected_archives = {
        '"https://github.com/$Repository/archive/$Commit.zip"',
        '"https://github.com/$Repository/archive/refs/tags/$Tag.zip"',
        '"https://github.com/$Repository/archive/refs/heads/$Branch.zip"',
    }
    assert expected_archives <= {line.strip().split(" = ", 1)[-1] for line in script.splitlines()}
    assert 'pinnedRepository = $Repository' in script
    assert "$safeZipLabel = $zipLabel -replace '[^A-Za-z0-9._-]', '_'" in script
    assert '$zipPath = "$env:TEMP\\hermes-agent-$safeZipLabel.zip"' in script
    assert '$env:GITHUB_REPOSITORY = $Repository' in script
    assert "github.com/NousResearch/hermes-agent/archive" not in script

    shell_script = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert '"pinnedRepository": "%s"' in shell_script
    assert '"$REPOSITORY"' in shell_script

    desktop_main = (ROOT / "apps" / "desktop" / "electron" / "main.ts").read_text(encoding="utf-8")
    assert "pinnedRepository: payload.pinnedRepository" in desktop_main

    remote_update = script.index("remote set-url origin $RepoUrlHttps")
    branch_fetch = script.index("fetch origin $Branch", remote_update)
    assert remote_update < branch_fetch


def test_posix_nested_desktop_build_preserves_repository_branch_and_commit():
    script = INSTALLER_SH.read_text(encoding="utf-8")

    pack_start = script.index("_desktop_pack() {")
    pack_end = script.index("\n}", pack_start)
    pack_body = script[pack_start:pack_end]

    assert 'export GITHUB_REPOSITORY="$REPOSITORY"' in pack_body
    assert 'export GITHUB_REF_NAME="$BRANCH"' in pack_body
    assert 'GITHUB_SHA="$(git -C "$INSTALL_DIR" rev-parse HEAD' in pack_body
    assert "export GITHUB_SHA" in pack_body
