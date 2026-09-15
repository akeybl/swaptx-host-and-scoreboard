"""Host mode: the dongle as the admin headset. Every message it sends was seen from the stock host."""
import time

from swaptx.frames import parse_line, Frame, HOST_MAC, BROADCAST
from swaptx.host import HostController
from swaptx.protocol import Protocol
from swaptx.state import GameState
from conftest import line, mac, H, B


class FakeReader:
    connected = True
    port = "fake"

    def __init__(self):
        self.lines = []

    def write(self, ln):
        self.lines.append(ln)
        return True


class FakeStore:
    def __init__(self):
        self.settings = {}

    def get_setting(self, k):
        return self.settings.get(k)

    def set_setting(self, k, v):
        self.settings[k] = v


class FakeHub:
    def __init__(self):
        self.protocol = Protocol()
        self.state = GameState(self.protocol, names={1: "Alex", 2: "Sam", 4: "Maya"})
        self.reader = FakeReader()
        self.store = FakeStore()
        self.loop = None
        self.frames = []
        self.t = 1_700_000_000.0
        self.seq = 0

    def ingest_frame(self, frame):
        self.frames.append(frame)
        return self.state.apply(frame)

    # a player's headset says something
    def hear(self, src, dst, txt, dt=1.0):
        self.t += dt
        self.seq += 1
        fr = parse_line(line(src, dst, txt, seq=self.seq), ts=self.t)
        evs = self.ingest_frame(fr)
        self.host.on_frame(fr, evs)
        return evs

    def tx(self):
        return [ln for ln in self.reader.lines if ln.startswith("tx,")]


def make(hide_royale=False, hide_five_lives=False):
    hub = FakeHub()
    hub.state.config["hide_royale"] = hide_royale          # the tests exercise both modes
    hub.state.config["hide_five_lives"] = hide_five_lives
    hub.host = HostController(hub)
    return hub, hub.host


def test_enable_announces_host_and_lobby_and_acks_players():
    hub, host = make()
    host.enable(True)
    assert hub.reader.lines[:2] == ["rate,0", "host,1"]
    assert hub.tx()[:1] == ["tx,bcast,36,90,1,1,42"]
    host.tick(time.time() + 2)                      # the second copy and the lobby beacon are due
    tx = hub.tx()
    assert tx.count("tx,bcast,36,90,1,1,42") == 2 and tx.count("tx,bcast,36,65,1,0,0,3,0,0,1,1,42") == 2
    assert hub.state.game.phase == "lobby"          # our own beacons drive the board
    evs = hub.hear(mac(1), H, "36,79,0,1,0,3,1,42")
    assert hub.tx()[-1] == f"tx,{mac(1)},36,97,15,3,0,0,0,0,0,1,42"   # 15: the host gun index we claim
    assert hub.state.players[1].team == 0 and hub.state.players[1].in_game


def test_settings_change_re_announces_and_start_sends_the_start_beacon_twice():
    hub, host = make()
    host.enable(True)
    host.tick(time.time() + 2)
    n = len(hub.tx())
    host.set_settings(mode=1, lives=0, time=1, lighting=1)
    host.tick(time.time() + 2)
    # a mode change is a fresh start: host-up again, then the new lobby
    assert hub.tx()[n:] == ["tx,bcast,36,90,1,1,42"] * 2 + ["tx,bcast,36,65,1,1,0,3,1,1,0,1,42"] * 2
    assert host.settings == {"mode": 1, "lighting": 1, "time": 1, "lives": 0}
    hub.hear(mac(1), H, "36,79,0,1,2,3,1,42")
    hub.hear(mac(2), H, "36,79,1,1,2,3,1,42")
    host.start()
    host.tick(time.time() + 2)
    assert hub.tx()[-2:] == ["tx,bcast,36,65,1,1,0,1,1,1,0,1,42"] * 2
    assert hub.state.game.phase == "live" and hub.state.game_kind() == "royale"
    hub.hear(mac(1), H, "36,79,0,1,2,1,1,42")      # in-game report-in gets the real settings back
    assert hub.tx()[-1] == f"tx,{mac(1)},36,97,14,1,1,0,1,1,0,1,42"   # 14: the host index for Battle Royale
    assert host.status()["can_end"] and not host.status()["can_start"]


