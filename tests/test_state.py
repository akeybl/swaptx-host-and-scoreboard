"""Reducer tests against the protocol as mapped on the air (docs/mapping-2026-09-13.md)."""
from swaptx.frames import parse_line
from swaptx.protocol import Protocol
from swaptx.state import GameState
from conftest import line, mac, H, B

# settings tuples: (lighting, time, mode, lives)
TEAM = (0, 0, 0, 1)      # Team Battle, unlimited lives, no timer
TEAM5 = (0, 1, 0, 0)     # Team Battle, 5 lives, 10-minute timer
ROYALE = (0, 1, 1, 0)    # Battle Royale, storm on, low HP


def beacon(origin, phase, st):
    l, t, m, v = st
    return f"36,65,{origin},{l},0,{phase},{t},{m},{v},1,42"


def ack(phase, st=None):
    if st is None:
        return "36,97,1,3,0,0,0,0,0,1,42"          # lobby ack: placeholder zeros
    l, t, m, v = st
    return f"36,97,1,{phase},{l},0,{t},{m},{v},1,42"


def lives_token(st):
    return 1 if st[2] == 1 else (5 if st[3] == 0 else 255)


class Sim:
    def __init__(self, **kw):
        self.gs = GameState(Protocol(kw.pop("overrides", None)), names=kw.pop("names", {1: "Alex", 2: "Sam"}),
                            config=kw.pop("config", None))
        self.t = 1_700_000_000.0
        self.seq = 0

    def f(self, src, dst, txt, dt=1.0, seq=None):
        self.t += dt
        if seq is None:
            self.seq += 1
            seq = self.seq
        return self.gs.apply(parse_line(line(src, dst, txt, seq=seq), ts=self.t))

    # -- the real message flow --
    def host_up(self):
        return self.f(H, B, "36,90,1,1,42")

    def online(self, p, team, lives=1):
        evs = self.f(mac(p), H, f"36,79,{p-1},{lives},{team},3,1,42")
        evs += self.f(H, mac(p), ack(3), 0.1)
        return evs

    def lobby(self, st=TEAM):
        return self.f(H, B, beacon(1, 3, st))

    def start(self, st=TEAM, players=None):
        evs = self.f(H, B, beacon(1, 1, st))
        evs += self.f(H, B, beacon(1, 1, st), 0.1)
        for p, team in (players or {}).items():
            evs += self.f(mac(p), H, f"36,79,{p-1},{lives_token(st)},{team},1,1,42", 0.2)
            evs += self.f(H, mac(p), ack(1, st), 0.1)
        return evs

    def setup(self, teams=None, st=TEAM):
        teams = teams or {1: 0, 2: 1, 3: 0, 4: 1}
        self.host_up()
        for p, team in teams.items():
            self.online(p, team)
        self.lobby(st)
        return self.start(st, teams)

    def damage(self, shooter, victim, dt=1.0):
        return self.f(mac(victim), mac(shooter), f"36,75,{shooter-1},{victim-1},1,42", dt)

    def death(self, killer, victim, vteam, dt=1.0):
        return self.f(mac(victim), mac(killer), f"36,68,{killer-1},{victim-1},{vteam},0,0,1,42", dt)

    def report(self, p, team, lives, kills, elim=False, dt=1.0):
        return self.f(mac(p), H, f"36,101,{p-1},{team},{lives},{kills},0,{2 if elim else 1},{1 if elim else 0},1,42", dt)

    def over(self, winner, dt=2.0):
        evs = self.f(H, B, f"36,69,1,{winner},1,42", dt)
        self.f(H, B, f"36,69,1,{winner},1,42", 0.3)
        return evs


def kinds(evs):
    return [e["kind"] for e in evs]


