"""安裝設定失敗時，桌面捷徑與開機啟動值必須回到原狀。"""
import base64
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import windows_install as wi


def test_desktop_location_preserves_unicode_path():
    expected = Path(r"C:\Users\使用者\桌面")
    encoded = base64.b64encode(str(expected).encode("utf-16-le")) + b"\r\n"
    assert wi._desktop_path(lambda script: SimpleNamespace(stdout=encoded)) == expected


def test_shortcut_and_run_values_return_to_previous_state(tmp_path, monkeypatch):
    shortcut = tmp_path / "QuotaDock.lnk"
    shortcut.write_bytes(b"old-shortcut")
    old_run = {"QuotaDock": (r"C:\old.exe", 2), "CodexUsageWidget": (r"C:\legacy.exe", 1)}
    restored = []
    monkeypatch.setattr(wi, "_desktop_path", lambda powershell: tmp_path)
    monkeypatch.setattr(wi, "_snapshot_run_values", lambda: old_run)
    monkeypatch.setattr(wi, "_restore_run_values", restored.append)
    monkeypatch.setattr(wi, "set_frozen_autostart", lambda target, enabled: None)
    settings = wi.InstallSettings(tmp_path / "QuotaDock.exe", True,
                                  lambda script: shortcut.write_bytes(b"new-shortcut"))
    settings.apply()
    assert shortcut.read_bytes() == b"new-shortcut"
    settings.rollback()
    assert shortcut.read_bytes() == b"old-shortcut"
    assert restored == [old_run]
    assert not list(tmp_path.glob(".quota-shortcut-*"))


def test_first_install_partial_settings_failure_removes_new_shortcut(tmp_path, monkeypatch):
    shortcut = tmp_path / "QuotaDock.lnk"
    restored = []
    monkeypatch.setattr(wi, "_desktop_path", lambda powershell: tmp_path)
    monkeypatch.setattr(wi, "_snapshot_run_values",
                        lambda: {"QuotaDock": None, "CodexUsageWidget": None})
    monkeypatch.setattr(wi, "_restore_run_values", restored.append)

    def failed_autostart(target, enabled):
        raise OSError("模擬登錄設定失敗")

    monkeypatch.setattr(wi, "set_frozen_autostart", failed_autostart)
    settings = wi.InstallSettings(tmp_path / "QuotaDock.exe", True,
                                  lambda script: shortcut.write_bytes(b"new-shortcut"))
    with pytest.raises(OSError):
        settings.apply()
    assert shortcut.exists()
    settings.rollback()
    assert not shortcut.exists()
    assert restored == [{"QuotaDock": None, "CodexUsageWidget": None}]


def test_registry_snapshot_and_restore_preserve_value_types(monkeypatch):
    current = {"QuotaDock": (r"C:\old.exe", 2), "CodexUsageWidget": (r"C:\legacy.exe", 1)}

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def query(key, name):
        if name not in current:
            raise FileNotFoundError(name)
        return current[name]

    def delete(key, name):
        if name not in current:
            raise FileNotFoundError(name)
        del current[name]

    fake_winreg = SimpleNamespace(
        HKEY_CURRENT_USER=1, KEY_QUERY_VALUE=2, KEY_SET_VALUE=4,
        OpenKey=lambda *args: Key(), CreateKeyEx=lambda *args: Key(),
        QueryValueEx=query, DeleteValue=delete,
        SetValueEx=lambda key, name, reserved, kind, value: current.__setitem__(name, (value, kind)),
    )
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    original = wi._snapshot_run_values()
    current.clear()
    current["QuotaDock"] = (r"C:\new.exe", 1)
    wi._restore_run_values(original)
    assert current == original


def test_autostart_creates_missing_run_key_only_when_enabled(tmp_path, monkeypatch):
    values = {}
    created = []

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def missing_key(*args):
        raise FileNotFoundError("Run")

    def create_key(*args):
        created.append(True)
        return Key()

    def delete_value(key, name):
        if name not in values:
            raise FileNotFoundError(name)
        del values[name]

    fake_winreg = SimpleNamespace(
        HKEY_CURRENT_USER=1, KEY_SET_VALUE=2, REG_SZ=1,
        OpenKey=missing_key, CreateKeyEx=create_key, DeleteValue=delete_value,
        SetValueEx=lambda key, name, reserved, kind, value: values.__setitem__(name, (value, kind)),
    )
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    target = tmp_path / "QuotaDock.exe"
    wi.set_frozen_autostart(target, False)
    assert not created
    wi.set_frozen_autostart(target, True)
    assert created == [True]
    assert values["QuotaDock"] == (f'"{target.resolve()}"', 1)


