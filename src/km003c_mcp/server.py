"""MCP server exposing the ChargerLab POWER-Z KM003C as tools (stdio transport)."""

from __future__ import annotations

import atexit
import contextlib
import functools
import inspect
import logging
import os
import signal
import threading
import time
from collections import Counter
from dataclasses import asdict
from types import SimpleNamespace
from typing import Callable, Literal

import serial
from mcp.server.mcpserver import MCPServer

from . import capture_read
from .protocol import PdEventStream, ProtocolError
from .recorder import Recorder
from .trigger import (
    TriggerPort, entry_command, fast_charge_command, find_trigger_port, pd_request_command,
)
from .usb_device import (
    Km003cDevice, Km003cError, MeterLaggingError, PyUsbTransport, TransportError,
)

log = logging.getLogger(__name__)

RATE_INDEX = {2: 0, 10: 1, 50: 2, 1000: 3}
VBUS_WARNING = ("Makes the attached charger raise VBUS on the meter's output; "
                "disconnect sensitive loads first.")
# Unset KM003C_IDLE_RELEASE_S: None keeps the meter claimed until the server exits.
DEFAULT_IDLE_RELEASE_S: float | None = None
# After a USB reset the meter re-enumerates; reopening is retried this often.
RESET_REOPEN_ATTEMPTS = 10
RESET_REOPEN_DELAY_S = 0.3


def idle_release_from_env() -> float | None:
    """Seconds the meter stays claimed after its last lease ends, from ``KM003C_IDLE_RELEASE_S``.

    ``0`` releases as soon as the last lease ends. ``never`` (or ``off``) holds the claim
    until the server exits. Unset or invalid gives :data:`DEFAULT_IDLE_RELEASE_S`.
    """
    raw = os.environ.get("KM003C_IDLE_RELEASE_S", "").strip().lower()
    if not raw:
        return DEFAULT_IDLE_RELEASE_S
    if raw in ("never", "off"):
        return None
    try:
        seconds = float(raw)
    except ValueError:
        seconds = -1.0
    if not seconds >= 0:
        log.warning("ignoring KM003C_IDLE_RELEASE_S=%r", raw)
        return DEFAULT_IDLE_RELEASE_S
    return seconds


# Unset KM003C_ENABLE_TRIGGER: True registers the trigger tools.
DEFAULT_ENABLE_TRIGGER = True
TRIGGER_TOOLS = ("trigger_detect", "trigger_enter", "trigger_pd_source_caps",
                 "trigger_pd_request", "trigger_fast_charge", "trigger_reset", "trigger_close",
                 "trigger_raw")


def trigger_enabled_from_env() -> bool:
    """Whether to register the trigger tools, from ``KM003C_ENABLE_TRIGGER``.

    Unset or empty gives :data:`DEFAULT_ENABLE_TRIGGER`. ``1`` registers them. Any other value
    leaves them out, so a value meant as "off", such as ``false`` or ``no``, cannot fall back
    to a default that registers them.
    """
    raw = os.environ.get("KM003C_ENABLE_TRIGGER", "").strip()
    if not raw:
        return DEFAULT_ENABLE_TRIGGER
    if raw == "1":
        return True
    if raw != "0":
        log.warning("KM003C_ENABLE_TRIGGER=%r is not 1; the trigger tools stay off", raw)
    return False


def _daemon_timer(delay_s: float, fn: Callable[[], None]) -> threading.Timer:
    timer = threading.Timer(delay_s, fn)
    timer.daemon = True
    return timer


class Lease:
    """A claim on the meter. The USB interface stays claimed while any lease is held.

    Use it as a context manager for the length of a call, or keep it and call
    :meth:`release` when a capture or stream stops. Releasing twice is harmless.
    """

    def __init__(self, service: Km003cService, holder: str) -> None:
        self.holder = holder
        self._service = service
        self._held = True

    def release(self) -> None:
        self._service._end_lease(self)

    def __enter__(self) -> Lease:
        return self

    def __exit__(self, *exc) -> None:
        self.release()


_FROM_ENV = object()


