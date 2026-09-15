"""FastAPI application: orchestrates store + reducer + dongle reader, serves the
wall/admin UI and pushes live updates over a WebSocket."""
from __future__ import annotations

import asyncio
import csv
from contextlib import asynccontextmanager
import io
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__
from .frames import Frame, DongleMessage, parse_line
from .protocol import Protocol, ROLES
from .serial_reader import SerialReader, candidate_ports
from .host import HostController
from .simulator import Simulator
from .state import GameState, DEFAULT_CONFIG
from .store import Store

log = logging.getLogger("swaptx")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

CONFIG_KEYS = {"hide_royale": bool, "hide_five_lives": bool, "dedup_window_s": float, "start_repeat_window_s": float, "lives_low": int, "hp_low": int, "hp_high": int, "time_limit_s": int,
               "online_timeout_s": int,
               "stale_game_s": int, "auto_end_grace_s": int}
RULE_KEYS = {"time_limit_s": int, "team_kill_target": int, "player_kill_target": int, "capture_target": int,
             "label": str, "game_type": str}


class Hub:
    """Everything the HTTP/WS layer touches. Single asyncio loop; ingestion is serialised."""

    def __init__(self, db_path: str, serial_port: str | None, baud: int, simulate: bool,
                 sim_opts: dict[str, Any] | None = None, replay_hours: float = 24.0):
        self.store = Store(db_path)
        self.serial_port = serial_port
        self.baud = baud
        self.simulate = simulate
        self.sim_opts = sim_opts or {}
        self.replay_hours = replay_hours
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.clients: set[WebSocket] = set()
        self.reader: Optional[SerialReader | Simulator] = None
        self._pending_events: list[dict] = []
        self._broadcast_task: Optional[asyncio.Task] = None
        self._last_game_key: tuple | None = None
        self.started_at = time.time()
        self.host = HostController(self)          # the dongle as the admin headset (off until enabled)
        self._build_state()

    # ------------------------------------------------------------ lifecycle
    def _build_state(self) -> None:
        settings = self.store.all_settings()
        self.protocol = Protocol(settings.get("protocol_overrides") or {})
        players = self.store.players()
        names = {n: r["name"] for n, r in players.items() if r.get("name")}
        overrides = {n: r["team_override"] for n, r in players.items() if r.get("team_override") is not None}
        config = settings.get("config") or {}
        team_names = {int(k): v for k, v in (settings.get("team_names") or {}).items()}
        self.state = GameState(self.protocol, names, config, overrides, team_names=team_names)

    def replay(self) -> int:
        """Rebuild state from stored frames (startup, or after a protocol re-label)."""
        self._build_state()
        since = time.time() - self.replay_hours * 3600
        open_game = self.store.latest_open_game()
        if open_game and open_game.get("started_at"):
            since = min(since, float(open_game["started_at"]) - 120)
        frames = self.store.frames_since(ts=since)
        for fr in frames:
            try:
                self.state.apply(fr)
            except Exception:  # never let one bad frame kill the replay
                log.exception("replay: frame %s failed", fr.id)
        self._persist_game(force=True)
        self._persist_history(since)
        log.info("replayed %d frames since %s", len(frames), time.strftime("%H:%M:%S", time.localtime(since)))
        return len(frames)

    def _persist_history(self, since: float) -> None:
        """The games table is derived data: after a replay (new decoder, re-labelled protocol) it
        must describe the games the replay reconstructed, not what an older decoder once wrote."""
        keep = []
        for g in self.state.games:
            if not g.get("started_at"):
                continue
            keep.append(g["id"])
            self.store.upsert_game({"id": g["id"], "started_at": g["started_at"], "ended_at": g.get("ended_at"),
                                    "status": "ended", "mode": g.get("mode"), "summary": g})
        if self.state.game.id:
            keep.append(self.state.game.id)
        self.store.prune_games(since, keep)

    def start_reader(self) -> None:
        if self.simulate:
            self.reader = Simulator(self._on_line_threadsafe, self._on_status_threadsafe, **self.sim_opts)
        elif self.serial_port == "none":
            self.reader = None                       # --no-dongle: this server only ever hears /api/ingest
        else:
            self.reader = SerialReader(self._on_line_threadsafe, self._on_status_threadsafe,
                                       port=self.serial_port, baud=self.baud)
        if self.reader is not None:
            self.reader.start()

    def stop(self) -> None:
        if self.reader:
            self.reader.stop()
        self.store.close()

    # ------------------------------------------------------------ ingestion
    def _on_line_threadsafe(self, line: str, ts: float) -> None:
        if self.loop:
            self.loop.call_soon_threadsafe(self.ingest_line, line, ts, "sim" if self.simulate else "serial")

    def _on_status_threadsafe(self, connected: bool, port: str | None) -> None:
        if self.loop:
            self.loop.call_soon_threadsafe(self._on_status, connected, port)

    def _on_status(self, connected: bool, port: str | None) -> None:
        evs = self.state.set_dongle_connection(connected, port)
        try:
            self.store.log_dongle("connected" if connected else "disconnected", port or "")
        except Exception:
            pass                                   # the store is already closed during shutdown
        if connected:
            self.host.on_dongle_connected()
        self._queue(evs)

    def ingest_line(self, line: str, ts: float, source: str = "serial") -> list[dict]:
        parsed = parse_line(line, ts=ts, source=source)
        if parsed is None:
            return []
        if isinstance(parsed, DongleMessage):
            evs = self.state.apply_dongle(parsed)
            if parsed.kind in ("boot", "legacy_boot"):
                self.store.log_dongle("boot", json.dumps(parsed.data)[:500], ts)
            self._queue(evs)
            return evs
        return self.ingest_frame(parsed)

    def ingest_frame(self, frame: Frame) -> list[dict]:
        self.store.insert_frame(frame)
        try:
            evs = self.state.apply(frame)
        except Exception:
            log.exception("reducer failed on frame %s: %r", frame.id, frame.txt)
            evs = []
        self._persist_game()
        self._queue(evs)
        try:
            self.host.on_frame(frame, evs)
        except Exception:
            log.exception("host controller failed on frame %s", frame.id)
        return evs

    def manual(self, cmd: str, arg: str | None = None) -> list[dict]:
        txt = f"MANUAL,{cmd}" + (f",{arg}" if arg is not None else "")
        return self.ingest_frame(Frame(ts=time.time(), src=None, dst=None, txt=txt, source="manual"))

    def _persist_game(self, force: bool = False) -> None:
        g = self.state.game
        if not g.id:
            return
        key = (g.id, g.phase, g.total_kills, g.ended_at)
        if key == self._last_game_key and not force:
            return
        self._last_game_key = key
        self.store.upsert_game({
            "id": g.id, "started_at": g.started_at, "ended_at": g.ended_at, "status": g.phase,
            "mode": g.mode, "summary": g.summary if g.phase == "ended" else self.state.game_summary(time.time()),
        })

    # ------------------------------------------------------------ websocket
    def persist_config(self) -> None:
        cfg = self.state.config
        self.store.set_setting("config", {k: v for k, v in cfg.items() if k in CONFIG_KEYS or k == "rules"})

    def _queue(self, events: list[dict]) -> None:
        self._pending_events.extend(events)
        if self.loop and self.clients and (self._broadcast_task is None or self._broadcast_task.done()):
            self._broadcast_task = self.loop.create_task(self._broadcast_soon())

    async def _broadcast_soon(self) -> None:
        await asyncio.sleep(0.08)   # coalesce bursts
        events, self._pending_events = self._pending_events, []
        await self.broadcast({"type": "update", "events": events, "snapshot": self.snapshot()})

    async def broadcast(self, msg: dict) -> None:
        if not self.clients:
            return
        data = json.dumps(msg, default=str)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def ticker(self) -> None:
        while True:
            await asyncio.sleep(2.0)
            try:
                evs = self.state.tick(time.time())
                if evs:
                    self._persist_game()
                    self._queue(evs)
                self.host.tick(time.time())
                if self.clients:
                    await self.broadcast({"type": "tick", "now": time.time(), "dongle": self.dongle_status()})
            except Exception:
                log.exception("tick failed")

    # ------------------------------------------------------------ views
    def snapshot(self) -> dict[str, Any]:
        snap = self.state.snapshot()
        snap["server"] = {"version": __version__, "started_at": self.started_at, "simulate": self.simulate}
        snap["host"] = self.host.status()
        return snap

    def dongle_status(self) -> dict[str, Any]:
        d = dict(self.state.dongle)
        r = self.reader
        d["reader"] = {"port": getattr(r, "port", None), "connected": getattr(r, "connected", False),
                       "last_error": getattr(r, "last_error", None), "simulate": self.simulate}
        d["frames_total"] = self.state.frames_total
        d["last_frame_ts"] = self.state.last_frame_ts
        d["ports"] = candidate_ports() if not self.simulate else ["simulator"]
        return d


