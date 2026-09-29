"""驗證更新下載，並以可回復的方式替換安裝檔。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

GITHUB_REPO = "Andy61490963/Quota-PromptDock"
RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASE_DOWNLOAD_PREFIX = f"https://github.com/{GITHUB_REPO}/releases/download/"
RELEASE_ASSET = "QuotaDock-Windows-x64.exe"
MIN_RELEASE_BYTES = 5_000_000
MAX_RELEASE_BYTES = 512 * 1024 * 1024


def _release_tag(url: str) -> str | None:
    if not url.startswith(RELEASE_DOWNLOAD_PREFIX):
        return None
    parts = url[len(RELEASE_DOWNLOAD_PREFIX):].split("/")
    if len(parts) != 2 or parts[1] != RELEASE_ASSET:
        return None
    return parts[0] if re.fullmatch(r"[vV]?\d+\.\d+\.\d+", parts[0]) else None


def parse_release(payload: dict[str, Any]) -> tuple[str, str] | None:
    """只接受本專案正式版本、相符標籤與指定 Windows 附件。"""
    if payload.get("draft") or payload.get("prerelease"):
        return None
    tag = str(payload.get("tag_name") or "").strip()
    assets = payload.get("assets")
    if not isinstance(assets, list):
        return None
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("name") != RELEASE_ASSET:
            continue
        url = str(asset.get("browser_download_url") or "")
        if tag and _release_tag(url) == tag:
            return tag.lstrip("vV"), url
    return None


class UpdateChecker:
    def __init__(self, timeout_seconds: float = 10.0, version: str = "") -> None:
        self.timeout_seconds = timeout_seconds
        self.version = version

    def _request(self, url: str) -> urllib.request.Request:
        return urllib.request.Request(url, headers={
            "User-Agent": f"Quota-PromptDock/{self.version or 'update'}",
            "Accept": "application/vnd.github+json",
        })

    def _metadata(self, url: str) -> dict:
        with urllib.request.urlopen(self._request(url), timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("版本資訊格式錯誤，請稍後重試。")
        return payload

    def latest(self) -> tuple[str, str] | None:
        return parse_release(self._metadata(RELEASE_API))

    def download(self, url: str, destination: Path) -> Path:
        tag = _release_tag(url)
        if not tag:
            raise RuntimeError("下載網址不是本專案的正式版本，已中止。")
        # 依指定標籤重新核對附件；等待使用者點擊期間可能已有其他新版。
        metadata_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/{urllib.parse.quote(tag, safe='')}"
        payload = self._metadata(metadata_url)
        if parse_release(payload) != (tag.lstrip("vV"), url):
            raise RuntimeError("版本附件已變更，請重新檢查更新。")
        asset = next(a for a in payload["assets"] if isinstance(a, dict) and a.get("browser_download_url") == url)
        size, digest = asset.get("size"), asset.get("digest", "")
        if type(size) is not int or not MIN_RELEASE_BYTES <= size <= MAX_RELEASE_BYTES:
            raise RuntimeError("版本附件大小異常，已中止更新。")
        if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
            raise RuntimeError("新版缺少 SHA-256 校驗資料，請從發行頁手動下載。")

        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".quota-download-", suffix=".part", delete=False) as handle:
                temporary = Path(handle.name)
                hasher, received = hashlib.sha256(), 0
                with urllib.request.urlopen(self._request(url), timeout=180) as response:
                    while chunk := response.read(1024 * 1024):
                        received += len(chunk)
                        if received > size:
                            raise RuntimeError("下載大小與版本資訊不符，已中止更新。")
                        hasher.update(chunk)
                        handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if received != size or hasher.hexdigest() != digest[7:].lower():
                raise RuntimeError("更新檔校驗失敗，請重新下載。")
            os.replace(temporary, destination)
            return destination
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _copy_verified(source: Path, target: Path) -> None:
    shutil.copy2(source, target)
    with source.open("rb") as original, target.open("rb+") as copied:
        if hashlib.file_digest(original, "sha256").digest() != hashlib.file_digest(copied, "sha256").digest():
            raise RuntimeError("安裝檔複製校驗失敗。")
        copied.flush()
        os.fsync(copied.fileno())


def install_release(
    source: Path,
    target: Path,
    stop_running: Callable[[], None],
    finish_install: Callable[[], None],
    launch: Callable[[], None],
    *,
    recover: Callable[[], None] | None = None,
    rollback_settings: Callable[[], None] | None = None,
) -> None:
    """先完成暫存與校驗才停止舊版；替換後失敗可還原上一份執行檔。"""
    source, target = Path(source), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = target.with_name(target.name + ".bak")
    staged: Path | None = None
    backup_stage: Path | None = None
    existed = target.is_file()
    replaced = stop_attempted = False
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".quota-install-", suffix=".exe", delete=False) as handle:
            staged = Path(handle.name)
        _copy_verified(source, staged)
        stop_attempted = True
        stop_running()
        if existed:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".quota-backup-", suffix=".exe", delete=False) as handle:
                backup_stage = Path(handle.name)
            _copy_verified(target, backup_stage)
            os.replace(backup_stage, backup)
        os.replace(staged, target)
        replaced = True
        launch()
        finish_install()
    except Exception as exc:
        settings_error = None
        if replaced:
            stop_error = None
            try:
                stop_running()
            except Exception as error:
                stop_error = error
            if rollback_settings is not None:
                try:
                    rollback_settings()
                except Exception as error:
                    settings_error = error
            try:
                if stop_error is not None:
                    raise stop_error
                if existed:
                    os.replace(backup, target)
                else:
                    target.unlink(missing_ok=True)
            except Exception as restore_error:
                message = (f"更新未完成，無法自動還原；上一版備份位於 {backup}。"
                           if existed else "安裝未完成，無法自動移除新版；請結束程式後重新安裝。")
                if settings_error is not None:
                    message += " 捷徑或開機設定也需要重新確認。"
                raise RuntimeError(message) from restore_error
        if stop_attempted and (existed or recover is not None):
            try:
                (recover or launch)()
            except Exception:
                pass
        if settings_error is not None:
            raise RuntimeError("安裝失敗，程式已還原；捷徑或開機設定需要重新確認。") from settings_error
        message = "安裝失敗，已保留原版本；請稍後重試。" if existed else "安裝未完成，請稍後重試。"
        raise RuntimeError(message) from exc
    finally:
        for temporary in (staged, backup_stage):
            if temporary is not None:
                temporary.unlink(missing_ok=True)
