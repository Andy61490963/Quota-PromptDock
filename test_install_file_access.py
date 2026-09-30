"""使用隔離檔案與 Windows 原生句柄驗證安裝替換的等待與還原。"""

import ctypes
import os
import threading
from pathlib import Path

import pytest

import updates


pytestmark = pytest.mark.skipif(os.name != "nt", reason="需要 Windows 原生檔案分享模式")


class NoDeleteShare:
    """持有允許讀寫但不允許刪除分享的真實 Windows 檔案句柄。"""

    def __init__(self, path: Path) -> None:
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel.CreateFileW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ]
        self._kernel.CreateFileW.restype = ctypes.c_void_p
        self._kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self._kernel.CloseHandle.restype = ctypes.c_int
        self._guard = threading.Lock()
        self._handle = self._kernel.CreateFileW(str(path), 0x80000000, 0x1 | 0x2, None, 3, 0, None)
        if self._handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        with self._guard:
            if self._handle is not None:
                self._kernel.CloseHandle(self._handle)
                self._handle = None


@pytest.fixture
def installation(tmp_path):
    source = tmp_path / "download.exe"
    target = tmp_path / "installed" / "QuotaDock.exe"
    source.write_bytes(b"new-version")
    target.parent.mkdir()
    target.write_bytes(b"old-version")
    return source, target


def _release_after_native_failure(monkeypatch, predicate, holder: NoDeleteShare):
    original = os.replace
    failures = []
    release = threading.Timer(0.15, holder.close)
    release.daemon = True

    def observed(source, target):
        try:
            return original(source, target)
        except OSError as exc:
            if predicate(Path(source), Path(target)):
                failures.append(exc.winerror)
                if len(failures) == 1:
                    release.start()
            raise

    monkeypatch.setattr(updates.os, "replace", observed)
    return failures, release


def test_target_handle_released_during_retry_completes_install(installation, monkeypatch):
    source, target = installation
    monkeypatch.setattr(updates, "FILE_REPLACE_TIMEOUT_SECONDS", 1.5)
    holder = None
    release = None
    failures = []

    def stop():
        nonlocal holder, release, failures
        holder = NoDeleteShare(target)
        failures, release = _release_after_native_failure(
            monkeypatch,
            lambda src, dst: src.name.startswith(".quota-install-") and dst == target,
            holder,
        )
        return failures

    try:
        updates.install_release(source, target, stop, lambda: None, lambda: None)
        assert target.read_bytes() == b"new-version"
        assert target.with_name(target.name + ".bak").read_bytes() == b"old-version"
        assert failures and all(code == 5 for code in failures)
    finally:
        if release is not None:
            release.cancel()
        if holder is not None:
            holder.close()


def test_target_handle_timeout_keeps_old_file_and_recovers(installation, monkeypatch):
    source, target = installation
    monkeypatch.setattr(updates, "FILE_REPLACE_TIMEOUT_SECONDS", 0.15)
    holder = None
    recovered = []

    def stop():
        nonlocal holder
        holder = NoDeleteShare(target)

    def recover():
        recovered.append(target.read_bytes())

    try:
        with pytest.raises(RuntimeError, match="仍無法替換") as failure:
            updates.install_release(source, target, stop, lambda: None, lambda: None, recover=recover)
        assert "失敗步驟：替換程式" in str(failure.value)
        assert isinstance(failure.value.__cause__, RuntimeError)
        assert isinstance(failure.value.__cause__.__cause__, PermissionError)
        assert failure.value.__cause__.__cause__.winerror == 5
        assert target.read_bytes() == b"old-version"
        assert recovered == [b"old-version"]
    finally:
        if holder is not None:
            holder.close()


def test_staged_source_handle_released_during_retry_completes_install(installation, monkeypatch):
    source, target = installation
    monkeypatch.setattr(updates, "FILE_REPLACE_TIMEOUT_SECONDS", 1.5)
    holder = None
    release = None
    failures = []

    def stop():
        nonlocal holder, release, failures
        staged = list(target.parent.glob(".quota-install-*.exe"))
        assert len(staged) == 1
        holder = NoDeleteShare(staged[0])
        failures, release = _release_after_native_failure(
            monkeypatch,
            lambda src, dst: src == staged[0] and dst == target,
            holder,
        )
        return failures

    try:
        updates.install_release(source, target, stop, lambda: None, lambda: None)
        assert target.read_bytes() == b"new-version"
        assert failures and all(code == 32 for code in failures)
    finally:
        if release is not None:
            release.cancel()
        if holder is not None:
            holder.close()


def test_other_windows_error_fails_without_retry(installation, monkeypatch):
    source, target = installation
    original = os.replace
    attempts = []
    recovered = []

    def unrelated_error(src, dst):
        if Path(src).name.startswith(".quota-install-") and Path(dst) == target:
            attempts.append(1)
            raise ctypes.WinError(112)
        return original(src, dst)

    monkeypatch.setattr(updates.os, "replace", unrelated_error)
    with pytest.raises(RuntimeError, match="失敗步驟：替換程式") as failure:
        updates.install_release(source, target, lambda: None, lambda: None, lambda: None,
                                recover=lambda: recovered.append(target.read_bytes()))
    assert len(attempts) == 1
    assert isinstance(failure.value.__cause__, OSError)
    assert failure.value.__cause__.winerror == 112
    assert target.read_bytes() == b"old-version"
    assert recovered == [b"old-version"]


def test_rollback_waits_for_new_target_handle_to_close(installation, monkeypatch):
    source, target = installation
    monkeypatch.setattr(updates, "FILE_REPLACE_TIMEOUT_SECONDS", 1.5)
    backup = target.with_name(target.name + ".bak")
    holder = None
    release = None
    recovered = []
    failures = []

    def launch():
        nonlocal holder, release, failures
        assert target.read_bytes() == b"new-version"
        holder = NoDeleteShare(target)
        failures, release = _release_after_native_failure(
            monkeypatch,
            lambda src, dst: src == backup and dst == target,
            holder,
        )
        raise RuntimeError("模擬新版啟動失敗")

    try:
        with pytest.raises(RuntimeError, match="失敗步驟：啟動新版") as failure:
            updates.install_release(source, target, lambda: None, lambda: None, launch,
                                    recover=lambda: recovered.append(target.read_bytes()))
        assert "無法自動還原" not in str(failure.value)
        assert target.read_bytes() == b"old-version"
        assert recovered == [b"old-version"]
        assert failures and all(code == 5 for code in failures)
    finally:
        if release is not None:
            release.cancel()
        if holder is not None:
            holder.close()
