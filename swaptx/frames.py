"""Raw frame model and parsers for the dongle's serial line formats.

Two line formats are understood:

1. Our sniffer firmware (firmware/swaptx_sniffer): one JSON object per line,
   ``{"type":"frame","ms":1234,"src":"00:00:00:00:00:03","dst":"ff:ff:ff:ff:ff:ff",
   "rssi":-61,"ch":1,"seq":412,"rate":11,"len":18,"txt":"36,101,2,1,3,42","hex":"..."}``
   plus status objects (``type`` = boot/status/lock/scan/error/ack).

2. The stock LaserTagMods transceiver / JBOX firmware debug print:
   ``Received message from: 00:00:00:00:00:03 - 36,101,2,1,3,42``
   (no destination, RSSI or sequence number available).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

BROADCAST = "ff:ff:ff:ff:ff:ff"
HOST_MAC = "00:00:00:00:00:ff"
PLAYER_MAC_PREFIX = "00:00:00:00:00:"
MAX_PLAYERS = 16

_LEGACY_RX = re.compile(r"Received message from:\s*([0-9a-fA-F:]{17})\s*-\s*(.*)$")
_MAC_RX = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def norm_mac(mac: str | None) -> str | None:
    if not mac:
        return None
    mac = mac.strip().lower()
    return mac if _MAC_RX.match(mac) else None


def mac_player(mac: str | None) -> Optional[int]:
    """Return the 1-based player number encoded in a spoofed SWAPTX MAC, else None.

    Players 1..16 use 00:00:00:00:00:01 .. 00:00:00:00:00:10. The host/bridge is
    ...:ff and is not a player.
    """
    mac = norm_mac(mac)
    if not mac or not mac.startswith(PLAYER_MAC_PREFIX):
        return None
    last = int(mac[-2:], 16)
    return last if 1 <= last <= MAX_PLAYERS else None


def mac_role(mac: str | None) -> str:
    mac = norm_mac(mac)
    if mac is None:
        return "unknown"
    if mac == BROADCAST:
        return "broadcast"
    if mac == HOST_MAC:
        return "host"
    if mac_player(mac):
        return "player"
    return "other"


@dataclass
class Frame:
    """One ESP-NOW payload heard on the air, plus radio metadata."""
    ts: float                      # wall clock (unix seconds) when the laptop received it
    src: Optional[str]             # sender MAC (lowercase) or None
    dst: Optional[str]             # destination MAC or None (legacy format)
    txt: str                       # payload as printable text
    hex: Optional[str] = None      # payload bytes as hex when available
    rssi: Optional[int] = None
    channel: Optional[int] = None
    seq: Optional[int] = None      # 802.11 sequence number (dedup of retransmits)
    rate: Optional[int] = None
    dongle_ms: Optional[int] = None
    source: str = "serial"         # serial | sim | import
    id: Optional[int] = None       # database id once persisted
    raw_line: str = ""

    @property
    def src_player(self) -> Optional[int]:
        return mac_player(self.src)

    @property
    def dst_player(self) -> Optional[int]:
        return mac_player(self.dst)

    @property
    def is_broadcast(self) -> bool:
        return self.dst is None or self.dst == BROADCAST

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["src_player"] = self.src_player
        d["dst_player"] = self.dst_player
        d["src_role"] = mac_role(self.src)
        d["dst_role"] = mac_role(self.dst) if self.dst else "broadcast"
        return d


@dataclass
class DongleMessage:
    """A non-frame status line from the firmware (boot banner, channel lock, ...)."""
    ts: float
    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    raw_line: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"ts": self.ts, "kind": self.kind, "data": self.data}


def _payload_from_hex(h: str) -> str:
    try:
        b = bytes.fromhex(h)
    except ValueError:
        return ""
    return "".join(chr(c) if 0x20 <= c < 0x7F else f"\\x{c:02x}" for c in b)


def parse_line(line: str, ts: float | None = None, source: str = "serial"):
    """Parse one serial line. Returns Frame, DongleMessage, or None (noise)."""
    ts = time.time() if ts is None else ts
    line = line.rstrip("\r\n")
    s = line.strip()
    if not s:
        return None

    if s.startswith("{") and s.endswith("}"):
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None
        kind = obj.get("type")
        if kind == "frame":
            hexs = obj.get("hex")
            txt = obj.get("txt")
            if txt is None and hexs:
                txt = _payload_from_hex(hexs)
            return Frame(
                ts=ts,
                src=norm_mac(obj.get("src")),
                dst=norm_mac(obj.get("dst")),
                txt=str(txt or ""),
                hex=hexs,
                rssi=_int(obj.get("rssi")),
                channel=_int(obj.get("ch")),
                seq=_int(obj.get("seq")),
                rate=_int(obj.get("rate")),
                dongle_ms=_int(obj.get("ms")),
                source=source,
                raw_line=line,
            )
        if kind:
            data = {k: v for k, v in obj.items() if k != "type"}
            return DongleMessage(ts=ts, kind=str(kind), data=data, raw_line=line)
        return None

    m = _LEGACY_RX.search(s)
    if m:
        return Frame(ts=ts, src=norm_mac(m.group(1)), dst=None, txt=m.group(2).strip(),
                     source=source, raw_line=line)

    # Stock firmware boot/status chatter worth surfacing as dongle messages.
    low = s.lower()
    if "esp-now init" in low or "starting espnow" in low or "board mac address" in low:
        return DongleMessage(ts=ts, kind="legacy_boot", data={"text": s}, raw_line=line)
    return None


def _int(v) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None