def test_host_up_lobby_join_start_flow():
    s = Sim()
    evs = s.host_up()
    assert evs[0]["kind"] == "lobby" and s.gs.game.phase == "lobby"
    evs = s.online(1, 0)
    assert evs[0]["kind"] == "join" and s.gs.players[1].team == 0 and s.gs.players[1].team_source == "radio"
    s.online(2, 1)
    s.lobby(TEAM5)
    g = s.gs.game
    assert g.settings["mode_label"] == "Team Battle" and g.settings["lives"] == 0 and g.settings["time"] == 1 and g.settings["lighting"] == 0
    evs = s.start(TEAM5, {1: 0, 2: 1})
    assert evs[0]["kind"] == "game_start" and g.phase == "live" and g.start_source == "beacon"
    assert s.gs.players[1].lives_left == 5 and s.gs.players[1].lives_start == 5
    snap = s.gs.snapshot(now=s.t)
    assert snap["game"]["kind"] == "team"
    assert {p["number"]: p["team_name"] for p in snap["players"] if p["in_game"]} == {1: "Red", 2: "Blue"}


def test_relayed_beacons_never_restart_or_reopen():
    s = Sim()
    s.setup()
    gid = s.gs.game.id
    evs = s.f(mac(1), B, beacon(0, 1, TEAM), 3.0)        # a player relays the start
    assert s.gs.game.id == gid and evs[0]["wall"] is False
    evs = s.f(mac(2), B, beacon(0, 3, TEAM), 3.0)        # a relayed lobby beacon during the game
    assert s.gs.game.phase == "live" and s.gs.game.id == gid and evs[0]["wall"] is False
    # with no host beacon heard at all, a relay stands in for it
    s2 = Sim()
    s2.host_up(); s2.online(1, 0); s2.online(2, 1)
    s2.f(mac(1), B, beacon(0, 1, TEAM), 5.0)
    assert s2.gs.game.phase == "live" and s2.gs.game.start_source == "relay"


def test_death_counts_elimination_streak_and_first_blood():
    s = Sim()
    s.setup()
    evs = s.death(1, 2, 1)
    assert kinds(evs) == ["kill", "callout"] and evs[0]["title"] == "Alex ➜ Sam" and evs[1]["title"] == "FIRST KILL"
    p1, p2 = s.gs.players[1], s.gs.players[2]
    assert p1.kills == 1 and p2.deaths == 1 and p1.streak == 1 and p2.team == 1 and p2.lives_left is None and p2.alive
    s.death(1, 4, 1, 4)
    s.death(1, 2, 1, 4)
    assert p1.kills == 3 and p1.best_streak == 3 and s.gs.game.total_kills == 3
    s.death(2, 1, 0, 4)
    assert p1.streak == 0 and p1.deaths == 1 and p2.kills == 1


def test_damage_messages_never_count():
    s = Sim()
    s.setup()
    evs = s.damage(1, 2)
    s.damage(1, 2, 1.5)
    s.damage(1, 2, 1.5)
    assert evs[0]["kind"] == "damage" and evs[0]["wall"] is False
    assert s.gs.players[1].kills == 0 and s.gs.players[2].deaths == 0
    assert s.gs.players[2].hits_taken == 3 and s.gs.players[1].hits_dealt == 3


def test_radio_retries_are_folded_by_sequence_number():
    s = Sim()
    s.setup()
    s.death(1, 2, 1)
    s.f(mac(2), mac(1), "36,68,0,1,1,0,0,1,42", 0.05, seq=s.seq)     # same sequence number: a retry
    assert s.gs.players[1].kills == 1 and s.gs.frames_dup == 1
    s.death(1, 2, 1, 4.0)                                             # same payload, new sequence: a new death
    assert s.gs.players[1].kills == 2


def test_five_lives_game_eliminates_after_five_deaths():
    s = Sim()
    s.setup({1: 0, 2: 1}, TEAM5)
    for _ in range(4):
        s.death(1, 2, 1, 5)
    p2 = s.gs.players[2]
    assert p2.lives_left == 1 and p2.alive and not p2.eliminated
    evs = s.death(1, 2, 1, 5)
    assert p2.lives_left == 0 and p2.eliminated and "is OUT" in evs[0]["detail"]
    snap = s.gs.snapshot(now=s.t)
    assert [p for p in snap["players"] if p["number"] == 2][0]["eliminated"]


