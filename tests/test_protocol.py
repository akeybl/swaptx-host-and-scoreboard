from swaptx.frames import parse_line
from swaptx.protocol import Protocol, decode_winner, settings_are_placeholders
from conftest import line, mac, H, B


def fr(src, dst, txt):
    return parse_line(line(src, dst, txt), ts=1.0)


def test_start_beacon_decode():
    ev = Protocol().decode(fr(H, B, "36,65,1,0,0,1,1,0,0,1,42"))
    assert ev.kind == "game_control" and ev.well_formed and ev.src_role == "host"
    f = ev.fields
    assert f["origin"] == 1 and f["lighting"] == 0 and f["phase"] == 1 and f["time"] == 1 and f["mode"] == 0 and f["lives"] == 0
    relay = Protocol().decode(fr(mac(1), B, "36,65,0,0,0,3,1,0,0,1,42"))
    assert relay.fields["origin"] == 0 and relay.fields["phase"] == 3 and relay.src_role == "player"


def test_report_in_decode_and_players_are_1_based():
    ev = Protocol().decode(fr(mac(7), H, "36,79,6,5,1,1,1,42"))
    assert ev.kind == "report_in" and ev.fields["player"] == 7 and ev.src_player == 7 and ev.dst_role == "host"
    assert ev.fields["lives_left"] == 5 and ev.fields["player_team"] == 1 and ev.fields["phase"] == 1
    fresh = Protocol().decode(fr(mac(2), H, "36,79,1,100,0,3,1,42"))
    assert fresh.fields["lives_left"] == 100 and fresh.fields["phase"] == 3


def test_damage_death_and_state_report_decode():
    d = Protocol().decode(fr(mac(4), mac(1), "36,75,0,3,1,42"))
    assert d.kind == "damage" and d.fields["player"] == 1 and d.fields["victim"] == 4
    k = Protocol().decode(fr(mac(4), mac(1), "36,68,0,3,2,0,0,1,42"))
    assert k.kind == "death" and k.fields["player"] == 1 and k.fields["victim"] == 4 and k.fields["victim_team"] == 2
    r = Protocol().decode(fr(mac(1), H, "36,101,0,2,254,3,1,1,0,1,42"))
    assert r.kind == "state_report" and r.fields["player"] == 1 and r.fields["player_team"] == 2
    assert r.fields["lives_left"] == 254 and r.fields["kills"] == 3
    a = Protocol().decode(fr(H, mac(1), "36,102,0,1,0,1,42"))
    assert a.kind == "report_ack" and a.fields["player"] == 1


def test_game_over_winner_encoding():
    ev = Protocol().decode(fr(H, B, "36,69,1,13,1,42"))
    assert ev.kind == "game_over" and ev.fields["origin"] == 1 and ev.fields["winner"] == 13
    assert decode_winner(13) == (None, 4) and decode_winner(0) == (0, None) and decode_winner(3) == (3, None)
    assert decode_winner(99) == (None, None) and decode_winner(None) == (None, None)
    assert decode_winner(9) == (None, None)                    # a draw: the caller checks WINNER_DRAW


def test_host_ack_placeholders():
    p = Protocol()
    lobby = p.decode(fr(H, mac(1), "36,97,1,3,0,0,0,0,0,1,42"))
    assert lobby.kind == "host_ack" and lobby.fields["phase"] == 3 and settings_are_placeholders(lobby.fields)
    live = p.decode(fr(H, mac(1), "36,97,1,1,0,0,1,0,0,1,42"))
    assert live.fields["phase"] == 1 and live.fields["time"] == 1 and not settings_are_placeholders(live.fields)
    up = p.decode(fr(H, B, "36,90,1,1,42"))
    assert up.kind == "host_up"


def test_text_after_the_nul_is_ignored():
    txt = "36,79,1,1,1,3,1,42" + "\x00" * 4 + "xV"      # the 32-byte text field is followed by heap garbage
    ev = Protocol().decode(fr(mac(2), H, txt))
    assert ev.kind == "report_in" and ev.well_formed and ev.tokens[-1] == "42"

def test_overrides_relabel_a_field():
    p = Protocol({"75": {"fields": {"4": {"role": "unknown", "label": "damage units"}}}})
    ev = p.decode(fr(mac(2), mac(3), "36,75,2,1,1,42"))
    assert ev.kind == "damage" and ev.fields["unknown"] == {"4": 1}
    p2 = Protocol({"120": {"name": "hit", "label": "Hit", "fields": {"2": {"role": "player", "base": 0}}}})
    assert p2.decode(fr(mac(2), mac(3), "36,120,1,42")).kind == "hit"


def test_unknown_and_combat_family_and_capture_and_brx():
    p = Protocol()
    assert p.decode(fr(mac(2), mac(3), "36,100,5,42")).kind == "combat_unknown"
    assert p.decode(fr(mac(2), mac(3), "36,77,5,42")).kind == "unknown_36"
    ev = p.decode(fr("00:00:00:00:00:fe", B, "CAPTURE,2,1,15"))
    assert ev.kind == "capture" and ev.fields == {"jbox_id": 2, "team": 1, "player": 15}
    brx = p.decode(fr(mac(1), B, "$DD,1,*"))
    assert brx.kind == "brx" and brx.well_formed
    assert not p.decode(fr(H, B, "36,65,1,1")).well_formed
