"""Windows 安裝、捷徑與啟動設定；檔案替換交由可回復的更新服務處理。"""
from __future__ import annotations

import base64
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from install_handoff import HANDOFF_PATH, HANDOFF_TOKEN, ORIGIN_EXE, ORIGIN_PID, UpdateOrigin
from updates import install_release

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
READY_PATH_ENV = "QUOTA_PROMPTDOCK_READY_FILE"
READY_TOKEN_ENV = "QUOTA_PROMPTDOCK_READY_TOKEN"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAMES = ("QuotaDock", "CodexUsageWidget")


def signal_startup_ready() -> None:
    """由主視窗進入事件迴圈後通知安裝程序，避免把成功建立程序誤當啟動成功。"""
    filename = os.environ.pop(READY_PATH_ENV, "")
    token = os.environ.pop(READY_TOKEN_ENV, "")
    if filename and token:
        try:
            Path(filename).write_text(token, encoding="utf-8")
        except OSError:
            # 無法交付就緒訊號時，由安裝程序逾時並還原。
            pass


def wait_for_startup(process, ready_path: Path, token: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("新版在完成啟動前結束，正在還原原版本。")
        try:
            if ready_path.read_text(encoding="utf-8") == token:
                return
        except OSError:
            pass
        time.sleep(0.05)
    raise RuntimeError("新版啟動逾時，正在還原原版本。")


def set_frozen_autostart(executable: Path, enabled: bool) -> None:
    import winreg

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY,
                             0, winreg.KEY_SET_VALUE)
    except FileNotFoundError:
        if not enabled:
            return
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY,
                                 0, winreg.KEY_SET_VALUE)
    with key:
        if enabled:
            winreg.SetValueEx(key, "QuotaDock", 0, winreg.REG_SZ, f'"{executable.resolve()}"')
        else:
            try:
                winreg.DeleteValue(key, "QuotaDock")
            except FileNotFoundError:
                pass
        try:
            winreg.DeleteValue(key, "CodexUsageWidget")
        except FileNotFoundError:
            pass


def _desktop_path(powershell) -> Path:
    # 僅傳送 Base64 ASCII，避免 Windows PowerShell 主控台編碼破壞中文路徑。
    result = powershell(
        "[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes("
        "[Environment]::GetFolderPath('Desktop')))"
    )
    encoded = result.stdout.decode("ascii").strip()
    if not encoded:
        raise RuntimeError("無法取得桌面位置，已中止安裝設定。")
    return Path(base64.b64decode(encoded, validate=True).decode("utf-16-le"))


def _snapshot_run_values() -> dict[str, tuple[object, int] | None]:
    import winreg

    values: dict[str, tuple[object, int] | None] = {name: None for name in RUN_VALUE_NAMES}
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_QUERY_VALUE)
    except FileNotFoundError:
        return values
    with key:
        for name in RUN_VALUE_NAMES:
            try:
                values[name] = winreg.QueryValueEx(key, name)
            except FileNotFoundError:
                pass
    return values


def _restore_run_values(values: dict[str, tuple[object, int] | None]) -> None:
    import winreg

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
    except FileNotFoundError:
        if all(value is None for value in values.values()):
            return
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
    with key:
        for name, previous in values.items():
            if previous is None:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
            else:
                value, kind = previous
                winreg.SetValueEx(key, name, 0, kind, value)


