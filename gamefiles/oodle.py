"""Oodle (Kraken/Leviathan/Mermaid) decompression, used by Cyberpunk 2077 archives and
Mass Effect Legendary Edition packages. Oodle is licensed software, so Amalgam doesn't ship it:
it borrows the DLL that the game itself installs (oo2ext_*.dll / oo2core_*.dll)."""

import ctypes
import glob
import os

_lib = None
_search_dirs = []


def add_search_dirs(dirs):
    for d in dirs:
        if d and d not in _search_dirs:
            _search_dirs.append(d)


def _find_dll():
    patterns = ("oo2ext_*_win64.dll", "oo2core_*_win64.dll")
    for d in _search_dirs:
        for depth in ("", "*", os.path.join("*", "*"), os.path.join("*", "*", "*"), os.path.join("*", "*", "*", "*")):
            for pat in patterns:
                hits = glob.glob(os.path.join(d, depth, pat))
                if hits:
                    return sorted(hits)[-1]
    return None


def load():
    global _lib
    if _lib:
        return _lib
    if os.name != "nt":
        raise RuntimeError("Oodle decompression only works on Windows (it uses the game's own DLL).")
    path = _find_dll()
    if not path:
        raise RuntimeError("This file is Oodle-compressed. Amalgam uses the Oodle DLL that comes with the game, "
                           "but couldn't find one: add the game's install folder under Game folders in the Apps card.")
    lib = ctypes.WinDLL(path)
    fn = lib.OodleLZ_Decompress
    fn.restype = ctypes.c_int64
    fn.argtypes = [ctypes.c_void_p, ctypes.c_int64, ctypes.c_void_p, ctypes.c_int64, ctypes.c_int, ctypes.c_int,
                   ctypes.c_int, ctypes.c_void_p, ctypes.c_int64, ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_int64, ctypes.c_int]
    _lib = fn
    return fn


def decompress(src, size):
    fn = load()
    out = ctypes.create_string_buffer(size)
    srcbuf = ctypes.create_string_buffer(bytes(src), len(src))
    n = fn(srcbuf, len(src), out, size, 1, 0, 0, None, 0, None, None, None, 0, 3)
    if n <= 0:
        raise RuntimeError("Oodle couldn't decompress this data.")
    return out.raw[:n]
