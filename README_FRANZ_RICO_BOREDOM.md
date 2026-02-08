# FRANZ — Ricoeur Narrative Identity + Boredom Hormone (Final)

This project implements a desktop-control agent where **the story is the enduring entity**.
The vision-language model (VLM) is treated as an engine that *rewrites the entity* each turn.

## Files

- `franz_agent_ipso_boredom.py` — single-file Windows 11 / Python 3.12 agent runtime (ctypes only; no pip installs).
- `franz_memory.txt` — persistent narrative body (created at runtime).
- `dump/run_YYYYMMDD_HHMMSS/` — per-run debug artifacts:
  - `stepNNN.png` — model input screenshots (downsampled)
  - `events.jsonl` — structured trace of steps, tool calls, errors, boredom updates

## Core Philosophy

### The story is the AI
- FRANZ is defined by what is written in the **red-bordered memory window**.
- Each turn FRANZ **rewrites the entire narrative**, preserving identity through continuity and promise.

### IDEM / IPSE / TELOS (Ricoeur protocol)
In the FINAL tool call of each step, FRANZ rewrites:
- **IDEM** — unchanging constraints (sameness)
- **IPSE** — promise (selfhood through keeping faith)
- **TELOS** — goal written as if already achieved
Then FRANZ adds a short line of what it did (include WHERE coords).

### Hormone: Boredom
A runtime-computed `boredom_hormone` (0..1) is injected into the prompt.
- It rises with **noop**, **repetition**, **errors**.
- It falls with **progress actions**.
When boredom is high, the prompt forces FRANZ to resist passive commentary and to take actions that advance TELOS or reveal new information.

This is intentionally similar to intrinsic motivation: the agent is pushed to explore when it stagnates.

## How It Works

### Main loop (per step)
1. Capture screenshot (NOW / T) and downsample to model input resolution.
2. Call the VLM with:
   - previous narrative (T-1)
   - last action anchor (WHERE)
   - recent actions
   - boredom hormone (computed)
3. The VLM returns a **tool-call chain** (1..4 tool calls).
4. Runtime executes tool calls in order.
5. Only the FINAL tool call is allowed to contain a non-empty `story`.
   - If present, it becomes the next narrative body and is saved to `franz_memory.txt`.
6. Runtime updates the HUD to display the constitution + the current narrative.
7. Runtime updates `boredom_hormone` using:
   - noop chain
   - execution errors
   - story lexical similarity
   - repeated last action

### Tool chaining rules
- 1 to 4 tool calls per step.
- If **confidence < 0.65**, output **exactly one** tool call.
- Intermediate calls must use `story=""`.
- Only final call writes the full narrative rewrite.

### LM Studio compatibility behavior
If LM Studio fails to parse tool calls, it may return plain text content.
This runtime does not crash:
- logs the raw content preview in `events.jsonl`
- keeps the existing narrative stable
- applies a small boredom increase

## Run

```bat
python franz_agent_ipso_boredom.py --res high --tool-choice auto
```

Options:
- `--res {low,med,high}` controls model input resolution.
- `--tool-choice {auto,required}` controls whether the server is allowed to reply without tools.

## Debugging

- `events.jsonl` is the forensic trace. Key events:
  - `screenshot` — includes boredom value
  - `tool` — args (without story), success/failure
  - `vlm_error`, `no_tool_calls`
  - `boredom_update` — before/after, similarity, chain tools

## Timeline of changes (this chat)

### Phase 0 — Failure analysis
- Root cause of instability: tool args mismatch (e.g., `click` missing `y`) caused exec errors.
- HUD "memory reset" was actually control flow: last story reinitialized per step.
- Yellow annotation windows polluted screenshots and confused the VLM.

### Phase A/B — Stability hotfixes
- **A**: persist `last_story` across steps; never reset to default on errors.
- **B**: remove/hide annotation overlays so they never appear in screenshots.

### Multi-call chains
- Prompt changed to allow **1..4 tool calls** per step.
- Intermediate calls use empty story; only final call writes the memory update.

### tool_choice experimentation
- Switched to `tool_choice="auto"` to tolerate LM Studio tool parsing failures.
- Runtime logs fallback `message.content` instead of crashing.

### Foveated memory (hierarchical HUD)
- Introduced structured working memory blocks and last-action anchor.

### IPSO Ricoeur narrative entity
- Memory window became the "body".
- Narrative persisted in `franz_memory.txt`.
- Identity protocol enforced IDEM/IPSE/TELOS rewrite each turn.

### Final: boredom hormone + Ricoeur narrative
- Keep IPSO narrative protocol.
- Add a boredom hormone that rises with stagnation and forces decisive action selection.
- Boredom is injected into the model context and required in the narrative output.

## Minimal diffs (conceptual)

- Removed overlay annotations entirely.
- Added `boredom_hormone` state + update function.
- Injected `boredom_hormone` into the VLM prompt.
- Required narrative to end with two lines:
  - `Confidence 0.xx`
  - `Boredom 0.xx`

