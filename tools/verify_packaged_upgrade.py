"""使用真正的新舊 Windows 執行檔，隔離驗證升級與失敗回復。"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import windows_install as installer
from install_handoff import HANDOFF_PATH, HANDOFF_TOKEN, ORIGIN_EXE, ORIGIN_PID


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def stop_test_processes(target: Path, workspace: Path) -> None:
    """只清理本案暫存目錄內、完整執行檔路徑相符的測試程序。"""
    target = target.resolve()
    target.relative_to(workspace.resolve())
    environment = os.environ | {"QUOTADOCK_TEST_TARGET": str(target)}
    script = (
        "$ErrorActionPreference='Stop'; $target=$env:QUOTADOCK_TEST_TARGET; "
        "$deadline=[DateTime]::UtcNow.AddSeconds(10); "
        "do { $items=@(Get-CimInstance Win32_Process | Where-Object {$_.ExecutablePath -eq $target}); "
        "foreach($item in $items){ try { Stop-Process -Id $item.ProcessId -Force -ErrorAction Stop } "
        "catch { if(Get-Process -Id $item.ProcessId -ErrorAction SilentlyContinue){ throw } } }; "
        "if($items.Count){ Start-Sleep -Milliseconds 100 }; "
        "if([DateTime]::UtcNow -gt $deadline){ throw '測試程序未能結束' } "
        "} while($items.Count)"
    )
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                   env=environment, check=True, timeout=25, capture_output=True,
                   creationflags=subprocess.CREATE_NO_WINDOW)


def verify_case(old_exe: Path, new_exe: Path, *, rollback: bool) -> None:
    old_hash, new_hash = digest(old_exe), digest(new_exe)
    with tempfile.TemporaryDirectory(prefix="quota-packaged-upgrade-") as temporary:
        workspace = Path(temporary).resolve()
        target = workspace / "安裝目錄" / "QuotaDock.exe"
        target.parent.mkdir()
        shutil.copyfile(old_exe, target)
        data = workspace / "使用者資料"
        data.mkdir()
        sentinel = data / "既有資料.txt"
        sentinel.write_bytes("原有指令與使用者資料必須保留".encode("utf-8"))
        sentinel_before = sentinel.read_bytes()
        settings_marker = workspace / "隔離安裝設定.txt"
        settings_marker.write_bytes(b"old-settings")
        environment = os.environ.copy()
        for name in (HANDOFF_PATH, HANDOFF_TOKEN, ORIGIN_EXE, ORIGIN_PID,
                     installer.READY_PATH_ENV, installer.READY_TOKEN_ENV):
            environment.pop(name, None)
        environment.update(QT_QPA_PLATFORM="offscreen", QUOTA_PROMPTDOCK_DATA_DIR=str(data),
                           PYINSTALLER_RESET_ENVIRONMENT="1")
        real_popen = subprocess.Popen
        processes = []
        launches = []
        initial_ready = workspace / "舊版啟動訊號"
        initial_token = uuid.uuid4().hex

        class IsolatedSettings:
            """捷徑與 HKCU 設定改用本案標記檔，其餘安裝流程保持真實。"""

            def __init__(self, *_):
                self.previous = None

            def prepare(self):
                self.previous = settings_marker.read_bytes()

            def apply(self):
                settings_marker.write_bytes(b"new-settings")
                if rollback:
                    raise RuntimeError("隔離測試：模擬安裝設定失敗")

            def rollback(self):
                settings_marker.write_bytes(self.previous)

        def demo_popen(args, **kwargs):
            # subprocess.run 的 PowerShell 子程序仍使用原本參數。
            if Path(args[0]).resolve() == target:
                args = [*args, "--demo"]
                launch_environment = kwargs["env"].copy()
                if installer.READY_PATH_ENV not in launch_environment:
                    launch_environment[installer.READY_PATH_ENV] = str(workspace / "回復啟動訊號")
                    launch_environment[installer.READY_TOKEN_ENV] = uuid.uuid4().hex
                kwargs["env"] = launch_environment
                launched_hash = digest(target)
                process = real_popen(args, **kwargs)
                processes.append(process)
                launches.append((process, launched_hash, launch_environment))
                return process
            return real_popen(args, **kwargs)

        try:
            initial = real_popen([str(target), "--demo"], cwd=target.parent,
                                 env=environment | {installer.READY_PATH_ENV: str(initial_ready),
                                                    installer.READY_TOKEN_ENV: initial_token},
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            processes.append(initial)
            installer.wait_for_startup(initial, initial_ready, initial_token, timeout=60)
            with patch.dict(os.environ, environment, clear=True), \
                    patch.object(installer, "InstallSettings", IsolatedSettings), \
                    patch.object(subprocess, "Popen", demo_popen):
                if rollback:
                    try:
                        installer.install_windows_release(new_exe, target, False)
                    except RuntimeError as exc:
                        if "隔離測試：模擬安裝設定失敗" not in str(exc):
                            raise
                    else:
                        raise AssertionError("設定失敗卻未回報安裝錯誤")
                else:
                    installer.install_windows_release(new_exe, target, False)

            assert initial.poll() is not None, "原本執行中的舊版未結束"
            assert launches and launches[0][1] == new_hash, "沒有啟動真正的新版執行檔"
            assert sentinel.read_bytes() == sentinel_before, "既有資料遭到變更"
            if rollback:
                assert digest(target) == old_hash, "回復後的執行檔不是舊版"
                assert settings_marker.read_bytes() == b"old-settings", "安裝設定未回復"
                assert len(launches) >= 2 and launches[-1][1] == old_hash, "沒有重新開啟舊版"
                recovered, _, recovered_environment = launches[-1]
                installer.wait_for_startup(recovered, Path(recovered_environment[installer.READY_PATH_ENV]),
                                           recovered_environment[installer.READY_TOKEN_ENV], timeout=60)
                assert launches[0][0].poll() is not None, "失敗的新版仍在執行"
                print("通過：新版啟動後設定失敗，已還原舊 EXE、重開舊版並保留資料。", flush=True)
            else:
                assert digest(target) == new_hash, "安裝結果不符合新版 SHA-256"
                assert digest(target.with_name(target.name + ".bak")) == old_hash, "備份不符合舊版 SHA-256"
                assert settings_marker.read_bytes() == b"new-settings", "安裝設定未完成"
                assert launches[-1][0].poll() is None, "新版啟動後異常結束"
                print("通過：停止真正的舊 EXE、校驗並替換新版、收到新版 READY，備份與資料正確。", flush=True)
        finally:
            stop_test_processes(target, workspace)
            for process in processes:
                process.wait(timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-exe", type=Path, required=True, help="前一正式版 EXE")
    parser.add_argument("--new-exe", type=Path, required=True, help="待發布新版 EXE")
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("此驗證必須在 Windows 執行。")
    old_exe, new_exe = args.old_exe.resolve(), args.new_exe.resolve()
    if not old_exe.is_file() or not new_exe.is_file():
        raise SystemExit("缺少新舊執行檔，發布驗證不可略過。")
    if digest(old_exe) == digest(new_exe):
        raise SystemExit("新舊執行檔完全相同，無法驗證跨版本升級。")
    print("真實驗證：新舊 PyInstaller EXE、Windows CIM 停止程序、安裝服務、檔案替換與 READY。", flush=True)
    print("隔離替身：展示資料、無視窗 Qt、暫存資料目錄；捷徑與 HKCU 設定以暫存標記檔代替。", flush=True)
    print("範圍限制：不操作真實帳號、既有 App、桌面捷徑或開機啟動設定；未驗證單例及 UI 點擊更新。", flush=True)
    verify_case(old_exe, new_exe, rollback=False)
    verify_case(old_exe, new_exe, rollback=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    main()
