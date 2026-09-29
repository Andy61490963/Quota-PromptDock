"""首次使用的額度來源引導；連線檢查與外部動作由主程式處理。"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
    QRadioButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)


SOURCES = ("codex", "claude", "both")
PROVIDERS = ("codex", "claude")
STATUSES = (
    "unchecked", "checking", "available", "not_installed",
    "not_logged_in", "temporary_failure",
)
PROVIDER_NAMES = {"codex": "Codex", "claude": "Claude Code"}
STATUS_NAMES = {
    "unchecked": "未檢查",
    "checking": "檢查中",
    "available": "可用",
    "not_installed": "未安裝",
    "not_logged_in": "未登入",
    "temporary_failure": "暫時失敗",
}
STATUS_COLORS = {
    "unchecked": "#94A3B8",
    "checking": "#FBBF24",
    "available": "#6EE7B7",
    "not_installed": "#FCA5A5",
    "not_logged_in": "#FBBF24",
    "temporary_failure": "#FCA5A5",
}

ONBOARDING_STYLE = """
QDialog#onboardingDialog { background: #0B1220; color: #F8FAFC; }
QWidget#onboardingContent { background: #0B1220; }
QLabel#onboardingTitle { color: #F8FAFC; font-size: 22px; font-weight: 750; }
QLabel#onboardingHeading { color: #F8FAFC; font-size: 15px; font-weight: 700; }
QLabel#onboardingBody, QLabel#onboardingNote { color: #CBD5E1; font-size: 13px; }
QLabel#onboardingStatus { font-size: 12px; font-weight: 700; }
QFrame#onboardingCard { background: #111C2E; border: 1px solid #334155; border-radius: 10px; }
QRadioButton { color: #F8FAFC; font-size: 14px; min-height: 27px; }
QRadioButton:focus, QPushButton:focus { outline: 2px solid #F8FAFC; }
QPushButton#onboardingPrimary { background: #35E28A; color: #07130D; border: 0; border-radius: 8px;
    min-height: 34px; padding: 0 12px; font-weight: 700; }
QPushButton#onboardingSecondary { background: #182438; color: #F8FAFC; border: 1px solid #334155;
    border-radius: 8px; min-height: 34px; padding: 0 12px; }
