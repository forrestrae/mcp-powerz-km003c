"""Pure (I/O-free) encoding and parsing of the KM003C vendor bulk protocol.

All multi-byte values are little-endian. Field layouts follow the community
reverse-engineering work (okhsunrog/km003c-protocol-research) and were checked
against a live device.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

CMD_CONNECT = 0x02
CMD_DISCONNECT = 0x03
CMD_ACCEPT = 0x05
CMD_REJECT = 0x06
CMD_GET_DATA = 0x0C
CMD_START_GRAPH = 0x0E
CMD_STOP_GRAPH = 0x0F
CMD_ENABLE_PD_MONITOR = 0x10
CMD_DISABLE_PD_MONITOR = 0x11
CMD_NOT_READABLE = 0x27
CMD_PUT_DATA = 0x41
CMD_MEMORY_READ = 0x44
CMD_STREAMING_AUTH = 0x4C

ATT_ADC = 0x0001
ATT_ADCQUEUE = 0x0002
ATT_SETTINGS = 0x0008
ATT_PD = 0x0010

GRAPH_RATES_SPS = {0: 2, 1: 10, 2: 50, 3: 1000}

ADC_PAYLOAD_SIZE = 44
TEMP_INVALID_RAW = -32768


class ProtocolError(Exception):
    """Raised when a packet cannot be decoded."""


def encode_ctrl(cmd: int, tid: int, attribute: int = 0) -> bytes:
    """Encode a control header; the 15-bit attribute starts at bit 17."""
    word = (cmd & 0x7F) | ((tid & 0xFF) << 8) | ((attribute & 0x7FFF) << 17)
    return struct.pack("<I", word)


def encode_raw(cmd: int, tid: int, word: int) -> bytes:
    """Encode a header whose upper 16 bits are a raw word (MemoryRead, StreamingAuth)."""
    return bytes([cmd & 0xFF, tid & 0xFF]) + (word & 0xFFFF).to_bytes(2, "little")


@dataclass(frozen=True)
class Header:
    cmd: int
    flag: int
    tid: int
    attribute: int
    obj_count: int
    raw_word: int


def decode_header(data: bytes) -> Header:
    if len(data) < 4:
        raise ProtocolError(f"packet too short for a header: {len(data)} bytes")
    word = struct.unpack_from("<I", data)[0]
    return Header(
        cmd=word & 0x7F,
        flag=(word >> 7) & 1,
        tid=(word >> 8) & 0xFF,
        attribute=(word >> 17) & 0x7FFF,
        obj_count=word >> 22,
        raw_word=struct.unpack_from("<H", data, 2)[0],
    )


@dataclass(frozen=True)
class LogicalPacket:
    attribute: int
    next: bool
    chunk: int
    payload: bytes


def parse_putdata(data: bytes) -> tuple[Header, list[LogicalPacket]]:
    """Split a PutData response into its chained logical packets."""
    header = decode_header(data)
    packets: list[LogicalPacket] = []
    offset = 4
    while offset < len(data):
        if len(data) - offset < 4:
            raise ProtocolError(f"truncated extended header at offset {offset}")
        word = struct.unpack_from("<I", data, offset)[0]
        attribute = word & 0x7FFF
        chunk = (word >> 16) & 0x3F
        size = word >> 22
        if attribute == ATT_ADCQUEUE:
            # AdcQueue: `chunk` is the sample count and `size` the bytes per sample.
            size *= chunk
        offset += 4
        if len(data) - offset < size:
            raise ProtocolError(
                f"logical packet needs {size} bytes at offset {offset}, got {len(data) - offset}")
        packet = LogicalPacket(
            attribute=attribute,
            next=bool((word >> 15) & 1),
            chunk=chunk,
            payload=bytes(data[offset:offset + size]),
        )
        packets.append(packet)
        offset += size
        if not packet.next:
            break
    return header, packets


@dataclass(frozen=True)
class AdcReading:
    vbus_v: float
    ibus_a: float
    power_w: float
    vbus_avg_v: float
    ibus_avg_a: float
    vbus_ori_avg_v: float
    ibus_ori_avg_a: float
    temp_c: float | None
    cc1_v: float
    cc2_v: float
    dp_v: float
    dm_v: float
    vdd_v: float
    rate_index: int
    flags: int
    cc2_avg_v: float
    dp_avg_v: float
    dm_avg_v: float


def parse_adc(payload: bytes) -> AdcReading:
    if len(payload) < ADC_PAYLOAD_SIZE:
        raise ProtocolError(f"ADC payload must be {ADC_PAYLOAD_SIZE} bytes, got {len(payload)}")
    (vbus, ibus, vbus_avg, ibus_avg, vbus_ori, ibus_ori, temp,
     cc1, cc2, dp, dm, vdd, rate, flags, cc2_avg, dp_avg, dm_avg) = struct.unpack_from(
        "<iiiiiihHHHHHBBHHH", payload)
    vbus_v = vbus / 1e6
    ibus_a = ibus / 1e6
    return AdcReading(
        vbus_v=vbus_v,
        ibus_a=ibus_a,
        power_w=vbus_v * ibus_a,
        vbus_avg_v=vbus_avg / 1e6,
        ibus_avg_a=ibus_avg / 1e6,
        vbus_ori_avg_v=vbus_ori / 1e6,
        ibus_ori_avg_a=ibus_ori / 1e6,
        temp_c=None if temp == TEMP_INVALID_RAW else temp / 128,
        cc1_v=cc1 / 1e4,
        cc2_v=cc2 / 1e4,
        dp_v=dp / 1e4,
        dm_v=dm / 1e4,
        vdd_v=vdd / 1e4,
        rate_index=rate,
        flags=flags,
        cc2_avg_v=cc2_avg / 1e3,
        dp_avg_v=dp_avg / 1e3,
        dm_avg_v=dm_avg / 1e3,
    )


ADCQUEUE_SAMPLE_SIZE = 20
_SEQUENCE_STEPS = {0: 500, 1: 100, 2: 20, 3: 1}


def sequence_step(rate_index: int) -> int:
    """Expected increment of the device's 1 kHz sample counter per AdcQueue sample."""
    return _SEQUENCE_STEPS[rate_index]


