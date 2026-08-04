from pathlib import Path
from types import SimpleNamespace

from hermes_cli.update_desktop_runtime import (
    find_desktop_plugin_launchers,
    pause_desktop_plugins_for_update,
    resume_desktop_plugins_after_update,
)


def test_plugin_launcher_tree_is_managed_but_unrelated_venv_holder_is_not(tmp_path):
    plugin_root = tmp_path / "desktop-plugins"
    plugin_file = plugin_root / "tracker" / "service-host.vbs"
    plugin_file.parent.mkdir(parents=True)
    plugin_file.write_text("placeholder")

    records = [
        {"pid": 10, "ppid": 1, "argv": ["wscript.exe", str(plugin_file)], "cwd": str(tmp_path)},
        {"pid": 11, "ppid": 10, "argv": ["python.exe", "-m", "http.server", "8765"], "cwd": str(tmp_path)},
        {"pid": 12, "ppid": 1, "argv": ["python.exe", "-m", "http.server", "8766"], "cwd": str(tmp_path)},
    ]

    assert find_desktop_plugin_launchers(records, plugin_root) == [records[0]]


def test_pause_and_resume_only_replays_recorded_plugin_launcher(tmp_path):
    plugin_file = tmp_path / "desktop-plugins" / "tracker" / "service.py"
    plugin_file.parent.mkdir(parents=True)
    plugin_file.write_text("placeholder")
    records = [
        SimpleNamespace(info={"pid": 20, "ppid": 1, "cmdline": ["python.exe", str(plugin_file)], "cwd": str(tmp_path)}),
        SimpleNamespace(info={"pid": 21, "ppid": 1, "cmdline": ["python.exe", "-m", "http.server", "8765"], "cwd": str(tmp_path)}),
    ]
    terminated = []

    token = pause_desktop_plugins_for_update(
        tmp_path,
        process_iter=lambda: records,
        terminate_tree=terminated.append,
    )

    assert terminated == [20]
    assert token and token["launches"][0]["argv"] == ["python.exe", str(plugin_file)]

    spawned = []
    assert resume_desktop_plugins_after_update(
        token,
        popen=lambda argv, **kwargs: spawned.append((argv, kwargs)),
    ) == 1
    assert spawned[0][0] == ["python.exe", str(plugin_file)]
    assert resume_desktop_plugins_after_update(token, popen=lambda *_a, **_k: None) == 0
