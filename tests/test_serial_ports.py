from types import SimpleNamespace as NS
from swaptx.serial_reader import candidate_ports


def test_candidate_ports_prefers_usb_serial_and_skips_system_devices():
    ports = [
        NS(device="/dev/cu.debug-console", description="n/a", manufacturer=None, vid=None),
        NS(device="/dev/cu.Bluetooth-Incoming-Port", description="n/a", manufacturer=None, vid=None),
        NS(device="/dev/cu.usbserial-0001", description="CP2102N USB to UART Bridge Controller", manufacturer="Silicon Labs", vid=0x10C4),
        NS(device="/dev/cu.usbmodem14101", description="USB JTAG/serial debug unit", manufacturer="Espressif", vid=0x303A),
        NS(device="/dev/cu.wlan-debug", description="n/a", manufacturer=None, vid=None),
    ]
    out = candidate_ports(ports)
    assert out[0] == "/dev/cu.usbserial-0001"
    assert "/dev/cu.usbmodem14101" in out
    assert not any("debug" in p or "Bluetooth" in p or "wlan" in p for p in out)
    assert candidate_ports([]) == [] or all("usb" in p.lower() or "tty" in p for p in candidate_ports([]))
