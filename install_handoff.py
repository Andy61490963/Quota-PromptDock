"""更新安裝檔準備完成後，才請目前的主視窗正常結束。"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

HANDOFF_PATH = "QUOTA_PROMPTDOCK_HANDOFF_FILE"
HANDOFF_TOKEN = "QUOTA_PROMPTDOCK_HANDOFF_TOKEN"
ORIGIN_PID = "QUOTA_PROMPTDOCK_ORIGIN_PID"
ORIGIN_EXE = "QUOTA_PROMPTDOCK_ORIGIN_EXE"


def handoff_environment(installer: Path) -> tuple[dict[str, str], Path, str]:
    token = uuid.uuid4().hex
    path = installer.parent / f"handoff-{token}.ready"
    path.touch(exist_ok=False)
    environment = os.environ.copy()
    environment.update({HANDOFF_PATH: str(path), HANDOFF_TOKEN: token,
                        ORIGIN_PID: str(os.getpid()), ORIGIN_EXE: sys.executable,
                        "PYINSTALLER_RESET_ENVIRONMENT": "1"})
    return environment, path, token


def wait_for_handoff(process, path: Path, token: str, timeout: float | None = None) -> None:
    # 安裝程序仍存活時持續等待，不能開放重試而同時啟動第二份安裝器。
    deadline = None if timeout is None else time.monotonic() + timeout
    while deadline is None or time.monotonic() < deadline:
        try:
            if path.read_text(encoding="utf-8") == token:
                return
        except OSError:
            pass
        if process.poll() is not None:
            raise RuntimeError("安裝未完成，目前的程式仍可使用。")
        time.sleep(0.1)
    raise RuntimeError("安裝準備逾時，目前的程式仍可使用。")


@dataclass
class UpdateOrigin:
    executable: Path
    pid: int
    handoff_file: Path
    token: str
    handed_off: bool = False

    @classmethod
    def from_environment(cls) -> UpdateOrigin | None:
        values = [os.environ.get(key, "") for key in (ORIGIN_EXE, ORIGIN_PID, HANDOFF_PATH, HANDOFF_TOKEN)]
        if not all(values):
            return None
        try:
            pid = int(values[1])
        except ValueError:
            raise RuntimeError("更新交接資訊無效。") from None
        if pid <= 0:
            raise RuntimeError("更新交接資訊無效。")
        return cls(Path(values[0]), pid, Path(values[2]), values[3])

    def stop(self) -> None:
        if self.handed_off:
            return
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x00100000 | 0x1000, False, self.pid)
        if not handle:
            if ctypes.get_last_error() == 87:
                self.handed_off = True
                return
            raise RuntimeError("無法確認目前程式狀態，已保留原版本。")
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(buffer))
            if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                raise RuntimeError("無法核對目前程式，已中止更新。")
            if Path(buffer.value).resolve() != self.executable.resolve():
                raise RuntimeError("目前程序與更新來源不符，已中止更新。")
            self.handoff_file.write_text(self.token, encoding="utf-8")
            if kernel.WaitForSingleObject(handle, 15_000) != 0:
                raise RuntimeError("目前程式未完成結束，請稍後再試。")
            self.handed_off = True
        finally:
            kernel.CloseHandle(handle)


def schedule_download_cleanup(source: Path) -> None:
    """只清理本工具建立的更新暫存目錄；等安裝程序退出才刪自己的執行檔。"""
    directory = source.resolve().parent
    if (directory.parent != Path(tempfile.gettempdir()).resolve()
            or not directory.name.startswith("QuotaDock-update-")
            or source.name != "QuotaDock-Windows-x64.exe"):
        return
    environment = os.environ.copy()
    environment["QUOTADOCK_CLEANUP_EXE"] = str(source.resolve())
    environment["QUOTADOCK_CLEANUP_PID"] = str(os.getpid())
    script = (
        "$ErrorActionPreference='SilentlyContinue'; "
        "Wait-Process -Id ([int]$env:QUOTADOCK_CLEANUP_PID); "
        "$exe=[IO.Path]::GetFullPath($env:QUOTADOCK_CLEANUP_EXE); $dir=Split-Path -Parent $exe; "
        "for($i=0;$i -lt 100;$i++){ "
        "Remove-Item -LiteralPath $exe -Force; if(-not(Test-Path -LiteralPath $exe)){break}; Start-Sleep -Milliseconds 200 }; "
        "Get-ChildItem -LiteralPath $dir -Filter 'handoff-*.ready' -File | Remove-Item -Force; "
        "[IO.Directory]::Delete($dir)"
    )
    try:
        subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                         env=environment, close_fds=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        pass


def discard_download(source: Path) -> None:
    """尚未執行或已結束的下載檔可立即清理，不遞迴刪除其他內容。"""
    directory = source.resolve().parent
    if (directory.parent != Path(tempfile.gettempdir()).resolve()
            or not directory.name.startswith("QuotaDock-update-")
            or source.name != "QuotaDock-Windows-x64.exe"):
        return
    try:
        source.unlink(missing_ok=True)
        for handoff in directory.glob("handoff-*.ready"):
            handoff.unlink(missing_ok=True)
        directory.rmdir()
    except OSError:
        pass
