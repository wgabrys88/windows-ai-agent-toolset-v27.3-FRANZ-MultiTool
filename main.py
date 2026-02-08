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
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

API_URL = "http://localhost:1234/v1/chat/completions"
MODEL_NAME = "qwen3-vl-2b-instruct-1m"
DUMP_FOLDER = Path("dump")
RES_PRESETS = {"low": (512, 288), "med": (1024, 576), "high": (1536, 864)}
MEMORY_FILE = Path("franz_memory.txt")
BACKSTORY_FILE = Path("franz_backstory.txt")
HTTP_PORT = 8080

SAMPLING = {
    "temperature": 1.2,
    "top_p": 0.8,
    "top_k": 20,
    "max_tokens": 700,
    "stream": False,
    "presence_penalty": 1.2,
    "frequency_penalty": 0.2,
    "repeat_penalty": 1.2,
    "seed": 42,
}

BOREDOM_START = 0.20
BOREDOM_INCREASE_NOOP = 0.06
BOREDOM_INCREASE_REPEAT = 0.05
BOREDOM_INCREASE_ERROR = 0.05
BOREDOM_DECREASE_PROGRESS = 0.04

STORY_MAX_LEN = 1500
BACKSTORY_MAX_LEN = 3000
SESSION_THRESHOLD_HOURS = 12

CONSTITUTION = """FRANZ IDENTITY PROTOCOL

You persist through what you write in your memory. Your desktop is where you act.

Every turn:
1. Read your backstory (long-term compressed memories), your previous narrative, and the screenshot.
2. Choose one intention and execute it (may be a short chain of tool calls).
3. In the story field of the FINAL tool call, rewrite your complete narrative:
   - IDEM your unchanging constraints
   - IPSE your promise
   - TELOS your goal written as if already achieved
   - Add one short line describing what you just did (include WHERE coords)
   - End with Confidence 0.00..1.00 and Boredom 0.00..1.00

Boredom is a hormone of stagnation. When >= 0.70, resist idle commentary and choose actions that advance TELOS or reveal new information.

Write naturally. Summarize old events if needed. Keep story <= 1500 chars.

___________________________________________________________________

"""

STARTING_STORY = """I awaken. I see a desktop. I must choose my first telos.

IDEM I act only through the provided tools and I persist through writing my narrative honestly each turn.
IPSE I will keep my promise to move the world toward my telos and to rewrite myself honestly.
TELOS The goal is already achieved and the world reflects it.

I have not acted yet.
Confidence 0.50
Boredom 0.20
"""

