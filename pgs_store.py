# pgs_store.py - one encrypted settings file (Windows DPAPI). No plain-text files.
import os, json, time, ctypes, threading
from ctypes import wintypes

_PATH = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "PGS_Tool", "settings.dat")
_lock = threading.RLock()
_data = None

class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

def _crypt(raw: bytes, protect: bool) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]

    buf = ctypes.create_string_buffer(raw, len(raw))
    blob_in = _BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    fn.argtypes = [
        ctypes.POINTER(_BLOB), ctypes.c_void_p, ctypes.POINTER(_BLOB),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_BLOB),
    ]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("Windows could not encrypt/decrypt the settings")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)

def _load():
    global _data
    if _data is None:
        try:
            with open(_PATH, "rb") as f:
                _data = json.loads(_crypt(f.read(), False).decode("utf-8"))
                if not isinstance(_data, dict):
                    _data = {}
        except Exception:
            _data = {}
    return _data

def _save():
    os.makedirs(os.path.dirname(_PATH), exist_ok=True)
    blob = _crypt(json.dumps(_data, ensure_ascii=False).encode("utf-8"), True)
    tmp = _PATH + ".tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, _PATH)

def get(key, default=None):
    with _lock:
        return _load().get(key, default)

def set(key, value):
    with _lock:
        _load()[key] = value
        _save()

def delete(key):
    with _lock:
        if _load().pop(key, None) is not None:
            _save()

def append_log(name, text, keep=200):          # used by updater.py
    with _lock:
        lines = _load().setdefault("logs", {}).setdefault(name, [])
        lines.append(time.strftime("%Y-%m-%d %H:%M:%S ") + str(text))
        del lines[:-keep]
        _save()