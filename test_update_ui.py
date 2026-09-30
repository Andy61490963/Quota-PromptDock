"""更新程序無法啟動時，主程式仍可使用且會顯示可重試狀態。"""
from argparse import Namespace
from pathlib import Path
import subprocess

import pytest
from PySide6.QtWidgets import QApplication

import app


@pytest.fixture
def widget(tmp_path, monkeypatch):
    qapp = QApplication.instance() or QApplication([])
    monkeypatch.setattr(app, "APP_DIR", tmp_path)
    monkeypatch.setattr(app, "STATE_PATH", tmp_path / "quota.json")
    monkeypatch.setattr(app, "CLAUDE_STATE_PATH", tmp_path / "claude.json")
    view = app.UsageWidget(demo=True)
    yield view
    view._force_quit = True
    view.tray.hide()
    view.alert_bubble.close()
    view.close()


def test_failed_installer_launch_keeps_current_app_open(widget, monkeypatch):
    quits = []
    monkeypatch.setattr(widget, "quit_app", lambda: quits.append(True))
    def cannot_launch(*args, **kwargs):
        raise OSError("模擬無法執行安裝檔")
    monkeypatch.setattr(app.subprocess, "Popen", cannot_launch)
    widget._update_version = "1.4.5"
    widget._on_update_ready("unused.exe")
    assert not quits
    assert widget.update_button.isEnabled()
    assert "無法啟動安裝程式" in widget.error_label.text()


def test_install_failure_has_visible_error_and_nonzero_exit(widget, monkeypatch):
    monkeypatch.setattr(app, "parse_args", lambda: Namespace(install=True, demo=False, screenshot=None))
    def fail():
        raise RuntimeError("安裝失敗，已保留原版本。")
    monkeypatch.setattr(app, "install_frozen_release", fail)
    messages = []
    monkeypatch.setattr(app.QMessageBox, "critical", lambda parent, title, text: messages.append(text))
    assert app.main() == 1
    assert messages == ["安裝失敗，已保留原版本。"]


def test_unwritable_download_directory_reenables_retry(widget, monkeypatch):
    class ImmediateThread:
        def __init__(self, target, **kwargs):
            self.target = target
        def start(self):
            self.target()
    def fail(**kwargs):
        raise OSError("暫存空間不足")
    monkeypatch.setattr(app.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(app.tempfile, "mkdtemp", fail)
    widget._update_url = "unused"
    widget._on_update_clicked()
    assert not widget._update_downloading
    assert widget.update_button.isEnabled()
    assert "暫存空間不足" in widget.error_label.text()


def test_frozen_install_failure_exposes_saved_diagnostic(widget, tmp_path, monkeypatch):
    monkeypatch.setattr(app.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app.sys, "executable", str(tmp_path / "download.exe"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(app, "QSettings", lambda *args: widget.settings)
    cleaned = []
    monkeypatch.setattr(app, "schedule_download_cleanup", cleaned.append)
    def fail(*args):
        try:
            raise subprocess.CalledProcessError(1, ["powershell.exe"], stderr=b"NoProcessFoundForGivenId")
        except subprocess.CalledProcessError as cause:
            raise RuntimeError("安裝失敗。\n失敗步驟：等待舊版結束") from cause
    monkeypatch.setattr(app, "install_windows_release", fail)
    with pytest.raises(RuntimeError, match="安裝紀錄：") as result:
        app.install_frozen_release()
    path = tmp_path / "install.log"
    assert str(path) in str(result.value)
    assert "NoProcessFoundForGivenId" in path.read_text(encoding="utf-8")
    assert cleaned == [Path(app.sys.executable).resolve()]
