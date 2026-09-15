"""SQLite persistence. Raw frames are the source of truth; everything else is derived.

Tables
  frames    every line the dongle produced that parsed as a frame (plus manual synthetic frames)
  games     one row per game session we tracked (id, timestamps, summary JSON)
  players   gun number -> name / team override
  settings  key -> JSON (protocol overrides, display rules, wording, ...)
  dongle    dongle lifecycle log (connect / disconnect / boot)
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from .frames import Frame

SCHEMA = """
CREATE TABLE IF NOT EXISTS frames (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  src TEXT, dst TEXT, txt TEXT NOT NULL, hex TEXT,
  rssi INTEGER, channel INTEGER, seq INTEGER, rate INTEGER, dongle_ms INTEGER,
  source TEXT NOT NULL DEFAULT 'serial',
  raw_line TEXT
);
CREATE INDEX IF NOT EXISTS frames_ts ON frames(ts);
CREATE TABLE IF NOT EXISTS games (
  id TEXT PRIMARY KEY,
  started_at REAL, ended_at REAL, status TEXT, mode INTEGER,
  first_frame_id INTEGER, last_frame_id INTEGER,
  summary TEXT
);
CREATE TABLE IF NOT EXISTS players (
  number INTEGER PRIMARY KEY,
  name TEXT, team_override INTEGER, updated_at REAL
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS dongle (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, detail TEXT
);
"""


class Store:
    def __init__(self, path: str | Path = "data/swaptx.db"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ---------------------------------------------------------------- frames
    def insert_frame(self, f: Frame) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO frames(ts,src,dst,txt,hex,rssi,channel,seq,rate,dongle_ms,source,raw_line)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (f.ts, f.src, f.dst, f.txt, f.hex, f.rssi, f.channel, f.seq, f.rate, f.dongle_ms,
                 f.source, f.raw_line))
            self.conn.commit()
            f.id = int(cur.lastrowid)
            return f.id

    def _row_to_frame(self, r: sqlite3.Row) -> Frame:
        return Frame(ts=r["ts"], src=r["src"], dst=r["dst"], txt=r["txt"], hex=r["hex"], rssi=r["rssi"],
                     channel=r["channel"], seq=r["seq"], rate=r["rate"], dongle_ms=r["dongle_ms"],
                     source=r["source"], id=r["id"], raw_line=r["raw_line"] or "")

    def frames_since(self, ts: float | None = None, after_id: int | None = None, limit: int | None = None) -> list[Frame]:
        q = "SELECT * FROM frames WHERE 1=1"
        args: list[Any] = []
        if ts is not None:
            q += " AND ts >= ?"
            args.append(ts)
        if after_id is not None:
            q += " AND id > ?"
            args.append(after_id)
        q += " ORDER BY id ASC"
        if limit:
            q += f" LIMIT {int(limit)}"
        with self._lock:
            return [self._row_to_frame(r) for r in self.conn.execute(q, args)]

    def frames_between(self, start_ts: float, end_ts: float | None) -> list[Frame]:
        q = "SELECT * FROM frames WHERE ts >= ?"
        args: list[Any] = [start_ts]
        if end_ts is not None:
            q += " AND ts <= ?"
            args.append(end_ts)
        q += " ORDER BY id ASC"
        with self._lock:
            return [self._row_to_frame(r) for r in self.conn.execute(q, args)]

    def recent_frames(self, limit: int = 200, before_id: int | None = None) -> list[Frame]:
        q = "SELECT * FROM frames"
        args: list[Any] = []
        if before_id is not None:
            q += " WHERE id < ?"
            args.append(before_id)
        q += f" ORDER BY id DESC LIMIT {int(limit)}"
        with self._lock:
            rows = list(self.conn.execute(q, args))
        return [self._row_to_frame(r) for r in reversed(rows)]

    def all_frames(self) -> Iterable[Frame]:
        with self._lock:
            rows = list(self.conn.execute("SELECT * FROM frames ORDER BY id ASC"))
        return [self._row_to_frame(r) for r in rows]

    def clear_history(self) -> None:
        """Forget every frame and game (names and settings stay). Used by simulation mode."""
        with self._lock:
            self.conn.execute("DELETE FROM frames")
            self.conn.execute("DELETE FROM games")
            self.conn.execute("DELETE FROM dongle")
            self.conn.commit()

    def frame_count(self) -> int:
        with self._lock:
            return int(self.conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0])

    def last_frame_ts(self) -> Optional[float]:
        with self._lock:
            r = self.conn.execute("SELECT MAX(ts) FROM frames").fetchone()
        return r[0] if r and r[0] is not None else None

    # ----------------------------------------------------------------- games
    def upsert_game(self, g: dict[str, Any]) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO games(id,started_at,ended_at,status,mode,first_frame_id,last_frame_id,summary)"
                " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET started_at=excluded.started_at,"
                " ended_at=excluded.ended_at,status=excluded.status,mode=excluded.mode,"
                " first_frame_id=COALESCE(games.first_frame_id,excluded.first_frame_id),"
                " last_frame_id=excluded.last_frame_id,summary=excluded.summary",
                (g["id"], g.get("started_at"), g.get("ended_at"), g.get("status"), g.get("mode"),
                 g.get("first_frame_id"), g.get("last_frame_id"), json.dumps(g.get("summary")) if g.get("summary") is not None else None))
            self.conn.commit()

    def prune_games(self, since: float, keep_ids: list[str]) -> None:
        """Drop game rows from `since` on that a replay did not reproduce (stale decoder output)."""
        with self._lock:
            rows = [r["id"] for r in self.conn.execute("SELECT id FROM games WHERE started_at >= ?", (since,))]
            stale = [i for i in rows if i not in set(keep_ids)]
            if stale:
                self.conn.executemany("DELETE FROM games WHERE id=?", [(i,) for i in stale])
                self.conn.commit()

    def games(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self.conn.execute("SELECT * FROM games ORDER BY started_at DESC LIMIT ?", (limit,)))
        out = []
        for r in rows:
            d = dict(r)
            d["summary"] = json.loads(d["summary"]) if d.get("summary") else None
            out.append(d)
        return out

    def game(self, game_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            r = self.conn.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["summary"] = json.loads(d["summary"]) if d.get("summary") else None
        return d

    def latest_open_game(self) -> Optional[dict[str, Any]]:
        with self._lock:
            r = self.conn.execute("SELECT * FROM games WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1").fetchone()
        return dict(r) if r else None

    # --------------------------------------------------------------- players
    def players(self) -> dict[int, dict[str, Any]]:
        with self._lock:
            rows = list(self.conn.execute("SELECT * FROM players"))
        return {int(r["number"]): dict(r) for r in rows}

    def set_player(self, number: int, name: str | None = None,
                   team_override: int | None = None, clear_team: bool = False) -> dict[str, Any]:
        with self._lock:
            cur = self.conn.execute("SELECT * FROM players WHERE number=?", (number,)).fetchone()
            row = dict(cur) if cur else {"number": number, "name": None, "team_override": None}
            if name is not None:
                row["name"] = name.strip() or None
            if clear_team:
                row["team_override"] = None
            elif team_override is not None:
                row["team_override"] = int(team_override)
            self.conn.execute(
                "INSERT INTO players(number,name,team_override,updated_at) VALUES(?,?,?,?)"
                " ON CONFLICT(number) DO UPDATE SET name=excluded.name,"
                " team_override=excluded.team_override,updated_at=excluded.updated_at",
                (number, row["name"], row["team_override"], time.time()))
            self.conn.commit()
            return row

    # -------------------------------------------------------------- settings
    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            r = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock:
            self.conn.execute("INSERT INTO settings(key,value,updated_at) VALUES(?,?,?)"
                              " ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                              (key, json.dumps(value), time.time()))
            self.conn.commit()

    def all_settings(self) -> dict[str, Any]:
        with self._lock:
            rows = list(self.conn.execute("SELECT key,value FROM settings"))
        return {r["key"]: json.loads(r["value"]) for r in rows}

    # ---------------------------------------------------------------- dongle
    def log_dongle(self, kind: str, detail: str = "", ts: float | None = None) -> None:
        with self._lock:
            self.conn.execute("INSERT INTO dongle(ts,kind,detail) VALUES(?,?,?)", (ts or time.time(), kind, detail))
            self.conn.commit()

    def dongle_log(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self.conn.execute("SELECT * FROM dongle ORDER BY id DESC LIMIT ?", (limit,)))
        return [dict(r) for r in rows]