def test_state_reports_are_acked_and_a_last_team_standing_ends_the_game():
    hub, host = make()
    host.enable(True)
    host.set_settings(lives=0)                       # five lives
    host.tick(time.time() + 2)
    hub.hear(mac(1), H, "36,79,0,1,0,3,1,42")
    hub.hear(mac(2), H, "36,79,1,1,1,3,1,42")
    host.start()
    host.tick(time.time() + 2)
    for _ in range(5):
        hub.hear(mac(2), mac(1), "36,68,0,1,1,0,0,1,42", 5)
    hub.hear(mac(1), H, "36,101,0,0,5,5,0,1,0,1,42", 1)
    assert hub.tx()[-1] == f"tx,{mac(1)},36,102,0,15,0,1,42"
    assert hub.state.players[2].eliminated and host.end_due is not None
    host.tick(hub.t + 1)                             # not yet: the stock host waits a few seconds
    assert hub.state.game.phase == "live"
    host.tick(hub.t + 6)
    assert hub.tx()[-1] == "tx,bcast,36,69,1,0,1,42" and hub.state.game.phase == "ended"
    assert hub.state.game.winner_team == 0 and host.status()["can_start"]


def test_royale_winner_is_named_by_colour_or_by_index():
    hub, host = make()
    host.enable(True)
    host.set_settings(mode=1, lives=0)
    host.tick(time.time() + 2)
    for p, team in ((1, 2), (2, 2), (4, 2)):
        hub.hear(mac(p), H, f"36,79,{p-1},1,{team},3,1,42")
    host.start()
    host.tick(time.time() + 2)
    hub.hear(mac(2), mac(1), "36,68,0,1,2,0,0,1,42", 3)
    hub.hear(mac(1), mac(4), "36,68,3,0,2,0,0,1,42", 3)
    host.tick(hub.t + 6)
    assert hub.tx()[-1] == "tx,bcast,36,69,1,13,1,42"          # 10 + index 3 = Maya
    assert hub.state.game.winner_player == 4
    # two players on real colours: the colour is announced
    hub2, host2 = make()
    host2.enable(True)
    host2.set_settings(mode=1, lives=0)
    host2.tick(time.time() + 2)
    hub2.hear(mac(1), H, "36,79,0,1,0,3,1,42")
    hub2.hear(mac(2), H, "36,79,1,1,1,3,1,42")
    host2.start()
    host2.tick(time.time() + 2)
    hub2.hear(mac(2), mac(1), "36,68,0,1,1,0,0,1,42", 3)
    host2.tick(hub2.t + 6)
    assert hub2.tx()[-1] == "tx,bcast,36,69,1,0,1,42" and hub2.state.game.winner_player == 1


def test_timer_ends_a_timed_game_and_manual_end_works():
    hub, host = make()
    hub.state.config["time_limit_s"] = 100
    host.enable(True)
    host.set_settings(time=1)
    host.tick(time.time() + 2)
    hub.hear(mac(1), H, "36,79,0,1,0,3,1,42")
    hub.hear(mac(2), H, "36,79,1,1,1,3,1,42")
    host.start()
    t0 = host.started_at
    host.tick(t0 + 1)
    hub.hear(mac(2), mac(1), "36,68,0,1,1,0,0,1,42", 3)
    host.tick(t0 + 50)
    assert hub.state.game.phase == "live"
    host.tick(t0 + 101)
    assert hub.tx()[-1] == "tx,bcast,36,69,1,0,1,42" and hub.state.game.phase == "ended"
    host.start()
    host.tick(time.time() + 2)
    assert hub.state.game.phase == "live"
    host.end()
    assert hub.state.game.phase == "ended"
    host.enable(False)
    assert hub.reader.lines[-1] == "host,0" and not host.status()["enabled"]


def test_the_storm_not_the_timer_ends_a_battle_royale():
    hub, host = make()
    hub.t = time.time()                                      # the host's own clock is real time
    hub.state.config["time_limit_s"] = 100
    host.enable(True)
    host.set_settings(mode=1, time=1, lives=1)               # royale, storm on, 500 HP
    host.tick(time.time() + 2)
    hub.hear(mac(1), H, "36,79,0,1,0,3,1,42")
    hub.hear(mac(4), H, "36,79,3,1,1,3,1,42")
    host.start()
    t0 = host.started_at
    host.tick(t0 + 1)                                        # the second copy of the start beacon goes out
    host.tick(t0 + 101)
    assert hub.state.game.phase == "live"                    # no 10-minute end in royale
    hub.hear(mac(4), H, "36,68,99,3,1,0,0,1,42", 200)        # the storm takes gun 4
    assert hub.state.players[4].eliminated and hub.state.game.total_kills == 0
    assert host.end_due is not None
    host.tick(hub.t + 6)
    assert hub.tx()[-1] == "tx,bcast,36,69,1,0,1,42" and hub.state.game.winner_player == 1


