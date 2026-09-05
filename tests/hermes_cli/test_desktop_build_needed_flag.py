"""``hermes desktop --build-needed`` answers the stale-bundle question for Desktop.

After a same-branch reset the packaged bundle's stamp commit can be unrelated
to HEAD, so git-based skew detection in the Desktop reports "in sync" even
when the bundle was never rebuilt (2026-09-05). The flag exposes the
content-hash test ``hermes update`` itself uses, as one JSON line.
"""

from __future__ import annotations

import argparse
import json

import hermes_cli.main as hm


def _args(**overrides):
    return argparse.Namespace(build_needed=True, source=False, **overrides)


def test_reports_the_content_hash_verdict_as_json(monkeypatch, capsys, tmp_path):
    desktop = tmp_path / "apps" / "desktop"
    desktop.mkdir(parents=True)
    (desktop / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)
    seen = {}

    def fake_needed(desktop_dir, project_root, *, source_mode):
        seen["args"] = (desktop_dir, project_root, source_mode)
        return True

    monkeypatch.setattr(hm, "_desktop_build_needed", fake_needed)

    hm.cmd_gui(_args())

    out = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(out) == {"build_needed": True, "source_mode": False}
    assert seen["args"] == (desktop, tmp_path, False)


def test_a_probe_failure_answers_null_instead_of_crashing(monkeypatch, capsys, tmp_path):
    desktop = tmp_path / "apps" / "desktop"
    desktop.mkdir(parents=True)
    (desktop / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)

    def broken(*_args, **_kwargs):
        raise RuntimeError("stamp unreadable")

    monkeypatch.setattr(hm, "_desktop_build_needed", broken)

    hm.cmd_gui(_args())

    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["build_needed"] is None
    assert "stamp unreadable" in out["error"]
