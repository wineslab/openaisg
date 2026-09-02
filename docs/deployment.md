# Deploying on OpenShift

Namespace **`aisg`**, on whichever node the adapter is plugged into. Nothing
hardcodes a node name — NFD labels the node and the manifests select on that
label (see below). Where these commands say `<node>`, find it with:

```bash
oc get node -l feature.node.kubernetes.io/usb-ff_0403_f448.present=true
```

## Why a device plugin, not a privileged pod

Three separate things gate a USB device in a container, and only the third is
commonly missed:

| Gate | Satisfied by |
|---|---|
| Scheduling | `nodeSelector` on the NFD label |
| Visibility | `hostPath`, or a device plugin |
| **Device cgroup permission** | `privileged: true`, **or** a device plugin |

A `hostPath` mount of `/dev/ttyUSB0` into an unprivileged pod yields a device
node that returns `EPERM`. Linux capabilities do not help — it is a cgroup
allowlist, not a capability check, and only two things write to it:
`privileged: true`, or the kubelet Device Plugin API.

The plugin also sidesteps SELinux: it makes runc **mknod the node inside the
container's private `/dev`**, so it carries the pod's own MCS label rather than
the host's `tty_device_t`. Verified from inside the running pod:

```
uid=1000910000 gid=0(root) groups=0(root),18(dialout),1000910000
crw-rw-rw-. root dialout system_u:object_r:container_file_t:s0:c20,c30 188,0 /dev/ttyUSB0
```

So the service runs `runAsNonRoot`, `allowPrivilegeEscalation: false`,
`capabilities: drop ["ALL"]`, `seccompProfile: RuntimeDefault` under
`restricted-v2`. Only the plugin DaemonSet is privileged.

## Scheduling comes free from NFD

Node Feature Discovery already fingerprints the adapter (USB device class
`ff`, vendor-specific), so there is no need to hardcode a node name:

```
feature.node.kubernetes.io/usb-ff_0403_f448.present=true
```

## Order of operations

```bash
# 1. namespace, SA and the device plugin
oc apply -f deploy/device-plugin.yaml
oc adm policy add-scc-to-user privileged -z generic-device-plugin -n aisg

# 2. confirm the node advertises the resource
oc get node <node> -o jsonpath='{.status.allocatable.devic\.es/aisg}{"\n"}'   # -> 1

# 3. build the image (binary build; context is the repo root)
oc -n aisg new-build --binary --strategy=docker --name aisgctl   # first time only
oc -n aisg start-build aisgctl --from-dir=. -F

# 4. the service, Service and Route
oc apply -f deploy/aisgd.yaml
oc get route aisgd -n aisg -o jsonpath='{.spec.host}{"\n"}'
```

## Gotchas that cost real time

- **The resource domain is `devic.es`, not `squat.ai`.** The upstream
  generic-device-plugin README still says `squat.ai`; the current image
  defaults to `devic.es`. A pod requesting the wrong domain stays `Pending`
  forever with no useful message.
- **`allocatable` is 1, so `strategy: Recreate` is mandatory.** A
  `RollingUpdate` deadlocks: the new pod waits for a device the old pod still
  holds. For the same reason only one Deployment may exist — the old
  `aisgctl` sleep-infinity Deployment must be scaled to 0.
- **The `docker` build strategy demands the filename `Dockerfile`.** A
  `Containerfile` fails with `ManageDockerfileFailed` /
  `open /tmp/build/inputs/Dockerfile: no such file or directory`.
- **The Route needs both annotations.** The default HAProxy timeout is 30 s,
  which silently kills the event WebSocket:
  `haproxy.router.openshift.io/websocket-ports: "8080"` and
  `haproxy.router.openshift.io/timeout: 1h`.
- **`priorityClassName: system-node-critical` outside `kube-system`** is
  rejected by admission.
- **`terminationGracePeriodSeconds` must exceed the longest operation**
  (calibrate, 150 s) or a rollout abandons the actuator mid-travel.
- **RHCOS has no `lsusb`** — read `/sys/bus/usb/devices/*` via `oc debug node`.

## Sharing the port with the CLI

`AisgLink` takes an advisory `flock` (`exclusive=True`) by default, so the CLI
and the service cannot silently interleave HDLC onto the same half-duplex bus.
Contention becomes an error, not corruption:

```
error: /dev/ttyUSB0 is locked by another process (the aisgd service holds it
       while its AISG session is up)
```

In the default `duty-cycled` monitor mode the port is closed most of the time,
so `aisgctl` usually just works. To *guarantee* it, take a lease — an idle
timer alone is a race the operator loses, because any request or monitor tick
reopens the port:

```bash
HOST=$(oc get route aisgd -n aisg -o jsonpath='{.spec.host}')
curl -sk -X POST "https://$HOST/api/v1/lease" \
     -H 'content-type: application/json' -d '{"seconds":300}'
oc -n aisg exec deploy/aisgd -- aisgctl -p /dev/ttyUSB0 tilt
curl -sk -X DELETE "https://$HOST/api/v1/lease"
```

## Recovering a wedged adapter

The FTDI can lock up at the USB level: every open fails while the device node
still exists, and the host logs `ftdi_sio ttyUSB0: failed to set flow control:
-71` (EPROTO). The service reports this as `PortLost` and backs off rather
than retrying pointlessly. Rebind the driver — no bench visit needed:

```bash
oc debug node/<node> -- chroot /host sh -c \
  'IFACE=$(basename "$(ls -d /sys/bus/usb/drivers/ftdi_sio/*:* | head -1)");
   echo -n "$IFACE" > /sys/bus/usb/drivers/ftdi_sio/unbind;
   sleep 2;
   echo -n "$IFACE" > /sys/bus/usb/drivers/ftdi_sio/bind'
```
