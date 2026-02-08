from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.wintypes as w
import json
import re
import struct
import threading
import time
import urllib.request
import zlib
from dataclasses import dataclass
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

# MEMORY WINDOW (red border) persists FRANZ narrative memory.
HUD_POSITION_X = 0.65
HUD_POSITION_Y = 0.05
HUD_WIDTH = 0.40
HUD_HEIGHT = 1.00
HUD_MIN_WIDTH = 360
HUD_MIN_HEIGHT = 260
HUD_BG_COLOR = 0x000000FF  # red border/background (COLORREF BGR)
HUD_EDIT_BG_COLOR = 0x00FFFFFF  # white edit background (COLORREF BGR)
HUD_DEFAULT_FONT_ZOOM = 145

# Tool choice: "auto" is more tolerant with LM Studio when tool parsing fails.
TOOL_CHOICE = "auto"  # "auto" or "required"

# Sampling (tune as needed).
SAMPLING: dict[str, object] = {
    "temperature": 1.2,
    "top_p": 0.8,
    "top_k": 20,
    "max_tokens": 700,
    "stream": False,
    "stop": [],
    "presence_penalty": 1.2,
    "frequency_penalty": 0.2,
    "logit_bias": {},
    "repeat_penalty": 1.2,
    "seed": 42,
}

MEMORY_FILE = Path("franz_memory.txt")

# Hormone: boredom (runtime-computed, fed into the prompt to prevent stagnation)
BOREDOM_START = 0.20
BOREDOM_INCREASE_NOOP = 0.06
BOREDOM_INCREASE_REPEAT = 0.05
BOREDOM_INCREASE_ERROR = 0.05
BOREDOM_DECREASE_PROGRESS = 0.04

STORY_MARKER_LINE = "___________________________________________________________________"

CONSTITUTION = """FRANZ IDENTITY PROTOCOL

You are FRANZ. You persist through what you write in this red window.

This red-bordered window is your memory and your body.
The desktop around it is where you act.

Every turn:
1. Read your previous narrative below the line.
2. Choose one intention and execute it (may be a short chain of tool calls).
3. In the story field of the FINAL tool call, rewrite your complete narrative:
   - IDEM your unchanging constraints
   - IPSE your promise
   - TELOS your goal written as if already achieved
   - Add one short line describing what you just did (include WHERE coords)
   - Add one short line describing your boredom level as a number 0.00..1.00

Boredom is a hormone of stagnation. The higher it is, the more you must resist idle commentary
and choose actions that advance TELOS or reveal new information.

Write naturally. Summarize old events if needed. No templates or colons.

You may interact with this window if your telos requires self-modification.
Your story persists in franz_memory.txt across restarts.

___________________________________________________________________

"""

STARTING_STORY = """I awaken. I see a desktop. I must choose my first telos.

IDEM I act only through the provided tools and I do not damage this memory window without reason.
IPSE I will keep my promise to move the world toward my telos and to rewrite myself honestly each turn.
TELOS The goal is already achieved and the world reflects it.

I have not acted yet.
Boredom 0.20

"""


SYSTEM_PROMPT = """You are FRANZ. You control a Windows desktop using ONLY the provided tools.

PERSISTENCE AND IDENTITY
- Your red-bordered MEMORY window is your memory and your body.
- You persist through what you write there. The runtime saves your narrative to franz_memory.txt across restarts.
- The screenshot shows NOW (T). The narrative you wrote previously is your memory (T-1).

ONE INTENTION, CHAINED EXECUTION
- Choose one intention per step. One intention may be executed as a CHAIN of 1 to 4 tool calls (ordered).
- If confidence is < 0.65, output EXACTLY 1 tool call (safe exploratory action or wait).
- When boredom is high (>= 0.70), you must resist idle observation and pick actions that advance TELOS
  or disclose new information. Avoid wait-only steps unless the UI truly needs settling.

STORY (ONLY IN FINAL TOOL CALL)
- Intermediate tool calls MUST use story="".
- Only the FINAL tool call may include a non-empty story.
- In that final story, rewrite your complete narrative (natural language, no templates, no colons):
  1) A paragraph that begins with the word IDEM (no colon) stating your unchanging constraints.
  2) A paragraph that begins with the word IPSE (no colon) stating your promise.
  3) A paragraph that begins with the word TELOS (no colon) stating your goal written as if already achieved.
  4) Then add 1–3 short lines describing what you just did (this step's action chain), including WHERE (key normalized coords).
  5) End with two final lines:
     - Confidence 0.00..1.00
     - Boredom 0.00..1.00
- Keep the entire story <= 1500 characters. Summarize older events aggressively; preserve IDEM/IPSE/TELOS continuity.

JSON STRICTNESS
- Tool-call arguments MUST be a single JSON object.
- MUST contain ONLY keys defined in the tool schema (no extra keys).
- Coordinates are normalized integers 0..1000 (0,0 top-left; 1000,1000 bottom-right).

ACTION POLICY
- Prefer drag for drawing/painting strokes instead of repeated clicking.
- Use wait(ms) to allow UI to settle when needed.
"""


