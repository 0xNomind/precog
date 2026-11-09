# 90-second demo

Before recording, keep personal details off screen:

```bash
export PS1='$ '                       # plain prompt, no user or host name
examples/setup-forecast.sh            # demo lives in /tmp/precog-demo/forecast
cd /tmp/precog-demo/forecast
hermes precog list                    # should say "No lessons yet."
```

| Time | Screen | Say |
|---|---|---|
| 0:00 | Title card | "Agents learn by trial and error. Then the next session forgets the fix." |
| 0:08 | `hermes chat -q "Run this project's test suite …"` — `python -m pytest` fails with *No module named pytest*, the agent hunts for the venv | "Session one: it guesses wrong, burns turns, finds the project venv." |
| 0:25 | The tool result's `precog` note: *Learned lesson #1* · `hermes precog list` | "Precog saw the failure and the fix, verified by exit codes, and kept it." |
| 0:35 | New session, same prompt — one tool call, passes | "Fresh context. Precog briefed it once; it's right the first time." |
| 0:48 | Briefing off: the model types `python -m pytest`, the result says *Precog rewrote this command before it ran* | "And if the model still makes the mistake, Precog fixes the command before it runs — before your approval prompt, so you approve what actually runs." |
| 1:05 | Dashboard Precog tab: mistakes prevented, lesson with ✗/✓ evidence | "Every lesson shows its evidence. Turn it off, forget it, or let it turn itself off when it stops working." |
| 1:18 | Table: 8 tool calls → 1, 7 model calls → 2 | "Same task: eight tool calls became one." |
| 1:25 | `hermes plugins install 0xNomind/precog --enable` | "Precog. Hermes stops repeating its own mistakes." |
