"""懸浮圖示旁的本機額度摘要，不自行讀取或更新遠端資料。"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

from PySide6.QtCore import QEvent, QRect, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


TAIWAN_TZ = timezone(timedelta(hours=8))
FONT_STACK = '"Segoe UI Variable", "Microsoft JhengHei UI", "Segoe UI"'
CARD_WIDTH = 324
SCREEN_MARGIN = 8
ANCHOR_GAP = 10


@dataclass(frozen=True)
class QuotaSummaryRow:
    provider: str
    window: str
    remaining_percent: float | None
    resets_at: int | None
    fetched_at: int | None
    stale: bool = False
    status: str = ""


def _clock(timestamp: int | None) -> str | None:
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp, TAIWAN_TZ).strftime("%m/%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return None


def _countdown(timestamp: int | None, now: int) -> str:
    clock = _clock(timestamp)
    if clock is None:
        return "重置時間未提供"
    if timestamp <= now:
        return f"已到重置時間 · {clock}"
    remaining = timestamp - now
    days, remaining = divmod(remaining, 86_400)
    hours, remaining = divmod(remaining, 3_600)
    minutes = remaining // 60
    if days:
        value = f"{days} 天 {hours} 小時"
    elif hours:
        value = f"{hours} 小時 {minutes} 分"
    else:
        value = f"{max(1, minutes)} 分鐘"
    return f"{value}後重置 · {clock}"


def _remaining(value: float | None, provider: str) -> tuple[str, str]:
    if (value is None or isinstance(value, bool) or
            not isinstance(value, (int, float)) or not math.isfinite(value)):
        return "—", "#94A3B8"
    value = max(0.0, min(100.0, value))
    normal = "#D8A96A" if "claude" in provider.lower() else "#35E28A"
    color = "#F87171" if value <= 15 else "#FBBF24" if value <= 30 else normal
    return f"{round(value)}%", color


def _status(row: QuotaSummaryRow) -> str:
    status = row.status.strip()
    if not row.stale:
        return status
    if "同步失敗" in status:
        return status
    return f"同步失敗 · {status or ('顯示上次成功資料' if row.fetched_at is not None else '暫無資料')}"


class _RowView(QFrame):
    def __init__(self, row: QuotaSummaryRow, parent: QWidget) -> None:
        super().__init__(parent)
        self.row = row
        self.setObjectName("summaryRowClaude" if "claude" in row.provider.lower() else "summaryRowCodex")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(3)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.name = QLabel(f"{row.provider} · {self._window_name(row.window)}")
        self.name.setObjectName("summaryName")
        self.remaining = QLabel()
        self.remaining.setObjectName("summaryRemaining")
        self.remaining.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(self.name, 1)
        top.addWidget(self.remaining)
        layout.addLayout(top)
        self.reset = QLabel()
        self.reset.setObjectName("summaryReset")
        self.reset.setWordWrap(True)
        layout.addWidget(self.reset)
        self.fetched = QLabel()
        self.fetched.setObjectName("summaryFetched")
        self.fetched.setWordWrap(True)
        layout.addWidget(self.fetched)
        self.status = QLabel()
        self.status.setObjectName("summaryStatus")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.refresh(int(time.time()))

    @staticmethod
    def _window_name(value: str) -> str:
        return {"5h": "5 小時", "7d": "7 天"}.get(value.lower(), value)

    def refresh(self, now: int) -> None:
        remaining, color = _remaining(self.row.remaining_percent, self.row.provider)
        self.remaining.setText(remaining)
        self.remaining.setStyleSheet(f"color: {color};")
        self.reset.setText(_countdown(self.row.resets_at, now))
        clock = _clock(self.row.fetched_at)
        self.fetched.setText(
            ("上次成功：" if self.row.stale else "資料時間：") + (clock or "尚無成功同步") +
            ("（台灣時間）" if clock else "")
        )
        status = _status(self.row)
        self.status.setText(status)
        self.status.setVisible(bool(status))
        self.status.setStyleSheet("color: #FBBF24;" if self.row.stale else "color: #CBD5E1;")
        self.setAccessibleName(f"{self.row.provider} {self._window_name(self.row.window)}額度")
        self.setAccessibleDescription(
            f"剩餘 {remaining}。{self.reset.text()}。{self.fetched.text()}。{status}".strip("。")
        )


class QuotaSummaryCard(QFrame):
    """顯示 mini 已取得的額度，與 mini 分開定位且不接收焦點。"""

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setObjectName("quotaSummaryCard")
        self.setAccessibleName("額度摘要")
        self._rows: tuple[QuotaSummaryRow, ...] = ()
        self._views: list[_RowView] = []
        self._anchor: QRect | None = None
        self._available: QRect | None = None
        self._filter_installed = False

        frame_layout = QVBoxLayout(self)
        frame_layout.setContentsMargins(2, 2, 2, 2)
        shell = QFrame(self)
        shell.setObjectName("summaryShell")
        frame_layout.addWidget(shell)
        layout = QVBoxLayout(shell)
        self._shell_layout = layout
        layout.setContentsMargins(13, 12, 13, 12)
        layout.setSpacing(8)
        heading = QHBoxLayout()
        self._heading_layout = heading
        title = QLabel("額度摘要")
        title.setObjectName("summaryTitle")
        hint = QLabel("↑↓ 捲動 · Esc 收起")
        hint.setObjectName("summaryHint")
        heading.addWidget(title)
        heading.addStretch()
        heading.addWidget(hint)
        layout.addLayout(heading)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("summaryScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.content = QWidget()
        self.content.setObjectName("summaryContent")
        self.items = QVBoxLayout(self.content)
        self.items.setContentsMargins(0, 0, 0, 0)
        self.items.setSpacing(6)
        self.scroll.setWidget(self.content)
        layout.addWidget(self.scroll)
        self.empty = QLabel("目前沒有額度資料")
        self.empty.setObjectName("summaryEmpty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.items.addWidget(self.empty)

        self.timer = QTimer(self)
        self.timer.setInterval(60_000)
        self.timer.timeout.connect(self._refresh_countdowns)
        self.setStyleSheet(
            """
            #summaryShell { background: #0B1220; border: 1px solid #3A4B65; border-radius: 15px; }
            #summaryTitle { color: #F8FAFC; font-family: __FONTS__; font-size: 15px; font-weight: 750; }
            #summaryHint { color: #94A3B8; font-family: __FONTS__; font-size: 11px; }
            #summaryScroll, #summaryContent { background: transparent; border: 0; }
            #summaryRowCodex { background: #111C2E; border: 1px solid #26344A; border-radius: 10px; }
            #summaryRowClaude { background: #171A20; border: 1px solid #5B4935; border-radius: 10px; }
            #summaryName { color: #E2E8F0; font-family: __FONTS__; font-size: 12px; font-weight: 700; }
            #summaryRemaining { font-family: __FONTS__; font-size: 16px; font-weight: 800; }
            #summaryReset { color: #CBD5E1; font-family: __FONTS__; font-size: 11px; }
            #summaryFetched { color: #94A3B8; font-family: __FONTS__; font-size: 10px; }
            #summaryStatus { font-family: __FONTS__; font-size: 11px; font-weight: 700; }
            #summaryEmpty { color: #94A3B8; font-family: __FONTS__; font-size: 12px; }
            QScrollBar:vertical { background: #111C2E; width: 7px; border: 0; }
            QScrollBar::handle:vertical { background: #334155; border-radius: 3px; min-height: 20px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            """.replace("__FONTS__", FONT_STACK)
        )
        self._size_for(QRect(0, 0, 1920, 1080))

    @property
    def rows(self) -> tuple[QuotaSummaryRow, ...]:
        return self._rows

    def set_rows(self, rows: Sequence[QuotaSummaryRow]) -> None:
        updated = tuple(rows[:4])
        if updated == self._rows:
            self._refresh_countdowns()
            return
        self._rows = updated
        for view in self._views:
            self.items.removeWidget(view)
            view.deleteLater()
        self._views = []
        self.empty.setVisible(not self._rows)
        for row in self._rows:
            view = _RowView(row, self.content)
            self.items.addWidget(view)
            self._views.append(view)
        self._refresh_countdowns()
        self._size_for(self._available or QRect(0, 0, 1920, 1080))
        if self.isVisible() and self._anchor is not None:
            self._place(self._anchor, self._available or self._screen_area(self._anchor))

    @staticmethod
    def _screen_area(anchor: QRect) -> QRect:
        app = QApplication.instance()
        screen = app.screenAt(anchor.center()) or app.primaryScreen()
        return screen.availableGeometry()

    def _size_for(self, available: QRect) -> None:
        width = max(1, min(CARD_WIDTH, available.width() - 2 * SCREEN_MARGIN))
        self.setFixedWidth(width)
        content_height = max(36, self.items.sizeHint().height() + 2)
        outer = self.layout().contentsMargins()
        inner = self._shell_layout.contentsMargins()
        extra = (outer.top() + outer.bottom() + inner.top() + inner.bottom()
                 + self._heading_layout.sizeHint().height() + self._shell_layout.spacing())
        max_height = max(1, available.height() - 2 * SCREEN_MARGIN)
        self.content.setMinimumHeight(content_height)
        self.scroll.setFixedHeight(max(1, min(content_height, max_height - extra)))
        self.setFixedHeight(min(max_height, extra + self.scroll.height()))

    def show_near(self, anchor: QRect, available: QRect | None = None) -> None:
        """以全域座標的 mini 外框為錨點，優先貼在旁邊。"""
        self._anchor = QRect(anchor)
        self._available = QRect(available) if available is not None else self._screen_area(anchor)
        self._refresh_countdowns()
        self._size_for(self._available)
        self._place(self._anchor, self._available)
        self.show()
        self.raise_()
        self.timer.start()
        app = QApplication.instance()
        if not self._filter_installed:
            app.installEventFilter(self)
            self._filter_installed = True

    def _place(self, anchor: QRect, available: QRect) -> None:
        margin, gap = SCREEN_MARGIN, ANCHOR_GAP
        left_room = anchor.left() - (available.left() + margin)
        right_room = available.right() - margin - anchor.right()
        if left_room >= self.width() + gap or right_room >= self.width() + gap:
            prefer_left = anchor.center().x() >= available.center().x()
            use_left = left_room >= self.width() + gap and (prefer_left or right_room < self.width() + gap)
            x = anchor.left() - self.width() - gap if use_left else anchor.right() + gap + 1
            y = anchor.center().y() - self.height() // 2
        else:
            x = anchor.center().x() - self.width() // 2
            above = anchor.top() - available.top() - margin
            below = available.bottom() - margin - anchor.bottom()
            y = (anchor.top() - self.height() - gap if above >= self.height() + gap or above >= below
                 else anchor.bottom() + gap + 1)
        max_x = max(available.left() + margin, available.right() - margin - self.width() + 1)
        max_y = max(available.top() + margin, available.bottom() - margin - self.height() + 1)
        self.move(
            min(max(x, available.left() + margin), max_x),
            min(max(y, available.top() + margin), max_y),
        )

    def _refresh_countdowns(self) -> None:
        now = int(time.time())
        for view in self._views:
            view.refresh(now)

    def dismiss(self) -> None:
        self.hide()
        self.timer.stop()
        self._anchor = None
        self._available = None
        if self._filter_installed:
            QApplication.instance().removeEventFilter(self)
            self._filter_installed = False

    def scroll_by(self, steps: int, *, page: bool = False) -> None:
        """由懸浮圖示的方向鍵或滾輪捲動摘要，卡片本身不搶焦點。"""
        bar = self.scroll.verticalScrollBar()
        distance = bar.pageStep() if page else max(24, bar.singleStep())
        bar.setValue(bar.value() + steps * distance)

    def eventFilter(self, watched, event) -> bool:
        if self.isVisible() and event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            self.dismiss()
        return super().eventFilter(watched, event)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self.timer.stop()
        if self._filter_installed:
            QApplication.instance().removeEventFilter(self)
            self._filter_installed = False

