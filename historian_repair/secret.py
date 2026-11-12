"""Storage of passwords in the settings file.

Windows: encrypted with DPAPI (CryptProtectData), bound to the current Windows
user account. Other systems: only base64-encoded (NOT encrypted) - the GUI
says so; the settings file lives in the user's home directory.
"""
from __future__ import annotations

import base64
import os


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    inp = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    crypt32 = ctypes.windll.crypt32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI-Aufruf fehlgeschlagen")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def protection_text() -> str:
    return ("verschlüsselt für Ihr Windows-Benutzerkonto (DPAPI)" if os.name == "nt"
            else "nur kodiert, NICHT verschlüsselt")


def protect(pw: str) -> str:
    if not pw:
        return ""
    raw = pw.encode("utf-8")
    if os.name == "nt":
        try:
            return "dpapi:" + base64.b64encode(_dpapi(raw, True)).decode("ascii")
        except Exception:  # noqa: BLE001
            pass
    return "b64:" + base64.b64encode(raw).decode("ascii")


def unprotect(s: str) -> str:
    if not s:
        return ""
    try:
        kind, data = s.split(":", 1)
        raw = base64.b64decode(data)
        if kind == "dpapi":
            raw = _dpapi(raw, False)
        return raw.decode("utf-8")
    except Exception:  # noqa: BLE001
        return ""
