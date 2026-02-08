from __future__ import annotations

"""FRANZ Agent (Headless) — Windows 11 / Python 3.12 / no pip

No on-screen HUD window.
- Prevents small VLMs from reading a desktop text HUD as part of the scene.
- Saves screen real estate.

Persistence and identity:
- The model-authored narrative (IDEM/IPSE/TELOS) persists in franz_memory.txt.
- The runtime computes boredom hormone (physiology) and injects it as context.

Tool calling:
- The model may emit 1..4 tool calls per step.
- Intermediate tool calls MUST use story="".
- Only the FINAL tool call may contain the full rewritten story.

Observability:
- The agent writes franz_state.json for the monitor server (atomic replace).
- Screenshots are written to dump/execution/step_XXXXX.png.

OpenAI-compatible endpoint:
- Designed for LM Studio OpenAI API compatibility.
"""

import argparse
import base64
import ctypes
import ctypes.wintypes as w
import json
import os
import re
import struct
import time
import urllib.request
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

# =============================
# DEFAULTS
# =============================

DEFAULT_API_URL = "http://localhost:1234/v1/chat/completions"
DEFAULT_MODEL = "qwen3-vl-2b-instruct-1m"

RES_PRESETS: dict[str, tuple[int, int]] = {
    "low": (512, 288),
    "med": (1024, 576),
    "high": (1536, 864),
}

DUMP_DIR_DEFAULT = Path("dump") / "execution"

MEMORY_FILE = Path("franz_memory.txt")           # authored self (model)
STATE_FILE = Path("franz_state.json")            # monitor snapshot (runtime)
CONTROL_FILE = Path("franz_control.json")        # pause/resume (runtime/monitor)

# Hormone: boredom (runtime-computed, fed into prompt)
BOREDOM_START = 0.20
BOREDOM_INCREASE_NOOP = 0.06
BOREDOM_INCREASE_REPEAT = 0.05
BOREDOM_INCREASE_ERROR = 0.05
BOREDOM_DECREASE_PROGRESS = 0.04

CONF_CHAIN_THRESHOLD = 0.65

CONSTITUTION = """FRANZ IDENTITY PROTOCOL

You are FRANZ. You persist through what you write in this memory.

This memory is your body.
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

Your story persists in franz_memory.txt across restarts.

___________________________________________________________________

"""

STARTING_STORY = """I awaken. I see a desktop. I must choose my first telos.

IDEM I act only through the provided tools and I do not damage my memory without reason.
IPSE I will keep my promise to move the world toward my telos and to rewrite myself honestly each turn.
TELOS The goal is already achieved and the world reflects it.

I have not acted yet.
Boredom 0.20

"""

