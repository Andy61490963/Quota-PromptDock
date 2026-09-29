"""更新程序無法啟動時，主程式仍可使用且會顯示可重試狀態。"""
from argparse import Namespace

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
