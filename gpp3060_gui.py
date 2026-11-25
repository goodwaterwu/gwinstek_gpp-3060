#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""@brief PySide6 front panel for real and simulated GPP-3060 supplies.

@details The worker exclusively owns serial I/O. A bounded command queue
serializes user changes with polling; Qt signals deliver immutable snapshots.
"""

import argparse
from pathlib import Path
from queue import Empty, Full, Queue
import sys
import time

from PySide6.QtCore import QThread, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from serial.tools import list_ports

from gpp3060 import Device, Snapshot


class Worker(QThread):
    """@brief Serialize commands and one-second polling outside the GUI.

    @details Options are immutable after start. Only the bounded queue and
    interruption API cross threads. Errors end the connection to avoid using
    a desynchronized serial response stream. No automatic writes on startup.
    """

    identity = Signal(str)
    snapshot_ready = Signal(object)
    failed = Signal(str)
    completed = Signal(int)

    def __init__(self, port: str, baud: int, demo: bool) -> None:
        """@brief Store connection options without opening hardware.

        @param port Serial device path.
        @param baud Speed in bits per second.
        @param demo Whether to simulate the instrument.
        """
        super().__init__()
        self.options = (port, baud, demo)
        self.commands: Queue[tuple] = Queue(maxsize=12)

    def run(self) -> None:
        """@brief Open, identify, poll, and process commands until stopped.

        @details Emits errors instead of propagating I/O/data exceptions.
        Polls all output states/settings to detect front-panel changes and
        reads V/A/W for enabled channels. Slow responses extend the interval;
        polls never accumulate. Closing releases the port, preserving outputs.
        """
        device = None
        try:
            device = Device(*self.options)
            self.identity.emit(device.identify())
            next_poll = 0.0
            while not self.isInterruptionRequested():
                if time.monotonic() >= next_poll:
                    started = time.monotonic()
                    for channel in (1, 2, 3):
                        if self.isInterruptionRequested():
                            break
                        self.snapshot_ready.emit(device.snapshot(channel))
                    next_poll = max(started + 1.0, time.monotonic())
                try:
                    command = self.commands.get(timeout=0.05)
                except Empty:
                    continue
                if self.isInterruptionRequested():
                    break
                action, channel, *values = command
                if action == "configure":
                    device.configure(channel, *values)
                elif action == "output":
                    device.output(channel, *values)
                else:
                    raise ValueError(f"Unknown action: {action}")
                self.snapshot_ready.emit(device.snapshot(channel))
                self.completed.emit(channel)
        except (OSError, ValueError, UnicodeError) as exc:
            self.failed.emit(str(exc))
        finally:
            if device is not None:
                try:
                    device.close()
                except OSError as exc:
                    self.failed.emit(f"Closing serial port failed: {exc}")


class ChannelPanel(QGroupBox):
    """@brief Display confirmed settings separately from editable setpoints.

    @details Each panel owns controls for one channel and caches the last
    confirmed output state. Draft entries are initialized once per connection.
    """

    requested = Signal(tuple)

    def __init__(self, channel: int) -> None:
        """@brief Build channel controls using reference-controller limits.

        @param channel Channel number from 1 through 3.
        """
        super().__init__(f"CH{channel}")
        self.channel = channel
        self.enabled = False
        self.initialized = False
        self.busy = False
        layout = QVBoxLayout(self)
        self.state = QLabel("DISCONNECTED")
        self.state.setObjectName("state")
        layout.addWidget(self.state)
        self.readings = QLabel("— V\n— A\n— W")
        self.readings.setObjectName(f"readings{channel}")
        layout.addWidget(self.readings)
        self.confirmed = QLabel("Device settings: —")
        layout.addWidget(self.confirmed)
        form = QFormLayout()
        if channel == 3:
            self.voltage = QComboBox()
            self.voltage.addItems(["1.8", "2.5", "3.3", "5.0"])
        else:
            self.voltage = QDoubleSpinBox()
            self.voltage.setRange(0, 30)
            self.voltage.setDecimals(3)
            self.voltage.setSingleStep(0.1)
        self.current = QDoubleSpinBox()
        self.current.setRange(0, 5 if channel == 3 else 6)
        self.current.setDecimals(4)
        self.current.setSingleStep(0.1)
        form.addRow("Voltage (V)", self.voltage)
        form.addRow("Current limit (A)", self.current)
        layout.addLayout(form)
        self.apply = QPushButton("Apply V / A")
        self.apply.clicked.connect(self.apply_settings)
        layout.addWidget(self.apply)
        self.switch = QPushButton("Turn output ON")
        self.switch.clicked.connect(self.toggle_output)
        layout.addWidget(self.switch)
        self.updated = QLabel("Waiting for device")
        layout.addWidget(self.updated)
        self.setEnabled(False)

    @Slot()
    def apply_settings(self) -> None:
        """@brief Queue draft setpoints without switching outputs."""
        voltage = (
            float(self.voltage.currentText())
            if self.channel == 3
            else self.voltage.value()
        )
        self.requested.emit(
            ("configure", self.channel, voltage, self.current.value())
        )

    @Slot()
    def toggle_output(self) -> None:
        """@brief Request the opposite of the confirmed output state."""
        self.requested.emit(("output", self.channel, not self.enabled))

    def update_snapshot(self, snapshot: Snapshot) -> None:
        """@brief Render confirmed device data without overwriting drafts.

        @param snapshot Fresh state for this panel's channel.
        @details Initializes draft controls only on the first connection read.
        """
        self.enabled = snapshot.enabled
        if not self.initialized:
            if self.channel == 3:
                self.voltage.setCurrentText(f"{snapshot.voltage:.1f}")
            else:
                self.voltage.setValue(snapshot.voltage)
            self.current.setValue(snapshot.current)
            self.initialized = True
        self.state.setText("OUTPUT ON" if snapshot.enabled else "OUTPUT OFF")
        self.switch.setText(
            "Turn output OFF" if snapshot.enabled else "Turn output ON"
        )
        self.confirmed.setText(
            f"Device settings: {snapshot.voltage:.3f} V / "
            f"{snapshot.current:.4f} A"
        )
        if snapshot.measured is None:
            self.readings.setText("— V\n— A\n— W")
        else:
            volts, amps, watts = snapshot.measured
            self.readings.setText(
                f"{volts:.3f} V\n{amps:.4f} A\n{watts:.3f} W"
            )
        self.updated.setText(f"Read at {time.strftime('%H:%M:%S')}")
        self.setEnabled(not self.busy)

    def invalidate(self) -> None:
        """@brief Clear stale state and disable disconnected controls."""
        self.initialized = False
        self.busy = False
        self.setEnabled(False)
        self.state.setText("DISCONNECTED · STATE UNKNOWN")
        self.confirmed.setText("Device settings: —")
        self.readings.setText("— V\n— A\n— W")
        self.updated.setText("No live data")


class MainWindow(QMainWindow):
    """@brief Own the front panel and one asynchronously stopped worker.

    @details Serial selection lists COM1 through COM20 on Windows, or
    enumerated ports and /dev/tty* paths on other systems.
    Closing waits asynchronously for the worker; it never disables outputs.
    """

    def __init__(self, demo: bool = False) -> None:
        """@brief Create an initially disconnected front panel.

        @param demo Initial demo checkbox state; connection remains explicit.
        """
        super().__init__()
        self.worker: Worker | None = None
        self.closing = False
        self.error = ""
        self.setWindowTitle("GW Instek GPP-3060 · Remote Front Panel")
        self.resize(1040, 560)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        title = QLabel("GW INSTEK   /   GPP-3060")
        title.setObjectName("title")
        layout.addWidget(title)
        layout.addWidget(QLabel("PROGRAMMABLE DC POWER SUPPLY · 3 CHANNELS"))
        row = QHBoxLayout()
        self.port = QComboBox()
        self.port.setEditable(True)
        self.port.setMinimumWidth(220)
        self.refresh = QPushButton("Refresh ports")
        self.refresh.clicked.connect(self.refresh_ports)
        self.baud = QComboBox()
        self.baud.addItems(["9600", "19200", "38400", "57600", "115200"])
        self.baud.setCurrentText("115200")
        self.demo = QCheckBox("Demo mode")
        self.demo.setChecked(demo)
        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self.toggle_connection)
        for widget in (self.port, self.refresh, self.baud, self.demo):
            row.addWidget(widget)
        row.addWidget(self.connect_button)
        layout.addLayout(row)
        self.identity = QLabel("Disconnected")
        self.identity.setWordWrap(True)
        layout.addWidget(self.identity)
        panels = QHBoxLayout()
        self.panels = {ch: ChannelPanel(ch) for ch in (1, 2, 3)}
        for panel in self.panels.values():
            panel.requested.connect(self.enqueue)
            panels.addWidget(panel)
        layout.addLayout(panels)
        note = QLabel(
            "Live readings: every 1 s while ON · Apply changes settings; "
            "ON/OFF uses device settings.\n"
            "Disconnecting or closing leaves physical outputs unchanged."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.setStyleSheet(
            "QWidget { background: #17212d; color: #e6edf3; font-size: 14px; }"
            "QGroupBox { border: 1px solid #496078; border-radius: 8px; "
            "margin-top: 14px; padding: 18px 12px 10px; font-weight: bold; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 14px; }"
            "QPushButton, QComboBox, QDoubleSpinBox { background: #293b4e; "
            "padding: 8px; border: 1px solid #52708b; border-radius: 4px; }"
            "QPushButton:hover { background: #38536e; }"
            "QWidget:disabled { color: #768494; }"
            "QLabel#title { font-size: 26px; font-weight: bold; }"
            "QLabel#readings1 { color: #ffd166; font-size: 30px; }"
            "QLabel#readings2 { color: #6ecbff; font-size: 30px; }"
            "QLabel#readings3 { color: #88e0b0; font-size: 30px; }"
        )
        self.refresh_ports()

    @Slot()
    def refresh_ports(self) -> None:
        """@brief Refresh port candidates, retaining the typed selection.

        @details Windows lists COM1 through COM20 numerically and defaults
        to COM1. Other systems scan serial devices and /dev/tty* paths,
        defaulting to /dev/ttyUSB0. Custom selections remain in the list.
        Updates the combo box without opening or verifying any port.
        """
        windows = sys.platform == "win32"
        default_port = "COM1" if windows else "/dev/ttyUSB0"
        selected = self.port.currentText() or default_port
        if windows:
            ports = [f"COM{number}" for number in range(1, 21)]
            if selected not in ports:
                ports.append(selected)
        else:
            devices = {port.device for port in list_ports.comports()}
            devices.update(str(path) for path in Path("/dev").glob("tty*"))
            devices.update((selected, default_port))
            ports = sorted(devices)
        self.port.clear()
        self.port.addItems(ports)
        self.port.setCurrentText(selected)

    @Slot()
    def toggle_connection(self) -> None:
        """@brief Start a worker or request asynchronous disconnection."""
        if self.worker is not None:
            self.worker.requestInterruption()
            self.connect_button.setEnabled(False)
            for panel in self.panels.values():
                panel.setEnabled(False)
            self.statusBar().showMessage("Disconnecting…")
            return
        self.error = ""
        self.worker = Worker(
            self.port.currentText().strip(),
            int(self.baud.currentText()),
            self.demo.isChecked(),
        )
        self.worker.identity.connect(self.identity.setText)
        self.worker.snapshot_ready.connect(self.receive_snapshot)
        self.worker.failed.connect(self.receive_error)
        self.worker.completed.connect(self.command_completed)
        self.worker.finished.connect(self.disconnected)
        for widget in (self.port, self.refresh, self.baud, self.demo):
            widget.setEnabled(False)
        self.connect_button.setText("Disconnect")
        self.statusBar().showMessage("Connecting…")
        self.worker.start()

    @Slot(object)
    def receive_snapshot(self, snapshot: Snapshot) -> None:
        """@brief Deliver a worker snapshot to its panel on the GUI thread.

        @param snapshot Confirmed device state.
        """
        if (
            self.worker is not None
            and not self.worker.isInterruptionRequested()
        ):
            self.panels[snapshot.channel].update_snapshot(snapshot)
            self.statusBar().showMessage("Connected · polling every 1 s")

    @Slot(tuple)
    def enqueue(self, command: tuple) -> None:
        """@brief Queue a user operation and disable its panel until readback.

        @param command Action, channel, and action-specific values.
        @details Reports queue saturation; never blocks the GUI.
        """
        if self.worker is None or self.worker.isInterruptionRequested():
            return
        try:
            self.worker.commands.put_nowait(command)
        except Full:
            self.statusBar().showMessage("Command queue full; try again")
            return
        panel = self.panels[command[1]]
        panel.busy = True
        panel.setEnabled(False)
        self.statusBar().showMessage("Applying; waiting for device readback…")

    @Slot(int)
    def command_completed(self, channel: int) -> None:
        """@brief Unlock a panel after command readback finishes.

        @param channel Completed operation's channel.
        """
        panel = self.panels[channel]
        panel.busy = False
        if (
            self.worker is not None
            and not self.worker.isInterruptionRequested()
        ):
            panel.setEnabled(True)

    @Slot(str)
    def receive_error(self, message: str) -> None:
        """@brief Preserve a connection error for display after worker exit.

        @param message Transport or protocol error text.
        """
        self.error = message

    @Slot()
    def disconnected(self) -> None:
        """@brief Release the finished worker and invalidate all live data."""
        if self.worker is not None:
            self.worker.wait()
            self.worker.deleteLater()
            self.worker = None
        for panel in self.panels.values():
            panel.invalidate()
        for widget in (self.port, self.refresh, self.baud, self.demo):
            widget.setEnabled(True)
        self.connect_button.setEnabled(True)
        self.connect_button.setText("Connect")
        message = (
            f"Connection error: {self.error}" if self.error else "Disconnected"
        )
        self.identity.setText(message)
        self.statusBar().showMessage(message)
        if self.closing:
            self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        """@brief Defer window destruction until serial work has stopped.

        @param event Qt close event accepted only after worker exit.
        @details Preserves outputs and avoids forced thread termination.
        """
        if self.worker is not None:
            self.closing = True
            self.worker.requestInterruption()
            self.setEnabled(False)
            event.ignore()
        else:
            event.accept()


def main() -> int:
    """@brief Launch the GUI with an optional hardware-free demo selection.

    @return Qt application exit code.
    @details Parses --demo; hardware is opened only after clicking Connect.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="Select demo mode")
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    window = MainWindow(args.demo)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
