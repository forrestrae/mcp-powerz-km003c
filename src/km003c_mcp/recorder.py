"""Background poller for AdcQueue streaming and PD event capture.

A single daemon thread polls the device while a stream and/or a PD capture is
active, combining both attributes into one GetData request per cycle.
"""

from __future__ import annotations

import csv
import logging
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Callable

from .pd import PdDecoder
from .protocol import (
    ATT_ADCQUEUE, ATT_PD, GRAPH_RATES_SPS, AdcQueueSample, PdEventStream, parse_adcqueue,
    parse_pd, sequence_step,
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
        self._pd_events: list[dict] = []
        self._pd_decoder = PdDecoder()
        self._pd_monitor: PdMonitorEnable | None = None
        # Bumped by every start, before it sends anything to the meter. A failed poller's
        # cleanup compares them to skip commands that would hit a newer stream or capture.
        self._stream_gen = 0
        self._pd_gen = 0

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
                packets = device.get_data(mask)
                with self._state:
                    for packet in packets:
                        if packet.attribute == ATT_ADCQUEUE and self._stream:
                            self._stream.feed(parse_adcqueue(packet.payload, self._stream.rate_index))
                        elif packet.attribute == ATT_PD and self._pd_active:
                            self._ingest_pd(packet.payload)
                time.sleep(poll)
            except Exception as exc:  # noqa: BLE001 - any failure ends the capture and is reported
                self._fail(exc)
                return

    def abort(self, reason: str) -> bool:
        """Mark any running stream/capture as failed without touching the device.

        Returns whether a stream was running.
        """
        with self._state:
            had_stream, _, leases = self._abort_locked(reason)
        for lease in leases:
            lease.release()
        return had_stream

    def _abort_locked(self, reason: str) -> tuple[bool, bool, list]:
        """Mark the stream and capture failed; return whether each ran and their leases."""
        had_stream = self._stream is not None
        had_pd = self._pd_active
        if self._stream:
            self._stream.close()
            self._last_stats = self._stream.stats
            self._stream = None
            self._stream_error = reason
        if self._pd_active:
            self._pd_active = False
            self._pd_error = reason
        leases = [lease for lease in (self._stream_lease, self._pd_lease) if lease is not None]
        self._stream_lease = self._pd_lease = None
        return had_stream, had_pd, leases

    def _fail(self, exc: Exception) -> None:
        with self._state:
            had_stream, had_pd, leases = self._abort_locked(f"{type(exc).__name__}: {exc}")
            stream_gen, pd_gen = self._stream_gen, self._pd_gen
            # Stop being the poller now: a start during the cleanup below gets a new thread.
            if self._thread is threading.current_thread():
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

    def _ingest_pd(self, payload: bytes) -> None:
        parsed = parse_pd(payload)
        if not isinstance(parsed, PdEventStream):
            return
        for event in parsed.events:
            self._pd_events.append({
                "index": len(self._pd_events),
                "timestamp_ms": event.timestamp_ms,
                "kind": event.kind,
                "sop": event.sop,
                "wire_hex": event.wire.hex(),
                "decoded": self._pd_decoder.decode(event),
            })

    def start_pd(self) -> PdMonitorEnable:
        """Enable the meter's PD monitor and start polling for PD events.

        Raises PdMonitorNotEnabledError when the enable got neither Accept nor Reject. The
        enable outcome stays readable through :attr:`pd_monitor` until the next start.
        """
        with self._state:
            self._pd_gen += 1
        lease = self._take_lease("pd capture")
        try:
            device = self._provider()
            monitor = device.enable_pd_monitor()
        except BaseException:
            lease.release()
            raise
        with self._state:
            self._device = device
            self._pd_active = True
            self._pd_error = None
            self._pd_events = []
            self._pd_decoder = PdDecoder()
            self._pd_monitor = monitor
            previous, self._pd_lease = self._pd_lease, lease
            self._ensure_thread_locked()
        if previous is not None:
            previous.release()
        return monitor

    def stop_pd(self) -> None:
        """Stop polling and disable the PD monitor; it does not survive a release anyway."""
        with self._state:
            was_active, self._pd_active = self._pd_active, False
            lease, self._pd_lease = self._pd_lease, None
            device = self._device
        try:
            self._join_if_idle()
            if not was_active or device is None:
                return
            try:
                device.disable_pd_monitor()
            except TransportError:
                raise
            except Km003cError as exc:
                log.warning("DisablePdMonitor failed: %s", exc)
        finally:
            if lease is not None:
                lease.release()

    @property
    def pd_monitor(self) -> PdMonitorEnable | None:
        """Outcome of the enable sent by the most recent :meth:`start_pd`, if any."""
        with self._state:
            return self._pd_monitor

    def pd_events(self, since: int = 0, limit: int = 100, decode: bool = True) -> dict:
        with self._state:
            selected = self._pd_events[max(since, 0):max(since, 0) + max(limit, 0)]
            events = [e if decode else {k: v for k, v in e.items() if k != "decoded"}
                      for e in selected]
            return {"events": events, "next": max(since, 0) + len(selected),
                    "total": len(self._pd_events), "running": self._pd_active,
                    "error": self._pd_error}
