README — FRANZ: Narrative-Agent Desktop Controller
===============================================

Summary
-------
FRANZ is an autonomous Windows 11 desktop agent that uses a vision-language model (VLM) to perceive screenshots, executes small chained UI actions, maintains a narrative self (IDEM/IPSE/TELOS), and evolves across sessions by compressing long-term backstory. A remote HTTP HUD serves the narrative and live screenshots so humans can monitor and answer questions from a phone. The system adds a psychologically inspired boredom hormone to avoid stagnation and supports human-in-the-loop communication and goal evolution.

Why this is revolutionary
-------------------------
- Narrative identity (Ricoeur) as runtime memory: the agent persists by rewriting its own story each step, maintaining constraints (IDEM), promises (IPSE), and a goal-as-achieved (TELOS). That makes continuity explicit and inspectable.
- Multi-timescale memory: short-term narrative + compressed backstory across sessions enables gradual learning and contextual continuity over days/weeks.
- Intrinsic motivation via a boredom hormone prevents cycling and encourages discovery without reward engineering.
- Safe, minimal action model: actions are limited to strict normalized tool calls. Each chain is 1–4 steps; only the final call writes memory.
- Human oversight via simple HTTP HUD with live screenshots and question/answer channel; humans can intervene or answer agent queries.
- Local-first architecture: no external dependencies beyond a locally hosted VLM endpoint and the base Python runtime.

Main components
---------------
- Agent core (main loop)
  - Capture screen, downsample, encode to PNG, send to VLM.
  - Parse tool calls, execute with Win32 inputs, log events.
  - Update narrative memory, adjust boredom, compress sessions to backstory.
- Identity and memory
  - MEMORY_FILE (franz_memory.txt): current narrative (IDEM/IPSE/TELOS).
  - BACKSTORY_FILE (franz_backstory.txt): compressed session summaries (long-term).
- Boredom hormone
  - Scalar [0..1] updated each turn by heuristic rules (no-op, repeats, errors, progress).
  - Drives behavior selection via information in VLM prompt.
- Tools (strict schema)
  - click, double_click, right_click, drag, type_text, scroll, wait
  - ask_human, tell_human, propose_telos — for human interaction and telos evolution.
- Remote HTTP HUD
  - Minimal static HTML served over local network.
  - Shows current narrative, last messages, and live screenshot (auto-refresh).
  - Allows phone-based answers to agent questions.
- Logging and dump
  - Per-run dump folder with screenshots and events.jsonl for auditing and replay.

How data flows (workflow)
-------------------------
ASCII overview:
  +-------------+      screenshot PNG      +---------+      tool-calls       +---------+
  | Desktop OS  | ------------------------> | Capture | --------------------> | VLM API |
  +-------------+                             (downsample/encode)          +---------+
         ^                                                                  |
         |                                                                  v
  +-------------+ <---------------------- tool executions -------------- +------------+
  | Win32 input |                                                      | Agent core  |
  +-------------+                                                      +------------+
         |                                                                  |
         v                                                                  v
  screenshots, logs, memory file  <-----------------------------------  Remote HUD + HumanComm

Step-by-step:
1. Agent captures full-screen, downscales to model input and sends VLM prompt (backstory + previous narrative + screenshot + boredom + recent actions).
2. VLM returns a structured set of tool calls (1–4 in a chain). If confidence <0.65, VLM should return one safe exploratory call.
3. Agent executes calls using Win32 input; only final call contains a non-empty story which becomes the new narrative.
4. Agent updates boredom, logs the step, saves narrative to disk. If confidence high and boredom low, agent compresses session into backstory and may propose a new TELOS.
5. Remote HUD receives updated narrative and latest screenshot; human can answer pending questions via the HUD.

Philosophical rationale
-----------------------
- Identity as narrative: memory is not inert state but authorship; agent rewrites its whole narrative each turn to preserve continuity and integrity.
- Teleology as enacted fiction: TELOS is framed as "already achieved" so the agent plans toward a concrete future-state representation rather than maximizes a scalar reward.
- IPSE (promise) enforces honesty and procedural constraints: the agent must record actions and adhere to tool schema.
- Boredom models motivational dynamics that favor novelty and meaningful progress, reducing local minima and fixed-point behaviors.

Psychology & safety
-------------------
- Boredom increases with idle or repetitive behavior and on errors; decreases with meaningful progress. This aligns with human motivational patterns (avoidance of stagnation).
- Human-in-the-loop ensures oversight: ask_human triggers a blocking wait for human answer; tell_human broadcasts important events.
- Tool strictness (schema + normalized coords) reduces unexpected behaviors.
- Logs and screenshots enable post-hoc audit and rollback.

Prompt engineering advice
-------------------------
- Keep SYSTEM_PROMPT tight and authoritative (the code already enforces JSON strictness).
- Provide VLM with explicit blocks: backstory, narrative, last action, recent actions, boredom value, and image.
- Encourage brevity in final story: limit to 1500 characters; instruct model to include IDEM/IPSE/TELOS and numeric Confidence/Boredom.
- Ask model to output an explicit "confidence" value in final story; agent parses it to guide telos evolution / 1-call policy.
- When agent must ask, structure human questions to be specific and time-bounded.

Usage
-----
1. Ensure a local VLM endpoint matching API_URL is running and accepts the tools schema.
2. Run on Windows 11 with Python 3.12.
3. Start FRANZ: python franz.py --res high --port 8080
4. On a phone, open http://<machine-ip>:8080 to monitor memory and screenshots and answer questions.
5. Inspect dump folder for screenshots and events.jsonl for analysis.

Files produced
--------------
- dump/run_YYYYMMDD_HHMMSS/: stepNNN.png, events.jsonl
- franz_memory.txt — live narrative memory
- franz_backstory.txt — compressed long-term memory

Telos evolution & backstory
---------------------------
- If the model reports Confidence >= 0.85 and boredom <= 0.30, agent compresses session and appends a summary into backstory.
- The agent can propose a new TELOS via propose_telos; human may accept/reject via the HUD or by editing memory directly.

Three simulated scenarios (brief)
-------------------------------
1. Exploration mode: boredom rises as model issues wait-only steps. VLM, seeing boredom >= 0.7, picks actions that click into menus or open apps, produces new observations, boredom drops.
2. Task completion: the agent executes a chain to automate a UI task. Confidence rises; with confidence >= 0.85 and boredom low, the agent compresses the session into backstory and proposes a higher-level TELOS.
3. Ambiguity + human-in-loop: VLM returns ask_human with a precise question. The HUD shows the question; human answers from phone; agent resumes with that info. This prevents risky blind actions.

Limitations & future work
-------------------------
- VLM correctness and tool parsing depend on the external model; robust parsing/validation and retries could be added.
- Backstory compression is heuristic; consider a learned summarizer for semantic compression.
- Security: HTTP HUD is local and unauthenticated; add TLS and auth for exposed networks.
- Add replay / sandbox execution and safe-stop constraints for risky UI operations.

Concluding note
---------------
FRANZ combines narrative philosophy, a simple intrinsic motivation mechanism, VLM perception, and human oversight into a practical, inspectable autonomous desktop controller. It is designed for transparency, iterated improvement, and human trust.