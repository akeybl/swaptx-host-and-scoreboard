"""Fake dongle: generates a realistic SWAPTX game over the same line format as the
sniffer firmware, so the UI and the reducer can be exercised without hardware.

The scenario follows the protocol as mapped on the air (docs/mapping-2026-09-13.md):
host up -> players online (idle report-ins + lobby acks) -> lobby beacon (relayed by every
player) -> start beacon (relayed; in-game report-ins + acks) -> damage and death messages from
each victim to its shooter, state reports from killers and eliminated players to the host with
acks -> game over (twice, then relayed) -> final reports -> pause -> next game.
"""
from __future__ import annotations

import json
import random
import threading
import time
from typing import Callable, Optional

from .frames import BROADCAST, HOST_MAC


def mac(n: int) -> str:
    return f"00:00:00:00:00:{n:02x}"


class Simulator(threading.Thread):
    def __init__(self, on_line: Callable[[str, float], None], on_status: Callable[[bool, Optional[str]], None],
                 players: int = 8, mode: int = 0, speed: float = 1.0, seed: int | None = None,
                 kill_interval: float = 9.0, game_length_s: float = 300.0, loop: bool = True):
        super().__init__(name="swaptx-sim", daemon=True)
        self.on_line = on_line
        self.on_status = on_status
        self.n_players = max(2, min(16, players))
        self.mode = mode
        self.cycle = mode == 3
        if self.cycle:
            self.mode = 0
        self.speed = max(0.05, speed)
        self.rng = random.Random(seed)
        self.kill_interval = kill_interval
        self.game_length_s = game_length_s
        self.loop = loop
        self._stop = threading.Event()
        self.seq = 0
        self.ms0 = time.time()
        self.connected = False
        self.port = "simulator"
        self.commands: list[str] = []

    def stop(self) -> None:
        self._stop.set()

    def write(self, line: str) -> bool:
        self.commands.append(line)
        if line.strip() == "status":
            self._status()
        return True

    # -- emit helpers --------------------------------------------------------
    def _sleep(self, s: float) -> bool:
        return not self._stop.wait(s / self.speed)

    def _frame(self, src: str, dst: str, txt: str, rssi: int | None = None, seq: int | None = None) -> None:
        self.seq += 1
        obj = {"type": "frame", "ms": int((time.time() - self.ms0) * 1000), "src": src, "dst": dst,
               "rssi": rssi if rssi is not None else self.rng.randint(-78, -40), "ch": 1,
               "seq": seq if seq is not None else self.seq, "rate": 25, "len": len(txt),
               "txt": txt, "hex": txt.encode().hex()}
        self.on_line(json.dumps(obj), time.time())

    def _status(self) -> None:
        self.on_line(json.dumps({"type": "status", "fw": "swaptx-sniffer sim", "ch": 1, "mode": "sniff",
                                 "frames": self.seq, "uptime_s": int(time.time() - self.ms0), "heap": 200000}), time.time())

    def _boot(self) -> None:
        self.on_line(json.dumps({"type": "boot", "fw": "swaptx-sniffer sim", "mac": "24:6f:28:aa:bb:cc",
                                 "mode": "sniff", "ch": 1}), time.time())

    # -- scenario --------------------------------------------------------------
    def run(self) -> None:
        self.connected = True
        self.on_status(True, self.port)
        self._boot()
        while not self._stop.is_set():
            self.run_game()
            if self.cycle:
                self.mode = (self.mode + 1) % 3
            if not self.loop:
                break
            if not self._sleep(20):
                break
        self.connected = False
        self.on_status(False, self.port)

    def run_game(self) -> None:
        rng = self.rng
        players = list(range(1, self.n_players + 1))
        squads = False
        if self.mode == 0:
            real = [0, 1, 3]                                          # red, blue, green
            n_teams = 2 if self.n_players <= 8 else 3
            teams = {p: real[i % n_teams] for i, p in enumerate(players)}
        elif self.mode == 2:
            teams = {p: 2 for p in players}                           # everyone on the free-for-all pick
        else:
            squads = self.n_players >= 4 and rng.random() < 0.5     # royale in colours, or everyone free-for-all
            teams = {p: ([0, 1, 3][i % 2] if squads else 2) for i, p in enumerate(players)}
        host_mode = 0 if self.mode == 2 else self.mode                # FFA is a team pick, not a host mode
        lives = rng.choice([0, 1])
        time_flag = rng.choice([0, 1])
        lighting = rng.choice([0, 1])
        lives_tok = 1 if host_mode == 1 else (5 if lives == 0 else 255)

        def beacon(origin: int, phase: int) -> str:
            return f"36,65,{origin},{lighting},0,{phase},{time_flag},{host_mode},{lives},1,42"

        ack_lobby = "36,97,1,3,0,0,0,0,0,1,42"
        ack_game = f"36,97,1,1,{lighting},0,{time_flag},{host_mode},{lives},1,42"

        # host up: a gun entered admin mode
        self._frame(HOST_MAC, BROADCAST, "36,90,1,1,42")
        self._frame(HOST_MAC, BROADCAST, "36,90,1,1,42")
        if not self._sleep(2):
            return
        for p in players:
            self._frame(mac(p), HOST_MAC, f"36,79,{p-1},1,{teams[p]},3,1,42")
            self._frame(HOST_MAC, mac(p), ack_lobby)
            if not self._sleep(rng.uniform(0.5, 2.0)):
                return
        # lobby beacon (settings), relayed by every player
        for _ in range(2):
            self._frame(HOST_MAC, BROADCAST, beacon(1, 3))
        if not self._sleep(3):
            return
        for p in players:
            self._frame(mac(p), BROADCAST, beacon(0, 3))
        if not self._sleep(4):
            return
        # start
        for _ in range(2):
            self._frame(HOST_MAC, BROADCAST, beacon(1, 1))
            if not self._sleep(0.3):
                return
        if not self._sleep(3):
            return
        for p in players:
            self._frame(mac(p), BROADCAST, beacon(0, 1))
            self._frame(mac(p), HOST_MAC, f"36,79,{p-1},{lives_tok},{teams[p]},1,1,42")
            self._frame(HOST_MAC, mac(p), ack_game)
        t0 = time.time()
        kills = {p: 0 for p in players}
        left = {p: lives_tok for p in players}
        alive = set(players)
        late_joined = False

        def teams_alive() -> set[int]:
            return {teams[p] for p in alive}

        while self._stop.is_set() is False:
            elapsed = (time.time() - t0) * self.speed
            if elapsed > self.game_length_s:
                break
            if host_mode == 1 and len(teams_alive() if squads else alive) <= 1:
                break
            if host_mode == 0 and lives == 0 and len(teams_alive()) <= 1:
                break
            if not self._sleep(rng.expovariate(1 / self.kill_interval)):
                return
            cands = [p for p in alive]
            if len(cands) < 2:
                break
            killer = rng.choice(cands)
            victims = [v for v in cands if v != killer and (teams[v] != teams[killer] or teams[killer] == 2)]   # no team kills
            if not victims:
                continue
            victim = rng.choice(victims)
            # damage messages: victim's headset tells the shooter, roughly one per quarter of health
            for _ in range(rng.randint(3, 5)):
                self._frame(mac(victim), mac(killer), f"36,75,{killer-1},{victim-1},1,42")
                if not self._sleep(rng.uniform(0.3, 1.2)):
                    return
            kills[killer] += 1
            left[victim] -= 1
            death = f"36,68,{killer-1},{victim-1},{teams[victim]},0,0,1,42"
            self._frame(mac(victim), mac(killer), death)
            if rng.random() < 0.3:                                    # radio retry: same sequence number
                self._frame(mac(victim), mac(killer), death, seq=self.seq)
            # the killer reports to the host
            self._frame(mac(killer), HOST_MAC, f"36,101,{killer-1},{teams[killer]},{left[killer]},{kills[killer]},0,1,0,1,42")
            self._frame(HOST_MAC, mac(killer), f"36,102,{killer-1},1,0,1,42")
            if left[victim] <= 0:
                alive.discard(victim)
                self._frame(mac(victim), HOST_MAC, f"36,101,{victim-1},{teams[victim]},0,{kills[victim]},0,2,1,1,42")
                self._frame(HOST_MAC, mac(victim), f"36,102,{victim-1},1,0,1,42")
            # a late joiner once per game
            if not late_joined and elapsed > 60 and self.n_players < 16:
                late_joined = True
                p = self.n_players + 1
                players.append(p)
                teams[p] = min(set(teams.values()), key=lambda t: list(teams.values()).count(t))
                kills[p] = 0
                left[p] = lives_tok
                alive.add(p)
                self._frame(mac(p), HOST_MAC, f"36,79,{p-1},{lives_tok},{teams[p]},1,1,42")
                self._frame(HOST_MAC, mac(p), ack_game)
        # game over
        if host_mode == 0 or squads:
            tk: dict[int, int] = {}
            for p, k in kills.items():
                tk[teams[p]] = tk.get(teams[p], 0) + k
            if host_mode == 0 and lives == 0 and len(teams_alive()) == 1:
                winner = next(iter(teams_alive()))
            elif squads and len(teams_alive()) == 1:
                winner = next(iter(teams_alive()))
            else:
                winner = max(tk, key=tk.get)
        else:
            wp = next(iter(alive)) if len(alive) == 1 else max(kills, key=kills.get)
            winner = 10 + (wp - 1) if teams[wp] == 2 else teams[wp]
        for _ in range(2):
            self._frame(HOST_MAC, BROADCAST, f"36,69,1,{winner},1,42")
            if not self._sleep(0.3):
                return
        if not self._sleep(3):
            return
        for p in players:
            self._frame(mac(p), BROADCAST, f"36,69,0,{winner},1,42")
        for p in players:
            if p not in alive or host_mode == 0:
                self._frame(mac(p), HOST_MAC, f"36,101,{p-1},{teams[p]},{max(left[p], 0)},{kills[p]},0,2,1,1,42")
                self._frame(HOST_MAC, mac(p), f"36,102,{p-1},1,0,1,42")
