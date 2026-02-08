# FRANZ (Foveated Memory + Last-Action Anchor)

Minimal Windows 11 desktop-control agent for LM Studio (OpenAI-compatible API).  
Python 3.12, **no pip install commands**. Uses Win32 via `ctypes` only.

## What this version adds

1) **Multi-tool chains (1..4) per step**
- The model may output 1 to 4 tool calls in a single response.
- If the model is uncertain (`CHAIN_CONFIDENCE < 0.65`), it must output exactly **1** safe exploratory tool call.

2) **Intermediate tool calls must not update the HUD**
- For calls `1..N-1`: `story` must be an empty string.
- Only the **final** call in the chain may include the HUD story.

3) **Foveated / hierarchical HUD memory**
The HUD story is overwritten each step and must be rewritten in full using the fixed format:

```
[PAST] Long-term mission + constraints + stable facts.
[NOW]  What matters in the current screenshot.
[WHERE] Last action location + intent (tool + key coords + expected effect).
[DELTA] What changed since last action; or UNKNOWN.
[NEXT] Next chain plan (1..4 tools).
[CONF] 0.00..1.00
```

4) **Last-action anchor injected as text**
No on-screen overlays. The prompt includes:
- `HUD_MEMORY (T-1)`
- `LAST_ACTION (T-1)`
- `RECENT_ACTIONS` (last few actions)

This removes visual pollution while still giving the model a precise "WHERE" reference.

5) **Debug logging**
Each run produces:
- `dump/run_YYYYMMDD_HHMMSS/stepNNN.png` screenshots
- `dump/run_.../events.jsonl` (JSONL event log)

## Requirements

- Windows 11
- Python 3.12
- LM Studio running the OpenAI-compatible server at:
  - `http://localhost:1234/v1/chat/completions`
- A vision model loaded in LM Studio (default in code: `qwen3-vl-2b-instruct-1m`)

## Run

```bat
python franz_agent_foveated.py --res high --tool-choice auto
```

Options:
- `--res low|med|high` : screenshot downsample resolution for the model.
- `--tool-choice auto|required` : forwarded to LM Studio.

## Controls

- The HUD window starts **PAUSED**.
- Click **RESUME** to let the agent run a step.
- Click **PAUSE** to stop between steps.
- Close the HUD to terminate.

## Notes on tool_choice

- `auto` allows LM Studio to return normal text if it cannot parse tool calls.
  - This code logs that case and keeps the run stable.
- `required` forces tool usage but may amplify parse failures depending on template/model.

## Files

- `franz_agent_foveated.py` : main agent
- `dump/run_*/events.jsonl` : step-by-step trace for debugging
