"""SWAPTX / Evolver ESP-NOW protocol: opcode registry and frame -> event decoding.

Mapped live on 2026-09-13 with a promiscuous sniffer, one admin gun and two players
(``docs/mapping-2026-09-13.md``). Earlier guesses came from the LaserTagMods DIY host, whose
transceiver only forwarded three opcodes and only heard frames addressed to the host - so it
never saw the hit and death messages that players send each other.

Wire format (ASCII, comma separated, in a 32-byte text field followed by uninitialised memory):

    36,<opcode>,<args...>,42        Evolver headset/host traffic (opcodes are ASCII letters)
    CAPTURE,<jboxId>,<team>,<player> JBOX / JCUBE zone-base capture report (DIY gear only)
    $XX,...,*                        BRX / JEDGE tagger traffic (different gear line)

Addressing: the headset is the radio; its sender address is spoofed to its gun's ID
(00:00:00:00:00:01..10 = guns 1..16). The admin headset is the host, 00:00:00:00:00:ff.
ff:ff:ff:ff:ff:ff is the all-players broadcast. Unicasts are retried by the radio until
ACKed (same 802.11 sequence number); broadcasts are sent twice by the host and re-broadcast
once by every player's headset with the origin flag cleared.

Player indices on the wire are 0-based (gun ID minus 1).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Optional

from .frames import Frame, mac_player, mac_role, BROADCAST, HOST_MAC

# The Evolver's team pick is Red, Blue, Green or Free for All (the yellow one). Confirmed on the
# air: 0 red, 1 blue, 2 free-for-all, 3 green. The DIY host called 2 "yellow" (BRX has four teams).
TEAM_NAMES = {0: "Red", 1: "Blue", 2: "Free for all", 3: "Green"}
FFA_TEAM = 2
TEAM_COLORS = {0: "#ff3b3b", 1: "#2f8cff", 2: "#ffd21f", 3: "#3ddc6a"}
NO_TEAM = 4          # legacy DIY host: 36,69,99,4,42 = game over with no winning team
NO_PLAYER = 99       # "no player": the storm as a killer in 36,68 (Battle Royale); the DIY host's "no winner" in 36,69
WINNER_PLAYER_BASE = 10   # 36,69 winner field: 0..3 = team colour, 10 + player index = an individual
WINNER_DRAW = 9           # 36,69 winner field when nobody won: seen when the 10-minute timer ran out at 0-0

GAME_MODES = {0: "Team Battle", 1: "Battle Royale", 2: "Free for All"}   # 2 is never on the wire: FFA is the team pick (FFA_TEAM)
# "ffa" is not a host mode: it is the board layout when every player picked the free-for-all colour.
# Free-for-all players are lone wolves inside a Team Battle or a Battle Royale: they can be shot by
# anyone and can shoot anyone, including each other.
GAME_KINDS = {"team": "Team Battle", "royale": "Battle Royale"}   # the free-for-all pick is a side inside either
# The host's "Health/Lives" setting means two things: in Team Battle / Free for All it is lives
# (Low = 5, High = unlimited); in Battle Royale it is hit points (Low = 200, High = 500).
LIVES = {0: "Low / 5 lives", 1: "High / Unlimited lives"}
LIVES_ROYALE = {0: "Low health", 1: "High health"}
# Lighting: the gun's own words are "high" and "low"; the admin gun set to "high" sent 0. The DIY host
# called 0 "outdoor" and 1 "indoor" — a reading of what the setting is for, not something on the air.
LIGHTING = {0: "Outdoor", 1: "Indoor"}     # the guns' "high light" / "low light" setting
GAME_TIME = {0: "Off / Unlimited", 1: "On / 10 min (storm in Royale)"}
# A token the DIY LaserTagMods host exposes as "Respawn - Trigger / Base". The stock Evolver host
# gun has no such setting and the token never changed on the air; decoded for the protocol lab only.
RESPAWN = {0: "Auto (DIY host option)", 1: "Manual (DIY host option)"}
PHASES = {3: "lobby", 1: "in game", 0: "lobby"}
# Lives token in a report-in / state report.
LIVES_FRESH = 100        # headset just linked to its gun: no settings yet, team token meaningless
LIVES_UNLIMITED = 255    # unlimited-lives game (counts down anyway; deaths = 255 - lives)

# Semantic roles a token can be given. The state reducer only looks at roles,
# never at raw token indexes, so re-labelling is a pure config change.
ROLES = [
    "ignore", "player", "player_team", "victim", "victim_team", "lives_left", "kills", "deaths",
    "origin", "winner", "phase", "lighting", "respawn", "time", "mode", "lives",
    "score", "flag", "start", "win_player", "win_team", "kind", "objective_points",
    "jbox_id", "team", "unknown",
]


@dataclass
class FieldSpec:
    index: int                 # token index in the comma-split payload
    role: str                  # one of ROLES
    label: str                 # human label
    verified: bool = False     # True when confirmed on the air
    base: int = 0              # for player roles: 0 => token 0 == player 1
    note: str = ""


@dataclass
class OpcodeSpec:
    opcode: str
    name: str                  # machine name (event kind)
    label: str                 # human label
    fields: list[FieldSpec] = field(default_factory=list)
    verified: bool = False
    direction: str = ""        # host->all | player->host | victim->shooter | any
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "opcode": self.opcode, "name": self.name, "label": self.label,
            "verified": self.verified, "direction": self.direction, "note": self.note,
            "fields": [f.__dict__ for f in self.fields],
        }


def _settings_fields(offset: int, verified: bool) -> list[FieldSpec]:
    """The lighting/respawn/phase/time/mode/lives block of 36,65 and 36,97."""
    return [
        FieldSpec(offset + 0, "lighting", "Lighting (0 high-outdoor / 1 low-indoor)", verified),
        FieldSpec(offset + 1, "respawn", "Respawn token (DIY host option; always 0 on stock gear)", verified),
        FieldSpec(offset + 2, "phase", "Phase (3 lobby / 1 game started)", verified),
        FieldSpec(offset + 3, "time", "Time (0 off / 1 on: 10-minute timer, storm in Royale)", verified),
        FieldSpec(offset + 4, "mode", "Mode (0 Team Battle / 1 Battle Royale)", verified),
        FieldSpec(offset + 5, "lives", "Health/Lives (0 low: 5 lives, 200 HP in Royale / 1 high: unlimited, 500 HP)", verified),
        FieldSpec(offset + 6, "unknown", "Constant 1", False),
    ]


DEFAULT_OPCODES: dict[str, OpcodeSpec] = {
    "65": OpcodeSpec(
        "65", "game_control", "Lobby / start beacon (A)", verified=True, direction="host->all",
        note="Host broadcasts it twice; every player headset re-broadcasts it a few seconds later with "
             "the origin flag 0. Phase 3 = lobby opened / settings, phase 1 = game started. "
             "36,65,1,0,0,1,1,0,0,1,42 = start, lighting high, timer on, Team Battle, 5 lives.",
        fields=[FieldSpec(2, "origin", "Origin (1 host / 0 relayed copy)", True)] + _settings_fields(3, True),
    ),
    "69": OpcodeSpec(
        "69", "game_over", "Game over (E)", verified=True, direction="host->all",
        note="36,69,<origin>,<winner>,1,42. Winner 0..3 = winning team colour; 10 + player index = "
             "an individual winner (everyone on the free-for-all colour); 9 = a draw (the timer ran out "
             "with nothing to separate them). Sent exactly 10:00 after the start beacon when the timer is on. "
             "Relayed by players with origin 0.",
        fields=[
            FieldSpec(2, "origin", "Origin (1 host / 0 relayed copy)", True),
            FieldSpec(3, "winner", "Winner (0-3 team colour, 9 draw, 10+player index individual)", True),
            FieldSpec(4, "unknown", "Constant 1", False),
        ],
    ),
    "68": OpcodeSpec(
        "68", "death", "Death (D)", verified=True, direction="victim->shooter",
        note="Sent by the victim's headset to the shooter's headset when the victim dies. "
             "36,68,<killer>,<victim>,<victim team>,0,0,1,42. This is the elimination event. "
             "A storm death (Battle Royale) has killer 99 and goes to the host instead.",
        fields=[
            FieldSpec(2, "player", "Killer (0-based; 99 = the storm)", True, base=0),
            FieldSpec(3, "victim", "Victim (0-based)", True, base=0),
            FieldSpec(4, "victim_team", "Victim's team", True),
            FieldSpec(5, "unknown", "Constant 0", False),
            FieldSpec(6, "unknown", "Constant 0", False),
            FieldSpec(7, "unknown", "Constant 1", False),
        ],
    ),
    "75": OpcodeSpec(
        "75", "damage", "Damage taken (K)", verified=True, direction="victim->shooter",
        note="Sent by the victim's headset to the shooter's headset while taking damage: 3-5 per "
             "death regardless of where the hits land, so it does NOT count hits and carries no health.",
        fields=[
            FieldSpec(2, "player", "Shooter (0-based)", True, base=0),
            FieldSpec(3, "victim", "Victim (0-based)", True, base=0),
            FieldSpec(4, "unknown", "Constant 1", False),
        ],
    ),
    "79": OpcodeSpec(
        "79", "report_in", "Player online / state (O)", verified=True, direction="player->host",
        note="36,79,<player>,<lives>,<team>,<phase>,1,42. Sent every 4.3 s for ~3 minutes after a "
             "headset links to its gun (no host needed), and in a hosted game until the host acks it. "
             "Lives 100 = headset just linked (team token meaningless), 1 = idle, 5 = five-lives game, "
             "255 = unlimited, 1 in Royale (one life). Team 0 red, 1 blue, 2 free-for-all, 3 green.",
        fields=[
            FieldSpec(2, "player", "Player (0-based)", True, base=0),
            FieldSpec(3, "lives_left", "Lives (100 fresh link / 255 unlimited / count)", True),
            FieldSpec(4, "player_team", "Team pick (0 red / 1 blue / 2 free-for-all / 3 green)", True),
            FieldSpec(5, "phase", "Phase (3 idle / 1 in game)", True),
            FieldSpec(6, "unknown", "Constant 1", False),
        ],
    ),
    "82": OpcodeSpec(
        "82", "roster", "Roster / report relay (R)", verified=False, direction="any",
        note="Forwarded by the DIY transceiver; never heard from stock gear.",
        fields=[FieldSpec(2, "player", "Player (guess)", False, base=0),
                FieldSpec(3, "unknown", "Unknown", False)],
    ),
    "90": OpcodeSpec(
        "90", "host_up", "Host up (Z)", verified=True, direction="host->all",
        note="36,90,1,1,42 broadcast twice when a gun enters admin mode (its headset becomes the host).",
        fields=[FieldSpec(2, "unknown", "Constant 1", False), FieldSpec(3, "unknown", "Constant 1", False)],
    ),
    "97": OpcodeSpec(
        "97", "host_ack", "Host acknowledgement (a)", verified=True, direction="host->player",
        note="Unicast reply to a report-in: 36,97,<host gun>,<phase>,<lighting>,<respawn>,<time>,<mode>,<lives>,1,42. "
             "The first field is the index of the host's own gun: victims send hits and deaths meant for "
             "that gun to the host address instead of the gun's own. In the lobby (phase 3) the settings "
             "are placeholder zeros; in a game they are real. The DIY host sent 16/17 here.",
        fields=[FieldSpec(2, "host_player", "Host's own gun (0-based)", True, base=0),
                FieldSpec(3, "phase", "Phase (3 lobby / 1 in game)", True),
                FieldSpec(4, "lighting", "Lighting", True),
                FieldSpec(5, "respawn", "Respawn token", True),
                FieldSpec(6, "time", "Time", True),
                FieldSpec(7, "mode", "Mode", True),
                FieldSpec(8, "lives", "Health/Lives", True),
                FieldSpec(9, "unknown", "Constant 1", False)],
    ),
    "101": OpcodeSpec(
        "101", "state_report", "Player state report (e)", verified=True, direction="player->host",
        note="36,101,<player>,<team>,<lives>,<kills>,<c>,<d>,<e>,<f>,42. Sent to the host by the killer "
             "right after a kill and by every player at elimination / game over (d = 2 then). Lives count "
             "down from 255 in an unlimited game. The DIY host read the lives field as a score.",
        fields=[
            FieldSpec(2, "player", "Player (0-based)", True, base=0),
            FieldSpec(3, "player_team", "Team", True),
            FieldSpec(4, "lives_left", "Lives left (255 unlimited)", True),
            FieldSpec(5, "kills", "Kills this game", True),
            FieldSpec(6, "unknown", "c (unresolved; 1 seen once in an unlimited-lives game)", False),
            FieldSpec(7, "unknown", "d (1 while alive / 2 once out)", False),
            FieldSpec(8, "unknown", "e (0 while alive / 1 once out)", False),
            FieldSpec(9, "unknown", "Constant 1", False),
        ],
    ),
    "102": OpcodeSpec(
        "102", "report_ack", "State report acknowledgement (f)", verified=True, direction="host->player",
        note="36,102,<player>,<host gun>,0,1,42 from the host after a 36,101.",
        fields=[FieldSpec(2, "player", "Player (0-based)", True, base=0),
                FieldSpec(3, "host_player", "Host's own gun (0-based)", True, base=0), FieldSpec(4, "unknown", "Constant 0", False),
                FieldSpec(5, "unknown", "Constant 1", False)],
    ),
}

# Opcodes with a "36,10" prefix that we have not seen. Decoded generically so nothing is lost.
COMBAT_FAMILY_PREFIX = "10"


@dataclass
class Event:
    """A decoded protocol event (one per frame, plus a few per-frame extras)."""
    kind: str                       # game_control, death, report_in, ... unknown_36, capture, brx, text
    opcode: Optional[str]
    frame: Frame
    tokens: list[str]
    fields: dict[str, Any] = field(default_factory=dict)   # role -> value
    src_player: Optional[int] = None
    dst_player: Optional[int] = None
    src_role: str = "unknown"
    dst_role: str = "broadcast"
    well_formed: bool = True        # starts with 36 and ends with 42
    spec: Optional[OpcodeSpec] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "opcode": self.opcode, "tokens": self.tokens,
            "fields": self.fields, "src": self.frame.src, "dst": self.frame.dst,
            "src_player": self.src_player, "dst_player": self.dst_player,
            "src_role": self.src_role, "dst_role": self.dst_role,
            "well_formed": self.well_formed, "ts": self.frame.ts, "txt": self.frame.txt,
            "rssi": self.frame.rssi,
        }


class Protocol:
    """Opcode registry with user overrides layered on top of the defaults."""

    def __init__(self, overrides: dict[str, Any] | None = None):
        self.opcodes: dict[str, OpcodeSpec] = copy.deepcopy(DEFAULT_OPCODES)
        self.overrides: dict[str, Any] = {}
        if overrides:
            self.apply_overrides(overrides)

    # ---- configuration -------------------------------------------------
    def apply_overrides(self, overrides: dict[str, Any]) -> None:
        """overrides = {"101": {"name": ..., "label": ..., "fields": {"2": {"role": "victim", ...}}}}"""
        self.overrides = copy.deepcopy(overrides or {})
        self.opcodes = copy.deepcopy(DEFAULT_OPCODES)
        for op, ov in self.overrides.items():
            op = str(op)
            spec = self.opcodes.get(op)
            if spec is None:
                spec = OpcodeSpec(op, ov.get("name") or f"op_{op}", ov.get("label") or f"Opcode {op}")
                self.opcodes[op] = spec
            if ov.get("name"):
                spec.name = str(ov["name"])
            if ov.get("label"):
                spec.label = str(ov["label"])
            if ov.get("note") is not None:
                spec.note = str(ov["note"])
            for idx, fov in (ov.get("fields") or {}).items():
                idx = int(idx)
                fs = next((f for f in spec.fields if f.index == idx), None)
                if fs is None:
                    fs = FieldSpec(idx, "unknown", f"Token {idx}")
                    spec.fields.append(fs)
                    spec.fields.sort(key=lambda f: f.index)
                if fov.get("role") in ROLES:
                    fs.role = fov["role"]
                if fov.get("label"):
                    fs.label = str(fov["label"])
                if "base" in fov:
                    try:
                        fs.base = int(fov["base"])
                    except (TypeError, ValueError):
                        pass
                fs.verified = bool(fov.get("verified", fs.verified))
                if fov.get("note") is not None:
                    fs.note = str(fov["note"])

    def to_dict(self) -> dict[str, Any]:
        return {
            "roles": ROLES,
            "opcodes": {k: v.to_dict() for k, v in sorted(self.opcodes.items(), key=lambda kv: int(kv[0]))},
            "overrides": self.overrides,
            "teams": {str(k): {"name": v, "color": TEAM_COLORS[k]} for k, v in TEAM_NAMES.items()},
        }

    # ---- decoding --------------------------------------------------------
    def decode(self, frame: Frame) -> Event:
        txt = frame.txt
        # the sniffer prints the whole 32-byte text field; only the part before the first NUL is the message
        if "\x00" in txt:
            txt = txt.split("\x00", 1)[0]
        txt = txt.strip()
        tokens = [t.strip() for t in txt.split(",")] if txt else []
        ev = Event(kind="text", opcode=None, frame=frame, tokens=tokens,
                   src_player=mac_player(frame.src), dst_player=mac_player(frame.dst),
                   src_role=mac_role(frame.src),
                   dst_role=(mac_role(frame.dst) if frame.dst else "broadcast"))
        if not tokens:
            ev.kind = "empty"
            return ev
        head = tokens[0]
        if head == "36" and len(tokens) >= 2:
            self._decode_36(ev)
        elif head.upper() == "CAPTURE":
            self._decode_capture(ev)
        elif head.startswith("$"):
            ev.kind = "brx"
            ev.opcode = head
            ev.well_formed = tokens[-1].endswith("*")
            ev.fields = {"args": tokens[1:]}
        elif head.lower() == "arcade":
            ev.kind = "arcade_select"
            ev.opcode = "arcade"
            ev.fields = {"player": _to_int(tokens[1]) + 1 if len(tokens) > 1 and _to_int(tokens[1]) is not None else None}
        elif head.lower() == "update":
            ev.kind = "ota_update"
            ev.opcode = "update"
        else:
            ev.kind = "text"
        return ev

    def _decode_36(self, ev: Event) -> None:
        tokens = ev.tokens
        op = tokens[1]
        ev.opcode = op
        ev.well_formed = tokens[-1] == "42"
        spec = self.opcodes.get(op)
        if spec is None:
            ev.kind = "combat_unknown" if op.startswith(COMBAT_FAMILY_PREFIX) else "unknown_36"
            ev.fields = {"args": tokens[2:-1] if ev.well_formed else tokens[2:]}
            return
        ev.spec = spec
        ev.kind = spec.name
        fields: dict[str, Any] = {}
        last_data = len(tokens) - 1 if ev.well_formed else len(tokens)   # skip the 42 terminator
        for fs in spec.fields:
            if fs.index >= last_data:
                continue
            raw = tokens[fs.index]
            if fs.role in ("ignore",):
                continue
            val: Any = _to_int(raw)
            if val is None:
                val = raw
            if fs.role in ("player", "victim", "win_player", "host_player") and isinstance(val, int):
                if val == NO_PLAYER:
                    if fs.role == "player" and spec.name == "death":
                        fields["environment"] = "storm"      # 36,68,99,...: the storm took the last health
                    val = None
                else:
                    val = val - fs.base + 1          # normalise to 1-based player number
                    if not 1 <= val <= 16:
                        val = None
            if fs.role == "unknown":
                fields.setdefault("unknown", {})[str(fs.index)] = val
            else:
                fields[fs.role] = val
        ev.fields = fields

    def _decode_capture(self, ev: Event) -> None:
        # CAPTURE,<jboxId>,<team>,<player>  (JBOX SharePointsWithOthersLoRa / ESP-NOW node)
        ev.kind = "capture"
        ev.opcode = "CAPTURE"
        t = ev.tokens
        ev.fields = {
            "jbox_id": _to_int(t[1]) if len(t) > 1 else None,
            "team": _to_int(t[2]) if len(t) > 2 else None,
            "player": _to_int(t[3]) if len(t) > 3 else None,
        }


def _to_int(s: str) -> Optional[int]:
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def describe_settings(f: dict[str, Any]) -> dict[str, Any]:
    """Human readable game settings from decoded role fields."""
    out: dict[str, Any] = {}
    if isinstance(f.get("mode"), int):
        out["mode"] = f["mode"]
        out["mode_label"] = GAME_MODES.get(f["mode"], f"Mode {f['mode']}")
    if isinstance(f.get("lives"), int):
        out["lives"] = f["lives"]
        table = LIVES_ROYALE if f.get("mode") == 1 else LIVES
        out["lives_label"] = table.get(f["lives"], f"Lives {f['lives']}")
    if isinstance(f.get("time"), int):
        out["time"] = f["time"]
        out["time_label"] = GAME_TIME.get(f["time"], f"Time {f['time']}")
    if isinstance(f.get("respawn"), int):
        out["respawn"] = f["respawn"]
        out["respawn_label"] = RESPAWN.get(f["respawn"], f"Respawn {f['respawn']}")
    if isinstance(f.get("lighting"), int):
        out["lighting"] = f["lighting"]
        out["lighting_label"] = LIGHTING.get(f["lighting"], f"Lighting {f['lighting']}")
    return out


def settings_are_placeholders(f: dict[str, Any]) -> bool:
    """A lobby-phase ack carries all-zero settings that mean nothing."""
    return all(f.get(k) in (0, None) for k in ("lighting", "time", "mode", "lives"))


def decode_winner(w: Any) -> tuple[Optional[int], Optional[int]]:
    """36,69 winner field -> (team, player number). 0..3 = team colour, 10+i = player index i.
    9 (a draw) and anything else decode to (None, None); callers test WINNER_DRAW themselves."""
    if not isinstance(w, int):
        return None, None
    if 0 <= w <= 3:
        return w, None
    if WINNER_PLAYER_BASE <= w < WINNER_PLAYER_BASE + 16:
        return None, w - WINNER_PLAYER_BASE + 1
    return None, None


def team_name(t: Optional[int]) -> str:
    if t is None:
        return "No team"
    return TEAM_NAMES.get(t, "No team" if t == NO_TEAM else f"Team {t}")
