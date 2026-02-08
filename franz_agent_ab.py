from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.wintypes as w
import json
import struct
import threading
import time
import urllib.request
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# =============================
# CONFIG
# =============================

API_URL = "http://localhost:1234/v1/chat/completions"
MODEL_NAME = "qwen3-vl-2b-instruct-1m"

DUMP_FOLDER = Path("dump")

RES_PRESETS: dict[str, tuple[int, int]] = {
    "low": (512, 288),
    "med": (1024, 576),
    "high": (1536, 864),
}

# HUD (cyan) is your narrative memory.
HUD_POSITION_X = 0.65
HUD_POSITION_Y = 0.05
HUD_WIDTH = 0.40
HUD_HEIGHT = 1.00
HUD_MIN_WIDTH = 360
HUD_MIN_HEIGHT = 260
HUD_BG_COLOR = 0x00FFFF00  # cyan-ish BGR (COLORREF)
HUD_DEFAULT_FONT_ZOOM = 145

# Sampling (left as-is; if you need stricter tool JSON, lower temperature).
SAMPLING: dict[str, object] = {
    "temperature": 1.2,
    "top_p": 0.8,
    "top_k": 20,
    "max_tokens": 600,
    "stream": False,
    "stop": [],
    "presence_penalty": 1.2,
    "frequency_penalty": 0.2,
    "logit_bias": {},
    "repeat_penalty": 1.2,
    "seed": 42,
}

SYSTEM_PROMPT = (
    "You are FRANZ, operating a Windows desktop using ONLY the provided tools.\n"
    "The cyan HUD window contains the latest narrative memory. Never click, type into, or close the HUD.\n"
    "You MUST output EXACTLY ONE tool call per step.\n"
    "Tool-call arguments MUST be a single JSON object and MUST contain ONLY the keys defined in that tool's schema (no extra keys).\n"
    "Put all extra observations inside the story field.\n"
    "Coordinates are normalized integers 0..1000 for X and Y (0,0 is top-left; 1000,1000 is bottom-right).\n"
    "When drawing/painting, prefer the drag tool to make strokes instead of repeated clicking.\n"
    "Your story must be a coherent first-person narrative update (>=150 words) describing what you see and what you just did."
)

DEFAULT_HUD_TEXT = (
    "FRANZ wakes up inside a Windows desktop sandbox. The cyan HUD is his memory log. "
    "He will explore the environment methodically, using mouse and keyboard to interact with on-screen apps, "
    "while keeping a running narrative of intent, observations, and consequences."
)

# -----------------------------
# Tool schema (aligned: all tools require story)
# -----------------------------