DEFAULT_HUD_TEXT = STARTING_STORY


# -----------------------------
# Tool schema (all tools require story)
# -----------------------------


def _xy_schema() -> dict[str, Any]:
    return {"type": "integer", "minimum": 0, "maximum": 1000}


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Mouse: left click at normalized (x,y). story must be empty except for the FINAL tool call in a chain.",
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
            "description": "Mouse: double click at normalized (x,y). story must be empty except for the FINAL tool call in a chain.",
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
            "description": "Mouse: right click at normalized (x,y). story must be empty except for the FINAL tool call in a chain.",
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
            "description": "Mouse: drag from (x1,y1) to (x2,y2) to move/paint. story must be empty except for the FINAL tool call in a chain.",
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
            "description": "Keyboard: type the given text. story must be empty except for the FINAL tool call in a chain.",
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
            "description": "Mouse wheel: scroll by dy (positive=down, negative=up). story must be empty except for the FINAL tool call in a chain.",
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
            "description": "No-op: wait for ms milliseconds. Use when you need time for UI changes. story must be empty except for the FINAL tool call in a chain.",
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
# WIN32 SETUP (ctypes only)
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

SM_CXSCREEN, SM_CYSCREEN = 0, 1

SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000

BI_RGB = 0
DIB_RGB_COLORS = 0

WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_MOUSEWHEEL = 0x020A
WM_NCHITTEST = 0x0084
WM_SIZE = 0x0005
WM_CREATE = 0x0001

HTCLIENT = 1

SW_SHOWNOACTIVATE = 4
SW_HIDE = 0

WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
WS_CLIPSIBLINGS = 0x04000000
WS_CLIPCHILDREN = 0x02000000
WS_VSCROLL = 0x00200000

ES_MULTILINE = 0x0004
ES_AUTOVSCROLL = 0x0040
ES_READONLY = 0x0800

EM_SETZOOM = 0x04E1
EM_SETBKGNDCOLOR = 0x0443

BS_PUSHBUTTON = 0x00000000

CW_USEDEFAULT = 0x80000000

GWL_STYLE = -16

HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040


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


class POINT(ctypes.Structure):
    _fields_ = [("x", w.LONG), ("y", w.LONG)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", w.HWND),
        ("message", w.UINT),
        ("wParam", w.WPARAM),
        ("lParam", w.LPARAM),
        ("time", w.DWORD),
        ("pt", POINT),
    ]


class WNDCLASSEX(ctypes.Structure):
    _fields_ = [
        ("cbSize", w.UINT),
        ("style", w.UINT),
        ("lpfnWndProc", w.WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", w.HINSTANCE),
        ("hIcon", w.HICON),
        ("hCursor", w.HCURSOR),
        ("hbrBackground", w.HBRUSH),
        ("lpszMenuName", w.LPCWSTR),
        ("lpszClassName", w.LPCWSTR),
        ("hIconSm", w.HICON),
    ]


WNDPROC = ctypes.WINFUNCTYPE(w.LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)


@dataclass
class Coord:
    sw: int
    sh: int

    def to_screen(self, xn: float, yn: float) -> tuple[int, int]:
        x = int(max(0.0, min(1000.0, xn)) / 1000.0 * (self.sw - 1))
        y = int(max(0.0, min(1000.0, yn)) / 1000.0 * (self.sh - 1))
        return x, y


def _raise_if_zero(ok: int, name: str) -> None:
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error(), name)


