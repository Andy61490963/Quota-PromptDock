"""驗證檔案占用診斷保留不確定性，並只查詢隔離測試檔案。"""
import ctypes
import os
import shutil
import stat
import subprocess
from ctypes import wintypes
from types import SimpleNamespace

import pytest

import windows_file_diagnostics as diagnostics


pytestmark = pytest.mark.skipif(os.name != "nt", reason="驗證 Windows 檔案屬性與 Restart Manager")


def test_readonly_attributes_and_missing_file_are_reported_without_modification(tmp_path, monkeypatch):
    source = tmp_path / "暫存.exe"
    target = tmp_path / "目標.exe"
    source.write_bytes(b"source")
    source.chmod(stat.S_IREAD)
    monkeypatch.setattr(diagnostics, "_query_file_users", lambda *_: [])
    try:
        result = diagnostics.describe_file_access(source, target)
        assert "暫存檔唯讀：是" in result
        assert "目標檔唯讀：檔案不存在" in result
        assert "仍無法判定" in result
        assert source.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
        assert source.read_bytes() == b"source"
        assert not target.exists()
    finally:
        source.chmod(stat.S_IWRITE)


def test_query_failure_keeps_file_details_and_does_not_escape(tmp_path, monkeypatch):
    source = tmp_path / "暫存.exe"
    target = tmp_path / "目標.exe"
    source.write_bytes(b"source")
    target.write_bytes(b"target")

    def fail(*_):
        raise OSError("RmGetList 錯誤碼 5")

    monkeypatch.setattr(diagnostics, "_query_file_users", fail)
    result = diagnostics.describe_file_access(source, target)
    assert "暫存檔唯讀：否；目標檔唯讀：否" in result
    assert "占用查詢失敗，無法判定" in result
    assert "錯誤碼 5" in result


def test_program_list_is_limited_and_names_cannot_add_log_lines(tmp_path, monkeypatch):
    users = [(f"應用程式 {number}\n換行", number) for number in range(1, 8)]
    monkeypatch.setattr(diagnostics, "_query_file_users", lambda *_: users)
    result = diagnostics.describe_file_access(tmp_path / "source", tmp_path / "target")
    assert result.count("PID ") == 5
    assert "另有 2 個程序" in result
    assert "5 換行（PID 5）" in result
    assert "無法單獨判定" in result


@pytest.mark.parametrize("failure", ["RmStartSession", "RmRegisterResources", "RmGetList"])
def test_restart_manager_sessions_are_closed_after_query_failure(tmp_path, monkeypatch, failure):
    calls = []

    def operation(name):
        def call(*_):
            calls.append(name)
            return 5 if name == failure else 0
        return call

    api = SimpleNamespace(**{name: operation(name) for name in (
        "RmStartSession", "RmRegisterResources", "RmGetList", "RmEndSession",
    )})
    monkeypatch.setattr(diagnostics, "_restart_manager", lambda: api)
    result = diagnostics.describe_file_access(tmp_path / "source", tmp_path / "target")
    assert f"{failure} 錯誤碼 5" in result
    assert calls.count("RmEndSession") == (0 if failure == "RmStartSession" else 1)


def test_restart_manager_reports_running_temporary_executable_without_stopping_it(tmp_path):
    target = tmp_path / "占用測試.exe"
    source = tmp_path / "待替換.exe"
    shutil.copyfile(os.path.join(os.environ["SystemRoot"], "System32", "cmd.exe"), target)
    source.write_bytes(b"replacement")
    process = subprocess.Popen([str(target), "/d", "/q", "/k"], stdin=subprocess.PIPE,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        assert process.poll() is None
        with pytest.raises(PermissionError):
            os.replace(source, target)
        result = diagnostics.describe_file_access(source, target)
        assert f"PID {process.pid}" in result
        assert "暫存檔唯讀：否；目標檔唯讀：否" in result
        assert process.poll() is None
        assert source.read_bytes() == b"replacement"
    finally:
        try:
            process.communicate(b"exit\r\n", timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_native_handle_lock_is_not_reported_as_definitely_unlocked(tmp_path):
    source = tmp_path / "暫存.exe"
    target = tmp_path / "占用中的檔案.exe"
    source.write_bytes(b"source")
    target.write_bytes(b"target")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateFileW(str(target), 0x80000000, 3, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        with pytest.raises(PermissionError):
            os.replace(source, target)
        result = diagnostics.describe_file_access(source, target)
        assert "目標檔唯讀：否" in result
        # Restart Manager 不保證辨識一般資料檔的所有控制代碼。
        assert "無法" in result
        assert target.read_bytes() == b"target"
    finally:
        kernel32.CloseHandle(handle)
