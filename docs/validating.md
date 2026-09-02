# Validating against the vendor tool

The vendor installer (`ATCLite988Setup.exe`) is proprietary and is kept
outside this repo; obtain it separately for byte-for-byte comparison.

1. `python3 utils/sniff_bridge.py /dev/ttyUSB0` — prints a pty path
2. `ln -sf <pty> "$WINEPREFIX/dosdevices/com1"`
3. Run ATC Lite (Wine) in serial mode and do a scan/get-info/set-tilt
4. Compare `aisg_capture.log` with `cli/aisgctl -d` output for the same actions
