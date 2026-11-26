"""Capture files in the ``km003c-capture`` format, version 1, and the live capture session.

A capture file is JSON Lines: one record per line, each written and flushed as it arrives,
so a crash keeps everything before it. Records keep the meter's raw bytes and only a header
summary of each PD message; full decodes are computed when a file is read
(:mod:`km003c_mcp.capture_read`), so decoder fixes apply to old captures.

Record types: ``header``, ``status``, ``pd``, ``conn``, ``mark``, ``error``, ``footer``.
``docs/pd-capture.md`` describes every field.
"""

from __future__ import annotations

import json
import os
import re
import struct
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Callable

from .pd import SOP_NAMES, message_header
from .protocol import PD_STATUS_SIZE, PdEvent, split_pd

FORMAT = "km003c-capture"
VERSION = 1
STOP_ON = ("none", "contract", "identity")
STOP_REASONS = ("messages", "contract", "identity", "timeout", "manual", "error")
# Requests and the replies that end one; see StopTracker.
_REQUESTS = {"Request", "EPR_Request"}
_REQUEST_ENDS = {"Reject", "Wait", "Soft_Reset", "Not_Supported"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def server_version() -> str:
    try:
        return metadata.version("km003c-mcp")
    except metadata.PackageNotFoundError:
        return "unknown"


# --- file location -----------------------------------------------------------------------

_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


def capture_dir() -> Path:
    """``$KM003C_CAPTURE_DIR``, else ``captures`` under the current working directory."""
    raw = os.environ.get("KM003C_CAPTURE_DIR", "").strip()
    return Path(raw).expanduser() if raw else Path.cwd() / "captures"


def new_capture_path(path: str | None = None, label: str | None = None,
                     now: datetime | None = None) -> Path:
    """Absolute path for a new capture file; creates its directory.

    An explicit ``path`` names the file, or a directory when it exists as one or ends with a
    path separator. An existing file is never overwritten. Without ``path`` the file goes in
    :func:`capture_dir`, named from the UTC start time and the label.
    """
    now = now or datetime.now(timezone.utc)
    stem = "km003c-" + now.strftime("%Y%m%dT%H%M%SZ")
    slug = _SLUG.sub("-", label or "").strip("-.")[:40]
    if slug:
        stem += "-" + slug
    if path:
        target = Path(path).expanduser()
        if not (target.is_dir() or path.endswith(("/", os.sep))):
            target = target.resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise FileExistsError(f"capture file already exists: {target}")
            return target
        directory = target
    else:
        directory = capture_dir()
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{stem}.jsonl"
    n = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{n}.jsonl"
        n += 1
    return candidate


# --- writer ------------------------------------------------------------------------------

class CaptureWriter:
    """Appends records to a new capture file, flushing each line. Thread-safe."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._file = open(self.path, "x", encoding="utf-8")
        self._lock = threading.Lock()

    @property
    def closed(self) -> bool:
        return self._file is None

    def write(self, record: dict) -> None:
        line = json.dumps(record, separators=(",", ":")) + "\n"
        with self._lock:
            if self._file is None:
                raise ValueError(f"capture file is closed: {self.path}")
            self._file.write(line)
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            file, self._file = self._file, None
        if file is not None:
            file.close()


# --- stop conditions ---------------------------------------------------------------------

def is_discover_identity_ack(wire: bytes) -> bool:
    """Whether ``wire`` is a structured Vendor_Defined Discover Identity ACK."""
    if len(wire) < 6:
        return False
    header = int.from_bytes(wire[:2], "little")
    if header >> 15 or (header >> 12) & 7 == 0 or header & 0x1F != 15:
        return False
    vdm = int.from_bytes(wire[2:6], "little")
    return bool(vdm & 1 << 15) and vdm & 0x1F == 1 and (vdm >> 6) & 3 == 1


class RetryFilter:
    """Recognizes retransmissions.

    A port that gets no GoodCRC sends the same message again: the same bytes, so the same
    MessageID, from the same sender (power and data role bits, or cable plug) on the same
    SOP. A GoodCRC on that SOP ends the wait, so a later message with the same bytes is new.
    Feed every message, GoodCRC included, in capture order.
    """

    def __init__(self) -> None:
        self._last: dict[tuple[int | None, int], bytes] = {}

    def is_retry(self, sop: int | None, wire: bytes, goodcrc: bool) -> bool:
        if goodcrc:
            for key in [k for k in self._last if k[0] == sop]:
                del self._last[key]
            return False
        if len(wire) < 2:
            return False
        key = (sop, int.from_bytes(wire[:2], "little") & 0x0120)
        retry = self._last.get(key) == wire
        self._last[key] = wire
        return retry


class StopTracker:
    """Watches non-GoodCRC messages for the capture's stop condition.

    ``contract``: a PS_RDY on SOP after an Accept of a Request (or EPR_Request); a Reject,
    Wait, Soft_Reset, or Not_Supported in between starts over. ``identity``: a Discover
    Identity ACK on SOP, from either port. ``stop_after_messages`` counts non-GoodCRC
    messages on every SOP. The session leaves retransmissions out (see RetryFilter).
    """

    def __init__(self, stop_on: str = "none", stop_after_messages: int | None = None) -> None:
        self.stop_on = stop_on
        self.stop_after_messages = stop_after_messages
        self.messages = 0
        self._contract = 0  # 0 idle, 1 Request seen, 2 Accept seen

    def feed(self, sop: int | None, wire: bytes, msg: str | None) -> str | None:
        """Account for one non-GoodCRC message; return the stop reason it triggers, if any."""
        self.messages += 1
        reason = None
        if sop == 0 and self.stop_on == "contract":
            if msg in _REQUESTS:
                self._contract = 1
            elif msg == "Accept" and self._contract == 1:
                self._contract = 2
            elif msg == "PS_RDY" and self._contract == 2:
                reason = "contract"
            elif msg in _REQUEST_ENDS:
                self._contract = 0
        elif sop == 0 and self.stop_on == "identity" and is_discover_identity_ack(wire):
            reason = "identity"
        if reason is None and self.stop_after_messages is not None \
                and self.messages >= self.stop_after_messages:
            reason = "messages"
        return reason


# --- session -----------------------------------------------------------------------------

def status_fields(raw: bytes) -> dict:
    """The 12-byte PD status block as integers: uptime ms, VBUS mV, IBUS mA, CC1/CC2 mV."""
    t, vbus, ibus, cc1, cc2 = struct.unpack_from("<IHhHH", raw)
    return {"t_meter_ms": t, "vbus_mv": vbus, "ibus_ma": ibus, "cc1_mv": cc1, "cc2_mv": cc2}


class VbusStats:
    def __init__(self) -> None:
        self.first = self.last = self.min = self.max = None

    def add(self, mv: int) -> None:
        if self.first is None:
            self.first = self.min = self.max = mv
        self.last = mv
        self.min = min(self.min, mv)
        self.max = max(self.max, mv)

    def to_dict(self) -> dict:
        return {"first": self.first, "last": self.last, "min": self.min, "max": self.max}


class CaptureSession:
    """One capture: turns PdPacket payloads into records and decides when to stop.

    The recorder's poller calls :meth:`ingest` with each payload and :meth:`check_timeout`
    after each poll, and calls :meth:`finish` once with the stop reason. ``status_interval_ms``
    and ``settle_ms`` are measured in meter time, taken from the status blocks, so a replayed
    capture stops at the same record; ``timeout_s`` is host time.
    """

    def __init__(self, writer: CaptureWriter, *, stop_on: str = "none",
                 stop_after_messages: int | None = None, settle_ms: int = 0,
                 timeout_s: float = 60.0, status_interval_ms: int = 100,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time) -> None:
        validate_stop_options(stop_on, stop_after_messages, settle_ms, timeout_s,
                              status_interval_ms)
        self.writer = writer
        self.stop_on = stop_on
        self.stop_after_messages = stop_after_messages
        self.settle_ms = settle_ms
        self.timeout_s = timeout_s
        self.status_interval_ms = status_interval_ms
        self._clock = clock
        self._wall = wall
        self.deadline = clock() + timeout_s
        self.tracker = StopTracker(stop_on, stop_after_messages)
        self.retries = RetryFilter()
        self.counts: Counter[str] = Counter()
        self.vbus = VbusStats()
        self.pd_index = 0
        self.conn = 0
        self.marks = 0
        self.errors = 0
        self.fired: str | None = None
        self.settle_until: int | None = None
        self.reason: str | None = None
        self.header_written = False
        # Why the last payload passed to ingest() failed to parse, or None.
        self.parse_error: str | None = None
        self._last_status_t: int | None = None

    @property
    def path(self) -> Path:
        return self.writer.path

    @property
    def finished(self) -> bool:
        return self.reason is not None

    def stop_options(self) -> dict:
        return {"stop_on": self.stop_on, "stop_after_messages": self.stop_after_messages,
                "settle_ms": self.settle_ms, "timeout_s": self.timeout_s,
                "status_interval_ms": self.status_interval_ms}

    def write_header(self, *, meter: dict, poll_ms: float, pd_monitor: dict | None,
                     label: str | None, provenance: dict | None,
                     started_utc: str | None = None) -> None:
        self.writer.write({
            "type": "header", "format": FORMAT, "version": VERSION,
            "started_utc": started_utc or utc_now(), "meter": meter,
            "server": {"version": server_version(), "poll_ms": poll_ms},
            "pd_monitor": pd_monitor, "label": label, "provenance": provenance,
            "stop": self.stop_options(),
        })
        self.header_written = True

    def ingest(self, payload: bytes) -> str | None:
        """Write the records of one poll; return the stop reason if the capture is done.

        The poll's status block is written first when it carries events or the status
        interval has passed, and also last when this poll ends the capture.
        """
        t_host = self._wall()
        split = split_pd(payload)
        self.parse_error = split.error
        meter_t = None
        status = None
        if len(split.status_raw) == PD_STATUS_SIZE:
            fields = status_fields(split.status_raw)
            meter_t = fields["t_meter_ms"]
            self.vbus.add(fields["vbus_mv"])
            status = {"type": "status", "t_host": t_host, **fields,
                      "raw": split.status_raw.hex()}
            last = self._last_status_t
            if split.events or last is None or not 0 <= meter_t - last < self.status_interval_ms:
                self.writer.write(status)
                self._last_status_t = meter_t
                status = None
        for event in split.events:
            self._write_event(event)
        if split.error is not None:
            self.error("ProtocolError", split.error, payload=payload.hex(), t_host=t_host)
        if self.fired is None or (self.settle_until is not None
                                  and (meter_t is None or meter_t < self.settle_until)):
            return None
        if status is not None:
            self.writer.write(status)
            self._last_status_t = meter_t
        return self.fired

    def _write_event(self, event: PdEvent) -> None:
        if event.kind != "message":
            self.conn += 1
            self.writer.write({"type": "conn", "t_meter_ms": event.timestamp_ms,
                               "code": event.code, "event": event.kind,
                               "raw_event": event.raw.hex()})
            return
        sop = event.sop if event.sop is not None else 0
        header = message_header(event.wire, sop)
        self.writer.write({"type": "pd", "i": self.pd_index, "t_meter_ms": event.timestamp_ms,
                           "sop": SOP_NAMES.get(sop, f"SOP?{sop}"), "wire": event.wire.hex(),
                           "raw_event": event.raw.hex(), **header})
        self.pd_index += 1
        msg = header["msg"]
        self.counts[msg or "invalid"] += 1
        if self.retries.is_retry(sop, event.wire, msg == "GoodCRC") or msg == "GoodCRC":
            return
        reason = self.tracker.feed(sop, event.wire, msg)
        if reason is not None and self.fired is None:
            self.fired = reason
            if self.settle_ms:
                self.settle_until = event.timestamp_ms + self.settle_ms

    def check_timeout(self) -> str | None:
        """``timeout`` once ``timeout_s`` has passed; a condition met during its settle
        time keeps its own reason."""
        if self._clock() < self.deadline:
            return None
        return self.fired or "timeout"

    def mark(self, text: str) -> dict:
        record = {"type": "mark", "t_host": self._wall(), "text": text}
        self.writer.write(record)
        self.marks += 1
        return record

    def error(self, kind: str, message: str, payload: str | None = None,
              t_host: float | None = None) -> None:
        record = {"type": "error", "t_host": self._wall() if t_host is None else t_host,
                  "kind": kind, "message": message}
        if payload is not None:
            record["payload"] = payload
        self.errors += 1
        self.writer.write(record)

    def finish(self, reason: str, error: tuple[str, str] | None = None) -> None:
        """Write the footer (after an ``error`` record when given) and close the file.

        Safe to call twice; the second call does nothing. A failing write still closes the
        file, so a full disk cannot keep it open.
        """
        if self.reason is not None:
            return
        self.reason = reason
        try:
            if error is not None:
                self.error(*error)
            self.writer.write({
                "type": "footer", "stopped_utc": utc_now(), "reason": reason,
                "counts": dict(sorted(self.counts.items())),
                "messages": self.tracker.messages, "conn": self.conn, "marks": self.marks,
                "errors": self.errors, "vbus_mv": self.vbus.to_dict(),
            })
        finally:
            self.writer.close()


def validate_stop_options(stop_on: str, stop_after_messages: int | None, settle_ms: int,
                          timeout_s: float, status_interval_ms: int) -> None:
    if stop_on not in STOP_ON:
        raise ValueError(f"stop_on must be one of {', '.join(STOP_ON)}")
    if stop_after_messages is not None and stop_after_messages < 1:
        raise ValueError("stop_after_messages must be at least 1")
    if settle_ms < 0:
        raise ValueError("settle_ms must not be negative")
    if not timeout_s > 0:
        raise ValueError("timeout_s must be positive")
    if status_interval_ms < 0:
        raise ValueError("status_interval_ms must not be negative")
