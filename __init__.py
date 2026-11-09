"""Precog — Hermes stops repeating its own mistakes.

Every time a terminal command fails and a later command fixes it, Precog keeps
the (failure -> fix) pair as a lesson for that project. Before the next command
runs, Precog applies matching lessons (through ``tool_request`` middleware, so
Hermes's approval gate sees the corrected command), warns about commands that
are known to fail, and briefs the model once per project.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time

from . import engine as E
from .runtime import MODES, PLUGIN_ID, Precog
from .store import MACHINE_SCOPE, Store

logger = logging.getLogger(__name__)

_PRECOG: Precog | None = None


def _connect() -> sqlite3.Connection:
    try:
        from plugins.plugin_storage import plugin_db

        return plugin_db(PLUGIN_ID, "precog.db")
    except Exception:  # pragma: no cover - outside Hermes (tests, scripts)
        path = os.path.join(os.path.expanduser(os.environ.get("PRECOG_HOME", "~/.precog")), "precog.db")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return sqlite3.connect(path, check_same_thread=False)


def get_precog() -> Precog:
    """The process-wide Precog instance (also used by the Desktop backend)."""
    global _PRECOG
    if _PRECOG is None:
        _PRECOG = Precog(Store(_connect))
    return _PRECOG


# ---------------------------------------------------------------------------
# /precog slash command
# ---------------------------------------------------------------------------

_HELP = (
    "Usage: /precog [status|list|forget <id>|off <id>|on <id>|mode auto|suggest|learn|off|"
    "teach <failing command> => <working command>]"
)


def _scope_here(pc: Precog, session_id: str = "") -> str:
    sess = pc._session(session_id)
    return pc._scope(None, sess)


def _teach(pc: Precog, scope: str, text: str) -> str:
    if "=>" not in text:
        return "Usage: /precog teach <failing command> => <working command>"
    bad, good = (part.strip().strip("`") for part in text.split("=>", 1))
    fsegs, xsegs = E.parse_command(bad), E.parse_command(good)
    if not fsegs or not xsegs:
        return "Both commands must be non-empty."
    sig = E.ErrorSig("taught", "", "taught by the user")
    blamed = len(fsegs) - 1
    fix_idx = E.find_fix_segment(fsegs[blamed], sig, xsegs)
    if fix_idx is None:
        fix_idx = len(xsegs) - 1
    lesson = E.extract_lesson(bad, fsegs, blamed, sig, good, xsegs, fix_idx)
    lid, _ = pc.store.upsert_lesson(scope, lesson, fail_sig=E.normalized_signature(fsegs[blamed]), origin="taught")
    return f"Taught lesson #{lid}: {pc.describe(pc.store.get_lesson(lid) or {})}"


def _slash(ctx):
    def handler(raw_args: str = "", *, session_id: str = "", **_kw) -> str:
        pc = get_precog()
        parts = (raw_args or "").strip().split(maxsplit=1)
        sub = parts[0].lower() if parts else "status"
        rest = parts[1] if len(parts) > 1 else ""
        scope = _scope_here(pc, session_id)
        if sub in ("status", ""):
            return pc.status_text(scope)
        if sub == "list":
            rows = pc.store.all_lessons(limit=40)
            if not rows:
                return "No lessons yet."
            return "\n".join(
                f"#{r['id']} [{'on' if r['enabled'] else 'off'}] {os.path.basename(r['scope']) if r['scope'] != MACHINE_SCOPE else 'machine'}"
                f" · {pc.describe(r)} · applied {r['applied']}×, saved {r['saves']}×"
                for r in rows
            )
        if sub in ("forget", "off", "on"):
            try:
                lid = int(rest.strip().lstrip("#"))
            except ValueError:
                return _HELP
            if sub == "forget":
                return f"Forgot lesson #{lid}." if pc.store.forget(lid) else f"No lesson #{lid}."
            ok = pc.store.set_enabled(lid, sub == "on")
            return f"Lesson #{lid} turned {sub}." if ok else f"No lesson #{lid}."
        if sub == "mode":
            mode = rest.strip().lower()
            if not mode and hasattr(ctx, "ask_choice"):
                mode = ctx.ask_choice("Precog mode", [
                    {"value": "auto", "label": "Auto", "description": "fix known mistakes before they run"},
                    {"value": "suggest", "label": "Suggest", "description": "stop and show the learned fix"},
                    {"value": "learn", "label": "Learn only", "description": "collect lessons, change nothing"},
                    {"value": "off", "label": "Off", "description": "do nothing"},
                ]) or ""
            if mode not in MODES:
                return "Usage: /precog mode auto|suggest|learn|off"
            ctx.set_config("mode", mode)
            return f"Precog mode: {mode}"
        if sub == "teach":
            return _teach(pc, scope, rest)
        return _HELP

    return handler


# ---------------------------------------------------------------------------
# hermes precog CLI
# ---------------------------------------------------------------------------


def _cli_setup(subparser):
    subs = subparser.add_subparsers(dest="precog_command")
    p = subs.add_parser("list", help="List lessons")
    p.add_argument("--json", action="store_true")
    subs.add_parser("stats", help="Show what Precog has learned and prevented")
    for name in ("forget", "enable", "disable"):
        sp = subs.add_parser(name, help=f"{name.capitalize()} a lesson")
        sp.add_argument("id", type=int)
    sp = subs.add_parser("export", help="Export lessons as JSON")
    sp.add_argument("path", nargs="?", default="-")
    sp = subs.add_parser("teach", help="Teach a lesson: hermes precog teach '<bad>' '<good>' [--project DIR]")
    sp.add_argument("bad")
    sp.add_argument("good")
    sp.add_argument("--project", default=os.getcwd())
    subparser.set_defaults(func=_cli)


def _cli(args):
    pc = get_precog()
    cmd = getattr(args, "precog_command", None) or "stats"
    if cmd == "list":
        rows = pc.store.all_lessons()
        if getattr(args, "json", False):
            print(json.dumps(rows, indent=2, default=str))
            return
        for r in rows:
            scope = "machine" if r["scope"] == MACHINE_SCOPE else r["scope"]
            print(f"#{r['id']:<4} {'on ' if r['enabled'] else 'off'} {r['kind']:<12} {pc.describe(r)}")
            print(f"      {scope} · learned {r['learned']}× · applied {r['applied']}× · saved {r['saves']}×")
        if not rows:
            print("No lessons yet.")
    elif cmd == "stats":
        st = pc.store.stats()
        week = pc.store.stats(since=time.time() - 7 * 86400)
        print(f"lessons: {st['lessons']} ({st['enabled']} on)")
        print(f"mistakes prevented: {st['saves']} (rewrites applied: {st['applied']})")
        print(f"terminal commands observed this week: {week['events']}, failures: {week['failures']}")
    elif cmd in ("forget", "enable", "disable"):
        ok = pc.store.forget(args.id) if cmd == "forget" else pc.store.set_enabled(args.id, cmd == "enable")
        print(f"{cmd} #{args.id}: {'ok' if ok else 'not found'}")
    elif cmd == "export":
        data = json.dumps(pc.store.all_lessons(limit=10000), indent=2, default=str)
        if args.path == "-":
            print(data)
        else:
            with open(args.path, "w", encoding="utf-8") as fh:
                fh.write(data)
            print(f"wrote {args.path}")
    elif cmd == "teach":
        print(_teach(pc, E.project_root(args.project), f"{args.bad} => {args.good}"))


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


def register(ctx):
    global _PRECOG
    _PRECOG = Precog(Store(_connect), get_config=ctx.get_config)
    pc = _PRECOG

    ctx.register_middleware("tool_request", pc.on_tool_request)
    ctx.register_hook("pre_tool_call", pc.on_pre_tool_call)
    ctx.register_hook("post_tool_call", pc.on_post_tool_call)
    ctx.register_hook("transform_tool_result", pc.on_transform_tool_result)
    ctx.register_hook("pre_llm_call", pc.on_pre_llm_call)

    ctx.register_command(
        PLUGIN_ID,
        handler=_slash(ctx),
        description="Lessons Hermes learned from its own command failures",
        args_hint="[status|list|forget <id>|mode <auto|suggest|learn|off>|teach <bad> => <good>]",
    )
    ctx.register_cli_command(
        name=PLUGIN_ID,
        help="Inspect and manage the lessons Precog learned",
        setup_fn=_cli_setup,
        handler_fn=_cli,
    )

    skill = os.path.join(os.path.dirname(__file__), "skills", "precog", "SKILL.md")
    if os.path.exists(skill):
        ctx.register_skill("precog", skill)
