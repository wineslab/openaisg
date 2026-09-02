#!/usr/bin/env python3
"""Preflight checks before talking to a real AISG bus.

Verifies the port opens, checks line settings, and does a low-level XID
device-scan probe with a raw hex dump — so if nothing answers you can tell
whether it's a wiring/power problem vs. a protocol mismatch.

Usage: python3 preflight.py [/dev/ttyUSB0]
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

port = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"

try:
    import serial
except ImportError:
    sys.exit("pyserial not installed: pip install pyserial")

from openaisg import hdlc

print(f"[1] opening {port} @ 9600 8N1 ...")
try:
    ser = serial.Serial(port, 9600, bytesize=8, parity="N", stopbits=1, timeout=0.2)
except Exception as e:
    sys.exit(f"    FAILED: {e}\n"
             "    -> check the device node exists and you have rw (dialout group).")
print(f"    OK: {ser.name}, dtr={ser.dtr}, rts={ser.rts}")

# AISG modems power/enable the bus via control lines on some adapters.
ser.dtr = True
ser.rts = False
time.sleep(0.2)

# Broadcast device scan: PL=0 unique-id + PL=0 bitmask matches every
# unaddressed secondary (TS 25.462 4.8.4).
payload = hdlc.xid_encode([(hdlc.PI_UNIQUE_ID, b""), (hdlc.PI_BITMASK, b"")])
frame = hdlc.build_frame(hdlc.ADDR_BROADCAST, hdlc.CTRL_XID | hdlc.PF, payload)

print(f"[2] sending device-scan XID: {frame.hex(' ')}")
for attempt in range(1, 4):
    ser.reset_input_buffer()
    ser.write(frame)
    ser.flush()
    time.sleep(0.6)
    resp = ser.read(512)
    if resp:
        print(f"    attempt {attempt}: got {len(resp)} bytes:\n    {resp.hex(' ')}")
        try:
            for addr, ctrl, pl in hdlc.parse_frames(resp):
                p = hdlc.xid_decode(pl) if pl[:2] == b"\x81\xf0" else {}
                print(f"    -> addr=0x{addr:02X} ctrl=0x{ctrl:02X} params={p}")
        except hdlc.FrameError as e:
            print(f"    (frame decode: {e} — likely a bus collision = several "
                  "devices; that's still a positive signal)")
        break
    print(f"    attempt {attempt}: no response")
else:
    print("    no device answered. Checklist:")
    print("      - is the RET powered (AISG DC feed present)?")
    print("      - RS-485 A/B not swapped? try --no-dtr, or swap RTS/DTR")
    print("      - some adapters need RTS high to enable the transceiver")

ser.close()
print("done.")
