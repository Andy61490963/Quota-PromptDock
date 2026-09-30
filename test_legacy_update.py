"""舊版單例服務延遲退出時，安裝器須在有界時間內重試或回復。"""
from __future__ import annotations

import itertools
from pathlib import Path
from types import SimpleNamespace

import pytest

import windows_install as wi


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _isolated_install(tmp_path, monkeypatch, outcomes, *, popen_seconds=0.0):
    """保留實際檔案替換與 READY 等待，只攔截系統設定及程序邊界。"""
    source = tmp_path / "download.exe"
    source.write_bytes(b"new-v145")
    target = tmp_path / "installed" / "QuotaDock.exe"
    target.parent.mkdir()
    target.write_bytes(b"old-v144")
    clock = _Clock()
    events = {"settings": [], "stops": 0, "launches": [], "recovery": []}
    outcomes = iter(outcomes)

    class FakeSettings:
        def __init__(self, *args):
            pass

        def prepare(self):
            events["settings"].append("prepare")

        def apply(self):
            events["settings"].append("apply")

        def rollback(self):
            events["settings"].append("rollback")

    class FakeProcess:
        def __init__(self, outcome, environment):
            self.outcome = outcome
            self.environment = environment

        def poll(self):
            if self.outcome in ("ready", "ready_then_zero"):
                Path(self.environment[wi.READY_PATH_ENV]).write_text(
                    self.environment[wi.READY_TOKEN_ENV], encoding="utf-8"
                )
            if self.outcome in ("ready", "alive_no_ready"):
                return None
            if self.outcome == "ready_then_zero":
                return 0
            return self.outcome

    def fake_run(args, **kwargs):
        assert "Get-CimInstance Win32_Process" in args[-1]
        events["stops"] += 1
        return SimpleNamespace(stdout=b"")

    def fake_popen(args, **kwargs):
        version = Path(args[0]).read_bytes()
        environment = kwargs["env"]
        if wi.READY_PATH_ENV in environment:
            assert version == b"new-v145"
            clock.sleep(popen_seconds)
            events["launches"].append({
                "token": environment[wi.READY_TOKEN_ENV],
                "ready_path": Path(environment[wi.READY_PATH_ENV]),
            })
            return FakeProcess(next(outcomes), environment)
        events["recovery"].append(version)
        return FakeProcess("ready", environment)

    monkeypatch.setattr(wi.UpdateOrigin, "from_environment", lambda: None)
    monkeypatch.setattr(wi, "InstallSettings", FakeSettings)
    monkeypatch.setattr(wi.subprocess, "run", fake_run)
    monkeypatch.setattr(wi.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(wi, "time", clock)
    return source, target, events, clock


def test_legacy_single_instance_zero_exit_then_new_version_ready(tmp_path, monkeypatch):
    source, target, events, clock = _isolated_install(
        tmp_path, monkeypatch, [0, "ready"]
    )

    wi.install_windows_release(source, target, False)

    assert target.read_bytes() == b"new-v145"
    assert target.with_name(target.name + ".bak").read_bytes() == b"old-v144"
    assert events["settings"] == ["prepare", "apply"]
    assert events["recovery"] == []
    assert len(events["launches"]) == 2
    assert events["launches"][0]["token"] != events["launches"][1]["token"]
    assert events["launches"][0]["ready_path"] == events["launches"][1]["ready_path"]
    assert clock.now == pytest.approx(0.5)


def test_nonzero_exit_does_not_retry_and_recovers_old_version(tmp_path, monkeypatch):
    source, target, events, clock = _isolated_install(tmp_path, monkeypatch, [23])

    with pytest.raises(RuntimeError, match="啟動新版") as failure:
        wi.install_windows_release(source, target, False)

    assert isinstance(failure.value.__cause__, wi.StartupExitedError)
    assert failure.value.__cause__.exit_code == 23
    assert target.read_bytes() == b"old-v144"
    assert events["settings"] == ["prepare", "rollback"]
    assert len(events["launches"]) == 1
    assert events["recovery"] == [b"old-v144"]
    assert clock.now == 0


def test_continuous_zero_exit_respects_one_total_deadline_and_rolls_back(tmp_path, monkeypatch):
    source, target, events, clock = _isolated_install(
        tmp_path, monkeypatch, itertools.repeat(0)
    )
    monkeypatch.setattr(wi, "STARTUP_TIMEOUT_SECONDS", 3.0)

    with pytest.raises(RuntimeError, match="等待舊程式結束逾時"):
        wi.install_windows_release(source, target, False)

    assert clock.now == pytest.approx(3.0)
    assert len(events["launches"]) == 3
    assert target.read_bytes() == b"old-v144"
    assert events["settings"] == ["prepare", "rollback"]
    assert events["recovery"] == [b"old-v144"]


def test_ready_then_zero_exit_is_not_mistaken_for_legacy_single_instance(tmp_path, monkeypatch):
    source, target, events, clock = _isolated_install(
        tmp_path, monkeypatch, ["ready_then_zero"]
    )

    with pytest.raises(RuntimeError, match="啟動新版") as failure:
        wi.install_windows_release(source, target, False)

    assert isinstance(failure.value.__cause__, wi.StartupExitedError)
    assert failure.value.__cause__.exit_code == 0
    assert len(events["launches"]) == 1
    assert target.read_bytes() == b"old-v144"
    assert events["settings"] == ["prepare", "rollback"]
    assert events["recovery"] == [b"old-v144"]
    assert clock.now == 0


def test_popen_time_is_included_in_the_single_startup_deadline(tmp_path, monkeypatch):
    source, target, events, clock = _isolated_install(
        tmp_path, monkeypatch, ["alive_no_ready"], popen_seconds=2.4
    )
    monkeypatch.setattr(wi, "STARTUP_TIMEOUT_SECONDS", 3.0)

    with pytest.raises(RuntimeError, match="新版啟動逾時"):
        wi.install_windows_release(source, target, False)

    assert 3.0 <= clock.now <= 3.05
    assert len(events["launches"]) == 1
    assert target.read_bytes() == b"old-v144"
    assert events["settings"] == ["prepare", "rollback"]
    assert events["recovery"] == [b"old-v144"]


def test_live_process_without_ready_times_out_without_second_launch(tmp_path, monkeypatch):
    source, target, events, clock = _isolated_install(
        tmp_path, monkeypatch, ["alive_no_ready"]
    )
    monkeypatch.setattr(wi, "STARTUP_TIMEOUT_SECONDS", 3.0)

    with pytest.raises(RuntimeError, match="新版啟動逾時"):
        wi.install_windows_release(source, target, False)

    assert 3.0 <= clock.now <= 3.05
    assert len(events["launches"]) == 1
    assert target.read_bytes() == b"old-v144"
    assert events["settings"] == ["prepare", "rollback"]
    assert events["recovery"] == [b"old-v144"]
