"""Existing taskbar shortcuts must keep opening the active installed version."""
import json
import os
from pathlib import Path
import subprocess
import shutil
import time

import pytest

import windows_install as wi
import launch_entry as le
from test_install_file_access import NoDeleteShare

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows shell shortcuts")


def powershell(script, *, environment=None):
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); " + script],
        check=True, capture_output=True, timeout=25,
        env=environment, creationflags=subprocess.CREATE_NO_WINDOW)


def shortcut(path, target=None, arguments=None):
    env = os.environ | {"QUOTA_TEST_LINK": str(path)}
    script = "$shell=New-Object -ComObject WScript.Shell; $link=$shell.CreateShortcut($env:QUOTA_TEST_LINK); "
    if target is not None:
        env["QUOTA_TEST_EXE"] = str(target)
        script += "$link.TargetPath=$env:QUOTA_TEST_EXE; $link.Save(); "
    if arguments is not None:
        env["QUOTA_TEST_ARGUMENTS"] = arguments
        script += "$link.Arguments=$env:QUOTA_TEST_ARGUMENTS; $link.Save(); "
    script += "Write-Output $link.TargetPath"
    return Path(powershell(script, environment=env).stdout.decode("utf-8").strip())


@pytest.fixture
def shell_case(tmp_path, monkeypatch):
    root = tmp_path / "安裝目錄"
    root.mkdir()
    old = root / "QuotaDock.exe"
    old.write_bytes(b"old version")
    new = root / "versions" / ("1.4.11-" + "a" * 32) / "QuotaDock.exe"
    new.parent.mkdir(parents=True)
    new.write_bytes(b"new version")
    desktop = tmp_path / "桌面"
    desktop.mkdir()
    appdata = tmp_path / "AppData"
    pinned = appdata / "Microsoft/Internet Explorer/Quick Launch/User Pinned/TaskBar"
    pinned.mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(wi, "_desktop_path", lambda run: desktop)
    monkeypatch.setattr(wi, "_snapshot_run_values", lambda: {})
    monkeypatch.setattr(wi, "_restore_run_values", lambda values: None)
    monkeypatch.setattr(wi, "set_frozen_autostart", lambda *args: None)
    env = os.environ | {"QUOTADOCK_INSTALL_TARGET": str(new)}
    run = lambda script: powershell(script, environment=env)
    return root, old, new, desktop, pinned, run


def test_update_repairs_original_taskbar_shortcut_in_place(shell_case):
    root, old, new, desktop, pinned, run = shell_case
    link = pinned / "原本釘選的名稱.lnk"
    shortcut(link, old)
    unrelated = pinned / "其他程式.lnk"
    shortcut(unrelated, Path(os.environ["SystemRoot"]) / "System32/notepad.exe")
    untouched = unrelated.read_bytes()
    settings = wi.InstallSettings(new, False, run)
    settings.apply()
    assert shortcut(link) == new, "原本的工作列捷徑仍指向舊版"
    assert unrelated.read_bytes() == untouched


@pytest.fixture(scope="module")
def native_binaries(tmp_path_factory):
    folder = tmp_path_factory.mktemp("native-launcher")
    launcher = folder / le.LAUNCHER_NAME
    tools = Path(__file__).parent / "tools"
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-File",
                    str(tools / "build_launcher.ps1"), "-OutputPath", str(launcher)],
                   check=True, capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    # Exercise a Microsoft-signed Windows program, without generating a test EXE.
    probe = Path(os.environ["SystemRoot"]) / "System32/cmd.exe"
    return launcher, probe


def activate(root, target):
    (root / "current.json").write_text(json.dumps({"executable": str(target.relative_to(root))}), encoding="utf-8")


def wait_for_file(path):
    until = time.monotonic() + 10
    while not path.exists() and time.monotonic() < until:
        time.sleep(.05)
    assert path.exists(), "固定啟動程式未開啟目前版本"
    return path.read_text(encoding="utf-8-sig").splitlines()


