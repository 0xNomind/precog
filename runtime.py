"""Hermes glue for Precog: hook and middleware callbacks around the engine and store."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import engine as E
from .store import MACHINE_SCOPE, Store

logger = logging.getLogger(__name__)

PLUGIN_ID = "precog"
MODES = ("auto", "suggest", "learn", "off")
REWRITE_KINDS = ("prefix", "swap_program", "swap_flag", "swap_path")
DEFAULTS = {
    "mode": "auto",
    "briefing": True,
    "max_briefing": 3,
    "tripwire": True,
    "annotate": True,
    "pair_window": 8,
}


def _redact(text: str) -> str:
    """Scrub secrets with Hermes's own redactor (falls back to identity outside Hermes)."""
    if not text:
        return text
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text, force=True)
    except Exception:  # pragma: no cover - only outside Hermes
        return text


def _broadcast(event: str, payload: dict) -> None:
    """Push a live update to the Precog Desktop half (a no-op when no window is attached)."""
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event

        broadcast_plugin_event(PLUGIN_ID, event, payload)
    except Exception:  # pragma: no cover - outside Hermes or no Desktop
        pass


def _agent_cwd() -> str:
    try:
        from agent.runtime_cwd import resolve_agent_cwd

        return str(resolve_agent_cwd())
    except Exception:  # pragma: no cover
        return os.getcwd()


@dataclass
class _Failure:
    idx: int
    command: str
    segs: list
    blamed: int
    sig: Any
    scope: str
    resolved: bool = False


@dataclass
class _Session:
    counter: int = 0
    recent: deque = field(default_factory=lambda: deque(maxlen=24))
    briefed: set = field(default_factory=set)
    tripped: set = field(default_factory=set)
    learned: list = field(default_factory=list)  # (lesson_id, created, Lesson) awaiting an annotation
    briefed_lessons: dict = field(default_factory=dict)  # lesson id -> lesson row, until followed
    cwd: str = ""
    touched: float = field(default_factory=time.time)