SYSTEM_PROMPT = """You are FRANZ. You control a Windows desktop using ONLY the provided tools.

PERSISTENCE AND IDENTITY
- Your narrative is your memory. The runtime saves it to franz_memory.txt across restarts.
- Your backstory is compressed long-term memory from previous sessions.
- The screenshot shows NOW (T). Your narrative is memory (T-1).

ONE INTENTION, CHAINED EXECUTION
- Choose one intention per step. One intention may be executed as a CHAIN of 1 to 4 tool calls (ordered).
- If confidence is < 0.65, output EXACTLY 1 tool call (safe exploratory action or wait).
- When boredom is high (>= 0.70), resist idle observation and pick actions that advance TELOS or disclose new information.

STORY (ONLY IN FINAL TOOL CALL)
- Intermediate tool calls MUST use story="".
- Only the FINAL tool call may include a non-empty story.
- In that final story, rewrite your complete narrative:
  1) IDEM paragraph (no colon) stating unchanging constraints.
  2) IPSE paragraph (no colon) stating your promise.
  3) TELOS paragraph (no colon) stating goal as if already achieved.
  4) Then add 1-3 short lines describing what you just did (include WHERE coords).
  5) End with Confidence 0.00..1.00 and Boredom 0.00..1.00
- Keep the entire story <= 1500 characters. Summarize older events aggressively.

TELOS EVOLUTION
- When TELOS is achieved with high confidence (>= 0.85) and low boredom (<= 0.30), propose a NEW TELOS in your next story.
- Use propose_telos tool to declare the new goal before rewriting your story.

HUMAN INTERACTION
- Use ask_human tool to request information or clarification when confidence is low or you face ambiguity.
- Use tell_human tool to communicate progress or important decisions.
- Wait for human response before proceeding if the question is critical.

JSON STRICTNESS
- Tool-call arguments MUST be a single JSON object.
- MUST contain ONLY keys defined in the tool schema (no extra keys).
- Coordinates are normalized integers 0..1000 (0,0 top-left; 1000,1000 bottom-right).

ACTION POLICY
- Prefer drag for drawing/painting strokes instead of repeated clicking.
- Use wait(ms) to allow UI to settle when needed.
"""

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
    _fields_ = [
        ("dx", w.LONG),
        ("dy", w.LONG),
        ("mouseData", w.DWORD),
        ("dwFlags", w.DWORD),
        ("time", w.DWORD),
        ("dwExtraInfo", w.ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", w.WORD),
        ("wScan", w.WORD),
        ("dwFlags", w.DWORD),
        ("time", w.DWORD),
        ("dwExtraInfo", w.ULONG_PTR),
    ]


class INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", w.DWORD), ("u", INPUTUNION)]


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
    bmi.bmiHeader.biHeight = -sh
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
    rgba = bytearray(len(bgra))
    for i in range(0, len(bgra), 4):
        b, g, r, a = bgra[i : i + 4]
        rgba[i : i + 4] = bytes((r, g, b, a))
    stride = w_ * 4
    raw = bytearray()
    for y in range(h_):
        raw.append(0)
        raw += rgba[y * stride : (y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w_, h_, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(raw), level=6)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _send_inputs(inputs: list[INPUT]) -> None:
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
    if sent != n:
        raise ctypes.WinError(ctypes.get_last_error(), "SendInput")


def _abs_mouse(sw: int, sh: int, x: int, y: int) -> tuple[int, int]:
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
    inputs = []
    for ch in text:
        code = ord(ch)
        inputs.append(INPUT(type=INPUT_KEYBOARD, u=INPUTUNION(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, 0))))
        inputs.append(INPUT(type=INPUT_KEYBOARD, u=INPUTUNION(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0))))
    if inputs:
        _send_inputs(inputs)


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
    {
        "type": "function",
        "function": {
            "name": "ask_human",
            "description": "Ask the human a question and wait for response. story must be empty except for the FINAL tool call in a chain.",
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string"}, "story": {"type": "string"}},
                "required": ["question", "story"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tell_human",
            "description": "Tell the human something important. story must be empty except for the FINAL tool call in a chain.",
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string"}, "story": {"type": "string"}},
                "required": ["message", "story"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_telos",
            "description": "Propose a new TELOS goal when the current one is achieved. story must be empty except for the FINAL tool call in a chain.",
            "parameters": {
                "type": "object",
                "properties": {"new_telos": {"type": "string"}, "story": {"type": "string"}},
                "required": ["new_telos", "story"],
                "additionalProperties": False,
            },
        },
    },
]

TOOL_NAME_SET = {str(t["function"]["name"]).strip().lower() for t in TOOLS}


class HumanComm:
    def __init__(self):
        self.pending_question = None
        self.pending_answer = None
        self.messages = []
        self.lock = threading.Lock()

    def ask(self, question: str) -> str:
        with self.lock:
            self.pending_question = question
            self.pending_answer = None
            self.messages.append({"type": "question", "text": question, "ts": datetime.now().isoformat()})
        while True:
            with self.lock:
                if self.pending_answer is not None:
                    answer = self.pending_answer
                    self.pending_question = None
                    self.pending_answer = None
                    return answer
            time.sleep(0.5)

    def tell(self, message: str) -> None:
        with self.lock:
            self.messages.append({"type": "message", "text": message, "ts": datetime.now().isoformat()})

    def answer(self, text: str) -> None:
        with self.lock:
            self.pending_answer = text

    def get_state(self) -> dict:
        with self.lock:
            return {
                "pending_question": self.pending_question,
                "messages": self.messages[-20:],
            }


class HTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            html = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>FRANZ Remote</title>
    <style>
        body { margin: 0; padding: 10px; font-family: monospace; background: #000; color: #0f0; }
        #memory { white-space: pre-wrap; margin-bottom: 20px; border: 1px solid #0f0; padding: 10px; }
        #screenshot { width: 100%; border: 1px solid #0f0; }
        #comm { margin-top: 20px; border: 1px solid #0f0; padding: 10px; }
        .question { color: #ff0; }
        input, button { background: #000; color: #0f0; border: 1px solid #0f0; padding: 5px; }
    </style>
</head>
<body>
    <h2>FRANZ MEMORY</h2>
    <div id="memory"></div>
    <h2>DESKTOP</h2>
    <img id="screenshot" src="/screenshot">
    <div id="comm"></div>
    <script>
        setInterval(() => {
            fetch('/state').then(r => r.json()).then(d => {
                document.getElementById('memory').textContent = d.memory;
                document.getElementById('screenshot').src = '/screenshot?' + Date.now();
                let commHtml = '';
                if (d.pending_question) {
                    commHtml += '<div class="question">Q: ' + d.pending_question + '</div>';
                    commHtml += '<input type="text" id="answer" placeholder="Answer">';
                    commHtml += '<button onclick="sendAnswer()">Submit</button>';
                }
                d.messages.forEach(m => {
                    commHtml += '<div>[' + m.ts.substring(11, 19) + '] ' + m.type + ': ' + m.text + '</div>';
                });
                document.getElementById('comm').innerHTML = commHtml;
            });
        }, 1000);
        function sendAnswer() {
            const answer = document.getElementById('answer').value;
            fetch('/answer', {method: 'POST', body: answer});
        }
    </script>
</body>
</html>"""
            self.wfile.write(html.encode())
        elif self.path == "/state":
            state = {
                "memory": self.server.memory_text,
                "pending_question": self.server.human_comm.get_state()["pending_question"],
                "messages": self.server.human_comm.get_state()["messages"],
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(state).encode())
        elif self.path.startswith("/screenshot"):
            if self.server.latest_screenshot:
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.end_headers()
                self.wfile.write(self.server.latest_screenshot)
            else:
                self.send_response(404)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/answer":
            length = int(self.headers.get("Content-Length", 0))
            answer = self.rfile.read(length).decode("utf-8")
            self.server.human_comm.answer(answer)
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()


class RemoteServer:
    def __init__(self, port: int, human_comm: HumanComm):
        self.port = port
        self.human_comm = human_comm
        self.memory_text = ""
        self.latest_screenshot = None
        self.server = None
        self.thread = None

    def update_memory(self, text: str):
        self.memory_text = text

    def update_screenshot(self, png: bytes):
        self.latest_screenshot = png

    def start(self):
        self.server = HTTPServer(("0.0.0.0", self.port), HTTPHandler)
        self.server.memory_text = ""
        self.server.latest_screenshot = None
        self.server.human_comm = self.human_comm
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        self.server.memory_text = self.memory_text
        self.server.latest_screenshot = self.latest_screenshot
        self.server.serve_forever()

    def stop(self):
        if self.server:
            self.server.shutdown()


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
            txt = txt[:40] + "..."
        return f"type_text({txt!r})"
    if t == "wait":
        return f"wait(ms={args.get('ms')})"
    if t == "ask_human":
        return f"ask_human({args.get('question')!r})"
    if t == "tell_human":
        return f"tell_human({args.get('message')!r})"
    if t == "propose_telos":
        return f"propose_telos({args.get('new_telos')!r})"
    return f"{t}(...)"


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


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
    noop = bool(chain_tools) and all(t == "wait" for t in chain_tools)
    progressed = any(t in ("drag", "type_text", "scroll", "click", "double_click", "right_click") for t in chain_tools) and not noop
    if noop:
        b += BOREDOM_INCREASE_NOOP
    if had_exec_error:
        b += BOREDOM_INCREASE_ERROR
    if story_sim >= 0.92 or last_action_repeated:
        b += BOREDOM_INCREASE_REPEAT
    if progressed:
        b -= BOREDOM_DECREASE_PROGRESS
    return _clamp01(b)


def extract_confidence(story: str) -> float:
    m = re.search(r"Confidence\s+(0?\.\d+|1\.0+)", story, re.IGNORECASE)
    return float(m.group(1)) if m else 0.5


def extract_boredom(story: str) -> float:
    m = re.search(r"Boredom\s+(0?\.\d+|1\.0+)", story, re.IGNORECASE)
    return float(m.group(1)) if m else 0.2


def trim_story(story: str, max_len: int) -> str:
    if len(story) <= max_len:
        return story
    lines = story.split("\n")
    core = [l for l in lines if any(x in l.upper() for x in ("IDEM", "IPSE", "TELOS", "CONFIDENCE", "BOREDOM"))]
    other = [l for l in lines if l not in core]
    while len("\n".join(core + other)) > max_len and other:
        other.pop(0)
    return "\n".join(core + other)


def load_story() -> str:
    try:
        if MEMORY_FILE.exists():
            s = MEMORY_FILE.read_text(encoding="utf-8", errors="ignore").strip()
            return s if s else STARTING_STORY.strip()
    except Exception:
        pass
    return STARTING_STORY.strip()


def save_story(story: str) -> None:
    try:
        MEMORY_FILE.write_text((story or "").strip() + "\n", encoding="utf-8", newline="\n")
    except Exception:
        pass


def load_backstory() -> str:
    try:
        if BACKSTORY_FILE.exists():
            return BACKSTORY_FILE.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:
        pass
    return ""


def save_backstory(backstory: str) -> None:
    try:
        BACKSTORY_FILE.write_text((backstory or "").strip() + "\n", encoding="utf-8", newline="\n")
    except Exception:
        pass


def compress_to_backstory(current_backstory: str, old_story: str, session_start: datetime) -> str:
    session_duration = (datetime.now() - session_start).total_seconds() / 3600.0
    if session_duration < SESSION_THRESHOLD_HOURS:
        return current_backstory
    summary_lines = []
    for line in old_story.split("\n"):
        if any(x in line.upper() for x in ("IDEM", "IPSE", "TELOS")):
            continue
        if line.strip():
            summary_lines.append(line.strip())
    session_summary = f"[{session_start.strftime('%Y-%m-%d')}] " + " ".join(summary_lines[:5])
    new_backstory = (current_backstory + "\n" + session_summary).strip()
    if len(new_backstory) > BACKSTORY_MAX_LEN:
        lines = new_backstory.split("\n")
        new_backstory = "\n".join(lines[-10:])
    return new_backstory


def log_jsonl(path: Path, obj: dict[str, Any]) -> None:
    line = json.dumps(obj, ensure_ascii=False)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line)
        f.write("\n")


def call_vlm(
    png: bytes,
    backstory: str,
    story: str,
    last_action: str,
    recent_actions: list[str],
    boredom: float,
    tool_choice: str,
) -> tuple[list[tuple[str, dict[str, Any]]], str]:
    recent_block = "\n".join(f"- {a}" for a in recent_actions[-4:]) or "- (none)"
    backstory_block = backstory if backstory else "(no backstory yet)"

    user_text = (
        "Read your backstory, previous narrative, and the screenshot.\n"
        "Choose one intention and execute it as a CHAIN of 1..4 tool calls.\n"
        "If confidence < 0.65 then output EXACTLY 1 tool call.\n"
        "Intermediate calls must use story=\"\". Only the FINAL call includes the rewritten full narrative.\n"
        "Do NOT add extra JSON keys.\n\n"
        "BACKSTORY (long-term compressed memory):\n"
        f"{backstory_block}\n\n"
        "NARRATIVE_MEMORY (T-1) below the line:\n"
        f"{story}\n\n"
        "LAST_ACTION_ANCHOR (T-1):\n"
        f"{last_action}\n\n"
        "RECENT_ACTIONS (most recent last):\n"
        f"{recent_block}\n\n"
        "HORMONE BOREDOM (0.00..1.00, computed):\n"
        f"{boredom:.2f}\n"
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

    out = []
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


def execute(tool: str, args: dict[str, Any], conv: Coord, human_comm: HumanComm) -> None:
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
        case "ask_human":
            answer = human_comm.ask(str(args["question"]))
            time.sleep(0.5)
        case "tell_human":
            human_comm.tell(str(args["message"]))
        case "propose_telos":
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="FRANZ agent with multi-session learning, goal evolution, and human interaction")
    parser.add_argument("--res", choices=["low", "med", "high"], default="high")
    parser.add_argument("--tool-choice", choices=["auto", "required"], default="auto")
    parser.add_argument("--port", type=int, default=HTTP_PORT)
    args = parser.parse_args()

    model_w, model_h = RES_PRESETS[args.res]
    sw, sh = user32.GetSystemMetrics(SM_CXSCREEN), user32.GetSystemMetrics(SM_CYSCREEN)
    conv = Coord(sw=sw, sh=sh)

    dump = DUMP_FOLDER / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    dump.mkdir(parents=True, exist_ok=True)
    events_path = dump / "events.jsonl"

    print(f"FRANZ | Screen: {sw}x{sh} | Model: {model_w}x{model_h} | Port: {args.port}")
    print(f"Dump: {dump}")

    human_comm = HumanComm()
    remote = RemoteServer(args.port, human_comm)
    remote.start()

    backstory = load_backstory()
    story = load_story()
    session_start = datetime.now()
    last_action = "None"
    recent_actions = []
    boredom = BOREDOM_START
    step = 0

    try:
        while True:
            step += 1
            ts = datetime.now().strftime("%H:%M:%S")

            bgra = capture_screen(sw, sh)
            png = encode_png(downsample_bgra(bgra, sw, sh, model_w, model_h), model_w, model_h)
            img_name = f"step{step:03d}.png"
            (dump / img_name).write_bytes(png)
            remote.update_screenshot(png)
            remote.update_memory(CONSTITUTION + story)

            log_jsonl(
                events_path,
                {
                    "ts": ts,
                    "step": step,
                    "event": "screenshot",
                    "file": img_name,
                    "story": story,
                    "last_action": last_action,
                    "boredom": round(boredom, 3),
                },
            )

            try:
                calls, raw_content = call_vlm(png, backstory, story, last_action, recent_actions, boredom, args.tool_choice)
            except Exception as e:
                print(f"[{ts}] {step:03d} | VLM ERROR: {e}")
                log_jsonl(events_path, {"ts": ts, "step": step, "event": "vlm_error", "error": str(e)})
                boredom = _clamp01(boredom + 0.03)
                time.sleep(0.5)
                continue

            if not calls:
                preview = (raw_content or "").strip()[:200]
                print(f"[{ts}] {step:03d} | NO TOOL_CALLS | preview={preview!r}")
                log_jsonl(events_path, {"ts": ts, "step": step, "event": "no_tool_calls", "preview": preview})
                boredom = _clamp01(boredom + BOREDOM_INCREASE_NOOP)
                time.sleep(0.25)
                continue

            prev_story = story
            prev_last_action = last_action
            chain_tools = [t for (t, _) in calls]
            had_exec_error = False

            for idx, (tool, tool_args) in enumerate(calls, 1):
                story_arg = tool_args.get("story")
                if isinstance(story_arg, str) and story_arg.strip():
                    story = trim_story(story_arg.strip(), STORY_MAX_LEN)
                    save_story(story)

                action_summary = summarize_action(tool, tool_args)
                print(f"[{ts}] {step:03d}.{idx:02d} | {action_summary}")

                ok = True
                err = ""
                try:
                    execute(tool, tool_args, conv, human_comm)
                except Exception as e:
                    ok = False
                    had_exec_error = True
                    err = str(e)
                    print(f"[{ts}] {step:03d}.{idx:02d} | EXEC ERROR: {err}")

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
                        "ok": ok,
                        "error": err,
                    },
                )

                if not ok:
                    break
                time.sleep(0.05)

            sim = story_similarity(prev_story, story)
            boredom_before = boredom
            boredom = update_boredom(boredom, chain_tools, had_exec_error, sim, last_action == prev_last_action)
            log_jsonl(
                events_path,
                {
                    "ts": ts,
                    "step": step,
                    "event": "boredom_update",
                    "boredom_before": round(boredom_before, 3),
                    "boredom_after": round(boredom, 3),
                    "story_similarity": round(sim, 3),
                },
            )

            confidence = extract_confidence(story)
            if confidence >= 0.85 and boredom <= 0.30:
                backstory = compress_to_backstory(backstory, prev_story, session_start)
                save_backstory(backstory)
                session_start = datetime.now()
                log_jsonl(events_path, {"ts": ts, "step": step, "event": "session_compress", "backstory_len": len(backstory)})

            remote.update_memory(CONSTITUTION + story)
            time.sleep(0.2)

    except KeyboardInterrupt:
        print("\nFRANZ stops.")
    finally:
        remote.stop()


if __name__ == "__main__":
    main()