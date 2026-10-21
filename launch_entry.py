"""A permanent launcher and narrowly scoped migration of existing Windows links."""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

LAUNCHER_NAME = "QuotaDockLauncher.exe"
LAUNCHER_PROTOCOL = 1


def bundled_launcher() -> Path:
    root = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).parent / "build/launcher"
    path = root / LAUNCHER_NAME
    if not path.is_file():
        raise RuntimeError("安裝檔缺少固定啟動程式，請重新下載最新正式版。")
    return path


def _atomic_write(path: Path, data: bytes):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".quota-entry-", delete=False) as file:
            temporary = Path(file.name)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def valid_launcher(root: Path) -> bool:
    try:
        target = root / LAUNCHER_NAME
        receipt = root / "launcher.json"
        if target.is_symlink() or receipt.is_symlink() or receipt.stat().st_size > 4096:
            return False
        record = json.loads(receipt.read_text(encoding="utf-8"))
        return (type(record["protocol"]) is int and record["protocol"] >= LAUNCHER_PROTOCOL
                and 0 < target.stat().st_size <= 1024 * 1024
                and hashlib.sha256(target.read_bytes()).hexdigest() == record["sha256"])
    except (OSError, ValueError, KeyError, TypeError):
        return False


def autostart_entry(executable: Path) -> Path:
    executable = executable.resolve()
    if executable.parent.parent.name == "versions":
        root = executable.parent.parent.parent
        if valid_launcher(root):
            return root / LAUNCHER_NAME
    return executable


class LauncherInstall:
    def __init__(self, root: Path, source: Path):
        self.root, self.source = root, Path(source)
        self.target, self.receipt = root / LAUNCHER_NAME, root / "launcher.json"
        self.previous = None
        self.changed = False

    def prepare(self):
        if self.previous is None:
            self.previous = {}
            for path in (self.target, self.receipt):
                if path.is_symlink() or (path.exists() and not path.is_file()):
                    raise RuntimeError("固定啟動程式的位置無效，已保留原安裝。")
                if path.exists() and path.stat().st_size > 1024 * 1024:
                    raise RuntimeError("固定啟動程式資料異常，已保留原安裝。")
                self.previous[path] = path.read_bytes() if path.exists() else None

    def apply(self):
        self.prepare()
        if valid_launcher(self.root):
            return  # Do not replace a working launcher during each application update.
        data = self.source.read_bytes()
        if not data.startswith(b"MZ") or not 0 < len(data) <= 1024 * 1024:
            raise RuntimeError("固定啟動程式無效，已中止安裝。")
        self.changed = True
        _atomic_write(self.target, data)
        _atomic_write(self.receipt, json.dumps({"protocol": LAUNCHER_PROTOCOL,
            "sha256": hashlib.sha256(data).hexdigest()}).encode("utf-8"))

    def rollback(self):
        if self.changed:
            for path, data in self.previous.items():
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic_write(path, data)
            self.changed = False


def _encoded(value) -> str:
    return base64.b64encode(json.dumps(value, ensure_ascii=False).encode("utf-16-le")).decode("ascii")


def owned_shortcut_target(value: str, root: Path, aliases) -> bool:
    if not value:
        return False
    target = Path(os.path.expandvars(value)).resolve()
    if target in {Path(p).resolve() for p in aliases} | {root / "QuotaDock.exe", root / LAUNCHER_NAME}:
        return True
    try:
        relative = target.relative_to(root / "versions")
    except ValueError:
        return False
    return (len(relative.parts) == 2 and relative.name == "QuotaDock.exe"
            and re.fullmatch(r"\d+\.\d+\.\d+-[0-9a-f]{32}", relative.parts[0]) is not None)


def find_shortcuts(powershell, folders, root, aliases) -> dict[Path, bytes]:
    folders = [p.resolve() for p in folders if p.is_dir() and not p.is_symlink()]
    if not folders:
        return {}
    encoded = _encoded([str(p) for p in folders])
    result = powershell(
        "$ErrorActionPreference='Stop'; "
        f"$folders=ConvertFrom-Json ([Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('{encoded}'))); "
        "$shell=New-Object -ComObject WScript.Shell; $links=@(foreach($folder in $folders){ "
        "foreach($file in (Get-ChildItem -LiteralPath $folder -File -Filter '*.lnk')){ "
        "try { $link=$shell.CreateShortcut($file.FullName); "
        "[pscustomobject]@{path=$file.FullName; target=$link.TargetPath; arguments=$link.Arguments} "
        "} catch { continue } } }); ConvertTo-Json -InputObject $links -Compress"
    )
    records = json.loads(result.stdout.decode("utf-8-sig"))
    if not isinstance(records, list):
        raise RuntimeError("無法確認既有捷徑，已中止更新。")
    related = {}
    for record in records:
        path = Path(record["path"])
        if (path.parent.resolve() not in folders or path.is_symlink() or path.suffix.lower() != ".lnk"
                or "--demo" in record.get("arguments", "") or "--screenshot" in record.get("arguments", "")):
            continue
        if owned_shortcut_target(record["target"], root, aliases):
            related[path] = path.read_bytes()
    return related


def rewrite_shortcut(powershell, path: Path, target: Path):
    encoded = _encoded({"path": str(path), "target": str(target)})
    powershell(
        "$ErrorActionPreference='Stop'; "
        f"$item=ConvertFrom-Json ([Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('{encoded}'))); "
        "$shell=New-Object -ComObject WScript.Shell; $link=$shell.CreateShortcut($item.path); "
        "$link.TargetPath=$item.target; "
        "$link.WorkingDirectory=Split-Path -Parent $item.target; "
        "$link.Description='AI 額度與常用指令小工具'; $link.IconLocation=$item.target+',0'; $link.Save()"
    )


def notify_shortcuts(paths):
    if os.name != "nt":
        return
    shell = ctypes.WinDLL("shell32")
    shell.SHChangeNotify.argtypes = (ctypes.c_long, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p)
    shell.SHChangeNotify.restype = None
    for path in paths:
        name = ctypes.c_wchar_p(str(path))
        shell.SHChangeNotify(0x2000, 0x2005, ctypes.cast(name, ctypes.c_void_p), None)
