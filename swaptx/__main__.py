"""CLI: python -m swaptx [--simulate] [--port /dev/cu.usbserial-XXXX] [--http-port 8000]"""
from __future__ import annotations

import argparse
import logging
import socket

import uvicorn

from .server import Hub, create_app


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="swaptx", description="SWAPTX Evolver listen-only scoreboard")
    ap.add_argument("--port", help="serial port of the dongle (auto-detected if omitted)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--db", default="data/swaptx.db", help="SQLite database path")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--http-port", type=int, default=8000)
    ap.add_argument("--simulate", action="store_true", help="run a fake game instead of reading the dongle")
    ap.add_argument("--no-dongle", action="store_true",
                    help="never open a serial port (for a second, throwaway server: layout checks, tests)")
    ap.add_argument("--sim-players", type=int, default=8)
    ap.add_argument("--sim-mode", type=int, default=3, choices=(0, 1, 2, 3), help="0 team battle, 1 battle royale, 2 free for all, 3 cycle")
    ap.add_argument("--sim-speed", type=float, default=1.0)
    ap.add_argument("--sim-seed", type=int, default=None)
    ap.add_argument("--replay-hours", type=float, default=24.0, help="how far back to rebuild state on startup")
    ap.add_argument("--log-level", default="info")
    args = ap.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    hub = Hub(args.db, "none" if args.no_dongle or args.port == "none" else args.port, args.baud, args.simulate,
              sim_opts={"players": args.sim_players, "mode": args.sim_mode, "speed": args.sim_speed,
                        "seed": args.sim_seed}, replay_hours=args.replay_hours)
    app = create_app(hub)
    ip = lan_ip()
    print(f"\n  SWAPTX scoreboard\n  wall view : http://{ip}:{args.http_port}/\n  admin     : http://{ip}:{args.http_port}/admin\n"
          f"  mode      : {'SIMULATION' if args.simulate else 'listening on ' + (args.port or 'auto-detected USB serial')}\n")
    uvicorn.run(app, host=args.host, port=args.http_port, log_level=args.log_level)


if __name__ == "__main__":
    main()
