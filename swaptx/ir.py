"""Evolver infrared tag protocol (from LaserTagMods JBOX.ino).

The gun's IR shot is what actually deals damage; it never appears on the radio.
This module exists so a dongle fitted with a TSOP4138-style IR receiver (see the
``IR_RX_PIN`` option in the firmware) can report tags as ``{"type":"ir","pulses":[...]}``
lines and the backend can decode them into station/shot events.

Frame (38 kHz carrier, LOW pulses, 500 us gaps): 2500 us sync, then 22 data bits
where a ~500 us pulse is 0 and a ~1000 us pulse is 1:

    B1..B4   bullet type (0-15)     P1..P4 player (0-15)
    T1..T2   team (0 red,1 blue,2 yellow,3 green)
    D1..D8   damage (0-255)         C1 critical
    Z1..Z3   trailer (Z3 must be a 1)   [X1 must be absent/short]

Bullet types seen in JBOX for Evolver: 0 normal shot, 10 respawn station, 11 upgrade
station, 12 own-the-zone, 13 medic / royale checkpoint, 14 capture-the-flag.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional, Sequence

BULLET_TYPES = {
    0: "Shot", 10: "Respawn station", 11: "Upgrade station", 12: "Own the zone",
    13: "Medic / checkpoint", 14: "Capture the flag", 15: "BRX respawn",
}

BIT_ORDER = (["B"] * 4) + (["P"] * 4) + (["T"] * 2) + (["D"] * 8) + ["C"] + (["Z"] * 3)


@dataclass
class IRTag:
    bullet_type: int
    player: int          # 0-based as encoded; player number = player + 1
    team: int
    damage: int
    critical: bool
    parity_ok: bool
    raw_bits: str

    @property
    def player_number(self) -> int:
        return self.player + 1

    @property
    def bullet_label(self) -> str:
        return BULLET_TYPES.get(self.bullet_type, f"Bullet type {self.bullet_type}")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["player_number"] = self.player_number
        d["bullet_label"] = self.bullet_label
        return d


def pulses_to_bits(pulses: Sequence[int], threshold: int = 750) -> list[int]:
    return [1 if p > threshold else 0 for p in pulses]


def decode_bits(bits: Sequence[int]) -> Optional[IRTag]:
    """Decode the 22 data bits (after the sync pulse). Returns None if malformed."""
    if len(bits) < 21:
        return None
    b = list(bits[:22]) + [0] * (22 - len(bits[:22]))
    bullet = (b[0] << 3) | (b[1] << 2) | (b[2] << 1) | b[3]
    player = (b[4] << 3) | (b[5] << 2) | (b[6] << 1) | b[7]
    team = (b[8] << 1) | b[9]
    damage = 0
    for i in range(8):
        damage = (damage << 1) | b[10 + i]
    critical = bool(b[18])
    # JBOX parity: even parity over B,P,T,D bits, carried in Z1 (best reading of
    # Evolverparitycheck(); treated as advisory only).
    ones = sum(b[:19])
    parity_ok = (ones % 2) == b[19]
    return IRTag(bullet, player, team, damage, critical, parity_ok,
                 "".join(str(x) for x in b))


def decode_pulses(pulses: Sequence[int]) -> Optional[IRTag]:
    """Decode a pulse list as captured by the firmware (sync pulse first)."""
    if not pulses:
        return None
    p = list(pulses)
    if p[0] > 2000:            # sync pulse present
        p = p[1:]
    return decode_bits(pulses_to_bits(p))


def encode_bits(bullet_type: int, player: int, team: int, damage: int, critical: bool = False) -> list[int]:
    """Inverse of decode_bits, useful for tests and for a future IR emitter."""
    bits = []
    bits += [(bullet_type >> i) & 1 for i in (3, 2, 1, 0)]
    bits += [(player >> i) & 1 for i in (3, 2, 1, 0)]
    bits += [(team >> i) & 1 for i in (1, 0)]
    bits += [(damage >> i) & 1 for i in range(7, -1, -1)]
    bits += [1 if critical else 0]
    parity = sum(bits) % 2
    bits += [parity, 0, 1]
    return bits
