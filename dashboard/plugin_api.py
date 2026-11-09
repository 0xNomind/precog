"""Precog backend routes for Hermes Desktop and the web dashboard.

Mounted at /api/plugins/precog/. Reads and edits the same SQLite database the
agent-side plugin writes (``<hermes home>/plugin-data/precog/precog.db``).
Handlers are plain ``def`` so FastAPI runs them in a worker thread and the
gateway's event loop never waits on SQLite.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time

from fastapi import APIRouter, HTTPException

router = APIRouter()
MACHINE_SCOPE = "*"


def _db() -> sqlite3.Connection:
    from plugins.plugin_storage import plugin_db

    conn = plugin_db("precog", "precog.db")
    conn.row_factory = sqlite3.Row
    return conn


def _query(sql: str, params=()) -> list[dict]:
    conn = _db()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:
        return []  # no lessons table yet: Precog has not seen a command
    finally:
        conn.close()


def _describe(row: dict) -> str:
    kind = row.get("kind")
    try:
        data = json.loads(row.get("data") or "{}")
        key = " ".join(json.loads(row.get("key_args") or "[]"))
    except (TypeError, ValueError):
        data, key = {}, ""
    if kind == "swap_program":
        return f"use {data.get('to')} instead of {data.get('from')}" + (f" for {key}" if key else "")
    if kind == "prefix":
        return f"run {data.get('prefix')} before {row.get('program')}" + (f" {key}" if key else "")
    if kind == "swap_flag":
        return f"{row.get('program')} takes {data.get('to')}, not {data.get('from')}"
    if kind == "swap_path":
        return f"{data.get('from')} does not exist here; use {data.get('to')}"
    return row.get("hint") or "hint"


def _lesson_view(row: dict) -> dict:
    scope = row.get("scope") or ""
    return {
        "id": row["id"],
        "kind": row["kind"],
        "text": _describe(row),
        "project": "machine" if scope == MACHINE_SCOPE else os.path.basename(scope) or scope,
        "scope": scope,
        "fail_cmd": row.get("fail_cmd") or "",
        "fix_cmd": row.get("fix_cmd") or "",
        "error": row.get("error_key") or "",
        "origin": row.get("origin") or "learned",
        "learned": row.get("learned") or 0,
        "applied": row.get("applied") or 0,
        "saves": row.get("saves") or 0,
        "misses": row.get("misses") or 0,
        "enabled": bool(row.get("enabled")),
        "created_at": row.get("created_at"),
        "last_used_at": row.get("last_used_at"),
    }


@router.get("/summary")
def summary():
    totals = _query(
        "SELECT COUNT(*) AS lessons, COALESCE(SUM(enabled),0) AS enabled, "
        "COALESCE(SUM(saves),0) AS saves, COALESCE(SUM(applied),0) AS applied FROM lessons")
    week = _query(
        "SELECT COUNT(*) AS commands, COALESCE(SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END),0) AS failures "
        "FROM events WHERE ts >= ?", (time.time() - 7 * 86400,))
    recent = _query(
        "SELECT ts, scope, command, ok, error_key, lessons FROM events "
        "WHERE lessons != '[]' ORDER BY id DESC LIMIT 8")
    t = totals[0] if totals else {"lessons": 0, "enabled": 0, "saves": 0, "applied": 0}
    w = week[0] if week else {"commands": 0, "failures": 0}
    return {
        **t,
        "week_commands": w["commands"],
        "week_failures": w["failures"],
        "recent_saves": [
            {"ts": r["ts"], "project": os.path.basename(r["scope"] or ""), "command": r["command"][:200],
             "ok": bool(r["ok"]), "lessons": json.loads(r["lessons"] or "[]")}
            for r in recent
        ],
    }


@router.get("/lessons")
def lessons():
    rows = _query("SELECT * FROM lessons ORDER BY enabled DESC, saves DESC, updated_at DESC LIMIT 300")
    return {"lessons": [_lesson_view(r) for r in rows]}


@router.post("/lessons/{lesson_id}/enabled")
def set_enabled(lesson_id: int, body: dict):
    conn = _db()
    try:
        cur = conn.execute("UPDATE lessons SET enabled=?, updated_at=? WHERE id=?",
                           (1 if body.get("enabled") else 0, time.time(), lesson_id))
        conn.commit()
    finally:
        conn.close()
    if not cur.rowcount:
        raise HTTPException(status_code=404, detail="no such lesson")
    return {"ok": True}


@router.post("/lessons/{lesson_id}/forget")
def forget(lesson_id: int):
    conn = _db()
    try:
        cur = conn.execute("DELETE FROM lessons WHERE id=?", (lesson_id,))
        conn.commit()
    finally:
        conn.close()
    if not cur.rowcount:
        raise HTTPException(status_code=404, detail="no such lesson")
    return {"ok": True}
