"""Shared fixtures and fake transports for the KM003C tests."""

import threading

# GetData(ADC) response captured live from a KM003C (SN 014270) with no load attached.
LIVE_ADC = bytes.fromhex(
    "41028002" "0100000b"
    "00080000 65feffff 3b0d0000 f5ffffff 9f0d0000 ddffffff"
    "0080 0c03 6e01 4803 6e03 f87e 00 80 2300 5300 5600".replace(" ", ""))


def live_adc_with_tid(tid: int) -> bytes:
    data = bytearray(LIVE_ADC)
    data[1] = tid
    return bytes(data)


class FakeTransport:
    """Scripted UsbTransport: each read() pops the next scripted reply."""

    def __init__(self, script, serial_number="TEST"):
        self.script = list(script)
        self.writes: list[bytes] = []
        self.serial_number = serial_number
        self.closed = False
        self.resets = 0

    def write(self, data: bytes, timeout_ms: int) -> None:
        self.writes.append(bytes(data))

    def read(self, size: int, timeout_ms: int) -> bytes:
        from km003c_mcp.usb_device import DeviceTimeoutError
        if not self.script:
            raise DeviceTimeoutError("fake transport: no scripted reply")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True

    def reset(self) -> None:
        self.resets += 1
        self.closed = True


def _cstr_block(size: int, fields: dict[int, str]) -> bytes:
    block = bytearray(size)
    for offset, text in fields.items():
        block[offset:offset + len(text)] = text.encode()
    return bytes(block)


# Memory blocks of a KM003C on firmware 2.0.0, hardware 2.3 (see crypto.parse_device_info).
METER_MEMORY = {
    0x00000420: _cstr_block(64, {0x10: "KM003C", 0x1C: "2.3", 0x28: "2025.4.15"}),
    0x00004420: b"\x01\x00\x00\x00" + _cstr_block(60, {0x18: "2.0.0", 0x24: "2025.2.20"}),
    0x03000C00: _cstr_block(64, {0x00: "053893"}),
    0x40010450: bytes.fromhex("3037314b42500dff110affff"),
}
PD_STATUS_STEP_MS = 15


class MeterFake:
    """UsbTransport that answers like an idle KM003C on firmware 2.0.0.

    Each request queues its reply when written: Accept for Connect, StartGraph and the PD
    monitor commands, Reject for StopGraph, the live ADC block or an empty PutData for
    GetData, the blocks of :data:`METER_MEMORY` for MemoryRead, and ``03 11 00 00`` for
    Disconnect. ``hold_disconnect`` is an Event the Disconnect reply waits for. ``on_close``
    runs when the session is released.

    ``pd_payloads`` are PdPacket payloads returned in order, one per GetData that asks for
    the PD attribute. Once they run out, each poll returns the last status block with its
    uptime advanced by 15 ms, as an idle meter does.
    """

    def __init__(self, serial_number="TEST", hold_disconnect: threading.Event | None = None,
                 on_close=None, pd_payloads=()):
        self.serial_number = serial_number
        self.pd_payloads = [bytes(p) for p in pd_payloads]
        self.pd_polls = 0
        self._last_status: bytes | None = None
        self.hold_disconnect = hold_disconnect
        self.on_close = on_close
        self.writes: list[bytes] = []
        self.writes_after_close: list[bytes] = []
        self.pending: list[bytes] = []
        self.closed = False
        self.resets = 0
        self._lock = threading.Lock()

    def commands(self) -> list[int]:
        return [w[0] & 0x7F for w in self.writes]

    def write(self, data: bytes, timeout_ms: int) -> None:
        import struct

        from km003c_mcp import protocol as p
        if self.closed:
            self.writes_after_close.append(bytes(data))
        self.writes.append(bytes(data))
        cmd, tid = data[0] & 0x7F, data[1]
        attribute = (struct.unpack_from("<I", data)[0] >> 17) & 0x7FFF
        if cmd == p.CMD_STOP_GRAPH:
            reply = bytes([p.CMD_REJECT, tid, 0, 0])
        elif cmd == p.CMD_DISCONNECT:
            if self.hold_disconnect is not None:
                self.hold_disconnect.wait(5)
            reply = bytes.fromhex("03110000")
        elif cmd == p.CMD_GET_DATA and attribute & p.ATT_ADC:
            reply = live_adc_with_tid(tid)
        elif cmd == p.CMD_GET_DATA and attribute & p.ATT_PD and (
                self.pd_payloads or self._last_status):
            payload = self._next_pd_payload()
            ext = struct.pack("<I", p.ATT_PD | (len(payload) << 22))
            reply = bytes([p.CMD_PUT_DATA, tid, 0, 0]) + ext + payload
        elif cmd == p.CMD_MEMORY_READ:
            from km003c_mcp.crypto import KEY_MEMORY, aes_ecb
            address, size = struct.unpack_from("<II", aes_ecb(data[4:20], KEY_MEMORY, True))
            with self._lock:
                self.pending.extend(memory_read_replies(tid, address,
                                                        METER_MEMORY[address][:size]))
            return
        elif cmd == p.CMD_GET_DATA:
            reply = bytes([p.CMD_PUT_DATA, tid, 0, 0])
        else:
            reply = bytes([p.CMD_ACCEPT, tid, 0, 0])
        with self._lock:
            self.pending.append(reply)

    def _next_pd_payload(self) -> bytes:
        import struct
        self.pd_polls += 1
        if self.pd_payloads:
            payload = self.pd_payloads.pop(0)
            if len(payload) >= 12:
                self._last_status = payload[:12]
            return payload
        uptime = struct.unpack_from("<I", self._last_status)[0] + PD_STATUS_STEP_MS
        self._last_status = struct.pack("<I", uptime) + self._last_status[4:]
        return self._last_status

    def read(self, size: int, timeout_ms: int) -> bytes:
        from km003c_mcp.usb_device import DeviceTimeoutError
        with self._lock:
            if self.pending:
                return self.pending.pop(0)
        raise DeviceTimeoutError("fake meter: no reply pending")

    def close(self) -> None:
        if self.on_close is not None:
            self.on_close(self)
        self.closed = True

    def reset(self) -> None:
        self.resets += 1
        self.closed = True


