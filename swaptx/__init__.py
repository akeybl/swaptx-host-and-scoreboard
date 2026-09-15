"""SWAPTX Evolver listen-only scoreboard.

Pipeline: ESP32 sniffer dongle (USB serial) -> frames.parse_line -> protocol.decode
-> state.GameState.apply -> FastAPI/WebSocket -> wall + admin web UI.
Every raw frame is persisted; all derived state is recomputed from frames, so
re-labelling a protocol field or renaming a player rewrites history consistently.
"""
__version__ = "0.1.0"
