import threading

import pytest
import usb.core

import km003c_mcp.usb_device as ud
from fakes import FakeTransport, live_adc_with_tid, memory_read_replies
from km003c_mcp.crypto import ADDR_HARDWARE_ID
from km003c_mcp.usb_device import (
    DeviceBusyError, DeviceNotFoundError, DeviceTimeoutError, Km003cDevice, Km003cError,
    PdMonitorNotEnabledError, PyUsbTransport,
)

HWID = bytes.fromhex("3037314b42500dff110affff")


def test_connect_retries_after_disconnect_reply():
    t = FakeTransport([bytes.fromhex("03010000"), bytes.fromhex("05020000")])
    Km003cDevice(t).connect()
    assert [w[0] for w in t.writes] == [0x02, 0x02]


UNMATCHED = bytes.fromhex("41990000")


def test_disconnect_matches_reply_by_type():
    # Firmware 2.0.0 answers Disconnect with 03 11 00 00 whatever TID the request carried.
    t = FakeTransport([bytes.fromhex("03110000"), UNMATCHED])
    Km003cDevice(t).disconnect()
    assert t.writes == [bytes.fromhex("03010000")]
    assert t.script == [UNMATCHED]


def test_disconnect_accepts_same_tid_reply():
    t = FakeTransport([bytes.fromhex("05010000"), UNMATCHED])
    Km003cDevice(t).disconnect()
    assert t.script == [UNMATCHED]


def test_disconnect_ignores_timeout():
    t = FakeTransport([])
    Km003cDevice(t).disconnect()
    assert t.writes == [bytes.fromhex("03010000")]


@pytest.fixture
def no_retry_delay(monkeypatch):
    monkeypatch.setattr(ud, "PD_MONITOR_RETRY_DELAY_S", 0)


def test_enable_pd_monitor_answered_by_disconnect_then_accept(no_retry_delay):
    t = FakeTransport([bytes.fromhex("05010000"), bytes.fromhex("03020000"),
                       bytes.fromhex("05030000"), bytes.fromhex("05040000")])
    result = Km003cDevice(t).enable_pd_monitor()
    assert result.accepted and result.outcome == "accepted"
    assert result.attempts == 2 and result.last_reply == "05040000"
    assert t.writes == [bytes.fromhex("02010000"), bytes.fromhex("10020200"),
                        bytes.fromhex("02030000"), bytes.fromhex("10040200")]


def test_enable_pd_monitor_reject_is_not_fatal():
    t = FakeTransport([bytes.fromhex("05010000"), bytes.fromhex("06020000")])
    result = Km003cDevice(t).enable_pd_monitor()
    assert not result.accepted and result.outcome == "already enabled or refused"
    assert result.attempts == 1 and result.last_reply == "06020000"


def test_enable_pd_monitor_never_accepted(no_retry_delay):
    script = []
    for tid in range(1, 11, 2):
        script += [bytes([0x05, tid, 0, 0]), DeviceTimeoutError("no reply")]
    t = FakeTransport(script)
    with pytest.raises(PdMonitorNotEnabledError, match="5 attempts.*last reply none"):
        Km003cDevice(t).enable_pd_monitor()
    assert [w[0] for w in t.writes] == [0x02, 0x10] * 5


def test_enable_pd_monitor_never_accepted_reports_last_reply(no_retry_delay):
    script = []
    for tid in range(1, 11, 2):
        script += [bytes([0x05, tid, 0, 0]), bytes([0x03, tid + 1, 0, 0])]
    with pytest.raises(PdMonitorNotEnabledError, match="last reply 030a0000"):
        Km003cDevice(FakeTransport(script)).enable_pd_monitor()


class TimedTransport(FakeTransport):
    def __init__(self, script):
        super().__init__(script)
        self.read_timeouts = []

    def read(self, size, timeout_ms):
        self.read_timeouts.append(timeout_ms)
        return super().read(size, timeout_ms)


def test_enable_pd_monitor_bounds_how_long_it_holds_the_lock(monkeypatch):
    t = TimedTransport([])  # the meter never answers
    dev = Km003cDevice(t)
    free_between_attempts = []

    def sleep(_seconds):
        # Another thread, such as a stream's poller, must be able to take the lock here.
        got = []

        def probe():
            acquired = dev.lock.acquire(timeout=0.5)
            if acquired:
                dev.lock.release()
            got.append(acquired)
        thread = threading.Thread(target=probe)
        thread.start()
        thread.join()
        free_between_attempts.append(got[0])
    monkeypatch.setattr(ud.time, "sleep", sleep)
    with pytest.raises(PdMonitorNotEnabledError):
        dev.enable_pd_monitor()
    assert free_between_attempts == [True] * 4
    assert t.read_timeouts and max(t.read_timeouts) <= ud.PD_MONITOR_TIMEOUT_MS


def test_disable_pd_monitor():
    t = FakeTransport([bytes.fromhex("05010000"), bytes.fromhex("06020000")])
    dev = Km003cDevice(t)
    assert dev.disable_pd_monitor() is True
    assert dev.disable_pd_monitor() is False  # Reject: already disabled
    assert t.writes == [bytes.fromhex("11010000"), bytes.fromhex("11020000")]


def test_get_data_skips_stale_packets():
    t = FakeTransport([bytes.fromhex("05090000"), live_adc_with_tid(1)])
    r = Km003cDevice(t).read_adc()
    assert r.vdd_v == pytest.approx(3.2504)
    assert t.writes == [bytes.fromhex("0c010200")]


