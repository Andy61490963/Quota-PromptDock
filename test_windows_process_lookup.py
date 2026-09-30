"""CIM 無法讀到程序路徑時，安裝器仍須精確辨識目標程序。"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import windows_install as wi


def _run_stop_script(tmp_path: Path, monkeypatch, target: Path, prelude: str) -> None:
    real_run = subprocess.run
    monkeypatch.setattr(wi.UpdateOrigin, "from_environment", classmethod(lambda cls: None))
    monkeypatch.setattr(wi, "_desktop_path", lambda powershell: tmp_path)
    monkeypatch.setattr(wi, "_snapshot_run_values", lambda: {})
    monkeypatch.setattr(
        wi,
        "install_release",
        lambda source, target, stop_running, finish_install, launch, **callbacks: stop_running(),
    )

    def run_with_fake_cim(args, **kwargs):
        return real_run([*args[:-1], prelude + args[-1]], **kwargs)

    monkeypatch.setattr(wi.subprocess, "run", run_with_fake_cim)
    wi.install_windows_release(tmp_path / "download.exe", target, False)


def _start_test_process(path: Path) -> subprocess.Popen:
    # 測試檔位於獨立暫存目錄；不查詢或停止使用者安裝的程式。
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(os.environ["SystemRoot"]) / "System32" / "ping.exe", path)
    return subprocess.Popen(
        [str(path), "-n", "60", "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=wi.CREATE_NO_WINDOW,
    )


def _null_cim_path_for_pid(pid: int) -> str:
    return (
        "function Get-CimInstance { param($ClassName) "
        f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{ "
        f"[pscustomobject]@{{ Name='QuotaDock.exe'; ExecutablePath=$null; ProcessId={pid} }} "
        "} }; "
    )


@pytest.mark.skipif(os.name != "nt", reason="需要 Windows 原生程序路徑查詢")
def test_null_cim_path_stops_only_exact_target(tmp_path, monkeypatch):
    target = tmp_path / "installed" / "QuotaDock.exe"
    process = _start_test_process(target)
    try:
        _run_stop_script(tmp_path, monkeypatch, target, _null_cim_path_for_pid(process.pid))
        assert process.wait(timeout=5) is not None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="需要 Windows 原生程序路徑查詢")
def test_null_cim_path_preserves_same_name_at_different_path(tmp_path, monkeypatch):
    target = tmp_path / "installed" / "QuotaDock.exe"
    other = tmp_path / "other" / "QuotaDock.exe"
    process = _start_test_process(other)
    try:
        _run_stop_script(tmp_path, monkeypatch, target, _null_cim_path_for_pid(process.pid))
        assert process.poll() is None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="需要 Windows 原生程序路徑查詢")
def test_unverifiable_live_pid_fails_closed_with_pid(tmp_path, monkeypatch):
    pid = 2147483647
    prelude = (
        "function Get-CimInstance { param($ClassName) "
        f"[pscustomobject]@{{ Name='QuotaDock.exe'; ExecutablePath=$null; ProcessId={pid} }} "
        "}; "
        "function Get-Process { param($Id) [pscustomobject]@{ Id=$Id } }; "
    )
    with pytest.raises(subprocess.CalledProcessError) as failure:
        _run_stop_script(tmp_path, monkeypatch, tmp_path / "installed" / "QuotaDock.exe", prelude)
    message = failure.value.stderr.decode("utf-8", errors="replace")
    assert str(pid) in message
    assert "請先關閉該 App" in message


@pytest.mark.skipif(os.name != "nt", reason="需要 Windows 原生程序路徑查詢")
def test_unverifiable_exited_pid_is_ignored(tmp_path, monkeypatch):
    pid = 2147483647
    prelude = (
        "function Get-CimInstance { param($ClassName) "
        f"[pscustomobject]@{{ Name='QuotaDock.exe'; ExecutablePath=$null; ProcessId={pid} }} "
        "}; "
    )
    _run_stop_script(tmp_path, monkeypatch, tmp_path / "installed" / "QuotaDock.exe", prelude)