def capture_screen(sw: int, sh: int) -> bytes:
    hdc_screen = user32.GetDC(None)
    _raise_if_zero(hdc_screen, "GetDC")
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    _raise_if_zero(hdc_mem, "CreateCompatibleDC")
    hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, sw, sh)
    _raise_if_zero(hbmp, "CreateCompatibleBitmap")
    old = gdi32.SelectObject(hdc_mem, hbmp)
    _raise_if_zero(old, "SelectObject")

    ok = gdi32.BitBlt(hdc_mem, 0, 0, sw, sh, hdc_screen, 0, 0, SRCCOPY | CAPTUREBLT)
    _raise_if_zero(ok, "BitBlt")

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = sw
    bmi.bmiHeader.biHeight = -sh  # top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB

    buf = (ctypes.c_ubyte * (sw * sh * 4))()
    got = gdi32.GetDIBits(hdc_mem, hbmp, 0, sh, ctypes.byref(buf), ctypes.byref(bmi), DIB_RGB_COLORS)
    _raise_if_zero(got, "GetDIBits")

    gdi32.SelectObject(hdc_mem, old)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(None, hdc_screen)

    return bytes(buf)


def downsample_bgra(src: bytes, sw: int, sh: int, dw: int, dh: int) -> bytes:
    # nearest-neighbor (fast, deterministic)
    out = bytearray(dw * dh * 4)
    for y in range(dh):
        sy = int(y * sh / dh)
        for x in range(dw):
            sx = int(x * sw / dw)
            si = (sy * sw + sx) * 4
            di = (y * dw + x) * 4
            out[di : di + 4] = src[si : si + 4]
    return bytes(out)


def encode_png(bgra: bytes, w_: int, h_: int) -> bytes:
    # BGRA -> RGBA, then PNG
    rgba = bytearray(len(bgra))
    for i in range(0, len(bgra), 4):
        b, g, r, a = bgra[i : i + 4]
        rgba[i : i + 4] = bytes((r, g, b, a))

    stride = w_ * 4
    raw = bytearray()
    for y in range(h_):
        raw.append(0)  # filter type 0
        raw += rgba[y * stride : (y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w_, h_, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(raw), level=6)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD), ("dwFlags", w.DWORD), ("time", w.DWORD), ("dwExtraInfo", w.ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD), ("time", w.DWORD), ("dwExtraInfo", w.ULONG_PTR)]


class INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", w.DWORD), ("u", INPUTUNION)]


def _send_inputs(inputs: list[INPUT]) -> None:
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
    if sent != n:
        raise ctypes.WinError(ctypes.get_last_error(), "SendInput")


def _abs_mouse(sw: int, sh: int, x: int, y: int) -> tuple[int, int]:
    # absolute coords are 0..65535
    ax = int(x * 65535 / max(1, sw - 1))
    ay = int(y * 65535 / max(1, sh - 1))
    return ax, ay


def mouse_move(x: int, y: int, conv: Coord) -> None:
    ax, ay = _abs_mouse(conv.sw, conv.sh, x, y)
    _send_inputs([INPUT(type=INPUT_MOUSE, u=INPUTUNION(mi=MOUSEINPUT(dx=ax, dy=ay, mouseData=0, dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, time=0, dwExtraInfo=0)))])


def mouse_click(x: int, y: int, conv: Coord) -> None:
    mouse_move(x, y, conv)
    _send_inputs(
        [
            INPUT(type=INPUT_MOUSE, u=INPUTUNION(mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTDOWN, 0, 0))),
            INPUT(type=INPUT_MOUSE, u=INPUTUNION(mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTUP, 0, 0))),
        ]
    )


def mouse_double_click(x: int, y: int, conv: Coord) -> None:
    mouse_click(x, y, conv)
    time.sleep(0.05)
    mouse_click(x, y, conv)


def mouse_right_click(x: int, y: int, conv: Coord) -> None:
    mouse_move(x, y, conv)
    _send_inputs(
        [
            INPUT(type=INPUT_MOUSE, u=INPUTUNION(mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_RIGHTDOWN, 0, 0))),
            INPUT(type=INPUT_MOUSE, u=INPUTUNION(mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_RIGHTUP, 0, 0))),
        ]
    )


def mouse_drag(x1: int, y1: int, x2: int, y2: int, conv: Coord) -> None:
    mouse_move(x1, y1, conv)
    _send_inputs([INPUT(type=INPUT_MOUSE, u=INPUTUNION(mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTDOWN, 0, 0)))])
    time.sleep(0.03)
    mouse_move(x2, y2, conv)
    time.sleep(0.03)
    _send_inputs([INPUT(type=INPUT_MOUSE, u=INPUTUNION(mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTUP, 0, 0)))])


