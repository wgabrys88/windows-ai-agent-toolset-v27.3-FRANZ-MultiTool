# FRANZ (A+B minimal fixes)

This is a reduced, Windows-only loop that lets a vision-capable model (served locally via **LM Studio**'s OpenAI-compatible `/v1/chat/completions`) control the desktop using mouse/keyboard tools.

**This version applies only the two changes you requested:**

- **A — Persistent narrative memory:** the HUD story (`last_story`) is no longer reinitialized to the default every step. It persists across steps and survives tool-call failures.
- **B — Remove yellow target/annotation windows:** all code for the yellow on-screen annotation windows (and the `attend` pseudo-tool) is removed, so the model never sees those overlays in screenshots.

Additionally (requested in the message that asked about it), a tiny **`wait`** tool is included so that if `tool_choice` is set to `required`, the model has a true no-op option.

---

## Files

- `franz_agent_ab.py` — the agent loop.

---

## Requirements

- Windows 10/11
- Python 3.10+ (3.12 recommended)
- LM Studio running as a server with a vision-language model loaded

No external Python deps.

---

## Run

1) Start LM Studio server.

2) Load your vision-language model in LM Studio.

3) Run:

```bash
python franz_agent_ab.py --res high
```

A cyan HUD window appears. Edit the story while paused, then click **RESUME**.

- **CTRL + mouse wheel** over the HUD: zoom text

Screenshots are saved into `dump/run_YYYYMMDD_HHMMSS/` as `step###.png`.

---

## Tool interface (what the model receives)

Tools are standard OpenAI-style function tools:

- `click(x,y,story)`
- `double_click(x,y,story)`
- `right_click(x,y,story)`
- `drag(x1,y1,x2,y2,story)`
- `type_text(text,story)`
- `scroll(dy,story)`
- `wait(ms,story)`

**Coordinates are normalized integers 0..1000**.

**Important:** The system prompt explicitly tells the model:

- output exactly one tool call
- do not add extra JSON keys
- put all narrative into `story` (>=150 words)

---

## Why `attend` existed before (and why it is removed)

In the original file, `attend` was *not* a real tool available to the model (it was missing from the `TOOLS` list). It acted as a **debug / target-highlighting step** that created yellow overlay windows.

Those overlays polluted screenshots and distracted the VLM, so this version removes:

- `attend`
- `LabeledObsWindow` / `ObsManager`
- all overlay drawing / positioning logic

If you still want *some* “do nothing” step while keeping `tool_choice="required"`, use `wait(ms, story)`.

---

## Notes on drag vs click

If the task is painting/drawing, the model must use `drag` to create strokes. The prompt now explicitly biases toward `drag` when drawing.

If it still keeps clicking, the usual causes are:

- model/tool-calling template mismatch in the runtime (LM Studio “default tool use” vs “native tool use”)
- model is too small or not trained strongly for function calling

---

## Troubleshooting

### The model calls `click` but arguments are wrong (missing `y`, extra keys, etc.)

That indicates the model is producing malformed tool arguments.

Mitigations:

- Use a model/chat template with stronger **native tool calling** support.
- Lower `temperature` in `SAMPLING`.
- Keep the instruction **“no extra JSON keys”** in the system prompt.

### HUD resets to the default text

That should not happen in this version unless the process restarts. The story is preserved as `last_story` and rewritten to HUD each step.

---

## Security

This controls the real mouse/keyboard. Run only in an isolated sandbox VM.