def memory_read_replies(tid: int, address: int, plain: bytes, chunk: int = 16) -> list[bytes]:
    """Confirmation packet plus raw AES chunks for a MemoryRead of len(plain) bytes."""
    import binascii
    import struct

    from km003c_mcp.crypto import KEY_MEMORY, aes_ecb, memory_cipher_len

    head = struct.pack("<III", address, len(plain), 0xFFFFFFFF)
    confirmation = bytes([0xC4, tid, 0x01, 0x01]) + head + struct.pack("<I", binascii.crc32(head))
    padded = plain + b"\0" * (memory_cipher_len(len(plain)) - len(plain))
    cipher = aes_ecb(padded, KEY_MEMORY)
    return [confirmation] + [cipher[i:i + chunk] for i in range(0, len(cipher), chunk)]


def meter_info_replies(first_tid: int) -> list[bytes]:
    """Replies to the four MemoryReads of ``device_info()``, TIDs first_tid to first_tid + 3."""
    from km003c_mcp import crypto
    reads = [(crypto.ADDR_DEVICE_INFO, 64), (crypto.ADDR_FIRMWARE_INFO, 64),
             (crypto.ADDR_CALIBRATION, 64), (crypto.ADDR_HARDWARE_ID, crypto.HARDWARE_ID_SIZE)]
    replies: list[bytes] = []
    for k, (address, size) in enumerate(reads):
        replies += memory_read_replies(first_tid + k, address, METER_MEMORY[address][:size])
    return replies


class FakeSerial:
    """pyserial stand-in: replies are queued when the matching command line is written."""

    def __init__(self, replies: dict[str, list[bytes]]):
        self.replies = replies
        self.written: list[bytes] = []
        self.queue: list[bytes] = []
        self.closed = False

    def reset_input_buffer(self) -> None:
        self.queue.clear()

    def write(self, data: bytes) -> int:
        if self.closed:
            import serial
            raise serial.PortNotOpenError()
        self.written.append(bytes(data))
        self.queue.extend(self.replies.get(data.decode().strip(), []))
        return len(data)

    def read(self, size: int = 1) -> bytes:
        return self.queue.pop(0) if self.queue else b""

    def close(self) -> None:
        self.closed = True
