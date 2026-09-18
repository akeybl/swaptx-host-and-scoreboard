"""Host mode: the dongle plays the admin headset.

Everything the stock admin headset was seen doing on the air (docs/mapping-2026-09-13.md) is
reproduced here, and nothing else:

  host up          36,90,1,1                                  twice, when hosting starts
  lobby / settings 36,65,1,<lighting>,0,3,<time>,<mode>,<lives>,1   twice, whenever a setting changes
  ack a report-in  36,97,<host gun>,3,0,0,0,0,0,1  (lobby)  /  36,97,<host gun>,1,<settings>,1  (in a game)
  start            36,65,1,<lighting>,0,1,<time>,<mode>,<lives>,1   twice, 0.4 s apart
  ack a report     36,102,<player>,<host gun>,0,1
  game over        36,69,1,<winner>,1                         twice; winner = colour, 10 + index, or 9 = draw

The <host gun> field is the index of the host's own gun: victims send hits and deaths meant for
that gun to the host address. The board has no gun, so it claims an index of its own, one per
mode: 15 (gun ID 16) for Team Battle and 14 (gun ID 15) for Battle Royale. Nobody may use those
two IDs while the board hosts; any real gun's index would steal that gun's hits.

A gun picks its mode when it is switched on, so a mode change is a fresh start, the way a stock
admin gun reboots for it: the board announces itself again (36,90) under the other index, sends
the new lobby, drops the roster, and the wall asks for the guns to be switched on in the new mode.

Ends we decide ourselves, the way the stock host was observed to: a few seconds after the last
elimination that leaves one team (or one player) standing, or when the 10-minute timer runs out
in Team Battle. In Battle Royale the time bit is the storm, which the guns run on their own
clock (silently on the air), and the game ends when the storm leaves one side standing.

House rules (Team Battle only; the guns never hear them, the board just ends the game):
  time_limit_s      a game length of our own. The guns are told "untimed" (time bit 0) and we send
                    the game-over at the chosen time, naming the side that is standing, then ahead.
  team_kill_target  first side to this many kills wins, the moment it happens.
Both live in the board's config["rules"], the same place the display rules live, so the wall's
clock and progress bars already know how to show them.

The dongle only transmits what it is told ("tx,<dst>,<payload>") and keeps sniffing, so the board
still sees every player-to-player message. Every frame we send is also fed back into the normal
ingest path with the host address as sender, so the state machine, the history and the wall treat
our own beacons exactly like a real host's.

"""
from __future__ import annotations

import time
from typing import Any, Optional

from .frames import Frame, BROADCAST, HOST_MAC
from .protocol import FFA_TEAM, WINNER_DRAW, WINNER_PLAYER_BASE

DEFAULT_SETTINGS = {"mode": 0, "lighting": 0, "time": 0, "lives": 1}
SETTING_VALUES = {"mode": (0, 1), "lighting": (0, 1), "time": (0, 1), "lives": (0, 1)}
HOUSE_RULES = ("time_limit_s", "team_kill_target")       # ints, None = off; Team Battle only
BEACON_GAP_S = 0.4            # the stock host sends every broadcast twice, this far apart
END_AFTER_ELIMINATION_S = 5.0 # the stock host declared the win ~7 s after the last death
DEFAULT_TIME_LIMIT_S = 600.0
HOST_GUN_INDEX = {0: 15, 1: 14}   # the index we claim as "the host's gun", per mode: gun ID 16 (Team Battle), 15 (Royale)
MODE_LABELS = {0: "Team Battle", 1: "Battle Royale"}


def player_mac(n: int) -> str:
    return f"00:00:00:00:00:{n:02x}"


