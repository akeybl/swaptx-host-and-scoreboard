"""Battle Royale storm (2026-09-13 test): silent on the air, deaths with killer 99 go to the host,
and standing beats score when a game is called."""
from swaptx.frames import parse_line
from swaptx.protocol import Protocol
from swaptx.state import GameState
from conftest import line, mac, H, B

T0 = 1_700_000_000.0


def game(names):
    st = GameState(Protocol(), names=names)
    st._q = 0
    return st


def hear(st, dt, src, dst, txt):
    st._q += 1
    return st.apply(parse_line(line(src, dst, txt, seq=st._q), ts=T0 + dt))


def test_a_storm_death_credits_nobody_and_the_named_colour_is_the_lone_players_win():
    st = game({1: "Alex", 4: "Maya"})
    hear(st, 0, mac(1), H, "36,79,0,100,0,3,1,42")            # gun 1 links up, then becomes the host
    hear(st, 1, H, B, "36,90,1,1,42")
    hear(st, 2, H, B, "36,65,1,1,0,3,1,1,1,1,42")             # lobby: royale, storm on, 500 HP, low light
    hear(st, 3, mac(4), H, "36,79,3,1,0,3,1,42")              # gun 4 is red
    hear(st, 4, H, B, "36,65,1,1,0,1,1,1,1,1,42")             # start
    assert st.game.phase == "live" and st.game_kind() == "royale"
    assert st.clock(T0 + 10)["storm_at_s"] == 180 and st.clock(T0 + 10)["storm_siren_s"] == 120
    evs = hear(st, 360, mac(4), H, "36,68,99,3,0,0,0,1,42")   # the storm takes gun 4
    kill = [e for e in evs if e["kind"] == "kill"][0]
    assert kill["data"]["storm"] and kill["data"]["killer"] is None and kill["data"]["victim"] == 4
    assert st.players[4].deaths == 1 and st.players[4].eliminated
    assert st.game.total_kills == 0 and all(p.kills == 0 for p in st.players.values())
    assert st.game.first_blood_player is None
    hear(st, 367, H, B, "36,69,1,1,1,42")                     # host: blue wins = the host's own gun
    g = st.game
    assert g.phase == "ended" and g.winner_team == 1 and g.winner_player == 1
    assert st.players[1].effective_team == 1
    sm = st.game_summary(T0 + 400)
    assert sm["winner_label"] == "Alex"
    assert [(t["team"], t["rank"]) for t in sm["teams"]] == [(1, 1), (0, 2)]
    assert st.clock(T0 + 400)["storm_at_s"] == 180             # still a storm game after the end


def test_a_side_with_nobody_left_ranks_below_a_standing_side_whatever_its_kills():
    st = game({1: "Alex", 2: "Sam", 4: "Maya"})
    hear(st, 0, H, B, "36,90,1,1,42")
    hear(st, 1, H, B, "36,65,1,0,0,3,0,0,0,1,42")             # team battle, five lives
    hear(st, 2, mac(1), H, "36,79,0,1,0,3,1,42")              # red: 1 and 2
    hear(st, 3, mac(2), H, "36,79,1,1,0,3,1,42")
    hear(st, 4, mac(4), H, "36,79,3,1,1,3,1,42")              # blue: 4, alone
    hear(st, 5, H, B, "36,65,1,0,0,1,0,0,0,1,42")             # start
    for n, team in ((1, 0), (2, 0), (4, 1)):
        hear(st, 6, mac(n), H, f"36,79,{n-1},5,{team},1,1,42")
    t = 10
    for _ in range(4):                                       # Maya takes four lives off each red player
        for v in (1, 2):
            t += 5
            hear(st, t, mac(v), mac(4), f"36,68,3,{v-1},0,0,0,1,42")
    for _ in range(5):                                       # red takes all five of Maya's
        t += 5
        hear(st, t, mac(4), mac(1), "36,68,0,3,1,0,0,1,42")
    assert st.players[4].eliminated and st.players[1].lives_left == 1
    teams = st._team_table(T0 + t)
    assert teams[0]["team"] == 0 and teams[0]["kills"] == 5 and teams[0]["rank"] == 1
    assert teams[1]["team"] == 1 and teams[1]["kills"] == 8 and teams[1]["rank"] == 2
    assert teams[1]["out_at"] == T0 + t
    assert st._infer_winner() == (0, None)