class InstallSettings:
    """記住捷徑與開機啟動原值，讓安裝失敗能還原。"""

    def __init__(self, target: Path, autostart: bool, powershell) -> None:
        self.target = Path(target)
        self.autostart = autostart
        self.powershell = powershell
        self.shortcut: Path | None = None
        self.previous_shortcut: bytes | None = None
        self.previous_run_values: dict[str, tuple[object, int] | None] | None = None
        self.prepared = False

    def prepare(self) -> None:
        if self.prepared:
            return
        shortcut = _desktop_path(self.powershell) / "QuotaDock.lnk"
        if shortcut.is_symlink() or (shortcut.exists() and not shortcut.is_file()):
            raise RuntimeError("桌面捷徑位置不是檔案，已中止安裝設定。")
        previous_shortcut = shortcut.read_bytes() if shortcut.is_file() else None
        previous_run_values = _snapshot_run_values()
        self.shortcut = shortcut
        self.previous_shortcut = previous_shortcut
        self.previous_run_values = previous_run_values
        self.prepared = True

    def apply(self) -> None:
        self.prepare()
        assert self.shortcut is not None
        shortcut_path = base64.b64encode(str(self.shortcut).encode("utf-16-le")).decode("ascii")
        self.powershell(
            "$ErrorActionPreference='Stop'; $target=$env:QUOTADOCK_INSTALL_TARGET; "
            "$folder=Split-Path -Parent $target; "
            f"$path=[Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('{shortcut_path}')); "
            "$shell=New-Object -ComObject WScript.Shell; $shortcut=$shell.CreateShortcut($path); "
            "$shortcut.TargetPath=$target; $shortcut.WorkingDirectory=$folder; "
            "$shortcut.Description='AI 額度與常用指令小工具'; "
            "$shortcut.IconLocation=\"$target,0\"; $shortcut.Save()"
        )
        set_frozen_autostart(self.target, self.autostart)

    def rollback(self) -> None:
        if not self.prepared:
            return
        assert self.shortcut is not None and self.previous_run_values is not None
        failures = []
        try:
            if self.previous_shortcut is None:
                self.shortcut.unlink(missing_ok=True)
            else:
                staged: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(dir=self.shortcut.parent, prefix=".quota-shortcut-", delete=False) as handle:
                        staged = Path(handle.name)
                        handle.write(self.previous_shortcut)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(staged, self.shortcut)
                finally:
                    if staged is not None:
                        staged.unlink(missing_ok=True)
        except OSError as exc:
            failures.append(exc)
        try:
            _restore_run_values(self.previous_run_values)
        except OSError as exc:
            failures.append(exc)
        if failures:
            raise RuntimeError("安裝設定無法完整還原，請檢查桌面捷徑與開機啟動設定。") from failures[0]
        self.prepared = False


def install_windows_release(source: Path, target: Path, autostart: bool) -> None:
    origin = UpdateOrigin.from_environment()
    environment = os.environ.copy()
    for name in (HANDOFF_PATH, HANDOFF_TOKEN, ORIGIN_EXE, ORIGIN_PID):
        environment.pop(name, None)
    environment["QUOTADOCK_INSTALL_TARGET"] = str(target.resolve())
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"

    def powershell(script: str):
        return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                              check=True, timeout=25, capture_output=True,
                              creationflags=CREATE_NO_WINDOW, env=environment)

    def stop_running() -> None:
        # 只停止完整路徑相符的安裝版；先給視窗正常結束的機會。
        if origin is not None and not origin.handed_off:
            origin.stop()
        powershell(
            "$ErrorActionPreference='Stop'; "
            "$target=[IO.Path]::GetFullPath($env:QUOTADOCK_INSTALL_TARGET); "
            "$running=@(Get-CimInstance Win32_Process | Where-Object {$_.ExecutablePath -eq $target}); "
            "foreach($item in $running){ $p=Get-Process -Id $item.ProcessId -ErrorAction SilentlyContinue; "
            "if($p){ [void]$p.CloseMainWindow() } }; "
            "if($running.Count){ Start-Sleep -Seconds 2 }; "
            "Get-CimInstance Win32_Process | Where-Object {$_.ExecutablePath -eq $target} | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop }; "
            "$deadline=[DateTime]::UtcNow.AddSeconds(5); "
            "while(@(Get-CimInstance Win32_Process | Where-Object {$_.ExecutablePath -eq $target}).Count){ "
            "if([DateTime]::UtcNow -gt $deadline){ throw '無法停止既有安裝' }; Start-Sleep -Milliseconds 100 }"
        )

    settings = InstallSettings(target, autostart, powershell)
    # 新版啟動時也可能更新 Run 值，須先保存舊值才能在失敗時還原。
    settings.prepare()

    def finish_install() -> None:
        settings.apply()

    def rollback_settings() -> None:
        settings.rollback()

    def recover() -> None:
        if origin is not None and not origin.handed_off:
            # 原視窗仍在執行，不能多開另一份舊版。
            return
        previous = origin.executable if origin is not None else target
        if not previous.is_file():
            return
        subprocess.Popen([str(previous)], cwd=str(previous.parent), env=environment,
                         close_fds=True, creationflags=CREATE_NO_WINDOW)

    def launch() -> None:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".quota-ready-", delete=False) as handle:
            ready_path = Path(handle.name)
        token = uuid.uuid4().hex
        launch_environment = environment | {READY_PATH_ENV: str(ready_path), READY_TOKEN_ENV: token}
        process = None
        try:
            process = subprocess.Popen([str(target)], cwd=str(target.parent), env=launch_environment,
                                       close_fds=True, creationflags=CREATE_NO_WINDOW)
            wait_for_startup(process, ready_path, token)
        except Exception:
            if process is not None:
                try:
                    stop_running()
                except Exception:
                    pass
            raise
        finally:
            ready_path.unlink(missing_ok=True)

    install_release(source, target, stop_running, finish_install, launch,
                    recover=recover, rollback_settings=rollback_settings)
