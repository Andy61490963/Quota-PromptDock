"""Unicode Windows shell links through IShellLinkW, independent of the system ACP."""
from __future__ import annotations

import ctypes as c
from pathlib import Path
import uuid


class GUID(c.Structure):
    _fields_ = [("a", c.c_uint32), ("b", c.c_uint16), ("d", c.c_uint16), ("bytes", c.c_ubyte * 8)]

    @classmethod
    def parse(cls, value):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


CLSID_LINK = GUID.parse("00021401-0000-0000-c000-000000000046")
IID_LINK_W = GUID.parse("000214f9-0000-0000-c000-000000000046")
IID_FILE = GUID.parse("0000010b-0000-0000-c000-000000000046")


def _check(result):
    if result < 0:
        raise OSError(result & 0xffffffff, "Windows shell link operation failed")


def _call(pointer, index, types, *arguments):
    table = c.cast(pointer, c.POINTER(c.POINTER(c.c_void_p))).contents
    function = c.WINFUNCTYPE(c.c_long, c.c_void_p, *types)(table[index])
    return function(pointer, *arguments)


class ShellLink:
    def __init__(self):
        self.link, self.file = c.c_void_p(), c.c_void_p()
        self.ole = c.WinDLL("ole32")
        self.ole.CoInitializeEx.argtypes = (c.c_void_p, c.c_uint32)
        self.ole.CoInitializeEx.restype = c.c_long
        result = self.ole.CoInitializeEx(None, 2)
        self.initialized = result in (0, 1)
        if not self.initialized and result & 0xffffffff != 0x80010106:
            _check(result)
        self.ole.CoCreateInstance.argtypes = (c.POINTER(GUID), c.c_void_p, c.c_uint32,
                                             c.POINTER(GUID), c.POINTER(c.c_void_p))
        self.ole.CoCreateInstance.restype = c.c_long
        try:
            _check(self.ole.CoCreateInstance(c.byref(CLSID_LINK), None, 1, c.byref(IID_LINK_W), c.byref(self.link)))
            _check(_call(self.link, 0, (c.POINTER(GUID), c.POINTER(c.c_void_p)), c.byref(IID_FILE), c.byref(self.file)))
        except Exception:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        for pointer in (self.file, self.link):
            if pointer:
                _call(pointer, 2, ())
                pointer.value = None
        if self.initialized:
            self.ole.CoUninitialize()
            self.initialized = False

    def load(self, path):
        _check(_call(self.file, 5, (c.c_wchar_p, c.c_uint32), str(path), 0))

    def read(self):
        target, arguments = c.create_unicode_buffer(32768), c.create_unicode_buffer(32768)
        _check(_call(self.link, 3, (c.c_wchar_p, c.c_int, c.c_void_p, c.c_uint32), target, len(target), None, 4))
        _check(_call(self.link, 10, (c.c_wchar_p, c.c_int), arguments, len(arguments)))
        return target.value, arguments.value

    def text(self, index, value):
        _check(_call(self.link, index, (c.c_wchar_p,), str(value)))

    def save(self, path):
        _check(_call(self.file, 6, (c.c_wchar_p, c.c_int), str(path), 1))


def read_shortcut(path: Path):
    with ShellLink() as link:
        link.load(path)
        return link.read()


def write_shortcut(path: Path, target: Path, *, arguments=None):
    with ShellLink() as link:
        if path.is_file():
            try:
                link.load(path)
            except OSError:
                pass  # A corrupted reserved desktop link can be recreated from its saved backup.
        link.text(20, target)
        link.text(9, target.parent)
        link.text(7, "AI 額度與常用指令小工具")
        if arguments is not None:
            link.text(11, arguments)
        _check(_call(link.link, 17, (c.c_wchar_p, c.c_int), str(target), 0))
        link.save(path)
