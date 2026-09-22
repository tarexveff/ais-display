# ais-display

Containerized AIS receiver and live map viewer for RHEL. Receives AIS radio
signals via an RTL-SDR USB dongle, decodes NMEA sentences in real time, and
serves a web UI with a live vessel map and data table. ytfythgjgj

---

## Prerequisites

### 1. Podman

Podman must be installed on the RHEL host:

```bash
sudo dnf install -y podman
```

### 2. RTL-SDR USB dongle

Plug the RTL-SDR dongle into a USB port before building or running the container.

### 3. Blacklist the DVB kernel module

On RHEL, the kernel module `dvb_usb_rtl28xxu` auto-loads and claims the RTL-SDR
dongle before `rtl_ais` can access it. Blacklist it permanently and unload it
from the running kernel:

```bash
echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/rtlsdr.conf
sudo modprobe -r dvb_usb_rtl28xxu  # unload if already loaded; ignore "not found" errors
```

The blacklist takes effect automatically on every subsequent boot.

### 4. udev rule for non-root USB access

Allow non-root processes (including rootless Podman containers) to open the
dongle's USB device node:

```bash
echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", MODE="0666", GROUP="plugdev"' \
    | sudo tee /etc/udev/rules.d/rtlsdr.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

> **Note**: The vendor ID `0bda` covers all Realtek RTL28xx-based dongles. Run
> `lsusb` to confirm your dongle is listed with that vendor ID.

---

## Build

From the `ais-display/` directory:

```bash
podman build -t ais-display .
```

The build compiles `librtlsdr` and `rtl_ais` from source in a UBI 9 builder
stage, then copies the resulting binaries and libraries into the final
`hi/python` image alongside the Python application. Expect the first build to
take a few minutes; subsequent builds use the layer cache.

---

## Run

### Basic (auto-gain, no forwarding)

```bash
podman run -d \
  --name ais-display \
  --device /dev/bus/usb \
  -p 8080:8080 \
  -e RTL_GAIN=0 \
  -e RTL_PPM=0 \
  ais-display
```

### With NMEA forwarding to a remote receiver

Forward every raw NMEA sentence to one or more external tools (e.g. OpenCPN,
AISDispatcher, another ais-display instance) over UDP:

```bash
podman run -d \
  --name ais-display \
  --device /dev/bus/usb \
  -p 8080:8080 \
  -e FORWARD_TARGETS=192.168.1.50:10110 \
  ais-display
```

Multiple targets are separated by commas:

```bash
-e FORWARD_TARGETS=192.168.1.50:10110,10.0.0.5:10110
```

---

## Access the UI

Open a browser and navigate to:

```
http://localhost:8080
```

The map is centred on Troia, Portugal. Vessel markers and track lines update
automatically in real time via WebSocket.

---

## Logs

Stream container logs (includes `rtl_ais` stderr at DEBUG level):

```bash
podman logs -f ais-display
```

---

## Stop and remove

```bash
podman stop ais-display && podman rm ais-display
```

---

## Environment variables

| Variable           | Default  | Description                                                                                      |
|--------------------|----------|--------------------------------------------------------------------------------------------------|
| `PORT`             | `8080`   | Web server listen port inside the container.                                                     |
| `RTL_GAIN`         | `0`      | RTL-SDR tuner gain in tenths of dB (e.g. `496` = 49.6 dB). `0` enables automatic gain control. |
| `RTL_PPM`          | `0`      | Frequency correction in parts per million. Use `rtl_test -p` on the host to measure this.       |
| `RTL_UDP_PORT`     | `10110`  | Local UDP port on which `rtl_ais` emits NMEA sentences (consumed internally by the Python app). |
| `FORWARD_TARGETS`  | _(unset)_ | Comma-separated `host:port` pairs for UDP NMEA forwarding. Empty = forwarding disabled.         |

---

## Troubleshooting

### RTL-SDR device not found / `rtl_ais` cannot open device

1. Confirm the dongle is visible on the host: `lsusb | grep Realtek`
2. Verify the kernel module is not loaded: `lsmod | grep dvb_usb_rtl28xxu`
   — if it appears, run `sudo modprobe -r dvb_usb_rtl28xxu` and ensure
   `/etc/modprobe.d/rtlsdr.conf` contains `blacklist dvb_usb_rtl28xxu`.
3. Check the udev rule is active: `ls -l /dev/bus/usb/...` — the device node
   should have permissions `crw-rw-rw-`.
4. Re-run `sudo udevadm control --reload-rules && sudo udevadm trigger` after
   any udev rule change, then replug the dongle.

### `rtl_ais` exits immediately / no vessels appear

- Try setting a manual gain as a starting point:
  ```bash
  -e RTL_GAIN=496
  ```
  `496` corresponds to 49.6 dB, a good default for most dongles and antennas.
- Ensure the antenna is connected to the dongle.

### Frequency drift / no decodes despite signal present

Measure the PPM offset of your specific dongle with `rtl_test -p` running on
the host (outside the container) for several minutes, then pass the measured
value:

```bash
-e RTL_PPM=<measured_value>
```

### Port already in use

Change the host-side port mapping; the container port stays `8080`:

```bash
-p 9090:8080
```

Then access the UI at `http://localhost:9090`.