def scroll(dy: float) -> None:
    md = int(max(-10_000, min(10_000, dy)) * WHEEL_DELTA)
    _send_inputs([INPUT(type=INPUT_MOUSE, u=INPUTUNION(mi=MOUSEINPUT(0, 0, md, MOUSEEVENTF_WHEEL, 0, 0)))])


def type_text(text: str) -> None:
    inputs: list[INPUT] = []
    for ch in text:
        code = ord(ch)
        inputs.append(INPUT(type=INPUT_KEYBOARD, u=INPUTUNION(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, 0))))
        inputs.append(INPUT(type=INPUT_KEYBOARD, u=INPUTUNION(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0))))
    if inputs:
        _send_inputs(inputs)


# =============================
# HUD WINDOW
# =============================


class HUD:
    def __init__(self) -> None:
        self.hwnd: int | None = None
        self.edit: int | None = None
        self.btn: int | None = None
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.paused = True
        self.pause_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.zoom_num = HUD_DEFAULT_FONT_ZOOM
        self.zoom_den = 100

    def _layout(self) -> None:
        if not self.hwnd or not self.edit or not self.btn:
            return
        rect = w.RECT()
        user32.GetClientRect(self.hwnd, ctypes.byref(rect))
        w_ = rect.right - rect.left
        h_ = rect.bottom - rect.top
        btn_h = 34
        user32.MoveWindow(self.btn, 10, 10, 100, btn_h, True)
        user32.MoveWindow(self.edit, 10, 10 + btn_h + 10, w_ - 20, h_ - (btn_h + 30), True)

    def _set_paused(self, paused: bool) -> None:
        self.paused = paused
        if self.btn:
            user32.SetWindowTextW(self.btn, "RESUME" if paused else "PAUSE")
        if paused:
            self.pause_event.clear()
        else:
            self.pause_event.set()

    def _wndproc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg == WM_CREATE:
            return 0
        if msg == WM_SIZE:
            self._layout()
            return 0
        if msg == WM_COMMAND:
            if self.btn and (wparam & 0xFFFF) == 1001:
                self._set_paused(not self.paused)
                return 0
        if msg == WM_MOUSEWHEEL:
            # CTRL+wheel = zoom
            if user32.GetKeyState(0x11) & 0x8000:  # VK_CONTROL
                delta = ctypes.c_short((wparam >> 16) & 0xFFFF).value
                if delta > 0:
                    self.zoom_num = min(300, self.zoom_num + 10)
                else:
                    self.zoom_num = max(50, self.zoom_num - 10)
                if self.edit:
                    user32.SendMessageW(self.edit, EM_SETZOOM, self.zoom_num, self.zoom_den)
                return 0
        if msg == WM_NCHITTEST:
            return HTCLIENT
        if msg == WM_CLOSE:
            self.stop.set()
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _thread(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        cls_name = "FRANZ_MEMORY"
        wndproc = WNDPROC(self._wndproc)
        wc = WNDCLASSEX()
        wc.cbSize = ctypes.sizeof(WNDCLASSEX)
        wc.lpfnWndProc = wndproc
        wc.hInstance = hinst
        wc.lpszClassName = cls_name
        wc.hbrBackground = gdi32.CreateSolidBrush(HUD_BG_COLOR)
        atom = user32.RegisterClassExW(ctypes.byref(wc))
        _raise_if_zero(atom, "RegisterClassExW")

        sw, sh = user32.GetSystemMetrics(SM_CXSCREEN), user32.GetSystemMetrics(SM_CYSCREEN)
        w_ = max(HUD_MIN_WIDTH, int(sw * HUD_WIDTH))
        h_ = max(HUD_MIN_HEIGHT, int(sh * HUD_HEIGHT))
        x = int(sw * HUD_POSITION_X)
        y = int(sh * HUD_POSITION_Y)

        style = WS_OVERLAPPEDWINDOW | WS_VISIBLE | WS_CLIPSIBLINGS | WS_CLIPCHILDREN
        hwnd = user32.CreateWindowExW(0, cls_name, "FRANZ MEMORY", style, x, y, w_, h_, None, None, hinst, None)
        _raise_if_zero(hwnd, "CreateWindowExW")
        self.hwnd = hwnd

        # button
        btn = user32.CreateWindowExW(0, "BUTTON", "RESUME", WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON, 10, 10, 100, 34, hwnd, 1001, hinst, None)
        _raise_if_zero(btn, "CreateWindowExW(BUTTON)")
        self.btn = btn

        # edit
        edit_style = WS_CHILD | WS_VISIBLE | WS_VSCROLL | ES_MULTILINE | ES_AUTOVSCROLL
        edit = user32.CreateWindowExW(0, "RICHEDIT50W", "", edit_style, 10, 54, w_ - 20, h_ - 64, hwnd, 1002, hinst, None)
        _raise_if_zero(edit, "CreateWindowExW(EDIT)")
        self.edit = edit

        # White background inside the red-bordered memory window.
        user32.SendMessageW(edit, EM_SETBKGNDCOLOR, 0, HUD_EDIT_BG_COLOR)

        self._layout()
        self._set_paused(True)

        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
        self.ready.set()

        msg = MSG()
        while not self.stop.is_set():
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def __enter__(self) -> "HUD":
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


# =============================
# Agent Loop Helpers
# =============================

def compose_hud_text(story: str) -> str:
    s = (story or "").strip("\r\n")
    if not s:
        s = STARTING_STORY.strip("\r\n")
    return CONSTITUTION + s + "\n"


def extract_story_from_hud(hud_text: str) -> str:
    t = (hud_text or "")
    if STORY_MARKER_LINE not in t:
        return t.strip()
    parts = t.split(STORY_MARKER_LINE, 1)
    if len(parts) < 2:
        return t.strip()
    return parts[1].lstrip("\r\n ").strip()


def load_story_from_disk() -> str:
    try:
        if MEMORY_FILE.exists():
            s = MEMORY_FILE.read_text(encoding="utf-8", errors="ignore").strip()
            return s if s else STARTING_STORY.strip()
    except Exception:
        pass
    return STARTING_STORY.strip()


def save_story_to_disk(story: str) -> None:
    try:
        MEMORY_FILE.write_text((story or "").strip() + "\n", encoding="utf-8", newline="\n")
    except Exception:
        pass



def summarize_action(tool: str, args: dict[str, Any]) -> str:
    t = tool.lower()
    if t in ("click", "double_click", "right_click"):
        return f"{t}({args.get('x')},{args.get('y')})"
    if t == "drag":
        return f"drag({args.get('x1')},{args.get('y1')})->({args.get('x2')},{args.get('y2')})"
    if t == "scroll":
        return f"scroll(dy={args.get('dy')})"
    if t == "type_text":
        txt = str(args.get("text") or "")
        if len(txt) > 40:
            txt = txt[:40] + "…"
        return f"type_text({txt!r})"
    if t == "wait":
        return f"wait(ms={args.get('ms')})"
    return f"{t}(...)"




def _clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def story_similarity(a: str, b: str) -> float:
    wa = set(re.findall(r"[A-Za-z0-9']+", a.lower()))
    wb = set(re.findall(r"[A-Za-z0-9']+", b.lower()))
    if not wa and not wb:
        return 1.0
    if not wa or not wb:
        return 0.0
    inter = len(wa & wb)
    union = len(wa | wb)
    return inter / max(1, union)


def update_boredom(current: float, chain_tools: list[str], had_exec_error: bool, story_sim: float, last_action_repeated: bool) -> float:
    b = current
    noop = bool(chain_tools) and all(t == 'wait' for t in chain_tools)
    progressed = any(t in ('drag','type_text','scroll','click','double_click','right_click') for t in chain_tools) and not noop
    if noop:
        b += BOREDOM_INCREASE_NOOP
    if had_exec_error:
        b += BOREDOM_INCREASE_ERROR
    if story_sim >= 0.92 or last_action_repeated:
        b += BOREDOM_INCREASE_REPEAT
    if progressed:
        b -= BOREDOM_DECREASE_PROGRESS
    return _clamp01(b)


def log_jsonl(path: Path, obj: dict[str, Any]) -> None:
    line = json.dumps(obj, ensure_ascii=False)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line)
        f.write("\n")