def _xy_schema() -> dict[str, Any]:
    return {"type": "integer", "minimum": 0, "maximum": 1000}


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Mouse: left click at normalized (x,y). Always include updated story (>=150 words).",
            "parameters": {
                "type": "object",
                "properties": {"x": _xy_schema(), "y": _xy_schema(), "story": {"type": "string"}},
                "required": ["x", "y", "story"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "double_click",
            "description": "Mouse: double click at normalized (x,y). Always include updated story (>=150 words).",
            "parameters": {
                "type": "object",
                "properties": {"x": _xy_schema(), "y": _xy_schema(), "story": {"type": "string"}},
                "required": ["x", "y", "story"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "right_click",
            "description": "Mouse: right click at normalized (x,y). Always include updated story (>=150 words).",
            "parameters": {
                "type": "object",
                "properties": {"x": _xy_schema(), "y": _xy_schema(), "story": {"type": "string"}},
                "required": ["x", "y", "story"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drag",
            "description": "Mouse: drag from (x1,y1) to (x2,y2) to move/paint. Always include updated story (>=150 words).",
            "parameters": {
                "type": "object",
                "properties": {
                    "x1": _xy_schema(),
                    "y1": _xy_schema(),
                    "x2": _xy_schema(),
                    "y2": _xy_schema(),
                    "story": {"type": "string"},
                },
                "required": ["x1", "y1", "x2", "y2", "story"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Keyboard: type the given text. Always include updated story (>=150 words).",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "story": {"type": "string"}},
                "required": ["text", "story"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll",
            "description": "Mouse wheel: scroll by dy (positive=down, negative=up). Always include updated story (>=150 words).",
            "parameters": {
                "type": "object",
                "properties": {"dy": {"type": "integer"}, "story": {"type": "string"}},
                "required": ["dy", "story"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": "No-op: wait for ms milliseconds (useful when tool_choice is required). Always include updated story (>=150 words).",
            "parameters": {
                "type": "object",
                "properties": {"ms": {"type": "integer", "minimum": 1, "maximum": 10000}, "story": {"type": "string"}},
                "required": ["ms", "story"],
                "additionalProperties": False,
            },
        },
    },
]

TOOL_NAME_SET: set[str] = {str(t["function"]["name"]).strip().lower() for t in TOOLS}

# =============================
# WIN32 SETUP
# =============================

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ctypes.WinDLL("Shcore").SetProcessDpiAwareness(2)
kernel32.LoadLibraryW("Msftedit.dll")

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
WHEEL_DELTA = 120

MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE = 0x0001, 0x8000
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_WHEEL = 0x0800

KEYEVENTF_UNICODE, KEYEVENTF_KEYUP = 0x0004, 0x0002

WS_OVERLAPPED, WS_CAPTION, WS_SYSMENU = 0, 0x00C00000, 0x00080000
WS_THICKFRAME, WS_MINIMIZEBOX, WS_VISIBLE = 0x00040000, 0x00020000, 0x10000000
WS_VSCROLL, WS_CHILD, WS_POPUP = 0x00200000, 0x40000000, 0x80000000

ES_MULTILINE, ES_AUTOVSCROLL, ES_READONLY = 0x0004, 0x0040, 0x0800

WS_EX_TOPMOST, WS_EX_LAYERED = 0x00000008, 0x00080000

WM_SETFONT, WM_CLOSE, WM_DESTROY = 0x0030, 0x0010, 0x0002
WM_COMMAND, WM_SIZE, WM_MOUSEWHEEL = 0x0111, 0x0005, 0x020A

EM_SETBKGNDCOLOR, EM_SETREADONLY = 0x0443, 0x00CF
EM_SETTARGETDEVICE, EM_SETZOOM = 0x0449, 0x04E1

SW_SHOWNOACTIVATE = 4
SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0002, 0x0001, 0x0010, 0x0040
HWND_TOPMOST = -1

SRCCOPY, CAPTUREBLT = 0x00CC0020, 0x40000000
LWA_ALPHA = 0x00000002

CS_HREDRAW, CS_VREDRAW = 0x0002, 0x0001
IDC_ARROW, COLOR_WINDOW = 32512, 5

MAKEINTRESOURCEW = lambda i: ctypes.cast(ctypes.c_void_p(i & 0xFFFF), w.LPCWSTR)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", w.LONG),
        ("dy", w.LONG),
        ("mouseData", w.DWORD),
        ("dwFlags", w.DWORD),
        ("time", w.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", w.WORD),
        ("wScan", w.WORD),
        ("dwFlags", w.DWORD),
        ("time", w.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", w.DWORD), ("wParamL", w.WORD), ("wParamH", w.WORD)]


class _INPUTunion(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", w.DWORD), ("union", _INPUTunion)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", w.DWORD),
        ("biWidth", w.LONG),
        ("biHeight", w.LONG),
        ("biPlanes", w.WORD),
        ("biBitCount", w.WORD),
        ("biCompression", w.DWORD),
        ("biSizeImage", w.DWORD),
        ("biXPelsPerMeter", w.LONG),
        ("biYPelsPerMeter", w.LONG),
        ("biClrUsed", w.DWORD),
        ("biClrImportant", w.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", w.DWORD * 3)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", w.HWND), ("message", ctypes.c_uint), ("wParam", w.WPARAM), ("lParam", w.LPARAM), ("time", w.DWORD), ("pt", w.POINT)]


class RECT(ctypes.Structure):
    _fields_ = [("left", w.LONG), ("top", w.LONG), ("right", w.LONG), ("bottom", w.LONG)]


WNDPROC = ctypes.WINFUNCTYPE(w.LPARAM, w.HWND, ctypes.c_uint, w.WPARAM, w.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("style", ctypes.c_uint),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", w.HINSTANCE),
        ("hIcon", w.HANDLE),
        ("hCursor", w.HANDLE),
        ("hbrBackground", w.HANDLE),
        ("lpszMenuName", w.LPCWSTR),
        ("lpszClassName", w.LPCWSTR),
        ("hIconSm", w.HANDLE),
    ]


_SIGNATURES: list[tuple[Any, list[tuple[str, list[Any], Any]]]] = [
    (
        gdi32,
        [
            ("DeleteObject", [w.HGDIOBJ], w.BOOL),
            ("CreateCompatibleDC", [w.HDC], w.HDC),
            ("CreateDIBSection", [w.HDC, ctypes.POINTER(BITMAPINFO), ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p), w.HANDLE, w.DWORD], w.HBITMAP),
            ("SelectObject", [w.HDC, w.HGDIOBJ], w.HGDIOBJ),
            ("BitBlt", [w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HDC, ctypes.c_int, ctypes.c_int, w.DWORD], w.BOOL),
            ("StretchBlt", [w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.DWORD], w.BOOL),
            ("SetStretchBltMode", [w.HDC, ctypes.c_int], ctypes.c_int),
            ("DeleteDC", [w.HDC], w.BOOL),
            ("CreateFontW", [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.LPCWSTR], w.HFONT),
        ],
    ),
    (
        user32,
        [
            ("CreateWindowExW", [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID], w.HWND),
            ("ShowWindow", [w.HWND, ctypes.c_int], w.BOOL),
            ("SetWindowPos", [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint], w.BOOL),
            ("DestroyWindow", [w.HWND], w.BOOL),
            ("SendInput", [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int], ctypes.c_uint),
            ("GetSystemMetrics", [ctypes.c_int], ctypes.c_int),
            ("GetDC", [w.HWND], w.HDC),
            ("ReleaseDC", [w.HWND, w.HDC], ctypes.c_int),
            ("SetWindowTextW", [w.HWND, w.LPCWSTR], w.BOOL),
            ("SendMessageW", [w.HWND, ctypes.c_uint, w.WPARAM, w.LPARAM], w.LPARAM),
            ("PostMessageW", [w.HWND, ctypes.c_uint, w.WPARAM, w.LPARAM], w.BOOL),
            ("GetMessageW", [ctypes.POINTER(MSG), w.HWND, ctypes.c_uint, ctypes.c_uint], w.BOOL),
            ("TranslateMessage", [ctypes.POINTER(MSG)], w.BOOL),
            ("DispatchMessageW", [ctypes.POINTER(MSG)], w.LPARAM),
            ("SetLayeredWindowAttributes", [w.HWND, w.COLORREF, ctypes.c_ubyte, w.DWORD], w.BOOL),
            ("DefWindowProcW", [w.HWND, ctypes.c_uint, w.WPARAM, w.LPARAM], w.LPARAM),
            ("RegisterClassExW", [ctypes.POINTER(WNDCLASSEXW)], w.ATOM),
            ("LoadCursorW", [w.HINSTANCE, w.LPCWSTR], w.HANDLE),
            ("GetClientRect", [w.HWND, ctypes.POINTER(RECT)], w.BOOL),
            ("MoveWindow", [w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.BOOL], w.BOOL),
            ("GetAsyncKeyState", [ctypes.c_int], ctypes.c_short),
            ("GetWindowTextW", [w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
            ("GetWindowTextLengthW", [w.HWND], ctypes.c_int),
        ],
    ),
    (kernel32, [("GetModuleHandleW", [w.LPCWSTR], w.HMODULE)]),
]

for dll, funcs in _SIGNATURES:
    for name, args, res in funcs:
        fn = getattr(dll, name)
        fn.argtypes = args
        fn.restype = res


@dataclass(slots=True)
class Coord:
    sw: int
    sh: int

    def to_screen(self, x_norm: float, y_norm: float) -> tuple[int, int]:
        x = max(0.0, min(1000.0, x_norm)) * self.sw / 1000.0
        y = max(0.0, min(1000.0, y_norm)) * self.sh / 1000.0
        return int(x), int(y)

    def to_win32(self, x: int, y: int) -> tuple[int, int]:
        wx = (x * 65535 // self.sw) if self.sw > 0 else 0
        wy = (y * 65535 // self.sh) if self.sh > 0 else 0
        return wx, wy


def clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def send_input(inputs: list[INPUT]) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    if user32.SendInput(len(arr), arr, ctypes.sizeof(INPUT)) != len(inputs):
        raise ctypes.WinError(ctypes.get_last_error())
    time.sleep(0.05)


def make_mouse_input(dx: int, dy: int, flags: int, data: int = 0) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.union.mi = MOUSEINPUT(dx=dx, dy=dy, mouseData=data, dwFlags=flags, time=0, dwExtraInfo=None)
    return inp


def mouse_click(x: int, y: int, conv: Coord) -> None:
    wx, wy = conv.to_win32(x, y)
    send_input(
        [
            make_mouse_input(wx, wy, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE),
            make_mouse_input(0, 0, MOUSEEVENTF_LEFTDOWN),
            make_mouse_input(0, 0, MOUSEEVENTF_LEFTUP),
        ]
    )


def mouse_right_click(x: int, y: int, conv: Coord) -> None:
    wx, wy = conv.to_win32(x, y)
    send_input(
        [
            make_mouse_input(wx, wy, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE),
            make_mouse_input(0, 0, MOUSEEVENTF_RIGHTDOWN),
            make_mouse_input(0, 0, MOUSEEVENTF_RIGHTUP),
        ]
    )


def mouse_double_click(x: int, y: int, conv: Coord) -> None:
    wx, wy = conv.to_win32(x, y)
    send_input(
        [
            make_mouse_input(wx, wy, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE),
            make_mouse_input(0, 0, MOUSEEVENTF_LEFTDOWN),
            make_mouse_input(0, 0, MOUSEEVENTF_LEFTUP),
        ]
    )
    time.sleep(0.05)
    send_input([make_mouse_input(0, 0, MOUSEEVENTF_LEFTDOWN), make_mouse_input(0, 0, MOUSEEVENTF_LEFTUP)])


def mouse_drag(x1: int, y1: int, x2: int, y2: int, conv: Coord) -> None:
    wx1, wy1 = conv.to_win32(x1, y1)
    wx2, wy2 = conv.to_win32(x2, y2)

    send_input([make_mouse_input(wx1, wy1, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE), make_mouse_input(0, 0, MOUSEEVENTF_LEFTDOWN)])
    time.sleep(0.05)

    # smooth drag in 10 segments
    for i in range(1, 11):
        ix = int(wx1 + (wx2 - wx1) * i / 10)
        iy = int(wy1 + (wy2 - wy1) * i / 10)
        send_input([make_mouse_input(ix, iy, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE)])
        time.sleep(0.01)

    send_input([make_mouse_input(0, 0, MOUSEEVENTF_LEFTUP)])


def type_text(text: str) -> None:
    if not text:
        return
    utf16 = text.encode("utf-16le")
    inputs: list[INPUT] = []
    for i in range(0, len(utf16), 2):
        code = utf16[i] | (utf16[i + 1] << 8)
        d = INPUT()
        d.type = INPUT_KEYBOARD
        d.union.ki = KEYBDINPUT(wVk=0, wScan=code, dwFlags=KEYEVENTF_UNICODE, time=0, dwExtraInfo=None)
        inputs.append(d)
        u = INPUT()
        u.type = INPUT_KEYBOARD
        u.union.ki = KEYBDINPUT(wVk=0, wScan=code, dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, time=0, dwExtraInfo=None)
        inputs.append(u)
    send_input(inputs)


def scroll(dy: float) -> None:
    direction = 1 if dy > 0 else -1
    count = max(1, int(abs(dy) / WHEEL_DELTA))
    send_input([make_mouse_input(0, 0, MOUSEEVENTF_WHEEL, WHEEL_DELTA * direction) for _ in range(count)])


def capture_screen(sw: int, sh: int) -> bytes:
    sdc = user32.GetDC(0)
    if not sdc:
        raise ctypes.WinError(ctypes.get_last_error())

    mdc = gdi32.CreateCompatibleDC(sdc)
    if not mdc:
        user32.ReleaseDC(0, sdc)
        raise ctypes.WinError(ctypes.get_last_error())

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = sw, -sh
    bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount = 1, 32

    bits = ctypes.c_void_p()
    hbm = gdi32.CreateDIBSection(sdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    if not hbm:
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(0, sdc)
        raise ctypes.WinError(ctypes.get_last_error())

    gdi32.SelectObject(mdc, hbm)

    if not gdi32.BitBlt(mdc, 0, 0, sw, sh, sdc, 0, 0, SRCCOPY | CAPTUREBLT):
        gdi32.DeleteObject(hbm)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(0, sdc)
        raise ctypes.WinError(ctypes.get_last_error())

    out = ctypes.string_at(bits, sw * sh * 4)
    user32.ReleaseDC(0, sdc)
    gdi32.DeleteDC(mdc)
    gdi32.DeleteObject(hbm)
    return out


def downsample(src: bytes, sw: int, sh: int, dw: int, dh: int) -> bytes:
    if (sw, sh) == (dw, dh):
        return src
    if sw <= 0 or sh <= 0 or dw <= 0 or dh <= 0 or len(src) < sw * sh * 4:
        return b""

    sdc = user32.GetDC(0)
    if not sdc:
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        src_dc = gdi32.CreateCompatibleDC(sdc)
        dst_dc = gdi32.CreateCompatibleDC(sdc)
        if not src_dc or not dst_dc:
            raise ctypes.WinError(ctypes.get_last_error())

        bmi_src = BITMAPINFO()
        bmi_src.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi_src.bmiHeader.biWidth, bmi_src.bmiHeader.biHeight = sw, -sh
        bmi_src.bmiHeader.biPlanes, bmi_src.bmiHeader.biBitCount = 1, 32

        src_bits = ctypes.c_void_p()
        src_bmp = gdi32.CreateDIBSection(sdc, ctypes.byref(bmi_src), 0, ctypes.byref(src_bits), None, 0)
        if not src_bmp or not src_bits:
            raise ctypes.WinError(ctypes.get_last_error())

        old_src = gdi32.SelectObject(src_dc, src_bmp)
        ctypes.memmove(src_bits, src, sw * sh * 4)

        bmi_dst = BITMAPINFO()
        bmi_dst.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi_dst.bmiHeader.biWidth, bmi_dst.bmiHeader.biHeight = dw, -dh
        bmi_dst.bmiHeader.biPlanes, bmi_dst.bmiHeader.biBitCount = 1, 32

        dst_bits = ctypes.c_void_p()
        dst_bmp = gdi32.CreateDIBSection(sdc, ctypes.byref(bmi_dst), 0, ctypes.byref(dst_bits), None, 0)
        if not dst_bmp or not dst_bits:
            raise ctypes.WinError(ctypes.get_last_error())

        old_dst = gdi32.SelectObject(dst_dc, dst_bmp)
        gdi32.SetStretchBltMode(dst_dc, 4)

        if not gdi32.StretchBlt(dst_dc, 0, 0, dw, dh, src_dc, 0, 0, sw, sh, SRCCOPY):
            raise ctypes.WinError(ctypes.get_last_error())

        result = ctypes.string_at(dst_bits, dw * dh * 4)

        gdi32.SelectObject(src_dc, old_src)
        gdi32.SelectObject(dst_dc, old_dst)
        gdi32.DeleteObject(src_bmp)
        gdi32.DeleteObject(dst_bmp)
        gdi32.DeleteDC(src_dc)
        gdi32.DeleteDC(dst_dc)
        return result
    finally:
        user32.ReleaseDC(0, sdc)


def encode_png(bgra: bytes, width: int, height: int) -> bytes:
    # BGRA -> RGB PNG
    raw = bytearray((width * 3 + 1) * height)
    for y in range(height):
        raw[y * (width * 3 + 1)] = 0
        row = bgra[y * width * 4 : (y + 1) * width * 4]
        for x in range(width):
            raw[y * (width * 3 + 1) + 1 + x * 3 : y * (width * 3 + 1) + 1 + x * 3 + 3] = [
                row[x * 4 + 2],
                row[x * 4 + 1],
                row[x * 4 + 0],
            ]

    comp = zlib.compress(bytes(raw), 6)
    ihdr = struct.pack(">2I5B", width, height, 8, 2, 0, 0, 0
    )

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", comp) + chunk(b"IEND", b"")


def create_shared_fonts() -> tuple[w.HFONT, w.HFONT]:
    mono = gdi32.CreateFontW(-14, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 0, 0, "Consolas")
    ui = gdi32.CreateFontW(-14, 0, 0, 0, 700, 0, 0, 0, 1, 0, 0, 0, 0, "Segoe UI")
    return mono, ui


@dataclass(slots=True)
class HUD:
    hwnd: w.HWND | None = None
    edit: w.HWND | None = None
    btn: w.HWND | None = None
    thread: threading.Thread | None = None
    ready: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)
    paused: bool = True
    pause_event: threading.Event = field(default_factory=threading.Event)
    zoom_num: int = HUD_DEFAULT_FONT_ZOOM
    zoom_den: int = 100
    font_mono: w.HFONT = 0
    font_ui: w.HFONT = 0
    _wndproc_ref: WNDPROC | None = None
    _BTN_ID: int = 1001

    def _set_paused(self, p: bool) -> None:
        self.paused = p
        if self.btn:
            user32.SetWindowTextW(self.btn, "RESUME" if p else "PAUSE")
        if self.edit:
            user32.SendMessageW(self.edit, EM_SETREADONLY, 0 if p else 1, 0)
        if p:
            self.pause_event.clear()
        else:
            self.pause_event.set()

    def _layout(self) -> None:
        if not self.hwnd:
            return
        cr = RECT()
        if not user32.GetClientRect(self.hwnd, ctypes.byref(cr)):
            return
        cw, ch = max(1, cr.right), max(1, cr.bottom)
        pad, bh = 10, 40
        by = max(pad, ch - pad - bh)
        if self.edit:
            user32.MoveWindow(self.edit, pad, pad, max(10, cw - 20), max(10, by - 2 * pad), True)
            user32.SendMessageW(self.edit, EM_SETTARGETDEVICE, 0, 0)
        if self.btn:
            user32.MoveWindow(self.btn, pad, by, max(80, cw - 20), bh, True)

    def _wndproc(self, hwnd: w.HWND, msg: int, wparam: w.WPARAM, lparam: w.LPARAM) -> w.LPARAM:
        try:
            if msg == WM_COMMAND and (int(wparam) & 0xFFFF) == self._BTN_ID:
                self._set_paused(not self.paused)
                return 0
            if msg == WM_MOUSEWHEEL:
                delta = ctypes.c_short(wparam >> 16).value
                ctrl = bool(user32.GetAsyncKeyState(0x11) & 0x8000)
                if ctrl:
                    if delta > 0:
                        self.zoom_num = min(400, int(self.zoom_num * 1.1))
                    else:
                        self.zoom_num = max(20, int(self.zoom_num * 0.9))
                    if self.edit:
                        user32.SendMessageW(self.edit, EM_SETZOOM, self.zoom_num, self.zoom_den)
                    return 0
            if msg == WM_SIZE:
                self._layout()
            if msg in (WM_CLOSE, WM_DESTROY):
                self.stop.set()
                self.pause_event.set()
                if msg == WM_CLOSE:
                    user32.DestroyWindow(hwnd)
                return 0
        except Exception:
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _thread(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)

        w_px = min(max(HUD_MIN_WIDTH, int(sw * HUD_WIDTH)), sw)
        h_px = min(max(HUD_MIN_HEIGHT, int(sh * HUD_HEIGHT)), sh)
        x = clamp(int(sw * HUD_POSITION_X), 0, max(0, sw - w_px))
        y = clamp(int(sh * HUD_POSITION_Y), 0, max(0, sh - h_px))

        self._wndproc_ref = WNDPROC(self._wndproc)
        wc = WNDCLASSEXW(
            cbSize=ctypes.sizeof(WNDCLASSEXW),
            style=CS_HREDRAW | CS_VREDRAW,
            lpfnWndProc=self._wndproc_ref,
            cbClsExtra=0,
            cbWndExtra=0,
            hInstance=hinst,
            hIcon=None,
            hCursor=user32.LoadCursorW(None, MAKEINTRESOURCEW(IDC_ARROW)),
            hbrBackground=w.HANDLE(COLOR_WINDOW + 1),
            lpszMenuName=None,
            lpszClassName="FRANZHUD",
            hIconSm=None,
        )
        if not user32.RegisterClassExW(ctypes.byref(wc)) and ctypes.get_last_error() != 1410:
            self.ready.set()
            return

        self.hwnd = user32.CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_LAYERED,
            "FRANZHUD",
            "FRANZ",
            WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_THICKFRAME | WS_MINIMIZEBOX | WS_VISIBLE,
            x,
            y,
            w_px,
            h_px,
            None,
            None,
            hinst,
            None,
        )
        if not self.hwnd:
            self.ready.set()
            return

        user32.SetLayeredWindowAttributes(self.hwnd, 0, ctypes.c_ubyte(255), LWA_ALPHA)

        self.font_mono, self.font_ui = create_shared_fonts()

        self.edit = user32.CreateWindowExW(
            0,
            "RICHEDIT50W",
            "",
            WS_CHILD | WS_VISIBLE | WS_VSCROLL | ES_MULTILINE | ES_AUTOVSCROLL,
            0,
            0,
            10,
            10,
            self.hwnd,
            None,
            hinst,
            None,
        )
        if self.edit:
            if self.font_mono:
                user32.SendMessageW(self.edit, WM_SETFONT, self.font_mono, 1)
            user32.SendMessageW(self.edit, EM_SETBKGNDCOLOR, 0, HUD_BG_COLOR)
            user32.SendMessageW(self.edit, EM_SETZOOM, self.zoom_num, self.zoom_den)

        self.btn = user32.CreateWindowExW(0, "BUTTON", "RESUME", WS_CHILD | WS_VISIBLE, 0, 0, 10, 10, self.hwnd, w.HMENU(self._BTN_ID), hinst, None)
        if self.btn and self.font_ui:
            user32.SendMessageW(self.btn, WM_SETFONT, self.font_ui, 1)

        self._layout()
        self._set_paused(True)

        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
        self.ready.set()

        msg = MSG()
        while not self.stop.is_set():
            if user32.GetMessageW(ctypes.byref(msg), None, 0, 0) in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def __enter__(self) -> HUD:
        self.ready.clear()
        self.stop.clear()
        self.pause_event.clear()
        self.thread = threading.Thread(target=self._thread, daemon=True)
        self.thread.start()
        self.ready.wait(timeout=2.0)
        time.sleep(0.2)
        return self

    def __exit__(self, *_: Any) -> None:
        self.stop.set()
        self.pause_event.set()
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        if self.thread:
            self.thread.join(timeout=1.0)

    def get_text(self) -> str:
        if not self.edit:
            return ""
        n = user32.GetWindowTextLengthW(self.edit)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(self.edit, buf, n + 1)
        return buf.value

    def update(self, story: str) -> None:
        if self.edit:
            user32.SetWindowTextW(self.edit, story)
            user32.SendMessageW(self.edit, EM_SETZOOM, self.zoom_num, self.zoom_den)

    def wait(self) -> None:
        while self.paused and not self.stop.is_set():
            self.pause_event.wait(timeout=0.1)


def call_vlm(png: bytes) -> list[tuple[str, dict[str, Any]]]:
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Observe the screen and execute exactly ONE tool call that meaningfully changes the screen. "
                            "Put your full narrative update into the story field. "
                            "Remember: do NOT add extra JSON keys; only the tool schema keys are allowed."
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64.b64encode(png).decode('ascii')}"}},
                ],
            },
        ],
        "tools": TOOLS,
        "tool_choice": "required",
        **SAMPLING,
    }

    req = urllib.request.Request(API_URL, json.dumps(payload).encode("utf-8"), {"Content-Type": "application/json"})
    data = json.load(urllib.request.urlopen(req, timeout=120))

    choices = data.get("choices") or []
    if not choices:
        raise ValueError("No choices in VLM response")

    msg = choices[0].get("message") or {}
    tool_calls = msg.get("tool_calls") or []
    if not tool_calls:
        raise ValueError("No tool calls in VLM response")

    out: list[tuple[str, dict[str, Any]]] = []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        name = str(fn.get("name") or "").strip().lower()
        if name not in TOOL_NAME_SET:
            raise ValueError(f"Unknown tool: {name!r}")

        args_raw = fn.get("arguments", "")
        if isinstance(args_raw, str):
            args = json.loads(args_raw) if args_raw.strip() else {}
        elif isinstance(args_raw, dict):
            args = args_raw
        else:
            raise ValueError(f"Invalid arguments type: {type(args_raw).__name__}")

        if not isinstance(args, dict):
            raise ValueError("Tool arguments must be an object")

        out.append((name, args))

    return out


def execute(tool: str, args: dict[str, Any], conv: Coord) -> None:
    match tool:
        case "click":
            mouse_click(*conv.to_screen(float(args["x"]), float(args["y"])), conv)
        case "right_click":
            mouse_right_click(*conv.to_screen(float(args["x"]), float(args["y"])), conv)
        case "double_click":
            mouse_double_click(*conv.to_screen(float(args["x"]), float(args["y"])), conv)
        case "drag":
            mouse_drag(
                *conv.to_screen(float(args["x1"]), float(args["y1"])),
                *conv.to_screen(float(args["x2"]), float(args["y2"])),
                conv,
            )
        case "type_text":
            type_text(str(args["text"]))
        case "scroll":
            scroll(float(args["dy"]))
        case "wait":
            time.sleep(max(1, int(args["ms"])) / 1000.0)


def main() -> None:
    parser = argparse.ArgumentParser(description="FRANZ agent (A+B fixes only: persistent HUD story + no yellow overlays)")
    parser.add_argument("--res", choices=["low", "med", "high"], default="high", help="Model input resolution preset")
    args = parser.parse_args()

    screen_w, screen_h = RES_PRESETS[args.res]

    sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    conv = Coord(sw=sw, sh=sh)

    dump = DUMP_FOLDER / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    dump.mkdir(parents=True, exist_ok=True)

    print(f"FRANZ | Screen: {sw}x{sh} | Model input: {screen_w}x{screen_h} | Model: {MODEL_NAME}")
    print(f"Dump: {dump}")
    print("PAUSED - edit story in HUD, click RESUME to start")
    print("HUD: CTRL+Scroll to zoom text")

    with HUD() as hud:
        if not hud.get_text().strip():
            hud.update(DEFAULT_HUD_TEXT)

        # Wait for first resume, then lock in initial story.
        hud.wait()
        last_story = hud.get_text().strip() or DEFAULT_HUD_TEXT

        step = 0
        while not hud.stop.is_set():
            hud.wait()
            if hud.stop.is_set():
                break

            step += 1
            ts = datetime.now().strftime("%H:%M:%S")

            # B: no yellow annotation windows exist anymore (removed entirely), so capture is clean.
            bgra = capture_screen(sw, sh)
            png = encode_png(downsample(bgra, sw, sh, screen_w, screen_h), screen_w, screen_h)

            img_name = f"step{step:03d}.png"
            (dump / img_name).write_bytes(png)

            try:
                calls = call_vlm(png)
            except Exception as e:
                print(f"[{ts}] {step:03d} | VLM ERROR: {e}")
                time.sleep(1.0)
                continue

            for idx, (tool, tool_args) in enumerate(calls, 1):
                story = tool_args.get("story")
                if isinstance(story, str) and story.strip():
                    last_story = story.strip()

                print(f"[{ts}] {step:03d}.{idx:02d} | {tool}")

                try:
                    execute(tool, tool_args, conv)
                except Exception as e:
                    print(f"[{ts}] {step:03d}.{idx:02d} | EXEC ERROR ({tool}): {e} | args={tool_args}")
                    break

                time.sleep(0.05)

            # A: persist story across steps; do not reset to default on tool errors.
            hud.update(last_story)
            time.sleep(0.25)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nFRANZ stops.")