def test_disabled_host_never_transmits_and_our_own_frames_are_ignored():
    hub, host = make()
    hub.hear(mac(1), H, "36,79,0,1,0,3,1,42")
    assert hub.tx() == []
    host.enable(True)
    n = len(hub.tx())
    hub.host.on_frame(Frame(ts=time.time(), src=HOST_MAC, dst=BROADCAST, txt="36,90,1,1,42", source="host"), [])
    assert len(hub.tx()) == n
    assert hub.store.settings["host"]["enabled"] is True


def test_a_lone_wolf_counts_as_a_side_for_the_automatic_end():
    hub, host = make()
    host.enable(True)
    host.set_settings(lives=0)
    host.tick(time.time() + 2)
    hub.hear(mac(1), H, "36,79,0,1,0,3,1,42")        # red
    hub.hear(mac(2), H, "36,79,1,1,2,3,1,42")        # lone wolf
    hub.hear(mac(4), H, "36,79,3,1,2,3,1,42")        # lone wolf
    host.start()
    host.tick(time.time() + 2)
    for _ in range(5):
        hub.hear(mac(2), mac(1), "36,68,0,1,2,0,0,1,42", 5)
    assert host.end_due is None                     # a wolf is still standing
    for _ in range(5):
        hub.hear(mac(4), mac(1), "36,68,0,3,2,0,0,1,42", 5)
    assert host.end_due is not None
    host.tick(hub.t + 6)
    assert hub.tx()[-1] == "tx,bcast,36,69,1,0,1,42" and hub.state.game.winner_team == 0


def test_a_game_where_the_storm_takes_everyone_ends_a_second_after_the_last_death():
    hub, host = make()
    hub.t = time.time()
    host.enable(True)
    host.set_settings(mode=1, time=1, lives=1)
    host.tick(time.time() + 2)
    hub.hear(mac(1), H, "36,79,0,1,1,3,1,42")                # one player, blue
    host.start()
    host.tick(host.started_at + 1)
    hub.hear(mac(1), H, "36,68,99,0,1,0,0,1,42", 369)        # the storm takes the only player
    assert host.end_due is not None and host.end_due - hub.t <= 1.0
    host.tick(hub.t + 1.5)
    assert hub.state.game.phase == "ended" and hub.tx()[-1] == "tx,bcast,36,69,1,1,1,42"


def test_the_timer_running_out_with_nothing_to_separate_them_sends_a_draw():
    hub, host = make()
    hub.t = time.time()
    hub.state.config["time_limit_s"] = 100
    host.enable(True)
    host.set_settings(time=1, lives=0)
    host.tick(time.time() + 2)
    hub.hear(mac(1), H, "36,79,0,1,0,3,1,42")
    hub.hear(mac(2), H, "36,79,1,1,1,3,1,42")
    host.start()
    t0 = host.started_at
    host.tick(t0 + 1)
    host.tick(t0 + 101)
    assert hub.state.game.phase == "ended" and hub.state.game.draw
    assert hub.tx()[-1] == "tx,bcast,36,69,1,9,1,42"


def test_a_house_length_goes_out_as_untimed_and_the_board_ends_the_game_itself():
    hub, host = make()
    hub.t = time.time()
    host.enable(True)
    host.set_settings(time=1)                                # the guns' timer first
    host.set_settings(time_limit_s=300)                      # then a 5-minute house length
    assert host.settings["time"] == 0 and host.status()["rules"]["time_limit_s"] == 300
    assert hub.state.config["rules"]["time_limit_s"] == 300
    host.tick(time.time() + 2)
    assert hub.tx()[-1] == "tx,bcast,36,65,1,0,0,3,0,0,1,1,42"   # time bit 0 on the air
    hub.hear(mac(1), H, "36,79,0,1,0,3,1,42")
    hub.hear(mac(2), H, "36,79,1,1,1,3,1,42")
    host.start()
    t0 = host.started_at
    host.tick(t0 + 1)
    hub.hear(mac(2), mac(1), "36,68,0,1,1,0,0,1,42", 10)    # red scores once
    host.tick(t0 + 299)
    assert hub.state.game.phase == "live"
    host.tick(t0 + 301)
    assert hub.state.game.phase == "ended" and hub.tx()[-1] == "tx,bcast,36,69,1,0,1,42"
    host.set_settings(time=1)                                # back to the guns' timer clears the house length
    assert host.status()["rules"]["time_limit_s"] is None


