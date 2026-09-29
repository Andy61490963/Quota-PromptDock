from __future__ import annotations

import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

import quota_summary as summary


NOW = 1_780_000_000


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def card(qapp):
    widget = summary.QuotaSummaryCard()
    yield widget
    widget.dismiss()
    widget.close()
    qapp.processEvents()


def rows():
    return [
        summary.QuotaSummaryRow("Codex", "5h", 74.6, NOW + 3_661, NOW - 100),
        summary.QuotaSummaryRow("Codex", "7d", 25, NOW + 86_400, NOW - 100),
        summary.QuotaSummaryRow("Claude Code", "5h", 10, NOW + 300, NOW - 400,
                                stale=True),
        summary.QuotaSummaryRow("Claude Code", "7d", None, None, None,
                                status="尚未登入"),
    ]


def test_four_rows_show_remaining_reset_time_and_sync_state(card, monkeypatch):
    monkeypatch.setattr(summary.time, "time", lambda: NOW)
    card.set_rows(rows() + [summary.QuotaSummaryRow("其他", "5h", 99, None, None)])
    assert len(card.rows) == len(card._views) == 4
    assert [view.remaining.text() for view in card._views] == ["75%", "25%", "10%", "—"]
    assert "1 小時 1 分後重置" in card._views[0].reset.text()
    assert "同步失敗" in card._views[2].status.text()
    assert "上次成功" in card._views[2].fetched.text()
    assert card._views[3].status.text() == "尚未登入"
    assert "尚無成功同步" in card._views[3].fetched.text()
    assert "其他" not in " ".join(view.name.text() for view in card._views)


def test_countdown_refreshes_without_new_rows(card, monkeypatch):
    current = [NOW]
    monkeypatch.setattr(summary.time, "time", lambda: current[0])
    card.set_rows(rows()[:1])
    first = card._views[0]
    assert "1 小時 1 分後重置" in first.reset.text()
    current[0] += 3_600
    card.timer.timeout.emit()
    assert first is card._views[0]
    assert "1 分鐘後重置" in first.reset.text()
    current[0] += 100
    card.timer.timeout.emit()
    assert "已到重置時間" in first.reset.text()


@pytest.mark.parametrize("anchor", [QRect(900, 300, 74, 74), QRect(8, 300, 74, 74)])
def test_card_stays_on_screen_beside_mini(card, anchor):
    area = QRect(0, 0, 1000, 700)
    card.set_rows(rows())
    card.show_near(anchor, area)
    assert area.contains(card.geometry())
    assert not card.geometry().intersects(anchor)
    assert card.timer.isActive()
    card.dismiss()
    assert not card.isVisible() and not card.timer.isActive()


def test_narrow_screen_uses_above_and_keeps_mini_clickable(card):
    area = QRect(100, 50, 340, 650)
    anchor = QRect(358, 610, 74, 74)
    card.set_rows(rows())
    card.show_near(anchor, area)
    assert area.contains(card.geometry())
    assert not card.geometry().intersects(anchor)
    assert card.windowFlags() & Qt.WindowType.WindowTransparentForInput
    assert card.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)


def test_escape_closes_without_accepting_focus(card, qapp):
    host = QWidget()
    host.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    host.show()
    host.activateWindow()
    host.setFocus()
    qapp.processEvents()
    focused = qapp.focusWidget()
    try:
        card.set_rows(rows()[:1])
        card.show_near(QRect(800, 200, 74, 74), QRect(0, 0, 1000, 700))
        assert card.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
        assert card.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        assert qapp.focusWidget() is focused
        QTest.keyClick(host, Qt.Key.Key_Escape)
        assert not card.isVisible()
    finally:
        host.close()


def test_short_screen_can_scroll_without_focusing_card(card, qapp):
    card.set_rows(rows())
    card.show_near(QRect(900, 100, 74, 74), QRect(0, 0, 1000, 220))
    qapp.processEvents()
    bar = card.scroll.verticalScrollBar()
    assert bar.maximum() > 0
    card.scroll_by(100, page=True)
    assert bar.value() == bar.maximum()
    views = tuple(card._views)
    card.set_rows(rows())
    assert tuple(card._views) == views
    assert bar.value() == bar.maximum()

