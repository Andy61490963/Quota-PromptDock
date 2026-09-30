"""驗證更新下載，並以可回復的方式替換安裝檔。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
import os
import re
import shutil
import tempfile
import subprocess
import traceback
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


def install_failure_message(stage: str, cause: Exception) -> str:
    """介面保留失敗步驟與可讀原因，完整輔助程序輸出另存本機紀錄。"""
    if isinstance(cause, subprocess.CalledProcessError):
        reason = f"Windows 安裝輔助程序失敗（結束碼 {cause.returncode}）。"
    elif isinstance(cause, subprocess.TimeoutExpired):
        reason = "Windows 安裝輔助程序回應逾時。"
    else:
        reason = str(cause).strip() or type(cause).__name__
    return f"\n失敗步驟：{stage}\n原因：{reason[:400]}"


def record_install_failure(error: Exception, directory: Path, version: str) -> Path | None:
    """只保存安裝例外，不保存環境變數、憑證、子程序命令或常用指令及用量。"""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "install.log"
        details = [datetime.now(timezone.utc).isoformat(), f"安裝版本：{version}"]
        current: BaseException | None = error
        visited = set()
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            # 不使用 format_exception：CalledProcessError 的字串會包含完整子程序命令。
            details.append("".join(traceback.format_tb(current.__traceback__)))
            if isinstance(current, (subprocess.CalledProcessError, subprocess.TimeoutExpired)):
                result = (f"結束碼 {current.returncode}" if isinstance(current, subprocess.CalledProcessError)
                          else f"逾時 {current.timeout} 秒")
                details.append(f"{type(current).__name__}: {result}")
                stderr = current.stderr
                if stderr:
                    if isinstance(stderr, bytes):
                        stderr = stderr.decode("utf-8", errors="replace")
                    details.append("安裝輔助程序錯誤輸出：\n" + str(stderr)[:8192])
            else:
                details.append(f"{type(current).__name__}: {current}")
            current = current.__cause__ or current.__context__
        if path.exists() and path.stat().st_size >= 128 * 1024:
            os.replace(path, directory / "install.previous.log")
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(details)[-32 * 1024:] + "\n\n")
        return path
    except OSError:
        return None


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
    backup = target.with_name(target.name + ".bak")
    staged: Path | None = None
    backup_stage: Path | None = None
    existed = target.is_file()
    replaced = stop_attempted = False
    stage = "準備安裝目錄"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        stage = "暫存並校驗新版"
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".quota-install-", suffix=".exe", delete=False) as handle:
            staged = Path(handle.name)
        _copy_verified(source, staged)
        stage = "等待舊版結束"
        stop_attempted = True
        stop_running()
        if existed:
            stage = "備份原版本"
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".quota-backup-", suffix=".exe", delete=False) as handle:
                backup_stage = Path(handle.name)
            _copy_verified(target, backup_stage)
            os.replace(backup_stage, backup)
        stage = "替換程式"
        os.replace(staged, target)
        replaced = True
        stage = "啟動新版"
        launch()
        stage = "更新捷徑與開機設定"
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
                raise RuntimeError(message + install_failure_message(stage, exc)
                                   + install_failure_message("還原程式", restore_error)) from restore_error
        if stop_attempted and (existed or recover is not None):
            try:
                (recover or launch)()
            except Exception:
                pass
        if settings_error is not None:
            raise RuntimeError("安裝失敗，程式已還原；捷徑或開機設定需要重新確認。"
                               + install_failure_message(stage, exc)
                               + install_failure_message("還原安裝設定", settings_error)) from settings_error
        message = "安裝失敗，已保留原版本；請稍後重試。" if existed else "安裝未完成，請稍後重試。"
        raise RuntimeError(message + install_failure_message(stage, exc)) from exc
    finally:
        for temporary in (staged, backup_stage):
            if temporary is not None:
                temporary.unlink(missing_ok=True)
