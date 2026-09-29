import pytest
from PySide6.QtCore import QRectF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import app
from app import AlertBubble, ClaudeUsageSnapshot, MiniUsageDisplay, MiniUsageWidget, UsageSnapshot, UsageWindow


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_multiple_alerts_are_shown_in_order(qapp):
    bubble = AlertBubble()
    anchor = QRectF(100, 100, 50, 50)
    bubble.show_message("Codex 額度偏低", "剩餘 10%", 5_000, anchor)
    bubble.show_message("Claude Code 額度偏低", "剩餘 5%", 5_000, anchor)

    assert bubble.title_label.text() == "Codex 額度偏低"
    assert bubble.message_label.text() == "剩餘 10%"
    bubble._advance()
    assert bubble.title_label.text() == "Claude Code 額度偏低"
    assert bubble.message_label.text() == "剩餘 5%"
    bubble._advance()
    assert not bubble.isVisible()
    assert not bubble.timer.isActive()
    bubble.close()


class MiniOwner:
    def __init__(self):
        self.expanded = 0

    def expand_from_mini(self):
        self.expanded += 1


def test_mini_is_keyboard_activatable_and_reports_source_and_staleness(qapp):
    owner = MiniOwner()
    mini = MiniUsageWidget(owner)
    mini.set_usage(MiniUsageDisplay(18, "Claude Code", "7 天", 2_000_000_000), "min", stale=True)
    assert mini.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert "Claude Code 7 天額度剩餘 18%" in mini.toolTip()
    assert "同步失敗" in mini.accessibleDescription()

    QTest.keyClick(mini, Qt.Key.Key_Return)
    QTest.keyClick(mini, Qt.Key.Key_Space)
    assert owner.expanded == 2
    mini.close()


def test_failed_codex_refresh_marks_previous_mini_value_as_old(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(app, "APP_DIR", tmp_path)
    monkeypatch.setattr(app, "STATE_PATH", tmp_path / "quota.json")
    monkeypatch.setattr(app, "CLAUDE_STATE_PATH", tmp_path / "claude.json")
    widget = app.UsageWidget(demo=True)
    widget.settings.setValue("mini_source", "codex")
    widget._snapshot = UsageSnapshot(
        plan_type="plus", limit_id="codex", limit_name=None,
        primary=UsageWindow(80, 300, 2_000_000_000), secondary=None,
        has_credits=False, unlimited_credits=False, credit_balance="0",
        reset_credits=0, reached_type=None, fetched_at=2_000_000_000,
    )
    widget._on_fetch_success({
        "codex_error": "暫時無法連線",
        "claude": ClaudeUsageSnapshot.unavailable(installed=False),
    })

    assert "Codex 5 小時額度剩餘 20%" in widget.mini.toolTip()
    assert "同步失敗，顯示上次成功資料" in widget.mini.accessibleDescription()
    assert widget.mini._stale
    widget._force_quit = True
    widget.tray.hide()
    widget.close()
