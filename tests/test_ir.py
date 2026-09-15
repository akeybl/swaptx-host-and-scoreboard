from swaptx.ir import encode_bits, decode_bits, decode_pulses


def test_ir_roundtrip():
    bits = encode_bits(13, 6, 2, 200, critical=True)
    tag = decode_bits(bits)
    assert tag.bullet_type == 13 and tag.player == 6 and tag.player_number == 7
    assert tag.team == 2 and tag.damage == 200 and tag.critical and tag.parity_ok
    assert tag.bullet_label == "Medic / checkpoint"


def test_ir_pulses_with_sync():
    bits = encode_bits(0, 15, 3, 25)
    pulses = [2500] + [1000 if b else 500 for b in bits]
    tag = decode_pulses(pulses)
    assert tag.player == 15 and tag.team == 3 and tag.damage == 25 and tag.bullet_type == 0
    assert decode_pulses([]) is None and decode_bits([1, 0]) is None
