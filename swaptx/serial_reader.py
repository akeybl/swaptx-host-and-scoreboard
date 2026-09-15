"""Background serial reader for the ESP32 dongle with auto-detect and reconnect.

Runs in a thread; every line is handed to ``on_line(line, ts)`` and connection
changes to ``on_status(connected, port)``. Both callbacks are invoked from the
reader thread - the server marshals them onto the asyncio loop.
"""
from __future__ import annotations

import glob
import os
import threading
import time
from typing import Callable, Optional

try:
    import serial  # pyserial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover - pyserial is in requirements
    serial = None
    list_ports = None

USB_PATTERNS = ["/dev/cu.usbserial*", "/dev/cu.SLAB_USBtoUART*", "/dev/cu.wchusbserial*",
                "/dev/cu.usbmodem*", "/dev/ttyUSB*", "/dev/ttyACM*"]


USB_HINTS = ("cp210", "ch340", "ch9102", "silicon labs", "usb serial", "usb-serial", "uart", "wch", "ftdi", "esp32")
EXCLUDE = ("bluetooth", "debug-console", "debug_console", "wlan", "console")


def _score(device: str, description: str, vid) -> int:
    dev, desc = device.lower(), description.lower()
    if any(x in dev for x in EXCLUDE) or any(x in desc for x in EXCLUDE):
        return -1
    score = 0
    if vid is not None:
        score += 2                      # a real USB device
    if any(k in desc for k in USB_HINTS):
        score += 2
    if any(k in dev for k in ("usbserial", "slab_usbtouart", "wchusbserial", "usbmodem", "ttyusb", "ttyacm")):
        score += 1
    return score


def candidate_ports(comports=None) -> list[str]:
    """USB serial ports that look like an ESP32 dev board, best first.

    A port qualifies when it has a USB vendor id, a USB-serial chip in its description,
    or a classic usbserial/usbmodem device name; Bluetooth and system console devices
    are never returned.
    """
    if comports is None:
        comports = list_ports.comports() if list_ports is not None else []
    found: list[tuple[int, str]] = []
    for p in comports:
        dev = p.device
        desc = f"{getattr(p, 'description', '') or ''} {getattr(p, 'manufacturer', '') or ''}"
        sc = _score(dev, desc, getattr(p, "vid", None))
        if sc > 0:
            found.append((sc, dev))
    found.sort(key=lambda t: (-t[0], t[1]))
    out = [d for _, d in found]
    for pat in USB_PATTERNS:
        for dev in sorted(glob.glob(pat)):
            if dev not in out and _score(dev, "", None) > 0:
                out.append(dev)
    return out


class SerialReader(threading.Thread):
    def __init__(self, on_line: Callable[[str, float], None], on_status: Callable[[bool, Optional[str]], None],
                 port: str | None = None, baud: int = 115200, reconnect_s: float = 2.0,
                 capture_dir: str | None = "captures"):
        super().__init__(name="swaptx-serial", daemon=True)
        self.on_line = on_line
        self.on_status = on_status
        self.fixed_port = port
        self.baud = baud
        self.reconnect_s = reconnect_s
        # Every raw line the dongle prints is also appended to captures/<start>.jsonl (one
        # "<unix ts>\t<line>" per row): the complete, unfiltered record of a mapping session.
        self.capture_dir = capture_dir
        self.capture_path: Optional[str] = None
        self._cap = None
        self._stop = threading.Event()
        self._ser = None
        self._tx_lock = threading.Lock()
        self.port: Optional[str] = None
        self.connected = False
        self.last_error: Optional[str] = None

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._ser:
                self._ser.close()
        except Exception:
            pass

    def write(self, line: str) -> bool:
        """Send a command line to the dongle (e.g. 'chan,6'). Never sends radio traffic."""
        with self._tx_lock:
            if not self._ser or not self.connected:
                return False
            try:
                self._ser.write((line.rstrip("\r\n") + "\n").encode("ascii", "replace"))
                return True
            except Exception as e:  # pragma: no cover
                self.last_error = str(e)
                return False

    def run(self) -> None:  # pragma: no cover - exercised with hardware
        if serial is None:
            self.last_error = "pyserial not installed"
            return
        while not self._stop.is_set():
            port = self.fixed_port
            if not port:
                cands = candidate_ports()
                port = cands[0] if cands else None
            if not port:
                self.last_error = "no USB serial device found"
                self._stop.wait(self.reconnect_s)
                continue
            try:
                # exclusive: a second process (a scratch server, a monitor) must fail to open the
                # port rather than silently share it, which garbles both readers' streams
                self._ser = serial.Serial(port, self.baud, timeout=1, exclusive=True)
                # Opening the port toggles DTR/RTS on most dev boards, which resets the ESP32.
                # That is fine: we get a fresh boot banner and the firmware re-locks its channel.
                self.port = port
                self.connected = True
                self.last_error = None
                self._open_capture()
                self.on_status(True, port)
                buf = b""
                while not self._stop.is_set():
                    chunk = self._ser.read(256)
                    if not chunk:
                        continue
                    buf += chunk
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        line = raw.decode("utf-8", "replace")
                        if line.strip():
                            ts = time.time()
                            self._capture(line, ts)
                            self.on_line(line, ts)
                    if len(buf) > 4096:  # runaway line without newline
                        buf = b""
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
            finally:
                was = self.connected
                self.connected = False
                try:
                    if self._ser:
                        self._ser.close()
                except Exception:
                    pass
                self._ser = None
                self._close_capture()
                if was:
                    self.on_status(False, port)
            self._stop.wait(self.reconnect_s)

    # ---------------------------------------------------------------- capture log
    def _open_capture(self) -> None:
        if not self.capture_dir:
            return
        try:
            os.makedirs(self.capture_dir, exist_ok=True)
            self.capture_path = os.path.join(self.capture_dir, time.strftime("%Y%m%d-%H%M%S") + ".jsonl")
            self._cap = open(self.capture_path, "a", encoding="utf-8")
        except Exception as e:  # pragma: no cover
            self.last_error = f"capture: {e}"
            self._cap = None

    def _capture(self, line: str, ts: float) -> None:
        if self._cap is None:
            return
        try:
            self._cap.write(f"{ts:.3f}\t{line.rstrip()}\n")
            self._cap.flush()
        except Exception:  # pragma: no cover
            pass

    def _close_capture(self) -> None:
        try:
            if self._cap:
                self._cap.close()
        except Exception:
            pass
        self._cap = None
