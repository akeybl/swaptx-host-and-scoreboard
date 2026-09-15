import json
import time

import pytest
from fastapi.testclient import TestClient

from swaptx.frames import Frame
from swaptx.store import Store
from swaptx.server import Hub, create_app
from conftest import line, mac, H, B


def test_store_roundtrip(tmp_path):
    st = Store(tmp_path / "t.db")
    f = Frame(ts=5.0, src=mac(1), dst=H, txt="36,79,0,0,42", rssi=-50, seq=3)
    fid = st.insert_frame(f)
    assert fid == 1 and f.id == 1
    got = st.frames_since(ts=0)
    assert len(got) == 1 and got[0].txt == "36,79,0,0,42" and got[0].rssi == -50 and got[0].seq == 3
    st.set_player(3, name="Kai", team_override=2)
    st.set_player(3, name="Kai2")
    assert st.players()[3]["name"] == "Kai2" and st.players()[3]["team_override"] == 2
    st.set_player(3, clear_team=True)
    assert st.players()[3]["team_override"] is None
    st.set_setting("config", {"lives_low": 3})
    assert st.get_setting("config")["lives_low"] == 3 and st.all_settings()["config"]["lives_low"] == 3
    st.upsert_game({"id": "g1", "started_at": 1.0, "ended_at": None, "status": "live", "mode": 0, "summary": {"a": 1}})
    st.upsert_game({"id": "g1", "started_at": 1.0, "ended_at": 9.0, "status": "ended", "mode": 0, "summary": {"a": 2}})
    assert st.game("g1")["summary"] == {"a": 2} and st.latest_open_game() is None and len(st.games()) == 1
    st.close()


@pytest.fixture
def client(tmp_path):
    hub = Hub(str(tmp_path / "s.db"), None, 115200, simulate=True, sim_opts={"players": 4, "speed": 100, "loop": False})
    hub.simulate = False   # don't start the simulator thread; we feed lines through the API
    app = create_app(hub)
    with TestClient(app) as c:
        c.hub = hub
        yield c


def feed(c, *lines):
    return c.post("/api/ingest", json={"lines": list(lines)}).json()["events"]


def test_server_flow_and_restart_replay(client, tmp_path):
    c = client
    assert c.get("/api/health").json()["ok"]
    c.put("/api/players/1", json={"name": "Alex"})
    c.put("/api/players/2", json={"name": "Sam", "team_override": 1})
    feed(c, line(H, B, "36,65,1,1,0,3,1,0,0,1,42"), line(mac(1), H, "36,79,0,1,0,3,1,42"), line(mac(2), H, "36,79,1,1,1,3,1,42"))
    feed(c, line(H, B, "36,65,1,1,0,1,1,0,0,1,42"))
    evs = feed(c, line(mac(2), mac(1), "36,68,0,1,1,0,0,1,42"))
    assert evs[0]["title"] == "Alex ➜ Sam"
    st = c.get("/api/state").json()
    assert st["game"]["phase"] == "live" and st["players"][0]["display_name"] == "Alex"
    assert c.get("/api/players").json()[1]["team_override"] == 1
    tl = c.get("/api/timeline").json()
    assert any(e["kind"] == "kill" for e in tl)
    fr = c.get("/api/frames").json()
    assert fr[-1]["kind"] == "death" and fr[-1]["src_player"] == 2

    # settings + rules
    r = c.put("/api/settings", json={"lives_low": 3, "rules": {"label": "First to 10", "team_kill_target": 10, "time_limit_s": 300}})
    assert r.status_code == 200 and c.get("/api/state").json()["game"]["clock"]["limit_s"] == 300
    assert c.get("/api/settings").json()["config"]["lives_low"] == 3

    # protocol relabel triggers a full recompute
    r = c.put("/api/protocol/overrides", json={"overrides": {"101": {"fields": {"4": {"role": "unknown"}}}}})
    assert r.status_code == 200 and r.json()["replayed"] >= 5
    assert c.get("/api/protocol").json()["registry"]["opcodes"]["101"]["fields"][2]["role"] == "unknown"
    assert c.get("/api/state").json()["game"]["total_kills"] == 1

    # manual end, history, export
    c.post("/api/game/end")
    st = c.get("/api/state").json()
    assert st["game"]["phase"] == "ended" and st["game"]["summary"]["total_kills"] == 1
    games = c.get("/api/games").json()
    assert len(games) == 1 and games[0]["status"] == "ended"
    gid = games[0]["id"]
    assert c.get(f"/api/games/{gid}").json()["summary"]["winner_label"] == "Red team"
    csv_text = c.get(f"/api/games/{gid}/export?format=csv").text
    assert "Alex" in csv_text and "kill" in csv_text
    assert c.get("/api/games/nope").status_code == 404
    assert c.post("/api/dongle/command", json={"line": "rm -rf"}).status_code == 400

    # ---- restart: a fresh Hub on the same DB must rebuild the same state ----
    hub2 = Hub(str(tmp_path / "s.db"), None, 115200, simulate=False)
    n = hub2.replay()
    assert n >= 6
    s2 = hub2.state.snapshot()
    assert s2["game"]["phase"] == "ended" and s2["game"]["total_kills"] == 1
    assert s2["names"] == {"1": "Alex", "2": "Sam"} and s2["team_overrides"] == {"2": 1}
    assert hub2.state.config["lives_low"] == 3 and hub2.state.config["rules"]["team_kill_target"] == 10
    assert hub2.protocol.overrides["101"]["fields"]["4"]["role"] == "unknown"
    hub2.stop()