QPushButton:disabled { color: #94A3B8; background: #172235; }
"""


class OnboardingDialog(QDialog):
    """顯示來源選擇與各服務狀態，不自行存設定、啟動 CLI 或連線。"""

    source_selected = Signal(str)
    refresh_requested = Signal(str)
    provider_action_requested = Signal(str, str)
    completed = Signal(str)

    def __init__(
        self,
        selected_source: str = "both",
        style_sheet: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("onboardingDialog")
        self.setWindowTitle("開始使用 QuotaDock")
        self.setAccessibleName("首次使用與連線設定")
        self.setStyleSheet(style_sheet + "\n" + ONBOARDING_STYLE)
        screen = parent.screen() if parent is not None else QApplication.primaryScreen()
        available = screen.availableGeometry()
        minimum_width = max(240, min(300, available.width() - 24))
        minimum_height = max(260, min(300, available.height() - 32))
        self.setMinimumSize(minimum_width, minimum_height)
        self.resize(
            max(minimum_width, min(500, available.width() - 24)),
            max(minimum_height, min(620, available.height() - 32)),
        )
        self._source = "both"
        self._source_buttons: dict[str, QRadioButton] = {}
        self._cards: dict[str, QFrame] = {}
        self._status_labels: dict[str, QLabel] = {}
        self._detail_labels: dict[str, QLabel] = {}
        self._action_buttons: dict[str, QPushButton] = {}
        self._actions: dict[str, tuple[str, str]] = {}
        self._statuses: dict[str, str] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 16)
        outer.setSpacing(12)
        title = self._label("開始使用 QuotaDock", "onboardingTitle")
        outer.addWidget(title)
        introduction = self._label(
            "選擇要查看的訂閱額度來源。尚未安裝或登入也可以先使用常用指令，之後隨時回來設定。",
            "onboardingBody",
        )
        outer.addWidget(introduction)

        scroll = QScrollArea()
        scroll.setObjectName("onboardingScroll")
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll, 1)
        content = QWidget()
        content.setObjectName("onboardingContent")
        scroll.setWidget(content)
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 4, 4, 4)
        body.setSpacing(12)

        body.addWidget(self._label("額度來源", "onboardingHeading"))
        source_group = QButtonGroup(self)
        source_group.setExclusive(True)
        for source, label in (("codex", "Codex"), ("claude", "Claude Code"), ("both", "兩者都顯示")):
            radio = QRadioButton(label)
            radio.setObjectName(f"source_{source}")
            radio.setAccessibleName(f"額度來源：{label}")
            radio.toggled.connect(lambda checked, value=source: self._source_toggled(value, checked))
            source_group.addButton(radio)
            self._source_buttons[source] = radio
            body.addWidget(radio)
        self._source_group = source_group

        body.addWidget(self._label("連線狀態", "onboardingHeading"))
        self.refresh_button = QPushButton("檢查所選來源")
        self.refresh_button.setObjectName("onboardingSecondary")
        self.refresh_button.setAccessibleName("檢查所選額度來源的連線狀態")
        self.refresh_button.clicked.connect(lambda: self.refresh_requested.emit(self.selected_source))
        body.addWidget(self.refresh_button)

        for provider in PROVIDERS:
            card = self._make_provider_card(provider)
            body.addWidget(card)
        body.addWidget(self._label(
            "訂閱額度：顯示 Codex／Claude Code 回傳的剩餘比例與重置時間。\n"
            "本機 Token：只統計這台電腦上的 Codex 紀錄，不包含其他裝置或 Claude Code。\n"
            "API 估價：把本機 Token 依公開 API 單價換算，不是帳單或訂閱剩餘額度。",
            "onboardingNote",
        ))
        body.addStretch()

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self.later_button = QPushButton("稍後再說")
        self.later_button.setObjectName("onboardingSecondary")
        self.later_button.clicked.connect(self.reject)
        buttons.addWidget(self.later_button)
        buttons.addStretch()
        self.finish_button = QPushButton("完成設定")
        self.finish_button.setObjectName("onboardingPrimary")
        self.finish_button.setAutoDefault(False)
        self.finish_button.clicked.connect(self._complete)
        buttons.addWidget(self.finish_button)
        outer.addLayout(buttons)

        self.set_source(selected_source)
        for provider in PROVIDERS:
            self.set_provider_status(provider, "unchecked")

    @staticmethod
    def _label(text: str, object_name: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName(object_name)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setMinimumWidth(0)
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        return label

    def _make_provider_card(self, provider: str) -> QFrame:
        card = QFrame()
        card.setObjectName("onboardingCard")
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)
        header = QHBoxLayout()
        header.addWidget(self._label(PROVIDER_NAMES[provider], "onboardingHeading"), 1)
        status = self._label("未檢查", "onboardingStatus")
        status.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Preferred)
        status.setMinimumWidth(64)
        status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(status)
        layout.addLayout(header)
        detail = self._label("", "onboardingBody")
        layout.addWidget(detail)
        action = QPushButton()
        action.setObjectName("onboardingSecondary")
        action.clicked.connect(lambda checked=False, name=provider: self._request_action(name))
        layout.addWidget(action)
        self._cards[provider] = card
        self._status_labels[provider] = status
        self._detail_labels[provider] = detail
        self._action_buttons[provider] = action
        return card

    @property
    def selected_source(self) -> str:
        return self._source

    def set_source(self, source: str) -> None:
        if source not in SOURCES:
            raise ValueError(f"未知的額度來源：{source}")
        self._source = source
        self._source_buttons[source].setChecked(True)
        self._update_visible_cards()

    def _source_toggled(self, source: str, checked: bool) -> None:
        if not checked or self._source == source:
            return
        self._source = source
        self._update_visible_cards()
        self.source_selected.emit(source)

    def _update_visible_cards(self) -> None:
        for provider, card in self._cards.items():
            card.setVisible(self._source == "both" or self._source == provider)
        self._update_refresh_button()

    def _update_refresh_button(self) -> None:
        selected = PROVIDERS if self._source == "both" else (self._source,)
        checking = any(self._statuses.get(provider) == "checking" for provider in selected)
        self.refresh_button.setEnabled(not checking)
        self.refresh_button.setText("檢查中…" if checking else "檢查所選來源")

    def set_provider_status(self, provider: str, status: str, detail: str = "") -> None:
        if provider not in PROVIDERS:
            raise ValueError(f"未知的服務：{provider}")
        if status not in STATUSES:
            raise ValueError(f"未知的連線狀態：{status}")
        name = PROVIDER_NAMES[provider]
        default_details = {
            "unchecked": "尚未檢查。按下方按鈕即可查看目前狀態。",
            "checking": "正在讀取連線狀態，稍候會更新結果。",
            "available": "已可讀取訂閱額度。",
            "not_installed": (
                "找不到 Codex 本機服務。請先安裝 Codex，再回來重新檢查。"
                if provider == "codex" else
                "找不到 Claude Code CLI。若已安裝，請指定執行檔位置；否則先安裝再重新檢查。"
            ),
            "not_logged_in": (
                "請在 Codex 完成登入，再回來重新檢查。"
                if provider == "codex" else
                "請登入 Claude Code CLI；桌面版登入不會建立 CLI 憑證。"
            ),
            "temporary_failure": "目前無法判定連線狀態。請稍後再試。",
        }
        action = {
            "unchecked": ("檢查狀態", "refresh"),
            "checking": ("檢查中…", "none"),
            "available": ("重新檢查", "refresh"),
            "not_installed": ("查看安裝說明", "install") if provider == "codex" else ("指定 CLI 位置", "locate"),
            "not_logged_in": ("查看登入說明", "login") if provider == "codex" else ("登入 Claude Code", "login"),
            "temporary_failure": ("再試一次", "refresh"),
        }[status]
        self._status_labels[provider].setText(STATUS_NAMES[status])
        self._status_labels[provider].setStyleSheet(f"color: {STATUS_COLORS[status]};")
        self._detail_labels[provider].setText(detail or default_details[status])
        self._action_buttons[provider].setText(action[0])
        self._action_buttons[provider].setEnabled(status != "checking")
        self._action_buttons[provider].setAccessibleName(f"{name}：{action[0]}")
        self._actions[provider] = action
        self._statuses[provider] = status
        self._update_refresh_button()

    def _request_action(self, provider: str) -> None:
        action = self._actions[provider][1]
        if action == "refresh":
            self.refresh_requested.emit(provider)
        elif action != "none":
            self.provider_action_requested.emit(provider, action)

    def _complete(self) -> None:
        self.completed.emit(self.selected_source)
        self.accept()