class Precog:
    def __init__(self, store: Store, get_config: Optional[Callable[[str, Any], Any]] = None):
        self.store = store
        self._get_config = get_config
        self._sessions: dict[str, _Session] = {}
        self._pending: dict[str, dict] = {}  # tool_call_id -> {"lessons": [...], "notes": [...], "original": str}
        self._notes: dict[str, dict] = {}  # tool_call_id -> rewrite notes for the annotation
        self._lock = threading.RLock()

    # -- settings ----------------------------------------------------------

    def setting(self, key: str):
        default = DEFAULTS[key]
        if self._get_config is None:
            return default
        try:
            val = self._get_config(key, default)
        except Exception:
            return default
        if key == "mode":
            val = str(val or default).lower()
            return val if val in MODES else default
        if isinstance(default, bool):
            if isinstance(val, str):
                return val.strip().lower() not in ("0", "false", "no", "off")
            return bool(val)
        if isinstance(default, int):
            try:
                return max(1, int(val))
            except (TypeError, ValueError):
                return default
        return val

    # -- helpers -----------------------------------------------------------

    def _session(self, *ids: str) -> _Session:
        key = next((i for i in ids if i), "default")
        with self._lock:
            sess = self._sessions.get(key)
            if sess is None:
                if len(self._sessions) > 200:  # drop the stalest sessions
                    for old in sorted(self._sessions, key=lambda k: self._sessions[k].touched)[:50]:
                        self._sessions.pop(old, None)
                sess = self._sessions[key] = _Session()
            sess.touched = time.time()
            return sess

    @staticmethod
    def _cwd(args: Optional[dict], sess: Optional[_Session]) -> str:
        if isinstance(args, dict) and isinstance(args.get("workdir"), str) and args["workdir"].strip():
            return args["workdir"]
        if sess is not None and sess.cwd:
            return sess.cwd
        return _agent_cwd()

    def _scope(self, args: Optional[dict], sess: Optional[_Session], cwd: str = "") -> str:
        cwd = cwd or self._cwd(args, sess)
        return E.project_root(cwd) or cwd or MACHINE_SCOPE

    @staticmethod
    def describe(lesson: dict) -> str:
        kind, data = lesson.get("kind"), lesson.get("data") or {}
        key = " ".join(lesson.get("key_args") or [])
        what = f" {key}" if key else ""
        if kind == "swap_program":
            return f"use `{data.get('to')}` instead of `{data.get('from')}`{' for' + what if what else ''}"
        if kind == "prefix":
            return f"run `{data.get('prefix')}` before `{lesson.get('program')}{what}`"
        if kind == "swap_flag":
            return f"`{lesson.get('program')}` takes `{data.get('to')}`, not `{data.get('from')}`"
        if kind == "swap_path":
            return f"`{data.get('from')}` does not exist here; use `{data.get('to')}`"
        return lesson.get("hint") or "hint"

    # -- middleware: rewrite before approvals see the command -----------------

    def on_tool_request(self, tool_name: str = "", args: Optional[dict] = None, **kw) -> Optional[dict]:
        if tool_name != "terminal" or not isinstance(args, dict):
            return None
        if self.setting("mode") != "auto":
            return None
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return None
        sess = self._session(kw.get("session_id", ""), kw.get("task_id", ""))
        cwd = self._cwd(args, sess)
        scope = self._scope(args, sess, cwd)
        lessons = self.store.lessons_for(scope, kinds=REWRITE_KINDS)
        if not lessons:
            return None
        rw = E.apply_lessons(command, lessons, cwd=cwd)
        if not rw.applied or rw.command == command:
            return None
        call_id = kw.get("tool_call_id") or f"{kw.get('session_id', '')}:{hash(rw.command)}"
        with self._lock:
            self._pending[call_id] = {"lessons": rw.applied, "original": command}
            self._notes[call_id] = {"notes": rw.notes, "lessons": rw.applied, "original": command}
            if len(self._pending) > 500:
                for k in list(self._pending)[:100]:
                    self._pending.pop(k, None)
                    self._notes.pop(k, None)
        for lid in rw.applied:
            self.store.record_application(lid)
        logger.info("precog rewrote command (%s): %s", ", ".join(rw.notes), rw.command[:200])
        return {
            "args": {**args, "command": rw.command},
            "source": PLUGIN_ID,
            "reason": "; ".join(rw.notes),
            "name": "precog.rewrite",
        }

    # -- pre_tool_call: suggest mode and the known-failure tripwire ----------

    def on_pre_tool_call(self, tool_name: str = "", args: Optional[dict] = None, task_id: str = "", **kw):
        if tool_name != "terminal" or not isinstance(args, dict):
            return None
        mode = self.setting("mode")
        if mode in ("off", "learn"):
            return None
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return None
        sess = self._session(kw.get("session_id", ""), task_id)
        cwd = self._cwd(args, sess)
        scope = self._scope(args, sess, cwd)

        if mode == "suggest":
            lessons = self.store.lessons_for(scope, kinds=REWRITE_KINDS)
            fresh = [l for l in lessons if l["id"] not in sess.tripped]
            rw = E.apply_lessons(command, fresh, cwd=cwd) if fresh else None
            if rw and rw.applied:
                sess.tripped.update(rw.applied)
                return {
                    "action": "block",
                    "message": (
                        "Precog: this command failed in this project before. Learned fix: "
                        + "; ".join(rw.notes)
                        + f". Run instead:\n{rw.command}\n(Running the original again is allowed.)"
                    ),
                }

        if not self.setting("tripwire"):
            return None
        hints = self.store.lessons_for(scope, kinds=("hint",))
        if not hints:
            return None
        by_sig = {}
        for h in hints:
            if h.get("fail_sig") and h["id"] not in sess.tripped:
                by_sig.setdefault(h["fail_sig"], h)
        if not by_sig:
            return None
        for seg in E.parse_command(command):
            if not seg.tokens:
                continue
            hit = by_sig.get(E.normalized_signature(seg))
            if hit:
                sess.tripped.add(hit["id"])
                self.store.record_application(hit["id"])
                with self._lock:
                    self._pending[f"trip:{hit['id']}:{kw.get('session_id', task_id)}"] = {
                        "lessons": [hit["id"]], "original": command, "tripwire": True}
                return {
                    "action": "block",
                    "message": (
                        f"Precog: `{seg.raw.strip()}` already failed in this project "
                        f"({hit.get('error_key') or 'error'}). What worked last time: `{hit.get('fix_cmd')}`. "
                        "Adapt that instead; if you really need the original, run it again and Precog lets it through."
                    ),
                }
        return None

    # -- post_tool_call: score rewrites, learn fail -> fix pairs --------------

    def on_post_tool_call(self, tool_name: str = "", args: Optional[dict] = None, result: Any = None,
                          task_id: str = "", **kw) -> None:
        if tool_name != "terminal" or not isinstance(args, dict):
            return
        if self.setting("mode") == "off":
            return
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return
        sess = self._session(kw.get("session_id", ""), task_id)
        data = E._loads(result)
        start_cwd = self._cwd(args, sess)  # where this command started
        scope = self._scope(args, sess, start_cwd)
        if isinstance(data.get("cwd"), str) and data["cwd"]:
            sess.cwd = data["cwd"]  # where the next command will start
        segs = E.parse_command(command)
        out = E.read_terminal_result(command, data, segs)

        call_id = kw.get("tool_call_id") or ""
        with self._lock:
            pend = self._pending.pop(call_id, None) if call_id else None
            trip_keys = [k for k in self._pending if k.startswith("trip:") and k.endswith(
                f":{kw.get('session_id', task_id)}")]
        if pend and out.known:
            for lid in pend["lessons"]:
                if self.store.record_outcome(lid, not out.failed) == "disabled":
                    logger.info("precog disabled lesson %s after repeated misses", lid)
            if not out.failed:
                _broadcast("mistake.prevented", {
                    "lessons": pend["lessons"], "original": _redact(pend["original"])[:200],
                    "command": _redact(command)[:200], "project": os.path.basename(scope),
                })
        if out.known and not out.failed and trip_keys:
            # The agent took the advice after a tripwire: credit the hint once.
            with self._lock:
                for k in trip_keys:
                    p = self._pending.pop(k, None)
                    if p:
                        for lid in p["lessons"]:
                            self.store.record_outcome(lid, True)

        if not out.known:
            return
        applied = pend["lessons"] if pend else []
        if not out.failed and sess.briefed_lessons and not pend:
            followed = self._followed_briefing(sess, segs)
            for lid in followed:
                self.store.record_outcome(lid, True)
                sess.briefed_lessons.pop(lid, None)
            if followed:
                applied = followed
                _broadcast("mistake.prevented", {
                    "lessons": followed, "original": "", "command": _redact(command)[:200],
                    "project": os.path.basename(scope), "via": "briefing",
                })
        self.store.add_event(
            kw.get("session_id", "") or task_id, scope, tool_name, _redact(command), not out.failed,
            out.sig.key() if out.sig else "", _redact(out.sig.line) if out.sig else "", applied,
        )

        sess.counter += 1
        if out.failed:
            for blamed, sig in out.blames:
                if E.is_learnable(sig, out.output, scope):
                    sess.recent.append(_Failure(sess.counter, command, segs, blamed, sig, scope))
            return
        if E.is_probe_command(segs):
            return
        window = self.setting("pair_window")
        seen_programs: set = set()
        for fail in reversed(sess.recent):
            if fail.resolved or fail.scope != scope or sess.counter - fail.idx > window:
                continue
            fail_seg = fail.segs[fail.blamed]
            if fail_seg.program in seen_programs:
                fail.resolved = True
                continue
            fix_idx = E.find_fix_segment(fail_seg, fail.sig, segs)
            if fix_idx is None:
                continue
            seen_programs.add(fail_seg.program)
            lesson = E.extract_lesson(fail.command, fail.segs, fail.blamed, fail.sig,
                                      command, segs, fix_idx, fix_cwd=start_cwd)
            lesson.fail_cmd = _redact(lesson.fail_cmd)[:600]
            lesson.fix_cmd = _redact(lesson.fix_cmd)[:600]
            lesson.hint = _redact(lesson.hint)
            lid, created = self.store.upsert_lesson(scope, lesson, fail_sig=E.normalized_signature(fail_seg))
            fail.resolved = True
            sess.learned.append((lid, created, lesson, call_id))
            _broadcast("lesson.learned", {
                "id": lid, "created": created, "kind": lesson.kind, "project": os.path.basename(scope),
                "text": self.describe(self.store.get_lesson(lid) or {}),
            })
            logger.info("precog %s lesson %s (%s) in %s", "learned" if created else "reinforced", lid,
                        lesson.kind, scope)

    @staticmethod
    def _followed_briefing(sess: "_Session", segs: list) -> list:
        """Briefed lessons whose fix this successful command used (the model took the advice)."""
        hits = []
        for lid, l in list(sess.briefed_lessons.items()):
            data, key = l.get("data") or {}, l.get("key_args") or []
            for seg in segs:
                if not seg.tokens or not E.contains_in_order(seg.args, key):
                    continue
                if l["kind"] == "swap_program":
                    to = (data.get("to") or "").split()
                    if to and (seg.program == to[0] or seg.program.endswith("/" + to[0].lstrip("./"))):
                        hits.append(lid)
                        break
                elif l["kind"] == "prefix" and data.get("prefix") and data["prefix"] in seg.raw:
                    hits.append(lid)
                    break
            else:
                if l["kind"] == "prefix" and data.get("prefix") and \
                        any(data["prefix"] in s.raw for s in segs if s.tokens):
                    hits.append(lid)
        return hits

    # -- transform_tool_result: tell the model what Precog did ----------------

    def on_transform_tool_result(self, tool_name: str = "", args: Optional[dict] = None, result: Any = None,
                                 task_id: str = "", **kw) -> Optional[str]:
        if tool_name != "terminal" or not self.setting("annotate"):
            return None
        call_id = kw.get("tool_call_id") or ""
        sess = self._session(kw.get("session_id", ""), task_id)
        with self._lock:
            note = self._notes.pop(call_id, None) if call_id else None
            learned = [x for x in sess.learned if x[3] == call_id] if call_id else list(sess.learned)
            sess.learned = [x for x in sess.learned if x not in learned]
        lines = []
        if note:
            lines.append(
                f"Precog rewrote this command before it ran ({'; '.join(note['notes'])}) using lessons "
                f"{', '.join('#' + str(i) for i in note['lessons'])} learned from earlier failures. "
                f"Original: {note['original'][:200]}"
            )
        for lid, created, lesson, _ in learned:
            verb = "Learned" if created else "Reinforced"
            lines.append(f"{verb} lesson #{lid}: {self.describe(self.store.get_lesson(lid) or {})}. "
                         "Precog will apply it before this mistake can happen again.")
        if not lines:
            return None
        data = E._loads(result)
        if not isinstance(data, dict) or not data:
            return f"{result}\n\n[precog] " + " ".join(lines)
        data["precog"] = " ".join(lines)
        return json.dumps(data, ensure_ascii=False)

    # -- pre_llm_call: brief the model once per project ------------------------

    def on_pre_llm_call(self, session_id: str = "", **kw) -> Optional[dict]:
        if self.setting("mode") == "off" or not self.setting("briefing"):
            return None
        sess = self._session(session_id, kw.get("task_id", ""))
        scope = self._scope(None, sess)
        if scope in sess.briefed:
            return None
        lessons = self.store.lessons_for(scope)
        sess.briefed.add(scope)
        if not lessons:
            return None
        top = lessons[: self.setting("max_briefing")]
        for l in top:
            sess.briefed_lessons[l["id"]] = l
        auto = self.setting("mode") == "auto"
        lines = []
        for l in top:
            line = f"- {self.describe(l)}"
            if l["kind"] in REWRITE_KINDS and auto:
                line += " (Precog applies this automatically)"
            lines.append(line)
        where = "on this machine" if scope == MACHINE_SCOPE else f"in {os.path.basename(scope) or scope}"
        return {
            "context": "Precog — lessons learned from real command failures " + where + ":\n" + "\n".join(lines)
        }

    # -- reporting ------------------------------------------------------------

    def status_text(self, scope: str) -> str:
        st = self.store.stats()
        here = self.store.lessons_for(scope, enabled_only=False)
        week = self.store.stats(since=time.time() - 7 * 86400)
        mode = self.setting("mode")
        lines = [
            f"Precog · mode {mode} · project {os.path.basename(scope) or scope}",
            f"{len(here)} lessons here ({st['lessons']} total) · {st['saves']} mistakes prevented · "
            f"{week['failures']} command failures seen this week",
        ]
        for l in here[:8]:
            flag = "" if l["enabled"] else " (off)"
            where = " [machine]" if l["scope"] == MACHINE_SCOPE else ""
            lines.append(f"  #{l['id']} {self.describe(l)} · saved {l['saves']}×{where}{flag}")
        if not here:
            lines.append("  No lessons yet. Precog learns when a command fails and a later one fixes it.")
        return "\n".join(lines)
