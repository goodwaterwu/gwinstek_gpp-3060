#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""@brief Serial protocol and demo device based on gpp3060_ctrl.py.

@details Each Device belongs to one worker thread. SCPI commands and channel
limits follow /opt/scripts/gpp3060_ctrl.py; this module is self-contained.
"""

from dataclasses import dataclass
import math
import time

import serial


@dataclass(frozen=True)
class Snapshot:
    """@brief Immutable confirmed channel state in volts, amperes, and watts.

    @details Measurements are None while output is off; demo values model
    a 10-ohm load. Channel identifies CH1 through CH3.
    """

    channel: int
    enabled: bool
    voltage: float
    current: float
    measured: tuple[float, float, float] | None


def validate(channel: int, voltage: float, current: float) -> None:
    """@brief Validate both setpoints before any hardware writes.

    @param channel Channel number from 1 through 3.
    @param voltage Voltage in volts; CH3 accepts fixed levels only.
    @param current Current limit in amperes.
    @throws ValueError For invalid channels, nonfinite or out-of-range values.
    """
    if channel not in (1, 2, 3):
        raise ValueError("Channel must be 1, 2, or 3")
    valid_v = (
        voltage in (1.8, 2.5, 3.3, 5.0)
        if channel == 3
        else math.isfinite(voltage) and 0 <= voltage <= 30
    )
    if not valid_v:
        raise ValueError("Voltage outside channel limits")
    if not math.isfinite(current) or not 0 <= current <= (
        5 if channel == 3 else 6
    ):
        raise ValueError("Current outside channel limits")


class Device:
    """@brief Own a serial connection or an explicitly simulated supply.

    @details Construct and use in one thread. Closing never changes outputs.
    Demo state is private and resets whenever the device is constructed.
    """

    def __init__(self, port: str, baud: int, demo: bool = False) -> None:
        """@brief Open an exclusive 8N1 serial connection, or demo state.

        @param port Serial device path.
        @param baud Serial speed in bits per second.
        @param demo True to simulate without opening a port.
        @throws OSError On serial open or configuration failure.
        @details Reads and writes time out after one second.
        """
        self.connection = None
        self.states = {ch: [False, 5.0, 1.0] for ch in (1, 2, 3)}
        if not demo:
            self.connection = serial.Serial(
                port, baud, timeout=1, write_timeout=1, exclusive=True
            )
            try:
                time.sleep(0.1)
                self.connection.reset_input_buffer()
            except OSError:
                self.connection.close()
                raise

    def close(self) -> None:
        """@brief Release the serial port without changing supply outputs."""
        if self.connection is not None:
            self.connection.close()

    def write(self, command: str) -> None:
        """@brief Send one LF-terminated SCPI command and allow settling.

        @param command ASCII command without a newline.
        @throws OSError On failed or incomplete writes.
        @details May modify live hardware; demo mode performs no serial I/O.
        """
        if self.connection is not None:
            payload = (command + "\n").encode("ascii")
            if self.connection.write(payload) != len(payload):
                raise OSError("Incomplete serial write")
            time.sleep(0.05)

    def query(self, command: str) -> str:
        """@brief Read a complete ASCII response to one real-device query.

        @param command SCPI query ending in a question mark.
        @return Whitespace-trimmed device response.
        @throws OSError On timeout, truncated reply, or unavailable port.
        @throws UnicodeError On non-ASCII data.
        """
        if self.connection is None:
            raise OSError("No serial connection")
        payload = (command + "\n").encode("ascii")
        if self.connection.write(payload) != len(payload):
            raise OSError("Incomplete query write")
        reply = self.connection.read_until(b"\n", size=1024)
        if not reply.endswith(b"\n"):
            raise OSError(f"Timeout or incomplete response: {command}")
        return reply.decode("ascii").strip()

    def identify(self) -> str:
        """@brief Identify the supply before exposing output controls.

        @return Device identity or explicit demo identity.
        @throws ValueError If the connected model is not a GPP-3060.
        @throws OSError On serial query failure.
        @throws UnicodeError On malformed serial data.
        """
        if self.connection is None:
            return "DEMO · GPP-3060 · simulated 10 Ω load"
        identity = self.query("*IDN?")
        if "GPP-3060" not in identity.upper():
            raise ValueError(f"Expected GPP-3060, received: {identity!r}")
        return identity

    def configure(self, channel: int, voltage: float, current: float) -> None:
        """@brief Validate then write current and voltage setpoints.

        @param channel Channel number from 1 through 3.
        @param voltage Target voltage in volts.
        @param current Current limit in amperes.
        @throws ValueError On invalid setpoints.
        @throws OSError On serial failure; partial application is possible.
        @details Changes active outputs immediately; does not switch outputs.
        """
        validate(channel, voltage, current)
        self.write(f":SOURce{channel}:CURRent {current:.4f}")
        formatted = f"{voltage:.1f}" if channel == 3 else f"{voltage:.3f}"
        self.write(f":SOURce{channel}:VOLTage {formatted}")
        self.states[channel][1:] = [voltage, current]

    def output(self, channel: int, enabled: bool) -> None:
        """@brief Switch a channel's output terminals.

        @param channel Channel number from 1 through 3.
        @param enabled True energizes output; False disables it.
        @throws ValueError On an invalid channel.
        @throws OSError On transmission failure.
        @details Caller must query status to confirm the actual state.
        """
        if channel not in self.states:
            raise ValueError("Invalid channel")
        state = "ON" if enabled else "OFF"
        self.write(f":OUTPut{channel}:STATe {state}")
        self.states[channel][0] = enabled

    def number(self, command: str) -> float:
        """@brief Parse a finite numeric SCPI response.

        @param command Numeric SCPI query.
        @return Finite response value.
        @throws ValueError On malformed or nonfinite responses.
        @throws OSError On serial failure.
        @throws UnicodeError On non-ASCII data.
        """
        value = float(self.query(command))
        if not math.isfinite(value):
            raise ValueError(f"Nonfinite response: {command}")
        return value

    def snapshot(self, channel: int) -> Snapshot:
        """@brief Read settings/state and measure V/A/W only for active output.

        @param channel Channel number from 1 through 3.
        @return Confirmed hardware snapshot or simulated load snapshot.
        @throws ValueError On invalid channel or malformed response.
        @throws OSError On serial failure.
        @throws UnicodeError On non-ASCII data.
        """
        if channel not in self.states:
            raise ValueError("Invalid channel")
        if self.connection is None:
            enabled, voltage, current = self.states[channel]
            amps = min(voltage / 10, current)
            volts = min(voltage, amps * 10)
            measured = (volts, amps, volts * amps) if enabled else None
            return Snapshot(channel, enabled, voltage, current, measured)
        state = self.query(f":OUTPut{channel}:STATe?").upper()
        if state not in ("ON", "OFF", "1", "0"):
            raise ValueError(f"Unknown output state: {state!r}")
        enabled = state in ("ON", "1")
        voltage = self.number(f":SOURce{channel}:VOLTage?")
        current = self.number(f":SOURce{channel}:CURRent?")
        measured = None
        if enabled:
            measured = tuple(
                self.number(f":MEASure{channel}:{quantity}?")
                for quantity in ("VOLTage", "CURRent", "POWEr")
            )
        return Snapshot(channel, enabled, voltage, current, measured)
