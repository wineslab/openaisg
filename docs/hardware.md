# Hardware notes

The adapter, the antenna, and everything the device told us that is not
in the specs.

## The bench

| | |
|---|---|
| Modem | Andrew / CommScope **ATC200-LITE-USB** |
| USB ID | `0403:f448` (FTDI), 12 Mbit/s |
| Driver | `ftdi_sio` -> `/dev/ttyUSB0` (188,0, `root:dialout` 0660) |
| Actuator | **RET21-AS155D**, hw 2.00, sw 2.6.6 |
| Antenna | **Alpha Wireless AW3161-E-F-V2** |
| Host | single-node OpenShift (RHCOS, kernel 5.14 / el9) |

No D2XX or vendor driver is needed -- the adapter is a plain FTDI UART on
Linux, which is the whole reason a native implementation is possible.

**RHCOS ships no `lsusb`.** Enumerate from sysfs instead:

```bash
oc debug node/<node> -- chroot /host sh -c \
  'for d in /sys/bus/usb/devices/*/; do [ -f "$d/idVendor" ] || continue;
   printf "%s:%s %s\n" "$(cat $d/idVendor)" "$(cat $d/idProduct)" \
   "$(cat $d/product 2>/dev/null)"; done'
```

## Confirmed on the wire

- Device data fields 0x01-0x0B exist, 0x0C+ answer `FAIL`. 0x01 is the antenna
  model, 0x02 its serial, 0x04 beamwidth (65 deg x3 bands), 0x05 gain
  (18.0 dBi x3), 0x07/0x06 min/max tilt (0.0/10.0 deg, 0.1 deg steps).
- `AlarmSubscribe` (0x12) is supported: rc 0x00.
- The actuator is **lenient about sequence numbers** -- it answered an I-frame
  with N(S)=2 while its own N(R) was 1. Do not use it to validate the link
  layer; use the fake secondary.
- It does not answer the broadcast scan once addressed (TS 25.462 4.8.4), and
  appears to drop its address on link loss. DISC releases it immediately.
- The FTDI adapter can wedge at the USB level: every open then fails while the
  device node still exists, with `ftdi_sio ttyUSB0: failed to set flow
  control: -71` in the host log. Recover with a driver rebind on the node:

      # the interface name is whatever ftdi_sio has bound (<bus>-<port>:1.0)
      IFACE=$(basename "$(ls -d /sys/bus/usb/drivers/ftdi_sio/*:* | head -1)")
      echo -n "$IFACE" > /sys/bus/usb/drivers/ftdi_sio/unbind
      echo -n "$IFACE" > /sys/bus/usb/drivers/ftdi_sio/bind

## Still unconfirmed

- The ATC200-LITE-USB powers the RET from its own supply; DTR is asserted on
  open (`--no-dtr` to disable) since ATC Lite manipulates DTR via FT_SetDTR.
- Firmware download (0x40–0x42) and Andrew vendor-specific procedures
  (0x90+) are not implemented; capture them with the sniffer if needed.
