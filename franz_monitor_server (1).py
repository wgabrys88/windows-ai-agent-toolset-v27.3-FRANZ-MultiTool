from __future__ import annotations

"""FRANZ Monitor Server — Windows 11 / Python 3.12 / no pip

Serves:
- Dashboard (mobile-first, dark) at /
- JSON state at /api/state (reads franz_state.json)
- Screenshot list at /api/shots (scans dump/execution)
- Screenshot images at /shots/<filename>
- Pause/Resume:
    POST /api/pause
    POST /api/resume

This is intended for trusted LAN use.
"""

import argparse
import json
import os
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

STATE_FILE_DEFAULT = Path("franz_state.json")
CONTROL_FILE_DEFAULT = Path("franz_control.json")
SHOTS_DIR_DEFAULT = Path("dump") / "execution"

INDEX_HTML = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FRANZ Monitor</title>
  <style>
    :root {
      --bg: #0b0f14;
      --panel: #0f1620;
      --panel2: #0c121b;
      --text: #e6edf3;
      --muted: #93a4b7;
      --accent: #4aa3ff;
      --good: #35d07f;
      --warn: #ffcc66;
      --bad: #ff5d5d;
      --border: rgba(255,255,255,.08);
      --shadow: 0 10px 30px rgba(0,0,0,.35);
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      --sans: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, Helvetica, sans-serif;
    }
    html, body { height: 100%; background: var(--bg); color: var(--text); margin: 0; font-family: var(--sans); }
    * { box-sizing: border-box; }
    .wrap { max-width: 980px; margin: 0 auto; padding: 14px 14px 40px; }
    .topbar { display: flex; gap: 10px; align-items: center; justify-content: space-between; margin-bottom: 12px; }
    .brand { display:flex; flex-direction: column; gap: 2px; }
    .brand h1 { margin:0; font-size: 16px; letter-spacing: .12em; text-transform: uppercase; color: var(--muted); }
    .brand .sub { font-size: 12px; color: var(--muted); }
    .btns { display:flex; gap: 8px; }
    button {
      appearance: none; border: 1px solid var(--border); background: var(--panel);
      color: var(--text); padding: 10px 12px; border-radius: 12px; cursor: pointer;
      box-shadow: var(--shadow); font-weight: 600;
    }
    button.primary { border-color: rgba(74,163,255,.35); background: linear-gradient(180deg, rgba(74,163,255,.18), rgba(15,22,32,.2)); }
    button.danger { border-color: rgba(255,93,93,.35); background: linear-gradient(180deg, rgba(255,93,93,.16), rgba(15,22,32,.2)); }
    button:active { transform: translateY(1px); }
    .grid { display: grid; grid-template-columns: 1fr; gap: 12px; }
    @media (min-width: 860px) { .grid { grid-template-columns: 1.2fr .8fr; } }
    .card {
      background: linear-gradient(180deg, rgba(255,255,255,.03), rgba(255,255,255,.01));
      border: 1px solid var(--border); border-radius: 16px; box-shadow: var(--shadow);
      overflow: hidden;
    }
    .card h2 {
      margin:0; font-size: 13px; letter-spacing: .12em; text-transform: uppercase; color: var(--muted);
      padding: 12px 14px; border-bottom: 1px solid var(--border); background: rgba(0,0,0,.08);
    }
    .pad { padding: 12px 14px; }
    .kvs { display:grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .kv { background: rgba(0,0,0,.12); border: 1px solid var(--border); border-radius: 14px; padding: 10px; }
    .kv .k { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .12em; }
    .kv .v { font-size: 14px; margin-top: 4px; font-family: var(--mono); word-break: break-word; }
    .badgeline { display:flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
    .badge {
      border: 1px solid var(--border); background: rgba(0,0,0,.15);
      padding: 6px 10px; border-radius: 999px; font-size: 12px; font-family: var(--mono);
    }
    .badge.good { border-color: rgba(53,208,127,.25); color: var(--good); }
    .badge.warn { border-color: rgba(255,204,102,.25); color: var(--warn); }
    .badge.bad { border-color: rgba(255,93,93,.25); color: var(--bad); }

    pre {
      margin: 0; padding: 12px; background: var(--panel2);
      border: 1px solid var(--border); border-radius: 14px;
      font-family: var(--mono); font-size: 12px; line-height: 1.35; white-space: pre-wrap;
      max-height: 52vh; overflow: auto;
    }

    .shot { width: 100%; border-bottom: 1px solid var(--border); background: #000; }
    .shot img { width: 100%; height: auto; display:block; }
    .shotsList { display:flex; flex-wrap: wrap; gap: 8px; }
    .thumb {
      width: 112px; height: 74px; border-radius: 12px; overflow: hidden;
      border: 1px solid var(--border); background: #000; cursor:pointer;
    }
    .thumb img { width: 100%; height: 100%; object-fit: cover; display:block; }
    .muted { color: var(--muted); font-size: 12px; }
  </style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div class="brand">
      <h1>FRANZ Monitor</h1>
      <div class="sub" id="subline">connecting…</div>
    </div>
    <div class="btns">
      <button class="primary" id="btnResume">Resume</button>
      <button class="danger" id="btnPause">Pause</button>
    </div>
  </div>

  <div class="grid">
    <div class="card">
      <h2>Live Screenshot</h2>
      <div class="shot"><img id="shot" alt="latest screenshot"></div>
      <div class="pad">
        <div class="muted">Tap a thumbnail to view older shots.</div>
        <div style="height:10px"></div>
        <div class="shotsList" id="shotsList"></div>
      </div>
    </div>

    <div class="card">
      <h2>State</h2>
      <div class="pad">
        <div class="kvs">
          <div class="kv"><div class="k">Step</div><div class="v" id="step">-</div></div>
          <div class="kv"><div class="k">Paused</div><div class="v" id="paused">-</div></div>
          <div class="kv"><div class="k">Last Action</div><div class="v" id="lastAction">-</div></div>
          <div class="kv"><div class="k">Boredom</div><div class="v" id="boredom">-</div></div>
        </div>
        <div class="badgeline" id="badges"></div>
        <div style="height:12px"></div>
        <pre id="story">loading…</pre>
      </div>
    </div>
  </div>
</div>

<script>
  const $ = (id) => document.getElementById(id);
  let lastShot = "";

  async function post(path) {
    try {
      const r = await fetch(path, {method: "POST"});
      return await r.json();
    } catch (e) { return {ok:false, error:String(e)}; }
  }

  $("btnPause").onclick = () => post("/api/pause");
  $("btnResume").onclick = () => post("/api/resume");

  function setBadges(state) {
    const el = $("badges");
    el.innerHTML = "";
    const mk = (txt, cls) => {
      const b = document.createElement("div");
      b.className = "badge " + (cls || "");
      b.textContent = txt;
      el.appendChild(b);
    };
    const paused = !!state.paused;
    mk(paused ? "PAUSED" : "RUNNING", paused ? "warn" : "good");
    if (typeof state.boredom_hormone === "number") {
      const b = state.boredom_hormone;
      mk("BOREDOM " + b.toFixed(2), b >= 0.70 ? "warn" : "good");
    }
    if (state.event && state.event !== "step_done") {
      mk("EVENT " + state.event, state.event.includes("error") ? "bad" : "warn");
    }
    if (state.latest_screenshot) mk(state.latest_screenshot, "");
  }

  function setShot(name) {
    if (!name) return;
    const url = "/shots/" + encodeURIComponent(name) + "?t=" + Date.now();
    $("shot").src = url;
    lastShot = name;
  }

  async function refreshState() {
    try {
      const r = await fetch("/api/state?t=" + Date.now());
      const state = await r.json();
      $("subline").textContent = (state.ts || "") + "  |  " + (state.model || "");
      $("step").textContent = state.step ?? "-";
      $("paused").textContent = state.paused ? "true" : "false";
      $("lastAction").textContent = state.last_action || "-";
      $("boredom").textContent = (typeof state.boredom_hormone === "number") ? state.boredom_hormone.toFixed(3) : "-";
      $("story").textContent = state.story || "(no story)";
      setBadges(state);
      if (state.latest_screenshot && state.latest_screenshot !== lastShot) setShot(state.latest_screenshot);
    } catch (e) {
      $("subline").textContent = "disconnected: " + String(e);
    }
  }

  async function refreshShots() {
    try {
      const r = await fetch("/api/shots?t=" + Date.now());
      const data = await r.json();
      const list = data.shots || [];
      const el = $("shotsList");
      el.innerHTML = "";
      for (const name of list.slice(0, 18)) {
        const d = document.createElement("div");
        d.className = "thumb";
        const img = document.createElement("img");
        img.loading = "lazy";
        img.src = "/shots/" + encodeURIComponent(name) + "?t=" + Date.now();
        d.onclick = () => setShot(name);
        d.appendChild(img);
        el.appendChild(d);
      }
    } catch (e) { /* ignore */ }
  }

  setInterval(refreshState, 450);
  setInterval(refreshShots, 2000);
  refreshState();
  refreshShots();
</script>
</body>
</html>'''


def atomic_write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def safe_read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def list_shots(shots_dir: Path, limit: int = 200) -> list[str]:
    if not shots_dir.exists():
        return []
    shots = [p.name for p in shots_dir.glob("*.png")]
    shots.sort(reverse=True)  # step_00001.png lexical order works
    return shots[:limit]


class Handler(BaseHTTPRequestHandler):
    server_version = "FRANZMonitor/1.0"

    def _send_json(self, obj: dict, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, html: str) -> None:
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        p = parsed.path

        if p == "/" or p == "/index.html":
            self._send_html(INDEX_HTML)
            return

        if p == "/api/state":
            state = safe_read_json(self.server.state_file)  # type: ignore[attr-defined]
            state.setdefault("server_ts", datetime.now().isoformat(timespec="seconds"))
            if "latest_screenshot" not in state:
                shots = list_shots(self.server.shots_dir, 1)  # type: ignore[attr-defined]
                state["latest_screenshot"] = shots[0] if shots else ""
            self._send_json(state)
            return

        if p == "/api/shots":
            shots = list_shots(self.server.shots_dir, 200)  # type: ignore[attr-defined]
            self._send_json({"shots": shots})
            return

        if p.startswith("/shots/"):
            name = p[len("/shots/"):]
            if not name or "/" in name or "\\" in name or ".." in name:
                self.send_error(400)
                return
            path = (self.server.shots_dir / name)  # type: ignore[attr-defined]
            self._send_file(path)
            return

        self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        p = parsed.path

        if p == "/api/pause":
            atomic_write_json(self.server.control_file, {"paused": True, "ts": datetime.now().isoformat(timespec="seconds")})  # type: ignore[attr-defined]
            self._send_json({"ok": True, "paused": True})
            return

        if p == "/api/resume":
            atomic_write_json(self.server.control_file, {"paused": False, "ts": datetime.now().isoformat(timespec="seconds")})  # type: ignore[attr-defined]
            self._send_json({"ok": True, "paused": False})
            return

        self.send_error(404)

    def log_message(self, fmt: str, *args) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="FRANZ monitor HTTP server (LAN dashboard)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--state-file", default=str(STATE_FILE_DEFAULT))
    parser.add_argument("--control-file", default=str(CONTROL_FILE_DEFAULT))
    parser.add_argument("--shots-dir", default=str(SHOTS_DIR_DEFAULT))
    args = parser.parse_args()

    state_file = Path(args.state_file)
    control_file = Path(args.control_file)
    shots_dir = Path(args.shots_dir)
    shots_dir.mkdir(parents=True, exist_ok=True)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.state_file = state_file      # type: ignore[attr-defined]
    httpd.control_file = control_file  # type: ignore[attr-defined]
    httpd.shots_dir = shots_dir        # type: ignore[attr-defined]

    print(f"FRANZ Monitor listening on http://{args.host}:{args.port}/")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
