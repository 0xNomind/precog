---
name: precog
description: How to work with Precog, the plugin that learns from failed terminal commands and fixes repeat mistakes before they run.
---

# Working with Precog

Precog watches terminal commands. When a command fails and a later command
fixes it, Precog stores the pair as a lesson for that project. Before the next
command runs, it applies matching lessons, so a mistake that was already solved
does not cost another round trip.

## What you will see

- A tool result with a `precog` field: Precog rewrote the command before it
  ran (for example `python3` → `.venv/bin/python`) or learned a new lesson.
  Trust the rewritten command; it is the one that worked before.
- A blocked command with a message starting `Precog:`: that exact command
  already failed in this project. Adapt the fix it shows. If you are sure the
  original is needed now, run it again; Precog lets the second attempt through.
- A short "Precog — lessons learned" note in the user turn: facts about this
  machine and project that came from real failures. Prefer them over guesses.

## Teaching Precog

If the user tells you a project rule ("always use pnpm here", "tests need the
venv"), the user can record it with
`/precog teach <failing command> => <working command>`.
Use `/precog` to show what Precog knows for the current project.
