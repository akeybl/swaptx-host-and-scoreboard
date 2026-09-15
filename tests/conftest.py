import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

H = "00:00:00:00:00:ff"
B = "ff:ff:ff:ff:ff:ff"


def mac(n: int) -> str:
    return f"00:00:00:00:00:{n:02x}"


def line(src, dst, txt, rssi=-55, seq=1):
    return json.dumps({"type": "frame", "ms": 1, "src": src, "dst": dst, "rssi": rssi, "ch": 1, "seq": seq, "txt": txt,
                       "hex": txt.encode().hex()})