def test_state_report_catches_up_missed_kills_and_deaths():
    s = Sim()
    s.setup({1: 0, 2: 1}, TEAM5)
    s.death(1, 2, 1)
    evs = s.report(1, 0, 5, 3)                 # Alex says 3 kills: two deaths we never heard
    assert evs[0]["kind"] == "kill" and evs[0]["data"]["victim"] is None
    assert s.gs.players[1].kills == 3 and s.gs.game.total_kills == 3
    s.report(2, 1, 2, 0)                       # Sam says 2 lives left
    assert s.gs.players[2].lives_left == 2 and s.gs.players[2].deaths == 3
    s.report(2, 1, 0, 0, elim=True)
    assert s.gs.players[2].eliminated and not s.gs.players[2].alive
    s2 = Sim()
    s2.setup({1: 0, 2: 1}, TEAM)
    s2.report(2, 1, 253, 0)                    # an unlimited game counts down from 255
    assert s2.gs.players[2].deaths == 2 and s2.gs.players[2].lives_left is None


def test_royale_one_life_last_standing_and_individual_winner():
    s = Sim(names={1: "Alex", 2: "Sam", 3: "Kim"})
    s.setup({1: 2, 2: 2, 3: 2}, ROYALE)
    assert s.gs.game_kind() == "royale" and s.gs.players[1].lives_left == 1
    evs = s.death(1, 2, 2)
    assert s.gs.players[2].eliminated and any(e["title"] == "2 PLAYERS LEFT" for e in evs)
    evs = s.death(3, 1, 2, 3)
    assert any(e["title"] == "LAST ONE STANDING" and "Kim" in e["detail"] for e in evs)
    evs = s.over(12)                           # 10 + player index 2 = Kim
    assert evs[0]["title"] == "WINNER: KIM" and s.gs.game.winner_player == 3 and s.gs.game.winner_team is None


def test_two_player_royale_by_colour_names_the_player():
    s = Sim()
    s.setup({1: 0, 2: 1}, ROYALE)
    s.death(1, 2, 1)
    evs = s.over(0)                            # the host names the colour; only Alex is on it
    assert evs[0]["title"] == "WINNER: ALEX" and s.gs.game.winner_player == 1


def test_game_over_by_team_colour_and_relays():
    s = Sim()
    s.setup()
    s.death(1, 2, 1)
    evs = s.over(0)
    assert evs[0]["kind"] == "game_over" and evs[0]["title"] == "WINNER: RED TEAM" and s.gs.game.winner_team == 0
    g = s.gs.snapshot(now=s.t)["game"]
    assert g["phase"] == "ended" and g["summary"]["total_kills"] == 1
    labels = {a["label"] for a in g["summary"]["awards"]}
    assert "MVP" in labels and "First Kill" in labels
    evs = s.f(mac(1), B, "36,69,0,0,1,42", 3.0)          # a player relays it
    assert evs[0]["wall"] is False and s.gs.game.phase == "ended"
    s.host_up()                                          # the next host-up archives the game
    assert s.gs.game.phase == "lobby" and len(s.gs.games) == 1 and s.gs.games[0]["winner_team"] == 0


def test_inferred_winner_when_host_never_says():
    s = Sim()
    s.setup()
    s.death(1, 2, 1)
    s.death(1, 4, 1, 3)
    evs = s.f(None, None, "MANUAL,end", 5)
    assert s.gs.game.winner_team == 0 and evs[0]["title"] == "WINNER: RED TEAM"


def test_start_beacon_repeat_and_supersede():
    s = Sim()
    s.setup()
    gid = s.gs.game.id
    evs = s.f(H, B, beacon(1, 1, TEAM), 2.0)
    assert evs[0]["title"] == "Start beacon repeated" and s.gs.game.id == gid
    evs = s.f(H, B, beacon(1, 1, TEAM), 20.0)
    assert kinds(evs)[:2] == ["game_over", "game_start"] and s.gs.game.id != gid
    assert s.gs.games[-1]["end_reason"] == "superseded"