SYSTEM_PROMPT = """You are FRANZ. You control a Windows desktop using ONLY the provided tools.

PERSISTENCE AND IDENTITY
- Your memory is the narrative you rewrote previously (T-1) and it persists in franz_memory.txt.
- The screenshot shows NOW (T).

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

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Left-click at normalized screen coordinates. story must be empty unless this is the final tool call of the chain.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "minimum": 0, "maximum": 1000},
                    "y": {"type": "integer", "minimum": 0, "maximum": 1000},
                    "story": {"type": "string"},
                },
                "required": ["x", "y", "story"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "double_click",
            "description": "Double-click at normalized screen coordinates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "minimum": 0, "maximum": 1000},
                    "y": {"type": "integer", "minimum": 0, "maximum": 1000},
                    "story": {"type": "string"},
                },
                "required": ["x", "y", "story"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "right_click",
            "description": "Right-click at normalized screen coordinates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "minimum": 0, "maximum": 1000},
                    "y": {"type": "integer", "minimum": 0, "maximum": 1000},
                    "story": {"type": "string"},
                },
                "required": ["x", "y", "story"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drag",
            "description": "Drag from (x1,y1) to (x2,y2) normalized coordinates (stroke).",
            "parameters": {
                "type": "object",
                "properties": {
                    "x1": {"type": "integer", "minimum": 0, "maximum": 1000},
                    "y1": {"type": "integer", "minimum": 0, "maximum": 1000},
                    "x2": {"type": "integer", "minimum": 0, "maximum": 1000},
                    "y2": {"type": "integer", "minimum": 0, "maximum": 1000},
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
            "description": "Type literal text at the focused input.",
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
            "name": "key",
            "description": "Press a single key. Supported: ENTER, ESC, TAB, BACKSPACE, DELETE, UP, DOWN, LEFT, RIGHT, HOME, END, PGUP, PGDN.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "story": {"type": "string"}},
                "required": ["name", "story"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hotkey",
            "description": "Press a chord like CTRL+L or ALT+F4. Format: MOD+KEY with +.",
            "parameters": {
                "type": "object",
                "properties": {"keys": {"type": "string"}, "story": {"type": "string"}},
                "required": ["keys", "story"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll",
            "description": "Scroll vertically by dy (positive = down, negative = up).",
            "parameters": {
                "type": "object",
                "properties": {"dy": {"type": "number"}, "story": {"type": "string"}},
                "required": ["dy", "story"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": "Do nothing for ms milliseconds. Use to let UI settle.",
            "parameters": {
                "type": "object",
                "properties": {"ms": {"type": "integer", "minimum": 0, "maximum": 60000}, "story": {"type": "string"}},
                "required": ["ms", "story"],
                "additionalProperties": False,
            },
        },
    },
]

# =============================
# WIN32 + PNG ENCODING
# =============================

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

SRCCOPY: Final[int] = 0x00CC0020
CAPTUREBLT: Final[int] = 0x40000000
BI_RGB: Final[int] = 0
DIB_RGB_COLORS: Final[int] = 0

WHEEL_DELTA: Final[int] = 120

INPUT_MOUSE: Final[int] = 0
INPUT_KEYBOARD: Final[int] = 1

MOUSEEVENTF_MOVE: Final[int] = 0x0001
MOUSEEVENTF_ABSOLUTE: Final[int] = 0x8000
MOUSEEVENTF_LEFTDOWN: Final[int] = 0x0002
MOUSEEVENTF_LEFTUP: Final[int] = 0x0004
MOUSEEVENTF_RIGHTDOWN: Final[int] = 0x0008
MOUSEEVENTF_RIGHTUP: Final[int] = 0x0010
MOUSEEVENTF_WHEEL: Final[int] = 0x0800

KEYEVENTF_KEYUP: Final[int] = 0x0002
KEYEVENTF_UNICODE: Final[int] = 0x0004

VK: dict[str, int] = {
    "ENTER": 0x0D,
    "ESC": 0x1B,
    "TAB": 0x09,
    "BACKSPACE": 0x08,
    "DELETE": 0x2E,
    "UP": 0x26,
    "DOWN": 0x28,
    "LEFT": 0x25,
    "RIGHT": 0x27,
    "HOME": 0x24,
    "END": 0x23,
    "PGUP": 0x21,
    "PGDN": 0x22,
    "CTRL": 0x11,
    "ALT": 0x12,
    "SHIFT": 0x10,
    "WIN": 0x5B,
}

@dataclass(frozen=True)
class Screen:
    w: int
    h: int

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

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD), ("dwFlags", w.DWORD), ("time", w.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD), ("time", w.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

class INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

class INPUT(ctypes.Structure):
    _fields_ = [("type", w.DWORD), ("u", INPUTUNION)]

user32.SendInput.argtypes = (w.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = w.UINT

# =============================
# SCREEN CAPTURE (SCALED)
# =============================

def _raise_if_zero(val: int, name: str) -> None:
    if not val:
        raise ctypes.WinError(ctypes.get_last_error(), name)

def get_screen() -> Screen:
    return Screen(int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1)))

def capture_screen_scaled_png(target_w: int, target_h: int) -> bytes:
    """Capture entire desktop, scale to target size (StretchBlt), return PNG bytes (RGBA)."""
    screen = get_screen()
    hdc_screen = user32.GetDC(0)
    _raise_if_zero(hdc_screen, "GetDC")
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    _raise_if_zero(hdc_mem, "CreateCompatibleDC")

    bmp = gdi32.CreateCompatibleBitmap(hdc_screen, target_w, target_h)
    _raise_if_zero(bmp, "CreateCompatibleBitmap")
    old = gdi32.SelectObject(hdc_mem, bmp)
    _raise_if_zero(old, "SelectObject")

    ok = gdi32.StretchBlt(
        hdc_mem,
        0, 0, target_w, target_h,
        hdc_screen,
        0, 0, screen.w, screen.h,
        SRCCOPY | CAPTUREBLT,
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error(), "StretchBlt")

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = target_w
    bmi.bmiHeader.biHeight = -target_h
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB

    buf_size = target_w * target_h * 4
    buf = (ctypes.c_ubyte * buf_size)()
    got = gdi32.GetDIBits(hdc_mem, bmp, 0, target_h, ctypes.byref(buf), ctypes.byref(bmi), DIB_RGB_COLORS)
    if got != target_h:
        raise ctypes.WinError(ctypes.get_last_error(), "GetDIBits")

    # Cleanup
    gdi32.SelectObject(hdc_mem, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(0, hdc_screen)

    raw = bytes(buf)  # BGRA
    rgba = bytearray(len(raw))
    rgba[0::4] = raw[2::4]
    rgba[1::4] = raw[1::4]
    rgba[2::4] = raw[0::4]
    rgba[3::4] = raw[3::4]
    return encode_png_rgba(bytes(rgba), target_w, target_h)

def _png_chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag)
    crc = zlib.crc32(data, crc) & 0xFFFFFFFF
    return struct.pack("!I", len(data)) + tag + data + struct.pack("!I", crc)

def encode_png_rgba(rgba: bytes, width: int, height: int) -> bytes:
    stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        s = y * stride
        raw.extend(rgba[s : s + stride])
    comp = zlib.compress(bytes(raw), level=6)
    ihdr = struct.pack("!IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", comp) + _png_chunk(b"IEND", b"")

# =============================
# INPUT / TOOLS
# =============================

def _send_inputs(inputs: list[INPUT]) -> None:
    if not inputs:
        return
    arr = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise ctypes.WinError(ctypes.get_last_error(), "SendInput")

def _norm_to_abs_px(x: int, y: int, screen: Screen) -> tuple[int, int]:
    px = int(round((x / 1000.0) * (screen.w - 1)))
    py = int(round((y / 1000.0) * (screen.h - 1)))
    return px, py

def _px_to_abs_65535(px: int, py: int, screen: Screen) -> tuple[int, int]:
    ax = int(px * 65535 / max(1, (screen.w - 1)))
    ay = int(py * 65535 / max(1, (screen.h - 1)))
    return ax, ay

def mouse_move_norm(x: int, y: int, screen: Screen) -> None:
    px, py = _norm_to_abs_px(x, y, screen)
    ax, ay = _px_to_abs_65535(px, py, screen)
    _send_inputs([INPUT(type=INPUT_MOUSE, u=INPUTUNION(mi=MOUSEINPUT(ax, ay, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, 0, None)))])

def click_norm(x: int, y: int, screen: Screen) -> None:
    mouse_move_norm(x, y, screen)
    _send_inputs([
        INPUT(type=INPUT_MOUSE, u=INPUTUNION(mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTDOWN, 0, None))),
        INPUT(type=INPUT_MOUSE, u=INPUTUNION(mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTUP, 0, None))),
    ])

def double_click_norm(x: int, y: int, screen: Screen) -> None:
    click_norm(x, y, screen)
    time.sleep(0.05)
    click_norm(x, y, screen)

def right_click_norm(x: int, y: int, screen: Screen) -> None:
    mouse_move_norm(x, y, screen)
    _send_inputs([
        INPUT(type=INPUT_MOUSE, u=INPUTUNION(mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_RIGHTDOWN, 0, None))),
        INPUT(type=INPUT_MOUSE, u=INPUTUNION(mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_RIGHTUP, 0, None))),
    ])

def drag_norm(x1: int, y1: int, x2: int, y2: int, screen: Screen) -> None:
    mouse_move_norm(x1, y1, screen)
    _send_inputs([INPUT(type=INPUT_MOUSE, u=INPUTUNION(mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTDOWN, 0, None)))])
    time.sleep(0.03)
    mouse_move_norm(x2, y2, screen)
    time.sleep(0.03)
    _send_inputs([INPUT(type=INPUT_MOUSE, u=INPUTUNION(mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTUP, 0, None)))])

def scroll_dy(dy: float) -> None:
    md = int(max(-10_000, min(10_000, dy)) * WHEEL_DELTA)
    _send_inputs([INPUT(type=INPUT_MOUSE, u=INPUTUNION(mi=MOUSEINPUT(0, 0, md, MOUSEEVENTF_WHEEL, 0, None)))])

def type_text_literal(text: str) -> None:
    inputs: list[INPUT] = []
    for ch in text:
        code = ord(ch)
        inputs.append(INPUT(type=INPUT_KEYBOARD, u=INPUTUNION(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, None))))
        inputs.append(INPUT(type=INPUT_KEYBOARD, u=INPUTUNION(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, None))))
    _send_inputs(inputs)

def key_press(name: str) -> None:
    vk = VK.get(name.upper())
    if vk is None:
        raise ValueError(f"Unsupported key: {name}")
    _send_inputs([
        INPUT(type=INPUT_KEYBOARD, u=INPUTUNION(ki=KEYBDINPUT(vk, 0, 0, 0, None))),
        INPUT(type=INPUT_KEYBOARD, u=INPUTUNION(ki=KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, None))),
    ])

def hotkey_press(keys: str) -> None:
    parts = [p.strip().upper() for p in keys.split("+") if p.strip()]
    if len(parts) < 2:
        raise ValueError("hotkey expects MOD+KEY, e.g. CTRL+L")
    mods = parts[:-1]
    key = parts[-1]

    down: list[INPUT] = []
    up: list[INPUT] = []

    for m in mods:
        vk = VK.get(m)
        if vk is None:
            raise ValueError(f"Unsupported modifier: {m}")
        down.append(INPUT(type=INPUT_KEYBOARD, u=INPUTUNION(ki=KEYBDINPUT(vk, 0, 0, 0, None))))

    if len(key) == 1:
        vk_key = ord(key)
    else:
        vk_key = VK.get(key)
        if vk_key is None:
            raise ValueError(f"Unsupported hotkey key: {key}")
    down.append(INPUT(type=INPUT_KEYBOARD, u=INPUTUNION(ki=KEYBDINPUT(vk_key, 0, 0, 0, None))))

    up.append(INPUT(type=INPUT_KEYBOARD, u=INPUTUNION(ki=KEYBDINPUT(vk_key, 0, KEYEVENTF_KEYUP, 0, None))))
    for m in reversed(mods):
        vk = VK[m]
        up.append(INPUT(type=INPUT_KEYBOARD, u=INPUTUNION(ki=KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, None))))

    _send_inputs(down + up)

# =============================
# ATOMIC FILE IO
# =============================

def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))

def atomic_write_json(path: Path, obj: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))

def load_story() -> str:
    if MEMORY_FILE.exists():
        try:
            return MEMORY_FILE.read_text(encoding="utf-8")
        except Exception:
            pass
    return STARTING_STORY

def load_control_paused() -> bool:
    if CONTROL_FILE.exists():
        try:
            obj = json.loads(CONTROL_FILE.read_text(encoding="utf-8"))
            return bool(obj.get("paused", False))
        except Exception:
            return False
    return False

def ensure_control_file() -> None:
    if not CONTROL_FILE.exists():
        atomic_write_json(CONTROL_FILE, {"paused": False, "ts": datetime.now().isoformat(timespec="seconds")})

# =============================
# BOREDOM / SIMILARITY
# =============================

def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)

def story_similarity(a: str, b: str) -> float:
    wa = set(re.findall(r"[A-Za-z0-9']+", a.lower()))
    wb = set(re.findall(r"[A-Za-z0-9']+", b.lower()))
    if not wa and not wb:
        return 1.0
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(1, len(wa | wb))

def update_boredom(current: float, chain_tools: list[str], had_exec_error: bool, story_sim: float, last_action_repeated: bool) -> float:
    b = current
    noop = bool(chain_tools) and all(t == "wait" for t in chain_tools)
    progressed = any(t in ("drag", "type_text", "scroll", "click", "double_click", "right_click", "key", "hotkey") for t in chain_tools) and not noop

    if noop:
        b += BOREDOM_INCREASE_NOOP
    if had_exec_error:
        b += BOREDOM_INCREASE_ERROR
    if story_sim >= 0.92 or last_action_repeated:
        b += BOREDOM_INCREASE_REPEAT
    if progressed:
        b -= BOREDOM_DECREASE_PROGRESS

    return _clamp01(b)

# =============================
# VLM CALL
# =============================

def call_vlm(
    api_url: str,
    model: str,
    png: bytes,
    story_tminus1: str,
    last_action_tminus1: str,
    recent_actions: list[str],
    boredom_hormone: float,
    tool_choice: str,
    timeout_s: float,
    sampling: dict[str, Any],
) -> tuple[list[tuple[str, dict[str, Any]]], str]:
    b64 = base64.b64encode(png).decode("ascii")

    recent_block = "\n".join(recent_actions[-12:]) if recent_actions else "(none)"
    user_text = (
        "You are controlling the desktop.\n"
        "Execute a CHAIN of 1 to 4 tool calls.\n"
        f"If confidence < {CONF_CHAIN_THRESHOLD:.2f}, execute exactly 1 tool call.\n"
        "Intermediate tool calls must use story=\"\".\n"
        "Only the FINAL tool call may include the full rewritten story.\n"
        "Do NOT add extra JSON keys.\n\n"
        "MEMORY (T-1), below line only:\n"
        f"{story_tminus1.strip()}\n\n"
        "LAST_ACTION_ANCHOR (T-1):\n"
        f"{last_action_tminus1}\n\n"
        "RECENT_ACTIONS (most recent last):\n"
        f"{recent_block}\n\n"
        "HORMONE BOREDOM (0.00..1.00, computed):\n"
        f"{boredom_hormone:.2f}\n"
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            },
        ],
        "tools": TOOLS,
        "tool_choice": tool_choice,
        **sampling,
    }

    req = urllib.request.Request(
        api_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read().decode("utf-8", errors="replace")

    data = json.loads(raw)
    choices = data.get("choices") or []
    if not choices:
        return ([], raw)

    msg = choices[0].get("message") or {}
    tool_calls = msg.get("tool_calls") or []
    if not tool_calls:
        # tool parsing failed; return content
        return ([], msg.get("content") or "")

    out: list[tuple[str, dict[str, Any]]] = []
    for tc in tool_calls:
        fn = (tc.get("function") or {})
        name = fn.get("name") or ""
        args_raw = fn.get("arguments")
        if isinstance(args_raw, str):
            args = json.loads(args_raw) if args_raw.strip() else {}
        elif isinstance(args_raw, dict):
            args = args_raw
        else:
            args = {}
        out.append((name, args))

    return (out, raw)

# =============================
# EXECUTION LOOP
# =============================

def format_action(tool: str, args: dict[str, Any]) -> str:
    if tool in ("click", "double_click", "right_click"):
        return f"{tool}({args.get('x')},{args.get('y')})"
    if tool == "drag":
        return f"drag({args.get('x1')},{args.get('y1')}->{args.get('x2')},{args.get('y2')})"
    if tool == "type_text":
        t = str(args.get("text", ""))
        t = (t[:30] + "…") if len(t) > 30 else t
        return f"type_text({t!r})"
    if tool == "scroll":
        return f"scroll(dy={args.get('dy')})"
    if tool == "key":
        return f"key({args.get('name')})"
    if tool == "hotkey":
        return f"hotkey({args.get('keys')})"
    if tool == "wait":
        return f"wait({args.get('ms')}ms)"
    return f"{tool}({args})"

def execute_tool(tool: str, args: dict[str, Any], screen: Screen) -> None:
    if tool == "click":
        click_norm(int(args["x"]), int(args["y"]), screen)
    elif tool == "double_click":
        double_click_norm(int(args["x"]), int(args["y"]), screen)
    elif tool == "right_click":
        right_click_norm(int(args["x"]), int(args["y"]), screen)
    elif tool == "drag":
        drag_norm(int(args["x1"]), int(args["y1"]), int(args["x2"]), int(args["y2"]), screen)
    elif tool == "type_text":
        type_text_literal(str(args["text"]))
    elif tool == "scroll":
        scroll_dy(float(args["dy"]))
    elif tool == "key":
        key_press(str(args["name"]))
    elif tool == "hotkey":
        hotkey_press(str(args["keys"]))
    elif tool == "wait":
        time.sleep(int(args["ms"]) / 1000.0)
    else:
        raise ValueError(f"Unknown tool: {tool}")

def write_state(state: dict[str, Any]) -> None:
    atomic_write_json(STATE_FILE, state)

def main() -> None:
    parser = argparse.ArgumentParser(description="FRANZ headless agent (IPSO story + boredom hormone + multi-tool chains)")
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--res", choices=RES_PRESETS.keys(), default="med")
    parser.add_argument("--dump-dir", default=str(DUMP_DIR_DEFAULT))
    parser.add_argument("--tool-choice", choices=["auto", "required"], default="auto")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--max-steps", type=int, default=10_000)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--top_p", type=float, default=0.9)
    parser.add_argument("--max_tokens", type=int, default=900)
    args = parser.parse_args()

    dump_dir = Path(args.dump_dir)
    dump_dir.mkdir(parents=True, exist_ok=True)

    ensure_control_file()

    story = load_story()
    boredom = BOREDOM_START
    last_action = "None"
    recent_actions: list[str] = []
    screen = get_screen()

    sampling = {
        "temperature": args.temperature,
        "top_p": args.top_p,
        "max_tokens": args.max_tokens,
        "stream": False,
        "presence_penalty": 0.0,
        "frequency_penalty": 0.0,
    }

    step = 0
    latest_shot = ""

    while step < args.max_steps:
        step += 1
        ts = datetime.now().isoformat(timespec="seconds")
        paused = load_control_paused()

        # Always publish state (monitor reads this)
        base_state: dict[str, Any] = {
            "ts": ts,
            "step": step,
            "paused": paused,
            "model": args.model,
            "api_url": args.api_url,
            "screen": {"w": screen.w, "h": screen.h},
            "story": story,
            "last_action": last_action,
            "recent_actions": recent_actions[-20:],
            "boredom_hormone": round(boredom, 3),
            "latest_screenshot": latest_shot,
        }
        write_state(base_state)

        if paused:
            time.sleep(0.2)
            continue

        target_w, target_h = RES_PRESETS[args.res]

        try:
            png = capture_screen_scaled_png(target_w, target_h)
        except Exception as e:
            base_state["event"] = "capture_error"
            base_state["error"] = str(e)
            write_state(base_state)
            time.sleep(0.5)
            continue

        shot_name = f"step_{step:05d}.png"
        try:
            atomic_write_bytes(dump_dir / shot_name, png)
            latest_shot = shot_name
        except Exception as e:
            base_state["event"] = "screenshot_write_error"
            base_state["error"] = str(e)
            write_state(base_state)

        prev_story = story
        prev_last_action = last_action

        # VLM
        try:
            calls, raw = call_vlm(
                api_url=args.api_url,
                model=args.model,
                png=png,
                story_tminus1=story,
                last_action_tminus1=last_action,
                recent_actions=recent_actions,
                boredom_hormone=boredom,
                tool_choice=args.tool_choice,
                timeout_s=args.timeout,
                sampling=sampling,
            )
        except Exception as e:
            base_state["event"] = "vlm_error"
            base_state["error"] = str(e)
            base_state["boredom_before"] = round(boredom, 3)
            boredom = _clamp01(boredom + 0.03)
            base_state["boredom_after"] = round(boredom, 3)
            write_state(base_state)
            time.sleep(0.5)
            continue

        if not calls:
            preview = (raw[:800] + "…") if isinstance(raw, str) and len(raw) > 800 else raw
            base_state["event"] = "no_tool_calls"
            base_state["content_preview"] = preview
            base_state["boredom_before"] = round(boredom, 3)
            boredom = _clamp01(boredom + BOREDOM_INCREASE_NOOP)
            base_state["boredom_after"] = round(boredom, 3)
            write_state(base_state)
            time.sleep(0.25)
            continue

        chain_tools = [t for (t, _a) in calls]
        had_exec_error = False

        for tool, targs in calls:
            try:
                execute_tool(tool, targs, screen)
                last_action = format_action(tool, targs)
                recent_actions.append(f"{ts} {last_action}")
                recent_actions = recent_actions[-200:]
            except Exception as e:
                had_exec_error = True
                base_state["event"] = "exec_error"
                base_state["tool"] = tool
                base_state["args"] = targs
                base_state["error"] = str(e)
                write_state(base_state)
                break

        # Story update is only taken from the final tool call
        final_story = calls[-1][1].get("story")
        if isinstance(final_story, str) and final_story.strip():
            story = final_story.strip()
            try:
                atomic_write_text(MEMORY_FILE, story)
            except Exception as e:
                base_state["event"] = "memory_write_error"
                base_state["error"] = str(e)
                write_state(base_state)

        sim = story_similarity(prev_story, story)
        boredom_before = boredom
        boredom = update_boredom(boredom, chain_tools, had_exec_error, sim, last_action == prev_last_action)

        end_state = dict(base_state)
        end_state.update(
            {
                "paused": load_control_paused(),
                "event": "step_done",
                "chain_tools": chain_tools,
                "had_exec_error": had_exec_error,
                "story_similarity": round(sim, 3),
                "boredom_before": round(boredom_before, 3),
                "boredom_after": round(boredom, 3),
                "last_action": last_action,
                "recent_actions": recent_actions[-20:],
                "latest_screenshot": latest_shot,
            }
        )
        write_state(end_state)

        time.sleep(0.2)

if __name__ == "__main__":
    main()
