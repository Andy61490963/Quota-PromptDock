"""Verify the packaged stable entry selects real old/new EXEs after a restart."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from unittest.mock import patch

from PyInstaller.archive.readers import CArchiveReader

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launch_entry as le
import windows_install as wi
from tools.verify_packaged_upgrade import stop_test_processes
from tools.check_release_version import app_version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-exe", type=Path, required=True)
    parser.add_argument("--new-exe", type=Path, required=True)
    args = parser.parse_args()
    archive = CArchiveReader(str(args.new_exe.resolve()))
    payload = archive.extract(le.LAUNCHER_NAME)
    with tempfile.TemporaryDirectory(prefix="quota-stable-entry-") as temporary:
        work = Path(temporary).resolve()
        root = work / "固定安裝入口"
        root.mkdir()
        old = root / "versions" / ("1.4.10-" + uuid.uuid4().hex) / "QuotaDock.exe"
        new = root / "versions" / (app_version() + "-" + uuid.uuid4().hex) / "QuotaDock.exe"
        for path, source in ((old, args.old_exe), (new, args.new_exe)):
            path.parent.mkdir(parents=True)
            shutil.copyfile(source, path)
        data, desktop, roaming = work / "資料", work / "桌面", work / "Roaming"
        for path in (data, desktop, roaming):
            path.mkdir()
        pin_folder = roaming / "Microsoft/Internet Explorer/Quick Launch/User Pinned/TaskBar"
        pin_folder.mkdir(parents=True)
        pin = pin_folder / "原本的工作列捷徑.lnk"
        source_entry = work / le.LAUNCHER_NAME
        source_entry.write_bytes(payload)
        env = os.environ.copy()
        for name in (wi.HANDOFF_PATH, wi.HANDOFF_TOKEN, wi.ORIGIN_EXE, wi.ORIGIN_PID,
                     wi.READY_PATH_ENV, wi.READY_TOKEN_ENV):
            env.pop(name, None)
        env.update(APPDATA=str(roaming), QT_QPA_PLATFORM="offscreen",
                   QUOTA_PROMPTDOCK_DATA_DIR=str(data), PYINSTALLER_RESET_ENVIRONMENT="1")

        def run(script):
            return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); " + script],
                check=True, capture_output=True, timeout=25, env=env, creationflags=subprocess.CREATE_NO_WINDOW)

        le.rewrite_shortcut(run, pin, old)
        try:
            with patch.dict(os.environ, env), patch.object(wi, "_desktop_path", lambda _: desktop), \
                    patch.object(wi, "_snapshot_run_values", lambda: {}), \
                    patch.object(wi, "set_frozen_autostart", lambda *a: None):
                settings = wi.InstallSettings(new, False, run, source_entry, (old,))
                settings.apply()
            entry = root / le.LAUNCHER_NAME
            entry_hash = hashlib.sha256(entry.read_bytes()).hexdigest()
            pin_before = pin.read_bytes()
            for label, target in (("前一正式版", old), ("目前新版", new)):
                wi._atomic_bytes(root / "current.json", json.dumps({"executable": str(target.relative_to(root))}).encode())
                ready = work / ("ready-" + uuid.uuid4().hex)
                token = uuid.uuid4().hex
                process = subprocess.Popen([str(entry), "--demo"], env=env |
                    {wi.READY_PATH_ENV: str(ready), wi.READY_TOKEN_ENV: token},
                    cwd=root, creationflags=subprocess.CREATE_NO_WINDOW)
                assert process.wait(timeout=15) == 0
                until = time.monotonic() + 60
                while time.monotonic() < until:
                    if ready.is_file() and ready.read_text(encoding="utf-8") == token:
                        break
                    time.sleep(.1)
                else:
                    raise RuntimeError("固定入口未啟動指定版本：" + label)
                env["QUOTA_EXPECTED_CHILD"] = str(target)
                raw = run("@(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $env:QUOTA_EXPECTED_CHILD } | Select-Object ProcessId) | ConvertTo-Json -Compress").stdout
                assert json.loads(raw.decode("utf-8")), label
                assert pin.read_bytes() == pin_before
                assert hashlib.sha256(entry.read_bytes()).hexdigest() == entry_hash
                stop_test_processes(target, work)
                print("通過：同一固定入口開啟" + label + "，核對實際 EXE 程序與 READY；捷徑與啟動器未更換。", flush=True)
        finally:
            for target in (old, new):
                stop_test_processes(target, work)
    print("範圍：真正打包 EXE／固定入口與原生捷徑遷移；未點擊其他電腦的 Explorer 工作列按鈕。", flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
