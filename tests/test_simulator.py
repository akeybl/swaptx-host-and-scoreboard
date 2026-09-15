"""The simulator must speak the same protocol the reducer understands."""
from swaptx.frames import parse_line
from swaptx.simulator import Simulator
from swaptx.state import GameState


def run_sim(mode, players=6, seed=3):
    lines = []
    sim = Simulator(lambda ln, ts: lines.append((ln, ts)), lambda c, p: None, players=players, mode=mode,
                    speed=10000, seed=seed, game_length_s=120, loop=False)
    sim.run_game()
    return lines


def test_team_battle_simulation_plays_a_full_game():
    gs = GameState()
    t = 0.0
    for ln, _ in run_sim(0):
        t += 0.5
        fr = parse_line(ln, ts=t)
        gs.apply(fr)
    g = gs.game
    assert g.phase == "ended" and g.mode == 0 and g.winner_team in (0, 1) and g.total_kills > 0
    assert g.settings_source == "beacon" and g.summary["awards"]
    lives = {p.number: p.lives_left for p in gs.players.values() if p.in_game}
    assert all(v is None for v in lives.values()) or all(v is not None for v in lives.values())


def test_royale_simulation_ends_with_last_player_standing():
    gs = GameState()
    t = 0.0
    for ln, _ in run_sim(1, players=5, seed=9):
        t += 0.5
        gs.apply(parse_line(ln, ts=t))
    g = gs.game
    assert g.phase == "ended" and g.mode == 1 and (g.winner_player is not None or g.winner_team is not None)
    alive = [p for p in gs.players.values() if p.in_game and p.alive]
    # last player standing, or last squad standing (a survivor never heard on the radio has no known team)
    assert len(alive) <= 1 or len({p.effective_team for p in alive if p.effective_team is not None}) <= 1
