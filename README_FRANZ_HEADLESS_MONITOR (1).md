# FRANZ — Headless Agent + LAN Monitor (IPSO Ricoeur + Boredom Hormone)

This version removes the on-desktop HUD entirely.

- The VLM still receives **memory + hormones** via text.
- The screenshot is clean (no self-reading, no wasted screen space).
- A separate **standard-library-only** HTTP server provides a mobile-first dark dashboard to watch the agent in real time (including screenshots) and to Pause/Resume.

## Files

- `franz_agent_headless_ipso_boredom.py`
  - Captures screenshots
  - Calls OpenAI-compatible VLM endpoint (LM Studio)
  - Executes tool-call chains via Win32 SendInput
  - Persists IPSO narrative identity in `franz_memory.txt`
  - Computes boredom hormone in runtime
  - Writes `franz_state.json` for the monitor

- `franz_monitor_server.py`
  - LAN dashboard (single-file HTML/CSS/JS)
  - Shows current state + story + boredom
  - Shows latest screenshot + thumbnails from dump folder
  - Pause/Resume buttons writing `franz_control.json`

## Runtime artifacts

- `franz_memory.txt` — model-authored “self” (rewritten only when FINAL tool call provides non-empty `story`)
- `franz_state.json` — runtime snapshot for monitoring (atomic replace)
- `franz_control.json` — pause flag controlled by the monitor (atomic replace)
- `dump/execution/step_00001.png` ... — screenshots the monitor can show

Atomic writes use the **write-temp + `os.replace()`** pattern.

## Quick start

### 1) LM Studio

Run LM Studio with an OpenAI-compatible server endpoint.
Default expected URL:

- `http://localhost:1234/v1/chat/completions`

### 2) Run the agent

```bat
python franz_agent_headless_ipso_boredom.py --tool-choice auto --res med
```

Notes:
- `--tool-choice auto` is recommended with LM Studio: if LM Studio fails to parse tool calls, it will often return normal text rather than malformed JSON, so the agent doesn’t crash.
- Screenshots are saved to `dump/execution/`.

### 3) Run the monitor server

```bat
python franz_monitor_server.py --host 0.0.0.0 --port 8765
```

Open from a phone on the same LAN:

- `http://<YOUR_PC_LAN_IP>:8765/`

If Windows Firewall blocks it, allow inbound TCP 8765.

## Pause/Resume

- The dashboard Pause/Resume buttons write `franz_control.json`.
- The agent polls that file every loop.
- When paused, the agent stops acting but keeps publishing state.

## Prompt rules (what changed)

### Multi-tool chaining

- Each step can be a CHAIN of **1–4** tool calls.
- If Confidence < 0.65, the model must output **exactly 1** tool call.
- Intermediate tool calls must use `story=""`.
- Only the **final tool call** can include the rewritten story.

### IPSO Ricoeur identity

- The story is rewritten each step as:
  - IDEM (constraints)
  - IPSE (promise)
  - TELOS (goal already achieved)
  - then 1–3 lines: what was done (include WHERE coords)
  - end with `Confidence` and `Boredom`

### Boredom hormone

Boredom is computed by the runtime and injected as context.

Heuristics:
- Increases on wait-only chains
- Increases on repeated action / overly-similar story
- Increases on execution errors
- Decreases on real interaction (click/drag/type/scroll)

Goal:
- Prevent stagnation and “idle commentary loops”

## Monitoring UI

The monitor is a single-page dark dashboard optimized for mobile:

- Live screenshot
- Thumbnails of recent steps
- Story text
- Step, last action, boredom hormone
- Pause/Resume

## Timeline of changes in this chat (condensed transcript)

1) You reported: parameter mismatch → memory reset in HUD; yellow annotation windows confused the VLM; LM Studio tool incompatibility.
2) We aligned tool schemas (strict JSON, required keys, `additionalProperties: false`) and removed annotation overlays from the screenshot channel.
3) We moved to tool-call chaining (1..4) with confidence gating; intermediate calls no story; final call fixed-format story.
4) We adopted Ricoeur IPSO identity protocol (IDEM/IPSE/TELOS) so that “the story is the AI”.
5) We added an external, runtime-computed boredom hormone to push novelty and prevent stagnation.
6) You proposed removing on-screen HUD; we replaced it with headless file-backed state + an external LAN monitor server.

## Known limitations

- The monitor server uses Python’s `http.server` primitives (not for internet exposure). Use on a trusted LAN.
- If you open the monitor page on the same desktop FRANZ controls, the page can appear in screenshots and become a new distractor. Best practice: view from a separate device.