@dataclass(frozen=True)
class AdcQueueSample:
    sequence: int
    marker: int
    vbus_v: float
    ibus_a: float
    cc1_v: float
    cc2_v: float
    dp_v: float
    dm_v: float


def parse_adcqueue(payload: bytes, rate_index: int) -> list[AdcQueueSample]:
    if len(payload) % ADCQUEUE_SAMPLE_SIZE:
        raise ProtocolError(
            f"AdcQueue payload length {len(payload)} is not a multiple of {ADCQUEUE_SAMPLE_SIZE}")
    # Auxiliary voltages are 0.1 mV/count at 2 SPS and 1 mV/count at faster rates.
    aux_scale = 1e4 if rate_index == 0 else 1e3
    samples = []
    for offset in range(0, len(payload), ADCQUEUE_SAMPLE_SIZE):
        seq, marker, vbus, ibus, cc1, cc2, dp, dm = struct.unpack_from("<HHiiHHHH", payload, offset)
        samples.append(AdcQueueSample(
            sequence=seq,
            marker=marker,
            vbus_v=vbus / 1e6,
            ibus_a=ibus / 1e6,
            cc1_v=cc1 / aux_scale,
            cc2_v=cc2 / aux_scale,
            dp_v=dp / aux_scale,
            dm_v=dm / aux_scale,
        ))
    return samples


PD_STATUS_SIZE = 12
PD_EVENT_HEADER_SIZE = 6
PD_EVENT_CONNECTION = 0x45
# Firmware 1.8.x and 2.0.0 use 0x11/0x12 for the same attach/detach records.
PD_EVENT_CONNECT = (0x21, 0x11)
PD_EVENT_DISCONNECT = (0x22, 0x12)


@dataclass(frozen=True)
class PdStatus:
    timestamp_ms: int
    vbus_v: float
    ibus_a: float
    cc1_v: float
    cc2_v: float


