from types import SimpleNamespace

import pytest

from hermes_cli import update_cmd


def test_self_lock_deferral_never_bypasses_outer_recovery_owner(monkeypatch):
    resumed = []
    deferred = []
    fake_main = SimpleNamespace(
        _detect_self_loaded_native_modules=lambda: ["locked-extension.pyd"],
        _defer_update_for_self_lock=lambda modules: deferred.append(modules),
        _resume_windows_gateways_after_update=lambda token: resumed.append(token),
    )
    monkeypatch.setattr(update_cmd, "_m", lambda: fake_main)

    with pytest.raises(SystemExit) as exc:
        update_cmd._abort_dependency_sync_if_self_locked()

    assert exc.value.code == 2
    assert deferred == [["locked-extension.pyd"]]
    assert resumed == []