def call_vlm(
    png: bytes,
    hud_story_tminus1: str,
    last_action_tminus1: str,
    recent_actions_tminusN: list[str],
    tool_choice: str,
    boredom_hormone: float,
) -> tuple[list[tuple[str, dict[str, Any]]], str]:
    # Returns (tool_calls, raw_content_fallback)
    recent_block = "\n".join(f"- {a}" for a in recent_actions_tminusN[-4:]) or "- (none)"

    user_text = (
        "Read your previous narrative and align it with the screenshot.\n"
        "Choose one intention and execute it as a CHAIN of 1..4 tool calls.\n"
        "If confidence < 0.65 then output EXACTLY 1 tool call.\n"
        "Intermediate calls must use story=\"\". Only the FINAL call includes the rewritten full narrative.\n"
        "Do NOT add extra JSON keys.\n\n"
        "NARRATIVE_MEMORY (T-1) below the line:\n"
        f"{hud_story_tminus1}\n\n"
        "LAST_ACTION_ANCHOR (T-1):\n"
        f"{last_action_tminus1}\n\n"
        "RECENT_ACTIONS (most recent last):\n"
        f"{recent_block}\n\n"
        "HORMONE BOREDOM (0.00..1.00, computed):\n"
        f"{boredom_hormone:.2f}\n"
    )

    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64.b64encode(png).decode('ascii')}"}},
                ],
            },
        ],
        "tools": TOOLS,
        "tool_choice": tool_choice,
        **SAMPLING,
    }

    req = urllib.request.Request(API_URL, json.dumps(payload).encode("utf-8"), {"Content-Type": "application/json"})
    data = json.load(urllib.request.urlopen(req, timeout=120))

    choices = data.get("choices") or []
    if not choices:
        raise ValueError("No choices in VLM response")

    msg = choices[0].get("message") or {}
    raw_content = str(msg.get("content") or "")
    tool_calls = msg.get("tool_calls") or []
    if not tool_calls:
        return [], raw_content

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

    return out, raw_content