@dataclass(frozen=True)
class PdEvent:
    """One event record of a PdPacket payload.

    ``kind`` is ``connect``, ``disconnect``, ``connection`` (an unknown connection code), or
    ``message``. ``code`` is the connection-event code byte, None for messages. ``raw`` is the
    record as received: the 6-byte event header followed by the wire bytes. Both kinds carry
    the meter's 32-bit uptime in milliseconds in header bytes 1 to 4.
    """

    timestamp_ms: int
    kind: str
    sop: int | None
    wire: bytes
    code: int | None = None
    raw: bytes = b""


@dataclass(frozen=True)
class PdEventStream:
    preamble: PdStatus
    events: list[PdEvent]


@dataclass(frozen=True)
class PdPayload:
    """A PdPacket payload split leniently: what parsed, and why the rest did not.

    ``status`` is None when the payload is shorter than the 12-byte status block. ``error``
    describes the first record that could not be parsed; the records after it are lost,
    because the record length comes from the broken header.
    """

    status: PdStatus | None
    status_raw: bytes
    events: list[PdEvent]
    error: str | None


def _parse_pd_status(payload: bytes) -> PdStatus:
    ts, vbus, ibus, cc1, cc2 = struct.unpack_from("<IHhHH", payload)
    return PdStatus(ts, vbus / 1000, ibus / 1000, cc1 / 1000, cc2 / 1000)


def split_pd(payload: bytes) -> PdPayload:
    """Parse a PdPacket attribute without raising: keep every record before a corrupt one."""
    if len(payload) < PD_STATUS_SIZE:
        return PdPayload(None, bytes(payload), [], f"PD payload too short: {len(payload)} bytes")
    status = _parse_pd_status(payload)
    events: list[PdEvent] = []
    offset = PD_STATUS_SIZE
    while offset < len(payload):
        if len(payload) - offset < PD_EVENT_HEADER_SIZE:
            return PdPayload(status, bytes(payload[:PD_STATUS_SIZE]), events,
                             f"truncated PD event header at offset {offset}")
        head = bytes(payload[offset:offset + PD_EVENT_HEADER_SIZE])
        if head[0] == PD_EVENT_CONNECTION:
            # Bytes 1 to 4 are the 32-bit uptime, as for messages; byte 5 is the code. A
            # KM003C on firmware 2.0.0 past 2^24 ms of uptime sent 45 0e ef 63 02 11 with its
            # status block at 40103694 ms (0x0263ef0e), so the timestamp is not 24 bits.
            ts = struct.unpack_from("<I", head, 1)[0]
            code = head[5]
            kind = ("connect" if code in PD_EVENT_CONNECT
                    else "disconnect" if code in PD_EVENT_DISCONNECT else "connection")
            events.append(PdEvent(ts, kind, None, b"", code, head))
            offset += PD_EVENT_HEADER_SIZE
            continue
        wire_len = (head[0] & 0x3F) - 5
        if wire_len < 0:
            return PdPayload(status, bytes(payload[:PD_STATUS_SIZE]), events,
                             f"invalid PD event size flag 0x{head[0]:02x} at offset {offset}")
        start = offset + PD_EVENT_HEADER_SIZE
        if len(payload) - start < wire_len:
            return PdPayload(status, bytes(payload[:PD_STATUS_SIZE]), events,
                             f"PD event needs {wire_len} wire bytes at offset {start}")
        wire = bytes(payload[start:start + wire_len])
        ts = struct.unpack_from("<I", head, 1)[0]
        events.append(PdEvent(ts, "message", head[5], wire, None, head + wire))
        offset = start + wire_len
    return PdPayload(status, bytes(payload[:PD_STATUS_SIZE]), events, None)


def parse_pd(payload: bytes) -> PdStatus | PdEventStream:
    """Parse a PdPacket attribute: a 12-byte status, or a status preamble plus events.

    Raises :class:`ProtocolError` on a corrupt payload; :func:`split_pd` keeps what parsed.
    """
    split = split_pd(payload)
    if split.error is not None:
        raise ProtocolError(split.error)
    if len(payload) == PD_STATUS_SIZE:
        return split.status
    return PdEventStream(split.status, split.events)