def create_app(hub: Hub) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        hub.loop = asyncio.get_running_loop()
        if hub.simulate:
            hub.store.clear_history()      # a simulated world starts fresh; a real one replays its game
        hub.replay()
        hub.start_reader()
        tick = asyncio.create_task(hub.ticker())
        try:
            yield
        finally:
            tick.cancel()
            hub.stop()

    app = FastAPI(title="SWAPTX Scoreboard", version=__version__, lifespan=lifespan)
    app.state.hub = hub

    # ---- pages ----
    # Pages carry a version stamp on their script/style URLs (the newest web file's mtime) and
    # everything under /static is served no-cache, so a plain reload always gets the latest code.
    def asset_version() -> str:
        return str(int(max(p.stat().st_mtime for p in WEB_DIR.iterdir() if p.is_file())))

    def page(name: str) -> HTMLResponse:
        html = (WEB_DIR / name).read_text(encoding="utf-8").replace("__V__", asset_version())
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    @app.get("/")
    async def wall():
        return page("index.html")

    @app.get("/admin")
    async def admin():
        return page("admin.html")

    @app.middleware("http")
    async def no_cache_static(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    # ---- websocket ----
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        hub.clients.add(ws)
        try:
            await ws.send_text(json.dumps({"type": "snapshot", "snapshot": hub.snapshot(),
                                           "protocol": hub.protocol.to_dict()}, default=str))
            while True:
                msg = await ws.receive_text()
                if msg == "ping":
                    await ws.send_text(json.dumps({"type": "pong", "now": time.time()}))
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            hub.clients.discard(ws)

    # ---- state ----
    @app.get("/api/state")
    async def api_state():
        return hub.snapshot()

    @app.get("/api/health")
    async def api_health():
        return {"ok": True, "version": __version__, "frames": hub.state.frames_total,
                "phase": hub.state.game.phase, "dongle": hub.dongle_status()["reader"]}

    # ---- players / names ----
    @app.get("/api/players")
    async def api_players():
        snap = hub.snapshot()
        by_num = {p["number"]: p for p in snap["players"]}
        rows = []
        for n in range(1, 17):
            p = by_num.get(n) or {"number": n, "display_name": hub.state.names.get(n) or f"Player {n}",
                                  "name": hub.state.names.get(n), "in_game": False, "kills": 0, "deaths": 0,
                                  "team": None, "team_override": hub.state.team_overrides.get(n),
                                  "mac": f"00:00:00:00:00:{n:02x}", "last_seen": None, "online": False}
            rows.append(p)
        return rows

    @app.put("/api/players/{number}")
    async def api_set_player(number: int, body: dict):
        if not 1 <= number <= 16:
            raise HTTPException(400, "player number must be 1..16")
        name = body.get("name")
        clear_team = "team_override" in body and body["team_override"] in (None, "", "none")
        team_override = None
        if "team_override" in body and not clear_team:
            try:
                team_override = int(body["team_override"])
            except (TypeError, ValueError):
                raise HTTPException(400, "team_override must be 0..3 or null")
            if not 0 <= team_override <= 3:
                raise HTTPException(400, "team_override must be 0..3 or null")
        row = hub.store.set_player(number, name=name, team_override=team_override, clear_team=clear_team)
        if name is not None:
            hub.state.set_name(number, name)
        if clear_team:
            hub.state.set_team_override(number, None)
        elif team_override is not None:
            hub.state.set_team_override(number, team_override)
        hub._queue([])
        await hub.broadcast({"type": "update", "events": [], "snapshot": hub.snapshot()})
        return row

    # ---- team names ----
    @app.get("/api/teams")
    async def api_teams():
        from .protocol import TEAM_NAMES as DEFAULTS, TEAM_COLORS
        return [{"team": t, "color": TEAM_COLORS[t], "default": DEFAULTS[t], "name": hub.state.tname(t),
                 "custom": hub.state.team_is_custom(t)} for t in range(4)]

    @app.put("/api/teams/{team}")
    async def api_set_team(team: int, body: dict):
        if not 0 <= team <= 3:
            raise HTTPException(400, "team must be 0..3")
        hub.state.set_team_name(team, body.get("name"))
        hub.store.set_setting("team_names", {str(t): n for t, n in hub.state.team_names.items()})
        hub._persist_game(force=True)
        await hub.broadcast({"type": "update", "events": [], "snapshot": hub.snapshot()})
        return {"team": team, "name": hub.state.tname(team), "custom": hub.state.team_is_custom(team)}

    # ---- settings ----
    @app.get("/api/settings")
    async def api_settings():
        return {"config": hub.state.config, "protocol_overrides": hub.protocol.overrides,
                "defaults": DEFAULT_CONFIG}

    @app.put("/api/settings")
    async def api_set_settings(body: dict):
        cfg = dict(hub.state.config)
        changed = False
        for k, typ in CONFIG_KEYS.items():
            if k in body:
                v = body[k]
                try:
                    cfg[k] = (bool(v) if typ is bool else typ(v)) if v is not None else DEFAULT_CONFIG[k]
                except (TypeError, ValueError):
                    raise HTTPException(400, f"bad value for {k}")
                changed = True
        if "rules" in body and isinstance(body["rules"], dict):
            rules = dict(cfg.get("rules") or {})
            for k, typ in RULE_KEYS.items():
                if k in body["rules"]:
                    v = body["rules"][k]
                    if k == "game_type":
                        rules[k] = v if v in ("auto", "team", "royale") else "auto"
                        continue
                    if v in (None, "", 0, "0") and typ is not str:
                        rules[k] = None
                    else:
                        try:
                            rules[k] = typ(v)
                        except (TypeError, ValueError):
                            raise HTTPException(400, f"bad rule {k}")
            cfg["rules"] = rules
            changed = True
        if changed:
            stored = {k: v for k, v in cfg.items() if k in CONFIG_KEYS or k == "rules"}
            hub.store.set_setting("config", stored)
            prev_type = hub.state.game_type_override
            hub.state.config.update(cfg)
            needs_replay = any(k in body for k in ("dedup_window_s",
                                                    "lives_low", "start_repeat_window_s", "auto_end_grace_s"))
            if needs_replay:
                hub.replay()
            new_type = (cfg.get("rules") or {}).get("game_type") or "auto"
            if new_type != prev_type:
                hub.manual("game_type", new_type)   # recorded in history, applied from now on
            await hub.broadcast({"type": "update", "events": [], "snapshot": hub.snapshot()})
        return {"config": hub.state.config}

    # ---- protocol lab ----
    @app.get("/api/protocol")
    async def api_protocol():
        return {"registry": hub.protocol.to_dict(), "observed": hub.state.observed_dict(), "roles": ROLES}

    @app.put("/api/protocol/overrides")
    async def api_protocol_overrides(body: dict):
        overrides = body.get("overrides", body)
        if not isinstance(overrides, dict):
            raise HTTPException(400, "overrides must be an object")
        hub.store.set_setting("protocol_overrides", overrides)
        n = hub.replay()
        await hub.broadcast({"type": "update", "events": [], "snapshot": hub.snapshot()})
        await hub.broadcast({"type": "protocol", "protocol": hub.protocol.to_dict()})
        return {"replayed": n, "registry": hub.protocol.to_dict()}

    @app.post("/api/protocol/reset")
    async def api_protocol_reset():
        hub.store.set_setting("protocol_overrides", {})
        n = hub.replay()
        await hub.broadcast({"type": "update", "events": [], "snapshot": hub.snapshot()})
        return {"replayed": n}

    @app.post("/api/replay")
    async def api_replay():
        n = hub.replay()
        await hub.broadcast({"type": "update", "events": [], "snapshot": hub.snapshot()})
        return {"replayed": n}

    # ---- frames / timeline ----
    @app.get("/api/frames")
    async def api_frames(limit: int = 200, before: int | None = None):
        frames = hub.store.recent_frames(limit=min(limit, 2000), before_id=before)
        out = []
        for fr in frames:
            d = fr.to_dict()
            ev = hub.protocol.decode(fr)
            d["kind"] = ev.kind
            d["opcode"] = ev.opcode
            d["fields"] = ev.fields
            out.append(d)
        return out

    @app.get("/api/timeline")
    async def api_timeline(limit: int = 400):
        return list(hub.state.timeline)[-limit:]

    @app.post("/api/ingest")
    async def api_ingest(body: dict):
        """Feed lines as if they came from the dongle (for imports and tests)."""
        lines = body.get("lines") or ([body["line"]] if body.get("line") else [])
        evs = []
        for ln in lines:
            evs += hub.ingest_line(str(ln), time.time(), source=body.get("source", "import"))
        return {"events": evs}

    # ---- games ----
    @app.get("/api/games")
    async def api_games(limit: int = 50):
        return hub.store.games(limit=limit)

    @app.get("/api/games/{game_id}")
    async def api_game(game_id: str):
        g = hub.store.game(game_id)
        if not g:
            raise HTTPException(404, "no such game")
        if g.get("started_at"):
            frames = hub.store.frames_between(float(g["started_at"]) - 120, g.get("ended_at"))
            gs = GameState(hub.protocol, hub.state.names, hub.state.config, hub.state.team_overrides)
            for fr in frames:
                gs.apply(fr)
            g["timeline"] = [e for e in gs.timeline]
            if gs.game.id == game_id or gs.game.summary:
                g["summary"] = gs.game.summary or gs.game_summary(g.get("ended_at") or time.time())
        return g

    @app.get("/api/games/{game_id}/export")
    async def api_game_export(game_id: str, format: str = "json"):
        g = await api_game(game_id)
        if format == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["time", "game_t", "kind", "title", "detail", "players", "team", "raw", "src", "dst", "rssi"])
            for e in g.get("timeline", []):
                w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["ts"])), e.get("game_t"), e["kind"],
                            e["title"], e["detail"], " ".join(map(str, e["players"])), e.get("team"), e.get("raw"),
                            e.get("src"), e.get("dst"), e.get("rssi")])
            return Response(buf.getvalue(), media_type="text/csv",
                            headers={"Content-Disposition": f'attachment; filename="{game_id}.csv"'})
        return JSONResponse(g, headers={"Content-Disposition": f'attachment; filename="{game_id}.json"'})

    # ---- manual (display-only) game control ----
    @app.post("/api/game/{cmd}")
    async def api_game_cmd(cmd: str, body: dict | None = None):
        if cmd not in ("start", "end", "lobby", "reset"):
            raise HTTPException(400, "cmd must be start|end|lobby|reset")
        arg = None
        if cmd == "start" and body and body.get("mode") in (0, 1, "0", "1"):
            arg = str(body["mode"])
        evs = hub.manual(cmd, arg)
        return {"events": evs, "phase": hub.state.game.phase}

    # ---- dongle ----
    @app.get("/api/dongle")
    async def api_dongle():
        d = hub.dongle_status()
        d["log"] = hub.store.dongle_log(30)
        return d

    # ---- host mode: the dongle as the admin headset
    @app.get("/api/host")
    async def api_host():
        return hub.host.status()

    @app.post("/api/host")
    async def api_host_enable(body: dict):
        st = hub.host.enable(bool(body.get("enabled")))
        hub._queue([])
        return st

    @app.post("/api/host/settings")
    async def api_host_settings(body: dict):
        st = hub.host.set_settings(**{k: v for k, v in body.items()
                                      if k in ("mode", "lighting", "time", "lives", "time_limit_s", "team_kill_target")})
        hub._queue([])
        return st

    @app.post("/api/host/start")
    async def api_host_start():
        st = hub.host.start()
        hub._queue([])
        return st

    @app.post("/api/host/end")
    async def api_host_end():
        st = hub.host.end()
        hub._queue([])
        return st

    @app.post("/api/host/rate")
    async def api_host_rate(body: dict):
        try:
            rate = int(body.get("rate", 0))
        except (TypeError, ValueError):
            raise HTTPException(400, "rate must be an integer")
        st = hub.host.set_rate(rate)
        hub._queue([])
        return st

    @app.post("/api/dongle/command")
    async def api_dongle_command(body: dict):
        line = str(body.get("line", "")).strip()
        allowed = ("chan,", "scan", "status", "mode,", "autoscan,", "raw,", "help", "reboot", "led,", "all,", "survey", "ble", "rate,")
        if not line or not line.lower().startswith(allowed):
            raise HTTPException(400, "unknown dongle command")
        ok = hub.reader.write(line) if hub.reader else False
        return {"sent": ok, "line": line}

    return app
