"""驗證下載損毀、安裝中斷與回復，全部使用隔離檔案及網路替身。"""
import hashlib
import io
import json
from pathlib import Path

import pytest

import updates
from updates import RELEASE_ASSET, RELEASE_DOWNLOAD_PREFIX, UpdateChecker, install_release, parse_release

URL = RELEASE_DOWNLOAD_PREFIX + "v1.4.5/" + RELEASE_ASSET
DATA = b"MZ" + b"x" * 5_000_000


def metadata(data=DATA, **changes):
    asset = {"name": RELEASE_ASSET, "browser_download_url": URL, "size": len(data),
             "digest": "sha256:" + hashlib.sha256(data).hexdigest()}
    asset.update(changes)
    return {"tag_name": "v1.4.5", "draft": False, "prerelease": False, "assets": [asset]}


def fake_download(monkeypatch, payload=None, data=DATA):
    def open_url(request, timeout):
        if "api.github.com" in request.full_url:
            return io.BytesIO(json.dumps(payload or metadata()).encode())
        return io.BytesIO(data)
    monkeypatch.setattr(updates.urllib.request, "urlopen", open_url)


def test_download_verifies_github_size_and_digest_before_replacing(tmp_path, monkeypatch):
    fake_download(monkeypatch)
    target = tmp_path / "update.exe"
    target.write_bytes(b"previous-download")
    assert UpdateChecker().download(URL, target) == target
    assert target.read_bytes() == DATA
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("data", [DATA[:-500], DATA + b"extra", b"bad" + DATA[3:]],
                         ids=["truncated", "oversized", "corrupt"])
def test_bad_download_keeps_existing_file_and_removes_partial(tmp_path, monkeypatch, data):
    fake_download(monkeypatch, data=data)
    target = tmp_path / "update.exe"
    target.write_bytes(b"previous-download")
    with pytest.raises(RuntimeError):
        UpdateChecker().download(URL, target)
    assert target.read_bytes() == b"previous-download"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("changes", [{"digest": None}, {"digest": ""}, {"size": 10}, {"size": True}])
def test_unverifiable_asset_is_not_downloaded(tmp_path, monkeypatch, changes):
    fake_download(monkeypatch, metadata(**changes))
    with pytest.raises(RuntimeError):
        UpdateChecker().download(URL, tmp_path / "update.exe")
    assert not list(tmp_path.iterdir())


def test_download_rejects_external_or_mismatched_release(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError):
        UpdateChecker().download("https://example.invalid/update.exe", tmp_path / "update.exe")
    payload = metadata()
    payload["tag_name"] = "v1.4.6"
    fake_download(monkeypatch, payload)
    with pytest.raises(RuntimeError, match="版本附件已變更"):
        UpdateChecker().download(URL, tmp_path / "update.exe")
    assert parse_release(payload) is None
    assert parse_release({"assets": [None, "invalid"]}) is None


@pytest.fixture
def installation(tmp_path):
    source, target = tmp_path / "download.exe", tmp_path / "installed" / "QuotaDock.exe"
    source.write_bytes(b"new-version")
    target.parent.mkdir()
    target.write_bytes(b"old-version")
    return source, target


def test_install_keeps_backup_and_only_stops_after_valid_staging(installation):
    source, target = installation
    events = []
    def stop():
        assert target.read_bytes() == b"old-version"
        events.append("stop")
    def finish():
        assert target.read_bytes() == b"new-version"
        events.append("finish")
    install_release(source, target, stop, finish, lambda: events.append("launch"))
    assert events == ["stop", "launch", "finish"]
    assert target.with_name("QuotaDock.exe.bak").read_bytes() == b"old-version"
    assert not list(target.parent.glob(".quota-*"))


def test_copy_interruption_does_not_touch_running_version(installation, monkeypatch):
    source, target = installation
    events = []
    def interrupted_copy(src, dst):
        Path(dst).write_bytes(b"partial")
        raise OSError("模擬磁碟空間不足")
    monkeypatch.setattr(updates.shutil, "copy2", interrupted_copy)
    with pytest.raises(RuntimeError, match="保留原版本"):
        install_release(source, target, lambda: events.append("stop"), lambda: None, lambda: events.append("launch"))
    assert target.read_bytes() == b"old-version"
    assert events == []
    assert not list(target.parent.glob(".quota-*"))


def test_stop_failure_leaves_installed_bytes_untouched(installation):
    source, target = installation
    def stop():
        raise OSError("模擬無法停止舊程序")
    with pytest.raises(RuntimeError, match="保留原版本"):
        install_release(source, target, stop, lambda: None, lambda: None)
    assert target.read_bytes() == b"old-version"


@pytest.mark.parametrize("failure", ["replace", "finish", "launch"])
def test_failed_install_restores_and_restarts_old_version(installation, monkeypatch, failure):
    source, target = installation
    launches = []
    replace = updates.os.replace
    if failure == "replace":
        def fail_replace(src, dst):
            if Path(dst) == target:
                raise OSError("模擬防毒鎖定執行檔")
            return replace(src, dst)
        monkeypatch.setattr(updates.os, "replace", fail_replace)
    def finish():
        if failure == "finish":
            raise OSError("模擬捷徑建立失敗")
    def launch():
        version = target.read_bytes()
        launches.append(version)
        if failure == "launch" and version == b"new-version":
            raise OSError("模擬新版無法啟動")
    with pytest.raises(RuntimeError, match="保留原版本"):
        install_release(source, target, lambda: None, finish, launch)
    assert target.read_bytes() == b"old-version"
    assert launches[-1] == b"old-version"
    assert not list(target.parent.glob(".quota-*"))


def test_failed_first_install_does_not_leave_broken_target(tmp_path):
    source, target = tmp_path / "download.exe", tmp_path / "installed" / "QuotaDock.exe"
    source.write_bytes(b"new-version")
    def fail():
        raise OSError("模擬安裝失敗")
    with pytest.raises(RuntimeError, match="安裝未完成"):
        install_release(source, target, lambda: None, fail, lambda: None)
    assert not target.exists()


def test_failed_stop_during_rollback_still_restores_settings(installation):
    source, target = installation
    events = []
    def stop():
        events.append("stop")
        if events.count("stop") == 2:
            raise OSError("模擬新版程序無法結束")
    def finish():
        raise OSError("模擬捷徑建立失敗")
    with pytest.raises(RuntimeError, match="上一版備份位於"):
        install_release(source, target, stop, finish, lambda: None,
                        rollback_settings=lambda: events.append("rollback_settings"))
    assert events == ["stop", "stop", "rollback_settings"]
    assert target.with_name("QuotaDock.exe.bak").read_bytes() == b"old-version"
