"""以檔案屬性與 Restart Manager 查詢安裝失敗線索，不關閉程序或修改檔案。"""
from __future__ import annotations

import ctypes
import os
import stat
from ctypes import wintypes
from pathlib import Path


class _RM_UNIQUE_PROCESS(ctypes.Structure):
    _fields_ = [("dwProcessId", wintypes.DWORD), ("ProcessStartTime", wintypes.FILETIME)]


class _RM_PROCESS_INFO(ctypes.Structure):
    _fields_ = [
        ("Process", _RM_UNIQUE_PROCESS),
        ("strAppName", wintypes.WCHAR * 256),
        ("strServiceShortName", wintypes.WCHAR * 64),
        ("ApplicationType", ctypes.c_int),
        ("AppStatus", wintypes.ULONG),
        ("TSSessionId", wintypes.DWORD),
        ("bRestartable", wintypes.BOOL),
    ]


def _short_text(value: object, limit: int = 160) -> str:
    return " ".join(str(value).split())[:limit]


def _readonly_description(path: Path) -> str:
    try:
        attributes = path.stat().st_file_attributes
        return "是" if attributes & stat.FILE_ATTRIBUTE_READONLY else "否"
    except FileNotFoundError:
        return "檔案不存在"
    except Exception as exc:
        return f"無法判定（{_short_text(exc)}）"


def _restart_manager() -> object:
    # 僅載入系統 DLL；只宣告查詢所需的四個 API。
    api = ctypes.WinDLL("Rstrtmgr.dll", winmode=0x00000800)
    api.RmStartSession.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
                                   wintypes.LPWSTR]
    api.RmRegisterResources.argtypes = [
        wintypes.DWORD, wintypes.UINT, ctypes.POINTER(wintypes.LPCWSTR),
        wintypes.UINT, ctypes.POINTER(_RM_UNIQUE_PROCESS),
        wintypes.UINT, ctypes.POINTER(wintypes.LPCWSTR),
    ]
    api.RmGetList.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.UINT),
                             ctypes.POINTER(wintypes.UINT), ctypes.POINTER(_RM_PROCESS_INFO),
                             ctypes.POINTER(wintypes.DWORD)]
    api.RmEndSession.argtypes = [wintypes.DWORD]
    for name in ("RmStartSession", "RmRegisterResources", "RmGetList", "RmEndSession"):
        getattr(api, name).restype = wintypes.DWORD
    return api


def _check_result(result: int, operation: str) -> None:
    if result:
        raise OSError(f"{operation} 錯誤碼 {result}")


def _query_file_users(source: Path, target: Path) -> list[tuple[str, int]]:
    api = _restart_manager()
    handle = wintypes.DWORD()
    key = ctypes.create_unicode_buffer(33)
    _check_result(api.RmStartSession(ctypes.byref(handle), 0, key), "RmStartSession")
    try:
        paths = list(dict.fromkeys((str(source.absolute()), str(target.absolute()))))
        filenames = (wintypes.LPCWSTR * len(paths))(*paths)
        _check_result(api.RmRegisterResources(handle, len(paths), filenames, 0, None, 0, None),
                      "RmRegisterResources")
        capacity = 0
        # 程序清單可能在兩次查詢之間增長；限制次數與配置大小，避免無限重試。
        for _ in range(3):
            needed = wintypes.UINT()
            count = wintypes.UINT(capacity)
            reasons = wintypes.DWORD()
            entries = (_RM_PROCESS_INFO * capacity)() if capacity else None
            result = api.RmGetList(handle, ctypes.byref(needed), ctypes.byref(count),
                                   entries, ctypes.byref(reasons))
            if result == 234:
                if not 0 < needed.value <= 1024:
                    raise OSError("占用程序數量超出診斷上限")
                capacity = needed.value
                continue
            _check_result(result, "RmGetList")
            if not entries:
                return []
            return [(entry.strAppName or entry.strServiceShortName or "未提供名稱",
                     int(entry.Process.dwProcessId))
                    for entry in entries[:min(count.value, capacity)]]
        raise OSError("占用程序清單持續變動")
    finally:
        # 診斷失敗不應遮蔽原始的安裝錯誤。
        try:
            api.RmEndSession(handle)
        except Exception:
            pass


def describe_file_access(source: Path, target: Path) -> str:
    """提供有限的唯讀診斷；未列出程序並不表示不存在鎖定或權限問題。"""
    try:
        if os.name != "nt":
            return "檔案存取診斷：僅支援 Windows，無法判定占用狀態。"
        lines = [f"暫存檔唯讀：{_readonly_description(source)}；"
                 f"目標檔唯讀：{_readonly_description(target)}。"]
        try:
            users = _query_file_users(source, target)
            if users:
                names = "、".join(f"{_short_text(name, 100)}（PID {pid}）"
                                 for name, pid in users[:5])
                suffix = f"，另有 {len(users) - 5} 個程序" if len(users) > 5 else ""
                lines.append(f"Restart Manager 列出的相關程序：{names}{suffix}。")
                lines.append("此清單是查詢當下的線索，無法單獨判定存取遭拒的原因。")
            else:
                lines.append("Restart Manager 未列出相關程序；仍無法判定檔案是否被占用。")
        except Exception as exc:
            lines.append(f"占用查詢失敗，無法判定（{_short_text(exc)}）。")
        return "\n".join(lines)
    except Exception:
        return "檔案存取診斷失敗，無法判定占用狀態。"