def test_missed_start_inferred_from_death_and_from_ack():
    s = Sim()
    s.host_up(); s.online(1, 0); s.online(2, 1)
    evs = s.death(1, 2, 1, 5)
    assert kinds(evs)[0] == "game_start" and s.gs.game.start_source == "inferred" and s.gs.players[1].kills == 1
    s2 = Sim()
    s2.host_up(); s2.online(1, 0)
    s2.f(H, mac(1), ack(1, TEAM5), 5)
    assert s2.gs.game.phase == "live" and s2.gs.game.start_source == "ack" and s2.gs.game.settings["lives"] == 0


def test_late_join_during_game_and_quiet_repeats():
    s = Sim()
    s.setup({1: 0, 2: 1}, TEAM5)
    evs = s.f(mac(3), H, "36,79,2,5,0,1,1,42", 30)
    assert evs[0]["kind"] == "join" and "late" in evs[0]["title"] and s.gs.players[3].late_join
    assert s.gs.players[3].lives_left == 5 and s.gs.players[3].team == 0
    evs = s.f(mac(1), H, "36,79,0,5,0,1,1,42", 4)         # players repeat their report until acked
    assert evs[0]["kind"] == "join" and evs[0]["wall"] is False and s.gs.players[1].gun_restarts == 0


def test_team_switch_and_fresh_link():
    s = Sim()
    s.host_up()
    evs = s.f(mac(1), H, "36,79,0,100,0,3,1,42")            # headset just linked: its "red" means nothing yet
    assert evs[0]["kind"] == "join" and s.gs.players[1].team is None
    s.online(1, 1)
    assert s.gs.players[1].team == 1
    evs = s.online(1, 3)
    assert evs[0]["kind"] == "team" and "Green" in evs[0]["title"] and s.gs.players[1].team == 3


def test_manual_commands_and_replay_determinism():
    s = Sim()
    s.f(None, None, "MANUAL,start,1")
    assert s.gs.game.phase == "live" and s.gs.game.mode == 1
    s.death(1, 2, 2)
    s.f(None, None, "MANUAL,end")
    assert s.gs.game.phase == "ended"
    frames = [parse_line(line(mac(1), H, "36,79,0,5,0,1,1,42", seq=1), ts=10.0),
              parse_line(line(mac(2), mac(1), "36,68,0,1,1,0,0,1,42", seq=2), ts=12.0)]
    a, b = GameState(), GameState()
    for fr in frames:
        a.apply(fr)
    for fr in frames:
        b.apply(fr)
    sa, sb = a.snapshot(now=20.0), b.snapshot(now=20.0)
    assert sa["players"] == sb["players"] and sa["game"]["total_kills"] == 1


def test_observed_protocol_lab_hints():
    s = Sim()
    s.setup({1: 0, 2: 1})
    s.death(1, 2, 1)
    obs = s.gs.observed_dict()
    assert obs["68"]["count"] == 1 and obs["68"]["tokens"]["3"]["eq_src0"] == 1
    assert obs["79"]["tokens"]["2"]["eq_src0"] == obs["79"]["count"] >= 4


def test_unknown_messages_are_kept_but_hidden_from_wall():
    s = Sim()
    evs = s.f(mac(2), mac(3), "36,100,7,42")
    assert evs[0]["kind"] == "unknown" and evs[0]["wall"] is False and "Combat-family" in evs[0]["title"]
    evs = s.f(mac(1), B, "$DD,1,*")
    assert evs[0]["kind"] == "brx"


def test_host_ack_placeholders_do_not_override_beacon_settings():
    s = Sim()
    s.host_up()
    s.lobby(TEAM5)
    s.f(H, mac(1), ack(3), 1)
    assert s.gs.game.settings["lives"] == 0 and s.gs.game.settings["time"] == 1 and s.gs.game.settings_source == "beacon"
    s.online(1, 0)
    s.start(TEAM5, {1: 0})
    s.f(H, mac(1), "36,97,1,1,1,0,0,1,1,1,42", 1)          # a contradicting in-game ack never beats the beacon
    assert s.gs.game.settings["mode"] == 0 and s.gs.game.settings_source == "beacon"
    s2 = Sim()
    s2.f(H, mac(1), ack(3), 1)                              # an ack alone opens the lobby, without settings
    assert s2.gs.game.phase == "lobby" and "mode" not in s2.gs.game.settings