class Km003cService:
    """Owns the USB session, the trigger port and the background recorder.

    The meter is claimed on demand. Every user of the device holds a :class:`Lease`; when the
    last one ends, the interface is released after ``idle_release_s`` seconds (0 at once,
    None never). A call that arrives while a release runs waits for it, then reopens.
    """

    def __init__(self, transport_factory: Callable = PyUsbTransport.open,
                 trigger_factory: Callable[[str], TriggerPort] = TriggerPort,
                 port_finder: Callable = find_trigger_port,
                 idle_release_s: float | None | object = _FROM_ENV,
                 timer_factory: Callable[[float, Callable[[], None]], threading.Timer] = _daemon_timer,
                 ) -> None:
        self._transport_factory = transport_factory
        self._trigger_factory = trigger_factory
        self._port_finder = port_finder
        self.idle_release_s = idle_release_from_env() if idle_release_s is _FROM_ENV \
            else idle_release_s
        self._timer_factory = timer_factory
        self._lock = threading.RLock()
        self._released = threading.Condition(self._lock)
        self._device: Km003cDevice | None = None
        self._trigger: TriggerPort | None = None
        self._leases: Counter[str] = Counter()
        self._releasing = False
        self._timer = None
        self.recorder = Recorder(self.device, on_transport_error=self.invalidate_device,
                                 lease=self.lease)

    # --- leases and release ------------------------------------------------------------

    def lease(self, holder: str = "call") -> Lease:
        """Take a lease; it waits while a release is in progress."""
        with self._released:
            while self._releasing:
                self._released.wait()
            self._cancel_timer_locked()
            self._leases[holder] += 1
            return Lease(self, holder)

    def leases(self) -> dict[str, int]:
        """Current lease holders and counts."""
        with self._lock:
            return dict(+self._leases)

    @property
    def claimed(self) -> bool:
        with self._lock:
            return self._device is not None

    def _end_lease(self, lease: Lease) -> None:
        with self._lock:
            if not lease._held:
                return
            lease._held = False
            self._leases[lease.holder] -= 1
            if self._leases[lease.holder] <= 0:
                del self._leases[lease.holder]
            if self._leases or self._device is None or self.idle_release_s is None:
                return
            if self.idle_release_s > 0:
                self._cancel_timer_locked()
                timer = self._timer_factory(self.idle_release_s, lambda: self._on_timer(timer))
                self._timer = timer
                timer.start()
                return
            device = self._begin_release_locked()
        self._finish_release(device)

    def _cancel_timer_locked(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    def _on_timer(self, timer) -> None:
        with self._lock:
            # A lease taken after the timer started cancelled it; a cancelled timer can still
            # fire once if it was already waiting for the lock.
            if timer is not self._timer or self._leases:
                return
            self._timer = None
            device = self._begin_release_locked()
        self._finish_release(device)

    def _begin_release_locked(self) -> Km003cDevice | None:
        self._cancel_timer_locked()
        device, self._device = self._device, None
        if device is not None:
            self._releasing = True
        return device

    def _finish_release(self, device: Km003cDevice | None) -> None:
        if device is None:
            return
        try:
            device.close()
            log.info("released the KM003C")
        except Exception as exc:  # noqa: BLE001 - the interface is dropped either way
            log.warning("releasing the KM003C failed: %s", exc)
        finally:
            with self._released:
                self._releasing = False
                self._released.notify_all()

    def release(self) -> bool:
        """Release the meter now. Returns False if it was not claimed.

        Refuses with :class:`Km003cError` while a capture, a stream, or another call holds a
        lease, because releasing would cut that work off.
        """
        with self._released:
            while self._releasing:
                self._released.wait()
            if self._leases:
                holders = ", ".join(sorted(self._leases))
                raise Km003cError(
                    f"the meter is in use ({holders}); stop a running capture or stream, "
                    "or retry once the other call ends")
            device = self._begin_release_locked()
        self._finish_release(device)
        return device is not None

    # --- device ------------------------------------------------------------------------

    def device(self) -> Km003cDevice:
        """The open session, opened on first use. Callers hold a lease while they use it."""
        with self._released:
            while self._releasing:
                self._released.wait()
            if self._device is None:
                device = self._open_device()
                try:
                    # A previous session may have left the firmware streaming; Reject is fine.
                    device.stop_graph()
                except Km003cError:
                    device.transport.close()
                    raise
                self._device = device
            return self._device

    def _open_device(self) -> Km003cDevice:
        """Claim the meter and Connect; reset the USB device once if the meter answers late."""
        transport = self._transport_factory()
        device = Km003cDevice(transport)
        try:
            device.connect()
            return device
        except MeterLaggingError as exc:
            log.warning("%s; resetting the USB device", exc)
            transport.reset()
        except Km003cError:
            transport.close()
            raise
        transport = self._reopen_after_reset()
        device = Km003cDevice(transport)
        try:
            device.connect()
        except Km003cError as exc:
            transport.close()
            raise Km003cError(
                "the KM003C answers late and still does after a USB reset "
                f"({exc}); unplug and replug the meter") from exc
        return device

    def _reopen_after_reset(self):
        """Open the meter again while it re-enumerates after a reset.

        A claim refused during re-enumeration is expected, so the holder lookup (``ioreg`` on
        macOS) is skipped here when the factory takes one; the loop then lasts at most
        ``RESET_REOPEN_ATTEMPTS * RESET_REOPEN_DELAY_S`` plus the open calls.
        """
        factory = self._transport_factory
        try:
            takes_lookup = "holder_lookup" in inspect.signature(factory).parameters
        except (TypeError, ValueError):
            takes_lookup = False
        if takes_lookup:
            factory = functools.partial(factory, holder_lookup=lambda serial: None)
        last: Exception | None = None
        for _ in range(RESET_REOPEN_ATTEMPTS):
            time.sleep(RESET_REOPEN_DELAY_S)
            try:
                return factory()
            except Km003cError as exc:
                last = exc
        waited = RESET_REOPEN_ATTEMPTS * RESET_REOPEN_DELAY_S
        raise Km003cError(
            f"the KM003C did not come back within {waited:g} s after a USB reset ({last}); "
            "unplug and replug the meter")

    def invalidate_device(self) -> None:
        """Drop a broken USB session; the next call opens a fresh one."""
        with self._lock:
            self._cancel_timer_locked()
            device, self._device = self._device, None
        if device is None:
            return
        self.recorder.abort("USB session reset after a transport error")
        with device.lock:
            try:
                device.transport.close()
            except Exception:  # noqa: BLE001 - the link is already broken
                pass

    def usb_serial(self) -> str | None:
        try:
            with self.lease("trigger port lookup"):
                return self.device().serial_number or None
        except Km003cError:
            return None

    def trigger(self) -> TriggerPort:
        with self._lock:
            if self._trigger is None:
                self._trigger = self._trigger_factory(self._port_finder(self.usb_serial()))
            return self._trigger

    def invalidate_trigger(self) -> None:
        with self._lock:
            trigger, self._trigger = self._trigger, None
        if trigger is not None:
            trigger.dispose()

    def shutdown(self) -> None:
        """Best-effort cleanup at process exit."""
        steps = [self.recorder.stop_pd]
        if self.recorder.stream_status()["running"]:
            steps.append(self.recorder.stop_stream)
        if self._trigger is not None:
            steps.append(self._trigger.close)
        for step in steps:
            try:
                step()
            except Exception:  # noqa: BLE001
                pass
        with self._released:
            while self._releasing:
                self._released.wait()
            device = self._begin_release_locked()
        self._finish_release(device)


def build_tools(service: Km003cService, enable_trigger: bool | None = None) -> SimpleNamespace:
    """Create the tool callables; errors come back as {"error", "kind"} dicts.

    The trigger tools are left out unless ``enable_trigger`` is True; None reads
    ``KM003C_ENABLE_TRIGGER``. They make the meter a PD sink that can raise VBUS on its
    output, so leaving them out keeps an agent from seeing or calling them.
    """
    if enable_trigger is None:
        enable_trigger = trigger_enabled_from_env()

    def safe(fn, claims_meter: bool = False):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            # The lease ends after a broken session is dropped, so release never touches it.
            lease = service.lease(fn.__name__) if claims_meter else contextlib.nullcontext()
            with lease:
                try:
                    return fn(*args, **kwargs)
                except TransportError as exc:
                    service.invalidate_device()
                    return {"error": str(exc), "kind": type(exc).__name__}
                except serial.SerialException as exc:
                    service.invalidate_trigger()
                    return {"error": str(exc), "kind": type(exc).__name__}
                except (Km003cError, ProtocolError, ValueError, OSError) as exc:
                    return {"error": str(exc), "kind": type(exc).__name__}
        return wrapper

    def meter(fn):
        """Like ``safe``, holding a lease on the meter for the length of the call."""
        return safe(fn, claims_meter=True)

    def trigger_run(command: str, timeout_s: float = 3.0) -> dict:
        port = service.trigger()
        port.ensure_open()
        return {"command": command, "lines": port.command(command, timeout_s=timeout_s)}

    @meter
    def device_info() -> dict:
        """Identify the attached KM003C: model, hardware/firmware version, serial, trigger port."""
        device = service.device()
        info = asdict(device.device_info())
        info["usb_serial"] = device.serial_number
        try:
            info["trigger_port"] = service._port_finder(device.serial_number)
        except Km003cError:
            info["trigger_port"] = None
        return info

    @meter
    def measure(samples: int = 1, interval_ms: int = 100) -> dict:
        """Read VBUS/IBUS/power, CC1/CC2, D+/D-, VDD and temperature (V, A, W, °C).

        With samples > 1 the readings are taken interval_ms apart and mean/min/max are
        returned for vbus_v, ibus_a and power_w. Positive current flows from the meter's
        USB-C receptacle (input) to its plug (output).
        """
        if not 1 <= samples <= 100:
            raise ValueError("samples must be 1..100")
        readings = []
        for i in range(samples):
            if i:
                time.sleep(interval_ms / 1000)
            readings.append(asdict(service.device().read_adc()))
        if samples == 1:
            return readings[0]
        out: dict = {"samples": samples}
        for key in ("vbus_v", "ibus_a", "power_w"):
            values = [r[key] for r in readings]
            out[key] = {"mean": sum(values) / len(values), "min": min(values), "max": max(values)}
        out["last"] = readings[-1]
        return out

    @meter
    def pd_status() -> dict:
        """Read the PD-side status snapshot (timestamp, VBUS, IBUS, CC1, CC2).

        Not available while a PD capture runs, because reading would consume captured events.
        """
        if service.recorder.pd_running:
            raise Km003cError("a PD capture is running; use pd_capture_read instead")
        result = service.device().read_pd()
        if isinstance(result, PdEventStream):
            out = asdict(result.preamble)
            out["discarded_events"] = len(result.events)
            return out
        return asdict(result)

    @meter
    def stream_start(rate_sps: Literal[2, 10, 50, 1000] = 1000, csv_path: str | None = None) -> dict:
        """Start high-rate VBUS/IBUS sampling in the background (2, 10, 50 or 1000 samples/s).

        Optionally writes every sample to csv_path. Use stream_status / stream_samples while
        running and stream_stop for the final statistics (energy, charge, min/max/mean).
        """
        if rate_sps not in RATE_INDEX:
            raise ValueError(f"rate_sps must be one of {sorted(RATE_INDEX)}")
        service.recorder.start_stream(RATE_INDEX[rate_sps], csv_path)
        return {"started": True, "rate_sps": rate_sps, "csv_path": csv_path}

    @safe
    def stream_status() -> dict:
        """Running state, error (if any), live statistics and the most recent samples."""
        return service.recorder.stream_status()

    @safe
    def stream_samples(max_samples: int = 500) -> dict:
        """Return up to max_samples evenly spaced samples from the running stream's buffer."""
        return {"samples": service.recorder.stream_samples(max_samples)}

    @meter
    def stream_stop() -> dict:
        """Stop the stream and return its statistics."""
        return service.recorder.stop_stream().to_dict()

    def read_capture(path, *args, extra: dict | None = None) -> dict:
        """Read a capture file; when it is this session's capture, say whether it still runs.

        The added fields count toward the summary's size bound.
        """
        extra = dict(extra or {})
        state = service.recorder.pd_capture_state()
        if state["path"] is not None and state["path"] == str(capture_read.resolve_path(path)):
            extra["running"] = state["running"]
        return capture_read.read(path, *args, extra=extra)

    def capture_summary(path, extra: dict | None = None) -> dict:
        return read_capture(path, "summary", extra=extra)

    def current_capture() -> str:
        path = service.recorder.capture_path
        if path is None:
            raise Km003cError("no PD capture in this session; pass path to read a saved file")
        return str(path)

    @meter
    def pd_capture_start(path: str | None = None, label: str | None = None,
                         provenance: dict | None = None, stop_after_messages: int | None = None,
                         stop_on: Literal["none", "contract", "identity"] = "none",
                         settle_ms: int = 0, timeout_s: float = 60,
                         status_interval_ms: int = 100) -> dict:
        """Start capturing USB PD traffic to a capture file (JSON Lines) in the background.

        The file is path, or a new file in $KM003C_CAPTURE_DIR, else ./captures; the result
        gives its absolute path. label and provenance (any JSON object) go in its header.
        The capture stops on its own when stop_on is met: contract (PS_RDY after an Accept
        of a Request) or identity (a Discover Identity ACK on SOP); or after
        stop_after_messages non-GoodCRC messages; then keeps capturing settle_ms more
        (meter time). It always stops after timeout_s. A status record (VBUS, IBUS, CC) is
        written every status_interval_ms and with every poll that carries events.

        Start the capture before the stimulus: the meter records only what happens after
        monitoring is enabled. Then use pd_capture_wait, pd_capture_mark, pd_capture_stop
        and pd_capture_read. pd_monitor reports the enable: accepted, or "already enabled
        or refused" when the meter answered Reject, which does not stop capture.
        """
        monitor = service.recorder.start_pd(
            path, label, provenance, stop_on=stop_on, stop_after_messages=stop_after_messages,
            settle_ms=settle_ms, timeout_s=timeout_s, status_interval_ms=status_interval_ms)
        return {"started": True, "path": str(service.recorder.capture_path),
                "pd_monitor": monitor.to_dict()}

    @safe
    def pd_capture_wait(timeout_s: float = 60) -> dict:
        """Wait up to timeout_s for the running capture to stop, then return its summary.

        running is True in the result when the capture is still going; reason says why it
        stopped. Waiting does not stop the capture. timeout_s is clamped to 0 to 86400.
        """
        service.recorder.wait_pd(timeout_s)
        return capture_summary(current_capture())

    @safe
    def pd_capture_mark(text: str) -> dict:
        """Write an annotation into the running capture, such as "stimulus GSrC sent"."""
        record = service.recorder.mark_pd(text)
        return {"marked": True, "path": current_capture(), "t_host": record["t_host"]}

    @meter
    def pd_capture_stop() -> dict:
        """Stop the running PD capture and return its summary (stopped is False when none ran;
        the summary is then the last capture's)."""
        stopped = service.recorder.stop_pd() is not None
        return capture_summary(current_capture(), extra={"stopped": stopped})

    @safe
    def pd_capture_read(path: str | None = None,
                        mode: Literal["summary", "messages", "facts", "full"] = "summary",
                        sop: str | None = None, types: list[str] | None = None,
                        since: int | None = None, after: int | None = None,
                        limit: int | None = None, include_goodcrc: bool = False) -> dict:
        """Read a capture file; never touches the meter. Without path: the running or last
        capture of this session.

        mode summary (default): about 2 KB at most; one line per non-GoodCRC message such as
        "t=84002 SOP Request pos 4, 3.0 A [8210 2cb18441]", connection events and marks,
        counts, VBUS first/last/min/max (mV), the final contract, and warnings from the
        sanity rules (no negotiation, PD monitoring inactive, capture started late).
        messages: the same lines, filtered and paged (limit default 50): pass a page's
        next_after as after for the next page; next_after is absent on the last page. facts: Source_Capabilities PDOs, Request RDOs with their
        object position, Accept and PS_RDY times, and Discover Identity ACKs grouped by SOP
        and sender, each value beside its hex; at most about 8 KB, with omitted counting what
        a long capture left out. full: complete decodes (limit default 20).
        Filters: sop ("SOP", "SOP'", "SOP''"), types (message type or VDM command names,
        such as Request or Discover_Identity; also connect, disconnect, mark, error), since
        (keeps entries at or after that meter time in ms; marks without a time drop out),
        include_goodcrc. after is a line number in the file (exclusive) and is how pages
        advance; since only narrows the window. Times are the meter's uptime in ms. running
        appears when the file is this session's capture.
        """
        return read_capture(path or current_capture(), mode, sop, types, since, limit,
                            include_goodcrc, after)

    @safe
    def device_release() -> dict:
        """Release the meter's USB interface now, so another program can claim it.

        Refused while a PD capture or a stream runs; stop it first. The next tool that needs
        the meter claims it again. released is False when the meter was not claimed.
        """
        return {"released": service.release()}

    @safe
    def trigger_detect(extended: bool = False, timeout_s: float = 15) -> dict:
        """Detect the fast-charge protocols the attached charger supports (`entry list`).

        extended=True also probes VOOC/MTK PE (`entry list+`), which takes much longer.
        """
        return trigger_run("entry list+" if extended else "entry list", timeout_s)

    @safe
    def trigger_enter(protocol: str, timeout_s: float = 5) -> dict:
        """Enter a trigger protocol: pd, ufcs, qc, fcp, scp, afc, vfcp, sfcp, tfcp, bc, apple."""
        return trigger_run(entry_command(protocol), timeout_s)

    @safe
    def trigger_pd_source_caps() -> dict:
        """Ask the charger for its PD Source_Capabilities (`pd pdo`)."""
        return trigger_run("pd pdo")

    @safe
    def trigger_pd_request(position: int, voltage_mv: int | None = None,
                           current_ma: int | None = None) -> dict:
        """Request PDO `position` (1-based); voltage_mv is needed for PPS/AVS PDOs."""
        return trigger_run(pd_request_command(position, voltage_mv, current_ma))

    @safe
    def trigger_fast_charge(protocol: str, voltage_mv: int, current_ma: int | None = None,
                            request: int | None = None) -> dict:
        """Request a voltage via qc, qc3, fcp, afc, sfcp, scp, vfcp, tfcp or ufcs."""
        return trigger_run(fast_charge_command(protocol, voltage_mv, current_ma, request))

    @safe
    def trigger_reset() -> dict:
        """Reset the protocol trigger to its initial state (re-opened on the next trigger call)."""
        port = service.trigger()
        lines = port.command("reset")
        port.is_open = False
        return {"command": "reset", "lines": lines}

    @safe
    def trigger_close() -> dict:
        """Leave protocol trigger mode (`pdm close`)."""
        lines = service.trigger().close()
        service.invalidate_trigger()
        return {"command": "pdm close", "lines": lines}

    @safe
    def trigger_raw(command: str, timeout_s: float = 3) -> dict:
        """Send any trigger command from ChargerLab's virtual serial port sheet verbatim."""
        return trigger_run(command, timeout_s)

    for fn in (trigger_pd_request, trigger_fast_charge, trigger_raw):
        fn.__doc__ = f"{fn.__doc__}\n\n{VBUS_WARNING}"

    return SimpleNamespace(**{fn.__name__: fn for fn in (
        device_info, measure, pd_status, stream_start, stream_status, stream_samples,
        stream_stop, pd_capture_start, pd_capture_wait, pd_capture_mark, pd_capture_read,
        pd_capture_stop, device_release,
        trigger_detect, trigger_enter, trigger_pd_source_caps, trigger_pd_request,
        trigger_fast_charge, trigger_reset, trigger_close, trigger_raw)
        if enable_trigger or fn.__name__ not in TRIGGER_TOOLS})


def build_server(tools: SimpleNamespace) -> MCPServer:
    """Register every tool in ``tools`` on a new MCP server."""
    triggering = hasattr(tools, "trigger_raw")
    server = MCPServer(
        "km003c",
        instructions="Tools for the ChargerLab POWER-Z KM003C USB-C power meter: measurements, "
                     "high-rate streaming, USB PD capture/decoding"
                     + (" and fast-charge protocol triggering." if triggering else "."),
    )
    for tool in vars(tools).values():
        server.tool()(tool)
    return server


service = Km003cService()
tools = build_tools(service)
mcp = build_server(tools)


# After SIGTERM the server exits this long after starting its cleanup, even if a step hangs.
SIGTERM_EXIT_DEADLINE_S = 10.0


def install_sigterm_handler(service: Km003cService, exit_fn: Callable[[int], None] = os._exit,
                            deadline_s: float = SIGTERM_EXIT_DEADLINE_S) -> None:
    """On SIGTERM, stop a running capture and stream, release the meter, and exit 0.

    A running capture gets its footer with reason ``manual``. The cleanup runs on its own
    thread, because the signal can arrive while the main thread holds a lock the cleanup
    needs; the process exits when it is done, or after ``deadline_s`` at the latest.
    """
    def stop_and_exit() -> None:
        try:
            service.shutdown()
        finally:
            exit_fn(0)

    def on_sigterm(signum, frame) -> None:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        threading.Thread(target=stop_and_exit, name="km003c-sigterm", daemon=True).start()
        watchdog = threading.Timer(deadline_s, exit_fn, args=(0,))
        watchdog.daemon = True
        watchdog.start()

    signal.signal(signal.SIGTERM, on_sigterm)


def main() -> None:
    atexit.register(service.shutdown)
    install_sigterm_handler(service)
    mcp.run()
