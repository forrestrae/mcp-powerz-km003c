"""KM003C device access over the vendor bulk interface (interface 0, EP 0x01/0x81)."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from typing import Callable, Protocol

import usb.core
import usb.util

from . import crypto
from .protocol import (
    ATT_ADC, ATT_PD, CMD_ACCEPT, CMD_CONNECT, CMD_DISABLE_PD_MONITOR, CMD_DISCONNECT,
    CMD_ENABLE_PD_MONITOR, CMD_GET_DATA, CMD_MEMORY_READ, CMD_NOT_READABLE, CMD_PUT_DATA,
    CMD_REJECT, CMD_START_GRAPH, CMD_STOP_GRAPH, CMD_STREAMING_AUTH, AdcReading, LogicalPacket,
    PdEventStream, PdStatus, ProtocolError, encode_ctrl, parse_adc, parse_pd, parse_putdata,
)

log = logging.getLogger(__name__)

VID = 0x5FC9
PID = 0x0063
INTERFACE = 0
EP_OUT = 0x01
EP_IN = 0x81
TIMEOUT_MS = 2000
READ_SIZE = 8192
CONNECT_ATTEMPTS = 3
PD_MONITOR_ATTEMPTS = 5
PD_MONITOR_RETRY_DELAY_S = 0.2
# Reply timeout for the Connect and EnablePdMonitor of each enable attempt. Bounds how long
# one attempt holds the device lock, so a running stream's poller is not starved.
PD_MONITOR_TIMEOUT_MS = 500


class Km003cError(Exception):
    """Base error for KM003C device access."""


class DeviceNotFoundError(Km003cError):
    pass


class DeviceBusyError(Km003cError):
    pass


class TransportError(Km003cError):
    """The USB link failed; the session must be reopened."""


class DeviceTimeoutError(TransportError):
    pass


class CommandRejectedError(Km003cError):
    pass


class PdMonitorNotEnabledError(Km003cError):
    """EnablePdMonitor got neither Accept nor Reject on any attempt."""


@dataclass(frozen=True)
class PdMonitorEnable:
    """Outcome of :meth:`Km003cDevice.enable_pd_monitor`.

    ``accepted`` is False when the meter answered Reject. Firmware 2.0.0 does so when the
    monitor is already enabled and no Connect came between; other firmware may also refuse.
    Capture still works then.
    ``last_reply`` is the hex header of the last reply, or None if the last attempt timed out.
    """

    accepted: bool
    outcome: str
    attempts: int
    last_reply: str | None

    def to_dict(self) -> dict:
        return asdict(self)


class UsbTransport(Protocol):
    serial_number: str

    def write(self, data: bytes, timeout_ms: int) -> None: ...

    def read(self, size: int, timeout_ms: int) -> bytes: ...

    def close(self) -> None: ...


def _backend():
    """Load libusb-1.0: an explicit path, then the bundled library, then the system's.

    ``KM003C_LIBUSB`` (or its older name ``KM003C_LIBUSB_DLL``) names the library file.
    Without it, the copy bundled by ``libusb-package`` is tried first, then pyusb's normal
    search, which also finds Homebrew's ``/opt/homebrew/lib/libusb-1.0.dylib`` on Apple
    Silicon and a distribution's ``libusb-1.0.so`` on Linux.
    """
    import usb.backend.libusb1

    path = os.environ.get("KM003C_LIBUSB") or os.environ.get("KM003C_LIBUSB_DLL")
    if path:
        backend = usb.backend.libusb1.get_backend(find_library=lambda _name: path)
        if backend is None:
            raise Km003cError(f"cannot load libusb-1.0 from {path!r} (KM003C_LIBUSB)")
        return backend
    try:
        import libusb_package

        backend = libusb_package.get_libusb1_backend()
    except ImportError:
        backend = None
    if backend is None:
        backend = usb.backend.libusb1.get_backend()
    if backend is None:
        raise Km003cError(
            "no libusb-1.0 library found; install libusb (for example `brew install libusb`) "
            "or set KM003C_LIBUSB to the library file")
    return backend


def _find_devices() -> list:
    return list(usb.core.find(find_all=True, idVendor=VID, idProduct=PID, backend=_backend()))


def _serial_of(dev) -> str:
    try:
        return dev.serial_number or ""
    except (ValueError, usb.core.USBError):
        return ""


class PyUsbTransport:
    def __init__(self, dev, serial_number: str) -> None:
        self._dev = dev
        self.serial_number = serial_number

    @classmethod
    def open(cls, serial: str | None = None) -> PyUsbTransport:
        serial = serial or os.environ.get("KM003C_SERIAL") or None
        devices = _find_devices()
        if not devices:
            raise DeviceNotFoundError("no POWER-Z KM003C (USB 5FC9:0063) found")
        serials = [_serial_of(d) for d in devices]
        if serial is not None:
            matches = [d for d, s in zip(devices, serials) if s == serial]
            if not matches:
                raise DeviceNotFoundError(f"no KM003C with serial {serial!r}; found {serials}")
            dev = matches[0]
        elif len(devices) > 1:
            raise Km003cError(
                f"multiple KM003C devices attached ({', '.join(serials)}); set KM003C_SERIAL")
        else:
            dev = devices[0]
        try:
            dev.set_configuration()
            usb.util.claim_interface(dev, INTERFACE)
        except (usb.core.USBError, NotImplementedError) as exc:
            # Close the handle the failed attempt opened. While it stays open, for example
            # because the raised error's traceback keeps ``dev`` alive, macOS refuses this
            # process's next claim with errno 19 even after the other holder has exited.
            usb.util.dispose_resources(dev)
            raise DeviceBusyError(
                f"cannot claim the KM003C vendor interface ({exc}); "
                "close the POWER-Z PC software or any other program using the meter") from exc
        return cls(dev, _serial_of(dev))

    def write(self, data: bytes, timeout_ms: int) -> None:
        try:
            self._dev.write(EP_OUT, data, timeout=timeout_ms)
        except usb.core.USBTimeoutError as exc:
            raise DeviceTimeoutError("USB write timed out") from exc
        except usb.core.USBError as exc:
            raise TransportError(f"USB write failed: {exc}") from exc

    def read(self, size: int, timeout_ms: int) -> bytes:
        try:
            return bytes(self._dev.read(EP_IN, size, timeout=timeout_ms))
        except usb.core.USBTimeoutError as exc:
            raise DeviceTimeoutError("no reply from the KM003C") from exc
        except usb.core.USBError as exc:
            raise TransportError(f"USB read failed: {exc}") from exc

    def close(self) -> None:
        try:
            usb.util.release_interface(self._dev, INTERFACE)
        except usb.core.USBError:
            pass
        usb.util.dispose_resources(self._dev)


def _cmd(packet: bytes) -> int:
    return packet[0] & 0x7F if packet else -1


class Km003cDevice:
    """Request/response session with one KM003C. All transactions hold ``lock``."""

    def __init__(self, transport: UsbTransport) -> None:
        self.transport = transport
        self.lock = threading.RLock()
        self.graph_rate: int | None = None
        self.auth_level: int | None = None
        self._tid = 0

    @property
    def serial_number(self) -> str:
        return self.transport.serial_number

    def _next_tid(self) -> int:
        self._tid = self._tid % 255 + 1
        return self._tid

    def _transact(self, packet: bytes, match: Callable[[bytes], bool],
                  timeout_ms: int = TIMEOUT_MS) -> bytes:
        """Send a packet and return the first reply accepted by ``match``; drop the rest."""
        self.transport.write(packet, timeout_ms)
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            remaining = int((deadline - time.monotonic()) * 1000)
            if remaining <= 0:
                raise DeviceTimeoutError(f"no matching reply to command 0x{packet[0]:02x}")
            reply = self.transport.read(READ_SIZE, remaining)
            if len(reply) >= 4 and match(reply):
                return reply
            log.debug("dropping unmatched packet %s", reply[:8].hex())

    def _control(self, cmd: int, attribute: int = 0, timeout_ms: int = TIMEOUT_MS) -> bytes:
        """Send a control command and return its same-TID reply."""
        tid = self._next_tid()
        return self._transact(encode_ctrl(cmd, tid, attribute), lambda r: r[1] == tid,
                              timeout_ms)

    def connect(self) -> None:
        with self.lock:
            last: Exception | None = None
            for attempt in range(CONNECT_ATTEMPTS):
                try:
                    reply = self._control(CMD_CONNECT)
                    if _cmd(reply) == CMD_ACCEPT:
                        return
                    last = CommandRejectedError(f"Connect answered with 0x{_cmd(reply):02x}")
                except DeviceTimeoutError as exc:
                    last = exc
                if attempt < CONNECT_ATTEMPTS - 1:
                    time.sleep(0.1)
            raise Km003cError(f"cannot connect to the KM003C: {last}")

    def disconnect(self) -> None:
        with self.lock:
            tid = self._next_tid()
            # Firmware 2.0.0 answers Disconnect with a Disconnect packet whose TID is fixed at
            # 0x11 (03 11 00 00), so a same-TID match alone would wait out the full timeout.
            try:
                self._transact(encode_ctrl(CMD_DISCONNECT, tid),
                               lambda r: r[1] == tid or _cmd(r) == CMD_DISCONNECT)
            except DeviceTimeoutError:
                pass

    def enable_pd_monitor(self) -> PdMonitorEnable:
        """Connect, then send EnablePdMonitor; retry with Connect and a short pause between.

        Accept and Reject both end the retries. Firmware 2.0.0 captures PD traffic without the
        enable and answers a repeated enable with Reject, so Reject is reported, not raised.

        Each attempt sends one Connect and one EnablePdMonitor, each waiting at most
        ``PD_MONITOR_TIMEOUT_MS`` for its reply, and holds the device lock only for that
        attempt. A running stream's poller therefore waits at most about 1 s for the lock,
        and the whole call takes at most about 6 s when the meter never answers.
        """
        last_reply: str | None = None
        last_error: Exception | None = None
        for attempt in range(1, PD_MONITOR_ATTEMPTS + 1):
            if attempt > 1:
                time.sleep(PD_MONITOR_RETRY_DELAY_S)
            with self.lock:
                try:
                    reply = self._control(CMD_CONNECT, timeout_ms=PD_MONITOR_TIMEOUT_MS)
                    if _cmd(reply) != CMD_ACCEPT:
                        last_reply = None
                        last_error = CommandRejectedError(
                            f"Connect answered with 0x{_cmd(reply):02x}")
                        continue
                    reply = self._control(CMD_ENABLE_PD_MONITOR, 1,
                                          timeout_ms=PD_MONITOR_TIMEOUT_MS)
                except DeviceTimeoutError as exc:
                    last_reply, last_error = None, exc
                    continue
            last_reply = reply[:4].hex()
            if _cmd(reply) == CMD_ACCEPT:
                return PdMonitorEnable(True, "accepted", attempt, last_reply)
            if _cmd(reply) == CMD_REJECT:
                log.info("EnablePdMonitor answered Reject: already enabled or refused")
                return PdMonitorEnable(False, "already enabled or refused", attempt, last_reply)
            last_error = CommandRejectedError(
                f"EnablePdMonitor answered with 0x{_cmd(reply):02x}")
        raise PdMonitorNotEnabledError(
            f"PD monitor not enabled after {PD_MONITOR_ATTEMPTS} attempts "
            f"(last reply {last_reply or 'none'}): {last_error}")

    def disable_pd_monitor(self) -> bool:
        """Send DisablePdMonitor. Returns True on Accept, False on Reject (already off)."""
        with self.lock:
            reply = self._control(CMD_DISABLE_PD_MONITOR, 0)
            if _cmd(reply) not in (CMD_ACCEPT, CMD_REJECT):
                raise CommandRejectedError(f"DisablePdMonitor answered with 0x{_cmd(reply):02x}")
            return _cmd(reply) == CMD_ACCEPT

    def get_data(self, attribute_mask: int) -> list[LogicalPacket]:
        with self.lock:
            tid = self._next_tid()
            reply = self._transact(
                encode_ctrl(CMD_GET_DATA, tid, attribute_mask),
                lambda r: r[1] == tid and _cmd(r) in (CMD_PUT_DATA, CMD_REJECT))
            if _cmd(reply) == CMD_REJECT:
                raise CommandRejectedError(f"GetData 0x{attribute_mask:04x} rejected")
            return parse_putdata(reply)[1]

    def _single(self, attribute: int) -> bytes:
        for packet in self.get_data(attribute):
            if packet.attribute == attribute:
                return packet.payload
        raise ProtocolError(f"reply carries no attribute 0x{attribute:04x}")

    def read_adc(self) -> AdcReading:
        return parse_adc(self._single(ATT_ADC))

    def read_pd(self) -> PdStatus | PdEventStream:
        return parse_pd(self._single(ATT_PD))

    def memory_read(self, address: int, size: int) -> bytes:
        with self.lock:
            if self.graph_rate is not None:
                raise Km003cError("device memory cannot be read while streaming is active")
            tid = self._next_tid()
            reply = self._transact(
                crypto.build_memory_read_packet(address, size, tid),
                lambda r: r[1] == tid and _cmd(r) in (CMD_MEMORY_READ, CMD_REJECT, CMD_NOT_READABLE))
            if _cmd(reply) == CMD_REJECT:
                raise CommandRejectedError(f"MemoryRead at 0x{address:08x} rejected")
            if _cmd(reply) == CMD_NOT_READABLE:
                raise Km003cError(f"memory at 0x{address:08x} is not readable")
            echoed = crypto.parse_memory_confirmation(reply[4:20])
            if echoed != (address, size):
                raise ProtocolError(f"MemoryRead confirmation echoed {echoed}, expected {(address, size)}")
            expected = crypto.memory_cipher_len(size)
            cipher = bytearray(reply[20:])
            while len(cipher) < expected:
                cipher += self.transport.read(READ_SIZE, TIMEOUT_MS)
            return crypto.decrypt_memory(bytes(cipher), size)

    def device_info(self) -> crypto.DeviceInfo:
        with self.lock:
            return crypto.parse_device_info(
                self.memory_read(crypto.ADDR_DEVICE_INFO, 64),
                self.memory_read(crypto.ADDR_FIRMWARE_INFO, 64),
                self.memory_read(crypto.ADDR_CALIBRATION, 64),
                self.memory_read(crypto.ADDR_HARDWARE_ID, crypto.HARDWARE_ID_SIZE),
            )

    def authenticate(self) -> int:
        with self.lock:
            hwid = self.memory_read(crypto.ADDR_HARDWARE_ID, crypto.HARDWARE_ID_SIZE)
            packet = crypto.build_streaming_auth_packet(hwid, self._next_tid(), int(time.time() * 1000))
            # The device answers StreamingAuth with transaction id 0, so match on type only.
            reply = self._transact(packet, lambda r: _cmd(r) == CMD_STREAMING_AUTH)
            self.auth_level = crypto.parse_streaming_auth_response(reply)
            return self.auth_level

    def start_graph(self, rate_index: int) -> None:
        with self.lock:
            if not self.auth_level:
                self.authenticate()
            if not self.auth_level:
                raise Km003cError("streaming auth failed; the device refused AdcQueue streaming")
            reply = self._control(CMD_START_GRAPH, rate_index)
            if _cmd(reply) != CMD_ACCEPT:
                raise CommandRejectedError(f"StartGraph answered with 0x{_cmd(reply):02x}")
            self.graph_rate = rate_index

    def stop_graph(self) -> None:
        with self.lock:
            reply = self._control(CMD_STOP_GRAPH)
            # Reject means the firmware had already stopped streaming on its own.
            if _cmd(reply) not in (CMD_ACCEPT, CMD_REJECT):
                raise CommandRejectedError(f"StopGraph answered with 0x{_cmd(reply):02x}")
            self.graph_rate = None

    def close(self) -> None:
        with self.lock:
            try:
                if self.graph_rate is not None:
                    self.stop_graph()
                self.disconnect()
            except Km003cError:
                pass
            self.transport.close()