def test_team_carries_over_as_a_guess_and_override_precedence():
    s = Sim()
    s.setup({1: 0, 2: 1})
    assert s.gs.players[1].team == 0 and s.gs.players[1].team_source == "radio"
    s.over(0)
    s.host_up()
    p1 = s.gs.players[1]
    assert p1.team is None and p1.team_carried == 0 and p1.effective_team == 0 and p1.team_source == "carried"
    s.gs.set_team_override(1, 1)
    assert p1.effective_team == 1 and p1.team_source == "override"
    s.online(1, 2)                                          # the gun now says free-for-all: radio beats both
    assert p1.team == 2 and p1.effective_team == 2 and p1.team_source == "radio"
    s.gs.set_team_override(9, 3)
    assert s.gs.players[9].team_source == "override" and s.gs.players[9].effective_team == 3


def test_free_for_all_from_the_team_pick():
    s = Sim(names={1: "Alex", 2: "Sam", 3: "Kim"})
    s.setup({1: 2, 2: 2, 3: 2})
    assert s.gs.game_kind() == "team"                      # no free-for-all kind: wolves inside a team battle
    evs = s.death(1, 2, 2)
    assert kinds(evs) == ["kill", "callout"]                # no same-team warning in free-for-all
    s2 = Sim()
    s2.setup({1: 0, 2: 2})
    assert s2.gs.game_kind() == "team"


def test_same_team_death_is_a_warning_not_friendly_fire():
    s = Sim()
    s.setup({1: 0, 2: 0})
    evs = s.death(1, 2, 0)
    assert kinds(evs) == ["kill", "warning", "callout"] and s.gs.players[1].team_kills == 0
    assert s.gs.snapshot(now=s.t)["teams"][0]["score"] == 1


def test_timed_game_auto_ends_when_host_beacon_is_missed():
    cfg = {"time_limit_s": 100, "auto_end_grace_s": 30}
    s = Sim(config=cfg)
    s.setup({1: 0, 2: 1}, TEAM5)
    t0 = s.gs.game.started_at
    s.death(1, 2, 1, 10)
    assert s.gs.tick(t0 + 120) == []                         # inside the grace period
    evs = s.gs.tick(t0 + 131)
    assert evs and evs[0]["kind"] == "game_over" and s.gs.game.end_reason == "time_expired"
    assert s.gs.game.ended_at == t0 + 100 and s.gs.game.winner_team == 0
    s2 = Sim(config=cfg)
    s2.setup({1: 0, 2: 1}, TEAM5)
    t0 = s2.gs.game.started_at
    s2.death(1, 2, 1, 105)                                   # a death after the deadline: still playing
    assert s2.gs.tick(t0 + 200) == [] and s2.gs.game.phase == "live"
    s3 = Sim(config=cfg)
    s3.setup({1: 2, 2: 2}, ROYALE)                           # storm mode never auto-ends
    assert s3.gs.tick(s3.gs.game.started_at + 500) == []
    s4 = Sim(config=cfg)
    s4.setup({1: 0, 2: 1}, TEAM5)
    evs = s4.death(1, 2, 1, 200)                             # first frame after deadline + grace
    assert kinds(evs)[:2] == ["game_over", "game_start"]


def test_streak_callout_is_flash_only():
    s = Sim(names={1: "Alex", 2: "Sam", 3: "Kim", 4: "Maya"})
    s.setup()
    for v in (2, 4, 2):
        evs = s.death(1, v, 1, 4)
    streak = [e for e in evs if e["kind"] == "callout"][0]
    assert streak["feed"] is False and streak["wall"] is True
    assert all(e["feed"] for e in s.gs.timeline if e["kind"] != "callout")
    fb = [e for e in s.gs.timeline if e["title"] == "FIRST KILL"][0]
    assert fb["kind"] == "callout" and fb["feed"] is False and fb["wall"] is True
    kill0 = [e for e in s.gs.timeline if e["kind"] == "kill"][0]
    assert kill0["data"]["first_blood"] is True


