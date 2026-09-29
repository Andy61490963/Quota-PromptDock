"""確認引導狀態、訊號、跳過與窄視窗鍵盤操作。"""
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QScrollArea

from onboarding import OnboardingDialog


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_source_can_be_changed_by_keyboard_and_reopened(qapp):
    dialog = OnboardingDialog(selected_source="claude")
    chosen = []
    dialog.source_selected.connect(chosen.append)
    dialog.show()
    qapp.processEvents()
    assert dialog.selected_source == "claude"
    assert dialog._cards["claude"].isVisible()
    assert not dialog._cards["codex"].isVisible()

    codex = dialog.findChild(type(dialog._source_buttons["codex"]), "source_codex")
    codex.setFocus()
    QTest.keyClick(codex, Qt.Key.Key_Space)
    assert dialog.selected_source == "codex"
    assert chosen == ["codex"]
    assert dialog._cards["codex"].isVisible()
    assert not dialog._cards["claude"].isVisible()
    qapp.processEvents()
    assert dialog._status_labels["codex"].width() >= 64
    dialog.close()

    reopened = OnboardingDialog(selected_source="both")
    assert reopened.selected_source == "both"
    reopened.set_source("claude")
    assert reopened.selected_source == "claude"
    reopened.close()


@pytest.mark.parametrize("provider,status,expected_action", [
    ("codex", "unchecked", ("refresh", "codex")),
    ("codex", "available", ("refresh", "codex")),
    ("codex", "not_installed", ("action", "codex", "install")),
    ("codex", "not_logged_in", ("action", "codex", "login")),
    ("codex", "temporary_failure", ("refresh", "codex")),
    ("claude", "not_installed", ("action", "claude", "locate")),
    ("claude", "not_logged_in", ("action", "claude", "login")),
])
def test_status_action_is_only_a_signal(qapp, provider, status, expected_action):
    dialog = OnboardingDialog()
    events = []
    dialog.refresh_requested.connect(lambda source: events.append(("refresh", source)))
    dialog.provider_action_requested.connect(
        lambda source, action: events.append(("action", source, action))
    )
    dialog.set_provider_status(provider, status, "顯示下一步")
    assert dialog._status_labels[provider].text()
    assert dialog._detail_labels[provider].text() == "顯示下一步"
    dialog._action_buttons[provider].click()
    assert events == [expected_action]

    dialog.set_provider_status(provider, "checking")
    assert not dialog._action_buttons[provider].isEnabled()
    dialog._action_buttons[provider].click()
    assert events == [expected_action]
    dialog.close()


def test_refresh_selection_and_complete_do_not_require_installed_provider(qapp):
    dialog = OnboardingDialog(selected_source="both")
    events = []
    completed = []
    dialog.refresh_requested.connect(events.append)
    dialog.completed.connect(completed.append)
    dialog.set_provider_status("codex", "not_installed")
    dialog.set_provider_status("claude", "not_logged_in")
    dialog.refresh_button.click()
    assert events == ["both"]
    dialog.finish_button.click()
    assert completed == ["both"]
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_checking_disables_duplicate_refresh_for_selected_source(qapp):
    dialog = OnboardingDialog(selected_source="both")
    events = []
    dialog.refresh_requested.connect(events.append)
    dialog.set_provider_status("claude", "checking")
    assert not dialog.refresh_button.isEnabled()
    dialog.refresh_button.click()
    assert events == []
    dialog.set_source("codex")
    assert dialog.refresh_button.isEnabled()
    dialog.refresh_button.click()
    assert events == ["codex"]
    dialog.close()


def test_later_and_window_close_both_skip_without_completion(qapp):
    for close_method in ("later", "window", "escape"):
        dialog = OnboardingDialog()
        completed = []
        dialog.completed.connect(completed.append)
        dialog.show()
        qapp.processEvents()
        if close_method == "later":
            dialog.later_button.click()
        elif close_method == "window":
            dialog.close()
        else:
            QTest.keyClick(dialog, Qt.Key.Key_Escape)
        assert not completed
        assert dialog.result() == QDialog.DialogCode.Rejected


def test_narrow_dialog_wraps_long_status_without_horizontal_scroll(qapp):
    dialog = OnboardingDialog()
    dialog.set_provider_status("claude", "temporary_failure", "暫時無法讀取連線狀態，" * 20)
    dialog.resize(330, 450)
    dialog.show()
    qapp.processEvents()
    scroll = dialog.findChild(QScrollArea, "onboardingScroll")
    assert dialog.width() <= 360
    assert scroll.horizontalScrollBar().maximum() == 0
    assert dialog.finish_button.isVisible()
    dialog.close()

    short_dialog = OnboardingDialog()
    short_dialog.resize(320, 300)
    short_dialog.show()
    qapp.processEvents()
    assert short_dialog.height() <= 320
    assert short_dialog.later_button.isVisible()
    assert short_dialog.finish_button.isVisible()
    assert short_dialog.findChild(QScrollArea, "onboardingScroll").horizontalScrollBar().maximum() == 0
    short_dialog.close()


def test_invalid_source_and_status_are_rejected(qapp):
    dialog = OnboardingDialog()
    with pytest.raises(ValueError):
        dialog.set_source("none")
    with pytest.raises(ValueError):
        dialog.set_provider_status("unknown", "available")
    with pytest.raises(ValueError):
        dialog.set_provider_status("codex", "signed_out")
    dialog.close()
