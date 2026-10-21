import threading

import pytest
from PySide6.QtCore import QSettings, QTimer
from PySide6.QtTest import QTest

import app
import price_sync as ps
import token_panel as tp
from test_price_sync import flat
from test_token_panel import qapp, until
from test_token_usage import context, meta, response, write


def test_download_does_not_block_collection_or_ui_and_reprices_without_new_logs(qapp, tmp_path, monkeypatch):
    root = tmp_path / "codex"
    path = root / "sessions" / "rollout.jsonl"
    write(path, [meta(), context(model="future-sol"), response()])
    release = threading.Event()

    def slow_fetch():
        release.wait(5)
        return flat()

    monkeypatch.setattr(tp, "PriceSynchronizer", lambda catalog, changed:
                        ps.PriceSynchronizer(catalog, changed, fetch=slow_fetch))
    settings = QSettings(str(tmp_path / "prefs.ini"), QSettings.Format.IniFormat)
    settings.setValue(tp.TOKEN_PERIOD_SETTING, "all")
    panel = tp.TokenUsagePanel(settings, app.DIALOG_STYLE)
    service = tp.TokenUsageService(tmp_path / "ledger.sqlite3", "all", root, sync_prices=True)
    panel.attach_service(service)
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start(10)
    try:
        service.start()
        until(lambda: panel.report is not None and panel.report.responses == 1)
        assert panel.report.priced_responses == 0
        QTest.qWait(130)
        assert len(ticks) >= 8
        release.set()
        until(lambda: panel.report.priced_responses == 1)
        assert panel.report.api_cost_usd == pytest.approx(.000248)
        assert "官方價格" in panel.price_status.full_text
        assert "官方價格" in panel.cost.toolTip()
        assert "未列出的既有模型" in tp.coverage_text(panel.report)
    finally:
        release.set()
        timer.stop()
        service.stop()
        panel.close()


def test_main_version_badge_survives_small_layout(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(app, "APP_DIR", tmp_path)
    monkeypatch.setattr(app, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(app, "CLAUDE_STATE_PATH", tmp_path / "claude.json")
    view = app.UsageWidget(demo=True)
    try:
        view.show()
        view._height_limit = 430
        view.setFixedSize(400, 430)
        view._adapt_layout()
        QTest.qWait(70)
        assert view.version_badge.text() == f"v{app.APP_VERSION}"
        assert view.rect().contains(view.version_badge.mapTo(view, view.version_badge.rect().bottomRight()))
        assert not view.token_panel.price_status.isVisible()
    finally:
        view._force_quit = True
        view.tray.hide()
        view.close()