def test_custom_team_names_flow_through_titles_and_tables():
    s = Sim()
    s.gs.set_team_name(0, "Sharks")
    s.gs.set_team_name(1, "  ")            # blank = default colour
    s.setup({1: 0, 2: 1})
    s.death(1, 2, 1)
    snap = s.gs.snapshot(now=s.t)
    assert snap["team_names"] == {"0": "Sharks", "1": "Blue", "2": "Free for all", "3": "Green"}
    assert snap["teams"][0]["name"] == "Sharks" and snap["players"][0]["team_name"] == "Sharks"
    evs = s.over(0)
    assert evs[0]["title"] == "SHARKS WIN" and s.gs.game.summary["winner_label"] == "Sharks"
    s.gs.set_team_name(0, "Team Rocket")
    assert s.gs.tname(0) == "Team Rocket"
    s2 = Sim()
    s2.gs.set_team_name(1, "blue")         # same as the colour: not custom
    assert not s2.gs.team_is_custom(1) and s2.gs.tname(1) == "Blue"


def test_team_columns_keep_first_heard_order():
    s = Sim(names={1: "Alex", 2: "Sam", 3: "Kim", 4: "Maya"})
    s.host_up()
    s.online(2, 1)                          # blue is the first colour heard
    assert s.gs.game.team_order == [1]
    s.online(1, 0)
    assert s.gs.game.team_order == [1, 0]
    s.lobby()
    s.start(TEAM, {1: 0, 2: 1})
    assert s.gs.game.team_order == [1, 0] and s.gs.snapshot(now=50.0)["game"]["team_order"] == [1, 0]


def test_battle_royale_in_squads_is_last_team_standing():
    s = Sim(names={1: "Alex", 2: "Sam", 3: "Kim", 4: "Maya"})
    s.setup({1: 0, 2: 0, 3: 1, 4: 1}, ROYALE)
    assert s.gs.game_kind() == "royale" and s.gs.squads()
    s.death(1, 3, 1)
    s.death(1, 4, 1, 3)
    s.f(None, None, "MANUAL,end", 5)                         # host never said: last squad standing
    assert s.gs.game.winner_team == 0 and s.gs.game.winner_player is None
    s2 = Sim(names={1: "Alex", 2: "Sam", 3: "Kim", 4: "Maya"})
    s2.setup({1: 0, 2: 0, 3: 1, 4: 1}, ROYALE)
    s2.death(1, 3, 1)
    s2.death(1, 4, 1, 3)
    evs = s2.over(0)
    assert evs[0]["title"] == "WINNER: RED TEAM" and s2.gs.game.winner_team == 0


def test_lone_wolves_inside_a_team_battle():
    s = Sim(names={1: "Alex", 2: "Sam", 3: "Kim", 4: "Maya"})
    s.setup({1: 0, 2: 2, 3: 2, 4: 1})
    assert s.gs.game_kind() == "team"
    evs = s.death(2, 3, 2)                          # two lone wolves can shoot each other
    assert kinds(evs) == ["kill", "callout"]
    evs = s.death(2, 1, 0, 3)                       # and anyone else
    assert kinds(evs) == ["kill"]
    sides = s.gs._sides([p for p in s.gs.players.values() if p.in_game])
    assert sorted(str(t) for t, _ in sides) == ["0", "1", "None", "None"]
    # royale in squads with a lone wolf: the wolf standing alone wins as a player
    s2 = Sim(names={1: "Alex", 2: "Sam", 3: "Kim"})
    s2.setup({1: 0, 2: 0, 3: 2}, ROYALE)
    s2.death(3, 1, 0)
    s2.death(3, 2, 0, 3)
    s2.f(None, None, "MANUAL,end", 5)
    assert s2.gs.game.winner_player == 3 and s2.gs.game.winner_team is None


def test_the_yellow_pick_is_never_renamed():
    s = Sim(names={1: "Alex"})
    s.gs.set_team_name(0, "Sharks")
    s.gs.set_team_name(2, "Wolves")                         # the free-for-all pick is not a team
    assert s.gs.tname(0) == "Sharks" and s.gs.tname(2) == "Free for all"
    assert s.gs.team_is_custom(0) and not s.gs.team_is_custom(2)
