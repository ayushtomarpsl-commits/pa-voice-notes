"""Small Windows helpers (ctypes only, no admin rights needed):
battery status, single-instance lock, low CPU priority, DPAPI secret storage, and one hidden
window thread that receives the global pause hotkey and sleep/resume/lock events."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import threading
from typing import Callable

log = logging.getLogger("pa.win")
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
u32 = ctypes.WinDLL("user32", use_last_error=True)
crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)


# ------------------------------------------------------------- power
class _SPS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", wt.DWORD), ("BatteryFullLifeTime", wt.DWORD)]


def power_status() -> dict:
    s = _SPS()
    if not k32.GetSystemPowerStatus(ctypes.byref(s)):
        return {"on_ac": True, "percent": None}
    return {"on_ac": s.ACLineStatus != 0,  # 1 = AC, 255 = unknown -> treat as AC
            "percent": None if s.BatteryLifePercent == 255 else int(s.BatteryLifePercent),
            "battery_saver": s.SystemStatusFlag == 1}


# ------------------------------------------------------------- process
k32.CreateMutexW.restype = wt.HANDLE
k32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]


def single_instance(name: str = r"Local\PA-personal-assistant") -> object | None:
    """Returns a handle to keep, or None when another copy is already running."""
    h = k32.CreateMutexW(None, False, name)
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        k32.CloseHandle(h)
        return None
    return h


def set_low_priority() -> None:
    BELOW_NORMAL_PRIORITY_CLASS = 0x4000
    k32.SetPriorityClass(k32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS)


# ------------------------------------------------------------- DPAPI (encrypt for this Windows user)
class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(b: bytes) -> _BLOB:
    buf = ctypes.create_string_buffer(b, len(b))
    return _BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _take(blob: _BLOB) -> bytes:
    data = ctypes.string_at(blob.pbData, blob.cbData)
    k32.LocalFree(blob.pbData)
    return data


def dpapi_encrypt(data: bytes) -> bytes:
    out = _BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(_blob(data)), "PA", None, None, None, 0, ctypes.byref(out)):
        raise ctypes.WinError(ctypes.get_last_error())
    return _take(out)


def dpapi_decrypt(data: bytes) -> bytes:
    out = _BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(_blob(data)), None, None, None, None, 0, ctypes.byref(out)):
        raise ctypes.WinError(ctypes.get_last_error())
    return _take(out)


# ------------------------------------------------------------- hotkey + power/session events
WM_HOTKEY, WM_POWERBROADCAST, WM_WTSSESSION_CHANGE = 0x0312, 0x0218, 0x02B1
WM_CLOSE, WM_DESTROY = 0x0010, 0x0002
PBT_APMSUSPEND, PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC, PBT_APMPOWERSTATUSCHANGE = 4, 7, 18, 10
MODS = {"ctrl": 0x2, "alt": 0x1, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT = 0x4000

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR)]


u32.DefWindowProcW.restype = LRESULT
u32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
u32.CreateWindowExW.restype = wt.HWND
u32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
u32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
u32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
u32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
u32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
k32.GetModuleHandleW.restype = wt.HMODULE


def parse_hotkey(text: str) -> tuple[int, int]:
    """'ctrl+alt+p' -> (modifiers, virtual key)."""
    mods, vk = MOD_NOREPEAT, 0
    for part in text.lower().replace(" ", "").split("+"):
        if part in MODS:
            mods |= MODS[part]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit():
            vk = 0x6F + int(part[1:])
    if not vk:
        raise ValueError(f"bad hotkey {text!r}")
    return mods, vk


class WinEvents(threading.Thread):
    """Hidden window. Calls on_event('hotkey' | 'suspend' | 'resume' | 'power' | 'lock' | 'unlock')."""

    def __init__(self, on_event: Callable[[str], None], hotkey: str = "ctrl+alt+p"):
        super().__init__(daemon=True, name="win-events")
        self.on_event, self.hotkey = on_event, hotkey
        self.hwnd = None
        self.hotkey_ok = False
        self.ready = threading.Event()

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_HOTKEY:
                self.on_event("hotkey")
            elif msg == WM_POWERBROADCAST:
                if wp == PBT_APMSUSPEND:
                    self.on_event("suspend")
                elif wp in (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
                    self.on_event("resume")
                elif wp == PBT_APMPOWERSTATUSCHANGE:
                    self.on_event("power")
                return 1
            elif msg == WM_WTSSESSION_CHANGE:
                if wp in (7, 8):
                    self.on_event("lock" if wp == 7 else "unlock")
            elif msg == WM_CLOSE:
                u32.DestroyWindow(hwnd)
                return 0
            elif msg == WM_DESTROY:
                u32.PostQuitMessage(0)
                return 0
        except Exception:
            log.exception("event handler failed")
        return u32.DefWindowProcW(hwnd, msg, wp, lp)

    def run(self) -> None:
        self._proc = WNDPROC(self._wndproc)  # keep a reference, or Python frees it and Windows crashes
        hinst = k32.GetModuleHandleW(None)
        wc = _WNDCLASSW(lpfnWndProc=self._proc, hInstance=hinst, lpszClassName="PA_Events")
        u32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = u32.CreateWindowExW(0, "PA_Events", "PA", 0, 0, 0, 0, 0, None, None, hinst, None)
        try:
            mods, vk = parse_hotkey(self.hotkey)
            self.hotkey_ok = bool(u32.RegisterHotKey(self.hwnd, 1, mods, vk))
            if not self.hotkey_ok:
                log.warning("hotkey %s is used by another app", self.hotkey)
        except ValueError as e:
            log.warning("%s", e)
        try:
            ctypes.WinDLL("wtsapi32").WTSRegisterSessionNotification(self.hwnd, 0)
        except Exception:
            pass
        self.ready.set()
        msg = wt.MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))
        u32.UnregisterHotKey(self.hwnd, 1)

    def stop(self) -> None:
        if self.hwnd:
            u32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