def test_handoff_recovery_uses_original_exe_without_stale_environment(tmp_path, monkeypatch):
    source = tmp_path / "new.exe"
    target = tmp_path / "installed" / "QuotaDock.exe"
    original = tmp_path / "portable.exe"
    original.write_bytes(b"old")
    events = []

    class Origin:
        executable = original
        handed_off = False

        def stop(self):
            self.handed_off = True
            events.append("handoff")

    origin = Origin()
    monkeypatch.setattr(wi.UpdateOrigin, "from_environment", lambda: origin)
    monkeypatch.setattr(wi, "_desktop_path", lambda powershell: tmp_path)
    monkeypatch.setattr(wi, "_snapshot_run_values", lambda: {})
    for name in (wi.HANDOFF_PATH, wi.HANDOFF_TOKEN, wi.ORIGIN_EXE, wi.ORIGIN_PID):
        monkeypatch.setenv(name, "舊資訊")
    monkeypatch.setattr(wi.subprocess, "run",
                        lambda *args, **kwargs: events.append("stop-target") or SimpleNamespace(stdout=b""))

    def fake_popen(args, **kwargs):
        events.append((args, kwargs["env"]))
        return SimpleNamespace()

    monkeypatch.setattr(wi.subprocess, "Popen", fake_popen)

    def fake_install(source, target, stop_running, finish_install, launch, **callbacks):
        assert callbacks["rollback_settings"] is not None
        stop_running()
        callbacks["recover"]()

    monkeypatch.setattr(wi, "install_release", fake_install)
    wi.install_windows_release(source, target, False)
    assert events[:2] == ["handoff", "stop-target"]
    assert events[2][0] == [str(original)]
    assert all(name not in events[2][1] for name in
               (wi.HANDOFF_PATH, wi.HANDOFF_TOKEN, wi.ORIGIN_EXE, wi.ORIGIN_PID))


def test_failed_handoff_does_not_launch_second_old_app(tmp_path, monkeypatch):
    source = tmp_path / "download.exe"
    source.write_bytes(b"new-version")
    target = tmp_path / "installed" / "QuotaDock.exe"
    target.parent.mkdir()
    target.write_bytes(b"old-version")
    launched = []

    class Origin:
        executable = target
        handed_off = False

        def stop(self):
            raise TimeoutError("原視窗沒有結束")

    monkeypatch.setattr(wi.UpdateOrigin, "from_environment", lambda: Origin())
    monkeypatch.setattr(wi, "_desktop_path", lambda powershell: tmp_path)
    monkeypatch.setattr(wi, "_snapshot_run_values", lambda: {})
    monkeypatch.setattr(wi.subprocess, "Popen", lambda *args, **kwargs: launched.append(args))
    with pytest.raises(RuntimeError, match="保留原版本"):
        wi.install_windows_release(source, target, False)
    assert target.read_bytes() == b"old-version"
    assert launched == []


@pytest.mark.parametrize("existing", [False, True], ids=["首次安裝", "更新既有版本"])
def test_failed_finish_restores_exe_shortcut_and_run_values(tmp_path, monkeypatch, existing):
    source = tmp_path / "download.exe"
    source.write_bytes(b"new-version")
    target = tmp_path / "installed" / "QuotaDock.exe"
    target.parent.mkdir()
    if existing:
        target.write_bytes(b"old-version")
    shortcut = tmp_path / "QuotaDock.lnk"
    if existing:
        shortcut.write_bytes(b"old-shortcut")
    old_run = {"QuotaDock": (r"C:\old.exe", 2) if existing else None,
               "CodexUsageWidget": None}
    current_run = old_run.copy()
    restored = []
    launched = []
    monkeypatch.setattr(wi.UpdateOrigin, "from_environment", lambda: None)
    monkeypatch.setattr(wi, "_desktop_path", lambda powershell: tmp_path)
    monkeypatch.setattr(wi, "_snapshot_run_values", lambda: current_run.copy())

    def restore_run(values):
        restored.append(values)
        current_run.clear()
        current_run.update(values)

    monkeypatch.setattr(wi, "_restore_run_values", restore_run)
    monkeypatch.setattr(wi, "wait_for_startup", lambda *args: None)

    def fake_run(args, **kwargs):
        if "CreateShortcut" in args[-1]:
            shortcut.write_bytes(b"new-shortcut")
        return SimpleNamespace(stdout=b"")

    def fail_autostart(target, enabled):
        raise OSError("模擬寫入登錄值失敗")

    def fake_popen(args, **kwargs):
        version = Path(args[0]).read_bytes()
        launched.append(version)
        if version == b"new-version":
            # 新 GUI 在 READY 前就修改 Run，安裝快照必須早於此時。
            current_run["QuotaDock"] = (r"C:\new.exe", 1)
        return SimpleNamespace()

    monkeypatch.setattr(wi.subprocess, "run", fake_run)
    monkeypatch.setattr(wi.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(wi, "set_frozen_autostart", fail_autostart)
    with pytest.raises(RuntimeError, match="安裝失敗|安裝未完成"):
        wi.install_windows_release(source, target, True)

    if existing:
        assert target.read_bytes() == b"old-version"
        assert shortcut.read_bytes() == b"old-shortcut"
    else:
        assert not target.exists()
        assert not shortcut.exists()
    assert restored == [old_run]
    assert current_run == old_run
    expected_launches = [b"new-version", b"old-version"] if existing else [b"new-version"]
    assert launched == expected_launches
