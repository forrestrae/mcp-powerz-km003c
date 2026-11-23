"""Background poller for AdcQueue streaming and PD event capture.

A single daemon thread polls the device while a stream and/or a PD capture is
active, combining both attributes into one GetData request per cycle. A PD capture
writes a capture file (see :mod:`km003c_mcp.capture`) and stops itself when its stop
condition or timeout is met.
"""

from __future__ import annotations

import csv
import logging
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .capture import CaptureSession, CaptureWriter, new_capture_path, validate_stop_options
from .protocol import (
    ATT_ADCQUEUE, ATT_PD, GRAPH_RATES_SPS, PD_STATUS_SIZE, AdcQueueSample, PdStatus,
    ProtocolError, parse_adcqueue, parse_pd, sequence_step,
)
from .usb_device import Km003cDevice, Km003cError, PdMonitorEnable, TransportError

log = logging.getLogger(__name__)

CSV_HEADER = ["sequence", "marker", "vbus_v", "ibus_a", "power_w", "cc1_v", "cc2_v", "dp_v", "dm_v"]
RECENT_SAMPLES = 20
# A reply carries at most 63 queued samples and older ones are lost, so at 1000 SPS the
# queue must be drained well within 63 ms.
FAST_POLL_INTERVAL_S = 0.02
# Slower streams are polled at least this often, whatever the PD poll interval is, so a
# long KM003C_PD_POLL_MS cannot overflow the 63-sample queue at 50 SPS.
STREAM_POLL_INTERVAL_S = 0.05
# A KM003C on firmware 2.0.0 returned a whole 16-message negotiation in one poll even at
# 1000 ms, so 50 ms leaves a wide margin.
DEFAULT_PD_POLL_MS = 50
# KM003C_PD_POLL_MS is clamped to this range; a huge value would make the poller's sleep
# overflow, and the meter gains nothing from polls under 1 ms apart.
MIN_PD_POLL_MS = 1
MAX_PD_POLL_MS = 10_000
# A reply that fails to parse loses that poll's data but leaves the link working, so the
# poller records it and goes on; this many in a row end the stream or the capture they hit.
MAX_CONSECUTIVE_PROTOCOL_ERRORS = 10
# wait_pd waits at most this long per call; larger values (and inf) are clamped, because
# Condition.wait raises OverflowError past threading.TIMEOUT_MAX.
MAX_WAIT_S = 24 * 3600.0


@dataclass
class StreamStats:
    rate_sps: int = 0
    samples: int = 0
    dropped_samples: int = 0
    duration_s: float = 0.0
    vbus_min_v: float | None = None
    vbus_max_v: float | None = None
    vbus_mean_v: float | None = None
    ibus_min_a: float | None = None
    ibus_max_a: float | None = None
    ibus_mean_a: float | None = None
    power_max_w: float | None = None
    power_mean_w: float | None = None
    energy_wh: float = 0.0
    charge_mah: float = 0.0
    csv_path: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _sample_dict(s: AdcQueueSample) -> dict:
    return {"sequence": s.sequence, "marker": s.marker, "vbus_v": s.vbus_v, "ibus_a": s.ibus_a,
            "power_w": s.vbus_v * s.ibus_a, "cc1_v": s.cc1_v, "cc2_v": s.cc2_v,
            "dp_v": s.dp_v, "dm_v": s.dm_v}


