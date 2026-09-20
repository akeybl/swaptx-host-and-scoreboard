"""Game state reducer: frames in, scoreboard/timeline out.

Design rules
* The reducer is deterministic and uses only ``frame.ts`` for time, so replaying
  the stored frames after a restart rebuilds exactly the same state.
* It never relies on raw token indexes - only on the semantic roles assigned in
  :mod:`swaptx.protocol` - so re-labelling a field and replaying fixes history.
* We are a passive listener: nothing here sends anything. "Manual" game
  start/end from the admin UI are recorded as synthetic frames (``MANUAL,...``)
  so they replay too.
"""
from __future__ import annotations

import time
from collections import Counter, deque
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from .frames import Frame, DongleMessage, BROADCAST, HOST_MAC
from .protocol import (WINNER_DRAW, Protocol, Event, describe_settings, team_name, TEAM_NAMES, FFA_TEAM,
                       TEAM_COLORS, NO_TEAM, GAME_KINDS, settings_are_placeholders, decode_winner,
                       LIVES_FRESH, LIVES_UNLIMITED)

DEFAULT_CONFIG: dict[str, Any] = {
    "dedup_window_s": 3.0,          # identical payloads inside this window are one event
    "start_repeat_window_s": 8.0,   # repeated start beacons inside this window don't restart
    "lives_low": 5,                 # Health/Lives "Low" in Team Battle / FFA = 5 lives (hearts)
    "hp_low": 200,                  # the same setting in Battle Royale is hit points: Low = 200
    "hp_high": 500,                 #                                                  High = 500
    "time_limit_s": 600,            # Time/Storm "On" = 10 minutes (team battle)
    "storm_start_s": 180,           # Battle Royale with the storm on: first siren ~3:00 after the start beacon
    "storm_siren_s": 120,           # ... and a siren every 2:00 after that (measured 2026-09-13, silent on the air)
    "online_timeout_s": 120,        # player considered offline after this silence
    "stale_game_s": 1800,           # live game with no traffic this long is flagged stale
    "timeline_max": 400,
    "auto_end_grace_s": 45,         # timed game: end it this long after the limit if no game-over beacon was heard (0 = never)
    "post_end_grace_s": 20,         # after a game-over, stray hits and deaths never start a new game for this long
    "late_kill_window_s": 3,        # ... a death this soon after the end still counts, in the game that just ended
    "death_retry_s": 4,             # the same death from the same headset within this window is a retry (retries came 2.5 s apart)
    "hide_royale": True,            # the board's own hosting offers Team Battle only (a gun hosting Royale is still followed)
    "hide_five_lives": True,        # ... and unlimited lives only (the five-lives option is hidden)
    "rules": {                      # display-only game rules (we are not the host)
        "time_limit_s": None, "team_kill_target": None, "player_kill_target": None,
        "capture_target": None, "label": "", "game_type": "auto",   # auto | team | royale
    },
}


def plural(n: int, word: str) -> str:
    """'1 kill', '3 kills'."""
    return f"{n} {word}{'' if n == 1 else 's'}"


@dataclass
class Player:
    number: int                           # 1..16, as spoken by the gun
    name: Optional[str] = None
    team: Optional[int] = None            # heard on the radio in the CURRENT game (0 red 1 blue 2 yellow 3 green)
    team_override: Optional[int] = None   # set from the admin UI
    team_inferred: Optional[int] = None   # deduced this game: a tag proves tagger and victim differ
    team_carried: Optional[int] = None    # last team heard in a previous game (players usually keep it)
    team_not: list = field(default_factory=list)  # teams this game's tags have ruled out (who tagged them)
    mac: Optional[str] = None
    # session presence
    first_seen: Optional[float] = None
    last_seen: Optional[float] = None
    rssi: Optional[int] = None
    rssi_avg: Optional[float] = None
    frames: int = 0
    # per-game
    in_game: bool = False
    joined_at: Optional[float] = None
    late_join: bool = False
    kills: int = 0
    deaths: int = 0
    team_kills: int = 0                   # friendly fire kills
    captures: int = 0
    score_reported: Optional[int] = None  # last running score the gun announced
    score_base: int = 0                   # added to reported score after a gun restart
    gun_restarts: int = 0
    streak: int = 0
    best_streak: int = 0
    death_streak: int = 0
    worst_death_streak: int = 0
    alive: bool = True
    eliminated: bool = False
    lives_left: Optional[int] = None
    lives_start: Optional[int] = None     # lives at the start of this game (None = unlimited)
    hits_taken: int = 0                   # damage messages heard (not hits: ~one per quarter of health)
    hits_dealt: int = 0
    alive_since: Optional[float] = None
    time_alive: float = 0.0
    last_kill_at: Optional[float] = None
    last_death_at: Optional[float] = None
    first_blood: bool = False
    kills_by_victim: dict = field(default_factory=dict)
    deaths_by_killer: dict = field(default_factory=dict)
    kill_times: list = field(default_factory=list)
    death_times: list = field(default_factory=list)
    host_acked: bool = False
    out_at: Optional[float] = None        # unicast game-over to this player (arcade end)

    @property
    def effective_team(self) -> Optional[int]:
        # radio truth for this game beats a manual override, which beats last game's team
        if self.team is not None:
            return self.team
        if self.team_override is not None:
            return self.team_override
        if self.team_inferred is not None:
            return self.team_inferred
        return self.team_carried

    @property
    def team_source(self) -> Optional[str]:
        if self.team is not None:
            return "radio"
        if self.team_override is not None:
            return "override"
        if self.team_inferred is not None:
            return "inferred"
        if self.team_carried is not None:
            return "carried"
        return None

    def new_round(self, carry: bool = True) -> None:
        """Start of a new lobby/game: per-game stats go; the radio team becomes a guess for
        the next round only if the round that just ended was a team game. In Free for All
        and Royale the colour token is meaningless, so carrying it would poison the next
        team battle with fake friendly fire."""
        self.reset_game()
        if carry and self.team is not None:
            self.team_carried = self.team
        elif not carry:
            self.team_carried = None
        self.team = None
        self.team_inferred = None

    def reset_game(self) -> None:
        for k, v in dict(in_game=False, joined_at=None, late_join=False, kills=0, deaths=0,
                         team_kills=0, captures=0, score_reported=None, score_base=0,
                         gun_restarts=0, streak=0, best_streak=0, death_streak=0,
                         worst_death_streak=0, alive=True, eliminated=False, lives_left=None,
                         lives_start=None, hits_taken=0, hits_dealt=0, alive_since=None, time_alive=0.0, last_kill_at=None, last_death_at=None,
                         first_blood=False, host_acked=False, out_at=None).items():
            setattr(self, k, v)
        self.kills_by_victim = {}
        self.deaths_by_killer = {}
        self.kill_times = []
        self.death_times = []
        self.team_not = []

    def display_name(self) -> str:
        return self.name or f"Player {self.number}"

    def to_dict(self, now: float, team_names: dict | None = None) -> dict[str, Any]:
        d = asdict(self)
        d["display_name"] = self.display_name()
        t = self.effective_team
        d["effective_team"] = t
        d["team_source"] = self.team_source
        d["team_name"] = ((team_names or {}).get(t) or team_name(t)) if t is not None else None
        d["team_color"] = TEAM_COLORS.get(t) if t is not None else None
        d["kd"] = round(self.kills / self.deaths, 2) if self.deaths else float(self.kills)
        d["online"] = self.last_seen is not None and (now - self.last_seen) < 120
        d["seconds_since_seen"] = round(now - self.last_seen, 1) if self.last_seen else None
        d["nemesis"] = max(self.deaths_by_killer, key=self.deaths_by_killer.get) if self.deaths_by_killer else None
        d["favorite_victim"] = max(self.kills_by_victim, key=self.kills_by_victim.get) if self.kills_by_victim else None
        alive_now = self.time_alive + ((now - self.alive_since) if (self.alive and self.alive_since) else 0.0)
        d["time_alive_total"] = round(alive_now, 1)
        d["kills_by_victim"] = {str(k): v for k, v in self.kills_by_victim.items()}
        d["deaths_by_killer"] = {str(k): v for k, v in self.deaths_by_killer.items()}
        return d


@dataclass
class Game:
    id: Optional[str] = None
    phase: str = "idle"                   # idle | lobby | live | ended
    mode: Optional[int] = None            # 0 team battle, 1 royale
    team_order: list = field(default_factory=list)   # colours in the order they first showed on the board this game
    arcade: bool = False
    settings: dict = field(default_factory=dict)
    lobby_at: Optional[float] = None
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    end_reason: Optional[str] = None      # game_over | superseded | manual | lobby
    winner_player: Optional[int] = None
    winner_team: Optional[int] = None
    draw: bool = False                        # the host said nobody won (36,69 winner 9)
    tied_teams: list = field(default_factory=list)     # who a draw is between: real teams sharing first place ...
    tied_players: list = field(default_factory=list)   # ... or players sharing the top kills (lone wolves)
    host_mac: Optional[str] = None
    host_seen_at: Optional[float] = None
    last_traffic_at: Optional[float] = None
    total_kills: int = 0
    first_blood_player: Optional[int] = None
    first_blood_at: Optional[float] = None
    start_source: str = ""                # beacon | ack | manual | arcade
    settings_source: str = ""             # beacon | ack | manual
    played_kind: Optional[str] = None     # team | royale as it was when the game ran
    team_objective_points: dict = field(default_factory=dict)
    summary: Optional[dict] = None        # filled at end