def test_lone_wolves_are_never_ranked_as_a_team():
    st = game({1: "Alex", 2: "Sam", 4: "Maya"})
    hear(st, 0, H, B, "36,90,1,1,42")
    hear(st, 1, H, B, "36,65,1,0,0,3,0,0,1,1,42")
    hear(st, 2, mac(1), H, "36,79,0,1,0,3,1,42")              # red
    hear(st, 3, mac(2), H, "36,79,1,1,1,3,1,42")              # blue
    hear(st, 4, mac(4), H, "36,79,3,1,2,3,1,42")              # lone wolf
    hear(st, 5, H, B, "36,65,1,0,0,1,0,0,1,1,42")
    hear(st, 10, mac(1), mac(4), "36,68,3,0,0,0,0,1,42")      # the wolf tags red
    hear(st, 15, mac(2), mac(4), "36,68,3,1,1,0,0,1,42")      # and blue
    hear(st, 20, mac(4), mac(2), "36,68,1,3,2,0,0,1,42")      # blue tags the wolf
    teams = st._team_table(T0 + 30)
    ranks = {t["team"]: t["rank"] for t in teams}
    assert ranks[2] is None and ranks[1] == 1 and ranks[0] == 2


def test_when_the_storm_takes_everyone_the_last_to_fall_wins_and_the_hosts_death_is_broadcast():
    st = game({1: "Alex", 4: "Maya"})
    hear(st, 0, mac(1), H, "36,79,0,100,0,3,1,42")
    hear(st, 1, H, B, "36,90,1,1,42")
    hear(st, 2, mac(4), H, "36,79,3,1,0,3,1,42")              # gun 4 red
    hear(st, 3, H, B, "36,65,1,1,0,1,1,1,1,1,42")             # start: royale, storm on
    hear(st, 369, H, B, "36,68,99,0,1,0,0,1,42")              # the host gun (blue) falls first, broadcast
    assert st.players[1].eliminated and st.players[1].team == 1 and st.game.total_kills == 0
    hear(st, 371, mac(4), H, "36,68,99,3,0,0,0,1,42")         # gun 4 two seconds later
    assert st.players[4].eliminated
    assert st._infer_winner() == (0, None)                    # nobody left: the side of the last to fall, as the host names it
    hear(st, 372, H, B, "36,69,1,0,1,42")                     # and that is what the host said: red
    g = st.game
    assert g.phase == "ended" and g.winner_team == 0 and g.winner_player == 4
    assert st._winner_label() == "Maya"
    sm = st.game_summary(T0 + 400)
    assert [(t["team"], t["rank"]) for t in sm["teams"]] == [(0, 1), (1, 2)]
    assert g.summary["winner_player"] == 4 and g.summary["winner_label"] == "Maya"   # the frozen summary agrees


def test_the_timer_running_out_at_nil_nil_is_a_draw():
    st = game({1: "Alex", 4: "Maya"})
    hear(st, 0, H, B, "36,90,1,1,42")
    hear(st, 1, H, B, "36,65,1,1,0,3,1,0,0,1,42")             # team battle, five lives, timer on
    hear(st, 2, mac(4), H, "36,79,3,1,2,3,1,42")
    hear(st, 3, H, B, "36,65,1,1,0,1,1,0,0,1,42")             # start (22:54:14)
    evs = hear(st, 603, H, B, "36,69,1,9,1,42")               # 23:04:14: winner 9
    g = st.game
    assert g.phase == "ended" and g.draw and g.winner_team is None and g.winner_player is None
    over = [e for e in evs if e["kind"] == "game_over"][0]
    assert over["title"] == "DRAW" and st._winner_label() == "Draw"
    assert g.summary["draw"] is True and st.game_summary(T0 + 700)["draw"] is True


