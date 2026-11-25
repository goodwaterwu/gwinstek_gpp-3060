# GPP-3060 remote front panel

A PySide6 desktop front panel with three channel cards, serial-device selection,
voltage/current editing, output ON/OFF controls, confirmed settings, and live
voltage/current/power readings. Default port: `COM1` on Windows,
`/dev/ttyUSB0` on Linux; baud: `115200`.

## Setup and launch

Requires Windows or Linux, Python 3.10+, a graphical desktop, and access to the
serial port. On Windows, install the USB serial driver required by your device.
Run the following commands from the project directory.

Linux:

Use the system Python explicitly if your PATH points at a cross-toolchain Python.

```sh
/usr/bin/python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python gpp3060_gui.py
```

Windows (PowerShell or Command Prompt):

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe gpp3060_gui.py
```

Choose the port and baud rate, then click **Connect**. On Windows, **Refresh
ports** lists `COM1` through `COM20` in numeric order, regardless of whether
those ports exist. On Linux, it includes pyserial-enumerated devices and all
`/dev/tty*` paths. You can also type a port such as `COM21` or
`/dev/serial/by-id/...`; refreshing preserves the selection.
Entries are candidates, not verified instruments;
connection checks `*IDN?` for GPP-3060 before enabling controls.

Each card separates editable values from **Device settings**, which come from
readback. Click **Apply V / A** to send both setpoints. Click **Turn output ON/OFF**
to switch that channel using its existing device settings; editing fields alone
does not apply them. Applying settings to an enabled channel affects its output.
CH1/CH2 allow 0–30 V and 0–6 A. CH3 selects 1.8, 2.5, 3.3, or 5 V and 0–5 A,
following the provided reference controller.

State/settings are polled every second, including disabled channels, to detect
front-panel changes. Enabled channels additionally query measured V/A/W; disabled
channels show dashes. Polls and commands are serialized in a worker thread.
Slow instrument responses can extend the refresh interval; polls do not queue up.
A communication/protocol error disconnects and clears readings. Reconnect to read
actual state; a failed multi-command write may have applied some settings.

Connecting does not program setpoints or switch outputs. Disconnecting, closing,
or encountering an error does **not** turn physical outputs off. Switch outputs
off explicitly if desired before disconnecting. Do not run another serial
controller on the same device concurrently. If opening fails with permission
denied, check the device's permissions and your host's serial-access group.

## Demo mode

```sh
.venv/bin/python gpp3060_gui.py --demo
```

Click **Connect** to simulate a 10-ohm load per channel with current limiting.
The identity label explicitly says DEMO. No serial port is opened. Demo state
resets on reconnect; simulated values are not measurements of hardware.

## Verification

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m unittest -v
.venv/bin/python -m black --check --line-length 79 gpp3060.py gpp3060_gui.py test_gpp3060.py
.venv/bin/python -m flake8 gpp3060.py gpp3060_gui.py test_gpp3060.py
```

Tests use a fake serial transport and offscreen Qt; they do not energize hardware.
Physical-device compatibility and desktop rendering must also be checked on the
actual system. Protocol commands and limits are derived from
`/opt/scripts/gpp3060_ctrl.py`; that file is not modified or needed at runtime.
The background worker follows Qt's documented
[QThread threading model](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QThread.html).