def test_websocket_snapshot_and_update(client):
    c = client
    with c.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot" and "protocol" in first
        feed(c, line(mac(3), H, "36,79,2,0,42"))
        msg = ws.receive_json()
        assert msg["type"] == "update" and msg["events"][0]["kind"] == "join"
        ws.send_text("ping")
        assert ws.receive_json()["type"] == "pong"


def test_legacy_line_and_dongle_boot(client):
    c = client
    evs = feed(c, '{"type":"boot","fw":"swaptx-sniffer 1.0","ch":1}', "Received message from: 00:00:00:00:00:04 - 36,79,3,0,42")
    assert evs[0]["kind"] == "dongle" and evs[1]["kind"] == "join"
    d = c.get("/api/dongle").json()
    assert d["boots"] == 1 and d["fw"].startswith("swaptx-sniffer")


def test_game_type_change_is_history_not_retroactive(client, tmp_path):
    """Switching Admin -> game type after a Free-for-All must not turn that finished game
    into a team battle on replay (which would carry fake teams into the next game)."""
    c = client
    c.put("/api/settings", json={"rules": {"game_type": "royale"}})
    feed(c, line(H, B, "36,65,1,0,0,3,0,0,0,1,42"), line(mac(1), H, "36,79,0,1,0,3,1,42"), line(mac(2), H, "36,79,1,1,1,3,1,42"),
         line(H, B, "36,65,1,0,0,1,0,0,0,1,42"), line(mac(2), mac(1), "36,68,0,1,1,0,0,1,42"), line(H, B, "36,69,1,0,1,42"))
    assert c.get("/api/state").json()["game"]["kind"] == "royale"
    c.put("/api/settings", json={"rules": {"game_type": "auto"}})
    feed(c, line(H, B, "36,65,1,0,0,3,0,0,1,1,42"), line(mac(1), H, "36,79,0,1,0,3,1,42"), line(mac(2), H, "36,79,1,1,1,3,1,42"),
         line(H, B, "36,65,1,0,0,1,0,0,1,1,42"))
    evs = feed(c, line(mac(2), mac(1), "36,68,0,1,1,0,0,1,42", seq=2))   # new sequence number: not a radio retry of the earlier death
    assert evs[0]["kind"] == "kill"
    st = c.get("/api/state").json()
    assert st["game"]["kind"] == "team" and st["teams"][0]["team_kills"] == 0
    # a full replay (restart) reproduces the same answer because the type change is a frame
    hub2 = Hub(str(tmp_path / "s.db"), None, 115200, simulate=False)
    hub2.replay()
    s2 = hub2.state.snapshot()
    assert s2["game"]["kind"] == "team" and s2["teams"][0]["team_kills"] == 0 and hub2.state.games[-1]["kind"] == "royale"
    hub2.stop()


def test_team_names_api_and_persistence(client, tmp_path):
    c = client
    r = c.put("/api/teams/0", json={"name": "Sharks"})
    assert r.status_code == 200 and r.json()["custom"] is True
    assert c.put("/api/teams/7", json={"name": "x"}).status_code == 400
    teams = c.get("/api/teams").json()
    assert teams[0]["name"] == "Sharks" and teams[1]["name"] == "Blue" and teams[1]["custom"] is False
    st = c.get("/api/state").json()
    assert st["team_names"]["0"] == "Sharks"
    hub2 = Hub(str(tmp_path / "s.db"), None, 115200, simulate=False)
    hub2.replay()
    assert hub2.state.tname(0) == "Sharks"
    hub2.stop()


def test_simulation_mode_starts_from_a_clean_history(tmp_path):
    from swaptx.store import Store
    st = Store(tmp_path / "sim.db")
    st.insert_frame(Frame(ts=1.0, src=mac(1), dst=H, txt="36,79,0,0,42"))
    st.upsert_game({"id": "g1", "started_at": 1.0, "ended_at": None, "status": "live", "mode": 0, "summary": None})
    st.set_player(1, name="Alex")
    st.close()
    hub = Hub(str(tmp_path / "sim.db"), None, 115200, simulate=True, sim_opts={"players": 4, "loop": False})
    hub.simulate = True
    app = create_app(hub)
    with TestClient(app):
        # the old world is gone (the simulator thread may already be writing its own fresh frames)
        assert not any(f.ts == 1.0 for f in hub.store.frames_since(ts=0))
        assert hub.store.game("g1") is None
        assert hub.state.names == {1: "Alex"}