def test_memory_read_flow():
    plain = bytes(range(20))
    t = FakeTransport(memory_read_replies(1, 0x420, plain))
    assert Km003cDevice(t).memory_read(0x420, 20) == plain
    assert t.writes[0][:4] == bytes.fromhex("44010101")


def test_memory_read_not_readable():
    with pytest.raises(Km003cError, match="not readable"):
        Km003cDevice(FakeTransport([bytes.fromhex("27010000")])).memory_read(0x0, 16)


def test_memory_read_refused_while_streaming():
    dev = Km003cDevice(FakeTransport([]))
    dev.graph_rate = 3
    with pytest.raises(Km003cError, match="stream"):
        dev.memory_read(0x420, 16)


def test_authenticate_accepts_tid_zero_response():
    t = FakeTransport(memory_read_replies(1, ADDR_HARDWARE_ID, HWID)
                      + [bytes.fromhex("4c000302") + bytes(32)])
    dev = Km003cDevice(t)
    assert dev.authenticate() == 1 and dev.auth_level == 1
    assert t.writes[1][:4] == bytes.fromhex("4c020002")


def test_start_graph_fails_when_auth_level_zero():
    t = FakeTransport(memory_read_replies(1, ADDR_HARDWARE_ID, HWID)
                      + [bytes.fromhex("4c000102") + bytes(32)])
    with pytest.raises(Km003cError, match="streaming auth failed"):
        Km003cDevice(t).start_graph(3)


def test_start_and_stop_graph():
    t = FakeTransport([bytes.fromhex("05010000"), bytes.fromhex("06020000")])
    dev = Km003cDevice(t)
    dev.auth_level = 1
    dev.start_graph(3)
    assert dev.graph_rate == 3 and t.writes[0] == bytes.fromhex("0e010600")
    dev.stop_graph()  # Reject means "already stopped"
    assert dev.graph_rate is None


def test_timeout_when_no_reply():
    with pytest.raises(DeviceTimeoutError):
        Km003cDevice(FakeTransport([])).read_adc()


class StubUsb:
    def __init__(self, serial, fail=None):
        self.serial_number = serial
        self.fail = fail

    def set_configuration(self):
        if self.fail:
            raise self.fail


def test_open_busy_maps_to_device_busy(monkeypatch):
    err = usb.core.USBError("Access denied (insufficient permissions)", errno=13)
    monkeypatch.setattr(ud, "_find_devices", lambda: [StubUsb("A", fail=err)])
    with pytest.raises(DeviceBusyError, match="POWER-Z PC software"):
        PyUsbTransport.open()


def test_open_no_device(monkeypatch):
    monkeypatch.setattr(ud, "_find_devices", lambda: [])
    with pytest.raises(DeviceNotFoundError):
        PyUsbTransport.open()


def test_open_multiple_devices_requires_serial(monkeypatch):
    monkeypatch.setattr(ud, "_find_devices", lambda: [StubUsb("A"), StubUsb("B")])
    monkeypatch.setattr(usb.util, "claim_interface", lambda dev, intf: None)
    with pytest.raises(Km003cError, match="A.*B"):
        PyUsbTransport.open()
    assert PyUsbTransport.open("B").serial_number == "B"


class BackendCalls:
    """Records which libusb lookups _backend() makes; each returns its scripted result."""

    def __init__(self, monkeypatch, bundled=None, system=None, explicit=None):
        import libusb_package
        import usb.backend.libusb1

        self.calls = []

        def get_backend(find_library=None):
            if find_library is None:
                self.calls.append("system")
                return system
            self.calls.append(("explicit", find_library("usb-1.0")))
            return explicit

        def get_bundled():
            self.calls.append("bundled")
            return bundled

        monkeypatch.setattr(usb.backend.libusb1, "get_backend", get_backend)
        monkeypatch.setattr(libusb_package, "get_libusb1_backend", get_bundled)
        monkeypatch.delenv("KM003C_LIBUSB", raising=False)
        monkeypatch.delenv("KM003C_LIBUSB_DLL", raising=False)


def test_backend_prefers_explicit_path(monkeypatch):
    calls = BackendCalls(monkeypatch, bundled="B", explicit="E")
    monkeypatch.setenv("KM003C_LIBUSB", "/opt/lib/libusb-1.0.dylib")
    assert ud._backend() == "E"
    assert calls.calls == [("explicit", "/opt/lib/libusb-1.0.dylib")]


def test_backend_accepts_dll_alias(monkeypatch):
    calls = BackendCalls(monkeypatch, explicit="E")
    monkeypatch.setenv("KM003C_LIBUSB_DLL", r"C:\libusb-1.0.dll")
    assert ud._backend() == "E"
    assert calls.calls == [("explicit", r"C:\libusb-1.0.dll")]


def test_backend_explicit_path_that_fails_raises(monkeypatch):
    BackendCalls(monkeypatch, bundled="B", system="S", explicit=None)
    monkeypatch.setenv("KM003C_LIBUSB", "/missing/libusb-1.0.dylib")
    with pytest.raises(Km003cError, match="/missing/libusb-1.0.dylib"):
        ud._backend()


def test_backend_uses_bundled_library(monkeypatch):
    calls = BackendCalls(monkeypatch, bundled="B", system="S")
    assert ud._backend() == "B"
    assert calls.calls == ["bundled"]


def test_backend_falls_back_to_system_library(monkeypatch):
    calls = BackendCalls(monkeypatch, bundled=None, system="S")
    assert ud._backend() == "S"
    assert calls.calls == ["bundled", "system"]


def test_backend_reports_missing_libusb(monkeypatch):
    BackendCalls(monkeypatch)
    with pytest.raises(Km003cError, match="KM003C_LIBUSB"):
        ud._backend()
