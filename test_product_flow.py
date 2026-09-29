"""驗證產品引導與主介面的實際整合，隔離 CLI、登錄及原生貼上。"""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, QSettings, Qt
from PySide6.QtGui import QFocusEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

import app
from prompt_tools import PasteController


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def widget(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(app, "APP_DIR", tmp_path)
    monkeypatch.setattr(app, "STATE_PATH", tmp_path / "quota.json")
    monkeypatch.setattr(app, "CLAUDE_STATE_PATH", tmp_path / "claude.json")
    target = SimpleNamespace(previous=0, close=lambda: None)
    monkeypatch.setattr(app, "PasteController", lambda parent: PasteController(parent, target))
    view = app.UsageWidget(demo=True)
    view.show()
    QTest.qWait(150)
    yield view
    view._force_quit = True
    if view._onboarding_dialog is not None:
        view._onboarding_dialog.reject()
    view.timer.stop()
    view.countdown_timer.stop()
    view.mini.close()
    view.alert_bubble.close()
    view.tray.hide()
    view.close()


def test_onboarding_cancel_preserves_source_and_complete_persists(widget, qapp):
    widget.open_onboarding()
    dialog = widget._onboarding_dialog
    dialog.set_source("claude")
    dialog.reject()
    assert widget._tracked_providers() == ("codex", "claude")
    assert app._setting_bool(widget.settings, app.ONBOARDING_SEEN, False)
    widget.settings.setValue("mini_source", "codex")
    widget.open_onboarding()
    dialog = widget._onboarding_dialog
    dialog.set_source("claude")
    dialog.finish_button.click()
    qapp.processEvents()
    assert widget._tracked_providers() == ("claude",)
    assert widget.codex_card.isHidden() and not widget.claude_card.isHidden()
    assert widget.settings.value("mini_source") == "claude"
    assert all(row.provider == "Claude Code" for row in widget.mini.summary.rows)
    assert "Codex" not in widget._tray_tooltip()
    saved = QSettings(widget.settings.fileName(), QSettings.Format.IniFormat)
    assert not app._setting_bool(saved, "tracking/codex", True)
    widget.open_onboarding()
    assert widget._onboarding_dialog.selected_source == "claude"


def test_settings_can_open_guide_without_losing_draft(widget, qapp):
    settings = app.SettingsDialog(widget.settings, widget)
    settings.onboarding_requested.connect(lambda: widget.open_onboarding(parent=settings))
    settings.show_tokens.setChecked(False)
    settings.show()
    settings.connection_guide.click()
    qapp.processEvents()
    assert widget._onboarding_dialog.parentWidget() is settings
    widget._onboarding_dialog.reject()
    assert not settings.show_tokens.isChecked()
    assert app._setting_bool(widget.settings, "show_token_usage", True)
    settings.reject()


def fake_clients(monkeypatch, calls):
    def fetch_codex():
        calls.append("codex")
        return app._demo_snapshot()
    def fetch_claude():
        calls.append("claude")
        return app._demo_claude_snapshot()
    monkeypatch.setattr(app, "CodexUsageClient", lambda: SimpleNamespace(fetch=fetch_codex))
    monkeypatch.setattr(app, "ClaudeUsageClient", lambda: SimpleNamespace(fetch=fetch_claude))


def test_only_selected_provider_is_queried_and_other_state_is_preserved(widget, monkeypatch):
    calls = []
    fake_clients(monkeypatch, calls)
    class ImmediateThread:
        def __init__(self, target, **kwargs): self.target = target
        def start(self): self.target()
    monkeypatch.setattr(app.threading, "Thread", ImmediateThread)
    previous = widget._snapshot
    widget._complete_onboarding("claude")
    widget._demo = False
    widget.refresh()
    assert calls == ["claude"]
    assert widget._snapshot is previous and not widget._codex_sync_failed
    assert widget._provider_states["claude"][0] == "available"
    assert not widget._fetching


def test_source_change_during_fetch_runs_new_selection_after_completion(widget, monkeypatch, qapp):
    calls, tasks = [], []
    fake_clients(monkeypatch, calls)
    class DeferredThread:
        def __init__(self, target, **kwargs): self.target = target
        def start(self): tasks.append(self.target)
    monkeypatch.setattr(app.threading, "Thread", DeferredThread)
    widget._complete_onboarding("codex")
    widget._demo = False
    widget.refresh()
    widget._complete_onboarding("claude")
    assert len(tasks) == 1 and widget._fetching
    tasks.pop(0)()
    qapp.processEvents()
    assert len(tasks) == 1
    tasks.pop(0)()
    assert calls == ["codex", "claude"]
    assert not widget._fetching and widget._tracked_providers() == ("claude",)


@pytest.mark.parametrize("message,status", [
    ("未偵測到 Codex 桌面版。", "not_installed"),
    ("Codex 尚未登入。", "not_logged_in"),
    ("Codex 連線逾時，請確認桌面版已登入。", "temporary_failure"),
])
def test_guide_status_distinguishes_missing_login_and_temporary_failure(widget, message, status):
    widget.open_onboarding()
    widget._on_fetch_success({"providers": ("codex",), "codex_error": message})
    assert widget._provider_states["codex"][0] == status
    assert widget._onboarding_dialog._statuses["codex"] == status


def test_mini_summary_uses_cached_rows_and_closes_on_escape_and_hide(widget, qapp):
    mini = widget.mini
    mini.show()
    mini.focusInEvent(QFocusEvent(QEvent.Type.FocusIn))
    assert mini.summary.isVisible()
    assert len(mini.summary.rows) == 4
    assert any(row.remaining_percent is not None and not row.status for row in mini.summary.rows)
    QTest.keyClick(mini, Qt.Key.Key_Escape)
    assert not mini.summary.isVisible()
    mini._show_summary()
    mini.hide()
    assert not mini.summary.isVisible() and not mini._summary_timer.isActive()


def test_existing_configuration_skips_first_run_detection(widget):
    widget.settings.setValue("refresh_interval", 60)
    widget.settings.sync()
    reopened = app.UsageWidget(demo=True)
    assert not reopened._first_run
    reopened._force_quit = True
    reopened.timer.stop()
    reopened.mini.close()
    reopened.tray.hide()
    reopened.close()


def test_first_run_timer_waits_for_choice_but_explicit_check_works(widget, monkeypatch):
    tasks = []
    class DeferredThread:
        def __init__(self, target, **kwargs): self.target = target
        def start(self): tasks.append(self.target)
    monkeypatch.setattr(app.threading, "Thread", DeferredThread)
    widget._demo = False
    widget._first_run = True
    widget.settings.remove(app.ONBOARDING_SEEN)
    widget.refresh()
    assert tasks == [] and not widget._fetching
    widget.refresh(providers=("codex",))
    assert len(tasks) == 1 and widget._active_providers == ("codex",)


def test_old_settings_draft_cannot_restore_an_untracked_mini_source(widget, monkeypatch):
    monkeypatch.setattr(app, "configure_autostart", lambda enabled: None)
    settings = app.SettingsDialog(widget.settings, widget)
    settings.mini_source.setCurrentIndex(settings.mini_source.findData("claude"))
    widget._complete_onboarding("codex")
    settings.save()
    widget._update_mini_usage()
    assert widget.settings.value("mini_source") == "codex"
    assert widget.mini._remaining is not None


def test_inflight_usage_result_does_not_replace_login_progress(widget):
    widget._claude_logging_in = True
    widget._set_provider_status("claude", "checking", "等待完成登入")
    previous = widget._claude_snapshot
    widget._on_fetch_success({"providers": ("claude",),
                              "claude": app.ClaudeUsageSnapshot.unavailable(installed=True, logged_in=False)})
    assert widget._provider_states["claude"] == ("checking", "等待完成登入")
    assert widget._claude_snapshot is previous


def test_cancelled_claude_login_reports_failure(widget, monkeypatch):
    class ImmediateThread:
        def __init__(self, target, **kwargs): self.target = target
        def start(self): self.target()
    monkeypatch.setattr(app.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(app.ClaudeCliLocator, "locate", lambda: app.Path("claude.exe"))
    monkeypatch.setattr(app.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(wait=lambda: 1))
    widget._demo = False
    widget._start_claude_login()
    assert not widget._claude_logging_in
    assert widget._provider_states["claude"][0] == "temporary_failure"
    assert "取消" in widget._provider_states["claude"][1]


def test_completing_source_choice_discards_untracked_pending_checks(widget):
    widget._pending_providers.update(("codex", "claude"))
    widget._complete_onboarding("codex")
    assert widget._pending_providers == {"codex"}


def test_global_update_error_remains_visible_with_only_claude(widget, qapp):
    widget._complete_onboarding("claude")
    widget._on_update_failed("測試安裝校驗失敗")
    qapp.processEvents()
    assert widget.error_label.isVisible()
    assert "校驗失敗" in widget.error_label.text()