def test_same_original_shell_link_opens_next_version_without_rewriting_it(shell_case, native_binaries):
    root, old, new, desktop, pinned, run = shell_case
    launcher, probe = native_binaries
    shutil.copyfile(probe, new)
    assert new.is_file(), "probe copy missing"
    pin = pinned / "既有工作列捷徑.lnk"
    shortcut(pin, old, "/d /c echo opened>opened.txt")
    activate(root, new)
    settings = wi.InstallSettings(new, False, run, launcher)
    settings.apply()
    assert new.is_file(), "installed target missing after shortcut migration"
    entry = root / le.LAUNCHER_NAME
    check = subprocess.run([str(entry), "--check"], capture_output=True, timeout=10,
                           creationflags=subprocess.CREATE_NO_WINDOW)
    assert check.returncode == 0, check.stderr.decode(errors="replace")
    assert shortcut(pin) == shortcut(desktop / "QuotaDock.lnk") == entry
    os.startfile(pin)
    assert wait_for_file(new.parent / "opened.txt") == ["opened"]
    original_link = pin.read_bytes()
    newer = root / "versions" / ("1.4.12-" + "b" * 32) / "QuotaDock.exe"
    newer.parent.mkdir()
    shutil.copyfile(probe, newer)
    activate(root, newer)
    os.startfile(pin)
    assert wait_for_file(newer.parent / "opened.txt") == ["opened"]
    assert pin.read_bytes() == original_link


def test_launcher_preserves_quoted_arguments_and_uses_independent_child(shell_case, native_binaries):
    root, old, new, desktop, pinned, run = shell_case
    launcher, probe = native_binaries
    shutil.copyfile(probe, new)
    install = le.LauncherInstall(root, launcher)
    install.apply()
    activate(root, new)
    args = ["/d", "/c", "(echo words with spaces&echo %PYINSTALLER_RESET_ENVIRONMENT%)>opened.txt"]
    subprocess.run([str(install.target), *args], check=True, timeout=10,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    assert wait_for_file(new.parent / "opened.txt") == ["words with spaces", "1"]


def test_later_updates_keep_a_working_locked_launcher(shell_case, native_binaries):
    root, *_ = shell_case
    launcher, _ = native_binaries
    le.LauncherInstall(root, launcher).apply()
    target = root / le.LAUNCHER_NAME
    previous = target.read_bytes()
    holder = NoDeleteShare(target)
    try:
        next_install = le.LauncherInstall(root, root / "not-needed.exe")
        next_install.apply()
        assert not next_install.changed and target.read_bytes() == previous
    finally:
        holder.close()


def test_failed_settings_restore_original_pin_and_desktop(shell_case, native_binaries, monkeypatch):
    root, old, new, desktop, pinned, run = shell_case
    launcher, _ = native_binaries
    pin = pinned / "舊釘選.lnk"
    desk = desktop / "QuotaDock.lnk"
    shortcut(pin, old)
    shortcut(desk, old)
    previous = {p: p.read_bytes() for p in (pin, desk)}
    def fail(*args):
        raise OSError("autostart failure")
    monkeypatch.setattr(wi, "set_frozen_autostart", fail)
    settings = wi.InstallSettings(new, False, run, launcher)
    with pytest.raises(OSError, match="autostart failure"):
        settings.apply()
    settings.rollback()
    assert all(path.read_bytes() == data for path, data in previous.items())
    assert not (root / le.LAUNCHER_NAME).exists() and not (root / "launcher.json").exists()


@pytest.mark.parametrize("value", ["../outside.exe", "QuotaDock.exe", "/outside.exe", "versions/missing/QuotaDock.exe"])
def test_launcher_rejects_invalid_or_missing_active_targets(shell_case, native_binaries, value):
    root, *_ = shell_case
    launcher, _ = native_binaries
    install = le.LauncherInstall(root, launcher)
    install.apply()
    (root / "current.json").write_text(json.dumps({"executable": value}), encoding="utf-8")
    result = subprocess.run([str(install.target), "--check"], timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 1


def test_portable_update_origin_is_migrated_but_same_filename_elsewhere_is_untouched(shell_case, tmp_path):
    root, old, new, desktop, pinned, run = shell_case
    original = tmp_path / "下載的舊版.exe"
    original.write_bytes(b"old portable")
    other = tmp_path / "別的應用程式" / "QuotaDock.exe"
    other.parent.mkdir()
    other.write_bytes(b"other application")
    selected = pinned / "Portable 舊版.lnk"
    ignored = pinned / "其他同名檔.lnk"
    shortcut(selected, original)
    shortcut(ignored, other)
    previous = ignored.read_bytes()
    settings = wi.InstallSettings(new, False, run, None, (original,))
    settings.apply()
    assert shortcut(selected) == new
    assert ignored.read_bytes() == previous