class _Stream:
    """Accumulates statistics for one AdcQueue stream."""

    def __init__(self, rate_index: int, csv_path: str | None, keep: int) -> None:
        self.rate_index = rate_index
        self.step = sequence_step(rate_index)
        self.dt = self.step / 1000
        self.stats = StreamStats(rate_sps=GRAPH_RATES_SPS[rate_index], csv_path=csv_path)
        self.samples: deque[dict] = deque(maxlen=keep)
        self.warming = True
        self.pending: AdcQueueSample | None = None
        self.last_seq: int | None = None
        self._sums = [0.0, 0.0, 0.0]
        self._csv_file = open(csv_path, "w", newline="") if csv_path else None
        self._csv = csv.writer(self._csv_file) if self._csv_file else None
        if self._csv:
            self._csv.writerow(CSV_HEADER)

    def feed(self, samples: list[AdcQueueSample]) -> None:
        for s in samples:
            if self.warming:
                # After StartGraph the device may still deliver samples taken at the previous
                # rate; start counting once two consecutive samples show the expected step.
                if self.pending is not None and (s.sequence - self.pending.sequence) % 65536 == self.step:
                    self.warming = False
                    self._add(self.pending)
                    self._add(s)
                else:
                    self.pending = s
                continue
            self._add(s)

    def _add(self, s: AdcQueueSample) -> None:
        st = self.stats
        if self.last_seq is not None:
            diff = (s.sequence - self.last_seq) % 65536
            if diff > self.step:
                st.dropped_samples += diff // self.step - 1
        self.last_seq = s.sequence
        row = _sample_dict(s)
        p = row["power_w"]
        st.samples += 1
        st.vbus_min_v = s.vbus_v if st.vbus_min_v is None else min(st.vbus_min_v, s.vbus_v)
        st.vbus_max_v = s.vbus_v if st.vbus_max_v is None else max(st.vbus_max_v, s.vbus_v)
        st.ibus_min_a = s.ibus_a if st.ibus_min_a is None else min(st.ibus_min_a, s.ibus_a)
        st.ibus_max_a = s.ibus_a if st.ibus_max_a is None else max(st.ibus_max_a, s.ibus_a)
        st.power_max_w = p if st.power_max_w is None else max(st.power_max_w, p)
        self._sums[0] += s.vbus_v
        self._sums[1] += s.ibus_a
        self._sums[2] += p
        st.vbus_mean_v = self._sums[0] / st.samples
        st.ibus_mean_a = self._sums[1] / st.samples
        st.power_mean_w = self._sums[2] / st.samples
        st.energy_wh += p * self.dt / 3600
        st.charge_mah += s.ibus_a * self.dt / 3.6
        st.duration_s = (st.samples + st.dropped_samples) * self.dt
        self.samples.append(row)
        if self._csv:
            self._csv.writerow([row[k] for k in CSV_HEADER])

    def close(self) -> None:
        if self._csv_file:
            self._csv_file.close()
            self._csv_file = None
            self._csv = None


def pd_poll_interval_s() -> float:
    """PD poll interval: ``KM003C_PD_POLL_MS`` milliseconds if set and positive, else 50 ms.

    Values outside 1 ms to 10 s are clamped to that range, with a warning.
    """
    raw = os.environ.get("KM003C_PD_POLL_MS", "").strip()
    if not raw:
        return DEFAULT_PD_POLL_MS / 1000
    try:
        ms = float(raw)
    except ValueError:
        ms = 0
    if not ms > 0:
        log.warning("ignoring KM003C_PD_POLL_MS=%r; using %d ms", raw, DEFAULT_PD_POLL_MS)
        return DEFAULT_PD_POLL_MS / 1000
    clamped = min(max(ms, MIN_PD_POLL_MS), MAX_PD_POLL_MS)
    if clamped != ms:
        log.warning("KM003C_PD_POLL_MS=%r is outside %d to %d ms; using %g ms",
                    raw, MIN_PD_POLL_MS, MAX_PD_POLL_MS, clamped)
    return clamped / 1000


class _NoLease:
    def release(self) -> None:
        pass


@dataclass
class _PollOutcome:
    """What one poll did to the stream and the capture, kept apart."""

    pd_gen: int = 0
    stop_reason: str | None = None
    stream_bad: ProtocolError | None = None
    pd_bad: ProtocolError | None = None
    stream_failure: Exception | None = None
    pd_failure: Exception | None = None