def test_a_host_naming_the_yellow_pick_means_the_best_wolf_or_a_tie_between_wolves():
    st = game({1: "Alex", 2: "Sam", 4: "Maya"})
    hear(st, 0, H, B, "36,90,1,1,42")
    hear(st, 1, H, B, "36,65,1,0,0,3,0,0,1,1,42")
    hear(st, 2, mac(1), H, "36,79,0,1,0,3,1,42")              # red
    hear(st, 3, mac(2), H, "36,79,1,1,2,3,1,42")              # wolves
    hear(st, 4, mac(4), H, "36,79,3,1,2,3,1,42")
    hear(st, 5, H, B, "36,65,1,0,0,1,0,0,1,1,42")
    hear(st, 10, mac(1), mac(2), "36,68,1,0,0,0,0,1,42")      # Sam tags red twice, Maya once
    hear(st, 15, mac(1), mac(2), "36,68,1,0,0,0,0,1,42")
    hear(st, 20, mac(1), mac(4), "36,68,3,0,0,0,0,1,42")
    hear(st, 30, H, B, "36,69,1,2,1,42")                      # a host that names the yellow pick
    g = st.game
    assert g.phase == "ended" and g.winner_team is None and g.winner_player == 2 and not g.draw
    assert st._winner_label() == "Sam"
    # wolves sharing the top: a tie between those two, named
    st = game({1: "Alex", 2: "Sam", 4: "Maya"})
    hear(st, 0, H, B, "36,90,1,1,42")
    hear(st, 1, H, B, "36,65,1,0,0,3,0,0,1,1,42")
    hear(st, 2, mac(1), H, "36,79,0,1,0,3,1,42")
    hear(st, 3, mac(2), H, "36,79,1,1,2,3,1,42")
    hear(st, 4, mac(4), H, "36,79,3,1,2,3,1,42")
    hear(st, 5, H, B, "36,65,1,0,0,1,0,0,1,1,42")
    hear(st, 10, mac(1), mac(2), "36,68,1,0,0,0,0,1,42")
    hear(st, 20, mac(1), mac(4), "36,68,3,0,0,0,0,1,42")
    assert st._infer_winner() == (None, None)                 # nobody ahead: the board would call it a draw
    evs = hear(st, 30, H, B, "36,69,1,2,1,42")
    g = st.game
    assert g.draw and g.winner_player is None and g.tied_players == [2, 4]
    over = [e for e in evs if e["kind"] == "game_over"][0]
    assert over["title"] == "SAM & MAYA TIE" and st._winner_label() == "Tie: Sam & Maya"
    assert g.summary["tie_title"] == "SAM & MAYA TIE" and g.summary["tied_players"] == [2, 4]


def test_a_timer_draw_between_teams_names_both():
    st = game({1: "Alex", 2: "Sam"})
    hear(st, 0, H, B, "36,90,1,1,42")
    hear(st, 1, H, B, "36,65,1,0,0,3,1,0,1,1,42")
    hear(st, 2, mac(1), H, "36,79,0,1,0,3,1,42")
    hear(st, 3, mac(2), H, "36,79,1,1,1,3,1,42")
    hear(st, 4, H, B, "36,65,1,0,0,1,1,0,1,1,42")
    hear(st, 10, mac(1), mac(2), "36,68,1,0,0,0,0,1,42")     # one each
    hear(st, 20, mac(2), mac(1), "36,68,0,1,1,0,0,1,42")
    evs = hear(st, 604, H, B, "36,69,1,9,1,42")
    over = [e for e in evs if e["kind"] == "game_over"][0]
    assert over["title"] == "RED & BLUE TIE" and st.game.tied_teams == [0, 1]
