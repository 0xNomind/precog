"""SQLite persistence for Precog: lessons, recent tool events and pending rewrites.

The connection factory is injected so tests can use a temporary database while
the plugin uses Hermes's per-plugin data directory (``plugin_db("precog")``).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Callable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS lessons (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint  TEXT NOT NULL,
    scope        TEXT NOT NULL,
    kind         TEXT NOT NULL,
    program      TEXT NOT NULL DEFAULT '',
    key_args     TEXT NOT NULL DEFAULT '[]',
    data         TEXT NOT NULL DEFAULT '{}',
    hint         TEXT NOT NULL DEFAULT '',
    error_key    TEXT NOT NULL DEFAULT '',
    fail_cmd     TEXT NOT NULL DEFAULT '',
    fix_cmd      TEXT NOT NULL DEFAULT '',
    fail_sig     TEXT NOT NULL DEFAULT '',
    origin       TEXT NOT NULL DEFAULT 'learned',
    learned      INTEGER NOT NULL DEFAULT 1,
    applied      INTEGER NOT NULL DEFAULT 0,
    saves        INTEGER NOT NULL DEFAULT 0,
    misses       INTEGER NOT NULL DEFAULT 0,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    last_used_at REAL,
    UNIQUE (scope, fingerprint)
);
CREATE INDEX IF NOT EXISTS lessons_scope ON lessons (scope, enabled);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    session   TEXT NOT NULL DEFAULT '',
    scope     TEXT NOT NULL DEFAULT '',
    tool      TEXT NOT NULL,
    command   TEXT NOT NULL DEFAULT '',
    ok        INTEGER NOT NULL,
    error_key TEXT NOT NULL DEFAULT '',
    error     TEXT NOT NULL DEFAULT '',
    lessons   TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""

_JSON_FIELDS = ("key_args", "data")
MACHINE_SCOPE = "*"


class Store:
    def __init__(self, connect: Callable[[], sqlite3.Connection]):
        self._connect = connect
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None

    # -- plumbing --------------------------------------------------------

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = self._connect()
            conn.row_factory = sqlite3.Row
            conn.executescript(SCHEMA)
            conn.commit()
            self._conn = conn
        return self._conn

    def _rows(self, sql: str, params=()) -> list[dict]:
        with self._lock:
            cur = self._db().execute(sql, params)
            out = []
            for r in cur.fetchall():
                d = dict(r)
                for f in _JSON_FIELDS:
                    if f in d:
                        try:
                            d[f] = json.loads(d[f])
                        except (TypeError, ValueError):
                            d[f] = [] if f == "key_args" else {}
                out.append(d)
            return out

    def _exec(self, sql: str, params=()) -> int:
        with self._lock:
            db = self._db()
            cur = db.execute(sql, params)
            db.commit()
            return cur.lastrowid or cur.rowcount

    # -- lessons ---------------------------------------------------------

    def upsert_lesson(self, scope: str, lesson, fail_sig: str = "", origin: str = "learned") -> tuple[int, bool]:
        """Insert a lesson, or reinforce the identical one. Returns (id, created)."""
        now = time.time()
        fp = lesson.fingerprint()
        with self._lock:
            row = self._rows("SELECT id, enabled, misses, saves FROM lessons WHERE scope=? AND fingerprint=?",
                             (scope, fp))
            if row:
                lid = row[0]["id"]
                # Seeing the same fix again is evidence: re-enable a lesson that a
                # stale environment had disabled, and refresh its example commands.
                self._exec(
                    "UPDATE lessons SET learned=learned+1, updated_at=?, fail_cmd=?, fix_cmd=?, hint=?, "
                    "enabled=1, misses=0 WHERE id=?",
                    (now, lesson.fail_cmd, lesson.fix_cmd, lesson.hint, lid),
                )
                return lid, False
            lid = self._exec(
                "INSERT INTO lessons (fingerprint, scope, kind, program, key_args, data, hint, error_key, "
                "fail_cmd, fix_cmd, fail_sig, origin, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fp, scope, lesson.kind, lesson.program, json.dumps(lesson.key_args),
                 json.dumps(lesson.data, sort_keys=True), lesson.hint, lesson.error_key,
                 lesson.fail_cmd, lesson.fix_cmd, fail_sig, origin, now, now),
            )
            self._maybe_promote(lesson, fp)
            return lid, True

    def _maybe_promote(self, lesson, fp: str) -> None:
        """A program-level fix seen in two different projects is a machine fact."""
        if lesson.kind not in ("swap_program", "swap_flag"):
            return
        scopes = self._rows("SELECT DISTINCT scope FROM lessons WHERE fingerprint=? AND scope != ?",
                            (fp, MACHINE_SCOPE))
        if len(scopes) >= 2 and not self._rows(
                "SELECT id FROM lessons WHERE fingerprint=? AND scope=?", (fp, MACHINE_SCOPE)):
            now = time.time()
            self._exec(
                "INSERT INTO lessons (fingerprint, scope, kind, program, key_args, data, hint, error_key, "
                "fail_cmd, fix_cmd, origin, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fp, MACHINE_SCOPE, lesson.kind, lesson.program, json.dumps(lesson.key_args),
                 json.dumps(lesson.data, sort_keys=True), lesson.hint, lesson.error_key,
                 lesson.fail_cmd, lesson.fix_cmd, "promoted", now, now),
            )

    def lessons_for(self, scope: str, *, enabled_only: bool = True, kinds: Optional[tuple] = None) -> list[dict]:
        sql = "SELECT * FROM lessons WHERE scope IN (?, ?)"
        params: list = [scope, MACHINE_SCOPE]
        if enabled_only:
            sql += " AND enabled=1"
        if kinds:
            sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
            params.extend(kinds)
        sql += " ORDER BY (scope = ?) DESC, saves DESC, learned DESC, updated_at DESC"
        params.append(MACHINE_SCOPE)
        return self._rows(sql, params)

    def all_lessons(self, limit: int = 500) -> list[dict]:
        return self._rows("SELECT * FROM lessons ORDER BY updated_at DESC LIMIT ?", (limit,))

    def get_lesson(self, lid: int) -> Optional[dict]:
        rows = self._rows("SELECT * FROM lessons WHERE id=?", (lid,))
        return rows[0] if rows else None

    def set_enabled(self, lid: int, enabled: bool) -> bool:
        return self._exec("UPDATE lessons SET enabled=?, updated_at=? WHERE id=?",
                          (1 if enabled else 0, time.time(), lid)) > 0

    def forget(self, lid: int) -> bool:
        return self._exec("DELETE FROM lessons WHERE id=?", (lid,)) > 0

    def record_application(self, lid: int) -> None:
        self._exec("UPDATE lessons SET applied=applied+1, last_used_at=? WHERE id=?", (time.time(), lid))

    def record_outcome(self, lid: int, ok: bool, disable_after: int = 2) -> Optional[str]:
        """Credit a save, or count a miss; a lesson that keeps missing turns itself off."""
        if ok:
            self._exec("UPDATE lessons SET saves=saves+1 WHERE id=?", (lid,))
            return None
        self._exec("UPDATE lessons SET misses=misses+1 WHERE id=?", (lid,))
        row = self.get_lesson(lid)
        if row and row["misses"] >= disable_after and row["misses"] > row["saves"]:
            self.set_enabled(lid, False)
            return "disabled"
        return None

    # -- events ----------------------------------------------------------

    def add_event(self, session: str, scope: str, tool: str, command: str, ok: bool,
                  error_key: str = "", error: str = "", lessons: Optional[list[int]] = None,
                  keep: int = 3000) -> None:
        self._exec(
            "INSERT INTO events (ts, session, scope, tool, command, ok, error_key, error, lessons) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (time.time(), session, scope, tool, command[:2000], 1 if ok else 0, error_key, error[:300],
             json.dumps(lessons or [])),
        )
        if self._rows("SELECT COUNT(*) AS n FROM events")[0]["n"] > keep + 200:
            self._exec("DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY id ASC LIMIT 200)")

    # -- stats -----------------------------------------------------------

    def stats(self, since: float = 0.0) -> dict:
        lessons = self._rows(
            "SELECT COUNT(*) AS n, COALESCE(SUM(saves),0) AS saves, COALESCE(SUM(applied),0) AS applied, "
            "COALESCE(SUM(enabled),0) AS enabled FROM lessons")[0]
        ev = self._rows(
            "SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END),0) AS fails, "
            "COALESCE(SUM(CASE WHEN lessons != '[]' AND ok=1 THEN 1 ELSE 0 END),0) AS saved "
            "FROM events WHERE ts >= ?", (since,))[0]
        return {
            "lessons": lessons["n"], "enabled": lessons["enabled"], "applied": lessons["applied"],
            "saves": lessons["saves"], "events": ev["n"], "failures": ev["fails"], "saves_window": ev["saved"],
        }

    def meta_get(self, key: str, default: str = "") -> str:
        rows = self._rows("SELECT v FROM meta WHERE k=?", (key,))
        return rows[0]["v"] if rows else default

    def meta_set(self, key: str, value: str) -> None:
        self._exec("INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))