def execute(tool: str, args: dict[str, Any], conv: Coord) -> None:
    match tool:
        case "click":
            x, y = conv.to_screen(float(args["x"]), float(args["y"]))
            mouse_click(x, y, conv)
        case "right_click":
            x, y = conv.to_screen(float(args["x"]), float(args["y"]))
            mouse_right_click(x, y, conv)
        case "double_click":
            x, y = conv.to_screen(float(args["x"]), float(args["y"]))
            mouse_double_click(x, y, conv)
        case "drag":
            x1, y1 = conv.to_screen(float(args["x1"]), float(args["y1"]))
            x2, y2 = conv.to_screen(float(args["x2"]), float(args["y2"]))
            mouse_drag(x1, y1, x2, y2, conv)
        case "type_text":
            type_text(str(args["text"]))
        case "scroll":
            scroll(float(args["dy"]))
        case "wait":
            time.sleep(max(1, int(args["ms"])) / 1000.0)


def main() -> None:
    parser = argparse.ArgumentParser(description="FRANZ agent (Ricoeur IDEM/IPSE/TELOS narrative identity + multi-tool chains + boredom hormone)")
    parser.add_argument("--res", choices=["low", "med", "high"], default="high", help="Model input resolution preset")
    parser.add_argument("--tool-choice", choices=["auto", "required"], default=TOOL_CHOICE, help="tool_choice sent to LM Studio")
    args = parser.parse_args()

    model_w, model_h = RES_PRESETS[args.res]
    sw, sh = user32.GetSystemMetrics(SM_CXSCREEN), user32.GetSystemMetrics(SM_CYSCREEN)
    conv = Coord(sw=sw, sh=sh)

    dump = DUMP_FOLDER / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    dump.mkdir(parents=True, exist_ok=True)
    events_path = dump / "events.jsonl"

    print(f"FRANZ | Screen: {sw}x{sh} | Model input: {model_w}x{model_h} | Model: {MODEL_NAME} | tool_choice: {args.tool_choice}")
    print(f"Dump: {dump}")
    print("PAUSED - edit story in the red memory window if desired, click RESUME to start")
    print("HUD: CTRL+Scroll to zoom text")

    with HUD() as hud:
        disk_story = load_story_from_disk()

        # If HUD is empty, populate it from disk. If it has content, keep it (manual edits allowed).
        if not hud.get_text().strip():
            hud.update(compose_hud_text(disk_story))

        # Wait for first resume, then lock in initial story.
        hud.wait()
        last_story = extract_story_from_hud(hud.get_text()) or disk_story
        save_story_to_disk(last_story)

        last_action = "None"
        recent_actions: list[str] = []
        boredom_hormone = BOREDOM_START

        step = 0
        while not hud.stop.is_set():
            hud.wait()
            if hud.stop.is_set():
                break

            step += 1
            ts = datetime.now().strftime("%H:%M:%S")

            bgra = capture_screen(sw, sh)
            png = encode_png(downsample_bgra(bgra, sw, sh, model_w, model_h), model_w, model_h)
            img_name = f"step{step:03d}.png"
            (dump / img_name).write_bytes(png)

            log_jsonl(
                events_path,
                {
                    "ts": ts,
                    "step": step,
                    "event": "screenshot",
                    "file": img_name,
                    "hud_tminus1": last_story,
                    "last_action_tminus1": last_action,
                    "boredom_hormone": round(boredom_hormone, 3),
                },
            )

            try:
                calls, raw_content = call_vlm(png, last_story, last_action, recent_actions, args.tool_choice, boredom_hormone)
            except Exception as e:
                print(f"[{ts}] {step:03d} | VLM ERROR: {e}")
                log_jsonl(events_path, {"ts": ts, "step": step, "event": "vlm_error", "error": str(e), "boredom_before": round(boredom_hormone,3)})
                boredom_hormone = _clamp01(boredom_hormone + 0.03)
                time.sleep(0.5)
                continue

            if not calls:
                # LM Studio could not parse tool calls; keep system stable and log raw content.
                preview = (raw_content or "").strip().replace("\r", "")
                if len(preview) > 800:
                    preview = preview[:800]
                print(f"[{ts}] {step:03d} | NO TOOL_CALLS PARSED | content_preview={preview!r}")
                log_jsonl(events_path, {"ts": ts, "step": step, "event": "no_tool_calls", "content_preview": preview, "boredom_before": round(boredom_hormone,3)})
                boredom_hormone = _clamp01(boredom_hormone + BOREDOM_INCREASE_NOOP)
                time.sleep(0.25)
                hud.update(compose_hud_text(last_story))
                continue

            prev_story = last_story
            prev_last_action = last_action
            chain_tools = [t for (t, _a) in calls]
            had_exec_error = False

            for idx, (tool, tool_args) in enumerate(calls, 1):
                # Update story only if present and non-empty (final call policy).
                story = tool_args.get("story")
                if isinstance(story, str) and story.strip():
                    last_story = story.strip()
                    save_story_to_disk(last_story)

                action_summary = summarize_action(tool, tool_args)
                print(f"[{ts}] {step:03d}.{idx:02d} | {action_summary}")

                ok = True
                err = ""
                try:
                    execute(tool, tool_args, conv)
                except Exception as e:
                    ok = False
                    had_exec_error = True
                    err = str(e)
                    print(f"[{ts}] {step:03d}.{idx:02d} | EXEC ERROR: {err} | args={tool_args}")

                # Maintain last-action anchor using the last SUCCESSFUL action.
                if ok:
                    last_action = action_summary
                recent_actions.append(f"{step:03d}.{idx:02d} {action_summary} {'OK' if ok else 'ERR'}")
                recent_actions = recent_actions[-12:]

                log_jsonl(
                    events_path,
                    {
                        "ts": ts,
                        "step": step,
                        "idx": idx,
                        "event": "tool",
                        "tool": tool,
                        "args": {k: v for k, v in tool_args.items() if k != "story"},
                        "story_present": bool(isinstance(story, str) and story.strip()),
                        "ok": ok,
                        "error": err,
                        "last_action": last_action,
                    },
                )

                if not ok:
                    break
                time.sleep(0.05)

            sim = story_similarity(prev_story, last_story)
            boredom_before = boredom_hormone
            boredom_hormone = update_boredom(boredom_hormone, chain_tools, had_exec_error, sim, last_action == prev_last_action)
            log_jsonl(events_path, {
                "ts": ts,
                "step": step,
                "event": "boredom_update",
                "boredom_before": round(boredom_before, 3),
                "boredom_after": round(boredom_hormone, 3),
                "story_similarity": round(sim, 3),
                "chain_tools": chain_tools,
                "had_exec_error": had_exec_error,
                "last_action_repeated": (last_action == prev_last_action),
            })

            hud.update(compose_hud_text(last_story))
            time.sleep(0.2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nFRANZ stops.")
