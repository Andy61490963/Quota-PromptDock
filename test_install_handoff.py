"""驗證安裝交接與新版啟動確認，不停止使用者的既有程序。"""
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import install_handoff
from install_handoff import UpdateOrigin, wait_for_handoff
from windows_install import wait_for_startup


def test_installer_failure_before_handoff_keeps_old_app_in_control(tmp_path):
    path = tmp_path / "handoff.ready"
    path.touch()
    process = SimpleNamespace(poll=lambda: 1)
    with pytest.raises(RuntimeError, match="目前的程式仍可使用"):
        wait_for_handoff(process, path, "nonce")
    path.write_text("nonce", encoding="utf-8")
    wait_for_handoff(process, path, "nonce")


def test_slow_live_installer_keeps_exclusive_handoff_wait(tmp_path, monkeypatch):
    path = tmp_path / "handoff.ready"
    ticks = iter([0, 1000, 2000, 3000])
    monkeypatch.setattr(install_handoff.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(install_handoff.time, "sleep", lambda duration: None)
    def poll():
        path.write_text("nonce", encoding="utf-8")
        return None
    wait_for_handoff(SimpleNamespace(poll=poll), path, "nonce")


def test_created_process_must_signal_ready_before_install_succeeds(tmp_path):
    path = tmp_path / "ready"
    path.write_text("old-nonce", encoding="utf-8")
    with pytest.raises(RuntimeError, match="完成啟動前結束"):
        wait_for_startup(SimpleNamespace(poll=lambda: 1), path, "new-nonce")
    with pytest.raises(RuntimeError, match="啟動逾時"):
        wait_for_startup(SimpleNamespace(poll=lambda: None), path, "new-nonce", timeout=0.01)
    path.write_text("new-nonce", encoding="utf-8")
    wait_for_startup(SimpleNamespace(poll=lambda: None), path, "new-nonce")


@pytest.mark.skipif(os.name != "nt", reason="Windows 程序交接")
def test_handoff_verifies_and_waits_for_owned_child_process(tmp_path):
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.8)"],
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    path = tmp_path / "handoff.ready"
    origin = UpdateOrigin(Path(sys.executable), process.pid, path, "nonce")
    try:
        origin.stop()
        assert origin.handed_off
        assert path.read_text(encoding="utf-8") == "nonce"
        assert process.wait(timeout=2) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=2)
