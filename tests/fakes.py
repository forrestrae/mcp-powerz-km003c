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


class MeterFake:
    """UsbTransport that answers like an idle KM003C on firmware 2.0.0.

    Each request queues its reply when written: Accept for Connect, StartGraph and the PD
    monitor commands, Reject for StopGraph, the live ADC block or an empty PutData for
    GetData, and ``03 11 00 00`` for Disconnect. ``hold_disconnect`` is an Event the
    Disconnect reply waits for. ``on_close`` runs when the session is released.
    """

    def __init__(self, serial_number="TEST", hold_disconnect: threading.Event | None = None,
                 on_close=None):
        self.serial_number = serial_number
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
        elif cmd == p.CMD_GET_DATA:
            reply = bytes([p.CMD_PUT_DATA, tid, 0, 0])
        else:
            reply = bytes([p.CMD_ACCEPT, tid, 0, 0])
        with self._lock:
            self.pending.append(reply)

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
