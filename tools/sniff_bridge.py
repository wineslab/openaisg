#!/usr/bin/env python3
"""Serial sniffer bridge: lets ATC Lite (under Wine) talk to the real adapter
while logging every byte in both directions, decoded as AISG HDLC frames.

Creates a pty; point Wine's COM1 at it:
    ln -sf <printed pty> "$WINEPREFIX/dosdevices/com1"
then run ATC Lite in serial mode. Traffic is bridged to the real device
(default /dev/ttyUSB0) and logged to aisg_capture.log.

Usage: sniff_bridge.py [real_port] [logfile]
"""

import os
import pty
import select
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import serial
from aisg import hdlc


def decode(data: bytes) -> str:
    try:
        out = []
        for addr, ctrl, pl in hdlc.parse_frames(data):
            kind = "I" if ctrl & 1 == 0 else (
                "RR/S" if ctrl & 0x0F in (0x01, 0x05, 0x09) else f"U:{ctrl:02X}")
            out.append(f"[addr={addr:02X} ctrl={ctrl:02X} {kind} pl={pl.hex(' ')}]")
        return " ".join(out) if out else ""
    except hdlc.FrameError as e:
        return f"[{e}]"


def main():
    real_port = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"
    logfile = sys.argv[2] if len(sys.argv) > 2 else "aisg_capture.log"

    master, slave = pty.openpty()
    pty_name = os.ttyname(slave)
    real = serial.Serial(real_port, 9600, timeout=0)

    print(f"pty for Wine: {pty_name}")
    print(f'run: ln -sf {pty_name} "$WINEPREFIX/dosdevices/com1"')
    print(f"bridging to {real_port}, logging to {logfile}")

    with open(logfile, "a") as log:
        log.write(f"--- capture started {time.ctime()} ---\n")
        pending = {"app": bytearray(), "dev": bytearray()}
        last = {"app": 0.0, "dev": 0.0}

        def flush(side):
            if pending[side]:
                data = bytes(pending[side])
                line = (f"{time.monotonic():.3f} {side:>3} "
                        f"{data.hex(' ')}  {decode(data)}\n")
                log.write(line)
                log.flush()
                print(line, end="")
                pending[side].clear()

        while True:
            r, _, _ = select.select([master, real.fileno()], [], [], 0.02)
            now = time.monotonic()
            if master in r:
                data = os.read(master, 4096)
                real.write(data)
                pending["app"] += data
                last["app"] = now
            if real.fileno() in r:
                data = real.read(4096)
                if data:
                    os.write(master, data)
                    pending["dev"] += data
                    last["dev"] = now
            for side in ("app", "dev"):
                if pending[side] and now - last[side] > 0.02:
                    flush(side)


if __name__ == "__main__":
    main()
