# FRANZ (A+B fixes + multi-tool chaining prompt rules)

This is a reduced, Windows-only loop that lets a vision-capable model (served locally via **LM Studio**'s OpenAI-compatible `/v1/chat/completions`) control the desktop using mouse/keyboard tools.

This build keeps the same two stability fixes:
- **A — Persistent narrative memory:** HUD story (`last_story`) persists across steps and survives tool-call failures.
- **B — No yellow overlay annotations:** all code for yellow on-screen annotation windows (and the `attend` pseudo-tool) is removed, so the model never sees overlays in screenshots.

And it updates **only the prompt rules** to support **multi-tool call chains** with confidence gating.

---

## Files

- `franz_agent_chain_prompt.py` — the agent loop (prompt rules updated).

---

## Requirements

- Windows 10/11
- Python 3.10+ (3.12 recommended)
- LM Studio running as a server with a vision-language model loaded

No external Python dependencies.

---

## Run

1) Start LM Studio server.

2) Load your vision-language model in LM Studio.

3) Run:

```bash
python franz_agent_chain_prompt.py --res high
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

---

## Prompt rules for multi-tool chaining

The system prompt now instructs the model to:

### 1) Output a chain of tool calls
- **Allowed:** 1 to 4 tool calls per step (ordered).

### 2) Confidence-gated chaining
- If **CHAIN_CONFIDENCE < 0.65** → output **exactly 1** tool call (safe exploratory action).
- Otherwise it may output 2–4 tool calls.

### 3) Story handling
- For intermediate calls (1..N-1), the model must set:
  - `"story": ""`
- Only the FINAL call (call N) should include a non-empty HUD update.

### 4) Fixed-format story in the FINAL call
The final `story` must contain **exactly**:

```
[GOAL] ...
[STATE] ...
[LAST] ...
[NEXT] ...
[CONF] 0.00..1.00
```

The loop will keep the HUD unchanged for intermediate calls (empty story), and only update it when the final call supplies the structured story.

---

## Notes on drag vs click

For painting/drawing, the model should use `drag` to create strokes. The prompt biases toward `drag` when drawing.

---

## Security

This controls the real mouse/keyboard. Run only in an isolated sandbox VM.
