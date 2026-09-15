from swaptx.frames import parse_line, Frame, DongleMessage, mac_player, mac_role
from conftest import line, mac, H, B


def test_parse_sniffer_json():
    fr = parse_line(line(mac(3), B, "36,101,2,1,3,42", rssi=-61, seq=412), ts=100.0)
    assert isinstance(fr, Frame)
    assert fr.src == mac(3) and fr.dst == B and fr.txt == "36,101,2,1,3,42"
    assert fr.rssi == -61 and fr.seq == 412 and fr.ts == 100.0
    assert fr.src_player == 3 and fr.dst_player is None and fr.is_broadcast


def test_parse_hex_only_payload():
    fr = parse_line('{"type":"frame","src":"00:00:00:00:00:01","dst":"00:00:00:00:00:ff","hex":"33362c37392c302c302c3432"}')
    assert fr.txt == "36,79,0,0,42" and fr.dst_player is None and mac_role(fr.dst) == "host"


def test_parse_legacy_transceiver_line():
    fr = parse_line("Received message from: 00:00:00:00:00:07 - 36,79,6,5,42")
    assert isinstance(fr, Frame) and fr.src_player == 7 and fr.dst is None and fr.txt == "36,79,6,5,42"


def test_parse_status_and_noise():
    m = parse_line('{"type":"boot","fw":"swaptx-sniffer 1.0","ch":1}')
    assert isinstance(m, DongleMessage) and m.kind == "boot" and m.data["ch"] == 1
    assert parse_line("") is None
    assert parse_line("Broadcast message success") is None
    assert parse_line("{not json") is None
    assert isinstance(parse_line("ESP-NOW Init Success"), DongleMessage)


def test_mac_helpers():
    assert mac_player(mac(1)) == 1 and mac_player(mac(16)) == 16 and mac_player(mac(17)) is None
    assert mac_player(H) is None and mac_role(H) == "host" and mac_role(B) == "broadcast"
    assert mac_role("aa:bb:cc:dd:ee:ff") == "other" and mac_role(None) == "unknown"