class Recorder:
    def __init__(self, device_provider: Callable[[], Km003cDevice],
                 poll_interval_s: float | None = None, keep_samples: int = 10_000,
                 on_transport_error: Callable[[], None] | None = None,
                 lease: Callable[[str], object] | None = None) -> None:
        """``poll_interval_s`` defaults to :func:`pd_poll_interval_s`.

        ``lease(holder)`` returns an object with ``release()``. A running stream and a running
        PD capture each hold one, so the device owner keeps the meter claimed until they stop.
        """
        self._provider = device_provider
        self._take_lease = lease or (lambda holder: _NoLease())
        self._stream_lease = None
        self._pd_lease = None
        self._on_transport_error = on_transport_error
        self._poll = pd_poll_interval_s() if poll_interval_s is None else poll_interval_s
        self._keep = keep_samples
        self._state = threading.Lock()
        self._thread: threading.Thread | None = None
        self._device: Km003cDevice | None = None
        self._stream: _Stream | None = None
        self._last_stats: StreamStats | None = None
        self._stream_error: str | None = None
        self._pd_active = False
        self._pd_error: str | None = None
        # The running capture, or the last one once it stopped.
        self._pd: CaptureSession | None = None
        self._pd_changed = threading.Condition(self._state)
        # True from the moment a capture stops on its own until its cleanup (DisablePdMonitor,
        # lease release) is done; wait_pd returns only after that.
        self._pd_ending = False
        self._pd_monitor: PdMonitorEnable | None = None
        # (device, header "meter" block): the versions are read once per USB session.
        self._meter_cache: tuple[object, dict] | None = None
        # Bumped by every start, before it sends anything to the meter. A failed poller's
        # cleanup compares them to skip commands that would hit a newer stream or capture.
        self._stream_gen = 0
        self._pd_gen = 0
        # The latest PD status block the capture received, and when (time.monotonic()).
        self._pd_status: tuple[PdStatus, float] | None = None

    # --- lifecycle -------------------------------------------------------------------

    def _ensure_thread_locked(self) -> None:
        """Start the poller unless one is running; the caller holds ``_state``.

        The poller clears ``_thread`` under the same lock when it exits, so a start can
        never see a dying thread as running and leave its work unpolled.
        """
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="km003c-poller", daemon=True)
            self._thread.start()

    def _join_if_idle(self) -> None:
        thread = self._thread
        if thread is not None and self._stream is None and not self._pd_active:
            thread.join(timeout=2)

    def _run(self) -> None:
        me = threading.current_thread()
        stream_errors = pd_errors = 0
        while True:
            with self._state:
                if self._thread is not me:
                    return  # a failure handed polling to a newer thread
                mask = (ATT_ADCQUEUE if self._stream else 0) | (ATT_PD if self._pd_active else 0)
                device = self._device
                poll = self._poll
                if self._stream:
                    poll = min(poll, FAST_POLL_INTERVAL_S if self._stream.rate_index == 3
                               else STREAM_POLL_INTERVAL_S)
                if not mask or device is None:
                    self._thread = None
                    return
            try:
                frame_error: ProtocolError | None = None
                try:
                    packets = device.get_data(mask)
                except ProtocolError as exc:
                    packets, frame_error = [], exc
                outcome = self._ingest(packets, frame_error, mask)
                # Parse errors count per consumer: a bad AdcQueue block says nothing about the
                # PD data in the same reply, and the reverse.
                stream_errors = stream_errors + 1 if outcome.stream_bad else 0
                pd_errors = pd_errors + 1 if outcome.pd_bad else 0
                end_stream, end_pd = outcome.stream_failure, outcome.pd_failure
                if end_stream is None and stream_errors >= MAX_CONSECUTIVE_PROTOCOL_ERRORS:
                    end_stream = ProtocolError(f"{stream_errors} polls in a row failed to parse; "
                                               f"last: {outcome.stream_bad}")
                if end_pd is None and pd_errors >= MAX_CONSECUTIVE_PROTOCOL_ERRORS:
                    end_pd = ProtocolError(f"{pd_errors} polls in a row failed to parse; "
                                           f"last: {outcome.pd_bad}")
                if outcome.stream_bad or outcome.pd_bad:
                    log.warning("dropping data that failed to parse: %s",
                                outcome.stream_bad or outcome.pd_bad)
                if end_stream is not None:
                    stream_errors = 0
                    self._fail(end_stream, pd=False, last=False)
                if end_pd is not None:
                    pd_errors = 0
                    self._fail(end_pd, stream=False, last=False)
                elif outcome.stop_reason is not None:
                    self._end_pd(outcome.stop_reason, outcome.pd_gen)
                time.sleep(poll)
            except Exception as exc:  # noqa: BLE001 - any failure ends the capture and is reported
                self._fail(exc)
                return

    def _ingest(self, packets, frame_error: ProtocolError | None, mask: int) -> _PollOutcome:
        """Feed one reply to the stream and the capture, keeping each one's errors its own."""
        out = _PollOutcome()
        with self._state:
            out.pd_gen = self._pd_gen
            if frame_error is not None:
                out.stream_bad = frame_error if mask & ATT_ADCQUEUE and self._stream else None
                out.pd_bad = frame_error if mask & ATT_PD and self._pd_active else None
            for packet in packets:
                if packet.attribute == ATT_ADCQUEUE and self._stream:
                    try:
                        self._stream.feed(parse_adcqueue(packet.payload, self._stream.rate_index))
                    except ProtocolError as exc:
                        out.stream_bad = exc
                    except OSError as exc:  # the CSV file; the capture is not affected
                        out.stream_failure = exc
                elif packet.attribute == ATT_PD and self._pd_active:
                    if len(packet.payload) >= PD_STATUS_SIZE:  # for pd_status during a capture
                        status = parse_pd(packet.payload[:PD_STATUS_SIZE])
                        self._pd_status = (status, time.monotonic())
                    try:
                        out.stop_reason = self._pd.ingest(packet.payload) or out.stop_reason
                    except (OSError, ValueError) as exc:  # the capture file; not the stream
                        out.pd_failure = exc
                        continue
                    if self._pd.parse_error is not None:
                        # The session already wrote the error record with the payload.
                        out.pd_bad = ProtocolError(self._pd.parse_error)
            if self._pd_active and out.pd_failure is None:
                try:
                    if frame_error is not None and out.pd_bad is frame_error:
                        self._pd.error("ProtocolError", str(frame_error))
                    out.stop_reason = out.stop_reason or self._pd.check_timeout()
                except (OSError, ValueError) as exc:
                    out.pd_failure = exc
        return out

    def abort(self, reason: str) -> bool:
        """Mark any running stream/capture as failed without touching the device.

        Returns whether a stream was running.
        """
        with self._state:
            had_stream, _, leases = self._abort_locked(reason, ("aborted", reason))
        for lease in leases:
            lease.release()
        return had_stream

    def _abort_locked(self, reason: str, error: tuple[str, str], stream: bool = True,
                      pd: bool = True) -> tuple[bool, bool, list]:
        """Mark the stream and/or capture failed; return whether each ran and their leases.

        A running capture gets an ``error`` record (``error`` is its kind and message) and a
        footer with reason ``error``.
        """
        had_stream = stream and self._stream is not None
        had_pd = pd and self._pd_active
        leases = []
        if had_stream:
            try:
                self._stream.close()
            except OSError as exc:
                log.warning("could not close the stream's CSV file: %s", exc)
            self._last_stats = self._stream.stats
            self._stream = None
            self._stream_error = reason
        if stream and self._stream_lease is not None:
            leases.append(self._stream_lease)
            self._stream_lease = None
        if had_pd:
            self._pd_active = False
            self._pd_error = reason
            self._close_capture_locked("error", error)
        if pd and self._pd_lease is not None:
            leases.append(self._pd_lease)
            self._pd_lease = None
        return had_stream, had_pd, leases

    def _fail(self, exc: Exception, stream: bool = True, pd: bool = True,
              last: bool = True) -> None:
        """End the stream and/or capture after ``exc``; ``last`` when the poller then exits."""
        with self._state:
            had_stream, had_pd, leases = self._abort_locked(
                f"{type(exc).__name__}: {exc}", (type(exc).__name__, str(exc)), stream, pd)
            stream_gen, pd_gen = self._stream_gen, self._pd_gen
            if had_pd:
                self._pd_ending = True
            # Stop being the poller now: a start during the cleanup below gets a new thread.
            # A poller that keeps going for the other consumer stays the poller.
            if last and self._thread is threading.current_thread():
                self._thread = None
        device = self._device
        try:
            if isinstance(exc, TransportError):
                # The USB session is gone; let the owner drop it so the next call reopens.
                if self._on_transport_error is not None:
                    self._on_transport_error()
                return
            if device is None:
                return
            # The link still works, so stop the firmware stream and PD monitor instead of
            # leaving them running. Holding the device lock orders these commands against a
            # new start: one that began before the check below is skipped, a later one runs
            # after them.
            with device.lock:
                with self._state:
                    stream_current = self._stream_gen == stream_gen
                    pd_current = self._pd_gen == pd_gen
                if had_stream and stream_current:
                    try:
                        device.stop_graph()
                    except Exception:  # noqa: BLE001 - best effort
                        device.graph_rate = None
                if had_pd and pd_current:
                    try:
                        device.disable_pd_monitor()
                    except Exception:  # noqa: BLE001 - best effort
                        pass
        finally:
            # Only now may the owner release the meter: the cleanup above still uses it.
            for lease in leases:
                lease.release()
            if had_pd:
                self._pd_cleanup_done()

    # --- AdcQueue stream -------------------------------------------------------------

    def start_stream(self, rate_index: int, csv_path: str | None = None) -> None:
        if rate_index not in GRAPH_RATES_SPS:
            raise ValueError(f"rate index must be one of {sorted(GRAPH_RATES_SPS)}")
        with self._state:
            if self._stream is not None:
                raise Km003cError("a stream is already running; stop it first")
        # Open the CSV before starting the firmware stream so a bad path leaves nothing running.
        stream = _Stream(rate_index, csv_path, self._keep)
        with self._state:
            self._stream_gen += 1
        lease = self._take_lease("stream")
        try:
            device = self._provider()
            device.start_graph(rate_index)
        except BaseException:
            stream.close()
            lease.release()
            raise
        with self._state:
            self._device = device
            self._stream = stream
            self._stream_error = None
            previous, self._stream_lease = self._stream_lease, lease
            self._ensure_thread_locked()
        if previous is not None:
            previous.release()

    def stop_stream(self) -> StreamStats:
        with self._state:
            stream, self._stream = self._stream, None
            lease, self._stream_lease = self._stream_lease, None
            device = self._device
        if stream is None:
            if self._last_stats is None:
                raise Km003cError("no stream is running")
            return self._last_stats
        try:
            stream.close()
            self._last_stats = stream.stats
            self._join_if_idle()
            if device is not None:
                device.stop_graph()
        finally:
            if lease is not None:
                lease.release()
        return stream.stats

    def stream_status(self) -> dict:
        with self._state:
            stream = self._stream
            stats = stream.stats if stream else (self._last_stats or StreamStats())
            recent = list(stream.samples)[-RECENT_SAMPLES:] if stream else []
            return {"running": stream is not None, "error": self._stream_error,
                    "stats": stats.to_dict(), "recent": recent}

    def stream_samples(self, max_samples: int = 500) -> list[dict]:
        with self._state:
            samples = list(self._stream.samples) if self._stream else []
        if max_samples <= 0 or len(samples) <= max_samples:
            return samples
        step = len(samples) / max_samples
        return [samples[int(i * step)] for i in range(max_samples)]

    # --- PD capture ------------------------------------------------------------------

    def _close_capture_locked(self, reason: str, error: tuple[str, str] | None = None) -> None:
        """Write the capture's footer and wake :meth:`wait_pd`; the caller holds ``_state``."""
        try:
            self._pd.finish(reason, error)
        except (OSError, ValueError) as exc:
            log.warning("could not finish the capture file %s: %s", self._pd.path, exc)
        self._pd_changed.notify_all()

    @staticmethod
    def _meter_info(device) -> dict:
        """Serial, firmware and hardware version for the capture header.

        The versions come from AES memory reads, which the firmware refuses while streaming;
        they are None then, with the reason in ``error``. A broken link still raises.
        """
        info: dict = {"serial": getattr(device, "serial_number", None) or None,
                      "fw_version": None, "hw_version": None}
        read_info = getattr(device, "device_info", None)
        if read_info is None:
            return info
        try:
            details = read_info()
        except TransportError:
            raise
        except (Km003cError, ProtocolError) as exc:
            info["error"] = f"{type(exc).__name__}: {exc}"
            return info
        info.update(model=details.model or None, fw_version=details.fw_version or None,
                    hw_version=details.hw_version or None)
        return info

    def start_pd(self, path: str | None = None, label: str | None = None,
                 provenance: dict | None = None, *, stop_on: str = "none",
                 stop_after_messages: int | None = None, settle_ms: int = 0,
                 timeout_s: float = 60.0, status_interval_ms: int = 100) -> PdMonitorEnable:
        """Enable the meter's PD monitor and capture PD traffic to a new capture file.

        The capture stops on its own when ``stop_on`` or ``stop_after_messages`` is met (after
        ``settle_ms`` more meter time) or ``timeout_s`` passes; :attr:`capture_path` names its
        file. Raises PdMonitorNotEnabledError when the enable got neither Accept nor Reject;
        the file then holds a header, an error record and a footer. The enable outcome stays
        readable through :attr:`pd_monitor` until the next start.
        """
        validate_stop_options(stop_on, stop_after_messages, settle_ms, timeout_s,
                              status_interval_ms)
        with self._state:
            if self._pd_active:
                raise Km003cError("a PD capture is already running; stop it first")
        # Create the file before touching the meter, so a bad path leaves nothing running.
        session = CaptureSession(CaptureWriter(new_capture_path(path, label)), stop_on=stop_on,
                                 stop_after_messages=stop_after_messages, settle_ms=settle_ms,
                                 timeout_s=timeout_s, status_interval_ms=status_interval_ms)
        header = {"poll_ms": self._poll * 1000, "label": label, "provenance": provenance}
        with self._state:
            self._pd_gen += 1
        lease = self._take_lease("pd capture")
        meter: dict = {"serial": None, "fw_version": None, "hw_version": None}
        device = monitor = None
        try:
            device = self._provider()
            meter["serial"] = getattr(device, "serial_number", None) or None
            cached = self._meter_cache
            if cached is not None and cached[0] is device:
                meter = cached[1]
            else:
                meter = self._meter_info(device)
                if "error" not in meter:  # a refusal while streaming is retried next time
                    self._meter_cache = (device, meter)
            monitor = device.enable_pd_monitor()
            session.write_header(meter=meter, pd_monitor={"enabled": monitor.accepted,
                                                          **monitor.to_dict()}, **header)
        except BaseException as exc:
            try:
                self._abandon_start(session, meter, header, exc,
                                    device if monitor is not None else None)
            finally:
                lease.release()
            raise
        with self._state:
            lost = self._pd_active  # another start won the race
            if not lost:
                self._device = device
                self._pd = session
                self._pd_active = True
                self._pd_error = None
                self._pd_monitor = monitor
                self._pd_status = None
                previous, self._pd_lease = self._pd_lease, lease
                self._ensure_thread_locked()
        if lost:
            # The winner needs the monitor, so it stays enabled; only this file is closed.
            try:
                session.finish("error", ("Km003cError", "another PD capture started first"))
            except (OSError, ValueError) as exc:
                log.warning("could not finish the capture file %s: %s", session.path, exc)
            finally:
                lease.release()
            raise Km003cError("a PD capture is already running; stop it first")
        if previous is not None:
            previous.release()
        return monitor

    def _abandon_start(self, session: CaptureSession, meter: dict, header: dict,
                       exc: BaseException, enabled_on) -> None:
        """Clean up after a start that failed: finish and close its file, and disable the PD
        monitor when this start enabled it (``enabled_on`` is that device), unless a newer
        start has run since."""
        gen = self._pd_gen
        try:
            try:
                if not session.header_written:
                    session.write_header(meter=meter, pd_monitor=None, **header)
            finally:
                session.finish("error", (type(exc).__name__, str(exc)))
        except (OSError, ValueError) as error:
            log.warning("could not finish the capture file %s: %s", session.path, error)
        with self._state:
            if not self._pd_active:
                self._pd = session  # the failed attempt is the last capture
        if enabled_on is None:
            return
        with enabled_on.lock:
            with self._state:
                if self._pd_gen != gen or self._pd_active:
                    return
            try:
                enabled_on.disable_pd_monitor()
            except Exception as error:  # noqa: BLE001 - best effort; the start already failed
                log.warning("DisablePdMonitor after a failed start failed: %s", error)

    def _end_pd(self, reason: str, gen: int) -> None:
        """Stop the capture from the poller when its condition or timeout is met."""
        with self._state:
            if not self._pd_active or self._pd_gen != gen:
                return  # stopped or replaced meanwhile
            self._pd_active = False
            self._pd_ending = True
            lease, self._pd_lease = self._pd_lease, None
            device = self._device
            self._close_capture_locked(reason)
        try:
            if device is None:
                return
            with device.lock:
                with self._state:
                    if self._pd_gen != gen:
                        return  # a newer capture needs the monitor
                try:
                    device.disable_pd_monitor()
                except TransportError:
                    if self._on_transport_error is not None:
                        self._on_transport_error()
                except Km003cError as exc:
                    log.warning("DisablePdMonitor failed: %s", exc)
        finally:
            if lease is not None:
                lease.release()
            self._pd_cleanup_done()

    def _pd_cleanup_done(self) -> None:
        with self._state:
            self._pd_ending = False
            self._pd_changed.notify_all()

    def stop_pd(self) -> Path | None:
        """Stop the capture (footer reason ``manual``) and disable the PD monitor.

        Returns the path of the capture that was running, or None when none was. Waiters in
        :meth:`wait_pd` return once the monitor is disabled and the lease released. A capture
        started meanwhile keeps its monitor: the disable is skipped when a newer start
        already ran.
        """
        with self._state:
            was_active, self._pd_active = self._pd_active, False
            lease, self._pd_lease = self._pd_lease, None
            device = self._device
            gen = self._pd_gen
            if was_active:
                self._pd_ending = True
                self._close_capture_locked("manual")
            path = self._pd.path if was_active else None
        try:
            self._join_if_idle()
            if not was_active or device is None:
                return path
            # Holding the device lock orders the disable against a new start's enable, as in
            # _end_pd: a start that already ran is detected below, a later one runs after.
            with device.lock:
                with self._state:
                    if self._pd_gen != gen:
                        return path
                try:
                    device.disable_pd_monitor()
                except TransportError:
                    raise
                except Km003cError as exc:
                    log.warning("DisablePdMonitor failed: %s", exc)
        finally:
            if lease is not None:
                lease.release()
            if was_active:
                self._pd_cleanup_done()
        return path

    def wait_pd(self, timeout_s: float) -> bool:
        """Block until the capture stops or ``timeout_s`` passes; True if none is running.

        ``timeout_s`` is clamped to 0 to :data:`MAX_WAIT_S`; NaN counts as 0.
        """
        timeout_s = min(timeout_s, MAX_WAIT_S) if timeout_s >= 0 else 0.0
        deadline = time.monotonic() + timeout_s
        with self._pd_changed:
            while self._pd_active or self._pd_ending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._pd_changed.wait(remaining)
            return True

    def mark_pd(self, text: str) -> dict:
        """Write a ``mark`` record into the running capture."""
        with self._state:
            if not self._pd_active:
                raise Km003cError("no PD capture is running")
            return self._pd.mark(text)

    @property
    def pd_running(self) -> bool:
        with self._state:
            return self._pd_active

    @property
    def capture_path(self) -> Path | None:
        """The running capture's file, or the last one's after it stopped."""
        with self._state:
            return self._pd.path if self._pd is not None else None

    def pd_capture_state(self) -> dict:
        with self._state:
            session = self._pd
            return {"running": self._pd_active,
                    "path": str(session.path) if session else None,
                    "reason": session.reason if session else None,
                    "error": self._pd_error,
                    "messages": session.tracker.messages if session else 0}

    def latest_pd_status(self, wait_s: float = 0.0) -> tuple[PdStatus, float] | None:
        """The capture's latest PD status block and its age in seconds.

        Waits up to ``wait_s`` for the first block after a start. Returns None when no capture
        runs or no block arrived in time.
        """
        deadline = time.monotonic() + wait_s
        while True:
            with self._state:
                if not self._pd_active:
                    return None
                if self._pd_status is not None:
                    status, received = self._pd_status
                    return status, time.monotonic() - received
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.01)

    @property
    def pd_monitor(self) -> PdMonitorEnable | None:
        """Outcome of the enable sent by the most recent :meth:`start_pd`, if any."""
        with self._state:
            return self._pd_monitor