def test_first_to_x_kills_ends_the_game_on_the_spot_and_never_applies_in_royale():
    hub, host = make()
    hub.t = time.time()
    host.enable(True)
    host.set_settings(team_kill_target=3)
    host.tick(time.time() + 2)
    hub.hear(mac(1), H, "36,79,0,1,0,3,1,42")
    hub.hear(mac(2), H, "36,79,1,1,1,3,1,42")
    hub.hear(mac(4), H, "36,79,3,1,2,3,1,42")                # a lone wolf
    host.start()
    host.tick(host.started_at + 1)
    for _ in range(2):
        hub.hear(mac(1), mac(4), "36,68,3,0,0,0,0,1,42", 5)  # the wolf tags red twice
    hub.hear(mac(4), mac(2), "36,68,1,3,2,0,0,1,42", 5)      # blue tags the wolf once
    assert hub.state.game.phase == "live"
    hub.hear(mac(2), mac(4), "36,68,3,1,1,0,0,1,42", 5)      # the wolf's third: game over, wolf wins
    assert hub.state.game.phase == "ended" and hub.tx()[-1] == "tx,bcast,36,69,1,13,1,42"
    # a royale ignores the target
    hub2, host2 = make()
    hub2.t = time.time()
    host2.enable(True)
    host2.set_settings(mode=1, team_kill_target=1)
    host2.tick(time.time() + 2)
    hub2.hear(mac(1), H, "36,79,0,1,0,3,1,42")
    hub2.hear(mac(2), H, "36,79,1,1,1,3,1,42")
    hub2.hear(mac(4), H, "36,79,3,1,2,3,1,42")
    host2.start()
    host2.tick(host2.started_at + 1)
    hub2.hear(mac(2), mac(1), "36,68,0,1,1,0,0,1,42", 5)
    assert hub2.state.game.phase == "live"                   # two sides still standing, target ignored


def test_a_mode_change_while_hosting_starts_over_under_the_other_host_index():
    hub, host = make()
    hub.t = time.time()
    host.enable(True)
    host.tick(time.time() + 2)
    hub.hear(mac(1), H, "36,79,0,1,0,3,1,42")
    assert hub.state.players[1].in_game and host.status()["host_index"] == 15
    n = len(hub.tx())
    host.set_settings(mode=1)
    host.tick(time.time() + 2)
    assert hub.tx()[n:n + 2] == ["tx,bcast,36,90,1,1,42"] * 2
    assert hub.tx()[-1] == "tx,bcast,36,65,1,0,0,3,0,1,1,1,42"
    assert not hub.state.players[1].in_game                      # everyone joins again, in the new mode
    assert host.status()["awaiting_rejoin"] and host.status()["host_index"] == 14
    assert hub.state.game.phase == "lobby" and hub.state.game_kind() == "royale"
    hub.hear(mac(1), H, "36,79,0,1,0,3,1,42")
    assert hub.tx()[-1] == f"tx,{mac(1)},36,97,14,3,0,0,0,0,0,1,42"
    assert hub.state.players[1].in_game and not host.status()["awaiting_rejoin"]
    # the same mode again is not a change
    n = len(hub.tx())
    host.set_settings(mode=1, lives=0)
    host.tick(time.time() + 2)
    assert all(not t.startswith("tx,bcast,36,90") for t in hub.tx()[n:])


def test_hiding_battle_royale_keeps_this_board_on_team_battle():
    hub, host = make(hide_royale=True)
    host.settings["mode"] = 1                                # left over from an earlier session
    host.enable(True)
    assert host.settings["mode"] == 0
    host.set_settings(mode=1)
    assert host.settings["mode"] == 0                        # the option is hidden, so it cannot be chosen
    hub.state.config["hide_royale"] = False
    host.set_settings(mode=1)
    assert host.settings["mode"] == 1


def test_hiding_five_lives_keeps_this_boards_team_battles_unlimited():
    hub, host = make(hide_five_lives=True)
    host.settings["lives"] = 0
    host.enable(True)
    assert host.settings["lives"] == 1
    host.set_settings(lives=0)
    assert host.settings["lives"] == 1                       # hidden, so it cannot be chosen
    hub.state.config["hide_royale"] = False
    host.set_settings(mode=1, lives=0)                       # in Royale the same bit is 200 HP: still offered
    assert host.settings["lives"] == 0