class HostController:
    def __init__(self, hub):
        self.hub = hub
        self.enabled = False
        self.settings = dict(DEFAULT_SETTINGS)
        self.rate = 0
        self.started_at: Optional[float] = None
        self.end_due: Optional[float] = None
        self.pending: list[tuple[float, str, str]] = []     # (due, dst, payload)
        self.awaiting_rejoin = False                        # a mode change: guns must be switched on again
        self._resync_wanted = False                         # the dongle came back: host mode must be re-taken
        self._last_resync = 0.0
        self.tx_count = 0
        self.last_tx: Optional[dict] = None
        self.last_error: Optional[str] = None
        saved = {}
        try:
            saved = hub.store.get_setting("host") or {}
        except Exception:
            saved = {}
        for k, allowed in SETTING_VALUES.items():
            v = saved.get("settings", {}).get(k)
            if v in allowed:
                self.settings[k] = v
        if saved.get("rate") in range(0, 64):
            self.rate = saved["rate"]
        self._resume = bool(saved.get("enabled"))

    # ------------------------------------------------------------------ house rules
    def house_rules(self) -> dict[str, Optional[int]]:
        rules = (self.hub.state.config or {}).get("rules") or {}
        out = {}
        for k in HOUSE_RULES:
            v = rules.get(k)
            out[k] = int(v) if isinstance(v, (int, float)) and v > 0 else None
        return out

    def _set_rule(self, k: str, v: Optional[int]) -> None:
        cfg = self.hub.state.config
        cfg.setdefault("rules", {})[k] = v
        persist = getattr(self.hub, "persist_config", None)
        if persist is not None:
            try:
                persist()
            except Exception:
                pass

    def _royale_hidden(self) -> bool:
        return bool((self.hub.state.config or {}).get("hide_royale"))

    def _five_lives_hidden(self) -> bool:
        return bool((self.hub.state.config or {}).get("hide_five_lives"))

    def _host_index(self) -> int:
        return HOST_GUN_INDEX.get(self.settings.get("mode"), 15)

    def _time_limit(self) -> Optional[float]:
        """When this board ends a Team Battle: the house length, else the guns' 10 minutes, else never."""
        if self.settings.get("mode") != 0:
            return None
        custom = self.house_rules()["time_limit_s"]
        if custom:
            return float(custom)
        if self.settings.get("time") == 1:
            return float((self.hub.state.config or {}).get("time_limit_s") or DEFAULT_TIME_LIMIT_S)
        return None

    # ------------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        g = self.hub.state.game
        return {
            "enabled": self.enabled, "settings": dict(self.settings), "rate": self.rate, "rules": self.house_rules(),
            "host_index": self._host_index(), "awaiting_rejoin": self.awaiting_rejoin,
            "phase": g.phase, "can_start": self.enabled and g.phase != "live",
            "can_end": self.enabled and g.phase == "live", "started_at": self.started_at,
            "end_due": self.end_due, "tx_count": self.tx_count, "last_tx": self.last_tx,
            "last_error": self.last_error, "pending": len(self.pending),
            "available": self.hub.reader is not None and getattr(self.hub.reader, "connected", False),
        }

    def _save(self) -> None:
        try:
            self.hub.store.set_setting("host", {"enabled": self.enabled, "settings": dict(self.settings), "rate": self.rate})
        except Exception:
            pass

    # ------------------------------------------------------------------ dongle link
    def _command(self, line: str) -> bool:
        r = self.hub.reader
        if r is None:
            self.last_error = "no dongle"
            return False
        ok = bool(r.write(line))
        if not ok:
            self.last_error = "dongle not connected"
        return ok

    def on_dongle_connected(self) -> None:
        """The dongle (re)appeared. Opening the port resets it, and anything written while it boots
        is lost, so the host-mode commands wait for its boot banner (with a timer as a backstop)."""
        if self._resume and not self.enabled:
            self.enabled = True              # a backend restart mid-session keeps hosting quietly
        self._resume = False
        if self.enabled:
            self._resync_wanted = True
            self._later(3.0, self._resync_dongle)

    def on_dongle_message(self, msg) -> None:
        """Status lines from the firmware: the boot banner is the cue to (re)take host mode, and a
        status that says the dongle is not the host while we are means the command was lost."""
        if not self.enabled:
            return
        kind, data = getattr(msg, "kind", ""), getattr(msg, "data", {}) or {}
        if kind in ("boot", "legacy_boot"):
            self._resync_wanted = True
            self._later(0.8, self._resync_dongle)
        elif kind == "status" and data.get("host") is False:
            self._resync_dongle(force=True)
        elif kind == "error" and "host,1" in str(data.get("msg", "")):
            self._resync_dongle(force=True)
        elif kind == "host" and data.get("host") is True:
            self._resync_wanted = False

    def _later(self, delay: float, fn) -> None:
        loop = getattr(self.hub, "loop", None)
        if loop is None:
            fn()
            return
        try:
            loop.call_later(delay, fn)
        except Exception:
            fn()

    def _resync_dongle(self, force: bool = False) -> None:
        """Put the dongle in host mode and tell the guns where we stand: host-up and the current
        lobby (a game in progress is left alone). Throttled, since several cues can fire together."""
        now = time.time()
        if not self.enabled:
            return
        if not force and not self._resync_wanted:
            return
        if now - self._last_resync < 5.0:
            return
        self._last_resync = now
        self._resync_wanted = False
        self._command(f"rate,{self.rate}")
        self._command("host,1")
        if self.hub.state.game.phase != "live":
            self._send(BROADCAST, "36,90,1,1,42", 0.3, now)
            self._send(BROADCAST, "36,90,1,1,42", 0.3 + BEACON_GAP_S, now)
            self._announce_lobby(now, delay=1.3)

    # ------------------------------------------------------------------ actions
    def enable(self, on: bool) -> dict[str, Any]:
        now = time.time()
        if on and not self.enabled:
            self.enabled = True
            self.awaiting_rejoin = False
            if self._royale_hidden() and self.settings["mode"] == 1:
                self.settings["mode"] = 0            # Battle Royale is hidden: host Team Battle
            if self._five_lives_hidden() and self.settings["mode"] == 0 and self.settings["lives"] == 0:
                self.settings["lives"] = 1           # five lives is hidden: unlimited
            self._command(f"rate,{self.rate}")
            self._command("host,1")
            self._send(BROADCAST, "36,90,1,1,42", 0.0, now)
            self._send(BROADCAST, "36,90,1,1,42", BEACON_GAP_S, now)
            self._announce_lobby(now, delay=1.0)
        elif not on and self.enabled:
            self.enabled = False
            self.end_due = None
            self.pending = []
            self._command("host,0")
        self._resume = False
        self._save()
        return self.status()

    def set_settings(self, **kw) -> dict[str, Any]:
        changed = False
        try:
            new_mode = int(kw["mode"]) if "mode" in kw else None
        except (TypeError, ValueError):
            new_mode = None
        mode_changed = new_mode in (0, 1) and new_mode != self.settings["mode"]
        for k, v in kw.items():
            if k in HOUSE_RULES:
                try:
                    v = int(v) if v not in (None, "", False) else 0
                except (TypeError, ValueError):
                    continue
                v = v if v > 0 else None
                if self.house_rules()[k] != v:
                    self._set_rule(k, v)
                    changed = True
                if k == "time_limit_s" and v and self.settings["time"] == 1:
                    self.settings["time"] = 0            # our own length replaces the guns' timer on the air
                    changed = True
                continue
            if k not in SETTING_VALUES:
                continue
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
            if k == "mode" and v == 1 and self._royale_hidden():
                continue                             # Battle Royale is hidden from this board's hosting
            if k == "lives" and v == 0 and self._five_lives_hidden() and self.settings["mode"] == 0:
                continue                             # five lives is hidden: Team Battle is unlimited lives
            if v in SETTING_VALUES[k] and self.settings[k] != v:
                self.settings[k] = v
                changed = True
                if k == "time" and v == 1 and self.house_rules()["time_limit_s"]:
                    self._set_rule("time_limit_s", None)  # the guns' timer is on: no house length
        self._save()
        if changed and self.enabled and self.hub.state.game.phase != "live":
            if mode_changed:
                self._rehost(time.time())
            else:
                self._announce_lobby(time.time())
        return self.status()

    def _rehost(self, now: float) -> None:
        """A mode change is a fresh start for the guns: announce ourselves again under the other
        index, send the new lobby, and have the board drop everyone until they join again."""
        self.awaiting_rejoin = True
        self._send(BROADCAST, "36,90,1,1,42", 0.0, now)
        self._send(BROADCAST, "36,90,1,1,42", BEACON_GAP_S, now)
        self._announce_lobby(now, delay=1.0)

    def set_rate(self, rate: int) -> dict[str, Any]:
        if rate in range(0, 64):
            self.rate = rate
            self._save()
            self._command(f"rate,{rate}")
        return self.status()

    def start(self) -> dict[str, Any]:
        now = time.time()
        if not self.enabled:
            self.last_error = "not hosting"
            return self.status()
        if self.hub.state.game.phase == "live":
            self.last_error = "a game is already running"
            return self.status()
        self.started_at = now
        self.end_due = None
        self.awaiting_rejoin = False
        self._send(BROADCAST, self._beacon(1), 0.0, now)
        self._send(BROADCAST, self._beacon(1), BEACON_GAP_S, now)
        return self.status()

    def end(self, reason: str = "manual", winner: Optional[int] = None) -> dict[str, Any]:
        now = time.time()
        if not self.enabled:
            self.last_error = "not hosting"
            return self.status()
        if self.hub.state.game.phase != "live":
            self.last_error = "no game is running"
            return self.status()
        w = self._winner_code() if winner is None else winner
        self.end_due = None
        self.started_at = None
        self._send(BROADCAST, f"36,69,1,{w},1,42", 0.0, now)
        self._send(BROADCAST, f"36,69,1,{w},1,42", BEACON_GAP_S, now)
        return self.status()

    # ------------------------------------------------------------------ reactions
    def on_frame(self, frame: Frame, events: list[dict]) -> None:
        """Called after the reducer has applied an inbound frame."""
        if not self.enabled or frame.src == HOST_MAC or frame.source in ("host", "manual"):
            return
        ev = self.hub.protocol.decode(frame)
        now = frame.ts
        if ev.kind == "report_in" and ev.src_player and frame.dst in (HOST_MAC, BROADCAST, None):
            self.awaiting_rejoin = False
            self._send(player_mac(ev.src_player), self._ack(), 0.0, now)
        elif ev.kind == "state_report" and ev.src_player:
            self._send(player_mac(ev.src_player), f"36,102,{ev.src_player - 1},{self._host_index()},0,1,42", 0.0, now)
        if ev.kind in ("death", "state_report", "report_in"):
            self._check_kill_target()
            self._check_elimination_end(now)

    def tick(self, now: float) -> None:
        self._flush(now)
        if not self.enabled:
            return
        g = self.hub.state.game
        if g.phase == "live":
            if self.end_due is not None and now >= self.end_due:
                self.end("elimination")
                return
            limit = self._time_limit()               # house length, the guns' 10 minutes, or none
            started = self.started_at or g.started_at
            if limit and started and now >= started + limit:
                self.end("time")
        else:
            self.end_due = None

    # ------------------------------------------------------------------ internals
    def _beacon(self, phase: int) -> str:
        s = self.settings
        return f"36,65,1,{s['lighting']},0,{phase},{s['time']},{s['mode']},{s['lives']},1,42"

    def _ack(self) -> str:
        if self.hub.state.game.phase == "live":
            s = self.settings
            return f"36,97,{self._host_index()},1,{s['lighting']},0,{s['time']},{s['mode']},{s['lives']},1,42"
        return f"36,97,{self._host_index()},3,0,0,0,0,0,1,42"

    def _announce_lobby(self, now: float, delay: float = 0.0) -> None:
        self._send(BROADCAST, self._beacon(3), delay, now)
        self._send(BROADCAST, self._beacon(3), delay + BEACON_GAP_S, now)

    def _winner_code(self) -> int:
        st = self.hub.state
        wt, wp = st._infer_winner()
        if wp is not None:
            p = st.players.get(wp)
            team = p.effective_team if p else None
            if team is not None and team != FFA_TEAM:
                return team                      # the stock host names the colour when the winner has one
            return WINNER_PLAYER_BASE + (wp - 1)
        if wt is not None:
            return wt
        return WINNER_DRAW                       # nothing to separate them: the stock host sends 9

    def _check_kill_target(self) -> None:
        """House rule: the first side to the target wins on the spot (Team Battle only)."""
        target = self.house_rules()["team_kill_target"]
        st = self.hub.state
        if not target or self.settings.get("mode") != 0 or st.game.phase != "live":
            return
        players = [p for p in st.players.values() if p.in_game]
        sides = [(team, sum(p.kills for p in ps), ps) for team, ps in st._sides(players)]
        reached = [s for s in sides if s[1] >= target]
        if not reached:
            return
        best = max(s[1] for s in reached)
        winners = [s for s in reached if s[1] == best]
        if len(winners) != 1:
            self.end("target", winner=WINNER_DRAW)
            return
        team, _, ps = winners[0]
        self.end("target", winner=team if team is not None else WINNER_PLAYER_BASE + (ps[0].number - 1))

    def _check_elimination_end(self, now: float) -> None:
        st = self.hub.state
        g = st.game
        if g.phase != "live" or self.end_due is not None:
            return
        players = [p for p in st.players.values() if p.in_game]
        if not players:
            return
        if not any(p.alive for p in players):
            # the storm took everyone: the stock host called it a second after the last death
            self.end_due = now + 1.0
            return
        if len(players) < 2:
            return
        # sides: one per colour, and every lone wolf (free-for-all pick) on their own
        sides = st._sides(players)
        standing = [s for s in sides if any(p.alive for p in s[1])]
        last_side = len(sides) >= 2 and len(standing) <= 1
        if st.game_kind() == "royale":
            over = last_side
        else:
            over = self.settings.get("lives") == 0 and last_side
        if over:
            self.end_due = now + END_AFTER_ELIMINATION_S

    def _send(self, dst: str, payload: str, delay: float, now: float) -> None:
        self.pending.append((now + delay, dst, payload))
        if delay <= 0:
            self._flush(now)
        else:
            loop = getattr(self.hub, "loop", None)
            if loop is not None:
                try:
                    loop.call_later(delay + 0.02, self._flush, None)
                except Exception:
                    pass

    def _flush(self, now: Optional[float]) -> None:
        now = time.time() if now is None else now
        due = [p for p in self.pending if p[0] <= now + 1e-6]
        if not due:
            return
        self.pending = [p for p in self.pending if p[0] > now + 1e-6]
        for _, dst, payload in sorted(due):
            self._transmit(dst, payload, now)

    def _transmit(self, dst: str, payload: str, now: float) -> None:
        ok = self._command(f"tx,{'bcast' if dst == BROADCAST else dst},{payload}")
        self.tx_count += 1
        self.last_tx = {"ts": now, "dst": dst, "txt": payload, "ok": ok}
        # our own beacon goes through the same path as everything heard on the air
        self.hub.ingest_frame(Frame(ts=now, src=HOST_MAC, dst=dst, txt=payload, source="host"))