class GameState:
    def __init__(self, protocol: Protocol | None = None, names: dict[int, str] | None = None,
                 config: dict[str, Any] | None = None, team_overrides: dict[int, int] | None = None,
                 team_names: dict[int, str] | None = None):
        self.protocol = protocol or Protocol()
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self.config["rules"] = {**DEFAULT_CONFIG["rules"], **((config or {}).get("rules") or {})}
        self.players: dict[int, Player] = {}
        # the admin's game-type choice; changes arrive as MANUAL,game_type frames so a replay
        # applies them at the moment they were made instead of rewriting finished games
        self.game_type_override: str = (self.config.get("rules") or {}).get("game_type") or "auto"
        self._death_seen: dict[tuple, float] = {}       # (src, dst, killer, victim) -> last time heard
        self.names: dict[int, str] = dict(names or {})
        self.team_names: dict[int, str] = {int(k): v for k, v in (team_names or {}).items() if v}
        self.team_overrides: dict[int, int] = dict(team_overrides or {})
        self.game = Game()
        self.games: list[dict] = []                  # finished game summaries (this process)
        self.timeline: deque = deque(maxlen=int(self.config["timeline_max"]))
        self.event_seq = 0
        self.frames_total = 0
        self.frames_dup = 0
        self.last_frame_ts: Optional[float] = None
        self._dedup: dict[tuple, float] = {}
        self.dongle: dict[str, Any] = {"connected": False, "port": None, "channel": None,
                                       "mode": None, "fw": None, "boots": 0, "last_line_ts": None,
                                       "frames": 0, "status": {}}
        # protocol lab observations
        self.observed: dict[str, dict] = {}
        for n, name in self.names.items():
            self._player(n).name = name
        for n, t in self.team_overrides.items():
            self._player(n).team_override = t

    # ------------------------------------------------------------------ utils
    def _player(self, number: int, ts: float | None = None) -> Player:
        p = self.players.get(number)
        if p is None:
            p = Player(number=number, name=self.names.get(number),
                       team_override=self.team_overrides.get(number),
                       mac=f"00:00:00:00:00:{number:02x}")
            self.players[number] = p
        if ts is not None:
            if p.first_seen is None:
                p.first_seen = ts
            p.last_seen = ts
            p.frames += 1
        return p

    def _emit(self, ts: float, kind: str, title: str, detail: str = "", *, players: list[int] | None = None,
              team: int | None = None, frame: Frame | None = None, severity: str = "info",
              data: dict | None = None, wall: bool = True, feed: bool = True) -> dict:
        self.event_seq += 1
        gt = None
        if self.game.started_at is not None and self.game.phase in ("live", "ended"):
            end = self.game.ended_at if self.game.phase == "ended" else None
            gt = round(max(0.0, (min(ts, end) if end else ts) - self.game.started_at), 1)
        ev = {
            "id": self.event_seq, "ts": ts, "game_t": gt, "kind": kind, "title": title,
            "detail": detail, "players": players or [], "team": team, "severity": severity,
            "wall": wall, "feed": feed, "game_id": self.game.id, "frame_id": frame.id if frame else None,
            "raw": frame.txt if frame else None, "src": frame.src if frame else None,
            "dst": frame.dst if frame else None, "rssi": frame.rssi if frame else None,
            "data": data or {},
        }
        self.timeline.append(ev)
        return ev

    def pname(self, n: Optional[int]) -> str:
        if n is None:
            return "Unknown"
        return self._player(n).display_name()

    def set_name(self, number: int, name: Optional[str]) -> None:
        name = (name or "").strip() or None
        if name:
            self.names[number] = name
        else:
            self.names.pop(number, None)
        self._player(number).name = name

    def tname(self, t: Optional[int]) -> str:
        """Team display name: the admin's custom name for that colour, else the colour."""
        if t is None:
            return "No team"
        return self.team_names.get(t) or team_name(t)

    def team_is_custom(self, t: Optional[int]) -> bool:
        return t is not None and bool(self.team_names.get(t))

    def set_team_name(self, t: int, name: Optional[str]) -> None:
        name = (name or "").strip()
        if t == FFA_TEAM:
            name = ""                    # the yellow pick is always Free for all: not a team, never renamed
        if name and name.lower() != team_name(t).lower():
            self.team_names[t] = name
        else:
            self.team_names.pop(t, None)

    def set_team_override(self, number: int, team: Optional[int]) -> None:
        if team is None:
            self.team_overrides.pop(number, None)
        else:
            self.team_overrides[number] = team
        self._player(number).team_override = team

    # ---------------------------------------------------------------- kind
    def game_kind(self) -> str:
        """team | royale. Battle Royale is the host's mode 1; everything else is a Team Battle.
        Free for All is not a game kind: it is the fourth team pick (token 2), and a player on it
        is a side of their own inside either kind, however many of them there are."""
        gt = self.game_type_override
        if gt in ("team", "royale"):
            return gt
        return "royale" if self.game.mode == 1 else "team"

    def squads(self) -> bool:
        """Battle Royale played in teams: the gun still lets players pick a colour, so a royale
        in which a real colour (not the free-for-all pick) has been heard is last squad standing."""
        return self.game_kind() == "royale" and any(
            p.team is not None and p.team != FFA_TEAM for p in self.players.values() if p.in_game)

    def _sides(self, players: list) -> list[tuple[Optional[int], list]]:
        """The sides in play: one per real colour, and every free-for-all (or unknown) player alone."""
        by_team: dict[int, list] = {}
        sides: list[tuple[Optional[int], list]] = []
        for p in players:
            t = p.effective_team
            if t is None or t == FFA_TEAM:
                sides.append((None, [p]))
            else:
                by_team.setdefault(t, []).append(p)
        return [(t, ps) for t, ps in by_team.items()] + sides

    def _carry_teams(self) -> bool:
        """Teams learned in the game that just ran carry into the next round only if that
        game had teams (a team battle or a squads royale). The free-for-all pick carries nothing."""
        kind = self.game.played_kind or self.game_kind()
        return kind == "team" or (kind == "royale" and self.squads())

    def tick(self, now: float) -> list[dict]:
        """Time-driven checks (no frame needed): end a timed game whose limit passed."""
        return self._check_time_expiry(now)

    def _check_time_expiry(self, now: float) -> list[dict]:
        g = self.game
        grace = float(self.config.get("auto_end_grace_s") or 0)
        if grace <= 0 or g.phase != "live" or g.started_at is None:
            return []
        if g.settings.get("time") != 1 or self.game_kind() == "royale":
            return []            # only the guns' own 10-minute timer; storm mode never auto-ends
        limit = float(self.config["time_limit_s"])
        deadline = g.started_at + limit
        if now < deadline + grace:
            return []
        if any(k > deadline for p in self.players.values() for k in p.kill_times):
            return []            # the guns are clearly still playing; leave it to the host beacon
        return self._end_game(deadline, "time_expired", None)

    # -------------------------------------------------------------- ingestion
    def apply_dongle(self, msg: DongleMessage) -> list[dict]:
        out = []
        d = self.dongle
        d["last_line_ts"] = msg.ts
        d["connected"] = True
        if msg.kind in ("boot", "legacy_boot"):
            d["boots"] += 1
            d["fw"] = msg.data.get("fw") or d.get("fw")
            d["mode"] = msg.data.get("mode") or d.get("mode")
            d["channel"] = msg.data.get("ch") or d.get("channel")
            out.append(self._emit(msg.ts, "dongle", "Dongle (re)started",
                                  f"{d['fw'] or 'stock transceiver firmware'} on channel {d['channel'] or '?'}",
                                  severity="system", wall=False))
        elif msg.kind == "lock":
            d["channel"] = msg.data.get("ch", d["channel"])
            out.append(self._emit(msg.ts, "dongle", f"Locked to channel {d['channel']}", severity="system", wall=False))
        elif msg.kind == "scan":
            d["channel"] = msg.data.get("ch", d["channel"])
        elif msg.kind == "status":
            d["status"] = msg.data
            d["channel"] = msg.data.get("ch", d["channel"])
            d["mode"] = msg.data.get("mode", d["mode"])
            d["fw"] = msg.data.get("fw") or d.get("fw")      # the boot line is missed when the board was already up
        elif msg.kind == "error":
            out.append(self._emit(msg.ts, "dongle", "Dongle error", str(msg.data), severity="warn", wall=False))
        return out

    def set_dongle_connection(self, connected: bool, port: str | None, ts: float | None = None) -> list[dict]:
        ts = ts or time.time()
        was = self.dongle["connected"]
        self.dongle["connected"] = connected
        self.dongle["port"] = port
        if connected and not was:
            return [self._emit(ts, "dongle", "Dongle connected", port or "", severity="system", wall=False)]
        if not connected and was:
            return [self._emit(ts, "dongle", "Dongle disconnected", port or "", severity="warn", wall=False)]
        return []

    def apply(self, frame: Frame) -> list[dict]:
        """Apply one frame; returns the timeline events it produced (possibly none)."""
        ts = frame.ts
        self.frames_total += 1
        self.last_frame_ts = ts
        self.dongle["frames"] += 1
        pre = self._check_time_expiry(ts)   # a timed game may have ended before this frame arrived
        self.game.last_traffic_at = ts
        ev = self.protocol.decode(frame)
        self._observe(ev)

        # presence bookkeeping for the sender
        if ev.src_player:
            p = self._player(ev.src_player, ts)
            if frame.rssi is not None:
                p.rssi = frame.rssi
                p.rssi_avg = frame.rssi if p.rssi_avg is None else round(p.rssi_avg * 0.8 + frame.rssi * 0.2, 1)
            if self.game.phase == "live" and not p.in_game and ev.kind not in ("report_in",):
                p.in_game = True
                p.joined_at = p.joined_at or ts
                if p.alive_since is None:
                    p.alive_since = ts
                self._init_lives(p)
        if frame.src == HOST_MAC:
            self.game.host_mac = frame.src
            self.game.host_seen_at = ts

        # manual synthetic frames
        if ev.tokens and ev.tokens[0] == "MANUAL":
            out = self._manual(ev, frame)
            self._note_team_order()
            return out

        # dedup identical payloads (retransmits / relays)
        key = (frame.src, frame.seq, ev.opcode, tuple(ev.tokens))
        last = self._dedup.get(key)
        self._dedup[key] = ts
        if last is not None and 0 <= ts - last < float(self.config["dedup_window_s"]):
            self.frames_dup += 1
            return pre
        if len(self._dedup) > 2000:
            cutoff = ts - 60
            self._dedup = {k: v for k, v in self._dedup.items() if v >= cutoff}

        handler = getattr(self, f"_on_{ev.kind}", None)
        out = pre + (self._on_other(ev, frame) if handler is None else handler(ev, frame))
        self._note_team_order()
        return out

    def _note_team_order(self) -> None:
        """The wall's team columns keep the order in which colours first showed up this game
        (a carried-over or worked-out team counts), so a column never jumps when another
        colour is finally heard. The game's kind is refreshed here too: a Free for All only
        reveals itself once the first elimination carries the free-for-all pick."""
        if self.game.phase == "live":
            self.game.played_kind = self.game_kind()
        for p in self.players.values():
            t = p.effective_team
            if p.in_game and t is not None and t not in self.game.team_order:
                self.game.team_order.append(t)

    # ----------------------------------------------------------- protocol lab
    def _observe(self, ev: Event) -> None:
        key = ev.opcode or ev.kind
        o = self.observed.setdefault(key, {"count": 0, "kind": ev.kind, "tokens": {}, "examples": [],
                                           "src_roles": Counter(), "dst_roles": Counter(), "lengths": Counter()})
        o["count"] += 1
        o["src_roles"][ev.src_role] += 1
        o["dst_roles"][ev.dst_role] += 1
        o["lengths"][len(ev.tokens)] += 1
        if len(o["examples"]) < 8 and ev.frame.txt not in o["examples"]:
            o["examples"].append(ev.frame.txt)
        for i, tok in enumerate(ev.tokens):
            t = o["tokens"].setdefault(str(i), {"values": Counter(), "eq_src0": 0, "eq_src1": 0,
                                                "eq_dst0": 0, "eq_dst1": 0})
            t["values"][tok] += 1
            try:
                v = int(tok)
            except ValueError:
                continue
            if ev.src_player is not None:
                t["eq_src0"] += int(v == ev.src_player - 1)
                t["eq_src1"] += int(v == ev.src_player)
            if ev.dst_player is not None:
                t["eq_dst0"] += int(v == ev.dst_player - 1)
                t["eq_dst1"] += int(v == ev.dst_player)

    # ------------------------------------------------------------ handlers
    def _on_game_control(self, ev: Event, frame: Frame) -> list[dict]:
        """36,65 from the host: phase 3 opens the lobby (settings), phase 1 starts the game.
        Every player's headset re-broadcasts it a few seconds later with origin 0; a relayed
        copy only ever stands in for a host beacon we missed."""
        f = ev.fields
        settings = describe_settings(f)
        origin = f.get("origin")
        phase = f.get("phase")
        relayed = origin == 0
        ts = frame.ts
        out: list[dict] = []
        if relayed and not frame.is_broadcast and ev.dst_player and phase is None:
            # legacy DIY host: unicast arcade start for one player
            p = self._player(ev.dst_player)
            if self.game.phase != "live":
                out += self._start_game(ts, settings, frame, "arcade")
                self.game.arcade = True
            p.in_game, p.joined_at, p.alive, p.eliminated = True, p.joined_at or ts, True, False
            p.alive_since = p.alive_since or ts
            out.append(self._emit(ts, "join", f"{p.display_name()} is in (arcade start)",
                                  "Host started this player individually", players=[p.number],
                                  team=p.effective_team, frame=frame))
            return out
        if settings and not relayed:
            self.game.settings_source = "beacon"
        if phase == 1 or (phase is None and f.get("start") == 1):
            g = self.game
            # the host repeats the start once and every headset relays it: a relay never restarts a
            # live game, and the host's own repeat inside the window is the same start
            if g.phase == "live" and (relayed or (g.started_at is not None and ts - g.started_at < float(self.config["start_repeat_window_s"]))):
                if not relayed:
                    g.settings.update(settings)
                    if isinstance(settings.get("mode"), int):
                        g.mode = settings["mode"]
                return [self._emit(ts, "settings", "Start beacon relayed by a player" if relayed else "Start beacon repeated",
                                   self._settings_line(settings), frame=frame, wall=False)]
            return self._start_game(ts, settings, frame, "relay" if relayed else "beacon")
        # phase 3 (legacy 0): lobby open / settings announced
        if self.game.phase in ("idle", "ended"):
            self._begin_lobby(ts, settings)
            out.append(self._emit(ts, "lobby", "Lobby open - pick your teams",
                                  self._settings_line(settings), frame=frame, severity="phase"))
        elif self.game.phase == "lobby":
            g = self.game
            mode = settings.get("mode")
            if not relayed and isinstance(mode, int) and isinstance(g.mode, int) and mode != g.mode:
                # A gun picks its mode when it is switched on, so a lobby whose mode changes is a
                # fresh start: everyone has to switch on again in the new mode. Nobody is in yet.
                g.settings.update(settings)
                g.mode = mode
                carry = self._carry_teams()
                for p in self.players.values():
                    p.new_round(carry)
                g.team_order = []
                label = settings.get("mode_label") or GAME_KINDS.get(self.game_kind(), "the new mode")
                out.append(self._emit(ts, "lobby", f"Mode changed to {label}",
                                      "Switch the guns off and on in the new mode to join", frame=frame, severity="phase"))
                return out
            if not relayed:
                g.settings.update(settings)
                if isinstance(mode, int):
                    g.mode = mode                            # the kind of game follows the lobby's mode
            out.append(self._emit(ts, "settings", "Lobby beacon relayed by a player" if relayed else "Host updated game settings",
                                  self._settings_line(settings), frame=frame, wall=False))
        else:  # live
            out.append(self._emit(ts, "settings", "Lobby beacon during a live game",
                                  self._settings_line(settings), frame=frame, wall=False, severity="warn"))
        return out

    def _on_host_ack(self, ev: Event, frame: Frame) -> list[dict]:
        """36,97 unicast from the host to a player that reported in. In the lobby (phase 3) its
        settings block is placeholder zeros; in a game (phase 1) it carries the real settings."""
        f = ev.fields
        ts = frame.ts
        settings = describe_settings(f)
        out: list[dict] = []
        phase = f.get("phase")
        real = bool(settings) and not settings_are_placeholders(f)
        if real and self.game.settings_source != "beacon":
            self.game.settings.update(settings)
            self.game.settings_source = "ack"
            if isinstance(settings.get("mode"), int):
                self.game.mode = settings["mode"]
        if phase == 1 and self.game.phase != "live":
            # we missed the start beacon but the host is confirming a started game
            out += self._start_game(ts, settings if real else {}, frame, "ack")
        elif phase in (0, 3) and self.game.phase in ("idle", "ended"):
            self._begin_lobby(ts, settings if real else {})
            out.append(self._emit(ts, "lobby", "Lobby open (host acknowledged a player)",
                                  self._settings_line(settings) if real else "", frame=frame, severity="phase"))
        if ev.dst_player:
            p = self._player(ev.dst_player)
            p.host_acked = True
            out.append(self._emit(ts, "ack", f"Host confirmed {p.display_name()}", players=[p.number],
                                  team=p.effective_team, frame=frame, wall=False))
        elif not out:
            out.append(self._emit(ts, "ack", "Host confirm beacon", self._settings_line(settings) or frame.txt,
                                  frame=frame, wall=False))
        return out

    def _on_host_up(self, ev: Event, frame: Frame) -> list[dict]:
        """36,90,1,1: a gun entered admin mode and its headset is now the host."""
        ts = frame.ts
        if self.game.phase in ("idle", "ended"):
            self._begin_lobby(ts, {})
        ours = frame.source == "host"
        return [self._emit(ts, "lobby", "Host is up", "This board is hosting" if ours else "A gun entered admin mode",
                           frame=frame, severity="phase")]

    def _on_host_players(self, ev: Event, frame: Frame) -> list[dict]:   # legacy name
        return self._on_host_up(ev, frame)

    def _apply_lives(self, p: Player, lives: Any, ts: float) -> None:
        """Lives token from a report-in (in a game) or a state report: 255 counts down from
        unlimited, otherwise it is the lives left. Deaths we never heard are caught up from it."""
        if not isinstance(lives, int) or lives == LIVES_FRESH:
            return
        if lives > 32:
            p.lives_left = None
            p.lives_start = None
            if LIVES_UNLIMITED - lives > p.deaths:
                p.deaths = LIVES_UNLIMITED - lives
            return
        if p.lives_start is None or p.lives_start < lives:
            p.lives_start = max(lives, 1)
        p.lives_left = lives
        if p.lives_start - lives > p.deaths:
            p.deaths = p.lives_start - lives
        if lives == 0 and p.alive:
            p.alive, p.eliminated, p.out_at = False, True, ts
            if p.alive_since is not None:
                p.time_alive += max(0.0, ts - p.alive_since)
                p.alive_since = None

    def _ensure_in_game(self, p: Player, ts: float) -> None:
        if not p.in_game:
            p.in_game, p.joined_at = True, p.joined_at or ts
            p.alive_since = p.alive_since or ts
            self._init_lives(p)

    def _on_report_in(self, ev: Event, frame: Frame) -> list[dict]:
        """36,79,<player>,<lives>,<team>,<phase>,1: the player's headset announcing itself.
        The team token is the gun's own pick, so teams are known before anyone scores."""
        ts = frame.ts
        f = ev.fields
        n = f.get("player") or ev.src_player
        if not n:
            return [self._emit(ts, "unknown", "Report-in from unknown player", frame.txt, frame=frame, wall=False)]
        p = self._player(n, ts)
        lives = f.get("lives_left")
        team = f.get("player_team")
        phase = f.get("phase")
        fresh = lives == LIVES_FRESH            # headset just linked to its gun: team token is a default
        out: list[dict] = []
        if isinstance(team, int) and 0 <= team <= 3 and not fresh:
            if p.team is not None and p.team != team:
                out.append(self._emit(ts, "team", f"{p.display_name()} switched to {self.tname(team)}",
                                      "", players=[n], team=team, frame=frame))
            p.team = team
        if self.game.phase == "live":
            if not p.in_game:
                p.in_game, p.joined_at, p.late_join = True, ts, True
                p.alive, p.eliminated, p.alive_since = True, False, ts
                self._init_lives(p)
                out.append(self._emit(ts, "join", f"{p.display_name()} joined late", "Joined the running game",
                                      players=[n], team=p.effective_team, frame=frame, severity="good"))
            else:
                out.append(self._emit(ts, "join", f"{p.display_name()} checked in", "", players=[n],
                                      team=p.effective_team, frame=frame, wall=False))
            if phase == 1:
                self._apply_lives(p, lives, ts)
        else:
            if self.game.phase in ("idle", "ended"):
                self._begin_lobby(ts, {})
            first = not p.in_game
            p.in_game, p.joined_at = True, p.joined_at or ts
            p.late_join = False
            if first:
                out.append(self._emit(ts, "join", f"{p.display_name()} is online",
                                      "Headset linked" if fresh else "Reported in to the host",
                                      players=[n], team=p.effective_team, frame=frame, severity="good"))
            else:
                out.append(self._emit(ts, "join", f"{p.display_name()} reported in again",
                                      "", players=[n], team=p.effective_team, frame=frame, wall=False))
        return out

    def _on_damage(self, ev: Event, frame: Frame) -> list[dict]:
        """36,75,<shooter>,<victim>,1 from the victim's headset to the shooter's. Roughly one per
        quarter of health lost, so it is never a hit count; it only proves both are playing."""
        ts = frame.ts
        f = ev.fields
        shooter_n = f.get("player") or ev.dst_player
        victim_n = f.get("victim") or ev.src_player
        out: list[dict] = []
        if self.game.phase != "live":
            if self._just_ended(ts):
                return [self._emit(ts, "damage", "Hit reported after the game ended", frame.txt, frame=frame, wall=False)]
            out += self._start_game(ts, {}, frame, "inferred")
        names = []
        if shooter_n:
            k = self._player(shooter_n, None)
            if self._phantom(k):
                out.append(self._emit(ts, "unknown", f"Hit credited to {k.display_name()}, a gun never heard on the radio (a misread shot)",
                                      frame.txt, frame=frame, wall=False))
            else:
                self._ensure_in_game(k, ts)
                k.hits_dealt += 1
                names.append(k.display_name())
        if victim_n:
            v = self._player(victim_n, None)
            self._ensure_in_game(v, ts)
            v.hits_taken += 1
        out.append(self._emit(ts, "damage", f"{self.pname(victim_n)} took damage" + (f" from {names[0]}" if names else ""),
                              "", players=[x for x in (shooter_n, victim_n) if x], frame=frame, wall=False))
        return out

    def _on_death(self, ev: Event, frame: Frame) -> list[dict]:
        """36,68,<killer>,<victim>,<victim team>,0,0,1 from the victim's headset to the shooter's:
        the elimination event the board is built on."""
        ts = frame.ts
        f = ev.fields
        out: list[dict] = []
        # A victim's headset resends a death until the shooter's headset answers, each burst with a
        # new sequence number: the same death from the same headset inside the window is a retry.
        key = (frame.src, frame.dst, f.get("player"), f.get("victim"))
        last = self._death_seen.get(key)
        self._death_seen[key] = ts
        if last is not None and 0 <= ts - last < float(self.config["death_retry_s"]):
            return [self._emit(ts, "retry", "Death message retried", frame.txt, frame=frame, wall=False)]
        into_ended = False
        if self.game.phase != "live":
            if self._just_ended(ts):
                if ts - (self.game.ended_at or 0) > float(self.config["late_kill_window_s"]):
                    return [self._emit(ts, "late", "Death reported after the game ended, not counted", frame.txt,
                                       frame=frame, wall=False)]
                into_ended = True             # a kill from the last moments, still that game's
            else:
                out += self._start_game(ts, {}, frame, "inferred")
        killer_n = f.get("player") or ev.dst_player
        victim_n = f.get("victim") or ev.src_player
        if victim_n and killer_n and not f.get("environment"):
            k = self._player(killer_n, None)
            if self._phantom(k):
                # a shooter no radio has ever heard from is a misread shot: the death is real, the credit is not
                v = self._player(victim_n, None)
                self._ensure_in_game(v, ts)
                vt = f.get("victim_team")
                if isinstance(vt, int) and 0 <= vt <= 3:
                    v.team = vt
                out += self._record_unattributed_death(ts, v, frame, storm=False)
                if into_ended:
                    self.game.summary = self.game_summary(ts)
                return out
        if victim_n and f.get("environment") == "storm":
            v = self._player(victim_n, None)
            self._ensure_in_game(v, ts)
            vt = f.get("victim_team")
            if isinstance(vt, int) and 0 <= vt <= 3:
                v.team = vt
            out += self._record_unattributed_death(ts, v, frame, storm=True)
            if into_ended:
                self.game.summary = self.game_summary(ts)
            return out
        if not killer_n or not victim_n:
            out.append(self._emit(ts, "unknown", "Death message without both players", frame.txt, frame=frame, wall=False))
            return out
        k = self._player(killer_n, None)
        v = self._player(victim_n, None)
        self._ensure_in_game(k, ts)
        self._ensure_in_game(v, ts)
        vt = f.get("victim_team")
        if isinstance(vt, int) and 0 <= vt <= 3:
            v.team = vt
        if v.team is None and (self.game_kind() == "team" or self.squads()) and k.effective_team is not None:
            self._infer_victim_team(k, v)
        if self.game_kind() == "team" or self.squads():
            if k.team is None and v.team is not None and v.team not in k.team_not:
                k.team_not.append(v.team)          # the killer is not on the victim's team
                self._reinfer_team(k)
            self._reinfer_teams()
        out += self._record_elimination(ts, k, v, frame)
        if into_ended:
            self.game.summary = self.game_summary(ts)   # the game-over screen follows
        return out

    def _just_ended(self, ts: float) -> bool:
        """Inside the grace after a game-over: stray hits and deaths belong to that game, or to nobody."""
        g = self.game
        return g.phase == "ended" and g.ended_at is not None and 0 <= ts - g.ended_at < float(self.config["post_end_grace_s"])

    def _phantom(self, p: Player) -> bool:
        """A player nobody has ever heard on the radio: a shooter index a headset misread from a shot."""
        return p.last_seen is None and not p.in_game

    def _record_unattributed_death(self, ts: float, v: Player, frame: Frame | None, storm: bool) -> list[dict]:
        """A death with nobody to credit: the storm (36,68,99,...) or a shooter that does not exist."""
        v.deaths += 1
        v.death_streak += 1
        v.worst_death_streak = max(v.worst_death_streak, v.death_streak)
        v.streak = 0
        v.last_death_at = ts
        v.death_times.append(ts)
        if v.alive and v.alive_since is not None:
            v.time_alive += max(0.0, ts - v.alive_since)
        v.alive_since = ts
        if v.lives_left is not None:
            v.lives_left = max(0, v.lives_left - 1)
        if self.game_kind() == "royale" or v.lives_left == 0:
            v.alive, v.eliminated, v.out_at = False, True, ts
            v.alive_since = None
        detail = f"{v.display_name()} is OUT" if v.eliminated else ""
        who = "STORM" if storm else "?"
        return [self._emit(ts, "kill", f"{who} ➜ {v.display_name()}", detail, players=[v.number],
                           team=v.effective_team, frame=frame, severity="kill",
                           data={"killer": None, "victim": v.number, "storm": storm, "unknown_shooter": not storm,
                                 "streak": 0, "first_blood": False})]

    def _record_elimination(self, ts: float, k: Player, v: Player, frame: Frame | None) -> list[dict]:
        out: list[dict] = []
        k.kills += 1
        k.streak += 1
        k.best_streak = max(k.best_streak, k.streak)
        k.death_streak = 0
        k.last_kill_at = ts
        k.kill_times.append(ts)
        self.game.total_kills += 1
        first_blood = self.game.first_blood_player is None
        if first_blood:
            self.game.first_blood_player = k.number
            self.game.first_blood_at = ts
            k.first_blood = True
        v.deaths += 1
        v.death_streak += 1
        v.worst_death_streak = max(v.worst_death_streak, v.death_streak)
        v.streak = 0
        v.last_death_at = ts
        v.death_times.append(ts)
        if v.alive and v.alive_since is not None:
            v.time_alive += max(0.0, ts - v.alive_since)
        v.alive_since = ts
        k.kills_by_victim[v.number] = k.kills_by_victim.get(v.number, 0) + 1
        v.deaths_by_killer[k.number] = v.deaths_by_killer.get(k.number, 0) + 1
        if v.lives_left is not None:
            v.lives_left = max(0, v.lives_left - 1)
        if self.game_kind() == "royale" or v.lives_left == 0:
            v.alive, v.eliminated, v.out_at = False, True, ts
            v.alive_since = None
        title = f"{k.display_name()} ➜ {v.display_name()}"
        detail = f"{v.display_name()} is OUT" if v.eliminated else ""
        # lone wolves (the free-for-all pick) can shoot each other; real teammates cannot
        same_team = (self.game_kind() == "team" and k.team is not None and v.team is not None
                     and k.team == v.team and k.team != FFA_TEAM)
        out.append(self._emit(ts, "kill", title, detail, players=[k.number, v.number], team=k.effective_team,
                              frame=frame, severity="kill",
                              data={"killer": k.number, "victim": v.number, "streak": k.streak,
                                    "first_blood": first_blood}))
        if same_team:
            out.append(self._emit(ts, "warning", f"Same-team kill heard: {k.display_name()} ➜ {v.display_name()}",
                                  "Stock guns ignore teammates: a team must be mis-assigned",
                                  players=[k.number, v.number], team=k.team, frame=frame, severity="warn", wall=False))
        if first_blood:
            out.append(self._emit(ts, "callout", "FIRST KILL", k.display_name(), players=[k.number],
                                  team=k.effective_team, severity="callout", feed=False))
        if k.streak in (3, 5, 7, 10) or (k.streak > 10 and k.streak % 5 == 0):
            label = {3: "ON A ROLL", 5: "UNSTOPPABLE", 7: "ON FIRE", 10: "LEGENDARY"}.get(k.streak, "UNREAL")
            out.append(self._emit(ts, "callout", f"{label} ×{k.streak}", k.display_name(), players=[k.number],
                                  team=k.effective_team, severity="callout", data={"streak": k.streak},
                                  feed=False))   # flashes on the wall; the kill row already says STREAK ×n
        if v.eliminated and self.game_kind() == "royale":
            alive = [p for p in self.players.values() if p.in_game and p.alive]
            if len(alive) == 1:
                out.append(self._emit(ts, "callout", "LAST ONE STANDING", alive[0].display_name(),
                                      players=[alive[0].number], team=alive[0].effective_team, severity="callout", feed=False))
            else:
                out.append(self._emit(ts, "callout", f"{len(alive)} PLAYERS LEFT", "", severity="callout_soft", wall=False))
        return out

    def _on_state_report(self, ev: Event, frame: Frame) -> list[dict]:
        """36,101,<player>,<team>,<lives>,<kills>,...: a player's own counters, sent to the host
        after each kill and at elimination / game over. Used to catch up anything we missed."""
        ts = frame.ts
        f = ev.fields
        n = f.get("player") or ev.src_player
        if not n:
            return [self._emit(ts, "unknown", "State report from unknown player", frame.txt, frame=frame, wall=False)]
        out: list[dict] = []
        if self.game.phase != "live" and self.game.phase != "ended":
            out += self._start_game(ts, {}, frame, "inferred")
        p = self._player(n, None)
        self._ensure_in_game(p, ts)
        team = f.get("player_team")
        if isinstance(team, int) and 0 <= team <= 3:
            p.team = team
        kills = f.get("kills")
        if isinstance(kills, int) and kills > p.kills:
            missed = kills - p.kills
            p.kills = kills
            p.kill_times.extend([ts] * missed)
            self.game.total_kills += missed
            if self.game.first_blood_player is None:
                self.game.first_blood_player, self.game.first_blood_at, p.first_blood = p.number, ts, True
            out.append(self._emit(ts, "kill", f"{p.display_name()} got {plural(missed, 'kill')}",
                                  "Heard from the player's own report", players=[n], team=p.effective_team,
                                  frame=frame, severity="kill", data={"killer": n, "victim": None, "streak": p.streak,
                                                                     "first_blood": False}))
            if self.game.phase == "ended":
                self.game.summary = self.game_summary(ts)
        if self.game.phase == "live":
            self._apply_lives(p, f.get("lives_left"), ts)
        out.append(self._emit(ts, "report", f"{p.display_name()} reported {plural(p.kills, 'kill')}",
                              (f"{p.lives_left} lives left" if p.lives_left is not None else "unlimited lives")
                              + (" · out" if p.eliminated else ""), players=[n], team=p.effective_team,
                              frame=frame, wall=False))
        return out

    def _on_report_ack(self, ev: Event, frame: Frame) -> list[dict]:
        n = ev.fields.get("player") or ev.dst_player
        return [self._emit(frame.ts, "ack", f"Host acknowledged {self.pname(n)}'s report" if n else "Host acknowledged a report",
                           "", players=[n] if n else [], frame=frame, wall=False)]

    def _infer_victim_team(self, k: Player, v: Player) -> None:
        """Being tagged by team X proves you are not on team X. With two teams in play that
        pins the victim's team; with more, it narrows the choice and confirms or rejects a
        carried-over guess. Until a second colour is heard the player is only known to be
        on "some other team" (team_not), which the wall shows in the open second column."""
        kt = k.effective_team
        if kt is not None and kt not in v.team_not:
            v.team_not.append(kt)
        self._reinfer_team(v)

    def _reinfer_team(self, v: Player) -> None:
        """Re-evaluate a guessed team from what this game's tags have proven so far. A guess
        sticks until the radio contradicts it, so rows don't hop around as more colours show up."""
        if v.team is not None or not v.team_not:
            return
        excluded = set(v.team_not)
        if v.team_inferred in excluded:
            v.team_inferred = None                    # contradicted: they were tagged by that team
        if v.team_carried in excluded:
            v.team_carried = None                     # last game's team is a stale guess: drop it
        if v.team_inferred is not None:
            return
        known = {p.effective_team for p in self.players.values() if p.in_game and p.effective_team is not None}
        candidates = known - excluded
        if len(candidates) == 1:
            v.team_inferred = next(iter(candidates))
        elif v.team_carried is not None:
            v.team_inferred = v.team_carried          # consistent with the evidence: promote it

    def _reinfer_teams(self) -> None:
        for p in self.players.values():
            if p.in_game and p.team is None and p.team_not:
                self._reinfer_team(p)

    def _on_game_over(self, ev: Event, frame: Frame) -> list[dict]:
        """36,69,<origin>,<winner>,1 from the host (twice), relayed by players with origin 0.
        Winner 0-3 = team colour, 10 + index = an individual."""
        ts = frame.ts
        f = ev.fields
        if not frame.is_broadcast and ev.dst_player and "winner" not in f:
            # legacy DIY host: unicast end for one (arcade) player
            p = self._player(ev.dst_player)
            p.alive, p.eliminated, p.out_at = False, True, ts
            if p.alive_since is not None:
                p.time_alive += max(0.0, ts - p.alive_since)
                p.alive_since = None
            return [self._emit(ts, "out", f"{p.display_name()} has been taken out of the game",
                               "Host ended this player's game", players=[p.number], team=p.effective_team,
                               frame=frame, severity="warn")]
        relayed = f.get("origin") == 0
        if self.game.phase != "live":
            if self.game.phase == "ended":
                return [self._emit(ts, "game_over", "Game-over beacon relayed" if relayed else "Game-over beacon (repeat)",
                                   frame.txt, frame=frame, wall=False)]
            return [self._emit(ts, "game_over", "Game over announced (no game was tracked)", frame.txt,
                               frame=frame, severity="phase")]
        if "winner" in f:
            wt, wp = decode_winner(f.get("winner"))
        else:
            wp = f.get("win_player")
            wt = f.get("win_team")
            wt = wt if isinstance(wt, int) and 0 <= wt <= 3 else None
        draw, tied_players = f.get("winner") == WINNER_DRAW, []
        if wt == FFA_TEAM:
            # the yellow pick is not a team, so a host naming it means the best of the lone wolves
            # took it; wolves sharing the top are a tie between those players
            wt = None
            wolves = [p for p in self.players.values() if p.in_game and p.effective_team == FFA_TEAM]
            if wolves:
                top = max(p.kills for p in wolves)
                leaders = [p for p in wolves if p.kills == top]
                if len(leaders) == 1:
                    wp = leaders[0].number
                else:
                    draw, tied_players = True, [p.number for p in leaders]
        if wt is not None:
            confirmed = [p for p in self.players.values() if p.in_game and p.team == wt]
            if not confirmed:
                unheard = [p for p in self.players.values() if p.in_game and p.team is None]
                if len(unheard) == 1:              # the admin's own gun never reports in: the win names its colour
                    unheard[0].team_inferred, unheard[0].team_carried = wt, None
        return self._end_game(ts, "game_over", frame, winner_player=wp, winner_team=wt,
                              draw=draw, tied_players=tied_players)

    def _on_roster(self, ev: Event, frame: Frame) -> list[dict]:
        n = ev.fields.get("player") or ev.src_player
        return [self._emit(frame.ts, "roster", f"Roster relay{(' from ' + self.pname(n)) if n else ''}", frame.txt,
                           players=[n] if n else [], frame=frame, wall=False)]

    def _on_capture(self, ev: Event, frame: Frame) -> list[dict]:
        ts = frame.ts
        f = ev.fields
        team = f.get("team")
        pl = f.get("player")
        out: list[dict] = []
        if self.game.phase != "live":
            if self._just_ended(ts):
                return [self._emit(ts, "late", "Capture reported after the game ended", frame.txt, frame=frame, wall=False)]
            out += self._start_game(ts, {}, frame, "inferred")
        if isinstance(team, int) and 0 <= team <= 3:
            self.game.team_objective_points[str(team)] = self.game.team_objective_points.get(str(team), 0) + 1
        pn = None
        if isinstance(pl, int):
            pn = pl + 1 if 0 <= pl < 16 else (pl if 1 <= pl <= 16 else None)
        if pn:
            p = self._player(pn)
            p.captures += 1
            if isinstance(team, int) and 0 <= team <= 3 and p.team is None:
                p.team = team
        # DIY LaserTagMods JBOX gear only: stock SWAPTX has no bases
        base = f"JBOX base {f.get('jbox_id')}" if f.get("jbox_id") is not None else "A JBOX base"
        out.append(self._emit(ts, "capture", f"{base} captured for {self.tname(team)}",
                              f"by {self.pname(pn)}" if pn else "", players=[pn] if pn else [],
                              team=team if isinstance(team, int) else None, frame=frame, severity="objective"))
        return out

    def _on_brx(self, ev: Event, frame: Frame) -> list[dict]:
        return [self._emit(frame.ts, "brx", f"BRX/JEDGE traffic {ev.opcode}", frame.txt, frame=frame, wall=False)]

    def _on_combat_unknown(self, ev: Event, frame: Frame) -> list[dict]:
        who = ""
        if ev.src_player or ev.dst_player:
            who = f"{self.pname(ev.src_player) if ev.src_player else ev.src_role} → " \
                  f"{self.pname(ev.dst_player) if ev.dst_player else ev.dst_role}"
        return [self._emit(frame.ts, "unknown", f"Combat-family message 36,{ev.opcode}", (who + "  " + frame.txt).strip(),
                           players=[n for n in (ev.src_player, ev.dst_player) if n], frame=frame,
                           severity="unknown", wall=False)]

    def _on_unknown_36(self, ev: Event, frame: Frame) -> list[dict]:
        return [self._emit(frame.ts, "unknown", f"Unknown message 36,{ev.opcode}", frame.txt, frame=frame,
                           players=[n for n in (ev.src_player, ev.dst_player) if n], severity="unknown", wall=False)]

    def _on_arcade_select(self, ev: Event, frame: Frame) -> list[dict]:
        return [self._emit(frame.ts, "host", f"Host targets {self.pname(ev.fields.get('player'))} for next command",
                           frame.txt, frame=frame, wall=False)]

    def _on_ota_update(self, ev: Event, frame: Frame) -> list[dict]:
        return [self._emit(frame.ts, "host", "Host firmware update command", frame.txt, frame=frame, wall=False, severity="warn")]

    def _on_other(self, ev: Event, frame: Frame) -> list[dict]:
        if ev.kind == "empty":
            return []
        return [self._emit(frame.ts, "unknown", "Unrecognised radio text", frame.txt, frame=frame,
                           players=[n for n in (ev.src_player, ev.dst_player) if n], severity="unknown", wall=False)]

    def _manual(self, ev: Event, frame: Frame) -> list[dict]:
        ts = frame.ts
        cmd = ev.tokens[1] if len(ev.tokens) > 1 else ""
        if cmd == "start":
            mode = int(ev.tokens[2]) if len(ev.tokens) > 2 and ev.tokens[2].isdigit() else None
            settings = {"mode": mode, "mode_label": {0: "Team Battle", 1: "Battle Royale"}.get(mode)} if mode is not None else {}
            return self._start_game(ts, settings, frame, "manual")
        if cmd == "end":
            if self.game.phase == "live":
                return self._end_game(ts, "manual", frame)
            return [self._emit(ts, "game_over", "Manual end (no live game)", frame=frame, wall=False)]
        if cmd == "game_type":
            val = ev.tokens[2] if len(ev.tokens) > 2 else "auto"
            self.game_type_override = val if val in ("auto", "team", "royale") else "auto"
            self.config.setdefault("rules", {})["game_type"] = self.game_type_override
            return [self._emit(ts, "system", f"Game type set to {GAME_KINDS.get(self.game_type_override, 'automatic')}",
                               "From the admin page", frame=frame, severity="system", wall=False)]
        if cmd == "lobby":
            self._begin_lobby(ts, {})
            return [self._emit(ts, "lobby", "Lobby opened from the admin page", frame=frame, severity="phase")]
        if cmd == "reset":
            carry = self._carry_teams()
            self.game = Game()
            for p in self.players.values():
                p.new_round(carry)
            self.game.team_order = []
            return [self._emit(ts, "system", "Board reset from the admin page", frame=frame, severity="system")]
        return [self._emit(ts, "system", f"Manual command {cmd}", frame.txt, frame=frame, wall=False)]

    # -------------------------------------------------------------- phases
    def _settings_line(self, s: dict) -> str:
        parts = [s.get(k) for k in ("mode_label", "lives_label", "time_label", "lighting_label") if s.get(k)]
        return " · ".join(parts)

    def _begin_lobby(self, ts: float, settings: dict) -> None:
        carry = self._carry_teams()
        if self.game.phase == "ended":
            self._archive_game(carry)
        g = self.game
        g.phase = "lobby"
        g.lobby_at = g.lobby_at or ts
        g.settings.update(settings)
        if settings:
            g.settings_source = g.settings_source or "beacon"
        if isinstance(settings.get("mode"), int):
            g.mode = settings["mode"]
        for p in self.players.values():
            p.new_round(carry)
        self.game.team_order = []

    def _init_lives(self, p: Player) -> None:
        g = self.game
        if self.game_kind() == "royale":
            p.lives_left = p.lives_start = 1
        elif g.settings.get("lives") == 0:
            p.lives_left = p.lives_start = int(self.config["lives_low"])
        else:
            p.lives_left = p.lives_start = None

    def _start_game(self, ts: float, settings: dict, frame: Frame | None, source: str) -> list[dict]:
        g = self.game
        out: list[dict] = []
        carry = self._carry_teams()
        if g.phase == "live":
            if g.started_at is not None and ts - g.started_at < float(self.config["start_repeat_window_s"]):
                g.settings.update(settings)
                if isinstance(settings.get("mode"), int):
                    g.mode = settings["mode"]
                return [self._emit(ts, "settings", "Start beacon repeated", self._settings_line(settings),
                                   frame=frame, wall=False)]
            out += self._end_game(ts, "superseded", frame)
        if g.phase == "ended":
            self._archive_game(carry)
            g = self.game
        if g.phase != "lobby":
            for p in self.players.values():
                p.new_round(carry)
            self.game.team_order = []
        g.phase = "live"
        g.started_at = ts
        g.ended_at = None
        g.end_reason = None
        g.start_source = source
        self._death_seen.clear()                 # a death in a new game is never a retry of one from the last
        g.settings.update(settings)
        if isinstance(settings.get("mode"), int):
            g.mode = settings["mode"]
        g.id = g.id or f"g{int(ts * 1000)}"
        g.played_kind = self.game_kind()
        g.total_kills = 0
        g.first_blood_player = None
        g.team_objective_points = {}
        for p in self.players.values():
            if p.in_game:
                p.alive, p.eliminated, p.alive_since = True, False, ts
                self._init_lives(p)
        players_in = [p.number for p in self.players.values() if p.in_game]
        mode = "Arcade" if source == "arcade" else GAME_KINDS[self.game_kind()]
        how = {"beacon": "", "relay": " (relayed beacon)", "ack": " (from host confirm)",
               "inferred": " (inferred from traffic)", "manual": " (manual)", "arcade": " (arcade)"}.get(source, "")
        out.append(self._emit(ts, "game_start", f"{mode.upper()} STARTED{how}",
                              self._settings_line(g.settings) + (f" · {len(players_in)} players" if players_in else ""),
                              players=players_in, frame=frame, severity="phase"))
        return out

    def _end_game(self, ts: float, reason: str, frame: Frame | None, winner_player: int | None = None,
                  winner_team: int | None = None, draw: bool = False,
                  tied_players: list[int] | None = None) -> list[dict]:
        g = self.game
        g.played_kind = self.game_kind()
        g.phase = "ended"
        g.ended_at = ts
        g.end_reason = reason
        g.draw = draw
        g.tied_teams, g.tied_players = [], list(tied_players or [])
        if draw:
            winner_team = winner_player = None
            if not g.tied_players:
                g.tied_teams, g.tied_players = self._tied_parties()
        for p in self.players.values():
            if p.in_game and p.alive and p.alive_since is not None:
                p.time_alive += max(0.0, ts - p.alive_since)
                p.alive_since = None
        if winner_team is None and winner_player is None and not draw:
            winner_team, winner_player = self._infer_winner()
        if winner_team is not None and winner_player is None and self.game_kind() != "team":
            # a colour with a single player on it is that player's win (a two-player royale): keep both
            on = [p for p in self.players.values() if p.in_game and p.effective_team == winner_team]
            if len(on) == 1:
                winner_player = on[0].number
        g.winner_player, g.winner_team = winner_player, winner_team
        g.summary = self.game_summary(ts)
        if draw:
            title = self._tie_title() or "DRAW"
        elif winner_player is not None and (winner_team is None or self.game_kind() != "team"):
            title = f"WINNER: {self.pname(winner_player).upper()}"
        elif winner_team is not None:
            tn = self.tname(winner_team).upper()
            title = f"WINNER: {tn}" if self.team_is_custom(winner_team) else f"WINNER: {tn} TEAM"   # "Winner:" needs no plural
        else:
            title = "GAME OVER"
        detail = {"game_over": "Host ended the game", "superseded": "A new game started",
                  "manual": "Ended from the admin page",
                  "time_expired": "Time ran out (no game-over beacon heard; winner inferred)"}.get(reason, reason)
        if draw:
            detail = "The host called it a draw"
        ev = self._emit(ts, "game_over", title, detail, players=[winner_player] if winner_player else [],
                        team=winner_team, frame=frame, severity="phase", data={"summary": g.summary})
        return [ev]

    def _infer_winner(self) -> tuple[Optional[int], Optional[int]]:
        g = self.game
        players = [p for p in self.players.values() if p.in_game]
        if not players:
            return None, None
        if self.game_kind() == "royale" and self.squads():
            # last side standing: a colour, or a lone wolf; several left -> most standing, then most
            # eliminations; nobody left -> the side of the last player out
            def strength(side):
                return (sum(p.alive for p in side[1]), sum(p.kills for p in side[1]))
            standing = [s for s in self._sides(players) if any(p.alive for p in s[1])]
            if standing:
                best = max(standing, key=strength)
                peers = [s for s in standing if strength(s) == strength(best)]
                if len(peers) != 1:
                    return None, None
                return (best[0], None) if best[0] is not None else (None, best[1][0].number)
            last = max(players, key=lambda p: ((p.out_at or p.last_death_at or 0), -p.number))
            t = last.effective_team
            return (t, None) if t is not None and t != FFA_TEAM else (None, last.number)
        if self.game_kind() == "royale":
            # elimination mode: placement decides. Last one standing wins; if the host stops the
            # game with several still in, the survivor with the most tags; if nobody is left, the
            # last to go out.
            alive = [p for p in players if p.alive]
            if alive:
                best = max(alive, key=lambda p: (p.kills, -p.deaths, -p.number))
            else:
                best = max(players, key=lambda p: ((p.out_at or p.last_death_at or 0), -p.number))
            return None, best.number
        # Team Battle: sides are the real colours and every lone wolf on their own. Standing beats
        # score (a side with nobody left has lost), then kills; a shared top is a tie, and nobody
        # wins a game in which nobody scored and nobody fell.
        ranked = self._ranked_sides(players)
        if not ranked:
            return None, None
        top = self._side_key(ranked[0])
        if len(ranked) > 1 and self._side_key(ranked[1]) == top:
            return None, None
        if top[0] == 0 and top[2] == 0:
            return None, None
        team, ps = ranked[0]
        return (team, None) if team is not None else (None, ps[0].number)

    def _side_key(self, side) -> tuple:
        team, ps = side
        out = bool(ps) and all(p.eliminated for p in ps)
        out_at = max((p.out_at or 0.0) for p in ps) if out else 0.0
        return (1 if out else 0, -out_at, -sum(p.kills for p in ps), sum(p.deaths for p in ps))

    def _ranked_sides(self, players) -> list:
        return sorted(self._sides(players), key=lambda s: self._side_key(s) + ((s[0] if s[0] is not None else 99), s[1][0].number))

    def _archive_game(self, carry: bool | None = None) -> None:
        if carry is None:
            carry = self._carry_teams()
        g = self.game
        if g.started_at is not None:
            self.games.append(g.summary or self.game_summary(g.ended_at or g.started_at))
            self.games = self.games[-50:]
        self.game = Game()
        for p in self.players.values():
            p.new_round(carry)
        self.game.team_order = []

    # ---------------------------------------------------------------- views
    def _team_table(self, ts: float) -> list[dict]:
        g = self.game
        teams: dict[int, dict] = {}
        for p in self.players.values():
            if not p.in_game:
                continue
            t = p.effective_team
            if t is None or not 0 <= t <= 3:
                continue
            row = teams.setdefault(t, {"team": t, "name": self.tname(t), "color": TEAM_COLORS[t], "kills": 0,
                                       "deaths": 0, "team_kills": 0, "players": [], "alive": 0, "captures": 0,
                                       "objective_points": g.team_objective_points.get(str(t), 0)})
            row["kills"] += p.kills
            row["deaths"] += p.deaths
            row["team_kills"] += p.team_kills
            row["captures"] += p.captures
            row["alive"] += int(p.alive)
            row["players"].append(p.number)
        out = []
        for row in teams.values():
            row["score"] = row["kills"] + row["objective_points"]   # a team's score is simply its KOs
            ps = [self.players[n] for n in row["players"]]
            # a side with nobody left has fallen: when it fell decides its place among the fallen
            row["out_at"] = max((p.out_at or 0.0) for p in ps) if row["alive"] == 0 and all(p.eliminated for p in ps) else None
            out.append(row)
        # Standing beats score, the way the host calls a game: a side with nobody left has lost
        # whatever its kills, and among the fallen the later exit ranks higher. Then kills.
        def key(r):
            return (r["out_at"] is not None, -(r["out_at"] or 0.0), -r["score"], -r["kills"])
        out.sort(key=lambda r: key(r) + (r["team"],))
        rank = 0
        real = [r for r in out if r["team"] != FFA_TEAM]        # lone wolves are not a team
        for i, r in enumerate(real):
            if i == 0 or key(r) != key(real[i - 1]):
                rank = i + 1
            r["rank"] = rank
        for r in out:
            r.setdefault("rank", None)
        return out

    def clock(self, now: float) -> dict[str, Any]:
        g = self.game
        rules = self.config.get("rules") or {}
        kind = self.game_kind()
        limit = rules.get("time_limit_s") if kind != "royale" else None      # house rules never time a royale
        if limit is None and g.settings.get("time") == 1 and kind != "royale":
            limit = int(self.config["time_limit_s"])
        storm = g.settings.get("time") == 1 and kind == "royale"
        elapsed = None
        if g.started_at is not None:
            end = g.ended_at if g.phase == "ended" else now
            elapsed = max(0.0, end - g.started_at)
        left = (limit - elapsed) if (limit and elapsed is not None) else None
        return {"elapsed_s": round(elapsed, 1) if elapsed is not None else None,
                "limit_s": limit, "left_s": round(left, 1) if left is not None else None,
                "overtime": bool(left is not None and left < 0), "storm": storm,
                "storm_at_s": float(self.config["storm_start_s"]) if storm else None,
                "storm_siren_s": float(self.config["storm_siren_s"]) if storm else None,
                "stale": bool(g.phase == "live" and g.last_traffic_at and now - g.last_traffic_at > float(self.config["stale_game_s"]))}

    def _tied_parties(self) -> tuple[list[int], list[int]]:
        """Who a draw is between: the sides sharing first place, teams and lone wolves alike."""
        players = [p for p in self.players.values() if p.in_game]
        ranked = self._ranked_sides(players)
        if len(ranked) < 2:
            return [], []
        top = self._side_key(ranked[0])
        if self._side_key(ranked[1]) != top or (top[0] == 0 and top[2] == 0):
            return [], []
        tied = [s for s in ranked if self._side_key(s) == top]
        return [s[0] for s in tied if s[0] is not None], [s[1][0].number for s in tied if s[0] is None]

    def _tie_title(self) -> Optional[str]:
        """'TIE: RED & BLUE', 'TIE: ZOE & LEO', '4-WAY TIE'; None when the draw is between nobody in particular."""
        g = self.game
        names = [self.tname(t) for t in g.tied_teams] + [self.pname(p) for p in g.tied_players]
        if not names:
            return None
        if len(names) > 3:
            return f"{len(names)}-WAY TIE"
        return "TIE: " + " & ".join(n.upper() for n in names)

    def _winner_label(self) -> Optional[str]:
        """Who won, as a name: the player when a colour with one player on it won a royale, else the team."""
        g = self.game
        if g.draw:
            names = [self.tname(t) for t in g.tied_teams] + [self.pname(p) for p in g.tied_players]
            return ("Tie: " + " & ".join(names)) if names else "Draw"
        if g.winner_player and (g.winner_team is None or self.game_kind() != "team"):
            return self.pname(g.winner_player)
        if g.winner_team is not None:
            return self.tname(g.winner_team) if self.team_is_custom(g.winner_team) else self.tname(g.winner_team) + " team"
        return None

    def game_summary(self, now: float) -> dict[str, Any]:
        g = self.game
        players = [p for p in self.players.values() if p.in_game]
        pl = [p.to_dict(now, self.team_names) for p in players]
        awards = []
        if players:
            mk = max(players, key=lambda p: (p.kills, -p.deaths))
            if mk.kills:
                awards.append({"key": "mvp", "label": "MVP", "player": mk.number, "value": plural(mk.kills, "kill")})
            kd_pool = [p for p in players if p.kills > 0]
            if kd_pool:
                bk = max(kd_pool, key=lambda p: (p.kills / p.deaths if p.deaths else p.kills * 10, p.kills))
                awards.append({"key": "kd", "label": "Best Ratio", "player": bk.number,
                               "value": f"{plural(bk.kills, 'kill')} · {plural(bk.deaths, 'death')}"})
            st = max(players, key=lambda p: p.best_streak)
            if st.best_streak >= 2:
                awards.append({"key": "streak", "label": "Longest Streak", "player": st.number, "value": f"{st.best_streak} in a row"})
            if g.first_blood_player:
                awards.append({"key": "first_blood", "label": "First Kill", "player": g.first_blood_player, "value": ""})
            et = max(players, key=lambda p: p.deaths)
            if et.deaths >= 2:
                awards.append({"key": "easy_target", "label": "Most Deaths", "player": et.number, "value": plural(et.deaths, 'death')})
            unt = [p for p in players if p.deaths == 0 and p.kills >= 1]
            if unt:
                u = max(unt, key=lambda p: p.kills)
                awards.append({"key": "untouchable", "label": "Untouchable", "player": u.number, "value": f"{plural(u.kills, 'kill')}, never died"})
            cap = max(players, key=lambda p: p.captures)
            if cap.captures:
                awards.append({"key": "captures", "label": "JBOX Base Captain", "player": cap.number, "value": f"{cap.captures} captures"})
            last_kill = max((p for p in players if p.last_kill_at), key=lambda p: p.last_kill_at, default=None)
            if last_kill and last_kill.number != (mk.number if mk.kills else None):
                awards.append({"key": "closer", "label": "Last Kill", "player": last_kill.number, "value": "got the final kill"})
        if self.game_kind() == "royale":
            # one life each: the ratio is just the KO count and "never KO'd" is just the winner
            awards = [a for a in awards if a["key"] not in ("kd", "untouchable")]
        dur = (g.ended_at or now) - g.started_at if g.started_at else 0
        return {
            "id": g.id, "mode": g.mode, "mode_label": g.settings.get("mode_label"), "arcade": g.arcade,
            "kind": self.game_kind(), "kind_label": GAME_KINDS[self.game_kind()], "squads": self.squads(),
            "settings": g.settings, "started_at": g.started_at, "ended_at": g.ended_at, "end_reason": g.end_reason,
            "duration_s": round(dur, 1), "winner_player": g.winner_player, "winner_team": g.winner_team, "draw": g.draw,
            "tie_title": self._tie_title(), "tied_teams": list(g.tied_teams), "tied_players": list(g.tied_players),
            "winner_label": self._winner_label(),
            "total_kills": g.total_kills,
            "kills_per_min": round(g.total_kills / (dur / 60), 2) if dur > 0 else 0,
            "players": sorted(pl, key=lambda d: (-d["kills"], d["deaths"], d["number"])),
            "teams": self._team_table(now), "awards": awards,
            "first_blood_player": g.first_blood_player,
        }

    def snapshot(self, now: float | None = None, timeline_limit: int = 120) -> dict[str, Any]:
        now = now if now is not None else time.time()
        g = self.game
        players = sorted((p.to_dict(now, self.team_names) for p in self.players.values()),
                         key=lambda d: (not d["in_game"], -d["kills"], d["deaths"], d["number"]))
        alive = [p for p in self.players.values() if p.in_game and p.alive]
        in_game = [p for p in self.players.values() if p.in_game]
        rules = self.config.get("rules") or {}
        return {
            "now": now,
            "game": {
                "id": g.id, "phase": g.phase, "mode": g.mode, "arcade": g.arcade, "team_order": g.team_order,
                "kind": self.game_kind(), "kind_label": GAME_KINDS[self.game_kind()], "squads": self.squads(),
                "mode_label": g.settings.get("mode_label") or ("Arcade" if g.arcade else None),
                "settings": g.settings, "lobby_at": g.lobby_at, "started_at": g.started_at,
                "ended_at": g.ended_at, "end_reason": g.end_reason, "start_source": g.start_source,
                "winner_player": g.winner_player, "winner_team": g.winner_team, "draw": g.draw,
            "tie_title": self._tie_title(), "tied_teams": list(g.tied_teams), "tied_players": list(g.tied_players),
                "winner_label": self._winner_label(),
                "host_seen_at": g.host_seen_at, "last_traffic_at": g.last_traffic_at,
                "total_kills": g.total_kills, "first_blood_player": g.first_blood_player,
                "players_in_game": len(in_game), "players_alive": len(alive),
                "team_objective_points": g.team_objective_points,
                "clock": self.clock(now), "rules": rules, "summary": g.summary,
            },
            "players": players,
            "teams": self._team_table(now),
            "timeline": list(self.timeline)[-timeline_limit:],
            "dongle": {**self.dongle, "frames_total": self.frames_total, "frames_dup": self.frames_dup,
                       "last_frame_ts": self.last_frame_ts,
                       "seconds_since_frame": round(now - self.last_frame_ts, 1) if self.last_frame_ts else None},
            "config": self.config,
            "names": {str(k): v for k, v in self.names.items()},
            "team_names": {str(t): self.tname(t) for t in range(4)},
            "team_custom": {str(t): self.team_is_custom(t) for t in range(4)},
            "team_overrides": {str(k): v for k, v in self.team_overrides.items()},
            "recent_games": self.games[-10:],
        }

    def observed_dict(self) -> dict[str, Any]:
        out = {}
        for op, o in self.observed.items():
            toks = {}
            for i, t in o["tokens"].items():
                vals = t["values"].most_common(12)
                toks[i] = {"values": vals, "distinct": len(t["values"]), "eq_src0": t["eq_src0"],
                           "eq_src1": t["eq_src1"], "eq_dst0": t["eq_dst0"], "eq_dst1": t["eq_dst1"]}
            out[op] = {"count": o["count"], "kind": o["kind"], "examples": o["examples"],
                       "src_roles": dict(o["src_roles"]), "dst_roles": dict(o["dst_roles"]),
                       "lengths": dict(o["lengths"]), "tokens": toks}
        return out
